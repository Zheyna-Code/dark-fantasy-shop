"""Лавка Странника: Telegram photo menu, mini app and authenticated admin API."""
import asyncio
import logging
import os
import re
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

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
PRIVACY_POLICY_URL = "https://teletype.in/@aishopditzzm/6rLg2BNAz8-"
USER_AGREEMENT_URL = "https://teletype.in/@aishopditzzm/OniyCUsM8gt"


def parse_admin_ids(value):
    parts = value.replace(" ", "").split(",")
    if any(part and (not part.isdecimal() or not 0 < int(part) < 2**63) for part in parts):
        raise ValueError("ADMIN_IDS must contain numeric Telegram user IDs separated by commas")
    return {int(part) for part in parts if part}


ADMIN_IDS = parse_admin_ids(os.environ.get("ADMIN_IDS", ""))
# Testers buy test products without payment to check automatic delivery.
TESTER_IDS = parse_admin_ids(os.environ.get("TESTER_IDS", ""))
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
    "Выбирай, куда отправиться: к товарам, в мини-лавку, к кошельку "
    "или в раздел «Прочее»."
)
SHOP_CAPTION = (
    "<b>Ты попал в Лавку Странника</b>\n\n"
    "Выбери свою магию."
)
CATEGORY_TITLES = {"chatgpt": "ChatGPT", "capcut": "CapCut", "gemini": "Gemini"}
CATEGORY_PHOTOS = {"chatgpt": "chatgptshop.jpg", "capcut": "Capcutshop.jpg", "gemini": "geminishop.jpg"}
ADD_STEPS = ("name", "price", "stock", "description")
ADD_PROMPTS = {
    "type": "Шаг 1 из 5. Это тестовый товар? Тестовый покупают тестеры без оплаты — чтобы проверить автовыдачу.",
    "name": "Шаг 2 из 5. Отправь название товара одной строкой (до 200 символов).",
    "price": "Шаг 3 из 5. Отправь цену целыми рублями (например, 590).",
    "stock": "Шаг 4 из 5. Отправь остаток целым числом (0 — товара нет в наличии; для предзаказа включается в карточке мини-лавки).",
    "description": "Шаг 5 из 5. Отправь описание товара или слово «пропустить».",
}
UPLOAD_PROMPT = (
    "<b>📤 Загрузка автовыдачи: {name}</b>\n\n"
    "Отправь одним сообщением список товара — <b>одна строка = одна единица товара</b> "
    "(логин:пароль, ключ, ссылка — до 2000 символов на строку, максимум 200 строк).\n\n"
    "Сразу после загрузки бот выдаст товар оплаченным предзаказам по очереди, "
    "а остаток выставит на полку. Тестовые покупки заберут строки без оплаты.\n\n"
    "Отмена — кнопка ниже или команда /cancel."
)
# Conversational product wizard: user_id -> {"slug", "step", "data", "message"}.
add_state = {}
# Conversational auto-delivery upload: user_id -> {"product_id", "name", "lines"}.
upload_state = {}
# Two-step delete confirmation: set of (user_id, product_id).
pending_delete = set()
# Last opened photo shelf per user, used to refresh it after add/delete.
last_shelf = {}


def blue_button(label, **action):
    return InlineKeyboardButton(text=label, style="primary", **action)


def styled_button(label, style, **action):
    return InlineKeyboardButton(text=label, style=style, **action)


def plain_button(label, **action):
    """Кнопка без цвета — например, предзаказ."""
    return InlineKeyboardButton(text=label, **action)


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
        [blue_button("💰 Кошелёк", callback_data="menu:wallet")],
        [blue_button("Прочее", callback_data="menu:more")],
    ])


def more_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("👤 Профиль", callback_data="menu:profile")],
        [blue_button("🛟 Техподдержка", callback_data="menu:support")],
        [blue_button("Политика конфиденциальности", url=PRIVACY_POLICY_URL)],
        [blue_button("Пользовательское соглашение", url=USER_AGREEMENT_URL)],
        [blue_button("В меню", callback_data="menu:home")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[blue_button("В меню", callback_data="menu:home")]])


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[styled_button("Отмена", "danger", callback_data="upload:cancel")]])


