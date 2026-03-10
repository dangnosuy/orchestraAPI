#!/usr/bin/env python3
"""
OrchestraAPI Backend — Auth, User, Admin API
============================================
JWT-based authentication with MySQL database.
"""
import os
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import aiomysql

from config import BACKEND_PORT, FRONTEND_URL
from database import get_pool, close_pool
from auth import (
    hash_password,
    verify_password,
    create_access_token,
    decode_token,
    require_token,
    require_admin,
    generate_api_key,
)
from paypal import create_order as paypal_create, capture_order as paypal_capture, verify_webhook_signature

# ═══════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str


class LoginRequest(BaseModel):
    login: str
    password: str


class UpdateProfileRequest(BaseModel):
    username: Optional[str] = None


class AdminModelRequest(BaseModel):
    model_id: str
    name: str
    input_price: float
    output_price: float
    is_active: bool = True
    discount_percent: float = 0.0


class ToggleModelRequest(BaseModel):
    is_active: bool


class BulkDiscountRequest(BaseModel):
    model_ids: list[str]
    discount_percent: float


# ═══════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════
app = FastAPI(title="OrchestraAPI Backend", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await get_pool()


@app.on_event("shutdown")
async def shutdown():
    await close_pool()


@app.get("/")
def root():
    return {"status": "running", "service": "OrchestraAPI Backend"}


# ═══════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.post("/api/auth/register")
async def register(body: RegisterRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT id FROM users WHERE username = %s", (body.username,))
            if await cur.fetchone():
                raise HTTPException(status_code=400, detail="Username already exists")

            await cur.execute("SELECT id FROM users WHERE email = %s", (body.email,))
            if await cur.fetchone():
                raise HTTPException(status_code=400, detail="Email already registered")

            if len(body.username) < 3:
                raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
            if len(body.password) < 6:
                raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

            pw_hash = hash_password(body.password)

            # api_key starts as NULL - user creates it manually later
            await cur.execute(
                "INSERT INTO users (username, email, password_hash, api_key, role, credit) VALUES (%s, %s, %s, NULL, 'user', 0)",
                (body.username, body.email, pw_hash),
            )
            user_id = cur.lastrowid

    token = create_access_token(user_id, "user")
    return {
        "message": "Registration successful",
        "token": token,
        "user": {
            "id": user_id,
            "username": body.username,
            "email": body.email,
            "role": "user",
            "api_key": None,
        },
    }


@app.post("/api/auth/login")
async def login(body: LoginRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT * FROM users WHERE username = %s OR email = %s",
                (body.login, body.login),
            )
            user = await cur.fetchone()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or email")

    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid password")

    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token(user["id"], user["role"])
    return {
        "message": "Login successful",
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "role": user["role"],
            "api_key": user["api_key"],
            "credit": float(user["credit"]),
        },
    }


# ═══════════════════════════════════════════════════════════════
# USER ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/user/profile")
async def get_profile(request: Request):
    payload = require_token(request)
    user_id = int(payload["sub"])

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, username, email, api_key, role, credit, is_active, created_at FROM users WHERE id = %s",
                (user_id,),
            )
            user = await cur.fetchone()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "api_key": user["api_key"],
        "role": user["role"],
        "credit": float(user["credit"]),
        "is_active": user["is_active"],
        "created_at": user["created_at"].isoformat() if user["created_at"] else None,
    }


@app.put("/api/user/profile")
async def update_profile(request: Request, body: UpdateProfileRequest):
    payload = require_token(request)
    user_id = int(payload["sub"])

    if not body.username:
        raise HTTPException(status_code=400, detail="New username required")

    if len(body.username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id FROM users WHERE username = %s AND id != %s",
                (body.username, user_id),
            )
            if await cur.fetchone():
                raise HTTPException(status_code=400, detail="Username already taken")

            await cur.execute(
                "UPDATE users SET username = %s WHERE id = %s",
                (body.username, user_id),
            )

    return {"message": "Updated successfully", "username": body.username}


@app.post("/api/user/regenerate-key")
async def regenerate_api_key(request: Request):
    payload = require_token(request)
    user_id = int(payload["sub"])

    new_key = generate_api_key()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET api_key = %s WHERE id = %s",
                (new_key, user_id),
            )

    return {"message": "New API key generated", "api_key": new_key}


