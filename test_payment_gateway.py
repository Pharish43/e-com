import copy
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import email_notifications
import local_store
import payment_gateway


class PaymentGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        root = Path(self.temp_directory.name)
        self.data_file_patch = patch.object(local_store, "DATA_FILE", root / "products.json")
        self.temp_file_patch = patch.object(local_store, "TEMP_FILE", root / "products.json.tmp")
        self.catalog_file_patch = patch.object(local_store, "CATALOG_FILE", root / "catalog.json")
        self.catalog_temp_file_patch = patch.object(local_store, "CATALOG_TEMP_FILE", root / "catalog.json.tmp")
        self.data_file_patch.start()
        self.temp_file_patch.start()
        self.catalog_file_patch.start()
        self.catalog_temp_file_patch.start()
        self.addCleanup(self.data_file_patch.stop)
        self.addCleanup(self.temp_file_patch.stop)
        self.addCleanup(self.catalog_file_patch.stop)
        self.addCleanup(self.catalog_temp_file_patch.stop)
        self.addCleanup(self.temp_directory.cleanup)
        self.product = {
            "id": "test-product",
            "name": "Test product",
            "category": "Plants",
            "description": "",
            "price": 33.0,
            "rating": None,
            "stock": 4,
            "emoji": "🌱",
            "color": "#dcebd8",
            "art": "custom",
        }
        self.seed_loader = lambda: [copy.deepcopy(self.product)]
        local_store.load_products(self.seed_loader)

    def test_public_catalog_contains_products_only(self):
        catalog = json.loads(
            local_store.CATALOG_FILE.read_text(encoding="utf-8")
        )

        self.assertEqual(catalog, [self.product])
        self.assertNotIn("pending_orders", json.dumps(catalog))
        self.assertNotIn("processed_orders", json.dumps(catalog))

    def test_loading_inventory_repairs_a_stale_public_catalog(self):
        local_store.CATALOG_FILE.write_text("[]\n", encoding="utf-8")

        products = local_store.load_products(self.seed_loader)
        catalog = json.loads(
            local_store.CATALOG_FILE.read_text(encoding="utf-8")
        )

        self.assertEqual(catalog, products)

    def test_inventory_changes_update_the_publishable_catalog(self):
        local_store.set_stock("test-product", 2, self.seed_loader)
        catalog = json.loads(
            local_store.CATALOG_FILE.read_text(encoding="utf-8")
        )

        self.assertEqual(catalog[0]["stock"], 2)

    def test_adding_product_updates_the_publishable_catalog(self):
        added_product = {
            **self.product,
            "id": "new-product",
            "name": "New product",
            "stock": 7,
        }
        local_store.add_product(added_product, self.seed_loader)
        catalog = json.loads(
            local_store.CATALOG_FILE.read_text(encoding="utf-8")
        )

        self.assertEqual(catalog, [self.product, added_product])

    def test_catalog_controls_order_total(self):
        app = type("App", (), {})()
        app.STORE_LOCK = threading.RLock()
        app.load_products = lambda: [copy.deepcopy(self.product)]
        with patch.object(payment_gateway, "_storefront", return_value=app):
            items, amount = payment_gateway._checkout_items(
                {"items": [{"product_id": "test-product", "quantity": 2}]}
            )

        self.assertEqual(amount, 6600)
        self.assertEqual(items[0]["unit_price_paise"], 3300)
        self.assertEqual(items[0]["quantity"], 2)

    def test_payment_confirmation_updates_stock_once_and_emails_once(self):
        local_store.add_pending_order(
            "order_test",
            3300,
            "INR",
            [{"product_id": "test-product", "quantity": 1}],
            self.seed_loader,
        )
        email_sender = patch.object(email_notifications, "payment_confirmed")
        send_email = email_sender.start()
        self.addCleanup(email_sender.stop)
        with patch.object(
            payment_gateway, "_seed_loader", return_value=self.seed_loader
        ):
            first = payment_gateway._mark_paid(
                "order_test", "pay_test", 3300, "INR"
            )
            duplicate = payment_gateway._mark_paid(
                "order_test", "pay_test", 3300, "INR"
            )

        self.assertFalse(first["already_processed"])
        self.assertTrue(duplicate["already_processed"])
        self.assertEqual(local_store.load_products(self.seed_loader)[0]["stock"], 3)
        self.assertEqual(send_email.call_count, 1)

    def test_order_email_contains_customer_and_item_but_local_state_does_not(self):
        customer = {
            "name": "Test Customer",
            "email": "customer@example.test",
            "phone": "9876543210",
            "address": "1 Test Road",
            "city": "Pune",
            "state": "Maharashtra",
            "pincode": "411001",
        }
        items = [
            {
                "product_id": "test-product",
                "name": "Test product",
                "quantity": 1,
                "unit_price_paise": 3300,
            }
        ]
        settings = {
            "RAZORPAY_KEY_ID": "rzp_test_placeholder",
            "RAZORPAY_KEY_SECRET": "test-secret-placeholder",
            "RAZORPAY_WEBHOOK_SECRET": "test-webhook-placeholder",
        }
        with (
            patch.dict(os.environ, settings),
            patch.object(email_notifications, "validate_configuration"),
            patch.object(payment_gateway, "_address", return_value=customer),
            patch.object(payment_gateway, "_checkout_items", return_value=(items, 3300)),
            patch.object(
                payment_gateway,
                "_razorpay_request",
                return_value={"id": "order_new", "amount": 3300, "currency": "INR"},
            ),
            patch.object(payment_gateway, "_seed_loader", return_value=self.seed_loader),
            patch.object(email_notifications, "order_created") as send_order_email,
        ):
            payment_gateway.create_order({"shipping_address": customer, "items": []})

        send_order_email.assert_called_once_with(
            "order_new", 3300, "INR", customer, items
        )
        saved = json.loads(local_store.DATA_FILE.read_text(encoding="utf-8"))
        pending_order = saved["pending_orders"]["order_new"]
        self.assertEqual(
            pending_order["items"],
            [{"product_id": "test-product", "quantity": 1}],
        )
        self.assertNotIn("customer", json.dumps(pending_order))
        self.assertNotIn("phone", json.dumps(pending_order))

    def test_order_notification_email_includes_customer_address_and_products(self):
        customer = {
            "name": "Test Customer",
            "email": "customer@example.test",
            "phone": "9876543210",
            "address": "1 Test Road",
            "city": "Pune",
            "state": "Maharashtra",
            "pincode": "411001",
        }
        items = [
            {
                "product_id": "test-product",
                "name": "Test product",
                "quantity": 2,
                "unit_price_paise": 3300,
            }
        ]
        with patch.object(email_notifications, "send_notification") as send_email:
            email_notifications.order_created(
                "order_test", 6600, "INR", customer, items
            )

        subject, body = send_email.call_args.args
        self.assertIn("order_test", subject)
        self.assertIn("Test Customer", body)
        self.assertIn("1 Test Road, Pune, Maharashtra 411001", body)
        self.assertIn("Test product", body)
        self.assertIn("INR 66.00", body)

    def test_legacy_product_array_migrates_to_json_store_without_reset(self):
        local_store.DATA_FILE.write_text(
            json.dumps([self.product]), encoding="utf-8"
        )

        products = local_store.load_products(
            lambda: self.fail("Existing product data must be preserved")
        )
        saved = json.loads(local_store.DATA_FILE.read_text(encoding="utf-8"))

        self.assertEqual(products, [self.product])
        self.assertEqual(saved["products"], [self.product])
        self.assertEqual(saved["pending_orders"], {})
        self.assertEqual(saved["processed_orders"], {})

    def test_webhook_rejects_invalid_signature(self):
        raw_body = json.dumps({"event": "payment.failed"}).encode()
        environment = {
            "RAZORPAY_KEY_ID": "rzp_test_placeholder",
            "RAZORPAY_KEY_SECRET": "test-secret-placeholder",
            "RAZORPAY_WEBHOOK_SECRET": "test-webhook-placeholder",
        }
        with patch.dict(os.environ, environment):
            with self.assertRaisesRegex(
                payment_gateway.PaymentRequestError, "signature"
            ):
                payment_gateway.verify_webhook(
                    raw_body, "invalid-signature", "event_test"
                )

    def test_webhook_verifies_raw_body_and_ignores_unhandled_event(self):
        raw_body = json.dumps({"event": "payment.failed"}).encode()
        secret = "test-webhook-placeholder"
        signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        environment = {
            "RAZORPAY_KEY_ID": "rzp_test_placeholder",
            "RAZORPAY_KEY_SECRET": "test-secret-placeholder",
            "RAZORPAY_WEBHOOK_SECRET": secret,
        }
        with patch.dict(os.environ, environment):
            result = payment_gateway.verify_webhook(
                raw_body, signature, "event_test"
            )

        self.assertEqual(result, {"received": True, "processed": False})


if __name__ == "__main__":
    unittest.main()