def categories_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button(title, callback_data="category:" + slug) for slug, title in CATEGORY_TITLES.items()],
        [plain_button("⏳ Предзаказы", callback_data="preorders")],
        [blue_button("В меню", callback_data="menu:home")],
    ])


async def send_photo(message, filename, caption, reply_markup):
    # Keep deployments with an older caption constant compatible: adjacent
    # strings should be a single value, but a trailing comma makes a tuple.
    if isinstance(caption, (tuple, list)):
        caption = "".join(caption)
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
        "Выбирай товар, путник: <b>зелёная</b> кнопка — товар в наличии, <b>красная</b> — кончился. "
        "Если в карточке доступен предзаказ, его можно оформить с предоплатой 100% — "
        "при поступлении бот выдаст предзаказы первыми, раньше полки.", "",
        "Перед покупкой обязательно открой карточку: там указаны цена, наличие и гарантия.",
    ]
    if not items:
        lines.append("\n🔴 Полка пуста: хранитель лавки ещё не выложил артефакты.")
    return "\n".join(lines)


def category_keyboard(user_id, slug, items, is_admin):
    rows = []
    for item in items[:12]:
        stock = item["stock"] if type(item["stock"]) is int else 0
        prefix = "🧪 " if item.get("is_test") else ""
        # Кнопка товара горит зелёным при наличии и красным, когда товара нет.
        style = "success" if stock > 0 else "danger"
        rows.append([styled_button(f"{prefix}{item['name'][:50]}", style, callback_data=f"product:{item['id']}")])
    if is_admin:
        for item in items[:20]:
            confirmed = (user_id, item["id"]) in pending_delete
            label = ("⚠️ Точно удалить: " if confirmed else "🗑 Удалить: ") + item["name"][:38]
            rows.append([blue_button(label, callback_data=f"del:{item['id']}")])
        rows.append([blue_button("➕ Добавить товар", callback_data=f"add:{slug}")])
    rows.append([styled_button("Назад", "danger", callback_data="menu:products")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_caption(item):
    stock = item["stock"] if type(item["stock"]) is int else 0
    if item.get("is_test"):
        availability = f"🧪 Тестовый товар · единиц для выдачи: {stock}"
    elif stock > 0:
        availability = f"🟢 В наличии: {stock}"
    elif stock == 0 and type(item.get("price")) is int and item["price"] > 0:
        availability = "🔴 Нет в наличии · предзаказ по предоплате 100%"
    else:
        availability = "🔴 Нет в наличии"
    description = escape(item.get("description") or "Описание уточняется у хранителя.")
    warranty = escape(item.get("warranty") or "Уточняется у хранителя перед оплатой.")
    price = f"{item['price']:,} ₽" if type(item.get("price")) is int and item["price"] > 0 else "Цена уточняется"
    note = ("\n\n🧪 Тестовый товар: тестер покупает его без оплаты, чтобы проверить автовыдачу."
            if item.get("is_test") else "")
    return (
        f"<b>{escape(item['name'])}</b>\n\n"
        f"{description}\n\n"
        f"<b>Цена:</b> {price}\n"
        f"<b>Наличие:</b> {availability}\n"
        f"<b>Гарантия:</b> {warranty}{note}\n\n"
        "Внимательно проверь условия гарантии перед оформлением заявки."
    )


def product_keyboard(item, can_test=False, is_admin=False):
    stock = item["stock"] if type(item["stock"]) is int else 0
    rows = []
    if item.get("is_test"):
        if stock > 0 and can_test:
            rows.append([styled_button("🧪 Тестовая покупка без оплаты", "success", callback_data=f"buy:{item['id']}")])
    elif stock > 0:
        rows.append([styled_button("Купить", "success", callback_data=f"buy:{item['id']}")])
    elif stock == 0 and type(item.get("price")) is int and item["price"] > 0:
        # Предзаказ — без цвета: он не покупка, а заявка на поступление.
        rows.append([plain_button("⏳ Предзаказ · предоплата 100%", callback_data=f"preorder:{item['id']}")])
    if is_admin:
        rows.append([blue_button("📤 Загрузить автовыдачу", callback_data=f"upload:{item['id']}")])
    rows.append([styled_button("Назад", "danger", callback_data=f"category:{item['category']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_category(message, user, slug):
    last_shelf[user.id] = slug
    include_test = user.id in TESTER_IDS or user.id in ADMIN_IDS
    catalog = await store.catalog(include_test=include_test)
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


def add_keyboard(step=None):
    if step == "type":
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                styled_button("🧪 Тестовый товар", "success", callback_data="addtype:test"),
                blue_button("📦 Обычный товар", callback_data="addtype:regular"),
            ],
            [styled_button("Отмена", "danger", callback_data="add:cancel")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[[styled_button("Отмена", "danger", callback_data="add:cancel")]])


async def prompt_add(message, state):
    text = add_prompt_caption(state)
    markup = add_keyboard(state["step"])
    if message.photo:
        try:
            await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception as error:
            log.warning("edit_caption failed, sending a new prompt: %s", error)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.message(Command("add"))
async def cmd_add(message: Message):
    """Быстрое добавление товара: сразу выбор категории, потом мастер."""
    user = message.from_user
    if user.id not in ADMIN_IDS:
        await message.answer("Добавлять товары может только хранитель лавки.")
        return
    add_state.pop(user.id, None)
    upload_state.pop(user.id, None)
    catalog = await store.catalog(admin=True)
    rows = [
        [blue_button(CATEGORY_TITLES.get(category["slug"], category["name"])[:50], callback_data=f"add:{category['slug']}")]
        for category in catalog["categories"] if category.get("active")
    ] or [[blue_button("ChatGPT", callback_data="add:chatgpt")], [blue_button("Gemini", callback_data="add:gemini")]]
    rows.append([styled_button("Отмена", "danger", callback_data="add:cancel")])
    await message.answer(
        "<b>➕ Добавление товара</b>\n\nШаг 1. Выбери категорию полки — "
        "потом выбери тип товара и заполни карточку.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


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
    upload_state.pop(message.from_user.id, None)
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
        "загружать товар для автовыдачи (одна строка = одна единица), "
        "отмечать оплату и выдачу заказов. Предзаказы оплачиваются на 100% вперёд: "
        "при поступлении товара бот сначала выдаёт оплаченные предзаказы по очереди, "
        "и только остаток выставляется на полку. "
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
    if step == "type":
        await message.answer("Сначала выбери тип товара кнопками выше: 🧪 тестовый или 📦 обычный.")
        return
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
        is_test = bool(state["data"].get("is_test"))
        payload = {
            **state["data"],
            "warranty": "",
            "category": state["slug"],
            "active": True,
            "is_test": is_test,
            "allow_preorder": not is_test,
        }
        try:
            saved = await store.save_product(payload)
        except ApiError as error:
            await message.answer(f"Не удалось сохранить товар: {error.message}")
            return
        kind_line = "🧪 Тестовый товар: тестеры смогут проверить автовыдачу без оплаты." if is_test else \
            "Когда появятся единицы, не забудь загрузить автовыдачу в карточке товара."
        await message.answer(
            f"<b>✅ Товар добавлен на полку</b>\n\n{escape(payload['name'])} — {payload['price']:,} ₽ · "
            f"остаток: {payload['stock']}.\n\n{kind_line}\nОткрой карточку и проверь, как она выглядит.",
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


@dp.message(F.text, ~F.text.startswith("/"), lambda message: message.from_user.id in upload_state)
async def upload_wizard(message: Message):
    user = message.from_user
    state = upload_state.get(user.id)
    if not state:
        return
    lines = [line.strip() for line in (message.text or "").splitlines()]
    lines = [line for line in lines if line]
    if not 1 <= len(lines) <= 200 or any(len(line) > 2000 for line in lines):
        await message.answer(
            "Нужно от 1 до 200 строк, каждая до 2000 символов: одна строка — одна единица товара. "
            "Отправь список заново или нажми «Отмена»."
        )
        return
    try:
        result = await store.add_deliveries(state["product_id"], {"items": lines})
    except ApiError as error:
        await message.answer(f"Не удалось загрузить автовыдачу: {error.message}")
        return
    upload_state.pop(user.id, None)
    parts = [f"<b>✅ Загружено единиц товара: {result['added']}</b>"]
    if result["delivered_to_preorders"]:
        parts.append(f"📦 Автовыдача сразу отправила товар {result['delivered_to_preorders']} оплаченным предзаказам — покупатели получили его в чат.")
    parts.append(f"🟢 Выставлено на полку: {result['stock_added']}")
    parts.append("Теперь кнопка покупки в карточке выдаёт эти строки автоматически.")
    await message.answer("\n".join(parts), parse_mode="HTML")
    if state.get("message") is not None:
        slug = last_shelf.get(user.id)
        if slug in CATEGORY_PHOTOS:
            await show_category(state["message"], user, slug)


@dp.callback_query(F.data.startswith("upload:"))
async def upload_callback(callback: CallbackQuery):
    user = callback.from_user
    value = callback.data.split(":", 1)[1]
    if value == "cancel":
        state = upload_state.pop(user.id, None)
        await callback.answer()
        if state and last_shelf.get(user.id) in CATEGORY_PHOTOS:
            slug = last_shelf[user.id]
            await show_category(callback.message, user, slug)
        return
    if user.id not in ADMIN_IDS:
        await callback.answer("Загружать автовыдачу может только хранитель лавки.", show_alert=True)
        return
    if not value.isdecimal() or len(value) > 19:
        await callback.answer()
        return
    item = await store.product(int(value))
    if not item:
        await callback.answer("Товар больше не найден.", show_alert=True)
        return
    add_state.pop(user.id, None)
    upload_state[user.id] = {"product_id": item["id"], "message": callback.message}
    await callback.answer()
    prompt = UPLOAD_PROMPT.format(name=escape(item["name"]))
    if callback.message.photo:
        try:
            await callback.message.edit_caption(caption=prompt, parse_mode="HTML", reply_markup=cancel_keyboard())
            return
        except Exception as error:
            log.warning("edit_caption failed, sending upload prompt: %s", error)
    await callback.message.answer(prompt, parse_mode="HTML", reply_markup=cancel_keyboard())


@dp.callback_query(F.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery):
    await callback.answer()
    message, user = callback.message, callback.from_user
    action = callback.data.split(":", 1)[1]
    if action in ("home", "products", "shop", "more"):
        add_state.pop(user.id, None)
        upload_state.pop(user.id, None)
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
    elif action == "more":
        await message.answer("<b>Прочее</b>", parse_mode="HTML", reply_markup=more_keyboard())
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
    upload_state.pop(callback.from_user.id, None)
    await show_category(callback.message, callback.from_user, slug)


@dp.callback_query(F.data == "preorders")
async def preorders_callback(callback: CallbackQuery):
    """Личная полка пользователя с его активными предзаказами."""
    await callback.answer()
    add_state.pop(callback.from_user.id, None)
    upload_state.pop(callback.from_user.id, None)
    personal = await store.user_preorders(callback.from_user.id)
    lines = [
        "<b>⏳ Полка предзаказов</b>", "",
        "Предзаказ действует по <b>предоплате 100%</b>: "
        "внеси полную сумму хранителю, и при поступлении бот выдаст оплаченные предзаказы "
        "первыми — раньше, чем остаток попадёт на полку.", "",
    ]
    if personal:
        lines.extend(["<b>Твои предзаказы</b>", ""])
        for order in personal[:12]:
            status = "оплачен, ждёт выдачи" if order["status"] == "paid" else "ждёт предоплату"
            names = ", ".join(str(item.get("name", "Товар")) for item in order["items"]) or "Товар"
            lines.append(f"⏳ Заказ № {order['id']} · {escape(names)} — {status}")
    personal_items = []
    for order in personal:
        for item in order["items"]:
            product_id = item.get("id")
            if not any(existing["id"] == product_id for existing in personal_items):
                personal_items.append({"id": product_id, "name": item.get("name", "Товар")})
    rows = [[styled_button(item["name"][:50], "danger", callback_data=f"product:{item['id']}")] for item in personal_items[:12]]
    if not personal:
        lines.append("У тебя пока нет оформленных предзаказов.")
    if len(personal_items) > 12:
        lines.append(f"… и ещё {len(personal_items) - 12}: список обрезан.")
    rows.append([styled_button("Назад", "danger", callback_data="menu:products")])
    await send_photo(callback.message, "shop.jpg", "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("product:"))
async def product_callback(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or len(value) > 19:
        await callback.answer()
        return
    item = await store.product(int(value))
    if not item or not item["active"]:
        await callback.answer("Товар больше не найден.", show_alert=True)
        return
    await callback.answer()
    user = callback.from_user
    can_test = user.id in TESTER_IDS or user.id in ADMIN_IDS
    markup = product_keyboard(item, can_test, user.id in ADMIN_IDS)
    if callback.message.photo:
        try:
            await callback.message.edit_caption(caption=product_caption(item), parse_mode="HTML", reply_markup=markup)
            return
        except Exception as error:
            log.warning("edit_caption failed, sending product details: %s", error)
    await callback.message.answer(product_caption(item), parse_mode="HTML", reply_markup=markup)


def issued_message(event, head):
    lines = [f"<b>{head}</b>", "", "Бот выдал покупку автоматически:", ""]
    for product_item in event.get("items", []):
        lines.append(f"<b>{escape(str(product_item.get('name', 'Товар')))}</b>")
        lines.extend(f"<code>{escape(str(payload))}</code>" for payload in product_item.get("payloads", []))
        lines.append("")
    lines.append(f"Если товар не работает — напиши в поддержку: @{SUPPORT_USERNAME}")
    return "\n".join(lines)


@dp.callback_query(F.data.startswith("buy:"))
async def buy_callback(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or len(value) > 19:
        await callback.answer()
        return
    user = callback.from_user
    item = await store.product(int(value))
    if not item or not item["active"]:
        await callback.answer("Товар больше не найден.", show_alert=True)
        return
    if item.get("is_test"):
        if user.id not in TESTER_IDS and user.id not in ADMIN_IDS:
            await callback.answer("🧪 Это тестовый товар: покупка без оплаты доступна только тестерам.", show_alert=True)
            return
        try:
            result = await store.create_test_order(user.id, item["id"])
        except ApiError as error:
            await callback.answer(error.message, show_alert=True)
            return
        await callback.message.answer(
            issued_message(result["issued"][0], f"🧪 Тестовая покупка — заказ № {result['order_id']}"),
            parse_mode="HTML",
        )
        await callback.answer("Тестовая покупка выполнена.")
        return
    await callback.answer(
        "Оплата ещё не подключена: оформи заказ в мини-лавке «🏪 Лавка Странника» "
        "или уточни условия у хранителя.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("preorder:"))
async def preorder_callback(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or len(value) > 19:
        await callback.answer()
        return
    user = callback.from_user
    item = await store.product(int(value))
    if not item or not item["active"] or item.get("is_test"):
        await callback.answer("Товар больше не найден.", show_alert=True)
        return
    if item["stock"] > 0:
        await callback.answer("Предзаказ недоступен: товар уже есть в наличии.", show_alert=True)
        return
    existing = await store.active_preorder(user.id, item["id"])
    if existing:
        await callback.answer(
            f"Предзаказ № {existing['id']} уже оформлен" + (
                " и оплачен — ждёт поступления товара." if existing["status"] == "paid"
                else ". Внеси предоплату 100% у хранителя, чтобы встать в очередь на выдачу."),
            show_alert=True,
        )
        return
    try:
        result = await store.create_order(user.id, {
            "cart": [{"id": item["id"], "qty": 1}], "kind": "preorder",
            "idempotency_key": uuid4().hex, "comment": "",
        })
    except ApiError as error:
        await callback.answer(error.message, show_alert=True)
        return
    price = f"{item['price']:,} ₽" if type(item.get("price")) is int and item["price"] > 0 else "уточняется у хранителя"
    await callback.message.answer(
        f"<b>⏳ Предзаказ № {result['order_id']} оформлен</b>\n\n"
        f"{escape(item['name'])} — {price}.\n\n"
        "Предзаказ действует по <b>предоплате 100%</b>: внеси полную сумму у хранителя лавки. "
        "Когда товар поступит, бот <b>сначала выдаст оплаченные предзаказы по очереди</b> "
        "и только потом выставит остаток на полку. Товар придёт тебе в этот чат автоматически.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [blue_button("🛟 Оплатить предоплату", url=f"https://t.me/{SUPPORT_USERNAME}")],
            [blue_button("В меню", callback_data="menu:home")],
        ]),
    )
    await callback.answer("Предзаказ оформлен.")


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
    add_state.pop(user.id, None)
    upload_state.pop(user.id, None)
    add_state[user.id] = {"slug": slug, "step": "type", "data": {}, "message": callback.message}
    await callback.answer()
    await prompt_add(callback.message, add_state[user.id])


@dp.callback_query(F.data.startswith("addtype:"))
async def addtype_callback(callback: CallbackQuery):
    """Выбор типа нового товара: тестовый или обычный."""
    user = callback.from_user
    if user.id not in ADMIN_IDS:
        await callback.answer("Добавлять товары может только хранитель лавки.", show_alert=True)
        return
    state = add_state.get(user.id)
    if not state or state["step"] != "type":
        await callback.answer()
        return
    state["data"]["is_test"] = callback.data == "addtype:test"
    state["step"] = "name"
    await callback.answer()
    await prompt_add(state["message"], state)


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


# Live bot instance for automatic delivery messages; set in main().
active_bot = {}


async def notify_deliveries(events):
    """Отправляет покупателям выданные строки товара; вызывается API после записи в базу."""
    bot = active_bot.get("bot")
    if not bot or not events:
        return
    for event in events:
        if event.get("kind") == "test":
            head = f"🧪 Тестовая покупка № {event.get('order_id')} — автовыдача сработала"
        elif event.get("kind") == "preorder":
            head = f"📦 Предзаказ № {event.get('order_id')} выполнен — товар у тебя"
        else:
            head = f"📦 Заказ № {event.get('order_id')} оплачен — товар выдан"
        try:
            await bot.send_message(event["user_id"], issued_message(event, head), parse_mode="HTML")
        except Exception as error:
            log.warning("Delivery notice to user %s failed: %s", event.get("user_id"), error)


async def make_app():
    application = web.Application(client_max_size=64 * 1024)
    register_api(application, store, BOT_TOKEN, ADMIN_IDS, SUPPORT_USERNAME, testers=TESTER_IDS, notifier=notify_deliveries)

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
                active_bot["bot"] = bot
                await bot.set_my_commands([
                    BotCommand(command="menu", description="Открыть меню лавки"),
                    BotCommand(command="admin", description="Управление лавкой"),
                    BotCommand(command="add", description="Добавить товар (для владельца)"),
                    BotCommand(command="id", description="Узнать свой Telegram ID"),
                    BotCommand(command="cancel", description="Отменить добавление товара"),
                ])
                await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
                await dp.start_polling(bot, close_bot_session=False)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
