#!/usr/bin/env python3
"""
OrchestraAPI Production Proxy — OpenAI-Compatible API Server
============================================================
Production proxy: Người dùng gửi API key (oct-xxx) của platform,
server validate qua MySQL DB, rồi dùng GitHub token nội bộ để
forward request tới Copilot API.

Billing bypass: X-Initiator: agent, random X-Interaction-Id per request,
persistent SESSION_ID + MACHINE_ID.

Usage:
  curl http://localhost:5000/v1/chat/completions \\
    -H "Authorization: Bearer oct-xxxYOUR_API_KEY" \\
    -H "Content-Type: application/json" \\
    -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hi"}]}'
"""

import json
import time
import uuid
import os
import logging
import asyncio
import collections
import contextvars
from typing import Optional, Dict, Tuple
from decimal import Decimal
from contextlib import asynccontextmanager
from ipaddress import ip_address, ip_network

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import aiomysql

# ═══════════════════════════════════════════════════════════════
# LOGGING  (structured with request_id via contextvars)
# ═══════════════════════════════════════════════════════════════
_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = _request_id_ctx.get("-")
        return True


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestraAPI")
logger.addFilter(_RequestIdFilter())
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] [req=%(request_id)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
_handler.addFilter(_RequestIdFilter())
logger.handlers = [_handler]
logger.propagate = False

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════
# GitHub token nội bộ — người dùng KHÔNG cần biết
GITHUB_TOKEN = os.environ.get(
    "GITHUB_TOKEN",
    "gho_xxxxxx"
)

# MySQL (same as backend)
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_USER = os.environ.get("DB_USER", "githubcopilot")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "ghcplserver")
DB_NAME = os.environ.get("DB_NAME", "api_gateway_db")

# Internal admin secret
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "orchestraapi_internal_2026")

# CORS
_cors_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
CORS_ALLOWED_ORIGINS: list[str] = [o.strip() for o in _cors_raw.split(",") if o.strip()] if _cors_raw else []

# Trusted proxies for X-Forwarded-For
_proxies_raw = os.environ.get("TRUSTED_PROXIES", "127.0.0.1,::1")
TRUSTED_PROXIES = {ip_network(p.strip(), strict=False) for p in _proxies_raw.split(",") if p.strip()}

# Rate limiting
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", 60))

# DB pool sizing
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", 5))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 30))

# API Key prefix
API_KEY_PREFIX = "oct-"

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
GITHUB_API = "https://api.github.com"
COPILOT_TOKEN_ENDPOINT = "/copilot_internal/v2/token"
GITHUB_API_VERSION = "2025-04-01"
COPILOT_API_VERSION = "2025-07-16"
USER_AGENT = "GitHubCopilotChat/0.31.5"

# Retry constants
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # exponential: 2s → 4s → 8s
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
UPSTREAM_TIMEOUT = httpx.Timeout(connect=15, read=300, write=30, pool=15)
STREAM_TIMEOUT = httpx.Timeout(connect=15, read=300, write=30, pool=15)

# ═══════════════════════════════════════════════════════════════
# BILLING BYPASS — Persistent session IDs + agent initiator
# ═══════════════════════════════════════════════════════════════
SESSION_ID = f"{uuid.uuid4()}{int(time.time() * 1000)}"
MACHINE_ID = uuid.uuid4().hex + uuid.uuid4().hex

# GPT Codex models use Responses API (POST /responses) instead of Chat Completions
GPT_CODEX_RESPONSES_MODELS = {
    "gpt-5.1-codex-mini", "gpt-5.1-codex", "gpt-5.2-codex", "gpt-5.3-codex",
    "gpt-5.1-codex-max", "gpt-5.4",
}


def _is_responses_model(model: str) -> bool:
    """Check if model requires Responses API. Supports exact match + prefix matching
    (e.g. 'gpt-5.4-2026-03-05' matches 'gpt-5.4')."""
    if model in GPT_CODEX_RESPONSES_MODELS:
        return True
    return any(model.startswith(m + "-") for m in GPT_CODEX_RESPONSES_MODELS)

# ═══════════════════════════════════════════════════════════════
# DATABASE POOL
# ═══════════════════════════════════════════════════════════════
_pool: Optional[aiomysql.Pool] = None


async def get_pool() -> aiomysql.Pool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            minsize=DB_POOL_MIN,
            maxsize=DB_POOL_MAX,
            pool_recycle=3600,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool and not _pool.closed:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


# ═══════════════════════════════════════════════════════════════
# COPILOT TOKEN CACHE  (github_token -> (copilot_token, api_base, expires_at))
# Double-check locking to prevent thundering herd on token refresh
# ═══════════════════════════════════════════════════════════════
_token_cache: Dict[str, Tuple[str, str, int]] = {}
_token_lock = asyncio.Lock()


