"""SQLite storage and authenticated API shared by the bot and mini app."""
import hashlib
import hmac
import json
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import aiosqlite
from aiohttp import web


class ApiError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def verify_init_data(raw, token, max_age=86400):
    """Validate Telegram's signature, freshness and user identity; fail closed."""
    try:
        if not isinstance(raw, str) or not raw or len(raw) > 16384 or not token:
            return None
        parts = parse_qsl(raw, strict_parsing=True)
        data = dict(parts)
        if len(data) != len(parts):
            return None
        signature = data.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        age = time.time() - int(data["auth_date"])
        if age < -60 or age > max_age:
            return None
        user = json.loads(data["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**63:
            return None
        return user
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def text(value, name, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ApiError(f"Проверь поле «{name}».")
    return value.strip()


def integer(value, name, maximum=10**9, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError(f"Проверь поле «{name}»: нужно целое число от {minimum} до {maximum}.")
    return value


def flag(value, name):
    if type(value) is not bool:
        raise ApiError(f"Проверь поле «{name}».")
    return int(value)


class Store:
    def __init__(self, db_path):
        self.db_path = str(db_path)

    @asynccontextmanager
    async def connection(self):
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    async def init_db(self):
        async with self.connection() as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    description TEXT DEFAULT '', price INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL, emoji TEXT DEFAULT '', stock INTEGER DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1, allow_preorder INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    items TEXT NOT NULL, total INTEGER NOT NULL, comment TEXT DEFAULT '',
                    status TEXT DEFAULT 'new', created_at INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'order', idempotency_key TEXT, request_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    traveler_no INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE,
                    first_name TEXT DEFAULT '', username TEXT DEFAULT '', created_at INTEGER NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0
                );
            """)
            migrations = {
                "products": {"active": "INTEGER NOT NULL DEFAULT 1", "allow_preorder": "INTEGER NOT NULL DEFAULT 0"},
                "orders": {"kind": "TEXT NOT NULL DEFAULT 'order'", "idempotency_key": "TEXT", "request_hash": "TEXT"},
            }
            for table, fields in migrations.items():
                columns = {row["name"] for row in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()}
                for name, definition in fields.items():
                    if name not in columns:
                        await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS order_retry ON orders(user_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
            for slug, name in (("chatgpt", "ChatGPT"), ("gemini", "Gemini")):
                await db.execute("INSERT INTO categories(slug,name) SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM categories WHERE slug=?)", (slug, name, slug))
            # Preserve old demo records, but do not sell candles/relics in the AI shop.
            await db.execute("INSERT OR IGNORE INTO categories(slug,name,active) SELECT DISTINCT category,category,0 FROM products WHERE category IS NOT NULL")
            await db.execute("CREATE INDEX IF NOT EXISTS orders_by_user ON orders(user_id,status)")
            await db.commit()

    async def catalog(self, admin=False):
        async with self.connection() as db:
            categories = [dict(row) for row in await (await db.execute("SELECT * FROM categories" + ("" if admin else " WHERE active=1") + " ORDER BY id")).fetchall()]
            products = [dict(row) for row in await (await db.execute("SELECT p.* FROM products p JOIN categories c ON c.slug=p.category" + ("" if admin else " WHERE p.active=1 AND c.active=1") + " ORDER BY p.id")).fetchall()]
        for category in categories:
            category["stock"] = sum(p["stock"] for p in products if p["category"] == category["slug"] and p["active"])
        return {"categories": categories, "products": products}

    async def profile(self, user, is_admin=False):
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("INSERT INTO users(user_id,first_name,username,created_at) SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM users WHERE user_id=?)", (user["id"], str(user.get("first_name", ""))[:256], str(user.get("username", ""))[:64], int(time.time()), user["id"]))
            await db.execute("UPDATE users SET first_name=?,username=? WHERE user_id=?", (str(user.get("first_name", ""))[:256], str(user.get("username", ""))[:64], user["id"]))
            row = dict(await (await db.execute("SELECT * FROM users WHERE user_id=?", (user["id"],))).fetchone())
            orders = await (await db.execute("SELECT items,total,status,kind FROM orders WHERE user_id=? ORDER BY id", (user["id"],))).fetchall()
            await db.commit()
        favorites, purchases, spent, preorders = Counter(), 0, 0, 0
        for order in orders:
            if order["status"] == "preorder":
                preorders += 1
            if order["status"] not in ("paid", "done"):
                continue
            spent += order["total"]
            try:
                items = json.loads(order["items"])
                for item in items:
                    qty = item.get("qty", 0)
                    if type(qty) is int and qty > 0:
                        purchases += qty
                        favorites[str(item.get("name", "Товар"))] += qty
            except (ValueError, TypeError, AttributeError):
                continue
        return {"traveler_no": row["traveler_no"], "purchases": purchases, "spent": spent,
                "favorite_product": favorites.most_common(1)[0][0] if favorites else None,
                "balance": row["balance"], "preorders": preorders, "is_admin": bool(is_admin)}

    async def save_category(self, body, item_id=None):
        name = text(body.get("name"), "Название", 120, True)
        active = flag(body.get("active", True), "Показывать")
        async with self.connection() as db:
            if item_id is None:
                slug = text(body.get("slug"), "Код категории", 80, True)
                if not re.fullmatch(r"[a-z0-9]+(?:(?:-|_)[a-z0-9]+)*", slug):
                    raise ApiError("Код категории: латинские буквы, цифры, дефис или подчёркивание.")
                try:
                    cursor = await db.execute("INSERT INTO categories(slug,name,active) VALUES(?,?,?)", (slug, name, active))
                except aiosqlite.IntegrityError:
                    raise ApiError("Категория с таким кодом уже существует.", 409)
                item_id = cursor.lastrowid
            else:
                if "slug" in body:
                    raise ApiError("Код существующей категории менять нельзя.")
                cursor = await db.execute("UPDATE categories SET name=?,active=? WHERE id=?", (name, active, item_id))
                if not cursor.rowcount:
                    raise ApiError("Категория не найдена.", 404)
            await db.commit()
        return {"ok": True, "id": item_id}

    async def save_product(self, body, item_id=None):
        values = (text(body.get("name"), "Название", 200, True), text(body.get("description", ""), "Описание", 4000),
                  integer(body.get("price", 0), "Цена"), text(body.get("category"), "Категория", 80, True),
                  integer(body.get("stock", 0), "Остаток", 10**6), flag(body.get("active", True), "Показывать"),
                  flag(body.get("allow_preorder", False), "Предзаказ"))
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            if not await (await db.execute("SELECT id FROM categories WHERE slug=?", (values[3],))).fetchone():
                raise ApiError("Сначала создай категорию.")
            if item_id is None:
                cursor = await db.execute("INSERT INTO products(name,description,price,category,stock,active,allow_preorder) VALUES(?,?,?,?,?,?,?)", values)
                item_id = cursor.lastrowid
            else:
                cursor = await db.execute("UPDATE products SET name=?,description=?,price=?,category=?,stock=?,active=?,allow_preorder=? WHERE id=?", (*values, item_id))
                if not cursor.rowcount:
                    raise ApiError("Товар не найден.", 404)
            await db.commit()
        return {"ok": True, "id": item_id}

    async def delete_product(self, item_id):
        async with self.connection() as db:
            cursor = await db.execute("DELETE FROM products WHERE id=?", (item_id,))
            if not cursor.rowcount:
                raise ApiError("Товар не найден.", 404)
            await db.commit()
        return {"ok": True, "id": item_id}

    async def create_order(self, user_id, body):
        cart = body.get("cart")
        if not isinstance(cart, list) or not 1 <= len(cart) <= 30:
            raise ApiError("Корзина должна содержать от 1 до 30 товаров.")
        kind = body.get("kind", "order")
        if kind not in ("order", "preorder"):
            raise ApiError("Неизвестный тип заявки.")
        key = text(body.get("idempotency_key"), "Ключ заявки", 128, True)
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
            raise ApiError("Неверный ключ заявки.")
        comment = text(body.get("comment", ""), "Комментарий", 300)
        requested = {}
        for item in cart:
            if not isinstance(item, dict):
                raise ApiError("Неверный формат корзины.")
            product_id = integer(item.get("id"), "Товар", 2**63 - 1, 1)
            qty = integer(item.get("qty", 1), "Количество", 100, 1)
            requested[product_id] = requested.get(product_id, 0) + qty
            if requested[product_id] > 100:
                raise ApiError("Не более 100 единиц одного товара.")
        digest = hashlib.sha256(json.dumps([sorted(requested.items()), kind, comment], ensure_ascii=False).encode()).hexdigest()
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            previous = await (await db.execute("SELECT * FROM orders WHERE user_id=? AND idempotency_key=?", (user_id, key))).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise ApiError("Этот ключ уже использован для другой заявки.", 409)
                return {"ok": True, "order_id": previous["id"], "total": previous["total"], "replayed": True}
            items, total = [], 0
            for product_id, qty in sorted(requested.items()):
                product = await (await db.execute("SELECT p.* FROM products p JOIN categories c ON c.slug=p.category WHERE p.id=? AND p.active=1 AND c.active=1", (product_id,))).fetchone()
                if not product or product["price"] <= 0:
                    raise ApiError("Товар недоступен или цена ещё не задана.", 409)
                if kind == "preorder":
                    if product["stock"] != 0 or not product["allow_preorder"]:
                        raise ApiError("Предзаказ этого товара сейчас недоступен.", 409)
                elif product["stock"] < qty:
                    raise ApiError("Недостаточно товара. Обнови каталог.", 409)
                else:
                    await db.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, product_id))
                total += product["price"] * qty
                items.append({"id": product_id, "name": product["name"], "price": product["price"], "qty": qty})
            cursor = await db.execute("INSERT INTO orders(user_id,items,total,comment,status,created_at,kind,idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?,?,?)", (user_id, json.dumps(items, ensure_ascii=False), total, comment, "preorder" if kind == "preorder" else "new", int(time.time()), kind, key, digest))
            await db.commit()
            return {"ok": True, "order_id": cursor.lastrowid, "total": total, "replayed": False}

    async def orders(self):
        async with self.connection() as db:
            rows = [dict(row) for row in await (await db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200")).fetchall()]
        for row in rows:
            row["created_at"] = datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat()
            row["items"] = json.loads(row["items"])
            row.pop("request_hash", None)
            row.pop("idempotency_key", None)
        return {"orders": rows}

    async def change_order(self, order_id, status):
        transitions = {"new": ("paid", "cancelled"), "preorder": ("paid", "cancelled"), "paid": ("done",)}
        if not isinstance(status, str):
            raise ApiError("Укажи статус заказа.")
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            order = await (await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))).fetchone()
            if not order:
                raise ApiError("Заказ не найден.", 404)
            if status == order["status"]:
                return {"ok": True, "id": order_id}
            if status not in transitions.get(order["status"], ()):
                raise ApiError("Этот переход статуса запрещён.", 409)
            if status == "cancelled" and order["kind"] == "order":
                for item in json.loads(order["items"]):
                    await db.execute("UPDATE products SET stock=stock+? WHERE id=?", (item["qty"], item["id"]))
            await db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
            await db.commit()
        return {"ok": True, "id": order_id}


@web.middleware
async def api_errors(request, handler):
    try:
        return await handler(request)
    except ApiError as error:
        return web.json_response({"error": error.message}, status=error.status)


def register_api(app, store, bot_token, admin_ids, support_username):
    app.middlewares.append(api_errors)

    def authenticate(request, admin=False):
        header = request.headers.get("Authorization", "")
        user = verify_init_data(header[4:], bot_token) if header.startswith("tma ") else None
        if not user:
            raise ApiError("Открой лавку заново через Telegram, чтобы подтвердить вход.", 401)
        if admin and user["id"] not in admin_ids:
            raise ApiError("Эта часть лавки доступна только администратору.", 403)
        return user

    async def body(request):
        try:
            value = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise ApiError("Неверный JSON.")
        if not isinstance(value, dict):
            raise ApiError("Ожидается объект JSON.")
        return value

    def item_id(request):
        value = request.match_info.get("id")
        if value is None:
            return None
        if not value.isdecimal() or len(value) > 19:
            raise ApiError("Неверный номер записи.")
        return integer(int(value), "Номер записи", 2**63 - 1, 1)

    async def catalog(request):
        return web.json_response(await store.catalog())

    async def legacy_products(request):
        return web.json_response((await store.catalog())["products"])

    async def config(request):
        return web.json_response({"currency": "RUB", "payment_enabled": False, "support_username": support_username,
                                  "images": {"menu": "/static/bg.jpg", "profile": "/static/2.jpg", "support": "/static/shop.jpg"}})

    async def me(request):
        user = authenticate(request)
        return web.json_response(await store.profile(user, user["id"] in admin_ids))

    async def admin_catalog(request):
        authenticate(request, True)
        return web.json_response(await store.catalog(admin=True))

    async def categories(request):
        authenticate(request, True)
        return web.json_response(await store.save_category(await body(request), item_id(request)))

    async def products(request):
        authenticate(request, True)
        return web.json_response(await store.save_product(await body(request), item_id(request)))

    async def orders(request):
        authenticate(request, True)
        return web.json_response(await store.orders())

    async def change_order(request):
        authenticate(request, True)
        return web.json_response(await store.change_order(item_id(request), (await body(request)).get("status")))

    async def delete_product(request):
        authenticate(request, True)
        return web.json_response(await store.delete_product(item_id(request)))

    async def order(request):
        user = authenticate(request)
        payload = await body(request)
        await store.profile(user, user["id"] in admin_ids)
        return web.json_response(await store.create_order(user["id"], payload))

    app.add_routes([
        web.get("/api/catalog", catalog), web.get("/api/products", legacy_products),
        web.get("/api/config", config), web.get("/api/me", me), web.post("/api/order", order),
        web.get("/api/admin/catalog", admin_catalog), web.get("/api/admin/orders", orders),
        web.post("/api/admin/categories", categories), web.patch("/api/admin/categories/{id}", categories),
        web.post("/api/admin/products", products), web.patch("/api/admin/products/{id}", products),
        web.delete("/api/admin/products/{id}", delete_product),
        web.patch("/api/admin/orders/{id}", change_order),
    ])