@app.get("/api/user/usage")
async def get_usage_history(request: Request):
    payload = require_token(request)
    user_id = int(payload["sub"])

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT uh.id, uh.model_id, uh.prompt_tokens, uh.completion_tokens,
                          uh.total_cost, uh.ip_address, uh.created_at
                   FROM usage_history uh
                   WHERE uh.user_id = %s
                   ORDER BY uh.created_at DESC
                   LIMIT 100""",
                (user_id,),
            )
            rows = await cur.fetchall()

    history = []
    for r in rows:
        history.append({
            "id": r["id"],
            "model_id": r["model_id"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "total_cost": float(r["total_cost"]),
            "ip_address": r["ip_address"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {"usage": history}


@app.get("/api/user/credits")
async def get_credits(request: Request):
    payload = require_token(request)
    user_id = int(payload["sub"])

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT credit FROM users WHERE id = %s", (user_id,))
            user = await cur.fetchone()

            await cur.execute(
                "SELECT COALESCE(SUM(total_cost), 0) as total_spent FROM usage_history WHERE user_id = %s",
                (user_id,),
            )
            spent = await cur.fetchone()

            await cur.execute(
                "SELECT COALESCE(SUM(total_cost), 0) as today_spent FROM usage_history WHERE user_id = %s AND DATE(created_at) = CURDATE()",
                (user_id,),
            )
            today = await cur.fetchone()

            await cur.execute(
                "SELECT COUNT(*) as total_requests FROM usage_history WHERE user_id = %s",
                (user_id,),
            )
            req_count = await cur.fetchone()

    return {
        "credit": float(user["credit"]) if user else 0,
        "total_spent": float(spent["total_spent"]),
        "today_spent": float(today["today_spent"]),
        "total_requests": req_count["total_requests"],
    }


# ═══════════════════════════════════════════════════════════════
# PUBLIC ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/models")
async def get_models():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT model_id, name, input_price, output_price, discount_percent, is_active, created_at FROM models WHERE is_active = TRUE ORDER BY name"
            )
            rows = await cur.fetchall()

    models = []
    for r in rows:
        discount = float(r.get("discount_percent") or 0)
        multiplier = 1 - discount / 100
        models.append({
            "model_id": r["model_id"],
            "name": r["name"],
            "input_price": round(float(r["input_price"]) * multiplier, 6),
            "output_price": round(float(r["output_price"]) * multiplier, 6),
            "original_input_price": float(r["input_price"]),
            "original_output_price": float(r["output_price"]),
            "discount_percent": discount,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {"models": models}


# ═══════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT COUNT(*) as count FROM users")
            total_users = (await cur.fetchone())["count"]

            await cur.execute("SELECT COUNT(*) as count FROM usage_history")
            total_requests = (await cur.fetchone())["count"]

            await cur.execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total_tokens FROM usage_history"
            )
            total_tokens = (await cur.fetchone())["total_tokens"]

            await cur.execute(
                "SELECT COALESCE(SUM(total_cost), 0) as total_revenue FROM usage_history"
            )
            total_revenue = float((await cur.fetchone())["total_revenue"])

            await cur.execute("SELECT COUNT(*) as count FROM models WHERE is_active = TRUE")
            active_models = (await cur.fetchone())["count"]

    return {
        "total_users": total_users,
        "total_requests": total_requests,
        "total_tokens": int(total_tokens),
        "total_revenue": total_revenue,
        "active_models": active_models,
    }


@app.get("/api/admin/users")
async def admin_users(request: Request):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, username, email, role, credit, is_active, created_at FROM users ORDER BY created_at DESC LIMIT 200"
            )
            rows = await cur.fetchall()

    users = []
    for r in rows:
        users.append({
            "id": r["id"],
            "username": r["username"],
            "email": r["email"],
            "role": r["role"],
            "credit": float(r["credit"]),
            "is_active": r["is_active"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {"users": users}


@app.get("/api/admin/models")
async def admin_models(request: Request):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM models ORDER BY created_at DESC")
            rows = await cur.fetchall()

    models = []
    for r in rows:
        models.append({
            "id": r["id"],
            "model_id": r["model_id"],
            "name": r["name"],
            "input_price": float(r["input_price"]),
            "output_price": float(r["output_price"]),
            "discount_percent": float(r.get("discount_percent") or 0),
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {"models": models}


@app.post("/api/admin/models")
async def admin_add_model(request: Request, body: AdminModelRequest):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT id FROM models WHERE model_id = %s", (body.model_id,))
            if await cur.fetchone():
                raise HTTPException(status_code=400, detail="Model ID already exists")

            await cur.execute(
                "INSERT INTO models (model_id, name, input_price, output_price, discount_percent, is_active) VALUES (%s, %s, %s, %s, %s, %s)",
                (body.model_id, body.name, body.input_price, body.output_price, body.discount_percent, body.is_active),
            )

    return {"message": "Model added successfully"}


@app.put("/api/admin/models/{model_id}")
async def admin_update_model(model_id: str, request: Request, body: AdminModelRequest):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE models SET name = %s, input_price = %s, output_price = %s, discount_percent = %s, is_active = %s WHERE model_id = %s",
                (body.name, body.input_price, body.output_price, body.discount_percent, body.is_active, model_id),
            )

    return {"message": "Model updated"}


@app.patch("/api/admin/models/{model_id}/toggle")
async def admin_toggle_model(model_id: str, request: Request, body: ToggleModelRequest):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE models SET is_active = %s WHERE model_id = %s",
                (body.is_active, model_id),
            )

    return {"message": "Model status updated"}


@app.delete("/api/admin/models/{model_id}")
async def admin_delete_model(model_id: str, request: Request):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM models WHERE model_id = %s", (model_id,))

    return {"message": "Model deleted"}


@app.patch("/api/admin/models/bulk-discount")
async def admin_bulk_discount(request: Request, body: BulkDiscountRequest):
    require_admin(request)

    if body.discount_percent < 0 or body.discount_percent > 100:
        raise HTTPException(status_code=400, detail="Discount must be between 0 and 100")

    if not body.model_ids:
        raise HTTPException(status_code=400, detail="No model IDs provided")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(body.model_ids))
            await cur.execute(
                f"UPDATE models SET discount_percent = %s WHERE model_id IN ({placeholders})",
                [body.discount_percent] + body.model_ids,
            )
            affected = cur.rowcount

    return {"message": f"Discount updated for {affected} models"}


@app.get("/api/admin/usage")
async def admin_usage(request: Request):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT uh.id, u.username, uh.model_id, uh.prompt_tokens,
                          uh.completion_tokens, uh.total_cost, uh.ip_address, uh.created_at
                   FROM usage_history uh
                   LEFT JOIN users u ON u.id = uh.user_id
                   ORDER BY uh.created_at DESC
                   LIMIT 500"""
            )
            rows = await cur.fetchall()

    history = []
    for r in rows:
        history.append({
            "id": r["id"],
            "username": r["username"],
            "model_id": r["model_id"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "total_cost": float(r["total_cost"]),
            "ip_address": r["ip_address"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {"usage": history}


@app.get("/api/admin/charts")
async def admin_charts(request: Request):
    """Admin analytics: daily usage, model distribution, hourly heatmap, top users, model usage with cost."""
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            # Daily usage last 30 days
            await cur.execute(
                """SELECT DATE(created_at) as date,
                          COUNT(*) as requests,
                          COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                          COALESCE(SUM(prompt_tokens + completion_tokens), 0) as tokens,
                          COALESCE(SUM(total_cost), 0) as cost
                   FROM usage_history
                   WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                   GROUP BY DATE(created_at)
                   ORDER BY date"""
            )
            daily_raw = await cur.fetchall()

            # Model distribution (with cost)
            await cur.execute(
                """SELECT model_id,
                          COUNT(*) as count,
                          COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total_tokens,
                          COALESCE(SUM(total_cost), 0) as cost
                   FROM usage_history
                   GROUP BY model_id
                   ORDER BY count DESC
                   LIMIT 20"""
            )
            model_dist = await cur.fetchall()

            # Hourly requests today
            await cur.execute(
                """SELECT HOUR(created_at) as hour, COUNT(*) as count
                   FROM usage_history
                   WHERE DATE(created_at) = CURDATE()
                   GROUP BY HOUR(created_at)
                   ORDER BY hour"""
            )
            hourly_raw = await cur.fetchall()

            # Top users by request count
            await cur.execute(
                """SELECT u.username, COUNT(*) as count,
                          COALESCE(SUM(uh.total_cost), 0) as total_cost
                   FROM usage_history uh
                   JOIN users u ON u.id = uh.user_id
                   GROUP BY uh.user_id
                   ORDER BY count DESC
                   LIMIT 10"""
            )
            top_users_raw = await cur.fetchall()

    daily_usage = []
    for r in daily_raw:
        daily_usage.append({
            "date": r["date"].strftime("%m/%d") if r["date"] else "",
            "requests": r["requests"],
            "prompt_tokens": int(r["prompt_tokens"]),
            "completion_tokens": int(r["completion_tokens"]),
            "tokens": int(r["tokens"]),
            "cost": float(r["cost"]),
        })

    model_usage = []
    for r in model_dist:
        model_usage.append({
            "model_id": r["model_id"],
            "count": r["count"],
            "total_tokens": int(r["total_tokens"]),
            "cost": float(r["cost"]),
        })

    hourly_requests = []
    for r in hourly_raw:
        hourly_requests.append({
            "hour": r["hour"],
            "count": r["count"],
        })

    top_users = []
    for r in top_users_raw:
        top_users.append({
            "username": r["username"],
            "count": r["count"],
            "total_cost": float(r["total_cost"]),
        })

    return {
        "daily_usage": daily_usage,
        "model_distribution": model_usage,
        "model_usage": model_usage,
        "hourly_requests": hourly_requests,
        "top_users": top_users,
    }


@app.get("/api/admin/charts/model/{model_id}")
async def admin_charts_by_model(model_id: str, request: Request):
    """Per-model daily usage for the last 30 days."""
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT DATE(created_at) as date,
                          COUNT(*) as requests,
                          COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                          COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                          COALESCE(SUM(total_cost), 0) as cost
                   FROM usage_history
                   WHERE model_id = %s
                     AND created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                   GROUP BY DATE(created_at)
                   ORDER BY date""",
                (model_id,),
            )
            rows = await cur.fetchall()

    daily = []
    for r in rows:
        daily.append({
            "date": r["date"].strftime("%m/%d") if r["date"] else "",
            "requests": r["requests"],
            "prompt_tokens": int(r["prompt_tokens"]),
            "completion_tokens": int(r["completion_tokens"]),
            "cost": float(r["cost"]),
        })

    return {"model_id": model_id, "daily_usage": daily}


@app.post("/api/admin/users/{user_id}/toggle")
async def admin_toggle_user(user_id: int, request: Request):
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT is_active FROM users WHERE id = %s", (user_id,))
            user = await cur.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            new_status = not user["is_active"]
            await cur.execute(
                "UPDATE users SET is_active = %s WHERE id = %s",
                (new_status, user_id),
            )

    return {"message": "User status updated", "is_active": new_status}


@app.post("/api/admin/users/{user_id}/credit")
async def admin_add_credit(user_id: int, request: Request):
    require_admin(request)
    body = await request.json()
    amount = float(body.get("amount", 0))

    if amount == 0:
        raise HTTPException(status_code=400, detail="Invalid amount")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET credit = credit + %s WHERE id = %s",
                (amount, user_id),
            )

    return {"message": f"Added {amount} credit to user #{user_id}"}


# ═══════════════════════════════════════════════════════════════
# PAYMENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════

class CreatePayPalOrderRequest(BaseModel):
    amount: float
    return_url: Optional[str] = None
    cancel_url: Optional[str] = None


@app.post("/api/payment/paypal/create-order")
async def payment_paypal_create_order(request: Request, body: CreatePayPalOrderRequest):
    """Create a PayPal order for credit top-up."""
    payload = require_token(request)
    user_id = int(payload["sub"])

    if body.amount < 1.0:
        raise HTTPException(status_code=400, detail="Minimum top-up is $1")
    if body.amount > 500.0:
        raise HTTPException(status_code=400, detail="Maximum top-up is $500")

    return_url = body.return_url or f"{FRONTEND_URL}/dashboard/billing?status=success"
    cancel_url = body.cancel_url or f"{FRONTEND_URL}/dashboard/billing?status=cancel"

    try:
        result = await paypal_create(body.amount, user_id, return_url, cancel_url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PayPal error: {str(e)}")

    # Save order to DB
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO payment_orders
                   (user_id, order_id, provider, amount, status)
                   VALUES (%s, %s, 'paypal', %s, 'pending')""",
                (user_id, result["order_id"], body.amount),
            )

    return result


@app.post("/api/payment/paypal/capture-order")
async def payment_paypal_capture_order(request: Request):
    """Capture a PayPal order after user approval."""
    payload = require_token(request)
    user_id = int(payload["sub"])
    body = await request.json()
    order_id = body.get("order_id", "")

    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    # Check that this order belongs to this user and is pending
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, user_id, amount, status FROM payment_orders WHERE order_id = %s",
                (order_id,),
            )
            order = await cur.fetchone()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Order does not belong to this user")
    if order["status"] == "completed":
        return {"status": "already_completed", "credit_added": float(order["amount"])}

    try:
        result = await paypal_capture(order_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PayPal capture error: {str(e)}")

    if result.get("status") == "COMPLETED":
        capture = result["purchase_units"][0]["payments"]["captures"][0]
        amount = float(capture["amount"]["value"])

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE users SET credit = credit + %s WHERE id = %s",
                    (amount, user_id),
                )
                await cur.execute(
                    "UPDATE payment_orders SET status = 'completed', metadata = %s WHERE order_id = %s",
                    (json.dumps({"paypal_capture_id": capture["id"]}), order_id),
                )

        return {"status": "success", "credit_added": amount}

    # Payment not completed
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE payment_orders SET status = 'failed' WHERE order_id = %s",
                (order_id,),
            )

    return {"status": "failed", "detail": result.get("status", "UNKNOWN")}


@app.post("/api/webhook/paypal")
async def webhook_paypal(request: Request):
    """
    PayPal webhook — backup handler for when user closes browser
    before frontend can call capture-order.
    """
    body = await request.json()
    headers = dict(request.headers)

    # Verify webhook signature
    is_valid = await verify_webhook_signature(headers, body)
    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = body.get("event_type", "")

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        resource = body.get("resource", {})
        capture_amount = float(resource.get("amount", {}).get("value", 0))
        custom_id = resource.get("custom_id", "")  # user_id set during create_order

        # Find the order
        supplementary = resource.get("supplementary_data", {})
        order_id = supplementary.get("related_ids", {}).get("order_id", "")

        if not order_id or not custom_id:
            return {"status": "ignored", "reason": "missing order_id or custom_id"}

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT id, status FROM payment_orders WHERE order_id = %s",
                    (order_id,),
                )
                order = await cur.fetchone()

                if order and order["status"] == "completed":
                    return {"status": "already_processed"}

                if order:
                    await cur.execute(
                        "UPDATE users SET credit = credit + %s WHERE id = %s",
                        (capture_amount, int(custom_id)),
                    )
                    await cur.execute(
                        "UPDATE payment_orders SET status = 'completed', metadata = %s WHERE order_id = %s",
                        (json.dumps({"webhook_capture": True, "capture_id": resource.get("id", "")}), order_id),
                    )

    return {"status": "ok"}