async def exchange_token() -> Tuple[str, str]:
    """Đổi GitHub token nội bộ -> Copilot session token + api_base.
    Cache trong RAM, auto-refresh khi hết hạn.
    Uses double-check locking to prevent concurrent API calls."""
    # Fast path: check cache without lock
    cached = _token_cache.get(GITHUB_TOKEN)
    if cached:
        copilot_token, api_base, expires_at = cached
        if time.time() < expires_at - 300:
            return copilot_token, api_base

    # Slow path: acquire lock, check again, then call API if needed
    async with _token_lock:
        cached = _token_cache.get(GITHUB_TOKEN)
        if cached:
            copilot_token, api_base, expires_at = cached
            if time.time() < expires_at - 300:
                return copilot_token, api_base

        url = f"{GITHUB_API}{COPILOT_TOKEN_ENDPOINT}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": USER_AGENT,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15)

        if resp.status_code == 401:
            logger.error("GitHub token invalid — cannot exchange for Copilot token")
            raise HTTPException(status_code=502, detail={
                "error": {
                    "message": "Internal GitHub token is invalid. Contact admin.",
                    "type": "server_error",
                }
            })
        if resp.status_code != 200:
            logger.error(f"GitHub token exchange failed: HTTP {resp.status_code}")
            raise HTTPException(status_code=502, detail={
                "error": {
                    "message": f"Copilot token exchange failed (HTTP {resp.status_code})",
                    "type": "server_error",
                }
            })

        data = resp.json()
        copilot_token = data["token"]
        api_base = data.get("endpoints", {}).get("api", "https://api.individual.githubcopilot.com")
        expires_at = data.get("expires_at", 0)

        _token_cache[GITHUB_TOKEN] = (copilot_token, api_base, expires_at)
        return copilot_token, api_base


# ═══════════════════════════════════════════════════════════════
# USER AUTHENTICATION — validate oct-xxx API key against DB
# ═══════════════════════════════════════════════════════════════

async def require_auth(request: Request) -> dict:
    """Extract API key từ Authorization header, validate against DB.
    Returns user dict: {id, username, credit, is_active}"""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={
            "error": {
                "message": "Missing Authorization header. Expected: Bearer oct-xxxYOUR_API_KEY",
                "type": "authentication_error",
                "code": "missing_api_key",
            }
        })

    api_key = auth[7:].strip()
    if not api_key or not api_key.startswith(API_KEY_PREFIX):
        raise HTTPException(status_code=401, detail={
            "error": {
                "message": f"Invalid API key format. Keys must start with '{API_KEY_PREFIX}'",
                "type": "authentication_error",
                "code": "invalid_api_key",
            }
        })

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, username, credit, is_active FROM users WHERE api_key = %s",
                (api_key,),
            )
            user = await cur.fetchone()

    if not user:
        raise HTTPException(status_code=401, detail={
            "error": {
                "message": "Invalid API key. Please check your key or generate a new one.",
                "type": "authentication_error",
                "code": "invalid_api_key",
            }
        })

    if not user["is_active"]:
        raise HTTPException(status_code=403, detail={
            "error": {
                "message": "Account disabled. Contact admin.",
                "type": "permission_error",
                "code": "account_disabled",
            }
        })

    if float(user["credit"]) <= 0:
        raise HTTPException(status_code=402, detail={
            "error": {
                "message": "Insufficient credit. Please top up your account.",
                "type": "billing_error",
                "code": "insufficient_credit",
            }
        })

    return user


# ═══════════════════════════════════════════════════════════════
# RATE LIMITING — in-memory sliding window per user
# ═══════════════════════════════════════════════════════════════
_rate_windows: Dict[int, collections.deque] = {}


def _check_rate_limit(user_id: int):
    """Enforce per-user rate limit (sliding window, requests per minute).
    Raises HTTP 429 if exceeded."""
    now = time.time()
    window = _rate_windows.get(user_id)
    if window is None:
        window = collections.deque()
        _rate_windows[user_id] = window

    # Purge entries older than 60s
    cutoff = now - 60.0
    while window and window[0] < cutoff:
        window.popleft()

    if len(window) >= RATE_LIMIT_RPM:
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": f"Rate limit exceeded: {RATE_LIMIT_RPM} requests per minute. Please slow down.",
                    "type": "rate_limit_error",
                    "code": "rate_limit_exceeded",
                }
            },
            headers={"Retry-After": "10"},
        )
    window.append(now)


# ═══════════════════════════════════════════════════════════════
# BILLING — calculate cost, deduct credit, log usage
# ═══════════════════════════════════════════════════════════════

