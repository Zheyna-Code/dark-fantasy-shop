"""Payment and inventory regressions; run with TEST_DATABASE_URL set to PostgreSQL."""
import asyncio
import hashlib
import hmac
import json
import os
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from shop_backend import ApiError, Store, register_api

USER = 222
CRYPTO_TOKEN = "crypto-token-for-local-tests-only"


class PaymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = (os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
        if not url:
            self.skipTest("PostgreSQL requires TEST_DATABASE_URL or DATABASE_URL")
        self.store = Store(url, schema="test_payments_" + uuid.uuid4().hex)
        self.addAsyncCleanup(self.store.close)
        await self.store.init_db()
        self.addAsyncCleanup(self.store.execute_raw, f'DROP SCHEMA "{self.store.schema}" CASCADE')
        await self.store.add_balance(USER, 100)
        app = web.Application()
        register_api(app, self.store, "test-bot-token", set(), "", crypto_token=CRYPTO_TOKEN)
        self.client = TestClient(TestServer(app))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()

    async def scalar(self, query, *args):
        async with self.store.connection() as db:
            return await db.fetchval(query, *args)

    async def stocked_product(self):
        product = await self.store.save_product({
            "name": "ChatGPT", "category": "chatgpt", "price": 100, "stock": 0,
        })
        await self.store.add_deliveries(product["id"], {"items": ["account-for-local-tests"]})
        return product["id"]

    async def order(self, product_id, payment="crypto"):
        return await self.store.create_order(USER, {
            "cart": [{"id": product_id, "qty": 1}], "payment": payment,
            "idempotency_key": uuid.uuid4().hex,
        })

    async def invoice(self, purpose="topup", order_id=None):
        invoice = await self.store.create_crypto_invoice(USER, 100, purpose, order_id)
        await self.store.bind_crypto_invoice(invoice["payload"], "9001")
        return invoice

    def webhook(self, invoice):
        raw = json.dumps({"update_type": "invoice_paid", "payload": {
            "payload": invoice["payload"], "invoice_id": 9001,
        }}).encode()
        key = hashlib.sha256(CRYPTO_TOKEN.encode()).digest()
        signature = hmac.new(key, raw, hashlib.sha256).hexdigest()
        return raw, {"Crypto-Pay-API-Signature": signature}

    async def test_webhook_rejects_missing_invalid_and_tampered_signatures(self):
        invoice = await self.invoice()
        raw, signed_headers = self.webhook(invoice)
        for body, headers in ((raw, {}), (raw, {"Crypto-Pay-API-Signature": "0" * 64}),
                              (raw + b" ", signed_headers)):
            with self.subTest(headers=headers, tampered=body != raw):
                response = await self.client.post("/api/payments/crypto/webhook", data=body, headers=headers)
                self.assertEqual(response.status, 401)
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 100)
        self.assertEqual(await self.scalar("SELECT status FROM crypto_invoices WHERE payload=$1", invoice["payload"]), "new")
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM bot_outbox"), 0)

    async def test_signed_webhook_retries_credit_and_notify_once(self):
        invoice = await self.invoice()
        raw, headers = self.webhook(invoice)
        for _ in range(2):
            response = await self.client.post("/api/payments/crypto/webhook", data=raw, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"ok": True})
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 200)
        self.assertEqual(await self.scalar("SELECT status FROM crypto_invoices WHERE payload=$1", invoice["payload"]), "paid")
        notices = await self.store.claim_bot_notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["kind"], "topup")
        self.assertEqual(notices[0]["user_id"], USER)
        self.assertEqual(notices[0]["payload"]["amount"], 100)
        self.assertEqual(notices[0]["payload"]["balance"], 200)

    async def test_concurrent_crypto_settlement_credits_once(self):
        invoice = await self.invoice()
        results = await asyncio.gather(*(
            self.store.settle_crypto_invoice(invoice["payload"], 9001) for _ in range(2)
        ))
        self.assertEqual(sum(bool(result.get("replayed")) for result in results), 1)
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 200)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM bot_outbox"), 1)

    async def test_invoice_id_mismatch_leaves_payment_untouched(self):
        invoice = await self.invoice()
        with self.assertRaises(ApiError) as error:
            await self.store.settle_crypto_invoice(invoice["payload"], 9002)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 100)
        self.assertEqual(await self.scalar("SELECT status FROM crypto_invoices WHERE payload=$1", invoice["payload"]), "new")
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM bot_outbox"), 0)

    async def test_crypto_order_retry_issues_goods_and_queues_delivery_once(self):
        product_id = await self.stocked_product()
        order = await self.order(product_id)
        invoice = await self.invoice("order", order["order_id"])
        result = await self.store.settle_crypto_invoice(invoice["payload"], 9001)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["issued"][0]["items"][0]["payloads"], ["account-for-local-tests"])
        replay = await self.store.settle_crypto_invoice(invoice["payload"], 9001)
        self.assertTrue(replay["replayed"])
        self.assertNotIn("issued", replay)
        self.assertEqual(await self.scalar("SELECT stock FROM products WHERE id=$1", product_id), 0)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM deliveries WHERE status='issued' AND order_id=$1", order["order_id"]), 1)
        self.assertEqual(await self.scalar("SELECT status FROM orders WHERE id=$1", order["order_id"]), "done")
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 100)
        notices = await self.store.claim_bot_notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["kind"], "delivery")
        self.assertEqual(notices[0]["user_id"], USER)
        self.assertEqual(notices[0]["payload"], result["issued"][0])

    async def test_outbox_failure_rolls_back_payment_and_delivery(self):
        product_id = await self.stocked_product()
        order = await self.order(product_id)
        invoice = await self.invoice("order", order["order_id"])
        with patch.object(self.store, "_enqueue_bot_notice", AsyncMock(side_effect=RuntimeError("outbox unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                await self.store.settle_crypto_invoice(invoice["payload"], 9001)
        self.assertEqual(await self.scalar("SELECT status FROM orders WHERE id=$1", order["order_id"]), "new")
        self.assertEqual(await self.scalar("SELECT status FROM crypto_invoices WHERE payload=$1", invoice["payload"]), "new")
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM deliveries WHERE status='ready' AND order_id IS NULL"), 1)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM bot_outbox"), 0)
        result = await self.store.settle_crypto_invoice(invoice["payload"], 9001)
        self.assertEqual(result["status"], "done")
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM bot_outbox"), 1)

    async def test_expired_crypto_reservation_restocks_once_and_rejects_late_payment(self):
        product_id = await self.stocked_product()
        order = await self.order(product_id)
        invoice = await self.invoice("order", order["order_id"])
        async with self.store.connection() as db:
            await db.execute("UPDATE crypto_invoices SET created_at=$1 WHERE payload=$2", int(time.time()) - 121, invoice["payload"])
        self.assertEqual(await self.store.expire_crypto_order_reservations(), 1)
        self.assertEqual(await self.store.expire_crypto_order_reservations(), 0)
        with self.assertRaises(ApiError) as error:
            await self.store.settle_crypto_invoice(invoice["payload"], 9001)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(await self.scalar("SELECT stock FROM products WHERE id=$1", product_id), 1)
        self.assertEqual(await self.scalar("SELECT status FROM orders WHERE id=$1", order["order_id"]), "cancelled")
        self.assertEqual(await self.scalar("SELECT status FROM crypto_invoices WHERE payload=$1", invoice["payload"]), "expired")
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM deliveries WHERE status='ready'"), 1)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM bot_outbox"), 0)

    async def test_balance_payment_checks_owner_funds_and_duplicate_payment(self):
        product_id = await self.stocked_product()
        order = await self.order(product_id, "manual")
        await self.store.add_balance(USER + 1, 100)
        with self.assertRaises(ApiError) as error:
            await self.store.pay_order_balance(USER + 1, order["order_id"])
        self.assertEqual(error.exception.status, 404)
        async with self.store.connection() as db:
            await db.execute("UPDATE users SET balance=99 WHERE user_id=$1", USER)
        with self.assertRaises(ApiError) as error:
            await self.store.pay_order_balance(USER, order["order_id"])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 99)
        self.assertEqual(await self.scalar("SELECT status FROM orders WHERE id=$1", order["order_id"]), "new")
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM deliveries WHERE status='ready'"), 1)
        await self.store.add_balance(USER, 1)
        result = await self.store.pay_order_balance(USER, order["order_id"])
        self.assertEqual(result["status"], "done")
        with self.assertRaises(ApiError) as error:
            await self.store.pay_order_balance(USER, order["order_id"])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 0)
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER + 1), 100)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM deliveries WHERE status='issued'"), 1)

    async def test_competing_balance_payments_cannot_overdraw_wallet(self):
        orders = [await self.order(await self.stocked_product(), "manual") for _ in range(2)]
        results = await asyncio.gather(*(
            self.store.pay_order_balance(USER, order["order_id"]) for order in orders
        ), return_exceptions=True)
        successes = [result for result in results if isinstance(result, dict)]
        errors = [result for result in results if isinstance(result, ApiError)]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(successes[0]["status"], "done")
        self.assertEqual(len(errors), 1, results)
        self.assertEqual(errors[0].status, 409)
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 0)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM orders WHERE status='new'"), 1)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM deliveries WHERE status='issued'"), 1)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM deliveries WHERE status='ready'"), 1)

    async def test_failed_multiline_checkout_rolls_back_stock_and_can_retry(self):
        first = await self.stocked_product()
        second = await self.stocked_product()
        async with self.store.connection() as db:
            await db.execute("UPDATE products SET active=0 WHERE id=$1", second)
        body = {"cart": [{"id": first, "qty": 1}, {"id": second, "qty": 1}],
                "payment": "manual", "idempotency_key": "rollback-checkout"}
        with self.assertRaises(ApiError) as error:
            await self.store.create_order(USER, body)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(await self.scalar("SELECT stock FROM products WHERE id=$1", first), 1)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM orders"), 0)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM deliveries WHERE status='ready'"), 2)
        self.assertEqual(await self.scalar("SELECT balance FROM users WHERE user_id=$1", USER), 100)
        async with self.store.connection() as db:
            await db.execute("UPDATE products SET active=1 WHERE id=$1", second)
        result = await self.store.create_order(USER, body)
        self.assertFalse(result["replayed"])
        self.assertEqual(result["total"], 200)
        self.assertEqual(await self.scalar("SELECT SUM(stock) FROM products"), 0)
        self.assertEqual(await self.scalar("SELECT COUNT(*) FROM orders"), 1)
