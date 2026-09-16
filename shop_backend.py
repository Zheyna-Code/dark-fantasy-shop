"""SQLite storage and authenticated API shared by the bot and mini app."""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import aiosqlite
try:
    import asyncpg
except ModuleNotFoundError:  # Optional for local SQLite-only development.
    asyncpg = None
from aiohttp import web

log = logging.getLogger("lavka.api")


class _PgResult:
    def __init__(self, rows=None, lastrowid=None):
        self._rows = rows or []
        self.lastrowid = lastrowid

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class _PgConnection:
    """Small asyncpg adapter matching the aiosqlite calls used by Store."""
    def __init__(self, conn):
        self.conn = conn

    @staticmethod
    def _sql(sql, params):
        sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.I)
        index = 0
        def replace(_match):
            nonlocal index
            index += 1
            return f"${index}"
        return re.sub(r"\?", replace, sql)

    async def execute(self, sql, params=()):
        params = tuple(params or ())
        ignore_conflicts = bool(re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO", sql, re.I))
        query = self._sql(sql, params)
        if ignore_conflicts:
            query += " ON CONFLICT DO NOTHING"
        is_insert = query.lstrip().upper().startswith("INSERT")
        # Store only reads generated ids from these tables. Users use
        # traveler_no as their primary key and do not need a RETURNING clause.
        needs_id = bool(re.search(r"\bINTO\s+(products|categories|orders)\b", query, re.I))
        if is_insert and needs_id and "RETURNING" not in query.upper():
            query += " RETURNING id"
        if query.lstrip().upper().startswith(("SELECT", "WITH")) or " RETURNING " in query.upper():
            rows = await self.conn.fetch(query, *params)
            return _PgResult(rows, rows[0]["id"] if rows and "id" in rows[0] else None)
        await self.conn.execute(query, *params)
        return _PgResult()

    async def executemany(self, sql, seq):
        values = [tuple(row) for row in seq]
        if values:
            await self.conn.executemany(self._sql(sql, values[0]), values)
        return _PgResult()

    async def executescript(self, script):
        script = script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        for statement in (part.strip() for part in script.split(";")):
            if statement:
                await self.conn.execute(statement)

    async def commit(self):
        await self.conn.execute("COMMIT")

    async def rollback(self):
        await self.conn.execute("ROLLBACK")

    async def close(self):
        await self.conn.close()


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
        self.database_url = os.environ.get("DATABASE_URL", "").strip()
        # Hosting providers commonly configure DB_PATH under a mounted
        # directory that is not present in a fresh container.
        Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def connection(self):
        if self.database_url:
            if asyncpg is None:
                raise RuntimeError("DATABASE_URL is set, but asyncpg is not installed")
            db = _PgConnection(await asyncpg.connect(self.database_url, ssl="require", statement_cache_size=0))
            try:
                yield db
            finally:
                await db.close()
            return
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    async def init_db(self):
        async with self.connection() as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    description TEXT DEFAULT '', warranty TEXT DEFAULT '', price INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL, emoji TEXT DEFAULT '', stock INTEGER DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1, allow_preorder INTEGER NOT NULL DEFAULT 0,
                    is_test INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready',
                    order_id INTEGER, created_at INTEGER NOT NULL,
                    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT NOT NULL,
                    items TEXT NOT NULL, total INTEGER NOT NULL, comment TEXT DEFAULT '',
                    status TEXT DEFAULT 'new', created_at INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'order', idempotency_key TEXT, request_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    traveler_no INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT NOT NULL UNIQUE,
                    first_name TEXT DEFAULT '', username TEXT DEFAULT '', created_at INTEGER NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0
                    ,referred_by BIGINT, referral_rewarded INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id BIGINT NOT NULL, product_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, PRIMARY KEY(user_id, product_id)
                );
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT NOT NULL,
                    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    created_at INTEGER NOT NULL
                );
            """)
            if self.database_url:
                await db.execute("ALTER TABLE users ALTER COLUMN user_id TYPE BIGINT USING user_id::bigint")
                await db.execute("ALTER TABLE orders ALTER COLUMN user_id TYPE BIGINT USING user_id::bigint")
            migrations = {
                "products": {
                    "warranty": "TEXT DEFAULT ''",
                    "active": "INTEGER NOT NULL DEFAULT 1",
                    "allow_preorder": "INTEGER NOT NULL DEFAULT 0",
                    "is_test": "INTEGER NOT NULL DEFAULT 0",
                },
                "orders": {"kind": "TEXT NOT NULL DEFAULT 'order'", "idempotency_key": "TEXT", "request_hash": "TEXT"},
                "users": {"referred_by": "BIGINT", "referral_rewarded": "INTEGER NOT NULL DEFAULT 0"},
            }
            for table, fields in migrations.items():
                if self.database_url:
                    continue
                columns = {row["name"] for row in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()}
                for name, definition in fields.items():
                    if name not in columns:
                        await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS order_retry ON orders(user_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
            for slug, name in (("chatgpt", "ChatGPT"), ("gemini", "Gemini"), ("capcut", "CapCut")):
                await db.execute("INSERT INTO categories(slug,name) SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM categories WHERE slug=?)", (slug, name, slug))
            # Preserve old demo records, but do not sell candles/relics in the AI shop.
            await db.execute("INSERT OR IGNORE INTO categories(slug,name,active) SELECT DISTINCT category,category,0 FROM products WHERE category IS NOT NULL")
            await db.execute("CREATE INDEX IF NOT EXISTS orders_by_user ON orders(user_id,status)")
            await db.execute("CREATE INDEX IF NOT EXISTS deliveries_queue ON deliveries(product_id,status,id)")
            await db.execute("CREATE INDEX IF NOT EXISTS deliveries_by_order ON deliveries(order_id)")
            await db.commit()

    async def catalog(self, admin=False, include_test=False):
        async with self.connection() as db:
            categories = [dict(row) for row in await (await db.execute("SELECT * FROM categories" + ("" if admin else " WHERE active=1") + " ORDER BY id")).fetchall()]
            counts = (
                "(SELECT COUNT(*) FROM deliveries d WHERE d.product_id=p.id AND d.status='ready') AS ready_items,"
                "(SELECT COUNT(*) FROM deliveries d WHERE d.product_id=p.id AND d.status='issued') AS issued_items"
            )
            where = "" if admin else " WHERE p.active=1 AND c.active=1" + ("" if include_test else " AND p.is_test=0")
            products = [dict(row) for row in await (await db.execute(f"SELECT p.*,{counts} FROM products p JOIN categories c ON c.slug=p.category{where} ORDER BY p.id")).fetchall()]
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
            if order["kind"] == "test":
                continue
            if order["status"] in ("preorder", "paid") and order["kind"] == "preorder":
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
        return {"traveler_no": row["traveler_no"], "user_id": row["user_id"], "referral_link": f"https://t.me/{os.environ.get('BOT_USERNAME', 'lavka_bot')}?start=ref_{row['user_id']}", "purchases": purchases, "spent": spent,
                "favorite_product": favorites.most_common(1)[0][0] if favorites else None,
                "balance": row["balance"], "preorders": preorders, "is_admin": bool(is_admin)}

    async def history(self, user_id):
        async with self.connection() as db:
            rows = await (await db.execute("SELECT id,items,total,status,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,))).fetchall()
        result = []
        for row in rows:
            result.append({"id": row["id"], "items": json.loads(row["items"]), "total": row["total"],
                           "status": row["status"], "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat()})
        return {"orders": result}

    async def toggle_favorite(self, user_id, product_id):
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT 1 FROM favorites WHERE user_id=? AND product_id=?", (user_id, product_id))).fetchone()
            if row:
                await db.execute("DELETE FROM favorites WHERE user_id=? AND product_id=?", (user_id, product_id)); active = False
            else:
                await db.execute("INSERT INTO favorites(user_id,product_id,created_at) VALUES(?,?,?)", (user_id, product_id, int(time.time()))); active = True
            await db.commit()
        return {"ok": True, "favorite": active}

    async def stats(self):
        async with self.connection() as db:
            row = await (await db.execute("SELECT COUNT(*) AS orders, COALESCE(SUM(total),0) AS revenue FROM orders WHERE status IN ('paid','done')")).fetchone()
            users = await (await db.execute("SELECT COUNT(*) AS n FROM users")).fetchone()
            products = await (await db.execute("SELECT COUNT(*) AS n FROM products WHERE active=1")).fetchone()
        return {"orders": row["orders"], "revenue": row["revenue"], "users": users["n"], "products": products["n"]}

    async def create_ticket(self, user_id, message):
        message = text(message, "Сообщение", 4000, True)
        async with self.connection() as db:
            cursor = await db.execute("INSERT INTO support_tickets(user_id,text,created_at) VALUES(?,?,?)", (user_id, message, int(time.time())))
            await db.commit()
            if kind == "order" and payment == "balance" and total > 200:
                async with self.connection() as reward_db:
                    await reward_db.execute("BEGIN IMMEDIATE")
                    await reward_db.execute("UPDATE users SET balance=balance+CAST(? * 0.03 AS INTEGER) WHERE user_id=(SELECT referred_by FROM users WHERE user_id=?) AND referral_rewarded=0", (total, user_id))
                    await reward_db.execute("UPDATE users SET referral_rewarded=1 WHERE user_id=? AND referral_rewarded=0", (user_id,))
                    await reward_db.commit()
        return {"ok": True, "id": cursor.lastrowid}

    async def add_balance(self, user_id, amount):
        amount = integer(amount, "Сумма пополнения", 10**6, 1)
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not row:
                await db.execute("INSERT INTO users(user_id,created_at,balance) VALUES(?,?,?)", (user_id, int(time.time()), amount))
            else:
                await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
            row = await (await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))).fetchone()
            await db.commit()
        return int(row["balance"])

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
                  text(body.get("warranty", ""), "Гарантия", 500),
                  integer(body.get("price", 0), "Цена"), text(body.get("category"), "Категория", 80, True),
                  integer(body.get("stock", 0), "Остаток", 10**6), flag(body.get("active", True), "Показывать"),
                  flag(body.get("allow_preorder", False), "Предзаказ"), flag(body.get("is_test", False), "Тестовый товар"))
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            if not await (await db.execute("SELECT id FROM categories WHERE slug=?", (values[4],))).fetchone():
                raise ApiError("Сначала создай категорию.")
            if item_id is None:
                cursor = await db.execute("INSERT INTO products(name,description,warranty,price,category,stock,active,allow_preorder,is_test) VALUES(?,?,?,?,?,?,?,?,?)", values)
                item_id = cursor.lastrowid
            else:
                cursor = await db.execute("UPDATE products SET name=?,description=?,warranty=?,price=?,category=?,stock=?,active=?,allow_preorder=?,is_test=? WHERE id=?", (*values, item_id))
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
        payment = body.get("payment", "manual")
        if payment not in ("manual", "balance", "crypto", "sbp"):
            raise ApiError("Неизвестный способ оплаты.")
        if kind not in ("order", "test"):
            raise ApiError("Неизвестный тип заявки.")
        if kind == "order" and payment != "balance":
            raise ApiError("Этот способ оплаты скоро будет доступен.", 409)
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
        digest = hashlib.sha256(json.dumps([sorted(requested.items()), kind, payment, comment], ensure_ascii=False).encode()).hexdigest()
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            previous = await (await db.execute("SELECT * FROM orders WHERE user_id=? AND idempotency_key=?", (user_id, key))).fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise ApiError("Этот ключ уже использован для другой заявки.", 409)
                result = {"ok": True, "order_id": previous["id"], "total": previous["total"], "replayed": True}
                if previous["kind"] == "test":
                    result["test"] = True
                    result["issued"] = await self._issued_event(db, previous)
                return result
            items, total, allocation = [], 0, []
            for product_id, qty in sorted(requested.items()):
                product = await (await db.execute("SELECT p.* FROM products p JOIN categories c ON c.slug=p.category WHERE p.id=? AND p.active=1 AND c.active=1", (product_id,))).fetchone()
                if not product or product["price"] <= 0:
                    raise ApiError("Товар недоступен или цена ещё не задана.", 409)
                if kind == "test":
                    if not product["is_test"]:
                        raise ApiError("Тестовая покупка доступна только для тестовых товаров.", 409)
                    if product["stock"] < qty:
                        raise ApiError("Недостаточно единиц тестового товара.", 409)
                    rows = await (await db.execute("SELECT id,payload FROM deliveries WHERE product_id=? AND status='ready' ORDER BY id LIMIT ?", (product_id, qty))).fetchall()
                    if len(rows) < qty:
                        raise ApiError("Сначала загрузи автовыдачу для тестового товара.", 409)
                    allocation.append((product_id, qty, rows))
                elif product["stock"] < qty:
                    raise ApiError("Недостаточно товара. Обнови каталог.", 409)
                else:
                    await db.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, product_id))
                total += product["price"] * qty
                items.append({"id": product_id, "name": product["name"], "price": product["price"], "qty": qty})
            if kind == "test":
                total = 0
            if kind == "order" and payment == "balance":
                user_row = await (await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))).fetchone()
                if not user_row or user_row["balance"] < total:
                    raise ApiError("Недостаточно средств на балансе.", 409)
                await db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (total, user_id))
            status = "done" if kind == "test" else ("preorder" if kind == "preorder" else ("paid" if payment == "balance" else "new"))
            cursor = await db.execute("INSERT INTO orders(user_id,items,total,comment,status,created_at,kind,idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?,?,?)", (user_id, json.dumps(items, ensure_ascii=False), total, comment, status, int(time.time()), kind, key, digest))
            order_id = cursor.lastrowid
            if kind == "test":
                for product_id, qty, rows in allocation:
                    await db.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, product_id))
                    await db.execute("UPDATE deliveries SET status='issued', order_id=? WHERE id IN (" + ",".join("?" * len(rows)) + ")", (order_id, *[row["id"] for row in rows]))
            issued = []
            if kind == "order" and payment == "balance":
                issued = await self._try_fulfill(db, await (await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))).fetchone())
                if issued:
                    await db.execute("UPDATE orders SET status='done' WHERE id=?", (order_id,))
            await db.commit()
            result = {"ok": True, "order_id": order_id, "total": total, "replayed": False}
            if issued:
                result["issued"] = issued
            if kind == "test":
                names = {item["id"]: item["name"] for item in items}
                result["test"] = True
                result["issued"] = [{"user_id": user_id, "order_id": order_id, "kind": "test",
                                     "items": [{"name": names[product_id], "payloads": [row["payload"] for row in rows]} for product_id, qty, rows in allocation]}]
            return result

    async def set_referrer(self, user_id, referrer_id):
        if user_id == referrer_id: return
        async with self.connection() as db:
            await db.execute("UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL", (referrer_id, user_id))
            await db.commit()

    async def product(self, product_id):
        async with self.connection() as db:
            row = await (await db.execute(
                "SELECT p.*,(SELECT COUNT(*) FROM deliveries d WHERE d.product_id=p.id AND d.status='ready') AS ready_items,"
                "(SELECT COUNT(*) FROM deliveries d WHERE d.product_id=p.id AND d.status='issued') AS issued_items "
                "FROM products p WHERE p.id=?", (product_id,))).fetchone()
        return dict(row) if row else None

    async def active_preorder(self, user_id, product_id):
        """Незакрытый предзаказ пользователя на товар, чтобы кнопка не плодила дубли."""
        async with self.connection() as db:
            rows = await (await db.execute("SELECT id,items,status FROM orders WHERE user_id=? AND kind='preorder' AND status IN ('preorder','paid') ORDER BY id DESC LIMIT 50", (user_id,))).fetchall()
        for row in rows:
            try:
                items = json.loads(row["items"])
            except (ValueError, TypeError):
                continue
            if any(isinstance(item, dict) and item.get("id") == product_id for item in items):
                return {"id": row["id"], "status": row["status"]}
        return None

    async def user_preorders(self, user_id):
        """Return this user's still-open preorders for the bot shelf."""
        async with self.connection() as db:
            rows = await (await db.execute(
                "SELECT id,items,total,status,created_at FROM orders "
                "WHERE user_id=? AND kind='preorder' AND status IN ('preorder','paid') ORDER BY id DESC",
                (user_id,),
            )).fetchall()
        result = []
        for row in rows:
            try:
                items = json.loads(row["items"])
            except (ValueError, TypeError):
                items = []
            result.append({"id": row["id"], "items": items, "total": row["total"], "status": row["status"],
                           "created_at": row["created_at"]})
        return result

    async def create_test_order(self, user_id, product_id, qty=1):
        """Покупка тестового товара без оплаты: заказ сразу выполнен, строка автовыдачи выдана."""
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            product = await (await db.execute("SELECT * FROM products WHERE id=? AND is_test=1", (product_id,))).fetchone()
            if not product or not product["active"]:
                raise ApiError("Тестовый товар не найден.", 404)
            if product["price"] <= 0:
                raise ApiError("Сначала задай цену тестового товара.", 409)
            rows = await (await db.execute("SELECT id,payload FROM deliveries WHERE product_id=? AND status='ready' ORDER BY id LIMIT ?", (product_id, qty))).fetchall()
            if product["stock"] < qty or len(rows) < qty:
                raise ApiError("Нет свободных единиц тестового товара: загрузи автовыдачу в админке.", 409)
            items = [{"id": product_id, "name": product["name"], "price": product["price"], "qty": qty}]
            cursor = await db.execute("INSERT INTO orders(user_id,items,total,comment,status,created_at,kind,idempotency_key,request_hash) VALUES(?,?,?,'','done',?,'test',NULL,NULL)",
                                      (user_id, json.dumps(items, ensure_ascii=False), 0, int(time.time())))
            order_id = cursor.lastrowid
            await db.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, product_id))
            for row in rows:
                await db.execute("UPDATE deliveries SET status='issued', order_id=? WHERE id=?", (order_id, row["id"]))
            await db.commit()
            return {"ok": True, "order_id": order_id, "total": 0, "issued": [{
                "user_id": user_id, "order_id": order_id, "kind": "test",
                "items": [{"name": product["name"], "payloads": [row["payload"] for row in rows]}]}]}

    async def add_deliveries(self, product_id, body):
        """Загрузка строк автовыдачи: сначала гасим оплаченные предзаказы, остаток идёт на полку."""
        raw = body.get("items")
        if isinstance(raw, str):
            lines = raw.splitlines()
        elif isinstance(raw, list) and all(isinstance(line, str) for line in raw):
            lines = raw
        else:
            raise ApiError("Отправь строки товара полем «items» (текст или список строк).")
        lines = [line.strip() for line in lines]
        lines = [line for line in lines if line]
        if not 1 <= len(lines) <= 200:
            raise ApiError("Нужно от 1 до 200 строк: одна строка — одна единица товара.")
        if any(len(line) > 2000 for line in lines):
            raise ApiError("Каждая строка товара — до 2000 символов.")
        now = int(time.time())
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            if not await (await db.execute("SELECT id FROM products WHERE id=?", (product_id,))).fetchone():
                raise ApiError("Товар не найден.", 404)
            await db.executemany("INSERT INTO deliveries(product_id,payload,status,created_at) VALUES(?,?,'ready',?)", [(product_id, line, now) for line in lines])
            await db.execute("UPDATE products SET stock=stock+? WHERE id=?", (len(lines), product_id))
            events, consumed = await self._fulfill_preorders(db, product_id)
            stock_added = len(lines) - consumed
            await db.commit()
        return {"ok": True, "id": product_id, "added": len(lines), "delivered_to_preorders": len(events),
                "stock_added": stock_added, "issued": events}

    async def product_deliveries(self, product_id):
        async with self.connection() as db:
            if not await (await db.execute("SELECT id FROM products WHERE id=?", (product_id,))).fetchone():
                raise ApiError("Товар не найден.", 404)
            counts = {row["status"]: row["n"] for row in await (await db.execute("SELECT status,COUNT(*) AS n FROM deliveries WHERE product_id=? GROUP BY status", (product_id,))).fetchall()}
            ready = await (await db.execute("SELECT payload FROM deliveries WHERE product_id=? AND status='ready' ORDER BY id LIMIT 5", (product_id,))).fetchall()
            issued = await (await db.execute("SELECT payload,order_id FROM deliveries WHERE product_id=? AND status='issued' ORDER BY id DESC LIMIT 10", (product_id,))).fetchall()
        return {"ok": True, "id": product_id, "ready": counts.get("ready", 0), "issued": counts.get("issued", 0),
                "ready_samples": [row["payload"] for row in ready],
                "recent_issued": [{"payload": row["payload"], "order_id": row["order_id"]} for row in issued]}

    async def _in_stock(self, db, items):
        """Автовыдача возможна, если в очереди есть готовые строки на каждую позицию."""
        for item in items:
            row = await (await db.execute("SELECT COUNT(*) AS n FROM deliveries WHERE product_id=? AND status='ready'", (item["id"],))).fetchone()
            if not row or row["n"] < item.get("qty", 0):
                return False
        return True

    async def _try_fulfill(self, db, order):
        """Выдаёт строки автовыдачи по оплаченному заказу: целиком или ничего."""
        try:
            items = json.loads(order["items"])
        except (ValueError, TypeError):
            return []
        plan = []
        for item in items:
            qty = item.get("qty", 0)
            if type(qty) is not int or qty < 1:
                return []
            rows = await (await db.execute("SELECT id,payload FROM deliveries WHERE product_id=? AND status='ready' ORDER BY id LIMIT ?", (item["id"], qty))).fetchall()
            if len(rows) < qty:
                return []
            plan.append((item, rows))
        delivered = []
        for item, rows in plan:
            for row in rows:
                await db.execute("UPDATE deliveries SET status='issued', order_id=? WHERE id=?", (order["id"], row["id"]))
            delivered.append({"name": item.get("name", "Товар"), "payloads": [row["payload"] for row in rows]})
        return [{"user_id": order["user_id"], "order_id": order["id"], "kind": order["kind"], "items": delivered}]

    async def _fulfill_preorders(self, db, product_id):
        """Оплаченные предзаказы (предоплата 100%) получают товар по очереди оформления — до пополнения полки."""
        events, consumed = [], 0
        rows = await (await db.execute("SELECT * FROM orders WHERE kind='preorder' AND status='paid' ORDER BY id")).fetchall()
        for order in rows:
            try:
                items = json.loads(order["items"])
            except (ValueError, TypeError):
                continue
            if not any(item.get("id") == product_id for item in items):
                continue
            if not await self._in_stock(db, items):
                # Do not let a later preorder jump ahead of an earlier one.
                break
            event = await self._try_fulfill(db, order)
            if not event:
                continue
            for item in items:
                await db.execute("UPDATE products SET stock=stock-? WHERE id=?", (item.get("qty", 0), item["id"]))
                if item.get("id") == product_id:
                    consumed += item.get("qty", 0)
            await db.execute("UPDATE orders SET status='done' WHERE id=?", (order["id"],))
            events.extend(event)
        return events, consumed

    async def _issued_event(self, db, order):
        rows = await (await db.execute(
            "SELECT d.product_id,d.payload,p.name FROM deliveries d LEFT JOIN products p ON p.id=d.product_id "
            "WHERE d.order_id=? ORDER BY d.id", (order["id"],))).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault((row["product_id"], row["name"] or "Товар"), []).append(row["payload"])
        return [{"user_id": order["user_id"], "order_id": order["id"], "kind": order["kind"],
                 "items": [{"name": name, "payloads": payloads} for (_, name), payloads in grouped.items()]}]

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
                return {"ok": True, "id": order_id, "status": order["status"]}
            if status not in transitions.get(order["status"], ()):
                raise ApiError("Этот переход статуса запрещён.", 409)
            new_status, issued = status, []
            items = json.loads(order["items"])
            if status == "cancelled" and order["kind"] == "order":
                for item in items:
                    await db.execute("UPDATE products SET stock=stock+? WHERE id=?", (item["qty"], item["id"]))
            elif status == "paid" and order["kind"] == "preorder":
                # Предзаказ действует по предоплате 100%; если товар уже поступил — выдаём сразу.
                if await self._in_stock(db, items):
                    event = await self._try_fulfill(db, order)
                    if event:
                        for item in items:
                            await db.execute("UPDATE products SET stock=stock-? WHERE id=?", (item["qty"], item["id"]))
                        new_status, issued = "done", event
            elif status == "paid":
                event = await self._try_fulfill(db, order)
                if event:
                    new_status, issued = "done", event
            await db.execute("UPDATE orders SET status=? WHERE id=?", (new_status, order_id))
            await db.commit()
        result = {"ok": True, "id": order_id, "status": new_status}
        if issued:
            result["issued"] = issued
        return result