async def get_model_pricing(model_id: str) -> Optional[dict]:
    """Lấy pricing từ DB. Returns {input_price, output_price} or None.
    Prices returned are already discounted if discount_percent column exists."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            try:
                await cur.execute(
                    "SELECT input_price, output_price, discount_percent FROM models WHERE model_id = %s AND is_active = TRUE",
                    (model_id,),
                )
            except Exception:
                # Fallback if discount_percent column doesn't exist yet
                await cur.execute(
                    "SELECT input_price, output_price FROM models WHERE model_id = %s AND is_active = TRUE",
                    (model_id,),
                )
            row = await cur.fetchone()
            if not row:
                return None
            discount = Decimal(str(row.get("discount_percent") or 0))
            multiplier = Decimal("1") - discount / Decimal("100")
            return {
                "input_price": Decimal(str(row["input_price"])) * multiplier,
                "output_price": Decimal(str(row["output_price"])) * multiplier,
            }


def calculate_cost(prompt_tokens: int, completion_tokens: int,
                   input_price, output_price) -> Decimal:
    """Tính chi phí: giá tính theo 1K tokens. Uses Decimal for precision."""
    inp = Decimal(str(input_price))
    out = Decimal(str(output_price))
    cost = (Decimal(prompt_tokens) * inp + Decimal(completion_tokens) * out) / Decimal("1000")
    return cost.quantize(Decimal("0.000001"))


async def log_usage_and_deduct(user_id: int, model_id: str,
                                prompt_tokens: int, completion_tokens: int,
                                total_cost, ip_address: str) -> bool:
    """Ghi usage_history + trừ credit từ user.
    Uses transaction + conditional UPDATE to prevent negative credit.
    Returns True if deduction succeeded, False if insufficient credit."""
    cost = Decimal(str(total_cost))
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Use explicit transaction (disable autocommit temporarily)
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                # Insert usage history
                await cur.execute(
                    """INSERT INTO usage_history
                       (user_id, model_id, prompt_tokens, completion_tokens, total_cost, ip_address)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (user_id, model_id, prompt_tokens, completion_tokens, str(cost), ip_address),
                )
                # Conditional deduct: only if user has enough credit
                await cur.execute(
                    "UPDATE users SET credit = credit - %s WHERE id = %s AND credit >= %s",
                    (str(cost), user_id, str(cost)),
                )
                if cur.rowcount == 0:
                    await conn.rollback()
                    logger.warning(f"Insufficient credit for deduction: user={user_id} cost={cost}")
                    return False
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    logger.info(
        f"Usage: user={user_id} model={model_id} "
        f"prompt={prompt_tokens} completion={completion_tokens} cost={cost}"
    )
    return True


def get_client_ip(request: Request) -> str:
    """Lấy IP thật của client. Only trusts X-Forwarded-For when direct
    connection comes from a trusted proxy."""
    direct_ip = request.client.host if request.client else "unknown"

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and direct_ip != "unknown":
        try:
            addr = ip_address(direct_ip)
            is_trusted = any(addr in net for net in TRUSTED_PROXIES)
        except ValueError:
            is_trusted = False
        if is_trusted:
            return forwarded.split(",")[0].strip()

    return direct_ip


# ═══════════════════════════════════════════════════════════════
# INPUT VALIDATION
# ═══════════════════════════════════════════════════════════════

def _validate_chat_body(body: dict):
    """Validate Chat Completions request body per OpenAI API spec.
    Raises HTTPException(400) on invalid input."""
    model = body.get("model")
    if not model or not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail={
            "error": {"message": "'model' is required and must be a non-empty string.", "type": "invalid_request_error"}
        })

    messages = body.get("messages")
    if messages is None or not isinstance(messages, list) or len(messages) == 0:
        raise HTTPException(status_code=400, detail={
            "error": {"message": "'messages' is required and must be a non-empty list.", "type": "invalid_request_error"}
        })

    max_tokens = body.get("max_tokens")
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            raise HTTPException(status_code=400, detail={
                "error": {"message": "'max_tokens' must be a positive integer.", "type": "invalid_request_error"}
            })
        if max_tokens > 128000:
            body["max_tokens"] = 128000

    temperature = body.get("temperature")
    if temperature is not None:
        if not isinstance(temperature, (int, float)) or temperature < 0 or temperature > 2:
            raise HTTPException(status_code=400, detail={
                "error": {"message": "'temperature' must be between 0 and 2.", "type": "invalid_request_error"}
            })

    stream = body.get("stream")
    if stream is not None and not isinstance(stream, bool):
        raise HTTPException(status_code=400, detail={
            "error": {"message": "'stream' must be a boolean.", "type": "invalid_request_error"}
        })



# ═══════════════════════════════════════════════════════════════
# COPILOT HEADERS (billing bypass)
# ═══════════════════════════════════════════════════════════════

