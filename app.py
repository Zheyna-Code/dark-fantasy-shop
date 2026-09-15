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
    InlineKeyboardMarkup, InputMediaPhoto, MenuButtonCommands, Message, WebAppInfo,
)

from shop_backend import ApiError, Store, register_api

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
    "Выбирай, куда отправиться: к товарам, в мини-лавку, в свой профиль, "
    "к кошельку или за помощью к хранителю лавки."
)
SHOP_CAPTION = (
    "<b>Ты попал в Лавку Странника</b>\n\n"
    "За каменными стенами мерцают магические артефакты нового века — нейросети. "
    "В нашей лавке ты найдёшь цифровых помощников для идей, работы и творчества.\n\n"
    "Выбери свою магию: <b>ChatGPT</b> или <b>Gemini</b>."
)
CATEGORY_TITLES = {"chatgpt": "ChatGPT", "gemini": "Gemini"}
CATEGORY_PHOTOS = {"chatgpt": "chatgptshop.jpg", "gemini": "geminishop.jpg"}
ADD_STEPS = ("name", "price", "stock", "description")
ADD_PROMPTS = {
    "name": "Шаг 1 из 4. Отправь название товара одной строкой (до 200 символов).",
    "price": "Шаг 2 из 4. Отправь цену целыми рублями (например, 590).",
    "stock": "Шаг 3 из 4. Отправь остаток целым числом (0 — товара нет в наличии).",
    "description": "Шаг 4 из 4. Отправь описание товара или слово «пропустить».",
}
# Conversational product wizard: user_id -> {"slug", "step", "data", "message"}.
add_state = {}
# Two-step delete confirmation: set of (user_id, product_id).
pending_delete = set()
# Last opened photo shelf per user, used to refresh it after add/delete.
last_shelf = {}


def blue_button(label, **action):
    return InlineKeyboardButton(text=label, style="primary", **action)


def webapp_url(**params):
    url = urlsplit(WEBAPP_URL)
    if url.scheme != "https" or not url.netloc or url.username or url.password:
        return None
    query = dict(parse_qsl(url.query))
    query.update(params)
    return urlunsplit((url.scheme, url.netloc, url.path or "/", urlencode(query), ""))


def menu_keyboard():
    url = webapp_url()
    shop_action = {"web_app": WebAppInfo(url=url)} if url else {"callback_data": "menu:shop"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("🛒 Товары", callback_data="menu:products")],
        [blue_button("🏪 Лавка Странника", **shop_action)],
        [blue_button("👤 Профиль", callback_data="menu:profile")],
        [blue_button("💰 Кошелёк", callback_data="menu:wallet")],
        [blue_button("🛟 Техподдержка", callback_data="menu:support")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[blue_button("В меню", callback_data="menu:home")]])


def categories_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button(title, callback_data="category:" + slug) for slug, title in CATEGORY_TITLES.items()],
        [blue_button("В меню", callback_data="menu:home")],
    ])


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


def category_items(slug, catalog):
    return [item for item in catalog["products"] if item["category"] == slug]


def category_caption(slug, items):
    lines = [
        f"<b>Полка {CATEGORY_TITLES[slug]}</b>", "",
        "Выбирай товар, путник. Если артефакт закончился, можно оформить предзаказ — "
        "хранитель сообщит срок поступления. Перед покупкой обязательно открой карточку: "
        "там указаны цена, наличие и гарантия на товар.", "",
    ]
    shown = items[:12]
    if not shown:
        lines.append("🔴 Полка пуста: хранитель лавки ещё не выложил артефакты.")
    for item in shown:
        stock = item["stock"] if type(item["stock"]) is int else 0
        mark = "🟢" if stock > 0 else "🔴"
        state = f"в наличии: {stock}" if stock > 0 else "нет в наличии"
        lines.append(f"{mark} <b>{escape(item['name'])}</b> — {state}; открой карточку кнопкой ниже")
    if len(items) > len(shown):
        lines.append(f"… и ещё {len(items) - len(shown)}: смотри мини-лавку.")
    lines += ["", "Нажми на название товара ниже, чтобы открыть подробности."]
    return "\n".join(lines)


