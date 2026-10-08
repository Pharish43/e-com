"""Atomic local JSON storage for the catalog and payment idempotency state."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DATA_DIR = Path(
    os.environ.get("FERNWOOD_DATA_DIR", str(Path(__file__).resolve().parent))
)
DATA_FILE = DATA_DIR / "products.json"
TEMP_FILE = DATA_FILE.with_suffix(".json.tmp")
STORE_LOCK = threading.RLock()


class LocalStoreError(RuntimeError):
    """The local catalog or payment state could not be read or saved."""


def _read_state(seed_loader: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
    if not DATA_FILE.exists():
        state = {
            "products": seed_loader(),
            "pending_orders": {},
            "processed_orders": {},
        }
        _write_state(state)
        return state
    try:
        content = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LocalStoreError(f"Could not read {DATA_FILE.name}: {error}") from error

    if isinstance(content, list):
        state = {
            "products": content,
            "pending_orders": {},
            "processed_orders": {},
        }
        _write_state(state)
        return state
    if not isinstance(content, dict):
        raise LocalStoreError(f"{DATA_FILE.name} must contain a JSON object or array.")

    products = content.get("products")
    pending = content.get("pending_orders", {})
    processed = content.get("processed_orders", {})
    if (
        not isinstance(products, list)
        or not isinstance(pending, dict)
        or not isinstance(processed, dict)
    ):
        raise LocalStoreError(f"{DATA_FILE.name} has an invalid data structure.")
    return {
        "products": products,
        "pending_orders": pending,
        "processed_orders": processed,
    }


def _write_state(state: dict[str, Any]) -> None:
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        TEMP_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(TEMP_FILE, DATA_FILE)
    except OSError as error:
        raise LocalStoreError(f"Could not save {DATA_FILE.name}: {error}") from error


def load_products(
    seed_loader: Callable[[], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    with STORE_LOCK:
        return _read_state(seed_loader)["products"]


def add_product(
    product: dict[str, Any], seed_loader: Callable[[], list[dict[str, Any]]]
) -> None:
    with STORE_LOCK:
        state = _read_state(seed_loader)
        if any(existing.get("id") == product["id"] for existing in state["products"]):
            raise LocalStoreError("A product with that ID already exists.")
        state["products"].append(product)
        _write_state(state)


def set_stock(
    product_id: str,
    stock: int,
    seed_loader: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    with STORE_LOCK:
        state = _read_state(seed_loader)
        product = next(
            (item for item in state["products"] if item.get("id") == product_id),
            None,
        )
        if product is None:
            return None
        product["stock"] = stock
        _write_state(state)
        return product


def delete_product(
    product_id: str, seed_loader: Callable[[], list[dict[str, Any]]]
) -> bool:
    with STORE_LOCK:
        state = _read_state(seed_loader)
        remaining = [
            item for item in state["products"] if item.get("id") != product_id
        ]
        if len(remaining) == len(state["products"]):
            return False
        state["products"] = remaining
        _write_state(state)
        return True


def add_pending_order(
    order_id: str,
    amount: int,
    currency: str,
    items: list[dict[str, Any]],
    seed_loader: Callable[[], list[dict[str, Any]]],
) -> None:
    with STORE_LOCK:
        state = _read_state(seed_loader)
        if order_id in state["processed_orders"]:
            raise LocalStoreError("This payment order was already processed.")
        state["pending_orders"][order_id] = {
            "amount": amount,
            "currency": currency,
            "items": [
                {"product_id": item["product_id"], "quantity": item["quantity"]}
                for item in items
            ],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_state(state)


def remove_pending_order(
    order_id: str, seed_loader: Callable[[], list[dict[str, Any]]]
) -> None:
    with STORE_LOCK:
        state = _read_state(seed_loader)
        if order_id in state["pending_orders"]:
            del state["pending_orders"][order_id]
            _write_state(state)


def complete_paid_order(
    order_id: str,
    amount: int,
    currency: str,
    seed_loader: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any]:
    with STORE_LOCK:
        state = _read_state(seed_loader)
        processed = state["processed_orders"].get(order_id)
        if processed is not None:
            return {
                "items": [],
                "already_processed": True,
                "payment_email_sent": processed.get("payment_email_sent", False),
            }

        order = state["pending_orders"].get(order_id)
        if order is None:
            raise ValueError("This payment order is not pending in Fernwood.")
        if order["amount"] != amount or order["currency"] != currency:
            raise ValueError("Payment amount or currency does not match the order.")

        products_by_id = {item["id"]: item for item in state["products"]}
        for item in order["items"]:
            product = products_by_id.get(item["product_id"])
            if product is None or product.get("stock", 0) < item["quantity"]:
                raise ValueError(
                    "Payment was captured, but stock changed before checkout completed. "
                    "Contact the store to complete or refund your order."
                )
        for item in order["items"]:
            products_by_id[item["product_id"]]["stock"] -= item["quantity"]

        processed = {"payment_email_sent": False}
        del state["pending_orders"][order_id]
        state["processed_orders"][order_id] = processed
        _write_state(state)
        return {
            "items": order["items"],
            "already_processed": False,
            "payment_email_sent": False,
        }


def send_payment_notification_once(
    order_id: str,
    notifier: Callable[[], None],
    seed_loader: Callable[[], list[dict[str, Any]]],
) -> bool:
    with STORE_LOCK:
        state = _read_state(seed_loader)
        processed = state["processed_orders"].get(order_id)
        if processed is None:
            raise LocalStoreError("The paid order is missing its local receipt state.")
        if processed.get("payment_email_sent", False):
            return False

        notifier()
        processed["payment_email_sent"] = True
        _write_state(state)
        return True
