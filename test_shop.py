"""Run: python -m unittest discover -p 'test_*.py' -v. No live bot required."""
import asyncio
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendPhoto
from aiogram.types import Update, User

import app as shop
from shop_backend import ApiError, Store, verify_init_data

TOKEN = "123456:TEST_TOKEN_FOR_LOCAL_TESTS_ONLY"
ADMIN = 111
USER = 222
TESTER = 333


def init_data(user_id=USER, age=0, **overrides):
    data = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id, "first_name": "Tester"})}
    data.update(overrides)
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def headers(user_id=USER):
    return {"Authorization": "tma " + init_data(user_id)}


class ShopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lavka-test-")
        self.store = Store(Path(self.temp.name) / "shop.db")
        await self.store.init_db()
        self.patches = [patch.object(shop, "store", self.store), patch.object(shop, "BOT_TOKEN", TOKEN),
                        patch.object(shop, "ADMIN_IDS", {ADMIN}), patch.object(shop, "TESTER_IDS", {TESTER}),
                        patch.object(shop, "WEBAPP_URL", "https://example.org")]
        for item in self.patches:
            item.start()
        self.client = TestClient(TestServer(await shop.make_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def product(self, stock=3, price=100, **kwargs):
        result = await self.store.save_product({"name": "ChatGPT 1M", "category": "chatgpt", "price": price, "stock": stock, **kwargs})
        return result["id"]

    def order(self, product_id, key="order-key-123", qty=1, kind="order"):
        return {"cart": [{"id": product_id, "qty": qty}], "idempotency_key": key, "kind": kind}

    async def test_public_routes_and_exact_photos(self):
        for path in ("/", "/health", "/api/catalog", "/api/config", "/api/products", "/static/1.jpg", "/static/shop.jpg", "/static/2.jpg"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
        catalog = await (await self.client.get("/api/catalog")).json()
        self.assertEqual([c["slug"] for c in catalog["categories"]], ["chatgpt", "gemini", "capcut"])
        self.assertEqual(catalog["products"], [])
        html = await (await self.client.get("/")).text()
        self.assertNotIn('<nav class="main-nav"', html)
        self.assertNotIn("/api/art/", html)

    async def test_signature_freshness_and_duplicate_fields(self):
        self.assertEqual(verify_init_data(init_data(), TOKEN)["id"], USER)
        for raw in (init_data(age=90000), init_data(age=-120), init_data() + "&auth_date=1", "bad", init_data(user="[]"), init_data(user=json.dumps({"id": True}))):
            self.assertIsNone(verify_init_data(raw, TOKEN), raw)
        self.assertIsNone(verify_init_data(init_data(), "wrong"))

    async def test_admin_permissions(self):
        for path in ("/api/me", "/api/admin/catalog", "/api/admin/orders"):
            self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.get("/api/admin/catalog", headers=headers())).status, 403)
        self.assertEqual((await self.client.get("/api/admin/catalog", headers=headers(ADMIN))).status, 200)
        me = await (await self.client.get("/api/me", headers=headers(ADMIN))).json()
        self.assertTrue(me["is_admin"])
        self.assertEqual((await self.client.post("/api/admin/products", headers=headers(), json={})).status, 403)

    async def test_stable_traveler_numbers(self):
        one = await self.store.profile({"id": USER})
        again = await self.store.profile({"id": USER})
        two = await self.store.profile({"id": ADMIN})
        self.assertEqual(one["traveler_no"], again["traveler_no"])
        self.assertEqual(two["traveler_no"], one["traveler_no"] + 1)
        self.assertEqual(one["balance"], 0)

    async def test_admin_crud_and_validation(self):
        response = await self.client.post("/api/admin/products", headers=headers(ADMIN), json={"name": "Gemini Pro", "category": "gemini", "price": 400, "stock": 5})
        self.assertEqual(response.status, 200)
        item_id = (await response.json())["id"]
        response = await self.client.patch(f"/api/admin/products/{item_id}", headers=headers(ADMIN), json={"name": "Gemini Pro", "category": "gemini", "price": 450, "stock": 2, "active": False})
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.store.catalog())["products"], [])
        self.assertEqual(len((await self.store.catalog(True))["products"]), 1)
        for payload in ([], {}, {"name": "X", "category": "gemini", "price": -1}, {"name": "X", "category": "gemini", "stock": True}, {"name": "X", "category": "missing"}):
            self.assertEqual((await self.client.post("/api/admin/products", headers=headers(ADMIN), json=payload)).status, 400)
        response = await self.client.post("/api/admin/categories", headers=headers(ADMIN), json={"name": "Test", "slug": "test"})
        self.assertEqual(response.status, 200)
        category_id = (await response.json())["id"]
        self.assertEqual((await self.client.patch(f"/api/admin/categories/{category_id}", headers=headers(ADMIN), json={"name": "Renamed", "active": False})).status, 200)
        self.assertEqual((await self.client.patch(f"/api/admin/categories/{category_id}", headers=headers(ADMIN), json={"name": "Renamed", "slug": "other"})).status, 400)

    async def test_order_idempotency_and_paid_profile(self):
        product_id = await self.product()
        payload = self.order(product_id, qty=2)
        one = await self.store.create_order(USER, payload)
        replay = await self.store.create_order(USER, payload)
        self.assertEqual(one["order_id"], replay["order_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual((await self.store.catalog())["products"][0]["stock"], 1)
        self.assertEqual((await self.store.profile({"id": USER}))["purchases"], 0)
        with self.assertRaises(ApiError):
            await self.store.create_order(USER, self.order(product_id, qty=1))
        await self.store.change_order(one["order_id"], "paid")
        profile = await self.store.profile({"id": USER})
        self.assertEqual((profile["purchases"], profile["spent"], profile["favorite_product"]), (2, 200, "ChatGPT 1M"))
        await self.store.change_order(one["order_id"], "done")
        self.assertEqual((await self.store.profile({"id": USER}))["spent"], 200)
        with self.assertRaises(ApiError):
            await self.store.change_order(one["order_id"], "cancelled")

    async def test_stock_concurrent_requests_and_cancellation(self):
        product_id = await self.product(stock=1)
        results = await asyncio.gather(self.store.create_order(USER, self.order(product_id, "unique-key-1")), self.store.create_order(ADMIN, self.order(product_id, "unique-key-2")), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, ApiError) for result in results), 1)
        accepted = next(result for result in results if isinstance(result, dict))
        await self.store.change_order(accepted["order_id"], "cancelled")
        await self.store.change_order(accepted["order_id"], "cancelled")
        self.assertEqual((await self.store.catalog())["products"][0]["stock"], 1)

    async def test_preorder_never_restocks(self):
        product_id = await self.product(stock=0)
        order = await self.store.create_order(USER, self.order(product_id, kind="preorder"))
        self.assertEqual((await self.store.profile({"id": USER}))["preorders"], 1)
        await self.store.change_order(order["order_id"], "cancelled")
        self.assertEqual((await self.store.catalog())["products"][0]["stock"], 0)

    async def test_preorder_prepay_queue_priority(self):
        """Предзаказ с предоплатой 100%: поступивший товар сначала уходит оплаченным предзаказам."""
        product_id = await self.product(stock=0)
        order = await self.store.create_order(USER, self.order(product_id, kind="preorder", key="pre-key-1"))
        paid = await self.store.change_order(order["order_id"], "paid")
        self.assertEqual(paid["status"], "paid")  # товара ещё нет — просто ждёт в очереди
        result = await self.store.add_deliveries(product_id, {"items": ["ticket-1"]})
        self.assertEqual(result["delivered_to_preorders"], 1)
        self.assertEqual(result["stock_added"], 0)  # вся поставка ушла предзаказу
        self.assertEqual((await self.store.catalog())["products"][0]["stock"], 0)
        data = await self.store.orders()
        done = next(item for item in data["orders"] if item["id"] == order["order_id"])
        self.assertEqual(done["status"], "done")
        # Следующая единица уже попадает на полку: очередь пуста.
        result = await self.store.add_deliveries(product_id, {"items": ["ticket-2"]})
        self.assertEqual((result["delivered_to_preorders"], result["stock_added"]), (0, 1))
        self.assertEqual((await self.store.catalog())["products"][0]["stock"], 1)

    async def test_paid_preorder_fulfilled_on_payment_when_stocked(self):
        product_id = await self.product(stock=0)
        order = await self.store.create_order(USER, self.order(product_id, kind="preorder", key="pre-key-2"))
        await self.store.add_deliveries(product_id, {"items": ["early-1"]})
        result = await self.store.change_order(order["order_id"], "paid")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["issued"][0]["items"][0]["payloads"], ["early-1"])
        self.assertEqual((await self.store.catalog())["products"][0]["stock"], 0)

    async def test_preorder_queue_does_not_skip_earlier_large_order(self):
        product_id = await self.product(stock=0)
        first = await self.store.create_order(USER, self.order(product_id, key="fifo-first", qty=2, kind="preorder"))
        second = await self.store.create_order(ADMIN, self.order(product_id, key="fifo-second", qty=1, kind="preorder"))
        await self.store.change_order(first["order_id"], "paid")
        await self.store.change_order(second["order_id"], "paid")
        await self.store.add_deliveries(product_id, {"items": ["one"]})
        orders = (await self.store.orders())["orders"]
        self.assertEqual({item["id"]: item["status"] for item in orders}, {first["order_id"]: "paid", second["order_id"]: "paid"})
        await self.store.add_deliveries(product_id, {"items": ["two"]})
        orders = (await self.store.orders())["orders"]
        self.assertEqual(next(item["status"] for item in orders if item["id"] == first["order_id"]), "done")
        self.assertEqual(next(item["status"] for item in orders if item["id"] == second["order_id"]), "paid")

    async def test_paid_order_auto_issue(self):
        product_id = await self.product(stock=0)
        await self.store.add_deliveries(product_id, {"items": ["secret-1"]})
        order = await self.store.create_order(USER, self.order(product_id, key="buy-key-1"))
        self.assertEqual((await self.store.catalog())["products"][0]["stock"], 0)
        result = await self.store.change_order(order["order_id"], "paid")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["issued"][0]["items"][0]["payloads"], ["secret-1"])

    async def test_test_products_and_tester_purchase(self):
        plain_id = await self.product(stock=1)
        test_id = await self.product(stock=0, name="TEST ITEM", is_test=True)
        self.assertEqual([item["id"] for item in (await self.store.catalog())["products"]], [plain_id])
        self.assertEqual(len((await self.store.catalog(include_test=True))["products"]), 2)
        with self.assertRaises(ApiError):
            await self.store.create_test_order(USER, test_id)  # нет загруженных строк
        await self.store.add_deliveries(test_id, {"items": ["login:pass"]})
        result = await self.store.create_test_order(USER, test_id)
        self.assertGreater(result["order_id"], 0)
        self.assertEqual(result["issued"][0]["items"][0]["payloads"], ["login:pass"])
        self.assertEqual((await self.store.product(test_id))["stock"], 0)
        profile = await self.store.profile({"id": USER})
        self.assertEqual((profile["purchases"], profile["spent"]), (0, 0))  # тест-покупки не в статистике
        with self.assertRaises(ApiError):
            await self.store.create_test_order(USER, test_id)  # строки закончились
        with self.assertRaises(ApiError):
            await self.store.create_test_order(USER, plain_id)  # обычный товар не продаётся без оплаты

    async def test_test_order_http_and_permissions(self):
        test_id = await self.product(stock=0, is_test=True)
        await self.store.add_deliveries(test_id, {"items": ["one", "two"]})
        catalog = await (await self.client.get("/api/catalog")).json()
        self.assertEqual(catalog["products"], [])
        tester_catalog = await (await self.client.get("/api/catalog", headers=headers(TESTER))).json()
        self.assertEqual([item["id"] for item in tester_catalog["products"]], [test_id])
        response = await self.client.post("/api/order", headers=headers(), json=self.order(test_id, kind="test", key="test-key-1"))
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/order", headers=headers(TESTER), json=self.order(test_id, kind="test", key="test-key-2"))
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data["test"])
        self.assertEqual((data["total"], data["issued"][0]["items"][0]["payloads"]), (0, ["one"]))
        self.assertEqual((await self.client.post(f"/api/admin/products/{test_id}/deliveries", headers=headers(TESTER), json={"items": "x"})).status, 403)
        response = await self.client.post(f"/api/admin/products/{test_id}/deliveries", headers=headers(ADMIN), json={"items": "a\n\nb"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["added"], 2)
        view = await (await self.client.get(f"/api/admin/products/{test_id}/deliveries", headers=headers(ADMIN))).json()
        self.assertEqual(view["ready"], 3)  # "two" осталась после тест-покупки + "a" и "b"
        self.assertEqual(view["ready_samples"][:1], ["two"])

    async def test_bot_product_buttons_preorder_and_test(self):
        def styles(markup):
            return {(button.text, button.model_dump(exclude_none=True).get("style")) for row in markup.inline_keyboard for button in row}
        test_id = await self.product(stock=2, is_test=True)
        item = await self.store.product(test_id)
        pairs = styles(shop.product_keyboard(item, can_test=True))
        self.assertIn(("🧪 Тестовая покупка без оплаты", "success"), pairs)
        self.assertNotIn(("Купить", "success"), pairs)
        pairs = styles(shop.product_keyboard(item, can_test=False))
        self.assertFalse(any("Тестовая покупка" in text for text, _ in pairs))
        plain_id = await self.product(stock=0)
        item = await self.store.product(plain_id)
        pairs = styles(shop.product_keyboard(item))
        self.assertIn(("⏳ Предзаказ · предоплата 100%", None), pairs)  # предзаказ без цвета
        stocked_id = await self.product(stock=1)
        item = await self.store.product(stocked_id)
        pairs = styles(shop.product_keyboard(item, can_test=False, is_admin=True))
        self.assertIn(("Купить", "success"), pairs)
        self.assertIn(("Назад", "danger"), pairs)

    async def test_bot_test_purchase_sends_payload_to_chat(self):
        test_id = await self.product(stock=1, is_test=True)
        await self.store.add_deliveries(test_id, {"items": ["login:pass"]})
        callback = AsyncMock()
        callback.message = AsyncMock()
        callback.from_user = User(id=TESTER, is_bot=False, first_name="Tester")
        callback.data = f"buy:{test_id}"
        await shop.buy_callback(callback)
        text = callback.message.answer.call_args.args[0]
        self.assertIn("Тестовая покупка", text)
        self.assertIn("login:pass", text)
        # не-тестер получает отказ
        stranger = AsyncMock()
        stranger.message = AsyncMock()
        stranger.from_user = User(id=USER, is_bot=False, first_name="Stranger")
        stranger.data = f"buy:{test_id}"
        await shop.buy_callback(stranger)
        stranger.message.answer.assert_not_awaited()
        self.assertTrue(stranger.answer.call_args.kwargs.get("show_alert"))

    async def test_bot_preorder_button_creates_order(self):
        plain_id = await self.product(stock=0)
        callback = AsyncMock()
        callback.message = AsyncMock()
        callback.from_user = User(id=USER, is_bot=False, first_name="Tester")
        callback.data = f"preorder:{plain_id}"
        await shop.preorder_callback(callback)
        text = callback.message.answer.call_args.args[0]
        self.assertIn("Предзаказ №", text)
        self.assertIn("предоплате 100%", text)
        profile = await self.store.profile({"id": USER})
        self.assertEqual(profile["preorders"], 1)
        shelf = AsyncMock()
        shelf.photo = None
        shelf_callback = AsyncMock()
        shelf_callback.message, shelf_callback.from_user, shelf_callback.data = shelf, callback.from_user, "preorders"
        await shop.preorders_callback(shelf_callback)
        self.assertIn("Твои предзаказы", shelf.answer_photo.call_args.kwargs["caption"])
        self.assertIn("Заказ №", shelf.answer_photo.call_args.kwargs["caption"])
        # повторный предзаказ не создаёт дубликат
        again = AsyncMock()
        again.message = AsyncMock()
        again.from_user = User(id=USER, is_bot=False, first_name="Tester")
        again.data = f"preorder:{plain_id}"
        await shop.preorder_callback(again)
        self.assertIn("уже оформлен", again.answer.call_args.args[0])
        profile = await self.store.profile({"id": USER})
        self.assertEqual(profile["preorders"], 1)

    async def test_bot_upload_wizard_and_auto_issue(self):
        product_id = await self.product(stock=0)
        order = await self.store.create_order(USER, self.order(product_id, kind="preorder", key="up-key-1"))
        await self.store.change_order(order["order_id"], "paid")
        user = User(id=ADMIN, is_bot=False, first_name="Owner")
        callback = AsyncMock()
        callback.message = AsyncMock()
        callback.message.photo = None
        callback.from_user = user
        callback.data = f"upload:{product_id}"
        await shop.upload_callback(callback)
        self.assertIn(ADMIN, shop.upload_state)
        self.assertIn("одна строка = одна единица", callback.message.answer.call_args.args[0])
        wizard_message = AsyncMock()
        wizard_message.from_user = user
        wizard_message.text = "acc1:pw1\nacc2:pw2"
        await shop.upload_wizard(wizard_message)
        self.assertNotIn(ADMIN, shop.upload_state)
        reply = wizard_message.answer.call_args.args[0]
        self.assertIn("Загружено единиц товара: 2", reply)
        self.assertIn("предзаказам", reply)
        data = await self.store.orders()
        done = next(item for item in data["orders"] if item["id"] == order["order_id"])
        self.assertEqual(done["status"], "done")
        shop.upload_state.clear()

    async def test_notify_deliveries_sends_messages(self):
        bot = AsyncMock()
        shop.active_bot["bot"] = bot
        try:
            await shop.notify_deliveries([{"user_id": USER, "order_id": 7, "kind": "preorder",
                                           "items": [{"name": "ChatGPT 1M", "payloads": ["tok-1"]}]}])
            bot.send_message.assert_awaited_once()
            args = bot.send_message.call_args.args
            self.assertEqual(args[0], USER)
            self.assertIn("tok-1", args[1])
            self.assertIn("Предзаказ", args[1])
        finally:
            shop.active_bot.pop("bot", None)

    async def test_bad_order_rolls_back_all_inventory(self):
        product_id = await self.product(stock=1)
        payload = self.order(product_id)
        payload["cart"].append({"id": 99999, "qty": 1})
        self.assertEqual((await self.client.post("/api/order", headers=headers(), json=payload)).status, 409)
        self.assertEqual((await self.store.catalog())["products"][0]["stock"], 1)
        for payload in ([], {}, self.order(product_id, qty=-1), self.order(product_id, qty=True), {"cart": [None]}):
            self.assertEqual((await self.client.post("/api/order", headers=headers(), json=payload)).status, 400)
        self.assertEqual((await self.client.post("/api/order", headers=headers(), data="{")).status, 400)

    async def test_order_http_contract(self):
        product_id = await self.product()
        response = await self.client.post("/api/order", headers=headers(), json=self.order(product_id))
        self.assertEqual(response.status, 200)
        order_id = (await response.json())["order_id"]
        response = await self.client.patch(f"/api/admin/orders/{order_id}", headers=headers(ADMIN), json={"status": "paid"})
        self.assertEqual(response.status, 200)
        data = await (await self.client.get("/api/admin/orders", headers=headers(ADMIN))).json()
        self.assertIn("T", data["orders"][0]["created_at"])
        self.assertIsInstance(data["orders"][0]["items"], list)

    async def test_old_schema_migration_preserves_data(self):
        old = Store(Path(self.temp.name) / "old.db")
        async with old.connection() as db:
            await db.executescript("""
                CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, description TEXT, price INTEGER, category TEXT, emoji TEXT, stock INTEGER);
                CREATE TABLE orders(id INTEGER PRIMARY KEY,user_id INTEGER,items TEXT,total INTEGER,comment TEXT,status TEXT,created_at INTEGER);
                INSERT INTO products VALUES(1,'Old candle','',120,'candles','',20);
                INSERT INTO orders VALUES(1,222,'[]',120,'','done',0);
            """)
            await db.commit()
        await old.init_db()
        await old.init_db()
        self.assertEqual((await old.catalog())["products"], [])
        self.assertEqual(len((await old.catalog(True))["products"]), 1)
        self.assertEqual((await old.profile({"id": USER}))["spent"], 120)

    async def test_menu_keyboards_and_photo_files(self):
        menu = shop.menu_keyboard().model_dump(exclude_none=True)
        self.assertEqual(len(menu["inline_keyboard"]), 5)
        for row in menu["inline_keyboard"]:
            self.assertEqual(row[0]["style"], "primary")
        self.assertEqual(menu["inline_keyboard"][1][0]["text"], "🏪 Лавка Странника")
        self.assertIn("web_app", menu["inline_keyboard"][1][0])
        categories = shop.categories_keyboard().inline_keyboard[0]
        self.assertEqual([button.text for button in categories], ["ChatGPT", "CapCut", "Gemini"])
        self.assertEqual([button.callback_data for button in categories], ["category:chatgpt", "category:capcut", "category:gemini"])
        self.assertTrue(all(button.web_app is None for button in categories))
        with patch.object(shop, "WEBAPP_URL", ""):
            self.assertEqual(shop.menu_keyboard().inline_keyboard[1][0].callback_data, "menu:shop")
        for image in ("1.jpg", "shop.jpg", "2.jpg", "chatgptshop.jpg", "geminishop.jpg"):
            self.assertTrue((shop.BASE_DIR / "webapp" / image).is_file())

    async def test_menu_products_profile_handlers(self):
        message = AsyncMock()
        user = User(id=USER, is_bot=False, first_name="Tester")
        message.from_user = user
        await shop.cmd_menu(message)
        self.assertTrue(str(message.answer_photo.call_args.args[0].path).endswith("1.jpg"))
        for action, filename in (("products", "shop.jpg"), ("profile", "2.jpg")):
            callback = AsyncMock()
            callback.message, callback.from_user, callback.data = message, user, "menu:" + action
            await shop.menu_callback(callback)
            callback.answer.assert_awaited_once()
            self.assertTrue(str(message.answer_photo.call_args.args[0].path).endswith(filename))
        self.assertIn("Привет, путник", message.answer_photo.call_args.kwargs["caption"])

    async def test_admin_command(self):
        message = AsyncMock()
        message.from_user = User(id=USER, is_bot=False, first_name="Tester")
        await shop.cmd_admin(message)
        self.assertIn("ADMIN_IDS", message.answer.call_args.args[0])
        message.from_user = User(id=ADMIN, is_bot=False, first_name="Owner")
        await shop.cmd_admin(message)
        self.assertIn("view=admin", message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url)

    async def test_actual_dispatcher_and_callback_user_identity(self):
        bot = Bot(TOKEN)
        calls = []
        async def record(bot, method, **kwargs):
            calls.append(method)
            return True
        try:
            with patch.object(bot, "session", AsyncMock(side_effect=record)):
                await shop.dp.feed_update(bot, Update.model_validate({"update_id": 1, "message": {
                    "message_id": 1, "date": int(time.time()), "chat": {"id": USER, "type": "private"},
                    "from": {"id": USER, "is_bot": False, "first_name": "Tester"}, "text": "/menu",
                    "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
                }}))
                self.assertTrue(any(isinstance(call, SendPhoto) for call in calls))
                calls.clear()
                await shop.dp.feed_update(bot, Update.model_validate({"update_id": 2, "callback_query": {
                    "id": "callback-1", "chat_instance": "test", "data": "menu:profile",
                    "from": {"id": USER, "is_bot": False, "first_name": "Tester"},
                    "message": {"message_id": 2, "date": int(time.time()), "chat": {"id": USER, "type": "private"},
                                "from": {"id": 123456, "is_bot": True, "first_name": "Shop"}},
                }}))
                self.assertTrue(any(isinstance(call, AnswerCallbackQuery) for call in calls))
                self.assertTrue(any(isinstance(call, SendPhoto) and "Привет, путник" in call.caption for call in calls))
                async with self.store.connection() as db:
                    ids = [row[0] for row in await (await db.execute("SELECT user_id FROM users")).fetchall()]
                self.assertEqual(ids, [USER])
        finally:
            await bot.session.close()

    async def test_category_shelf_photo_and_stock_marks(self):
        await self.product(stock=2, price=590)
        await self.store.save_product({"name": "Gemini Pro", "category": "gemini", "price": 400, "stock": 0})
        user = User(id=USER, is_bot=False, first_name="Tester")
        message = AsyncMock()
        message.photo = None
        callback = AsyncMock()
        callback.message, callback.from_user, callback.data = message, user, "category:chatgpt"
        await shop.category_callback(callback)
        callback.answer.assert_awaited_once()
        self.assertTrue(str(message.answer_photo.call_args.args[0].path).endswith("chatgptshop.jpg"))
        caption = message.answer_photo.call_args.kwargs["caption"]
        self.assertIn("Полка ChatGPT", caption)
        self.assertIn("зелёная", caption)
        self.assertNotIn("ChatGPT 1M", caption)  # товары не перечисляются текстом — только кнопками
        rows = message.answer_photo.call_args.kwargs["reply_markup"].inline_keyboard
        product_button = next(button for row in rows for button in row if button.callback_data == "product:1")
        self.assertEqual(product_button.text, "ChatGPT 1M")
        self.assertEqual(product_button.model_dump(exclude_none=True)["style"], "success")  # есть в наличии — зелёная
        self.assertEqual(rows[-1][0].text, "Назад")
        self.assertEqual((rows[-1][0].callback_data, rows[-1][0].model_dump(exclude_none=True)["style"]), ("menu:products", "danger"))
        self.assertFalse(any(button.callback_data.startswith("add:") for row in rows for button in row))
        gemini = AsyncMock()
        gemini.photo = None
        callback = AsyncMock()
        callback.message, callback.from_user, callback.data = gemini, user, "category:gemini"
        await shop.category_callback(callback)
        rows = gemini.answer_photo.call_args.kwargs["reply_markup"].inline_keyboard
        product_button = next(button for row in rows for button in row if button.callback_data == "product:2")
        self.assertEqual(product_button.model_dump(exclude_none=True)["style"], "danger")  # нет в наличии — красная
        self.assertTrue(str(gemini.answer_photo.call_args.args[0].path).endswith("geminishop.jpg"))

    async def test_preorder_shelf_and_categories_button(self):
        plain_id = await self.product(stock=0)
        await self.product(stock=1)  # в наличии — на полку предзаказов не попадает
        keyboard = shop.categories_keyboard().inline_keyboard
        preorders_button = next(button for row in keyboard for button in row if button.callback_data == "preorders")
        self.assertEqual(preorders_button.text, "⏳ Предзаказы")
        self.assertNotIn("style", preorders_button.model_dump(exclude_none=True))  # предзаказ без цвета
        user = User(id=USER, is_bot=False, first_name="Tester")
        shelf = AsyncMock()
        shelf.photo = None
        callback = AsyncMock()
        callback.message, callback.from_user, callback.data = shelf, user, "preorders"
        await shop.preorders_callback(callback)
        caption = shelf.answer_photo.call_args.kwargs["caption"]
        self.assertIn("Полка предзаказов", caption)
        self.assertIn("предоплате 100%", caption)
        rows = shelf.answer_photo.call_args.kwargs["reply_markup"].inline_keyboard
        product_button = next(button for row in rows for button in row if button.callback_data == f"product:{plain_id}")
        self.assertEqual(product_button.model_dump(exclude_none=True)["style"], "danger")
        self.assertEqual(rows[-1][0].callback_data, "menu:products")

    async def test_admin_shelf_add_wizard_and_two_step_delete(self):
        product_id = await self.product(stock=1)
        user = User(id=ADMIN, is_bot=False, first_name="Owner")
        message = AsyncMock()
        message.photo = None
        callback = AsyncMock()
        callback.message, callback.from_user, callback.data = message, user, "category:chatgpt"
        await shop.category_callback(callback)
        rows = message.answer_photo.call_args.kwargs["reply_markup"].inline_keyboard
        codes = [button.callback_data for row in rows for button in row]
        self.assertIn(f"del:{product_id}", codes)
        self.assertIn("add:chatgpt", codes)
        shop.add_state.clear()
        callback = AsyncMock()
        callback.message, callback.from_user, callback.data = message, user, "add:chatgpt"
        await shop.add_callback(callback)
        self.assertEqual(shop.add_state[ADMIN]["step"], "type")
        type_callback = AsyncMock()
        type_callback.message, type_callback.from_user, type_callback.data = message, user, "addtype:regular"
        await shop.addtype_callback(type_callback)
        self.assertEqual(shop.add_state[ADMIN]["step"], "name")
        for text, step in (("ChatGPT Plus", "name"), ("790", "price"), ("3", "stock"), ("пропустить", "description")):
            self.assertEqual(shop.add_state[ADMIN]["step"], step)
            wizard_message = AsyncMock()
            wizard_message.from_user = user
            wizard_message.text = text
            await shop.add_wizard(wizard_message)
        self.assertNotIn(ADMIN, shop.add_state)
        added = [item for item in (await self.store.catalog())["products"] if item["name"] == "ChatGPT Plus"]
        self.assertEqual(len(added), 1)
        self.assertEqual((added[0]["price"], added[0]["stock"]), (790, 3))
        callback = AsyncMock()
        callback.message, callback.from_user, callback.data = message, user, f"del:{product_id}"
        await shop.delete_callback(callback)
        self.assertIn((ADMIN, product_id), shop.pending_delete)
        self.assertEqual(len((await self.store.catalog(admin=True))["products"]), 2)
        await shop.delete_callback(callback)
        self.assertNotIn((ADMIN, product_id), shop.pending_delete)
        self.assertEqual([item["id"] for item in (await self.store.catalog(admin=True))["products"]], [added[0]["id"]])
        shop.pending_delete.clear()
        shop.add_state.clear()

    async def test_wizard_rejects_bad_values_and_cancel(self):
        user = User(id=ADMIN, is_bot=False, first_name="Owner")
        message = AsyncMock()
        message.photo = None
        callback = AsyncMock()
        callback.message, callback.from_user, callback.data = message, user, "add:gemini"
        await shop.add_callback(callback)
        bad = AsyncMock()
        bad.from_user = user
        bad.text = ""
        await shop.add_wizard(bad)
        self.assertEqual(shop.add_state[ADMIN]["step"], "type")  # текст не двигает шаг выбора типа
        type_callback = AsyncMock()
        type_callback.message, type_callback.from_user, type_callback.data = message, user, "addtype:regular"
        await shop.addtype_callback(type_callback)
        bad = AsyncMock()
        bad.from_user = user
        bad.text = ""
        await shop.add_wizard(bad)
        self.assertEqual(shop.add_state[ADMIN]["step"], "name")
        bad.text = "12.5"
        shop.add_state[ADMIN]["step"] = "price"
        await shop.add_wizard(bad)
        self.assertEqual(shop.add_state[ADMIN]["step"], "price")
        cancel = AsyncMock()
        cancel.from_user = user
        await shop.cmd_cancel(cancel)
        self.assertNotIn(ADMIN, shop.add_state)
        shop.add_state.clear()

    async def test_add_command_creates_test_and_regular_products(self):
        admin = User(id=ADMIN, is_bot=False, first_name="Owner")
        message = AsyncMock()
        message.from_user = admin
        await shop.cmd_add(message)
        self.assertIn("категорию", message.answer.call_args.args[0])
        rows = message.answer.call_args.kwargs["reply_markup"].inline_keyboard
        codes = [button.callback_data for row in rows for button in row]
        self.assertIn("add:chatgpt", codes)
        self.assertIn("add:gemini", codes)
        cancel_button = rows[-1][0]
        self.assertEqual((cancel_button.callback_data, cancel_button.model_dump(exclude_none=True)["style"]), ("add:cancel", "danger"))
        stranger = AsyncMock()
        stranger.from_user = User(id=USER, is_bot=False, first_name="Stranger")
        await shop.cmd_add(stranger)
        self.assertIn("хранитель", stranger.answer.call_args.args[0])
        # Тестовый товар: создаётся визардом после выбора 🧪 Тестовый.
        board = AsyncMock()
        board.photo = None
        callback = AsyncMock()
        callback.message, callback.from_user, callback.data = board, admin, "add:gemini"
        await shop.add_callback(callback)
        self.assertEqual(shop.add_state[ADMIN]["step"], "type")
        self.assertEqual(shop.add_state[ADMIN]["data"], {})
        type_callback = AsyncMock()
        type_callback.message, type_callback.from_user, type_callback.data = board, admin, "addtype:test"
        await shop.addtype_callback(type_callback)
        self.assertEqual(shop.add_state[ADMIN]["step"], "name")
        for text in ("Test Widget", "100", "4", "тест"):
            wizard_message = AsyncMock()
            wizard_message.from_user = admin
            wizard_message.text = text
            await shop.add_wizard(wizard_message)
        self.assertNotIn(ADMIN, shop.add_state)
        added = [item for item in (await self.store.catalog(include_test=True))["products"] if item["name"] == "Test Widget"]
        self.assertEqual((added[0]["is_test"], added[0]["allow_preorder"]), (1, 0))
        self.assertEqual((await self.store.catalog())["products"], [])  # обычным покупателям его не видно
        shop.add_state.clear()

    async def test_delete_product_api(self):
        product_id = await self.product()
        self.assertEqual((await self.client.delete(f"/api/admin/products/{product_id}")).status, 401)
        self.assertEqual((await self.client.delete(f"/api/admin/products/{product_id}", headers=headers())).status, 403)
        self.assertEqual((await self.client.delete(f"/api/admin/products/{product_id}", headers=headers(ADMIN))).status, 200)
        self.assertEqual((await self.client.delete(f"/api/admin/products/{product_id}", headers=headers(ADMIN))).status, 404)
        self.assertEqual((await self.store.catalog(admin=True))["products"], [])



if __name__ == "__main__":
    unittest.main()
