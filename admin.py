"""Local Fernwood storefront and inventory manager.

Run with ``py admin.py`` and open http://127.0.0.1:8000.
The server intentionally listens only on localhost.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import payment_gateway
import local_store


BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"
HOST = "127.0.0.1"
PORT = 8000
MAX_REQUEST_BYTES = 1_000_000
STORE_LOCK = local_store.STORE_LOCK
ALLOWED_CATEGORIES = {"Plants", "Pots", "Tools"}

def _load_seed_products() -> list[dict]:
    with STORE_LOCK:
        if not PRODUCTS_FILE.exists():
            try:
                PRODUCTS_FILE.write_text(
                    "[]\n",
                    encoding="utf-8",
                )
            except OSError as error:
                raise RuntimeError(
                    f"Could not create {PRODUCTS_FILE.name}: {error}"
                ) from error
        try:
            products = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Could not read {PRODUCTS_FILE.name}: {error}") from error
        if not isinstance(products, list):
            raise RuntimeError(f"{PRODUCTS_FILE.name} must contain a JSON array.")
        used_ids: set[str] = set()
        for product in products:
            if not isinstance(product, dict):
                raise RuntimeError(f"{PRODUCTS_FILE.name} contains an invalid product.")
            product_id = product.get("id")
            if not isinstance(product_id, str) or not product_id.strip() or product_id in used_ids:
                base_id = re.sub(
                    r"[^a-z0-9]+", "-", str(product.get("name", "")).lower()
                ).strip("-")[:55].strip("-") or "product"
                product_id = base_id
                suffix = 2
                while product_id in used_ids:
                    product_id = f"{base_id}-{suffix}"
                    suffix += 1
                product["id"] = product_id
            used_ids.add(product_id)
        return products


def load_products() -> list[dict]:
    with STORE_LOCK:
        try:
            return local_store.load_products(_load_seed_products)
        except local_store.LocalStoreError as error:
            raise RuntimeError(str(error)) from error


def bounded_text(value: object, field: str, max_length: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field} is required.")
    if len(result) > max_length:
        raise ValueError(f"{field} must be {max_length} characters or fewer.")
    return result


def parse_nonnegative_integer(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{field} must be a whole number from 0 to {maximum}.")
    return value


def create_product(payload: dict) -> dict:
    name = bounded_text(payload.get("name"), "Name", 80)
    category = bounded_text(payload.get("category"), "Category", 20)
    if category not in ALLOWED_CATEGORIES:
        raise ValueError("Category must be Plants, Pots, or Tools.")
    description = bounded_text(payload.get("description", ""), "Description", 240, required=False)
    try:
        price = Decimal(str(payload.get("price")))
    except (InvalidOperation, ValueError):
        raise ValueError("Price must be a valid amount.") from None
    if not price.is_finite() or price <= 0 or price > Decimal("100000"):
        raise ValueError("Price must be greater than ₹0 and no more than ₹100,000.")
    try:
        rounded_price = price.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("Price is outside the supported amount range.") from None
    if rounded_price != price:
        raise ValueError("Price must have at most two decimal places.")
    price = rounded_price
    if price <= 0:
        raise ValueError("Price must be at least ₹0.01.")
    price = float(price)
    stock = parse_nonnegative_integer(payload.get("stock"), "Stock", 1_000_000)
    emoji = bounded_text(payload.get("emoji", "🌱"), "Product icon", 8)
    color = bounded_text(payload.get("color", "#dcebd8"), "Card color", 7)
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("Card color must be a six-digit hex color.")
    base_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:55].strip("-")
    base_id = base_id or "product"

    with STORE_LOCK:
        products = load_products()
        product_id = base_id
        suffix = 2
        existing_ids = {product["id"] for product in products}
        while product_id in existing_ids:
            product_id = f"{base_id}-{suffix}"
            suffix += 1
        product = {
            "id": product_id,
            "name": name,
            "category": category,
            "description": description,
            "price": price,
            "rating": None,
            "stock": stock,
            "emoji": emoji,
            "color": color,
            "art": "custom",
        }
        try:
            local_store.add_product(product, _load_seed_products)
        except local_store.LocalStoreError as error:
            raise RuntimeError(str(error)) from error
    return product


class StorefrontHandler(BaseHTTPRequestHandler):
    server_version = "FernwoodLocal/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}")

    def send_json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Send request data as application/json.")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length header.") from None
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("Request body must be between 1 byte and 1 MB.")
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must contain valid JSON.") from None
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def read_raw_json(self) -> bytes:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Webhook content type must be application/json.")
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length header.") from None
        if content_length < 1 or content_length > payment_gateway.MAX_WEBHOOK_BYTES:
            raise ValueError("Webhook body must be between 1 byte and 1 MB.")
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            raise ValueError("Webhook body was incomplete.")
        return body

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/products":
            try:
                self.send_json(HTTPStatus.OK, load_products())
            except RuntimeError as error:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return
        if path in {"/", "/index.html"}:
            file_name = "index.html"
        elif path in {"/admin", "/admin.html"}:
            file_name = "admin.html"
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            body = (BASE_DIR / file_name).read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not read {file_name}")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/payment/create-order":
            try:
                result = payment_gateway.create_order(self.read_json())
            except ValueError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except payment_gateway.PaymentConfigurationError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except payment_gateway.PaymentStorageError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except payment_gateway.PaymentNotificationError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except payment_gateway.PaymentProviderError as error:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            except Exception:
                logging.exception("Fernwood payment order creation failed.")
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "Could not create the payment order. Check the server log."},
                )
            else:
                self.send_json(HTTPStatus.CREATED, result)
            return
        if path == "/api/payment/verify":
            try:
                result = payment_gateway.verify_payment(self.read_json())
            except ValueError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except payment_gateway.PaymentConfigurationError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except payment_gateway.PaymentStorageError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except payment_gateway.PaymentNotificationError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except payment_gateway.PaymentProviderError as error:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            except Exception:
                logging.exception("Fernwood payment verification failed.")
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "Could not verify the payment. Check the server log."},
                )
            else:
                self.send_json(HTTPStatus.OK, result)
            return
        if path == "/api/payment/webhook":
            try:
                result = payment_gateway.verify_webhook(
                    self.read_raw_json(),
                    self.headers.get("X-Razorpay-Signature"),
                    self.headers.get("X-Razorpay-Event-Id"),
                )
            except ValueError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except payment_gateway.PaymentConfigurationError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except payment_gateway.PaymentStorageError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except payment_gateway.PaymentNotificationError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except Exception:
                logging.exception("Fernwood Razorpay webhook processing failed.")
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "Could not process the payment webhook. Check the server log."},
                )
            else:
                self.send_json(HTTPStatus.OK, result)
            return
        if path != "/api/products":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            product = create_product(self.read_json())
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        else:
            self.send_json(HTTPStatus.CREATED, product)

    def do_PATCH(self) -> None:
        match = re.fullmatch(r"/api/products/([^/]+)/stock", urlsplit(self.path).path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        product_id = unquote(match.group(1))
        try:
            payload = self.read_json()
            stock = parse_nonnegative_integer(payload.get("stock"), "Stock", 1_000_000)
            with STORE_LOCK:
                product = local_store.set_stock(
                    product_id, stock, _load_seed_products
                )
                if product is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Product not found."})
                    return
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        else:
            self.send_json(HTTPStatus.OK, product)

    def do_DELETE(self) -> None:
        match = re.fullmatch(r"/api/products/([^/]+)", urlsplit(self.path).path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        product_id = unquote(match.group(1))
        try:
            with STORE_LOCK:
                if not local_store.delete_product(product_id, _load_seed_products):
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Product not found."})
                    return
        except RuntimeError as error:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        else:
            self.send_json(HTTPStatus.OK, {"deleted": product_id})


def main() -> None:
    try:
        load_products()
        server = ThreadingHTTPServer((HOST, PORT), StorefrontHandler)
    except (OSError, RuntimeError) as error:
        raise SystemExit(f"Could not start Fernwood: {error}") from error
    print(f"Fernwood is running at http://{HOST}:{PORT}")
    print(f"Inventory manager: http://{HOST}:{PORT}/admin")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Fernwood.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()