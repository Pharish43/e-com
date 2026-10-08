"""Razorpay checkout with email notifications and local payment idempotency."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import email_notifications
import local_store


RAZORPAY_API = "https://api.razorpay.com/v1"
REQUEST_TIMEOUT_SECONDS = 15
MAX_WEBHOOK_BYTES = 1_000_000


class PaymentRequestError(ValueError):
    """The checkout request or a verified payment does not match expectations."""


class PaymentConfigurationError(RuntimeError):
    """Required payment settings are missing."""


class PaymentStorageError(RuntimeError):
    """Local payment state could not be read or saved."""


class PaymentProviderError(RuntimeError):
    """Razorpay could not process or confirm the request."""


class PaymentNotificationError(RuntimeError):
    """A required order or payment email could not be sent."""


def _storefront() -> Any:
    module = sys.modules.get("admin")
    if module is not None and hasattr(module, "STORE_LOCK"):
        return module
    module = sys.modules.get("__main__")
    if module is not None and hasattr(module, "STORE_LOCK"):
        return module
    raise PaymentStorageError("The Fernwood inventory service is not initialized.")


def _credentials() -> tuple[str, str, str]:
    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
    webhook_secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not key_id or not key_secret or not webhook_secret:
        raise PaymentConfigurationError(
            "Set RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, and "
            "RAZORPAY_WEBHOOK_SECRET in the server environment."
        )
    return key_id, key_secret, webhook_secret


def _address(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("shipping_address")
    if not isinstance(raw, dict):
        raise PaymentRequestError("Enter a delivery address before checkout.")

    fields = {
        "name": ("Recipient name", 80, True),
        "phone": ("Phone number", 18, True),
        "address": ("Street address", 180, True),
        "city": ("City", 80, True),
        "state": ("State", 80, True),
        "pincode": ("PIN code", 6, True),
        "email": ("Email", 254, False),
    }
    result: dict[str, str] = {}
    for key, (label, limit, required) in fields.items():
        value = raw.get(key, "" if not required else None)
        if not isinstance(value, str):
            raise PaymentRequestError(f"{label} must be text.")
        value = value.strip()
        if required and not value:
            raise PaymentRequestError(f"{label} is required.")
        if len(value) > limit:
            raise PaymentRequestError(f"{label} must be {limit} characters or fewer.")
        result[key] = value

    phone = re.sub(r"\D", "", result["phone"])
    if not re.fullmatch(r"(?:91)?[6-9]\d{9}", phone):
        raise PaymentRequestError("Enter a valid 10-digit Indian mobile number.")
    if not re.fullmatch(r"\d{6}", result["pincode"]):
        raise PaymentRequestError("PIN code must contain exactly 6 digits.")
    if result["email"] and not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+", result["email"]
    ):
        raise PaymentRequestError("Enter a valid email address.")
    return result


def _checkout_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 50:
        raise PaymentRequestError("Your cart is empty or contains too many items.")

    quantities: dict[str, int] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            raise PaymentRequestError("Invalid item in your cart.")
        product_id = item.get("product_id")
        quantity = item.get("quantity")
        if not isinstance(product_id, str) or not product_id or len(product_id) > 80:
            raise PaymentRequestError("Invalid product in your cart.")
        if product_id in quantities:
            raise PaymentRequestError("Your cart contains a duplicate product.")
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or not 1 <= quantity <= 99
        ):
            raise PaymentRequestError("Each item quantity must be from 1 to 99.")
        quantities[product_id] = quantity

    order_items: list[dict[str, Any]] = []
    total_paise = 0
    app = _storefront()
    with app.STORE_LOCK:
        try:
            products = app.load_products()
        except RuntimeError as error:
            raise PaymentStorageError(str(error)) from error
        by_id = {product["id"]: product for product in products}
        for product_id, quantity in quantities.items():
            product = by_id.get(product_id)
            if product is None:
                raise PaymentRequestError(
                    "A product in your cart is no longer available. Refresh the shop."
                )
            stock = product.get("stock")
            if isinstance(stock, bool) or not isinstance(stock, int) or stock < quantity:
                available = max(0, int(stock)) if isinstance(stock, int) else 0
                raise PaymentRequestError(
                    f"Only {available} of {product.get('name', 'this product')} are available."
                )
            try:
                price = Decimal(str(product["price"]))
                unit_paise = int(
                    (price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
            except (KeyError, InvalidOperation, ValueError):
                raise PaymentStorageError(
                    f"The catalog price for product {product_id} is invalid."
                ) from None
            if unit_paise < 1:
                raise PaymentStorageError(
                    f"The catalog price for product {product_id} is invalid."
                )
            total_paise += unit_paise * quantity
            order_items.append(
                {
                    "product_id": product_id,
                    "name": product["name"],
                    "quantity": quantity,
                    "unit_price_paise": unit_paise,
                }
            )
    if total_paise > 99_999_999:
        raise PaymentRequestError(
            "The cart total exceeds Razorpay's supported order amount."
        )
    return order_items, total_paise


def _razorpay_request(
    method: str, resource: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    key_id, key_secret, _ = _credentials()
    authorization = base64.b64encode(
        f"{key_id}:{key_secret}".encode("utf-8")
    ).decode("ascii")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{RAZORPAY_API}/{resource.lstrip('/')}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        error.close()
        raise PaymentProviderError(
            f"Razorpay rejected the request (HTTP {error.code}). Check your Test Mode keys and account."
        ) from None
    except (TimeoutError, urllib.error.URLError):
        raise PaymentProviderError(
            "Could not reach Razorpay. Check the server connection and try again."
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise PaymentProviderError("Razorpay returned an invalid response.") from None
    if not isinstance(result, dict):
        raise PaymentProviderError("Razorpay returned an invalid response.")
    return result


def _seed_loader() -> Any:
    app = _storefront()
    loader = getattr(app, "_load_seed_products", None)
    if not callable(loader):
        raise PaymentStorageError("The Fernwood catalog loader is unavailable.")
    return loader


def create_order(payload: dict[str, Any]) -> dict[str, Any]:
    key_id, _, _ = _credentials()
    email_notifications.validate_configuration()
    shipping_address = _address(payload)
    items, amount = _checkout_items(payload)

    remote_order = _razorpay_request(
        "POST",
        "orders",
        {"amount": amount, "currency": "INR", "receipt": os.urandom(12).hex()},
    )
    order_id = remote_order.get("id")
    if (
        not isinstance(order_id, str)
        or remote_order.get("amount") != amount
        or remote_order.get("currency") != "INR"
    ):
        raise PaymentProviderError("Razorpay returned unexpected order details.")

    seed_loader = _seed_loader()
    try:
        local_store.add_pending_order(
            order_id, amount, "INR", items, seed_loader
        )
    except local_store.LocalStoreError as error:
        raise PaymentStorageError(str(error)) from error
    try:
        email_notifications.order_created(
            order_id, amount, "INR", shipping_address, items
        )
    except email_notifications.EmailNotificationError as error:
        try:
            local_store.remove_pending_order(order_id, seed_loader)
        except local_store.LocalStoreError as storage_error:
            raise PaymentStorageError(
                "The order email failed and local pending-order cleanup also failed."
            ) from storage_error
        raise PaymentNotificationError(str(error)) from error

    return {
        "key_id": key_id,
        "order_id": order_id,
        "amount": amount,
        "currency": "INR",
    }


def _mark_paid(
    order_id: str,
    payment_id: str | None,
    amount: int,
    currency: str,
) -> dict[str, Any]:
    seed_loader = _seed_loader()
    try:
        result = local_store.complete_paid_order(
            order_id, amount, currency, seed_loader
        )
    except ValueError as error:
        raise PaymentRequestError(str(error)) from error
    except local_store.LocalStoreError as error:
        raise PaymentStorageError(str(error)) from error

    if not result["payment_email_sent"]:
        try:
            local_store.send_payment_notification_once(
                order_id,
                lambda: email_notifications.payment_confirmed(
                    order_id, payment_id or "(not provided)", amount, currency
                ),
                seed_loader,
            )
        except email_notifications.EmailNotificationError as error:
            raise PaymentNotificationError(
                "Payment was confirmed and stock updated, but the payment email "
                "could not be sent. The next Razorpay retry will try again."
            ) from error
        except local_store.LocalStoreError as error:
            raise PaymentStorageError(str(error)) from error
    return {
        "status": "paid",
        "already_processed": result["already_processed"],
    }


def verify_payment(payload: dict[str, Any]) -> dict[str, Any]:
    order_id = payload.get("razorpay_order_id")
    payment_id = payload.get("razorpay_payment_id")
    signature = payload.get("razorpay_signature")
    if not all(
        isinstance(value, str) and 1 <= len(value) <= 128
        for value in (order_id, payment_id, signature)
    ):
        raise PaymentRequestError("Razorpay returned incomplete payment details.")

    _, key_secret, _ = _credentials()
    expected = hmac.new(
        key_secret.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PaymentRequestError("Payment signature verification failed.")

    payment = _razorpay_request(
        "GET", f"payments/{urllib.parse.quote(payment_id, safe='')}"
    )
    if (
        payment.get("id") != payment_id
        or payment.get("order_id") != order_id
        or not isinstance(payment.get("amount"), int)
        or not isinstance(payment.get("currency"), str)
    ):
        raise PaymentRequestError("Payment details do not match this order.")
    if payment.get("status") != "captured":
        raise PaymentRequestError(
            "Payment is not captured yet. Do not retry if your bank shows a debit; contact the store."
        )
    return _mark_paid(
        order_id, payment_id, payment["amount"], payment["currency"]
    )


def verify_webhook(
    raw_body: bytes, signature: str | None, event_id: str | None
) -> dict[str, Any]:
    if not raw_body or len(raw_body) > MAX_WEBHOOK_BYTES:
        raise PaymentRequestError("Webhook body is empty or too large.")
    if (
        not isinstance(signature, str)
        or not signature
        or (
            event_id is not None
            and (not isinstance(event_id, str) or len(event_id) > 100)
        )
    ):
        raise PaymentRequestError("Webhook signature or event ID is missing.")
    _, _, webhook_secret = _credentials()
    expected = hmac.new(
        webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        raise PaymentRequestError("Webhook signature verification failed.")
    try:
        event = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PaymentRequestError("Webhook body must be valid JSON.") from None
    if not isinstance(event, dict):
        raise PaymentRequestError("Webhook body must be a JSON object.")

    event_name = event.get("event")
    if event_name not in {"payment.captured", "order.paid"}:
        return {"received": True, "processed": False}
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise PaymentRequestError("Webhook payload is invalid.")
    payment_data = payload.get("payment")
    order_data = payload.get("order")
    payment_entity = payment_data.get("entity") if isinstance(payment_data, dict) else None
    order_entity = order_data.get("entity") if isinstance(order_data, dict) else None
    if not isinstance(payment_entity, dict):
        payment_entity = {}
    if not isinstance(order_entity, dict):
        order_entity = {}

    order_id = payment_entity.get("order_id") or order_entity.get("id")
    payment_id = payment_entity.get("id")
    amount = payment_entity.get("amount", order_entity.get("amount"))
    currency = payment_entity.get("currency", order_entity.get("currency"))
    if event_name == "payment.captured" and payment_entity.get("status") != "captured":
        raise PaymentRequestError("Razorpay webhook payment is not captured.")
    if event_name == "order.paid" and order_entity.get("status") != "paid":
        raise PaymentRequestError("Razorpay webhook order is not marked paid.")
    if (
        not isinstance(order_id, str)
        or not isinstance(amount, int)
        or isinstance(amount, bool)
        or not isinstance(currency, str)
    ):
        raise PaymentRequestError("Webhook does not contain valid order details.")

    result = _mark_paid(order_id, payment_id, amount, currency)
    return {"received": True, "processed": True, **result}
