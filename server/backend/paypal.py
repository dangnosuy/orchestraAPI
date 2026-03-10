"""
PayPal Integration Module
=========================
Handles OAuth2 token exchange, order creation, capture, and webhook verification.
"""
import httpx

from config import PAYPAL_CLIENT_ID, PAYPAL_SECRET, PAYPAL_MODE, PAYPAL_WEBHOOK_ID

BASE_URL = (
    "https://api-m.sandbox.paypal.com"
    if PAYPAL_MODE == "sandbox"
    else "https://api-m.paypal.com"
)


async def get_access_token() -> str:
    """Exchange client credentials for a PayPal access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def create_order(amount_usd: float, user_id: int, return_url: str, cancel_url: str) -> dict:
    """
    Create a PayPal checkout order.
    Returns {"order_id": str, "approve_url": str}.
    """
    token = await get_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/v2/checkout/orders",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "amount": {
                            "currency_code": "USD",
                            "value": f"{amount_usd:.2f}",
                        },
                        "custom_id": str(user_id),
                        "description": f"OrchestraAPI Credit Top-up ${amount_usd:.2f}",
                    }
                ],
                "application_context": {
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                    "brand_name": "OrchestraAPI",
                    "user_action": "PAY_NOW",
                    "shipping_preference": "NO_SHIPPING",
                },
            },
        )
        resp.raise_for_status()
        order = resp.json()

    approve_url = next(
        (link["href"] for link in order["links"] if link["rel"] == "approve"),
        None,
    )
    return {"order_id": order["id"], "approve_url": approve_url}


async def capture_order(order_id: str) -> dict:
    """Capture an approved PayPal order. Returns the full capture response."""
    token = await get_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/v2/checkout/orders/{order_id}/capture",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def verify_webhook_signature(headers: dict, body: dict) -> bool:
    """
    Verify a PayPal webhook signature via their API.
    Returns True if verification passes.
    """
    token = await get_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/v1/notifications/verify-webhook-signature",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "auth_algo": headers.get("paypal-auth-algo", ""),
                "cert_url": headers.get("paypal-cert-url", ""),
                "transmission_id": headers.get("paypal-transmission-id", ""),
                "transmission_sig": headers.get("paypal-transmission-sig", ""),
                "transmission_time": headers.get("paypal-transmission-time", ""),
                "webhook_id": PAYPAL_WEBHOOK_ID,
                "webhook_event": body,
            },
        )
        if resp.status_code != 200:
            return False
        return resp.json().get("verification_status") == "SUCCESS"
