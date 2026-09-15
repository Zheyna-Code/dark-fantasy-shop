"""Лавка Странника: Telegram photo menu, mini app and authenticated admin API."""
import asyncio
import logging
import os
import re
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, MenuButtonCommands, Message, WebAppInfo,
)

from shop_backend import Store, register_api

BASE_DIR = Path(__file__).resolve().parent
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "shop.db"))
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "DitzzmBack").lstrip("@")
if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", SUPPORT_USERNAME):
    raise ValueError("SUPPORT_USERNAME must be a Telegram username without a URL")


def parse_admin_ids(value):
    parts = value.replace(" ", "").split(",")
    if any(part and (not part.isdecimal() or not 0 < int(part) < 2**63) for part in parts):
        raise ValueError("ADMIN_IDS must contain numeric Telegram user IDs separated by commas")
    return {int(part) for part in parts if part}


ADMIN_IDS = parse_admin_ids(os.environ.get("ADMIN_IDS", ""))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lavka")
store = Store(DB_PATH)
dp = Dispatcher()
# Web App buttons and personal information must only appear in private chats.
dp.message.filter(F.chat.type == "private")
dp.callback_query.filter(F.message.chat.type == "private")

MENU_CAPTION = (
    "<b>Добро пожаловать в Лавку Странника!</b>\n\n"
    "Спасибо, что пользуешься нашей лавкой, путник. "
    "Отдохни у старого дуба: здесь начинается твой путь в мир нейросетей.\n\n"
    "Выбирай, куда отправиться: к товарам, в свой профиль, к кошельку "
    "или за помощью к хранителю лавки."
)
SHOP_CAPTION = (
    "<b>Ты попал в Лавку Странника</b>\n\n"
    "За каменными стенами мерцают магические артефакты нового века — нейросети. "
    "В нашей лавке ты найдёшь цифровых помощников для идей, работы и творчества.\n\n"
    "Выбери свою магию: <b>ChatGPT</b> или <b>Gemini</b>."
)


def blue_button(label, **action):
    return InlineKeyboardButton(text=label, style="primary", **action)


def menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("🛒 Товары", callback_data="menu:products")],
        [blue_button("👤 Профиль", callback_data="menu:profile")],
        [blue_button("💰 Кошелёк", callback_data="menu:wallet")],
        [blue_button("🛟 Техподдержка", callback_data="menu:support")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[blue_button("В меню", callback_data="menu:home")]])


def webapp_url(**params):
    url = urlsplit(WEBAPP_URL)
    if url.scheme != "https" or not url.netloc or url.username or url.password:
        return None
    query = dict(parse_qsl(url.query))
    query.update(params)
    return urlunsplit((url.scheme, url.netloc, url.path or "/", urlencode(query), ""))


def categories_keyboard():
    buttons = []
    for slug, title in (("chatgpt", "ChatGPT"), ("gemini", "Gemini")):
        url = webapp_url(category=slug)
        action = {"web_app": WebAppInfo(url=url)} if url else {"callback_data": "category:" + slug}
        buttons.append(blue_button(title, **action))
    return InlineKeyboardMarkup(inline_keyboard=[buttons, [blue_button("В меню", callback_data="menu:home")]])


async def send_photo(message, filename, caption, reply_markup):
    path = BASE_DIR / "webapp" / filename
    if path.is_file():
        await message.answer_photo(FSInputFile(path), caption=caption, parse_mode="HTML", reply_markup=reply_markup)
    else:
        # Keep navigation usable if a deployment accidentally omits a photo.
        log.error("Missing bot photo: %s", filename)
        await message.answer(caption, parse_mode="HTML", reply_markup=reply_markup)


async def send_menu(message, user):
    await store.profile(user.model_dump(), user.id in ADMIN_IDS)
    await send_photo(message, "1.jpg", MENU_CAPTION, menu_keyboard())


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await send_menu(message, message.from_user)


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer(
            "У тебя пока нет доступа к управлению лавкой.\n\n"
            f"Твой Telegram ID: <code>{user_id}</code>\n"
            "Если ты владелец бота, добавь этот ID в ADMIN_IDS в переменных окружения "
            "на хостинге, перезапусти приложение и снова отправь /admin. "
            "Для нескольких администраторов перечисли ID через запятую.\n\n"
            "Не отправляй токен бота в чат и не добавляй его в GitHub.", parse_mode="HTML",
        )
        return
    url = webapp_url(view="admin")
    if not url:
        await message.answer(
            "Права администратора подтверждены. Для открытия панели укажи WEBAPP_URL — "
            "публичный HTTPS-адрес этого приложения на хостинге — и перезапусти приложение. "
            "Адрес GitHub-репозитория или GitHub Pages не подходит: нужен запущенный Python-сервер."
        )
        return
    await message.answer(
        "<b>Управление Лавкой Странника</b>\n\n"
        "Здесь можно добавлять товары, менять цены и остатки, а также отмечать оплату и выдачу заказов. "
        "Нажми кнопку ниже. Доступ проверяется по твоему Telegram ID на сервере.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            blue_button("Открыть админку", web_app=WebAppInfo(url=url))
        ]]),
    )