@web.middleware
async def api_errors(request, handler):
    try:
        return await handler(request)
    except ApiError as error:
        return web.json_response({"error": error.message}, status=error.status)


def register_api(app, store, bot_token, admin_ids, support_username, testers=(), notifier=None):
    testers = set(testers)
    app.middlewares.append(api_errors)

    def authenticate(request, admin=False):
        user = optional_user(request)
        if not user:
            raise ApiError("Открой лавку заново через Telegram, чтобы подтвердить вход.", 401)
        if admin and user["id"] not in admin_ids:
            raise ApiError("Эта часть лавки доступна только администратору.", 403)
        return user

    def optional_user(request):
        header = request.headers.get("Authorization", "")
        return verify_init_data(header[4:], bot_token) if header.startswith("tma ") else None

    async def deliver(result):
        """После фиксации выдачи отправляем покупателям строки товара через бота."""
        if notifier and result.get("issued"):
            try:
                await notifier(result["issued"])
            except Exception as error:  # Заказ уже сохранён; сбой уведомления не должен ломать запрос.
                log.warning("Delivery notice failed: %s", error)
        return result

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
        user = optional_user(request)
        include_test = bool(user and (user["id"] in admin_ids or user["id"] in testers))
        return web.json_response(await store.catalog(include_test=include_test))

    async def legacy_products(request):
        user = optional_user(request)
        include_test = bool(user and (user["id"] in admin_ids or user["id"] in testers))
        return web.json_response((await store.catalog(include_test=include_test))["products"])

    async def config(request):
        return web.json_response({"currency": "RUB", "payment_enabled": True, "support_username": support_username,
                                  "images": {"menu": "/static/bg.jpg", "profile": "/static/2.jpg", "support": "/static/shop.jpg"}})

    async def me(request):
        user = authenticate(request)
        profile = await store.profile(user, user["id"] in admin_ids)
        profile["is_tester"] = user["id"] in testers or user["id"] in admin_ids
        return web.json_response(profile)

    async def history(request):
        user = authenticate(request)
        return web.json_response(await store.history(user["id"]))

    async def favorite(request):
        user = authenticate(request)
        return web.json_response(await store.toggle_favorite(user["id"], item_id(request)))

    async def ticket(request):
        user = authenticate(request)
        payload = await body(request)
        return web.json_response(await store.create_ticket(user["id"], payload.get("text", "")))

    async def topup(request):
        user = authenticate(request)
        payload = await body(request)
        amount = payload.get("amount")
        if amount not in (100, 250, 500, 1000, 1500):
            raise ApiError("Выбери доступную сумму пополнения.")
        await store.profile(user, user["id"] in admin_ids)
        raise ApiError("Пополнение через СБП и крипту скоро будет доступно.", 409)

    async def admin_catalog(request):
        authenticate(request, True)
        return web.json_response(await store.catalog(admin=True))

    async def stats(request):
        authenticate(request, True)
        return web.json_response(await store.stats())

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
        return web.json_response(await deliver(await store.change_order(item_id(request), (await body(request)).get("status"))))

    async def delete_product(request):
        authenticate(request, True)
        return web.json_response(await store.delete_product(item_id(request)))

    async def deliveries(request):
        authenticate(request, True)
        return web.json_response(await deliver(await store.add_deliveries(item_id(request), await body(request))))

    async def deliveries_view(request):
        authenticate(request, True)
        return web.json_response(await store.product_deliveries(item_id(request)))

    async def order(request):
        user = authenticate(request)
        payload = await body(request)
        if payload.get("kind") == "test" and user["id"] not in admin_ids and user["id"] not in testers:
            raise ApiError("Тестовые покупки доступны только тестерам лавки.", 403)
        await store.profile(user, user["id"] in admin_ids)
        return web.json_response(await deliver(await store.create_order(user["id"], payload)))

    app.add_routes([
        web.get("/api/catalog", catalog), web.get("/api/products", legacy_products),
        web.get("/api/config", config), web.get("/api/me", me), web.get("/api/history", history),
        web.post("/api/favorites/{id}", favorite), web.post("/api/support/tickets", ticket),
        web.post("/api/wallet/topup", topup), web.post("/api/order", order),
        web.get("/api/admin/stats", stats),
        web.get("/api/admin/catalog", admin_catalog), web.get("/api/admin/orders", orders),
        web.post("/api/admin/categories", categories), web.patch("/api/admin/categories/{id}", categories),
        web.post("/api/admin/products", products), web.patch("/api/admin/products/{id}", products),
        web.delete("/api/admin/products/{id}", delete_product),
        web.post("/api/admin/products/{id}/deliveries", deliveries),
        web.get("/api/admin/products/{id}/deliveries", deliveries_view),
        web.patch("/api/admin/orders/{id}", change_order),
    ])