def copilot_headers(copilot_token: str) -> dict:
    """Build headers cho request tới Copilot upstream.
    X-Initiator: agent + persistent session IDs, random X-Interaction-Id."""
    return {
        "Authorization": f"Bearer {copilot_token}",
        "X-Request-Id": str(uuid.uuid4()),
        "X-Interaction-Type": "conversation-agent",
        "OpenAI-Intent": "conversation-agent",
        "X-Interaction-Id": str(uuid.uuid4()),
        "X-Initiator": "agent",
        "VScode-SessionId": SESSION_ID,
        "VScode-MachineId": MACHINE_ID,
        "X-GitHub-Api-Version": COPILOT_API_VERSION,
        "Editor-Plugin-Version": "copilot-chat/0.31.5",
        "Editor-Version": "vscode/1.104.1",
        "Copilot-Integration-Id": "vscode-chat",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


# ═══════════════════════════════════════════════════════════════
# RETRY HELPER — shared by streaming & non-streaming paths
# ═══════════════════════════════════════════════════════════════

async def _request_with_retry(method: str, url: str, headers: dict,
                              json_body: dict, timeout: httpx.Timeout,
                              stream: bool = False):
    """Execute an HTTP request with exponential backoff retry.
    For non-streaming: returns httpx.Response.
    For streaming: returns an async context manager (client, response).
    Retries on RETRYABLE_STATUS_CODES and connection/timeout errors.
    Refreshes Copilot token between retries."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            if stream:
                client = httpx.AsyncClient()
                resp_cm = client.stream(method, url, headers=headers, json=json_body, timeout=timeout)
                resp = await resp_cm.__aenter__()
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    await resp.aclose()
                    await client.aclose()
                    if attempt < MAX_RETRIES - 1:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        logger.warning(f"Retry {attempt+1}/{MAX_RETRIES}: upstream {resp.status_code}, sleeping {delay}s")
                        await asyncio.sleep(delay)
                        # Refresh token for next attempt
                        copilot_token, _ = await exchange_token()
                        headers = {**headers, "Authorization": f"Bearer {copilot_token}"}
                        continue
                    # Last attempt — return error response
                    return None, None, resp.status_code
                return client, resp, resp.status_code
            else:
                async with httpx.AsyncClient() as client:
                    resp = await client.request(method, url, headers=headers, json=json_body, timeout=timeout)
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    if attempt < MAX_RETRIES - 1:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        logger.warning(f"Retry {attempt+1}/{MAX_RETRIES}: upstream {resp.status_code}, sleeping {delay}s")
                        await asyncio.sleep(delay)
                        copilot_token, _ = await exchange_token()
                        headers = {**headers, "Authorization": f"Bearer {copilot_token}"}
                        continue
                return resp
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"Retry {attempt+1}/{MAX_RETRIES}: {type(exc).__name__}, sleeping {delay}s")
                await asyncio.sleep(delay)
                copilot_token, _ = await exchange_token()
                headers = {**headers, "Authorization": f"Bearer {copilot_token}"}
                continue

    # All retries exhausted
    if stream:
        return None, None, 504
    raise HTTPException(status_code=504, detail={
        "error": {
            "message": f"Upstream request failed after {MAX_RETRIES} retries: {type(last_exc).__name__}",
            "type": "server_error",
        }
    })


# ═══════════════════════════════════════════════════════════════
# RESPONSE CLEANING (strip Copilot-specific metadata)
# ═══════════════════════════════════════════════════════════════

def _clean_delta(delta: dict) -> dict:
    """Keep only standard OpenAI fields in delta."""
    clean = {}
    if "role" in delta:
        clean["role"] = delta["role"]
    if "content" in delta:
        clean["content"] = delta["content"]
    if "tool_calls" in delta:
        clean["tool_calls"] = delta["tool_calls"]
    if "function_call" in delta:
        clean["function_call"] = delta["function_call"]
    if "refusal" in delta:
        clean["refusal"] = delta["refusal"]
    if "reasoning_text" in delta:
        clean["reasoning_text"] = delta["reasoning_text"]
    return clean


def clean_sse_chunk(raw: dict) -> Optional[dict]:
    """Clean 1 SSE chunk: strip Copilot-specific metadata,
    keep standard OpenAI SDK format."""
    choices = raw.get("choices", [])
    if not choices:
        return None

    clean_choices = []
    for c in choices:
        delta = c.get("delta", {})
        finish = c.get("finish_reason")

        has_useful = (
            delta.get("content") is not None
            or delta.get("role") is not None
            or "tool_calls" in delta
            or "function_call" in delta
            or delta.get("reasoning_text") is not None
            or finish is not None
        )
        if not has_useful:
            continue

        out = {
            "index": c.get("index", 0),
            "delta": _clean_delta(delta),
            "logprobs": c.get("logprobs", None),
            "finish_reason": finish,
        }
        clean_choices.append(out)

    if not clean_choices:
        return None

    result = {
        "id": raw.get("id", ""),
        "object": "chat.completion.chunk",
        "created": raw.get("created", int(time.time())),
        "model": raw.get("model", ""),
        "system_fingerprint": raw.get("system_fingerprint", None),
        "choices": clean_choices,
    }
    if "usage" in raw:
        result["usage"] = raw["usage"]
    return result


def _clean_message(msg: dict) -> dict:
    """Keep only standard OpenAI fields in message."""
    clean = {
        "role": msg.get("role", "assistant"),
        "content": msg.get("content"),
    }
    if msg.get("tool_calls"):
        clean["tool_calls"] = msg["tool_calls"]
    if msg.get("function_call"):
        clean["function_call"] = msg["function_call"]
    if msg.get("refusal") is not None:
        clean["refusal"] = msg["refusal"]
    else:
        clean["refusal"] = None
    return clean


def clean_response(raw: dict) -> dict:
    """Clean non-streaming response: strip Copilot metadata, keep standard OpenAI SDK format."""
    clean_choices = []
    for c in raw.get("choices", []):
        msg = c.get("message", {})
        finish = c.get("finish_reason", "stop")

        clean_choices.append({
            "index": c.get("index", 0),
            "message": _clean_message(msg),
            "logprobs": c.get("logprobs", None),
            "finish_reason": finish,
        })
    return {
        "id": raw.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}"),
        "object": "chat.completion",
        "created": raw.get("created", int(time.time())),
        "model": raw.get("model", ""),
        "system_fingerprint": raw.get("system_fingerprint", None),
        "choices": clean_choices,
        "usage": raw.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    }


# ═══════════════════════════════════════════════════════════════
# RESPONSES API CONVERSION (GPT Codex models)
# ═══════════════════════════════════════════════════════════════

def convert_messages_to_responses_input(body: dict) -> dict:
    """Convert Chat Completions request body to Responses API format.
    messages → input, tools format conversion, parameter mapping."""
    messages = body.get("messages", [])
    input_items = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            input_items.append({
                "role": "system",
                "content": [{"type": "input_text", "text": content or ""}],
            })
        elif role == "user":
            input_items.append({
                "role": "user",
                "content": [{"type": "input_text", "text": content or ""}],
            })
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                if content:
                    input_items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    input_items.append({
                        "type": "function_call",
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                        "call_id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                    })
            else:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content or ""}],
                })
        elif role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": content or "",
            })

    resp_body: dict = {
        "model": body.get("model", ""),
        "input": input_items,
        "stream": body.get("stream", False),
        "store": False,
        "truncation": "disabled",
        "reasoning": {"summary": "detailed"},
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": body.get("max_tokens", 128000),
    }

    if body.get("tools"):
        resp_tools = []
        for t in body["tools"]:
            if t.get("type") == "function":
                fn = t.get("function", {})
                resp_tools.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                    "strict": False,
                })
        if resp_tools:
            resp_body["tools"] = resp_tools

    if body.get("temperature") is not None:
        resp_body["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        resp_body["top_p"] = body["top_p"]

    return resp_body


def convert_responses_sse_to_chat(event_data: dict, model: str,
                                  response_id: str = "",
                                  _tc_index: dict = None) -> Optional[dict]:
    """Convert a single Responses API SSE event to a Chat Completions SSE chunk.
    Returns None for events that should be skipped."""
    event_type = event_data.get("type", "")
    created = int(time.time())

    if not response_id:
        response_id = event_data.get("response_id", f"chatcmpl-{uuid.uuid4().hex[:12]}")

    def _make_chunk(delta: dict, finish_reason: Optional[str] = None) -> dict:
        return {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "system_fingerprint": None,
            "choices": [{
                "index": 0,
                "delta": delta,
                "logprobs": None,
                "finish_reason": finish_reason,
            }],
        }

    if event_type == "response.output_text.delta":
        return _make_chunk({"content": event_data.get("delta", "")})

    elif event_type == "response.output_item.added":
        item = event_data.get("item", {})
        if item.get("type") == "function_call":
            if _tc_index is not None:
                idx = _tc_index.get("next", 0)
                _tc_index["next"] = idx + 1
                _tc_index[item.get("call_id", "")] = idx
            else:
                idx = 0
            return _make_chunk({
                "role": "assistant",
                "tool_calls": [{
                    "index": idx,
                    "id": item.get("call_id", f"call_{uuid.uuid4().hex[:24]}"),
                    "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": ""},
                }],
            })

    elif event_type == "response.function_call_arguments.delta":
        call_id = event_data.get("call_id", "")
        idx = 0
        if _tc_index is not None and call_id in _tc_index:
            idx = _tc_index[call_id]
        return _make_chunk({
            "tool_calls": [{
                "index": idx,
                "function": {"arguments": event_data.get("delta", "")},
            }],
        })

    elif event_type == "response.completed":
        resp = event_data.get("response", {})
        output = resp.get("output", [])
        has_function_call = any(item.get("type") == "function_call" for item in output)
        finish_reason = "tool_calls" if has_function_call else "stop"
        chunk = _make_chunk({}, finish_reason)
        usage = resp.get("usage", {})
        if usage:
            chunk["usage"] = {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            }
        return chunk

    return None


def convert_responses_full_to_chat(resp_data: dict, model: str) -> dict:
    """Convert Responses API non-streaming response to Chat Completions format."""
    output = resp_data.get("output", [])
    content_parts = []
    tool_calls = []
    tc_idx = 0

    for item in output:
        item_type = item.get("type", "")
        if item_type == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    content_parts.append(c.get("text", ""))
        elif item_type == "function_call":
            tool_calls.append({
                "id": item.get("call_id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            })
            tc_idx += 1

    message: dict = {
        "role": "assistant",
        "content": "\n".join(content_parts) if content_parts else None,
        "refusal": None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason = "tool_calls" if tool_calls else "stop"

    usage = resp_data.get("usage", {})
    return {
        "id": resp_data.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": None,
        "choices": [{
            "index": 0,
            "message": message,
            "logprobs": None,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


# ═══════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app):
    # Startup: init DB pool + pre-warm Copilot token cache
    await get_pool()
    logger.info("Database pool initialized")
    try:
        await exchange_token()
        logger.info("Copilot token pre-warmed successfully")
    except Exception as exc:
        logger.warning(f"Failed to pre-warm Copilot token (will retry on first request): {exc}")
    yield
    # Shutdown
    logger.info("Shutting down OrchestraAPI...")
    await close_pool()
    logger.info("Database pool closed — shutdown complete")


app = FastAPI(title="OrchestraAPI Production Proxy", version="3.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Inject request_id into contextvars for structured logging."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    _request_id_ctx.set(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.get("/")
def root():
    return {
        "status": "running",
        "service": "OrchestraAPI Production Proxy",
        "usage": "Set api_key to your oct-xxx API key, base_url to http://localhost:{port}/v1",
    }


# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK — unauthenticated, for load balancers
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    """Health endpoint for load balancers. Checks DB + token cache."""
    checks = {}
    healthy = True

    # Check DB
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        healthy = False

    # Check token cache
    cached = _token_cache.get(GITHUB_TOKEN)
    if cached:
        _, _, expires_at = cached
        remaining = int(expires_at - time.time())
        checks["copilot_token"] = f"cached, expires in {remaining}s"
        if remaining < 60:
            checks["copilot_token"] += " (expiring soon)"
    else:
        checks["copilot_token"] = "not cached"

    status_code = 200 if healthy else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "healthy" if healthy else "degraded", "checks": checks},
    )


@app.get("/v1/models")
async def list_models(request: Request):
    user = await require_auth(request)
    copilot_token, api_base = await exchange_token()

    headers = copilot_headers(copilot_token)
    headers["X-Interaction-Type"] = "model-access"
    headers["OpenAI-Intent"] = "model-access"

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{api_base}/models", headers=headers, timeout=15)
    if resp.status_code != 200:
        return JSONResponse(status_code=resp.status_code, content={"error": {"message": resp.text[:500]}})

    raw = resp.json()
    models = []
    for m in raw.get("data", []):
        models.append({
            "id": m.get("id", ""),
            "object": "model",
            "created": m.get("created", int(time.time())),
            "owned_by": m.get("vendor", "github-copilot"),
        })
    return JSONResponse(content={"object": "list", "data": models})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # 1. Validate API key & get user info
    user = await require_auth(request)
    user_id = user["id"]
    client_ip = get_client_ip(request)

    # 1b. Rate limiting
    _check_rate_limit(user_id)

    # 2. Parse request body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON body"}})

    # 2b. Validate input
    _validate_chat_body(body)

    model_id = body.get("model", "")

    # Default max_tokens to 32000 if not specified by user
    if "max_tokens" not in body and "max_completion_tokens" not in body:
        body["max_tokens"] = 32000

    # 3. Get model pricing — reject unknown models
    pricing = await get_model_pricing(model_id)
    if pricing is None:
        raise HTTPException(status_code=400, detail={
            "error": {
                "message": f"Model '{model_id}' is not available or not configured. Check /v1/models for available models.",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        })
    input_price = pricing["input_price"]
    output_price = pricing["output_price"]

    # 4. Exchange GitHub token → Copilot token
    copilot_token, api_base = await exchange_token()

    # 5. Build headers
    headers = copilot_headers(copilot_token)
    is_stream = body.get("stream", False)

    # ── GPT Codex models → Responses API ──────────────────────
    if _is_responses_model(model_id):
        resp_body = convert_messages_to_responses_input(body)
        url = f"{api_base}/responses"

        if is_stream:
            async def generate_responses():
                billed = False

                async def _bill_once(udata):
                    nonlocal billed
                    if billed:
                        return
                    if udata["total_tokens"] > 0:
                        cost = calculate_cost(
                            udata["prompt_tokens"],
                            udata["completion_tokens"],
                            input_price, output_price,
                        )
                        await log_usage_and_deduct(
                            user_id, model_id,
                            udata["prompt_tokens"],
                            udata["completion_tokens"],
                            cost, client_ip,
                        )
                        billed = True

                usage_data = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                tc_index: dict = {"next": 0}
                response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

                # Connect with retry
                s_client, s_resp, status = await _request_with_retry(
                    "POST", url, headers, resp_body, STREAM_TIMEOUT, stream=True
                )
                if s_client is None:
                    # All retries failed
                    chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_id,
                        "choices": [{"index": 0, "delta": {"content": f"[Error {status}] Upstream unavailable after {MAX_RETRIES} retries"}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                # Send initial role chunk
                yield f'data: {json.dumps({"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model_id, "choices": [{"index": 0, "delta": {"role": "assistant"}, "logprobs": None, "finish_reason": None}]})}\n\n'

                try:
                    buf = ""
                    async for raw_bytes in s_resp.aiter_bytes():
                        if not raw_bytes:
                            continue
                        buf += raw_bytes.decode("utf-8", errors="replace")
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("event:"):
                                continue
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:]
                            if payload.strip() == "[DONE]":
                                await _bill_once(usage_data)
                                yield "data: [DONE]\n\n"
                                return
                            try:
                                event_data = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            converted = convert_responses_sse_to_chat(
                                event_data, model_id, response_id, tc_index
                            )
                            if converted:
                                if "usage" in converted and converted["usage"]:
                                    usage_data = converted["usage"]
                                yield f"data: {json.dumps(converted, ensure_ascii=False)}\n\n"

                    # Flush remaining buffer
                    for leftover in buf.split("\n"):
                        leftover = leftover.strip()
                        if leftover.startswith("data: "):
                            p = leftover[6:]
                            if p.strip() == "[DONE]":
                                await _bill_once(usage_data)
                                yield "data: [DONE]\n\n"
                                return
                            try:
                                event_data = json.loads(p)
                                converted = convert_responses_sse_to_chat(
                                    event_data, model_id, response_id, tc_index
                                )
                                if converted:
                                    if "usage" in converted and converted["usage"]:
                                        usage_data = converted["usage"]
                                    yield f"data: {json.dumps(converted, ensure_ascii=False)}\n\n"
                            except json.JSONDecodeError:
                                pass

                    # Stream ended without [DONE]
                    await _bill_once(usage_data)
                    yield "data: [DONE]\n\n"
                except httpx.ReadTimeout:
                    await _bill_once(usage_data)
                    yield f'data: {{"error":"timeout"}}\n\n'
                    yield "data: [DONE]\n\n"
                finally:
                    await s_resp.aclose()
                    await s_client.aclose()

            return StreamingResponse(generate_responses(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        else:
            # Non-streaming Responses API with retry
            resp = await _request_with_retry("POST", url, headers, resp_body, UPSTREAM_TIMEOUT)

            if resp.status_code != 200:
                try:
                    err = resp.json()
                except Exception:
                    err = {"error": {"message": resp.text[:500]}}
                return JSONResponse(status_code=resp.status_code, content=err)

            raw_data = resp.json()
            cleaned = convert_responses_full_to_chat(raw_data, model_id)

            # Log usage & deduct credit
            usage = cleaned.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            if prompt_tokens > 0 or completion_tokens > 0:
                cost = calculate_cost(prompt_tokens, completion_tokens, input_price, output_price)
                await log_usage_and_deduct(
                    user_id, model_id,
                    prompt_tokens, completion_tokens,
                    cost, client_ip,
                )

            return JSONResponse(content=cleaned)

    # ── Standard models → Chat Completions API ────────────────
    # 5. Build headers & forward
    url = f"{api_base}/chat/completions"
    is_stream = body.get("stream", False)

    if is_stream:
        async def generate():
            billed = False

            async def _bill_once(udata):
                nonlocal billed
                if billed:
                    return
                if udata["total_tokens"] > 0:
                    cost = calculate_cost(
                        udata["prompt_tokens"],
                        udata["completion_tokens"],
                        input_price, output_price,
                    )
                    await log_usage_and_deduct(
                        user_id, model_id,
                        udata["prompt_tokens"],
                        udata["completion_tokens"],
                        cost, client_ip,
                    )
                    billed = True

            usage_data = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            streamed_content_chars = 0

            # Connect with retry
            s_client, s_resp, status = await _request_with_retry(
                "POST", url, headers, body, STREAM_TIMEOUT, stream=True
            )
            if s_client is None:
                chunk = {
                    "id": "error",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"content": f"[Error {status}] Upstream unavailable after {MAX_RETRIES} retries"}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return

            try:
                buf = ""
                async for raw_bytes in s_resp.aiter_bytes():
                    if not raw_bytes:
                        continue
                    buf += raw_bytes.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            await _bill_once(usage_data)
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            raw = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        # Capture usage from chunks (usually in the last chunk)
                        if "usage" in raw and raw["usage"]:
                            usage_data = raw["usage"]

                        # Track content chars for fallback estimation
                        delta = raw.get("choices", [{}])[0].get("delta", {})
                        if delta.get("content"):
                            streamed_content_chars += len(delta["content"])

                        cleaned = clean_sse_chunk(raw)
                        if cleaned:
                            yield f"data: {json.dumps(cleaned, ensure_ascii=False)}\n\n"

                # Flush remaining buffer
                for leftover in buf.split("\n"):
                    leftover = leftover.strip()
                    if leftover.startswith("data: "):
                        p = leftover[6:]
                        if p.strip() == "[DONE]":
                            await _bill_once(usage_data)
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            raw = json.loads(p)
                            if "usage" in raw and raw["usage"]:
                                usage_data = raw["usage"]
                            delta = raw.get("choices", [{}])[0].get("delta", {})
                            if delta.get("content"):
                                streamed_content_chars += len(delta["content"])
                            cleaned = clean_sse_chunk(raw)
                            if cleaned:
                                yield f"data: {json.dumps(cleaned, ensure_ascii=False)}\n\n"
                        except json.JSONDecodeError:
                            pass

                # Stream ended without [DONE]
                await _bill_once(usage_data)
                yield "data: [DONE]\n\n"
            except httpx.ReadTimeout:
                # Timeout — bill for what was generated
                self_usage = usage_data if usage_data["total_tokens"] > 0 else None
                if not self_usage and streamed_content_chars > 0:
                    # Estimate: ~3 chars per token (conservative)
                    est_completion = max(1, streamed_content_chars // 3)
                    est_prompt = body.get("_est_prompt_tokens", 100)
                    self_usage = {"prompt_tokens": est_prompt, "completion_tokens": est_completion, "total_tokens": est_prompt + est_completion}
                    logger.info(f"Timeout — estimated tokens from {streamed_content_chars} chars: ~{est_completion} completion")
                if self_usage:
                    await _bill_once(self_usage)
                yield f'data: {{"error":"timeout"}}\n\n'
                yield "data: [DONE]\n\n"
            except Exception as exc:
                # Connection killed by upstream (e.g. Copilot hard limit)
                logger.warning(f"Stream interrupted: {type(exc).__name__}: {exc}")
                self_usage = usage_data if usage_data["total_tokens"] > 0 else None
                if not self_usage and streamed_content_chars > 0:
                    est_completion = max(1, streamed_content_chars // 3)
                    est_prompt = body.get("_est_prompt_tokens", 100)
                    self_usage = {"prompt_tokens": est_prompt, "completion_tokens": est_completion, "total_tokens": est_prompt + est_completion}
                    logger.warning(f"Estimated tokens from {streamed_content_chars} chars: ~{est_completion} completion")
                if self_usage:
                    await _bill_once(self_usage)
                yield "data: [DONE]\n\n"
            finally:
                await s_resp.aclose()
                await s_client.aclose()

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    else:
        # Non-streaming with retry
        resp = await _request_with_retry("POST", url, headers, body, UPSTREAM_TIMEOUT)

        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = {"error": {"message": resp.text[:500]}}
            return JSONResponse(status_code=resp.status_code, content=err)

        raw_data = resp.json()
        cleaned = clean_response(raw_data)

        # Log usage & deduct credit
        usage = raw_data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        if prompt_tokens > 0 or completion_tokens > 0:
            cost = calculate_cost(prompt_tokens, completion_tokens, input_price, output_price)
            await log_usage_and_deduct(
                user_id, model_id,
                prompt_tokens, completion_tokens,
                cost, client_ip,
            )

        return JSONResponse(content=cleaned)


# ═══════════════════════════════════════════════════════════════
# ADMIN: Update GitHub token at runtime (optional)
# ═══════════════════════════════════════════════════════════════

@app.post("/internal/update-github-token")
async def update_github_token(request: Request):
    """Cho phép admin update GitHub token mà không cần restart server.
    Gọi nội bộ, không expose ra ngoài."""
    secret = request.headers.get("x-internal-secret", "")
    if secret != INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    new_token = body.get("github_token", "").strip()
    if not new_token:
        raise HTTPException(status_code=400, detail="Missing github_token")

    global GITHUB_TOKEN, _token_cache
    GITHUB_TOKEN = new_token
    _token_cache.clear()
    logger.info("GitHub token updated at runtime")
    return {"message": "Token updated successfully"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5002))
    logger.info(f"""
╔══════════════════════════════════════════════════════╗
║  OrchestraAPI Production Proxy Server                ║
║  http://127.0.0.1:{port}/v1{' ' * (37 - len(str(port)))}║
╠══════════════════════════════════════════════════════╣
║  GET  /v1/models            - List models            ║
║  POST /v1/chat/completions  - Chat (stream & sync)   ║
║  GET  /health               - Health check           ║
╠══════════════════════════════════════════════════════╣
║  Authorization: Bearer oct-xxxYOUR_API_KEY           ║
║  (GitHub token is managed internally by server)      ║
╚══════════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=port)