@dp.callback_query(F.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery):
    await callback.answer()
    message, user = callback.message, callback.from_user
    action = callback.data.split(":", 1)[1]
    if action == "home":
        await send_menu(message, user)
    elif action == "products":
        await store.profile(user.model_dump(), user.id in ADMIN_IDS)
        await send_photo(message, "shop.jpg", SHOP_CAPTION, categories_keyboard())
    elif action == "profile":
        profile = await store.profile(user.model_dump(), user.id in ADMIN_IDS)
        caption = (
            "<b>Привет, путник!</b>\n\n"
            "Вот что хранит летопись твоих странствий:\n\n"
            f"Куплено товаров: <b>{profile['purchases']}</b>\n"
            f"Потрачено: <b>{profile['spent']:,} ₽</b>\n"
            f"Твой номер в лавке: <b>№{profile['traveler_no']}</b>\n"
            f"Любимый товар: <b>{escape(profile['favorite_product'] or 'Пока нет покупок')}</b>\n\n"
            "В летопись попадают только оплаченные покупки."
        )
        await send_photo(message, "2.jpg", caption, back_keyboard())
    elif action == "wallet":
        profile = await store.profile(user.model_dump(), user.id in ADMIN_IDS)
        await message.answer(
            f"<b>Кошелёк странника</b>\n\nБаланс: <b>{profile['balance']:,} ₽</b>\n\n"
            "Пополнение пока не подключено. Оплату и получение товара согласуй с поддержкой; "
            "нажатие кнопок не списывает деньги.", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [blue_button("🛟 Техподдержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
                [blue_button("В меню", callback_data="menu:home")],
            ]),
        )
    elif action == "support":
        await message.answer(
            "<b>Хранитель лавки на связи</b>\n\n"
            "Нужна помощь с выбором нейросети, оплатой или покупкой? "
            "Напиши нам — поможем найти верный путь.", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [blue_button("🛟 Написать в поддержку", url=f"https://t.me/{SUPPORT_USERNAME}")],
                [blue_button("В меню", callback_data="menu:home")],
            ]),
        )


@dp.callback_query(F.data.startswith("category:"))
async def category_callback(callback: CallbackQuery):
    """Usable fallback when the shop's public HTTPS URL is not yet configured."""
    await callback.answer()
    slug = callback.data.split(":", 1)[1]
    if slug not in ("chatgpt", "gemini"):
        return
    title = "ChatGPT" if slug == "chatgpt" else "Gemini"
    catalog = await store.catalog()
    items = [item for item in catalog["products"] if item["category"] == slug]
    lines = [f"<b>Товары {title}</b>"]
    if not items:
        lines.append("Полка пока пуста. Новые товары появятся после добавления продавцом.")
    else:
        for item in items[:10]:
            lines.append(f"{escape(item['name'])} — {item['price']} ₽ · в наличии: {item['stock']}")
        if len(items) > 10:
            lines.append("Остальные варианты уточни у поддержки.")
    lines.append("Для покупки или уточнения наличия напиши хранителю лавки.")
    await callback.message.answer("\n\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("🛟 Техподдержка", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [blue_button("К категориям", callback_data="menu:products")],
    ]))


async def make_app():
    application = web.Application(client_max_size=64 * 1024)
    register_api(application, store, BOT_TOKEN, ADMIN_IDS, SUPPORT_USERNAME)

    async def index(request):
        return web.FileResponse(BASE_DIR / "webapp" / "index.html", headers={"Cache-Control": "no-cache"})

    async def health(request):
        return web.json_response({"ok": True})

    application.router.add_get("/", index)
    application.router.add_get("/health", health)
    application.router.add_static("/static", BASE_DIR / "webapp", show_index=False)
    return application


async def main():
    web_only = os.environ.get("WEB_ONLY", "").lower() in ("1", "true", "yes")
    if not web_only and not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN on the hosting service. For a local catalog preview use WEB_ONLY=1.")
    await store.init_db()
    runner = web.AppRunner(await make_app())
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "127.0.0.1")
    try:
        await web.TCPSite(runner, host, port).start()
        log.info("Shop listening at %s:%s", host, port)
        if web_only:
            await asyncio.Event().wait()
        else:
            async with Bot(BOT_TOKEN) as bot:
                await bot.set_my_commands([
                    BotCommand(command="menu", description="Открыть меню лавки"),
                    BotCommand(command="admin", description="Управление лавкой"),
                    BotCommand(command="id", description="Узнать свой Telegram ID"),
                ])
                await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
                await dp.start_polling(bot, close_bot_session=False)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
