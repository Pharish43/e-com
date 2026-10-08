import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import admin
import local_store


class InventoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        data_file = Path(self.temp_directory.name) / "data" / "products.json"
        self.data_file_patch = patch.object(local_store, "DATA_FILE", data_file)
        self.temp_file_patch = patch.object(
            local_store, "TEMP_FILE", data_file.with_suffix(".json.tmp")
        )
        self.products_file_patch = patch.object(admin, "PRODUCTS_FILE", data_file)
        self.data_file_patch.start()
        self.temp_file_patch.start()
        self.products_file_patch.start()
        self.addCleanup(self.products_file_patch.stop)
        self.addCleanup(self.temp_file_patch.stop)
        self.addCleanup(self.data_file_patch.stop)
        self.addCleanup(self.temp_directory.cleanup)
        self.seed_file_patch = patch.object(
            admin, "SEED_PRODUCTS_FILE", Path(self.temp_directory.name) / "missing.json"
        )
        self.seed_file_patch.start()
        self.addCleanup(self.seed_file_patch.stop)
        self.environment_patch = patch.dict(
            os.environ,
            {
                "INVENTORY_ADMIN_PASSWORD": "test-inventory-password",
                "FRONTEND_ORIGIN": "https://shop.example",
            },
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), admin.StorefrontHandler
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server_thread.join)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, *, method="GET", payload=None, password=None):
        headers = {"Origin": "https://shop.example"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if password is not None:
            headers["X-Inventory-Password"] = password
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, json.loads(response.read())

    def test_catalog_is_public_and_allows_only_configured_cors_origin(self):
        status, headers, products = self.request("/api/products")

        self.assertEqual(status, 200)
        self.assertEqual(products, [])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://shop.example")

    def test_inventory_mutations_require_password(self):
        payload = {
            "name": "Live product",
            "category": "Plants",
            "description": "A hosted inventory item",
            "price": 25,
            "stock": 4,
        }
        status, _, error = self.request(
            "/api/products", method="POST", payload=payload
        )

        self.assertEqual(status, 401)
        self.assertEqual(error["error"], "Inventory sign-in required.")
        self.assertFalse(local_store.DATA_FILE.exists())

    def test_authenticated_inventory_changes_are_visible_to_public_catalog(self):
        payload = {
            "name": "Live product",
            "category": "Plants",
            "description": "A hosted inventory item",
            "price": 25,
            "stock": 4,
        }
        status, _, created = self.request(
            "/api/products",
            method="POST",
            payload=payload,
            password="test-inventory-password",
        )
        catalog_status, _, products = self.request("/api/products")

        self.assertEqual(status, 201)
        self.assertEqual(catalog_status, 200)
        self.assertEqual(products, [created])
        self.assertTrue(local_store.DATA_FILE.exists())

    def test_stock_and_delete_operations_require_password(self):
        payload = {
            "name": "Protected product",
            "category": "Pots",
            "description": "",
            "price": 12,
            "stock": 2,
        }
        _, _, created = self.request(
            "/api/products",
            method="POST",
            payload=payload,
            password="test-inventory-password",
        )
        stock_status, _, _ = self.request(
            f"/api/products/{created['id']}/stock",
            method="PATCH",
            payload={"stock": 9},
        )
        delete_status, _, _ = self.request(
            f"/api/products/{created['id']}", method="DELETE"
        )

        self.assertEqual(stock_status, 401)
        self.assertEqual(delete_status, 401)

    def test_admin_session_rejects_wrong_password(self):
        status, _, _ = self.request(
            "/api/admin/session", password="incorrect-password"
        )
        self.assertEqual(status, 401)

    def test_cors_preflight_allows_inventory_password_header(self):
        request = Request(
            f"{self.base_url}/api/products",
            headers={
                "Origin": "https://shop.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-inventory-password",
            },
            method="OPTIONS",
        )

        with urlopen(request) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(
                response.headers["Access-Control-Allow-Origin"],
                "https://shop.example",
            )
            self.assertIn(
                "X-Inventory-Password",
                response.headers["Access-Control-Allow-Headers"],
            )

    def test_fresh_persistent_catalog_is_seeded_from_checked_in_products_only(self):
        seed_file = Path(self.temp_directory.name) / "seed.json"
        products = [{"id": "seed-product", "name": "Seed product"}]
        seed_file.write_text(
            json.dumps(
                {
                    "products": products,
                    "pending_orders": {"local-order": {}},
                    "processed_orders": {"old-order": {}},
                }
            ),
            encoding="utf-8",
        )
        seed_patch = patch.object(admin, "SEED_PRODUCTS_FILE", seed_file)
        seed_patch.start()
        self.addCleanup(seed_patch.stop)

        self.assertEqual(admin._load_seed_products(), products)


if __name__ == "__main__":
    unittest.main()
