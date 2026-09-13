"""
Лавка Странника — Telegram Dark Fantasy Shop
Бот + Web App API (aiogram 3 + aiohttp + SQLite)
"""
import asyncio
import json
import logging
import os
import time
import hashlib
import hmac
from urllib.parse import parse_qsl

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "http://localhost:8080")  # в проде: https URL
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lavka")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- База данных ----------

INIT_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    price INTEGER NOT NULL,          -- цена в золотых монетах
    category TEXT DEFAULT 'relics',
    emoji TEXT DEFAULT '🕯️',
    stock INTEGER DEFAULT 99
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    items TEXT NOT NULL,             -- JSON [{id, name, price, qty}]
    total INTEGER NOT NULL,
    comment TEXT DEFAULT '',
    status TEXT DEFAULT 'new',       -- new / paid / done
    created_at INTEGER NOT NULL
);
"""

SEED_PRODUCTS = [
    ("Свеча «Шёпот Таверны»", "Воск, тёплый янтарный свет, аромат дыма и мёда. Горит 40 часов.", 120, "candles", "🕯️", 20),
    ("Свеча «Северный Дозор»", "Чёрный воск, запах хвои и холодного камня.", 150, "candles", "🕯️", 15),
    ("Эликсир Бодрости", "Зелье алхимика: кофеин, женьшень и капля безумия. 250 мл.", 90, "potions", "⚗️", 30),
    ("Эликсир Спокойного Сна", "Лаванда и валериана. Выпей — и даже виверна не разбудит.", 110, "potions", "🧪", 25),
    ("Свиток Древнего Знания", "Пергамент ручной работы с гравировкой. Для записей и заклинаний.", 200, "relics", "📜", 10),
    ("Кулон «Око Ворона»", "Обсидиан на кожаном шнурке. Говорят, приносит везение.", 340, "relics", "🖤", 8),
    ("Травяной сбор «Лесная Ведьма»", "Чабрец, шиповник, мята. Завари на ночь.", 70, "potions", "🌿", 40),
    ("Кубок Путника", "Деревянный кубок с резьбой. Для мёда, вина и историй у костра.", 260, "relics", "🏆", 12),
]

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(INIT_SQL)
        cur = await db.execute("SELECT COUNT(*) FROM products")
        (count,) = await cur.fetchone()
        if count == 0:
            await db.executemany(
                "INSERT INTO products (name, description, price, category, emoji, stock) VALUES (?,?,?,?,?,?)",
                SEED_PRODUCTS,
            )
            log.info("Каталог засеян: %d товаров", len(SEED_PRODUCTS))
        await db.commit()

# ---------- Проверка подписи Telegram Web App ----------

def check_init_data(init_data: str) -> dict | None:
    """Верификация initData по алгоритму Telegram (HMAC-SHA256)."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    recv_hash = pairs.pop("hash", None)
    if not recv_hash:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    user = json.loads(pairs.get("user", "{}"))
    return {"user_id": user.get("id"), "user": user}

# ---------- HTTP API для Web App ----------

async def handle_products(request: web.Request) -> web.Response:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE stock > 0 ORDER BY category, price")
        rows = await cur.fetchall()
    products = [dict(r) for r in rows]
    return web.json_response(products, headers={"Access-Control-Allow-Origin": "*"})

async def handle_order(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    auth = check_init_data(body.get("initData", ""))
    if not auth or not auth.get("user_id"):
        return web.json_response({"ok": False, "error": "auth failed"}, status=403)

    cart = body.get("cart", [])
    if not cart:
        return web.json_response({"ok": False, "error": "cart empty"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        items, total = [], 0
        for entry in cart:
            cur = await db.execute("SELECT * FROM products WHERE id = ? AND stock > 0", (entry["id"],))
            row = await cur.fetchone()
            if not row:
                continue
            qty = max(1, min(int(entry.get("qty", 1)), row["stock"]))
            items.append({"id": row["id"], "name": row["name"], "price": row["price"], "qty": qty})
            total += row["price"] * qty
            await db.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (qty, row["id"]))
        if not items:
            return web.json_response({"ok": False, "error": "items unavailable"}, status=400)
        cur = await db.execute(
            "INSERT INTO orders (user_id, items, total, comment, created_at) VALUES (?,?,?,?,?)",
            (auth["user_id"], json.dumps(items, ensure_ascii=False), total, body.get("comment", "")[:300], int(time.time())),
        )
        await db.commit()
        order_id = cur.lastrowid

    # Уведомление пользователю
    lines = "\n".join(f"  {i['qty']}× {i['name']} — {i['price']} 🪙" for i in items)
    text = (
        f"🕯️ <b>Заказ №{order_id} принят в Лавке Странника</b>\n\n"
        f"{lines}\n\n"
        f"Итог: <b>{total} золотых</b>\n"
        f"Хранитель лавки свяжется с тобой в ближайшее время. Жди у очага. 🖤"
    )
    try:
        await bot.send_message(auth["user_id"], text, parse_mode="HTML")
    except Exception as e:
        log.warning("Не удалось отправить уведомление: %s", e)

    return web.json_response({"ok": True, "order_id": order_id, "total": total})

# ---------- Статика Web App ----------

async def handle_index(request: web.Request) -> web.Response:
    here = os.path.dirname(os.path.abspath(__file__))
    return web.FileResponse(os.path.join(here, "webapp", "index.html"))

async def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_static("/static", os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp"))
    app.router.add_get("/api/products", handle_products)
    app.router.add_post("/api/order", handle_order)
    app.router.add_route("OPTIONS", "/api/order", lambda r: web.Response(headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
    }))
    return app

# ---------- Бот ----------

@dp.message(CommandStart())
async def cmd_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🕯️ Войти в Лавку", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await m.answer(
        "🖤 <b>Добро пожаловать в Лавку Странника</b>\n\n"
        "Ты переступил порог, где мерцают свечи, а полки хранят "
        "зелья, свитки и реликвии из дальних земель.\n\n"
        "Присаживайся у очага. Нажми кнопку ниже — и лавка откроется.",
        parse_mode="HTML",
        reply_markup=kb,
    )

# ---------- Запуск ----------

async def main():
    await init_db()
    app = await make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
    await site.start()
    log.info("Web App слушает на порту %s", os.environ.get("PORT", 8080))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
