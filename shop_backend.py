"""PostgreSQL storage and authenticated API shared by the bot and mini app."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import asyncpg
from aiohttp import web

log = logging.getLogger("lavka.api")

# A self-managed PostgreSQL usually listens on localhost without TLS; hosted
# providers (Supabase, Neon, Railway) require it. DB_SSL=disable/require wins.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "host.docker.internal")


def normalize_dsn(raw):
    """Return (dsn without ssl query params, ssl mode for asyncpg).

    asyncpg rejects the `sslmode` query parameter that hosted providers put in
    their connection strings, so it is stripped here and passed as `ssl=` instead.
    """
    dsn = (raw or "").strip()
    if not dsn:
        return "", None
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://"):]
    scheme, sep, rest = dsn.partition("://")
    if not sep:
        return dsn, None
    address, qsep, query = rest.partition("?")
    kept, wanted = [], None
    for part in (query.split("&") if qsep else []):
        key, _, value = part.partition("=")
        if key.lower() in ("sslmode", "ssl"):
            wanted = value.lower()
            continue
        if key.lower() in ("sslrootcert", "sslcert", "sslkey", "channel_binding"):
            continue
        if part:
            kept.append(part)
    rebuilt = f"{scheme}{sep}{address}" + (("?" + "&".join(kept)) if kept else "")
    override = os.environ.get("DB_SSL", "").strip().lower()
    if override in ("disable", "off", "0"):
        ssl_mode = None
    elif override in ("require", "on", "1"):
        ssl_mode = "require"
    elif wanted in ("require", "verify-ca", "verify-full", "true", "1"):
        ssl_mode = "require"
    else:
        host = address.rsplit("@", 1)[-1].split(":")[0].split("/")[0]
        ssl_mode = None if host in LOCAL_HOSTS else "require"
    return rebuilt, ssl_mode


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


def affected(status):
    """Row count from an asyncpg command tag such as 'UPDATE 3' or 'DELETE 0'."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS categories (
    id BIGSERIAL PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
    warranty TEXT DEFAULT '', price INTEGER NOT NULL DEFAULT 0, category TEXT NOT NULL,
    emoji TEXT DEFAULT '', stock INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
    allow_preorder INTEGER NOT NULL DEFAULT 0, is_test INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries (
    id BIGSERIAL PRIMARY KEY, product_id BIGINT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready', order_id BIGINT, created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, items TEXT NOT NULL,
    total INTEGER NOT NULL, comment TEXT DEFAULT '', status TEXT DEFAULT 'new',
    created_at BIGINT NOT NULL, kind TEXT NOT NULL DEFAULT 'order', payment TEXT NOT NULL DEFAULT 'manual',
    idempotency_key TEXT, request_hash TEXT, units INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    traveler_no BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL UNIQUE,
    first_name TEXT DEFAULT '', username TEXT DEFAULT '', created_at BIGINT NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0, referred_by BIGINT,
    referral_rewarded INTEGER NOT NULL DEFAULT 0, bot_blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS favorites (
    user_id BIGINT NOT NULL, product_id BIGINT NOT NULL, created_at BIGINT NOT NULL,
    PRIMARY KEY (user_id, product_id)
);
CREATE TABLE IF NOT EXISTS support_tickets (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, product_id BIGINT NOT NULL,
    order_id BIGINT NOT NULL, rating INTEGER NOT NULL, rating_text TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL, UNIQUE (order_id, product_id)
);
CREATE INDEX IF NOT EXISTS orders_by_user ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS orders_by_kind ON orders(kind, status, id);
CREATE INDEX IF NOT EXISTS deliveries_queue ON deliveries(product_id, status, id);
CREATE INDEX IF NOT EXISTS deliveries_by_order ON deliveries(order_id);
CREATE INDEX IF NOT EXISTS reviews_by_product ON reviews(product_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS order_retry ON orders(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
"""

MIGRATIONS = {
    "products": {
        "warranty": "TEXT DEFAULT ''",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "allow_preorder": "INTEGER NOT NULL DEFAULT 0",
        "is_test": "INTEGER NOT NULL DEFAULT 0",
    },
    "orders": {
        "kind": "TEXT NOT NULL DEFAULT 'order'",
        "payment": "TEXT NOT NULL DEFAULT 'manual'",
        "idempotency_key": "TEXT",
        "request_hash": "TEXT",
        "units": "INTEGER NOT NULL DEFAULT 0",
    },
    "users": {
        "referred_by": "BIGINT",
        "referral_rewarded": "INTEGER NOT NULL DEFAULT 0",
        "bot_blocked": "INTEGER NOT NULL DEFAULT 0",
    },
}


class Store:
    """PostgreSQL store. One connection pool for the whole process."""

    def __init__(self, database_url=None, schema=None):
        # schema is used by the tests to keep their tables apart from production.
        self.schema = schema or os.environ.get("DB_SCHEMA", "").strip() or "public"
        # The DSN is only required when the bot actually starts, so importing the
        # module (tooling, tests) does not need a database.
        self.database_url, self.ssl_mode = normalize_dsn(database_url or os.environ.get("DATABASE_URL", ""))
        self.pool = None

    async def connect(self):
        """Create the pool. Safe to call twice; also usable as an explicit startup step."""
        if not self.database_url:
            raise RuntimeError(
                "Set DATABASE_URL to a PostgreSQL connection string (the Supabase or Neon URI). "
                "SQLite is no longer supported."
            )
        if self.pool is not None:
            return self.pool
        # statement_cache_size=0 keeps the pool compatible with transaction-mode
        # poolers (Supabase :6543, pgbouncer); DB_STATEMENT_CACHE=1 re-enables it.
        cache = 0 if os.environ.get("DB_STATEMENT_CACHE", "0") != "1" else 100
        self.pool = await asyncpg.create_pool(
            self.database_url, ssl=self.ssl_mode, min_size=int(os.environ.get("DB_POOL_MIN", "1")),
            max_size=int(os.environ.get("DB_POOL_MAX", "10")), max_inactive_connection_lifetime=180,
            statement_cache_size=cache, command_timeout=30,
            # Every pooled connection works inside this schema.
            server_settings={"search_path": self.schema},
        )
        return self.pool

    async def execute_raw(self, sql):
        """Direct statement on a pooled connection: used for DDL and test cleanup."""
        async with self.connection() as db:
            return await db.execute(sql)

    async def close(self):
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def connection(self):
        """Read-only connection from the pool (no transaction)."""
        if self.pool is None:
            await self.connect()
        async with self.pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def transaction(self):
        """Connection wrapped in a transaction: all writes commit or roll back together."""
        if self.pool is None:
            await self.connect()
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                yield connection

    async def init_db(self):
        async with self.connection() as db:
            await db.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
        async with self.transaction() as db:
            await db.execute(SCHEMA_TEMPLATE)
            for table, fields in MIGRATIONS.items():
                for name, definition in fields.items():
                    await db.execute(
                        f'ALTER TABLE "{self.schema}".{table} ADD COLUMN IF NOT EXISTS {name} {definition}'
                    )
            # One-time backfill for orders saved before the units column existed.
            await db.execute(
                "UPDATE orders SET units = COALESCE((SELECT SUM(COALESCE((item->>'qty')::int,0)) "
                "FROM jsonb_array_elements(items::jsonb) AS item),0) "
                "WHERE units=0 AND status IN ('paid','done') AND items LIKE '[%'"
            )
            for slug, name in (("chatgpt", "ChatGPT"), ("gemini", "Gemini"), ("capcut", "CapCut")):
                # Seeded per schema: a test schema must get the same three shelves.
                await db.execute(
                    "INSERT INTO categories(slug,name) VALUES($1,$2) ON CONFLICT (slug) DO NOTHING", slug, name,
                )
            # Preserve old demo records, but never sell candles/relics in the AI shop.
            await db.execute(
                "INSERT INTO categories(slug,name,active) "
                "SELECT DISTINCT category, category, 0 FROM products WHERE category IS NOT NULL "
                "ON CONFLICT (slug) DO NOTHING"
            )

    PRODUCT_COLUMNS = """
        p.*,
        (SELECT COUNT(*) FROM deliveries d WHERE d.product_id=p.id AND d.status='ready') AS ready_items,
        (SELECT COUNT(*) FROM deliveries d WHERE d.product_id=p.id AND d.status='issued') AS issued_items,
        (SELECT ROUND(AVG(r.rating)::numeric, 2) FROM reviews r WHERE r.product_id=p.id) AS rating,
        (SELECT COUNT(*) FROM reviews r WHERE r.product_id=p.id) AS reviews
    """

    def _crowd(self, row):
        """One product row with counters, rating and review count attached."""
        item = dict(row)
        item["ready_items"] = int(item.get("ready_items") or 0)
        item["issued_items"] = int(item.get("issued_items") or 0)
        item["rating"] = round(float(item["rating"]), 2) if item.get("rating") is not None else None
        item["reviews"] = int(item.get("reviews") or 0)
        return item

    async def catalog(self, admin=False, include_test=False):
        """Categories with stock totals plus every visible product, in two queries."""
        where = "" if admin else " WHERE p.active=1 AND c.active=1" + ("" if include_test else " AND p.is_test=0")
        async with self.connection() as db:
            categories = [dict(row) for row in await db.fetch(
                "SELECT * FROM categories" + ("" if admin else " WHERE active=1") + " ORDER BY id"
            )]
            products = [self._crowd(row) for row in await db.fetch(
                f"SELECT {self.PRODUCT_COLUMNS} FROM products p JOIN categories c ON c.slug=p.category{where} ORDER BY p.id"
            )]
        totals = {}
        for product in products:
            if product["active"]:
                totals[product["category"]] = totals.get(product["category"], 0) + product["stock"]
        for category in categories:
            category["stock"] = totals.get(category["slug"], 0)
        return {"categories": categories, "products": products}

    async def profile(self, user, is_admin=False):
        """Traveler card: spend, favorites, preorders and bonus in a few aggregates."""
        async with self.transaction() as db:
            row = dict(await db.fetchrow(
                "INSERT INTO users(user_id,first_name,username,created_at) VALUES($1,$2,$3,$4) "
                "ON CONFLICT (user_id) DO UPDATE SET first_name=EXCLUDED.first_name, username=EXCLUDED.username "
                "RETURNING *",
                user["id"], str(user.get("first_name", ""))[:256], str(user.get("username", ""))[:64], int(time.time()),
            ))
            stats = await db.fetchrow(
                "SELECT COALESCE(SUM(units),0) AS purchases, COALESCE(SUM(total),0) AS spent FROM orders "
                "WHERE user_id=$1 AND status IN ('paid','done') AND kind='order'", user["id"],
            )
            favorites = await db.fetch(
                "SELECT item->>'name' AS name, SUM(COALESCE((item->>'qty')::int,0)) AS qty FROM orders o, "
                "jsonb_array_elements(o.items::jsonb) AS item "
                "WHERE o.user_id=$1 AND o.status IN ('paid','done') AND o.kind='order' GROUP BY 1 ORDER BY 2 DESC",
                user["id"],
            )
            preorders = await db.fetchval(
                "SELECT COUNT(*) FROM orders WHERE user_id=$1 AND kind='preorder' AND status IN ('preorder','paid')",
                user["id"],
            )
        return {
            "traveler_no": row["traveler_no"], "user_id": row["user_id"], "purchases": int(stats["purchases"]),
            "spent": int(stats["spent"]), "favorite_product": (favorites[0]["name"] or "Товар") if favorites else None,
            "balance": row["balance"], "preorders": int(preorders or 0), "is_admin": bool(is_admin),
            "referral_link": f"https://t.me/{os.environ.get('BOT_USERNAME', 'wanderersshop_bot').lstrip('@')}?start=ref_{row['user_id']}",
        }

    async def referral_stats(self, user_id):
        """Invites and the accrued bonus: 3% of an invited traveler's paid orders."""
        async with self.connection() as db:
            row = await db.fetchrow(
                "SELECT (SELECT COUNT(*) FROM users WHERE referred_by=$1) AS invited, "
                "COALESCE((SELECT SUM(total) FROM orders WHERE status IN ('paid','done') AND kind='order' "
                "AND user_id IN (SELECT user_id FROM users WHERE referred_by=$1)),0) AS turnover", user_id,
            )
        turnover = int(row["turnover"] or 0)
        return {"invited": int(row["invited"] or 0), "turnover": turnover, "earned": turnover * 3 // 100}

    async def history(self, user_id):
        async with self.connection() as db:
            rows = await db.fetch(
                "SELECT id,items,total,status,created_at FROM orders WHERE user_id=$1 ORDER BY id DESC LIMIT 100",
                user_id,
            )
        result = []
        for row in rows:
            result.append({"id": row["id"], "items": json.loads(row["items"]), "total": row["total"],
                           "status": row["status"],
                           "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat()})
        return {"orders": result}

    async def toggle_favorite(self, user_id, product_id):
        async with self.transaction() as db:
            row = await db.fetchrow(
                "SELECT 1 FROM favorites WHERE user_id=$1 AND product_id=$2", user_id, product_id,
            )
            if row:
                await db.execute("DELETE FROM favorites WHERE user_id=$1 AND product_id=$2", user_id, product_id)
                active = False
            else:
                await db.execute(
                    "INSERT INTO favorites(user_id,product_id,created_at) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                    user_id, product_id, int(time.time()),
                )
                active = True
        return {"ok": True, "favorite": active}

    async def stats(self):
        async with self.connection() as db:
            row = await db.fetchrow(
                "SELECT COUNT(*) AS orders, COALESCE(SUM(total),0) AS revenue FROM orders WHERE status IN ('paid','done')"
            )
            users = await db.fetchval("SELECT COUNT(*) FROM users")
            products = await db.fetchval("SELECT COUNT(*) FROM products WHERE active=1")
            reviews = await db.fetchrow(
                "SELECT COUNT(*) AS n, COALESCE(ROUND(AVG(rating)::numeric,2),0) AS avg FROM reviews"
            )
        return {"orders": int(row["orders"]), "revenue": int(row["revenue"]), "users": int(users),
                "products": int(products), "reviews": int(reviews["n"]), "rating": float(reviews["avg"])}

    async def user_ids(self):
        async with self.connection() as db:
            rows = await db.fetch("SELECT user_id FROM users")
        return [int(row["user_id"]) for row in rows]

    async def restock_recipients(self):
        """Everyone who ever started the bot and has not blocked it."""
        async with self.connection() as db:
            rows = await db.fetch("SELECT user_id FROM users WHERE bot_blocked=0 ORDER BY traveler_no")
        return [int(row["user_id"]) for row in rows]

    async def mark_blocked(self, user_id, blocked=True):
        async with self.connection() as db:
            await db.execute("UPDATE users SET bot_blocked=$2 WHERE user_id=$1", user_id, 1 if blocked else 0)

    async def users_overview(self, limit=200):
        """Покупатели одним запросом: баланс, покупки и открытые предзаказы."""
        async with self.connection() as db:
            rows = await db.fetch(
                "SELECT u.traveler_no,u.user_id,u.first_name,u.username,u.balance,u.bot_blocked,"
                "(SELECT COALESCE(SUM(o.total),0) FROM orders o WHERE o.user_id=u.user_id "
                " AND o.status IN ('paid','done') AND o.kind='order') AS spent,"
                "(SELECT COUNT(*) FROM orders o WHERE o.user_id=u.user_id) AS orders,"
                "(SELECT COUNT(*) FROM orders o WHERE o.user_id=u.user_id AND o.kind='preorder' "
                " AND o.status IN ('preorder','paid')) AS preorders "
                "FROM users u ORDER BY u.traveler_no DESC LIMIT $1", limit,
            )
        return {"users": [{"traveler_no": int(row["traveler_no"]), "user_id": int(row["user_id"]),
                           "name": row["first_name"] or "Странник", "username": row["username"] or "",
                           "balance": int(row["balance"]), "spent": int(row["spent"]),
                           "orders": int(row["orders"]), "preorders": int(row["preorders"]),
                           "blocked": bool(row["bot_blocked"])} for row in rows]}

    async def create_ticket(self, user_id, message):
        message = text(message, "Сообщение", 4000, True)
        async with self.transaction() as db:
            ticket_id = await db.fetchval(
                "INSERT INTO support_tickets(user_id,text,created_at) VALUES($1,$2,$3) RETURNING id",
                user_id, message, int(time.time()),
            )
        return {"ok": True, "id": ticket_id}

    async def add_balance(self, user_id, amount):
        amount = integer(amount, "Сумма пополнения", 10**6, 1)
        async with self.transaction() as db:
            balance = await db.fetchval(
                "INSERT INTO users(user_id,created_at,balance) VALUES($1,$2,$3) "
                "ON CONFLICT (user_id) DO UPDATE SET balance=users.balance+EXCLUDED.balance RETURNING balance",
                user_id, int(time.time()), amount,
            )
        return int(balance)

    async def save_category(self, body, item_id=None):
        name = text(body.get("name"), "Название", 120, True)
        active = flag(body.get("active", True), "Показывать")
        async with self.transaction() as db:
            if item_id is None:
                slug = text(body.get("slug"), "Код категории", 80, True)
                if not re.fullmatch(r"[a-z0-9]+(?:(?:-|_)[a-z0-9]+)*", slug):
                    raise ApiError("Код категории: латинские буквы, цифры, дефис или подчёркивание.")
                try:
                    item_id = await db.fetchval(
                        "INSERT INTO categories(slug,name,active) VALUES($1,$2,$3) RETURNING id", slug, name, active,
                    )
                except asyncpg.UniqueViolationError:
                    raise ApiError("Категория с таким кодом уже существует.", 409)
            else:
                if "slug" in body:
                    raise ApiError("Код существующей категории менять нельзя.")
                if not affected(await db.execute(
                    "UPDATE categories SET name=$1,active=$2 WHERE id=$3", name, active, item_id,
                )):
                    raise ApiError("Категория не найдена.", 404)
        return {"ok": True, "id": item_id}

    async def save_product(self, body, item_id=None):
        values = (text(body.get("name"), "Название", 200, True), text(body.get("description", ""), "Описание", 4000),
                  text(body.get("warranty", ""), "Гарантия", 500),
                  integer(body.get("price", 0), "Цена"), text(body.get("category"), "Категория", 80, True),
                  integer(body.get("stock", 0), "Остаток", 10**6), flag(body.get("active", True), "Показывать"),
                  flag(body.get("allow_preorder", False), "Предзаказ"), flag(body.get("is_test", False), "Тестовый товар"))
        async with self.transaction() as db:
            if not await db.fetchval("SELECT id FROM categories WHERE slug=$1", values[4]):
                raise ApiError("Сначала создай категорию.")
            if item_id is None:
                item_id = await db.fetchval(
                    "INSERT INTO products(name,description,warranty,price,category,stock,active,allow_preorder,is_test) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id", *values,
                )
            elif not affected(await db.execute(
                "UPDATE products SET name=$1,description=$2,warranty=$3,price=$4,category=$5,stock=$6,active=$7,"
                "allow_preorder=$8,is_test=$9 WHERE id=$10", *values, item_id,
            )):
                raise ApiError("Товар не найден.", 404)
        return {"ok": True, "id": item_id}

    async def patch_product(self, item_id, fields):
        """Change a single product field from the bot admin menu."""
        allowed = ("name", "price", "stock", "description", "warranty", "category", "active",
                   "allow_preorder", "is_test")
        if not fields or set(fields) - set(allowed):
            raise ApiError("Неизвестное поле товара.")
        product = await self.product(item_id)
        if not product:
            raise ApiError("Товар не найден.", 404)
        body = {key: product[key] for key in
                ("name", "description", "warranty", "price", "category", "stock", "active", "allow_preorder", "is_test")}
        # PostgreSQL INTEGER flag columns come back as 0/1. save_product()
        # deliberately accepts real booleans only, so normalize untouched flags
        # before rebuilding the complete product payload.
        for key in ("active", "allow_preorder", "is_test"):
            body[key] = bool(body[key])
        for key, value in fields.items():
            if key in ("active", "allow_preorder", "is_test"):
                body[key] = bool(value)
            elif key in ("price", "stock"):
                body[key] = integer(value, "Значение", 10**6 if key == "stock" else 10**9)
            else:
                body[key] = text(value, "Значение", 4000 if key == "description" else 200, key == "name")
        return await self.save_product(body, item_id)

    async def delete_product(self, item_id):
        async with self.transaction() as db:
            await db.execute("DELETE FROM deliveries WHERE product_id=$1", item_id)
            await db.execute("DELETE FROM reviews WHERE product_id=$1", item_id)
            if not affected(await db.execute("DELETE FROM products WHERE id=$1", item_id)):
                raise ApiError("Товар не найден.", 404)
        return {"ok": True, "id": item_id}

    async def create_order(self, user_id, body):
        cart = body.get("cart")
        if not isinstance(cart, list) or not 1 <= len(cart) <= 30:
            raise ApiError("Корзина должна содержать от 1 до 30 товаров.")
        kind = body.get("kind", "order")
        payment = body.get("payment", "manual")
        if payment not in ("manual", "balance", "crypto", "sbp"):
            raise ApiError("Неизвестный способ оплаты.")
        if kind not in ("order", "test", "preorder"):
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
        digest = hashlib.sha256(json.dumps(
            [sorted(requested.items()), kind, payment, comment], ensure_ascii=False).encode()).hexdigest()
        async with self.transaction() as db:
            previous = await db.fetchrow(
                "SELECT * FROM orders WHERE user_id=$1 AND idempotency_key=$2", user_id, key,
            )
            if previous:
                if previous["request_hash"] != digest:
                    raise ApiError("Этот ключ уже использован для другой заявки.", 409)
                result = {"ok": True, "order_id": previous["id"], "total": previous["total"], "items": json.loads(previous["items"]), "replayed": True}
                if previous["kind"] == "test":
                    result["test"] = True
                    result["issued"] = await self._issued_event(db, previous)
                return result
            items, total, allocation = [], 0, []
            for product_id, qty in sorted(requested.items()):
                product = await db.fetchrow(
                    "SELECT p.* FROM products p JOIN categories c ON c.slug=p.category "
                    "WHERE p.id=$1 AND p.active=1 AND c.active=1", product_id,
                )
                if not product or product["price"] <= 0:
                    raise ApiError("Товар недоступен или цена ещё не задана.", 409)
                if kind == "test":
                    if not product["is_test"]:
                        raise ApiError("Тестовая покупка доступна только для тестовых товаров.", 409)
                    if product["stock"] < qty:
                        raise ApiError("Недостаточно единиц тестового товара.", 409)
                    rows = await db.fetch(
                        "SELECT id,payload FROM deliveries WHERE product_id=$1 AND status='ready' ORDER BY id LIMIT $2",
                        product_id, qty,
                    )
                    if len(rows) < qty:
                        raise ApiError("Сначала загрузи автовыдачу для тестового товара.", 409)
                    allocation.append((product_id, qty, rows))
                elif kind == "preorder":
                    if not product["allow_preorder"]:
                        raise ApiError("Предзаказ для этого товара недоступен.", 409)
                    if product["stock"] > 0:
                        raise ApiError("Товар уже в наличии — предзаказ не нужен.", 409)
                elif product["stock"] < qty:
                    raise ApiError("Недостаточно товара. Обнови каталог.", 409)
                else:
                    ready = await db.fetchval(
                        "SELECT COUNT(*) FROM deliveries WHERE product_id=$1 AND status='ready'", product_id,
                    )
                    if ready < qty:
                        raise ApiError("Товар ещё не готов к автоматической выдаче. Попробуй позже.", 409)
                    await db.execute("UPDATE products SET stock=stock-$1 WHERE id=$2", qty, product_id)
                total += product["price"] * qty
                items.append({"id": product_id, "name": product["name"], "price": product["price"], "qty": qty})
            if kind == "test":
                total = 0
            if kind == "order" and payment == "balance":
                balance = await db.fetchval("SELECT balance FROM users WHERE user_id=$1", user_id)
                if balance is None or balance < total:
                    raise ApiError("Недостаточно средств на балансе.", 409)
                await db.execute("UPDATE users SET balance=balance-$1 WHERE user_id=$2", total, user_id)
            status = "done" if kind == "test" else ("preorder" if kind == "preorder" else ("paid" if payment == "balance" else "new"))
            order_id = await db.fetchval(
                "INSERT INTO orders(user_id,items,total,comment,status,created_at,kind,payment,idempotency_key,request_hash,units) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id",
                user_id, json.dumps(items, ensure_ascii=False), total, comment, status, int(time.time()), kind, payment, key,
                digest, sum(item["qty"] for item in items),
            )
            if kind == "test":
                for product_id, qty, rows in allocation:
                    await db.execute("UPDATE products SET stock=stock-$1 WHERE id=$2", qty, product_id)
                    await db.execute(
                        "UPDATE deliveries SET status='issued', order_id=$1 WHERE id = ANY($2::bigint[])",
                        order_id, [row["id"] for row in rows],
                    )
            issued = []
            if kind == "order" and payment == "balance":
                issued = await self._try_fulfill(db, await db.fetchrow("SELECT * FROM orders WHERE id=$1", order_id))
                if issued:
                    await db.execute("UPDATE orders SET status='done' WHERE id=$1", order_id)
            result = {"ok": True, "order_id": order_id, "total": total, "items": items, "replayed": False}
            if issued:
                result["issued"] = issued
            if kind == "test":
                names = {item["id"]: item["name"] for item in items}
                result["test"] = True
                result["issued"] = [{"user_id": user_id, "order_id": order_id, "kind": "test",
                                     "items": [{"name": names[product_id], "payloads": [row["payload"] for row in rows]}
                                               for product_id, qty, rows in allocation]}]
            return result

    async def pay_order_balance(self, user_id, order_id):
        async with self.transaction() as db:
            order = await db.fetchrow("SELECT * FROM orders WHERE id=$1 AND user_id=$2 FOR UPDATE", order_id, user_id)
            if not order:
                raise ApiError("Счёт не найден.", 404)
            if order["status"] != "new":
                raise ApiError("Этот счёт уже обработан.", 409)
            balance = await db.fetchval("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            if balance is None or balance < order["total"]:
                raise ApiError("Недостаточно средств на балансе.", 409)
            await db.execute("UPDATE users SET balance=balance-$1 WHERE user_id=$2", order["total"], user_id)
            issued = await self._try_fulfill(db, order)
            status = "done" if issued else "paid"
            await db.execute("UPDATE orders SET status=$1,payment='balance' WHERE id=$2", status, order_id)
        result = {"ok": True, "id": order_id, "order_id": order_id, "status": status, "total": order["total"],
                  "items": json.loads(order["items"])}
        if issued:
            result["issued"] = issued
        return result

    async def set_referrer(self, user_id, referrer_id):
        if user_id == referrer_id:
            return
        async with self.connection() as db:
            await db.execute(
                "UPDATE users SET referred_by=$1 WHERE user_id=$2 AND referred_by IS NULL", referrer_id, user_id,
            )

    async def product(self, product_id):
        async with self.connection() as db:
            row = await db.fetchrow(f"SELECT {self.PRODUCT_COLUMNS} FROM products p WHERE p.id=$1", product_id)
        return self._crowd(row) if row else None

    async def active_preorder(self, user_id, product_id):
        """The user's open preorder for a product, so the button never creates duplicates."""
        async with self.connection() as db:
            row = await db.fetchrow(
                "SELECT o.id,o.status FROM orders o WHERE o.user_id=$1 AND o.kind='preorder' "
                "AND o.status IN ('preorder','paid') AND EXISTS "
                "(SELECT 1 FROM jsonb_array_elements(o.items::jsonb) AS item WHERE (item->>'id')::bigint=$2) "
                "ORDER BY o.id DESC LIMIT 1", user_id, product_id,
            )
        return {"id": row["id"], "status": row["status"]} if row else None

    async def user_preorders(self, user_id):
        """Return this user's still-open preorders for the bot shelf."""
        async with self.connection() as db:
            rows = await db.fetch(
                "SELECT id,items,total,status,created_at FROM orders "
                "WHERE user_id=$1 AND kind='preorder' AND status IN ('preorder','paid') ORDER BY id DESC", user_id,
            )
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
        """Test purchase without payment: the order is done and a delivery line is issued at once."""
        async with self.transaction() as db:
            product = await db.fetchrow("SELECT * FROM products WHERE id=$1 AND is_test=1", product_id)
            if not product or not product["active"]:
                raise ApiError("Тестовый товар не найден.", 404)
            if product["price"] <= 0:
                raise ApiError("Сначала задай цену тестового товара.", 409)
            rows = await db.fetch(
                "SELECT id,payload FROM deliveries WHERE product_id=$1 AND status='ready' ORDER BY id LIMIT $2",
                product_id, qty,
            )
            if product["stock"] < qty or len(rows) < qty:
                raise ApiError("Нет свободных единиц тестового товара: загрузи автовыдачу в админке.", 409)
            items = [{"id": product_id, "name": product["name"], "price": product["price"], "qty": qty}]
            order_id = await db.fetchval(
                "INSERT INTO orders(user_id,items,total,comment,status,created_at,kind,units) "
                "VALUES($1,$2,0,'','done',$3,'test',$4) RETURNING id",
                user_id, json.dumps(items, ensure_ascii=False), int(time.time()), qty,
            )
            await db.execute("UPDATE products SET stock=stock-$1 WHERE id=$2", qty, product_id)
            await db.execute("UPDATE deliveries SET status='issued', order_id=$1 WHERE id = ANY($2::bigint[])",
                             order_id, [row["id"] for row in rows])
            return {"ok": True, "order_id": order_id, "total": 0, "issued": [{
                "user_id": user_id, "order_id": order_id, "kind": "test",
                "items": [{"name": product["name"], "payloads": [row["payload"] for row in rows]}]}]}

    async def add_deliveries(self, product_id, body):
        """Upload auto-delivery lines: paid preorders are served first, the rest goes on the shelf."""
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
        async with self.transaction() as db:
            if not await db.fetchval("SELECT id FROM products WHERE id=$1", product_id):
                raise ApiError("Товар не найден.", 404)
            await db.executemany(
                "INSERT INTO deliveries(product_id,payload,status,created_at) VALUES($1,$2,'ready',$3)",
                [(product_id, line, now) for line in lines],
            )
            await db.execute("UPDATE products SET stock=stock+$1 WHERE id=$2", len(lines), product_id)
            events, consumed = await self._fulfill_preorders(db, product_id)
        return {"ok": True, "id": product_id, "added": len(lines), "delivered_to_preorders": len(events),
                "stock_added": len(lines) - consumed, "issued": events}

    async def delivery_items(self, product_id, limit=25):
        async with self.connection() as db:
            rows = await db.fetch(
                "SELECT id,payload,created_at FROM deliveries WHERE product_id=$1 AND status='ready' ORDER BY id LIMIT $2",
                product_id, limit,
            )
        return [{"id": row["id"], "payload": row["payload"], "created_at": row["created_at"]} for row in rows]

    async def delete_delivery(self, product_id, delivery_id):
        async with self.transaction() as db:
            if not await db.fetchval(
                "SELECT id FROM deliveries WHERE id=$1 AND product_id=$2 AND status='ready'", delivery_id, product_id,
            ):
                raise ApiError("Строка автовыдачи не найдена или уже выдана.", 404)
            await db.execute("DELETE FROM deliveries WHERE id=$1", delivery_id)
            await db.execute("UPDATE products SET stock=GREATEST(stock-1,0) WHERE id=$1", product_id)
        return {"ok": True, "id": delivery_id, "product_id": product_id}

    async def replace_delivery(self, product_id, delivery_id, payload):
        payload = text(payload, "Строка товара", 2000, True)
        async with self.transaction() as db:
            if not await db.fetchval(
                "SELECT id FROM deliveries WHERE id=$1 AND product_id=$2 AND status='ready'", delivery_id, product_id,
            ):
                raise ApiError("Строка автовыдачи не найдена или уже выдана.", 404)
            await db.execute("UPDATE deliveries SET payload=$1 WHERE id=$2", payload, delivery_id)
        return {"ok": True, "id": delivery_id, "product_id": product_id}

    async def product_deliveries(self, product_id):
        async with self.connection() as db:
            if not await db.fetchval("SELECT id FROM products WHERE id=$1", product_id):
                raise ApiError("Товар не найден.", 404)
            counts = {row["status"]: int(row["n"]) for row in await db.fetch(
                "SELECT status,COUNT(*) AS n FROM deliveries WHERE product_id=$1 GROUP BY status", product_id,
            )}
            ready = await db.fetch(
                "SELECT payload FROM deliveries WHERE product_id=$1 AND status='ready' ORDER BY id LIMIT 5", product_id,
            )
            issued = await db.fetch(
                "SELECT payload,order_id FROM deliveries WHERE product_id=$1 AND status='issued' ORDER BY id DESC LIMIT 10",
                product_id,
            )
        return {"ok": True, "id": product_id, "ready": counts.get("ready", 0), "issued": counts.get("issued", 0),
                "ready_samples": [row["payload"] for row in ready],
                "recent_issued": [{"payload": row["payload"], "order_id": row["order_id"]} for row in issued]}

    async def add_review(self, user_id, order_id, product_id, rating, rating_text=""):
        """One review per purchased item: leaving it again updates the previous one."""
        rating = integer(rating, "Оценка", 5, 1)
        rating_text = text(rating_text, "Отзыв", 1000)
        async with self.transaction() as db:
            order = await db.fetchrow("SELECT * FROM orders WHERE id=$1 AND user_id=$2", order_id, user_id)
            if not order:
                raise ApiError("Заказ не найден.", 404)
            if order["status"] not in ("paid", "done"):
                raise ApiError("Отзыв можно оставить только после покупки.", 409)
            try:
                items = json.loads(order["items"])
            except (ValueError, TypeError):
                items = []
            purchased = next((item for item in items if item.get("id") == product_id), None)
            if not purchased:
                raise ApiError("В этом заказе такого товара нет.", 409)
            await db.execute(
                "INSERT INTO reviews(user_id,product_id,order_id,rating,rating_text,created_at) "
                "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT (order_id,product_id) DO UPDATE SET "
                "rating=EXCLUDED.rating, rating_text=EXCLUDED.rating_text, created_at=EXCLUDED.created_at",
                user_id, product_id, order_id, rating, rating_text, int(time.time()),
            )
        return {
            "ok": True,
            "order_id": order_id,
            "product_id": product_id,
            "rating": rating,
            "product_name": purchased.get("name") or "Товар",
            "quantity": int(purchased.get("qty") or 1),
        }

    async def product_reviews(self, product_id, limit=10):
        async with self.connection() as db:
            rows = await db.fetch(
                "SELECT r.rating,r.rating_text,r.created_at,u.first_name FROM reviews r "
                "LEFT JOIN users u ON u.user_id=r.user_id WHERE r.product_id=$1 ORDER BY r.id DESC LIMIT $2",
                product_id, limit,
            )
            summary = await db.fetchrow(
                "SELECT COUNT(*) AS n, COALESCE(ROUND(AVG(rating)::numeric,2),0) AS avg FROM reviews WHERE product_id=$1",
                product_id,
            )
        return {"reviews": [{"rating": int(row["rating"]), "text": row["rating_text"],
                             "created_at": int(row["created_at"]), "name": row["first_name"] or "Странник"}
                            for row in rows],
                "count": int(summary["n"]), "average": float(summary["avg"])}

    async def recent_reviews(self, limit=20):
        async with self.connection() as db:
            rows = await db.fetch(
                "SELECT r.rating,r.rating_text,r.created_at,r.order_id,p.name AS product FROM reviews r "
                "LEFT JOIN products p ON p.id=r.product_id ORDER BY r.id DESC LIMIT $1", limit,
            )
        return {"reviews": [{"rating": int(row["rating"]), "text": row["rating_text"],
                             "created_at": int(row["created_at"]), "order_id": row["order_id"],
                             "product": row["product"] or "Товар"} for row in rows]}

    async def _in_stock(self, db, items):
        """Auto-delivery is possible only when every position has ready lines queued."""
        for item in items:
            ready = await db.fetchval(
                "SELECT COUNT(*) FROM deliveries WHERE product_id=$1 AND status='ready'", item["id"],
            )
            if ready < item.get("qty", 0):
                return False
        return True

    async def _try_fulfill(self, db, order):
        """Issue queued lines for a paid order: all or nothing."""
        try:
            items = json.loads(order["items"])
        except (ValueError, TypeError):
            return []
        plan = []
        for item in items:
            qty = item.get("qty", 0)
            if type(qty) is not int or qty < 1:
                return []
            rows = await db.fetch(
                "SELECT id,payload FROM deliveries WHERE product_id=$1 AND status='ready' ORDER BY id LIMIT $2",
                item["id"], qty,
            )
            if len(rows) < qty:
                return []
            plan.append((item, rows))
        delivered = []
        for item, rows in plan:
            await db.execute("UPDATE deliveries SET status='issued', order_id=$1 WHERE id = ANY($2::bigint[])",
                             order["id"], [row["id"] for row in rows])
            delivered.append({"name": item.get("name", "Товар"), "payloads": [row["payload"] for row in rows]})
        return [{"user_id": order["user_id"], "order_id": order["id"], "kind": order["kind"], "items": delivered}]

    async def _fulfill_preorders(self, db, product_id):
        """100% prepaid preorders get the goods in checkout order, before the shelf is restocked."""
        rows = await db.fetch(
            "SELECT * FROM orders WHERE kind='preorder' AND status='paid' AND EXISTS "
            "(SELECT 1 FROM jsonb_array_elements(items::jsonb) AS item WHERE (item->>'id')::bigint=$1) ORDER BY id",
            product_id,
        )
        events, consumed = [], 0
        for order in rows:
            try:
                items = json.loads(order["items"])
            except (ValueError, TypeError):
                continue
            if not await self._in_stock(db, items):
                # Do not let a later preorder jump ahead of an earlier one.
                break
            event = await self._try_fulfill(db, order)
            if not event:
                continue
            for item in items:
                await db.execute("UPDATE products SET stock=stock-$1 WHERE id=$2", item.get("qty", 0), item["id"])
                if item.get("id") == product_id:
                    consumed += item.get("qty", 0)
            await db.execute("UPDATE orders SET status='done' WHERE id=$1", order["id"])
            events.extend(event)
        return events, consumed

    async def _issued_event(self, db, order):
        rows = await db.fetch(
            "SELECT d.product_id,d.payload,p.name FROM deliveries d LEFT JOIN products p ON p.id=d.product_id "
            "WHERE d.order_id=$1 ORDER BY d.id", order["id"],
        )
        grouped = {}
        for row in rows:
            grouped.setdefault((row["product_id"], row["name"] or "Товар"), []).append(row["payload"])
        return [{"user_id": order["user_id"], "order_id": order["id"], "kind": order["kind"],
                 "items": [{"id": product_id, "name": name, "payloads": payloads}
                           for (product_id, name), payloads in grouped.items()]}]

    async def orders(self):
        async with self.connection() as db:
            rows = [dict(row) for row in await db.fetch("SELECT * FROM orders ORDER BY id DESC LIMIT 200")]
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
        async with self.transaction() as db:
            order = await db.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
            if not order:
                raise ApiError("Заказ не найден.", 404)
            if status == order["status"]:
                return {"ok": True, "id": order_id, "status": order["status"]}
            if status not in transitions.get(order["status"], ()):
                raise ApiError("Этот переход статуса запрещён.", 409)
            new_status, issued = status, []
            items = json.loads(order["items"])
            if status == "cancelled":
                # A preorder reserves nothing, so only ordinary orders give the stock back.
                if order["kind"] == "order":
                    for item in items:
                        await db.execute("UPDATE products SET stock=stock+$1 WHERE id=$2", item["qty"], item["id"])
            elif status == "paid" and order["kind"] == "preorder":
                # Preorders are prepaid in full; if the goods already arrived, issue at once.
                if await self._in_stock(db, items):
                    event = await self._try_fulfill(db, order)
                    if event:
                        for item in items:
                            await db.execute("UPDATE products SET stock=stock-$1 WHERE id=$2", item["qty"], item["id"])
                        new_status, issued = "done", event
            elif status == "paid":
                event = await self._try_fulfill(db, order)
                if event:
                    new_status, issued = "done", event
            await db.execute("UPDATE orders SET status=$1 WHERE id=$2", new_status, order_id)
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


def register_api(app, store, bot_token, admin_ids, support_username, testers=(), notifier=None,
                 order_notifier=None, product_notifier=None, panel_token=""):
    testers = set(testers)
    app.middlewares.append(api_errors)

    def optional_user(request):
        header = request.headers.get("Authorization", "")
        return verify_init_data(header[4:], bot_token) if header.startswith("tma ") else None

    def panel_authorized(request):
        """Admin panel in a normal browser: the owner's token instead of Telegram initData."""
        if not panel_token:
            return False
        return secrets.compare_digest(request.headers.get("X-Admin-Token", ""), panel_token)

    def authenticate(request, admin=False):
        if admin and panel_authorized(request):
            # The panel does not act as a buyer: a synthetic identity is enough for admin routes.
            return {"id": next(iter(admin_ids), 0), "first_name": "Хранитель", "username": ""}
        user = optional_user(request)
        if not user:
            raise ApiError("Открой лавку заново через Telegram, чтобы подтвердить вход.", 401)
        if admin and user["id"] not in admin_ids:
            raise ApiError("Эта часть лавки доступна только администратору.", 403)
        return user

    async def deliver(result):
        """Once the issue is committed, send the goods lines to buyers through the bot."""
        if notifier and result.get("issued"):
            try:
                await notifier(result["issued"])
            except Exception as error:  # The order is saved; a notice failure must not break the request.
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
                                  "images": {"menu": "/static/bg.jpg", "profile": "/static/2.jpg",
                                             "support": "/static/shop.jpg"}})

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
        return web.json_response({
            "ok": True,
            "amount": amount,
            "support_username": support_username,
            "message": f"Для пополнения на {amount} ₽ напиши администратору @{support_username}.",
        })

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
        payload = await body(request)
        # A card saved from the browser panel must not silently drop preorder/test flags.
        if request.method == "PATCH":
            return web.json_response(await store.patch_product(item_id(request), payload))
        result = await store.save_product(payload, item_id(request))
        if product_notifier and item_id(request) is None:
            try:
                await product_notifier(await store.product(result["id"]))
            except Exception as error:
                log.warning("Product announcement failed: %s", error)
        return web.json_response(result)

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

    async def deliveries_ready(request):
        authenticate(request, True)
        return web.json_response({"items": await store.delivery_items(item_id(request))})

    async def delivery_patch(request):
        authenticate(request, True)
        raw_delivery_id = request.match_info.get("delivery_id", "")
        if not raw_delivery_id.isdecimal():
            raise ApiError("Неверный номер строки.")
        payload = await body(request)
        return web.json_response(await store.replace_delivery(item_id(request), int(raw_delivery_id), payload.get("payload", "")))

    async def delivery_delete(request):
        authenticate(request, True)
        raw_delivery_id = request.match_info.get("delivery_id", "")
        if not raw_delivery_id.isdecimal():
            raise ApiError("Неверный номер строки.")
        return web.json_response(await store.delete_delivery(item_id(request), int(raw_delivery_id)))

    async def order(request):
        user = authenticate(request)
        payload = await body(request)
        if payload.get("kind") == "test" and user["id"] not in admin_ids and user["id"] not in testers:
            raise ApiError("Тестовые покупки доступны только тестерам лавки.", 403)
        await store.profile(user, user["id"] in admin_ids)
        result = await store.create_order(user["id"], payload)
        if order_notifier and payload.get("payment", "manual") == "manual" and not result.get("replayed"):
            try:
                await order_notifier(user, result)
            except Exception as error:
                log.warning("Order notice failed: %s", error)
        return web.json_response(await deliver(result))

    async def admin_users(request):
        authenticate(request, True)
        return web.json_response(await store.users_overview())

    async def admin_balance(request):
        """Пополнение баланса покупателя: после этого он платит заказ с баланса."""
        authenticate(request, True)
        payload = await body(request)
        user_id = integer(payload.get("user_id"), "ID покупателя", 2**63 - 1, 1)
        amount = integer(payload.get("amount"), "Сумма пополнения", 10**6, 1)
        balance = await store.add_balance(user_id, amount)
        return web.json_response({"ok": True, "user_id": user_id, "balance": balance})

    async def admin_reviews(request):
        authenticate(request, True)
        return web.json_response(await store.recent_reviews())

    app.add_routes([
        web.get("/api/admin/users", admin_users),
        web.post("/api/admin/balance", admin_balance),
        web.get("/api/admin/reviews", admin_reviews),
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
        web.get("/api/admin/products/{id}/deliveries/ready", deliveries_ready),
        web.patch("/api/admin/products/{id}/deliveries/{delivery_id}", delivery_patch),
        web.delete("/api/admin/products/{id}/deliveries/{delivery_id}", delivery_delete),
        web.patch("/api/admin/orders/{id}", change_order),
    ])