@app.get("/api/user/payments")
async def get_payment_history(request: Request):
    """Get user's payment/top-up history."""
    payload = require_token(request)
    user_id = int(payload["sub"])

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT id, order_id, provider, amount, status, created_at, updated_at
                   FROM payment_orders
                   WHERE user_id = %s
                   ORDER BY created_at DESC
                   LIMIT 50""",
                (user_id,),
            )
            rows = await cur.fetchall()

    payments = []
    for r in rows:
        payments.append({
            "id": r["id"],
            "order_id": r["order_id"],
            "provider": r["provider"],
            "amount": float(r["amount"]),
            "status": r["status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })

    return {"payments": payments}


@app.get("/api/admin/payments")
async def admin_payment_history(request: Request):
    """Admin: view all payment orders."""
    require_admin(request)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """SELECT po.id, u.username, po.order_id, po.provider, po.amount,
                          po.status, po.created_at, po.updated_at
                   FROM payment_orders po
                   LEFT JOIN users u ON u.id = po.user_id
                   ORDER BY po.created_at DESC
                   LIMIT 200"""
            )
            rows = await cur.fetchall()

    payments = []
    for r in rows:
        payments.append({
            "id": r["id"],
            "username": r["username"],
            "order_id": r["order_id"],
            "provider": r["provider"],
            "amount": float(r["amount"]),
            "status": r["status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })

    return {"payments": payments}


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    print(f"""
======================================================
  OrchestraAPI Backend Server
  http://127.0.0.1:{BACKEND_PORT}
======================================================
""")
    uvicorn.run(app, host="0.0.0.0", port=BACKEND_PORT)