def category_keyboard(user_id, slug, items, is_admin):
    rows = []
    for item in items[:12]:
        stock = item["stock"] if type(item["stock"]) is int else 0
        mark = "🟢" if stock > 0 else "🔴"
        rows.append([blue_button(f"{mark} {item['name'][:50]}", callback_data=f"product:{item['id']}")])
    if is_admin:
        for item in items[:20]:
            confirmed = (user_id, item["id"]) in pending_delete
            label = ("⚠️ Точно удалить: " if confirmed else "🗑 Удалить: ") + item["name"][:38]
            rows.append([blue_button(label, callback_data=f"del:{item['id']}")])
        rows.append([blue_button("➕ Добавить товар", callback_data=f"add:{slug}")])
    rows.append([blue_button("Назад", callback_data="menu:products")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_caption(item):
    stock = item["stock"] if type(item["stock"]) is int else 0
    if stock > 0:
        availability = f"🟢 В наличии: {stock}"
    elif item.get("allow_preorder"):
        availability = "🔴 Нет в наличии · доступен предзаказ"
    else:
        availability = "🔴 Нет в наличии"
    description = escape(item.get("description") or "Описание уточняется у хранителя.")
    warranty = escape(item.get("warranty") or "Уточняется у хранителя перед оплатой.")
    price = f"{item['price']:,} ₽" if type(item.get("price")) is int and item["price"] > 0 else "Цена уточняется"
    return (
        f"<b>{escape(item['name'])}</b>\n\n"
        f"{description}\n\n"
        f"<b>Цена:</b> {price}\n"
        f"<b>Наличие:</b> {availability}\n"
        f"<b>Гарантия:</b> {warranty}\n\n"
        "Внимательно проверь условия гарантии перед оформлением заявки."
    )


def product_keyboard(item):
    rows = [[blue_button("Купить", callback_data=f"buy:{item['id']}")]]
    rows.append([blue_button("Назад", callback_data=f"category:{item['category']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_category(message, user, slug):
    last_shelf[user.id] = slug
    catalog = await store.catalog()
    items = category_items(slug, catalog)
    caption = category_caption(slug, items)
    markup = category_keyboard(user.id, slug, items, user.id in ADMIN_IDS)
    path = BASE_DIR / "webapp" / CATEGORY_PHOTOS[slug]
    if message.photo:
        try:
            await message.edit_media(
                media=InputMediaPhoto(media=FSInputFile(path), caption=caption, parse_mode="HTML"),
                reply_markup=markup,
            )
            return
        except Exception as error:
            log.warning("edit_media failed, sending a new photo: %s", error)
    await send_photo(message, CATEGORY_PHOTOS[slug], caption, markup)


def add_prompt_caption(state):
    title = CATEGORY_TITLES[state["slug"]]
    return f"<b>➕ Новый товар {title}</b>\n\n{ADD_PROMPTS[state['step']]}\n\nОтмена — кнопка ниже или команда /cancel."


def add_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[blue_button("Отмена", callback_data="add:cancel")]])


async def prompt_add(message, state):
    if message.photo:
        try:
            await message.edit_caption(caption=add_prompt_caption(state), parse_mode="HTML", reply_markup=add_keyboard())
            return
        except Exception as error:
            log.warning("edit_caption failed, sending a new prompt: %s", error)
    await message.answer(add_prompt_caption(state), parse_mode="HTML", reply_markup=add_keyboard())


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await send_menu(message, message.from_user)


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    state = add_state.pop(message.from_user.id, None)
    if not state:
        await message.answer("Активных действий нет. Продолжай путь, путник.")
        return
    await message.answer("Добавление товара отменено.")
    if state.get("message") is not None and state["slug"] in CATEGORY_PHOTOS:
        await show_category(state["message"], message.from_user, state["slug"])


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
        "Здесь можно добавлять товары, менять цены и остатки, удалять карточки, "
        "а также отмечать оплату и выдачу заказов. "
        "Нажми кнопку ниже. Доступ проверяется по твоему Telegram ID на сервере.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            blue_button("Открыть админку", web_app=WebAppInfo(url=url))
        ]]),
    )


@dp.message(F.text, ~F.text.startswith("/"), lambda message: message.from_user.id in add_state)
async def add_wizard(message: Message):
    user = message.from_user
    state = add_state.get(user.id)
    if not state:
        return
    value = (message.text or "").strip()
    step = state["step"]
    if step == "name":
        if not value or len(value) > 200:
            await message.answer("Название: от 1 до 200 символов. Повтори ввод.")
            return
        state["data"]["name"] = value
    elif step == "price":
        if not value.isdecimal() or int(value) > 2**31:
            await message.answer("Цена: целое неотрицательное число рублей. Повтори ввод.")
            return
        state["data"]["price"] = int(value)
    elif step == "stock":
        if not value.isdecimal() or int(value) > 10**6:
            await message.answer("Остаток: целое неотрицательное число. Повтори ввод.")
            return
        state["data"]["stock"] = int(value)
    else:
        state["data"]["description"] = "" if value.lower() in ("пропустить", "-", "без описания") else value[:4000]
        add_state.pop(user.id, None)
        payload = {
            **state["data"],
            "warranty": "",
            "category": state["slug"],
            "active": True,
            "allow_preorder": True,
        }
        try:
            saved = await store.save_product(payload)
        except ApiError as error:
            await message.answer(f"Не удалось сохранить товар: {error.message}")
            return
        await message.answer(
            f"<b>✅ Товар добавлен на полку</b>\n\n{escape(payload['name'])} — {payload['price']:,} ₽ · "
            f"остаток: {payload['stock']}.\n\nНажми кнопку, чтобы открыть карточку товара и проверить гарантию.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                blue_button("Открыть карточку товара", callback_data=f"product:{saved['id']}")
            ]]),
        )
        if state.get("message") is not None:
            await show_category(state["message"], user, state["slug"])
        return
    state["step"] = ADD_STEPS[ADD_STEPS.index(step) + 1]
    await prompt_add(state["message"], state)


@dp.callback_query(F.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery):
    await callback.answer()
    message, user = callback.message, callback.from_user
    action = callback.data.split(":", 1)[1]
    if action in ("home", "products", "shop"):
        add_state.pop(user.id, None)
    if action == "home":
        await send_menu(message, user)
    elif action == "shop":
        url = webapp_url()
        if url:
            await message.answer(
                "Мини-лавка открывается кнопкой «Лавка Странника» в меню.", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [blue_button("🏪 Лавка Странника", web_app=WebAppInfo(url=url))],
                    [blue_button("В меню", callback_data="menu:home")],
                ]),
            )
        else:
            await send_photo(message, "shop.jpg", SHOP_CAPTION, categories_keyboard())
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
    """Photo shelf: picture, traveler caption, stock list and admin tools."""
    await callback.answer()
    slug = callback.data.split(":", 1)[1]
    if slug not in CATEGORY_PHOTOS:
        return
    add_state.pop(callback.from_user.id, None)
    await show_category(callback.message, callback.from_user, slug)


@dp.callback_query(F.data.startswith("product:"))
async def product_callback(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or len(value) > 19:
        await callback.answer()
        return
    item = next((product for product in (await store.catalog())["products"] if product["id"] == int(value)), None)
    if not item:
        await callback.answer("Товар больше не найден.", show_alert=True)
        return
    await callback.answer()
    markup = product_keyboard(item)
    if callback.message.photo:
        try:
            await callback.message.edit_caption(caption=product_caption(item), parse_mode="HTML", reply_markup=markup)
            return
        except Exception as error:
            log.warning("edit_caption failed, sending product details: %s", error)
    await callback.message.answer(product_caption(item), parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data.startswith("buy:"))
async def buy_callback(callback: CallbackQuery):
    await callback.answer(
        "Покупка пока не подключена. Оформи предзаказ или уточни условия у хранителя лавки.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("add:"))
async def add_callback(callback: CallbackQuery):
    user = callback.from_user
    slug = callback.data.split(":", 1)[1]
    if slug == "cancel":
        state = add_state.pop(user.id, None)
        await callback.answer()
        if state and state["slug"] in CATEGORY_PHOTOS and state.get("message") is not None:
            await show_category(state["message"], user, state["slug"])
        return
    if user.id not in ADMIN_IDS:
        await callback.answer("Добавлять товары может только хранитель лавки.", show_alert=True)
        return
    if slug not in CATEGORY_PHOTOS:
        return
    add_state[user.id] = {"slug": slug, "step": ADD_STEPS[0], "data": {}, "message": callback.message}
    await callback.answer()
    await prompt_add(callback.message, add_state[user.id])


@dp.callback_query(F.data.startswith("del:"))
async def delete_callback(callback: CallbackQuery):
    user = callback.from_user
    if user.id not in ADMIN_IDS:
        await callback.answer("Удалять товары может только хранитель лавки.", show_alert=True)
        return
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or len(value) > 19:
        return
    product_id = int(value)
    key = (user.id, product_id)
    if key not in pending_delete:
        for other in [item for item in pending_delete if item[0] == user.id]:
            pending_delete.discard(other)
        pending_delete.add(key)
        await callback.answer("Нажми ещё раз, чтобы подтвердить удаление.")
    else:
        pending_delete.discard(key)
        try:
            await store.delete_product(product_id)
            await callback.answer("Товар удалён с полки.")
        except ApiError as error:
            await callback.answer(error.message, show_alert=True)
    slug = last_shelf.get(user.id)
    if slug in CATEGORY_PHOTOS:
        await show_category(callback.message, user, slug)


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
    # Container platforms route traffic to the service through its network
    # interface; binding only to localhost commonly results in 502 Bad Gateway.
    host = os.environ.get("HOST", "0.0.0.0")
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
                    BotCommand(command="cancel", description="Отменить добавление товара"),
                ])
                await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
                await dp.start_polling(bot, close_bot_session=False)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
