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
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, MenuButtonCommands, Message, WebAppInfo,
)

from shop_backend import ApiError, Store, register_api

BASE_DIR = Path(__file__).resolve().parent
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "DitzmBack").lstrip("@")
if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", SUPPORT_USERNAME):
    raise ValueError("SUPPORT_USERNAME must be a Telegram username without a URL")
# Optional group/channel where completed reviews are announced. Telegram chat IDs
# are usually negative (for example -1001234567890); a public @username also works.
REVIEWS_GROUP_CHAT_ID = os.environ.get("REVIEWS_GROUP_CHAT_ID", "").strip()
# Channel where newly created and restocked products are announced.
PRODUCTS_CHANNEL_CHAT_ID = os.environ.get("PRODUCTS_CHANNEL_CHAT_ID", "@Ditzzm1337").strip()
REQUIRED_CHANNEL = "Ditzzm1337"
REQUIRED_CHANNEL_URL = "https://t.me/Ditzzm1337"
PRIVACY_POLICY_URL = "https://teletype.in/@aishopditzzm/6rLg2BNAz8-"
USER_AGREEMENT_URL = "https://teletype.in/@aishopditzzm/OniyCUsM8gt"
WARRANTY_TERMS_URL = "https://teletype.in/@aishopditzzm/Ml2mgNp0KFk"
BONUS_PERCENT = 3


def parse_admin_ids(value):
    parts = value.replace(" ", "").split(",")
    if any(part and (not part.isdecimal() or not 0 < int(part) < 2**63) for part in parts):
        raise ValueError("ADMIN_IDS must contain numeric Telegram user IDs separated by commas")
    return {int(part) for part in parts if part}


ADMIN_IDS = parse_admin_ids(os.environ.get("ADMIN_IDS", ""))
# Token for the full-size admin panel in a normal browser (on the site), not inside Telegram.
ADMIN_PANEL_TOKEN = os.environ.get("ADMIN_PANEL_TOKEN", "").strip()
# Testers buy test products without payment to check automatic delivery.
TESTER_IDS = parse_admin_ids(os.environ.get("TESTER_IDS", ""))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lavka")
store = Store()
dp = Dispatcher()
# Web App buttons and personal information must only appear in private chats.
dp.message.filter(F.chat.type == "private")
dp.callback_query.filter(F.message.chat.type == "private")

MENU_CAPTION = (
    "<b>Добро пожаловать в Лавку Странника!</b>\n\n"
    "Спасибо, что пользуешься нашей лавкой, путник. "
    "Отдохни у старого дуба: здесь начинается твой путь в мир нейросетей.\n\n"
    "Выбирай, куда отправиться."
)
SHOP_CAPTION = (
    "<b>🏪 Ты попал в Лавку Странника</b>\n\n"
    "Выбери товар или категорию ниже — всё оформим прямо в боте."
)
CATEGORY_PHOTOS = {"chatgpt": "chatgptshop.jpg", "capcut": "Capcutshop.jpg", "gemini": "geminishop.jpg"}
CATEGORY_TITLES = {"chatgpt": "ChatGPT", "capcut": "CapCut", "gemini": "Gemini"}
ADD_STEPS = ("name", "price", "stock", "description")
ADD_PROMPTS = {
    "type": "Шаг 1 из 5. Это тестовый товар? Тестовый покупают тестеры без оплаты — чтобы проверить автовыдачу.",
    "name": "Шаг 2 из 5. Отправь название товара одной строкой (до 200 символов).",
    "price": "Шаг 3 из 5. Отправь цену целыми рублями (например, 590).",
    "stock": "Шаг 4 из 5. Отправь остаток целым числом (0 — товара нет в наличии; тогда работает предзаказ).",
    "description": "Шаг 5 из 5. Отправь описание товара или слово «пропустить».",
}
EDIT_PROMPTS = {
    "name": "Отправь новое название товара одной строкой (до 200 символов).",
    "price": "Отправь новую цену целыми рублями (например, 590).",
    "stock": "Отправь новый остаток целым числом (0 — товара нет в наличии).",
    "description": "Отправь новое описание товара или слово «пропустить».",
}
FIELD_TITLES = {"name": "название", "price": "цену", "stock": "остаток", "description": "описание",
                "category": "категорию", "active": "показ на полке", "allow_preorder": "предзаказ",
                "is_test": "тестовый товар"}
UPLOAD_PROMPT = (
    "<b> Загрузка автовыдачи: {name}</b>\n\n"
    "Отправь одним сообщением список товара — <b>одна строка = одна единица товара</b> "
    "(логин:пароль, ключ, ссылка — до 2000 символов на строку, максимум 200 строк).\n\n"
    "Сразу после загрузки бот выдаст товар оплаченным предзаказам по очереди, "
    "а остаток выставит на полку и разошлёт уведомление покупателям."
)
# Conversational product wizard: user_id -> {"slug", "step", "data", "message"}.
add_state = {}
# Field editor: user_id -> {"product_id", "field", "message"}.
edit_state = {}
# Conversational auto-delivery upload: user_id -> {"product_id", "name", "message"}.
upload_state = {}
# Editing one ready auto-delivery line: user_id -> {product_id, delivery_id, message}.
delivery_edit_state = {}
# Waiting for an optional review comment: user_id -> {"order_id", "product_id"}.
review_state = {}
# Two-step delete confirmation: set of (user_id, product_id).
pending_delete = set()
# Last opened photo shelf per user, used to refresh it after add/delete.
last_shelf = {}
# Telegram file ids of our own photos: uploading a 3 MB jpg on every screen is
# the slowest part of the bot, so each picture is uploaded only once.
photo_cache = {}
# Live bot instance for background messages; set in main().
active_bot = {}
# Keep strong references to background broadcasts so they are never collected.
broadcast_tasks = set()


def blue_button(label, **action):
    return InlineKeyboardButton(text=label, style="primary", **action)


def styled_button(label, style, **action):
    return InlineKeyboardButton(text=label, style=style, **action)


def plain_button(label, **action):
    """Кнопка без цвета — например, предзаказ или оценка."""
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
        [blue_button("🎁 Бонус", callback_data="menu:bonus")],
        [blue_button("👤 Профиль", callback_data="menu:profile")],
        [blue_button("💰 Кошелёк", callback_data="menu:wallet")],
        [blue_button("⭐ Отзывы", url="https://t.me/otzivditzzm")],
        [blue_button("🛟 Техподдержка", callback_data="menu:support")],
    ])


def subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("📜 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
        [blue_button("✅ Проверить подписку", callback_data="subscription:check")],
    ])


async def is_subscribed(user_id):
    """Telegram confirms channel membership; admins keep access for maintenance."""
    if user_id in ADMIN_IDS:
        return True
    bot = active_bot.get("bot")
    if not bot:
        return False
    try:
        member = await bot.get_chat_member(f"@{REQUIRED_CHANNEL}", user_id)
        return member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and bool(getattr(member, "is_member", False))
        )
    except Exception as error:
        log.warning("Subscription check failed for %s: %s", user_id, error)
        return False


async def send_subscription_gate(target):
    caption = (
        "<b>Добро пожаловать, странник.</b>\n\n"
        "Перед входом в Лавку Странника подпишись на наш канал.\n"
        "После подписки нажми «Проверить подписку» — и двери лавки откроются."
    )
    await send_photo(target, "1.jpg", caption, subscription_keyboard())


class SubscriptionMiddleware(BaseMiddleware):
    """Re-check membership before every inline-button action."""
    async def __call__(self, handler, event, data):
        if getattr(event, "data", "") == "subscription:check":
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        if user and await is_subscribed(user.id):
            return await handler(event, data)
        await event.answer("Сначала подпишись на канал.", show_alert=True)
        if getattr(event, "message", None):
            await send_subscription_gate(event.message)
        return None


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[blue_button("В меню", callback_data="menu:home")]])


def support_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("🛟 Написать в поддержку", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [blue_button("🔒 Политика конфиденциальности", url=PRIVACY_POLICY_URL)],
        [blue_button("📜 Пользовательское соглашение", url=USER_AGREEMENT_URL)],
        [blue_button("🛡 Условия гарантии", url=WARRANTY_TERMS_URL)],
        [blue_button("В меню", callback_data="menu:home")],
    ])


def profile_keyboard(has_preorders=False):
    label = f"⏳ Предзаказы · {has_preorders}" if has_preorders else "⏳ Предзаказы"
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button(label, callback_data="preorders")],
        [blue_button("🛟 Техподдержка", callback_data="menu:support")],
        [blue_button("В меню", callback_data="menu:home")],
    ])


def bonus_keyboard(referral_link):
    """Copyable invite link plus the usual navigation."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("📋 Скопировать ссылку", copy_text={"text": referral_link})],
        [blue_button("🛒 Товары", callback_data="menu:products")],
        [blue_button("В меню", callback_data="menu:home")],
    ])


def wallet_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("💳 Пополнить 100 ₽", callback_data="wallet:100"), blue_button("💳 Пополнить 250 ₽", callback_data="wallet:250")],
        [blue_button("Пополнить 500 ₽", callback_data="wallet:500"), blue_button("Пополнить 1000 ₽", callback_data="wallet:1000")],
        [blue_button("Пополнить 1500 ₽", callback_data="wallet:1500")],
        [blue_button("В меню", callback_data="menu:home")],
    ])


def wallet_methods_keyboard(amount):
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button(f"🧾 Оплата админу · {amount} ₽", callback_data=f"walletpay:{amount}:admin")],
        [blue_button("⬅️ Назад к суммам", callback_data="menu:wallet")],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[styled_button("Отмена", "danger", callback_data="upload:cancel")]])


def categories_keyboard(categories=()):
    rows = [[blue_button(str(category.get("name") or category["slug"])[:50], callback_data="category:" + category["slug"])]
            for category in categories]
    rows.append([styled_button("Назад", "danger", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def review_keyboard(order_id, product_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [plain_button(f"{stars} ⭐", callback_data=f"review:{order_id}:{product_id}:{stars}") for stars in (1, 2, 3, 4, 5)],
        [styled_button("Позже", "danger", callback_data="review:later")],
    ])


def review_comment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[styled_button("Готово", "success", callback_data="review:done")]])


def photo_ref(filename):
    """Reuse Telegram's file id after the first upload instead of re-sending the file."""
    cached = photo_cache.get(filename)
    return cached if cached else FSInputFile(BASE_DIR / "webapp" / filename)


async def replace_message(message, text, reply_markup=None):
    """Edit the current Telegram message in place whenever Telegram permits it."""
    try:
        if getattr(message, "photo", None):
            await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    except Exception:
        pass
    # A callback message can occasionally be non-editable (old/deleted message).
    if getattr(message, "edit_text", None) is not None and getattr(message, "chat", None):
        try:
            await message.delete()
        except Exception:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)


async def remember_photo(sent, filename):
    try:
        if sent is not None and getattr(sent, "photo", None):
            photo_cache[filename] = sent.photo[-1].file_id
    except Exception:
        pass


async def send_photo(message, filename, caption, reply_markup):
    # Keep deployments with an older caption constant compatible: adjacent
    # strings should be a single value, but a trailing comma makes a tuple.
    if isinstance(caption, (tuple, list)):
        caption = "".join(caption)
    existing_photo = getattr(message, "photo", None)
    if isinstance(existing_photo, (list, tuple)) and existing_photo:
        try:
            await message.edit_media(
                media=InputMediaPhoto(media=photo_ref(filename), caption=caption, parse_mode="HTML"),
                reply_markup=reply_markup,
            )
            return
        except Exception as error:
            # A cached file id from another chat cannot always be reused in an edit.
            photo_cache.pop(filename, None)
            log.debug("edit_media failed, sending a new photo: %s", error)
    elif not existing_photo:
        try:
            await message.edit_text(caption, parse_mode="HTML", reply_markup=reply_markup)
            return
        except Exception as error:
            log.debug("edit_text failed, sending a new message: %s", error)
    if (BASE_DIR / "webapp" / filename).is_file():
        sent = await message.answer_photo(photo_ref(filename), caption=caption, parse_mode="HTML",
                                          reply_markup=reply_markup)
        await remember_photo(sent, filename)
    else:
        # Keep navigation usable if a deployment accidentally omits a photo.
        log.error("Missing bot photo: %s", filename)
        await message.answer(caption, parse_mode="HTML", reply_markup=reply_markup)


async def send_menu(message, user):
    await store.profile(user.model_dump(), user.id in ADMIN_IDS)
    await send_photo(message, "1.jpg", MENU_CAPTION, menu_keyboard())


def category_caption(slug, items, title=None):
    name = title or CATEGORY_TITLES.get(slug, slug)
    lines = [f"<b>Полка {escape(str(name))}</b>", "", "Путник, выбирай товар снизу"]
    if not items:
        lines.append("\n🔴 Полка пуста: хранитель лавки ещё не выложил артефакты.")
    return "\n".join(lines)


def category_keyboard(categories, slug, items):
    rows = []
    for item in items[:15]:
        stock = item["stock"] if type(item["stock"]) is int else 0
        prefix = "🧪 " if item.get("is_test") else ""
        # Зелёная кнопка — товар есть, красная — кончился (тогда работает предзаказ).
        style = "success" if stock > 0 else "danger"
        rows.append([styled_button(f"{prefix}{item['name'][:50]}", style, callback_data=f"product:{item['id']}")])
    rows.append([styled_button("Назад", "danger", callback_data="menu:products")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_caption(item):
    stock = item["stock"] if type(item["stock"]) is int else 0
    if item.get("is_test"):
        availability = f" Тестовый товар · единиц для выдачи: {stock}"
    elif stock > 0:
        availability = f"🟢 В наличии: {stock}"
    else:
        availability = "🔴 Нет в наличии" + (" · доступен предзаказ" if item.get("allow_preorder") else "")
    description = escape(item.get("description") or "Описание уточняется у хранителя.")
    price = f"{item['price']:,} ₽" if type(item.get("price")) is int and item["price"] > 0 else "Цена уточняется"
    reviews = int(item.get("reviews") or 0)
    rating_line = f"⭐ {item['rating']} · отзывов: {reviews}" if reviews and item.get("rating") else "Отзывов пока нет"
    note = ("\n\n🧪 Тестовый товар: тестер покупает его без оплаты, чтобы проверить автовыдачу."
            if item.get("is_test") else "")
    return (
        f"<b>{escape(item['name'])}</b>\n\n"
        f"{description}\n\n"
        f"<b>Цена:</b> {price}\n"
        f"<b>Наличие:</b> {availability}\n"
        f"<b>Оценки:</b> {rating_line}{note}\n\n"
        "Внимательно проверь условия покупки перед оплатой."
    )


def product_keyboard(item, can_test=False, is_admin=False):
    """Купить, если товар есть; иначе предзаказ — когда он разрешён для этого товара."""
    stock = item["stock"] if type(item["stock"]) is int else 0
    rows = []
    if item.get("is_test"):
        if stock > 0 and can_test:
            rows.append([styled_button("🧪 Тестовая покупка без оплаты", "success", callback_data=f"buy:{item['id']}")])
    elif stock > 0:
        rows.append([styled_button("Купить", "success", callback_data=f"buy:{item['id']}")])
    elif item.get("allow_preorder"):
        rows.append([plain_button(" Предзаказ · предоплата 100%", callback_data=f"preorder:{item['id']}")])
    if int(item.get("reviews") or 0):
        rows.append([blue_button(f"📝 Отзывы · {int(item['reviews'])}", callback_data=f"reviews:{item['id']}")])
    if is_admin:
        rows.append([blue_button("✏️ Управление товаром", callback_data=f"admprod:{item['id']}")])
    rows.append([styled_button("Назад", "danger", callback_data=f"category:{item['category']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_status_title(status):
    return {"new": "ждёт оплаты", "paid": "оплачен", "done": "выдан", "cancelled": "отменён",
            "preorder": "предзаказ (ждёт предоплату)"}.get(status, status)


def issued_message(event, head):
    lines = [f"<b>{head}</b>", "", "Бот выдал покупку автоматически:", ""]
    for product_item in event.get("items", []):
        lines.append(f"<b>{escape(str(product_item.get('name', 'Товар')))}</b>")
        lines.extend(f"<code>{escape(str(payload))}</code>" for payload in product_item.get("payloads", []))
        lines.append("")
    lines.append(f"Если товар не работает — напиши в поддержку: @{SUPPORT_USERNAME}")
    return "\n".join(lines)


async def ask_review(bot, chat_id, order_id, items):
    """После покупки просим оценку: она показывается в карточке товара."""
    for item in items[:5]:
        try:
            await bot.send_message(
                chat_id,
                f"<b>Как тебе покупка?</b>\n\n{escape(str(item.get('name', 'Товар')))}\n\n"
                "Поставь оценку — она поможет другим путникам выбрать.",
                parse_mode="HTML", reply_markup=review_keyboard(order_id, item["id"]),
            )
        except Exception as error:
            log.info("Review prompt to %s failed: %s", chat_id, error)


async def show_category(message, user, slug):
    last_shelf[user.id] = slug
    include_test = user.id in TESTER_IDS or user.id in ADMIN_IDS
    catalog = await store.catalog(include_test=include_test)
    items = [item for item in catalog["products"] if item["category"] == slug]
    title = next((category["name"] for category in catalog["categories"] if category["slug"] == slug), None)
    filename = CATEGORY_PHOTOS.get(slug, "shop.jpg")
    await send_photo(message, filename, category_caption(slug, items, title),
                     category_keyboard(catalog["categories"], slug, items))


# ---------------------------------------------------------------- админ-меню ---

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("➕ Добавить товар", callback_data="adm:add")],
        [blue_button("✏️ Редактировать товар", callback_data="adm:edit")],
        [blue_button("📤 Пополнить товар", callback_data="adm:topup")],
        [blue_button("🗑 Удалить товар", callback_data="adm:delete")],
        [blue_button("📦 Заказы", callback_data="adm:orders"), blue_button("📊 Статистика", callback_data="adm:stats")],
        [blue_button("👥 Покупатели", callback_data="adm:users")],
        [blue_button("⭐ Отзывы", callback_data="adm:reviews")]
        + ([blue_button("🖥 Панель на сайте", url=WEBAPP_URL.rstrip("/") + "/admin")]
           if WEBAPP_URL and ADMIN_PANEL_TOKEN else []),
        [blue_button("В меню", callback_data="menu:home")],
    ])


ADMIN_HELP = (
    "<b>🛠 Управление Лавкой Странника</b>\n\n"
    "Всё делается прямо здесь, в чате:\n"
    "• <b>Добавить товар</b> — новая позиция на полке;\n"
    "• <b>Редактировать</b> — название, цена, остаток, описание, категория, предзаказ;\n"
    "• <b>Пополнить</b> — загрузить автовыдачу: сначала уходит предзаказам, остаток на полку;\n"
    "• <b>Удалить</b> — убрать товар с полки.\n\n"
    "После пополнения бот сам разошлёт покупателям фото категории с ценой и кнопкой «🛒 Купить».\n\n"
    + (f"🖥 <b>Полная панель на сайте:</b> {escape(WEBAPP_URL.rstrip('/') + '/admin')}"
       "\nОткрой её в обычном браузере и введи токен панели." if WEBAPP_URL and ADMIN_PANEL_TOKEN else
       " Чтобы открыть полную панель на сайте без Telegram, задай на хостинге ADMIN_PANEL_TOKEN.")
)


def admin_products_keyboard(products, action, empty_hint="Товаров пока нет."):
    rows = [[blue_button(f"{item['name'][:44]} · {item['stock']} шт · {item['price']:,} ₽",
                         callback_data=f"{action}:{item['id']}")] for item in products[:25]]
    rows.append([blue_button("🛠 В админ-меню", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_product_keyboard(item):
    """Карточка товара для админа: правка каждого поля и быстрое пополнение."""
    on, off = "✅", "🚫"
    return InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("✏️ Название", callback_data=f"admedit:{item['id']}:name")],
        [blue_button("💰 Цена", callback_data=f"admedit:{item['id']}:price"),
         blue_button(" Остаток", callback_data=f"admedit:{item['id']}:stock")],
        [blue_button("📝 Описание", callback_data=f"admedit:{item['id']}:description")],
        [blue_button("🗂 Категория", callback_data=f"admedit:{item['id']}:category")],
        [blue_button(f"⏳ Предзаказ: {'вкл' if item['allow_preorder'] else 'выкл'} {on if item['allow_preorder'] else off}",
                     callback_data=f"admtoggle:{item['id']}:allow_preorder")],
        [blue_button(f"🧪 Тестовый: {'да' if item['is_test'] else 'нет'} {on if item['is_test'] else off}",
                     callback_data=f"admtoggle:{item['id']}:is_test"),
         blue_button(f" Показ: {'да' if item['active'] else 'нет'} {on if item['active'] else off}",
                     callback_data=f"admtoggle:{item['id']}:active")],
        [styled_button("📤 Добавить автовыдачу", "success", callback_data=f"upload:{item['id']}")],
        [blue_button("🧰 Изменить строки автовыдачи", callback_data=f"deliv:list:{item['id']}")],
        [styled_button("🗑 Удалить товар", "danger", callback_data=f"admdel:{item['id']}")],
        [blue_button("⬅️ К списку товаров", callback_data="adm:edit")],
    ])


def admin_product_caption(item):
    stock = int(item.get("stock") or 0)
    price = f"{item['price']:,} ₽" if item.get("price") else "цена не задана"
    lines = [
        f"<b>🛠 {escape(str(item['name']))}</b>", "",
        f"<b>Цена:</b> {price}",
        f"<b>Остаток:</b> {stock}",
        f"<b>Готово к выдаче:</b> {int(item.get('ready_items') or 0)} · выдано: {int(item.get('issued_items') or 0)}",
        f"<b>Категория:</b> {escape(str(item.get('category', '')))}",
        f"<b>Предзаказ:</b> {'включён' if item.get('allow_preorder') else 'выключен'}",
        f"<b>Показ на полке:</b> {'да' if item.get('active') else 'нет'}"
        f"{' · 🧪 тестовый' if item.get('is_test') else ''}",
        f"<b>Отзывы:</b> {int(item.get('reviews') or 0)}"
        + (f" · ⭐ {item['rating']}" if item.get("rating") else ""),
    ]
    if item.get("description"):
        lines.extend(["", escape(str(item["description"])[:600])])
    lines.extend(["", "Нажми поле, чтобы изменить его в чате."])
    return "\n".join(lines)


# -------------------------------------------------------------- обработчики ---

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    if not await is_subscribed(message.from_user.id):
        await send_subscription_gate(message)
        return
    if message.text and message.text.startswith("/start ref_"):
        raw = message.text.split("ref_", 1)[1].split()[0]
        if raw.isdecimal():
            await store.profile(message.from_user.model_dump(), message.from_user.id in ADMIN_IDS)
            await store.set_referrer(message.from_user.id, int(raw))
    await send_menu(message, message.from_user)


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    """Админ-меню в чате. Посторонним бот не отвечает и не подсказывает про ADMIN_IDS."""
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(ADMIN_HELP, parse_mode="HTML", reply_markup=admin_keyboard())


@dp.message(Command("id"))
async def cmd_id(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("add"))
async def cmd_add(message: Message):
    """Быстрое добавление товара из чата: выбор категории, затем мастер."""
    user = message.from_user
    if user.id not in ADMIN_IDS:
        return
    add_state.pop(user.id, None)
    upload_state.pop(user.id, None)
    edit_state.pop(user.id, None)
    catalog = await store.catalog(admin=True)
    rows = [[blue_button(str(category.get("name") or category["slug"])[:50], callback_data=f"add:{category['slug']}")]
            for category in catalog["categories"] if category.get("active")]
    rows.append([styled_button("Отмена", "danger", callback_data="add:cancel")])
    await message.answer("<b>➕ Новый товар</b>\n\nВыбери полку, на которую его выложить.", parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return
    state = add_state.pop(user_id, None)
    upload_state.pop(user_id, None)
    edit_state.pop(user_id, None)
    delivery_edit_state.pop(user_id, None)
    review_state.pop(user_id, None)
    if not state:
        await message.answer("Активных действий нет. Продолжай путь, путник.")
        return
    await message.answer("Действие отменено.")
    if state.get("message") is not None and state["slug"] in CATEGORY_PHOTOS:
        await show_category(state["message"], message.from_user, state["slug"])


def add_prompt_caption(state):
    title = CATEGORY_TITLES.get(state["slug"], state["slug"])
    return f"<b>➕ Новый товар · {escape(str(title))}</b>\n\n{ADD_PROMPTS[state['step']]}\n\nОтмена — кнопка ниже."


def add_keyboard(step=None):
    if step == "type":
        return InlineKeyboardMarkup(inline_keyboard=[
            [styled_button("🧪 Тестовый товар", "success", callback_data="addtype:test"),
             blue_button("📦 Обычный товар", callback_data="addtype:regular")],
            [styled_button("Отмена", "danger", callback_data="add:cancel")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[[styled_button("Отмена", "danger", callback_data="add:cancel")]])


async def prompt_add(message, state):
    caption, markup = add_prompt_caption(state), add_keyboard(state["step"])
    if message.photo:
        try:
            await message.edit_caption(caption=caption, parse_mode="HTML", reply_markup=markup)
            return
        except Exception as error:
            log.warning("edit_caption failed, sending a new prompt: %s", error)
    await message.answer(caption, parse_mode="HTML", reply_markup=markup)


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
            "allow_preorder": bool(state["data"].get("allow_preorder", not is_test)),
        }
        try:
            saved = await store.save_product(payload)
        except ApiError as error:
            await message.answer(f"Не удалось сохранить товар: {error.message}")
            return
        kind_line = "🧪 Тестовый товар: тестеры смогут проверить автовыдачу без оплаты." if is_test else \
            "Загрузи автовыдачу, иначе покупатель не сможет купить товар."
        await message.answer(
            f"<b>✅ Товар добавлен на полку</b>\n\n{escape(payload['name'])} — {payload['price']:,} ₽ · "
            f"остаток: {payload['stock']}.\n\n{kind_line}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [blue_button("✏️ Открыть управление товаром", callback_data=f"admprod:{saved['id']}")],
                [blue_button("📤 Загрузить автовыдачу", callback_data=f"upload:{saved['id']}")],
            ]),
        )
        await notify_new_product(await store.product(saved["id"]))
        if state.get("message") is not None:
            await show_category(state["message"], user, state["slug"])
        return
    state["step"] = ADD_STEPS[ADD_STEPS.index(step) + 1]
    await prompt_add(state["message"], state)


@dp.message(F.text, ~F.text.startswith("/"), lambda message: message.from_user.id in edit_state)
async def edit_wizard(message: Message):
    """Правка одного поля уже созданного товара."""
    user = message.from_user
    state = edit_state.get(user.id)
    if not state:
        return
    value = (message.text or "").strip()
    field = state["field"]
    if field == "name" and (not value or len(value) > 200):
        await message.answer("Название: от 1 до 200 символов. Повтори ввод.")
        return
    if field in ("price", "stock"):
        limit = 10**6 if field == "stock" else 2**31
        if not value.isdecimal() or int(value) > limit:
            await message.answer("Нужно целое неотрицательное число. Повтори ввод.")
            return
        value = int(value)
    if field == "description" and value.lower() in ("пропустить", "-", "без описания"):
        value = ""
    try:
        await store.patch_product(state["product_id"], {field: value})
    except ApiError as error:
        await message.answer(f"Не удалось сохранить: {error.message}")
        return
    edit_state.pop(user.id, None)
    item = await store.product(state["product_id"])
    await message.answer(f"<b>✅ Сохранено</b>\n\n{escape(str(item['name']))} · {FIELD_TITLES[field]} обновлён(а).",
                         parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [blue_button("✏️ Управление товаром", callback_data=f"admprod:{item['id']}")],
                             [blue_button("🛠 В админ-меню", callback_data="adm:home")],
                         ]))


@dp.message(F.text, ~F.text.startswith("/"), lambda message: message.from_user.id in review_state)
async def review_comment(message: Message):
    """Необязательный текст к уже поставленной оценке."""
    user = message.from_user
    state = review_state.pop(user.id, None)
    if not state:
        return
    body = (message.text or "").strip()[:1000]
    try:
        await store.add_review(user.id, state["order_id"], state["product_id"], state["rating"], body)
    except ApiError as error:
        await message.answer(f"Не удалось сохранить отзыв: {error.message}")
        return
    await message.answer("🙏 Спасибо! Отзыв сохранён.", reply_markup=back_keyboard())


@dp.message(F.text, ~F.text.startswith("/"), lambda message: message.from_user.id in delivery_edit_state)
async def delivery_edit_wizard(message: Message):
    user = message.from_user
    state = delivery_edit_state.get(user.id)
    if not state:
        return
    value = (message.text or "").strip()
    if not value or len(value) > 2000:
        await message.answer("Строка товара должна быть от 1 до 2000 символов.")
        return
    try:
        await store.replace_delivery(state["product_id"], state["delivery_id"], value)
    except ApiError as error:
        await message.answer(f"Не удалось заменить строку: {error.message}")
        return
    delivery_edit_state.pop(user.id, None)
    item = await store.product(state["product_id"])
    await message.answer("<b>✅ Строка автовыдачи заменена</b>", parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [blue_button("🧰 Изменить ещё", callback_data=f"deliv:list:{state['product_id']}")],
                             [blue_button("⬅️ К товару", callback_data=f"admprod:{state['product_id']}")],
                         ]))


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
    product = await store.product(state["product_id"])
    product_name = escape(str(product.get("name", "Товар"))) if product else "Товар"
    product_price = f"{product['price']:,} ₽" if product and isinstance(product.get("price"), int) else "Цена уточняется"
    parts = [f"<b>✅ Товар пополнен</b>\n\n<b>{product_name}</b>\nЦена: {product_price}\nЗагружено единиц: {result['added']}"]
    if result["delivered_to_preorders"]:
        parts.append(f"📦 {result['delivered_to_preorders']} предзаказ(ов) выданы сразу — покупатели получили товар в чат.")
    parts.append(f"🟢 Выставлено на полку: {result['stock_added']}")
    parts.append("Уведомления покупателям уходят в фоне — чат не будет ждать.")
    await message.answer("\n".join(parts), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [blue_button("✏️ Управление товаром", callback_data=f"admprod:{state['product_id']}")],
        [blue_button(" В админ-меню", callback_data="adm:home")],
    ]))
    await notify_restock(product, delivered=result["delivered_to_preorders"])
    if state.get("message") is not None:
        slug = last_shelf.get(user.id)
        if slug in CATEGORY_PHOTOS:
            await show_category(state["message"], user, slug)


@dp.callback_query(F.data.startswith("deliv:list:"))
async def delivery_list_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    raw_id = callback.data.split(":")[-1]
    if not raw_id.isdecimal():
        await callback.answer()
        return
    product_id = int(raw_id)
    item = await store.product(product_id)
    if not item:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    deliveries = await store.delivery_items(product_id, 25)
    rows = []
    for delivery in deliveries:
        preview = str(delivery["payload"]).replace("\n", " ")[:32]
        rows.append([blue_button(f"✏️ {preview}", callback_data=f"deliv:edit:{product_id}:{delivery['id']}"),
                     styled_button("🗑", "danger", callback_data=f"deliv:delete:{product_id}:{delivery['id']}")])
    rows.append([blue_button("📤 Добавить строки", callback_data=f"upload:{product_id}")])
    rows.append([blue_button("⬅️ К товару", callback_data=f"admprod:{product_id}")])
    await callback.answer()
    text = f"<b>🧰 Автовыдача · {escape(str(item['name']))}</b>\n\nГотовых строк: {len(deliveries)}\nВыбери строку для замены или удаления."
    await replace_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("deliv:edit:"))
async def delivery_edit_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or not parts[2].isdecimal() or not parts[3].isdecimal():
        await callback.answer()
        return
    product_id, delivery_id = int(parts[2]), int(parts[3])
    delivery_edit_state[callback.from_user.id] = {"product_id": product_id, "delivery_id": delivery_id}
    await callback.answer()
    await replace_message(callback.message, "<b>✏️ Замена строки автовыдачи</b>\n\nОтправь новую строку товара одним сообщением.\nОтмена — /cancel.",
                          InlineKeyboardMarkup(inline_keyboard=[[blue_button("⬅️ Назад", callback_data=f"deliv:list:{product_id}")]]))


@dp.callback_query(F.data.startswith("deliv:delete:"))
async def delivery_delete_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or not parts[2].isdecimal() or not parts[3].isdecimal():
        await callback.answer()
        return
    product_id, delivery_id = int(parts[2]), int(parts[3])
    try:
        await store.delete_delivery(product_id, delivery_id)
    except ApiError as error:
        await callback.answer(error.message, show_alert=True)
        return
    await callback.answer("Строка удалена")
    callback.data = f"deliv:list:{product_id}"
    await delivery_list_callback(callback)


@dp.callback_query(F.data.startswith("upload:"))
async def upload_callback(callback: CallbackQuery):
    user = callback.from_user
    value = callback.data.split(":", 1)[1]
    if value == "cancel":
        state = upload_state.pop(user.id, None)
        await callback.answer()
        if state and last_shelf.get(user.id) in CATEGORY_PHOTOS:
            await show_category(callback.message, user, last_shelf[user.id])
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
    edit_state.pop(user.id, None)
    upload_state[user.id] = {"product_id": item["id"], "message": callback.message}
    await callback.answer()
    prompt = UPLOAD_PROMPT.format(name=escape(str(item["name"])))
    if callback.message.photo:
        try:
            await callback.message.edit_caption(caption=prompt, parse_mode="HTML", reply_markup=cancel_keyboard())
            return
        except Exception as error:
            log.warning("edit_caption failed, sending upload prompt: %s", error)
    await callback.message.answer(prompt, parse_mode="HTML", reply_markup=cancel_keyboard())


@dp.callback_query(F.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery):
    message, user = callback.message, callback.from_user
    action = callback.data.split(":", 1)[1]
    if not await is_subscribed(user.id):
        await callback.answer("Сначала подпишись на канал и нажми «Проверить подписку».", show_alert=True)
        await send_subscription_gate(message)
        return
    await callback.answer()
    if action in ("home", "products", "shop", "profile", "bonus"):
        add_state.pop(user.id, None)
        upload_state.pop(user.id, None)
        edit_state.pop(user.id, None)
    if action == "home":
        await send_menu(message, user)
    elif action == "shop":
        url = webapp_url()
        if url:
            await replace_message(message,
                "Мини-лавка открывается кнопкой «Лавка Странника» в меню.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [blue_button("🏪 Лавка Странника", web_app=WebAppInfo(url=url))],
                    [blue_button("В меню", callback_data="menu:home")],
                ]),
            )
        else:
            await send_photo(message, "shop.jpg", SHOP_CAPTION, await catalog_keyboard())
    elif action == "products":
        await store.profile(user.model_dump(), user.id in ADMIN_IDS)
        await send_photo(message, "shop.jpg", SHOP_CAPTION, await catalog_keyboard())
    elif action == "profile":
        profile = await store.profile(user.model_dump(), user.id in ADMIN_IDS)
        caption = (
            "<b>Привет, путник!</b>\n\n"
            "Вот что хранит летопись твоих странствий:\n\n"
            f"Куплено товаров: <b>{profile['purchases']}</b>\n"
            f"Потрачено: <b>{profile['spent']:,} ₽</b>\n"
            f"Баланс: <b>{profile['balance']:,} ₽</b>\n"
            f"Твой номер в лавке: <b>№{profile['traveler_no']}</b>\n"
            f"Активных предзаказов: <b>{profile['preorders']}</b>\n"
            f"Любимый товар: <b>{escape(profile['favorite_product'] or 'Пока нет покупок')}</b>\n\n"
            "В летопись попадают только оплаченные покупки."
        )
        await send_photo(message, "2.jpg", caption, profile_keyboard(bool(profile["preorders"])))
    elif action == "bonus":
        profile = await store.profile(user.model_dump(), user.id in ADMIN_IDS)
        bonus = await store.referral_stats(user.id)
        caption = (
            "<b>🎁 Бонусы странника</b>\n\n"
            f"Приглашай друзей — получай <b>{BONUS_PERCENT}%</b> от их покупок на баланс.\n\n"
            f"Твоя ссылка:\n<code>{escape(profile['referral_link'])}</code>\n\n"
            f"Пришло друзей: <b>{bonus['invited']}</b>\n"
            f"Их покупок на: <b>{bonus['turnover']:,} ₽</b>\n"
            f"Заработано бонусов: <b>{bonus['earned']:,} ₽</b>\n\n"
            "Бонус начисляется, когда приглашённый оплачивает заказ."
        )
        await send_photo(message, "1.jpg", caption, bonus_keyboard(profile["referral_link"]))
    elif action == "wallet":
        profile = await store.profile(user.model_dump(), user.id in ADMIN_IDS)
        await replace_message(message, f"<b>Кошелёк странника</b>\n\nБаланс: <b>{profile['balance']:,} ₽</b>\n\nВыбери сумму пополнения:", wallet_keyboard())
    elif action == "support":
        await replace_message(message,
            "<b>Хранитель лавки на связи</b>\n\n"
            "Нужна помощь с выбором нейросети, оплатой или покупкой? "
            "Напиши нам — поможем найти верный путь.\n\n"
            f"🛡 <a href=\"{WARRANTY_TERMS_URL}\">Условия гарантии</a>",
            support_keyboard())


@dp.callback_query(F.data == "subscription:check")
async def subscription_check_callback(callback: CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.answer("Подписка подтверждена.")
        await send_menu(callback.message, callback.from_user)
    else:
        await callback.answer("Подписка не найдена. Подпишись на канал и проверь ещё раз.", show_alert=True)


async def catalog_keyboard():
    catalog = await store.catalog()
    visible = [category for category in catalog["categories"]
               if category.get("active") and any(item["category"] == category["slug"] for item in catalog["products"])]
    return categories_keyboard(visible or [category for category in catalog["categories"] if category.get("active")])


@dp.callback_query(F.data.startswith("wallet:"))
async def wallet_topup_callback(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or int(value) not in (100, 250, 500, 1000, 1500):
        await callback.answer("Недоступная сумма.", show_alert=True)
        return
    amount = int(value)
    await callback.answer()
    await replace_message(callback.message, f"<b>Пополнение баланса · {amount} ₽</b>\n\nВыбери способ оплаты:", wallet_methods_keyboard(amount))


@dp.callback_query(F.data.startswith("walletpay:"))
async def wallet_payment_callback(callback: CallbackQuery):
    try:
        _, raw_amount, method = callback.data.split(":", 2)
        amount = int(raw_amount)
    except (ValueError, TypeError):
        await callback.answer()
        return
    if method != "admin":
        await callback.answer("Выбери оплату через администратора.", show_alert=True)
        return
    await callback.answer()
    await replace_message(
        callback.message,
        f"<b>🧾 Пополнение кошелька · {amount} ₽</b>\n\n"
        f"Напиши администратору @{SUPPORT_USERNAME} и отправь сумму <b>{amount} ₽</b>.\n"
        "После проверки платежа администратор зачислит деньги на твой баланс.",
        InlineKeyboardMarkup(inline_keyboard=[
            [blue_button(" Написать администратору", url=f"https://t.me/{SUPPORT_USERNAME}")],
            [blue_button("⬅️ Назад к кошельку", callback_data="menu:wallet")],
        ]),
    )


@dp.callback_query(F.data.startswith("category:"))
async def category_callback(callback: CallbackQuery):
    """Photo shelf: picture, caption, stock marks and admin tools."""
    await callback.answer()
    slug = callback.data.split(":", 1)[1]
    add_state.pop(callback.from_user.id, None)
    upload_state.pop(callback.from_user.id, None)
    edit_state.pop(callback.from_user.id, None)
    await show_category(callback.message, callback.from_user, slug)


@dp.callback_query(F.data == "preorders")
async def preorders_callback(callback: CallbackQuery):
    """Личная полка пользователя с его активными предзаказами."""
    await callback.answer()
    add_state.pop(callback.from_user.id, None)
    upload_state.pop(callback.from_user.id, None)
    edit_state.pop(callback.from_user.id, None)
    personal = await store.user_preorders(callback.from_user.id)
    lines = [
        "<b> Полка предзаказов</b>", "",
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
    else:
        lines.append("У тебя пока нет оформленных предзаказов.")
    personal_items = []
    for order in personal:
        for item in order["items"]:
            if not any(existing["id"] == item.get("id") for existing in personal_items):
                personal_items.append({"id": item.get("id"), "name": item.get("name", "Товар")})
    rows = [[styled_button(item["name"][:50], "danger", callback_data=f"product:{item['id']}")]
            for item in personal_items[:12]]
    if len(personal_items) > 12:
        lines.append(f"… и ещё {len(personal_items) - 12}: список обрезан.")
    rows.append([blue_button("🛟 Оплатить предоплату", url=f"https://t.me/{SUPPORT_USERNAME}")])
    rows.append([styled_button("Назад", "danger", callback_data="menu:profile")])
    await send_photo(callback.message, "shop.jpg", "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("reviews:"))
async def reviews_callback(callback: CallbackQuery):
    """Отзывы по товару — их видно и покупателю, и админу."""
    await callback.answer()
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or len(value) > 19:
        return
    item = await store.product(int(value))
    if not item:
        await callback.answer("Товар больше не найден.", show_alert=True)
        return
    data = await store.product_reviews(item["id"])
    lines = [f"<b>📝 Отзывы · {escape(str(item['name']))}</b>", ""]
    if data["count"]:
        lines.append(f"Средняя оценка: <b>⭐ {data['average']}</b> · отзывов: <b>{data['count']}</b>")
        lines.append("")
        for review in data["reviews"]:
            lines.append(f"⭐ {review['rating']} · {escape(str(review['name']))}")
            if review["text"]:
                lines.append(f"<i>{escape(review['text'][:300])}</i>")
            lines.append("")
    else:
        lines.append("Отзывов пока нет. Стань первым, кто оценит этот товар.")
    await replace_message(callback.message, "\n".join(lines),
                          InlineKeyboardMarkup(inline_keyboard=[[
                              styled_button("Назад", "danger", callback_data=f"product:{item['id']}")
                          ]]))


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
    markup = product_keyboard(item, user.id in TESTER_IDS or user.id in ADMIN_IDS, user.id in ADMIN_IDS)
    if callback.message.photo:
        try:
            await callback.message.edit_caption(caption=product_caption(item), parse_mode="HTML", reply_markup=markup)
            return
        except Exception as error:
            log.warning("edit_caption failed, sending product details: %s", error)
    await callback.message.answer(product_caption(item), parse_mode="HTML", reply_markup=markup)


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
            await callback.answer(" Это тестовый товар: покупка без оплаты доступна только тестерам.", show_alert=True)
            return
        try:
            result = await store.create_test_order(user.id, item["id"])
        except ApiError as error:
            await callback.answer(error.message, show_alert=True)
            return
        await replace_message(
            callback.message,
            issued_message(result["issued"][0], f"🧪 Тестовая покупка — заказ № {result['order_id']}"),
            back_keyboard(),
        )
        await callback.answer("Тестовая покупка выполнена.")
        return
    if item["stock"] <= 0:
        await callback.answer("Товар закончился. Оформи предзаказ — бот сообщит, когда он появится.", show_alert=True)
        return
    price = f"{item['price']:,} ₽"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [blue_button(f"💰 Баланс · {price}", callback_data=f"pay:{item['id']}:balance")],
        [blue_button("🧾 Оплата через администратора", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [styled_button("⬅️ Назад", "danger", callback_data=f"product:{item['id']}")],
    ])
    await callback.answer()
    await replace_message(callback.message, f"<b>Оплата заказа</b>\n\n{escape(str(item['name']))} · {price}\n\nВыбери способ оплаты:", markup)


async def web_order_notice(user, result):
    bot = active_bot.get("bot")
    if not bot:
        return
    total = int(result.get("total") or 0)
    order_id = int(result.get("order_id") or 0)
    items = result.get("items") or []
    names = ", ".join(str(item.get("name", "Товар")) for item in items) or "Товар"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [blue_button(f"💰 Оплатить с баланса · {total:,} ₽", callback_data=f"orderpay:{order_id}:balance")],
        [blue_button("🧾 Оплатить админу", url=f"https://t.me/{SUPPORT_USERNAME}")],
        [blue_button("🏠 В меню", callback_data="menu:home")],
    ])
    await bot.send_message(
        user["id"],
        f"<b>🧾 Счёт № {order_id} создан</b>\n\n{escape(names)}\nСумма: <b>{total:,} ₽</b>\n\n"
        "Выбери оплату ниже. После списания с баланса товар придёт сюда автоматически.",
        parse_mode="HTML", reply_markup=markup,
    )


@dp.callback_query(F.data.startswith("orderpay:"))
async def web_order_payment_callback(callback: CallbackQuery):
    try:
        _, raw_order_id, payment = callback.data.split(":", 2)
        order_id = int(raw_order_id)
    except (ValueError, TypeError):
        await callback.answer()
        return
    if payment != "balance":
        await callback.answer("Оплата админу доступна по кнопке выше.", show_alert=True)
        return
    try:
        result = await store.pay_order_balance(callback.from_user.id, order_id)
    except ApiError as error:
        await callback.answer(error.message, show_alert=True)
        return
    await callback.answer("Оплата прошла")
    if result.get("issued"):
        event = result["issued"][0]
        await replace_message(callback.message, issued_message(event, f"📦 Покупка № {order_id} оплачена и выдана"), back_keyboard())
        bot = active_bot.get("bot")
        if bot:
            await ask_review(bot, callback.from_user.id, order_id, event.get("items", []))
    else:
        await replace_message(callback.message, f"<b>✅ Счёт № {order_id} оплачен</b>\n\nСписано: <b>{result['total']:,} ₽</b>. Товар будет выдан автоматически после пополнения.", back_keyboard())


@dp.callback_query(F.data.startswith("pay:"))
async def payment_callback(callback: CallbackQuery):
    try:
        _, raw_id, payment = callback.data.split(":", 2)
        product_id = int(raw_id)
    except (ValueError, TypeError):
        await callback.answer()
        return
    if payment in ("crypto", "sbp"):
        await callback.answer("Этот способ оплаты скоро будет доступен.", show_alert=True)
        return
    item = await store.product(product_id)
    if not item or not item["active"] or item.get("is_test"):
        await callback.answer("Товар больше не найден.", show_alert=True)
        return
    try:
        result = await store.create_order(callback.from_user.id, {"cart": [{"id": product_id, "qty": 1}], "kind": "order", "payment": "balance", "idempotency_key": uuid4().hex, "comment": ""})
    except ApiError as error:
        await callback.answer(error.message, show_alert=True)
        return
    await callback.answer("Заказ оплачен с баланса")
    if result.get("issued"):
        await replace_message(callback.message, issued_message(result["issued"][0], f" Покупка № {result['order_id']} оплачена и выдана"))
        bot = active_bot.get("bot")
        if bot:
            await ask_review(bot, callback.from_user.id, result["order_id"],
                             [{"id": product_id, "name": item["name"]}])
    else:
        await replace_message(callback.message, f"<b>Заказ № {result['order_id']} оформлен</b>\n\nСписано: <b>{result['total']:,} ₽</b>. Товар будет выдан автоматически после пополнения.", back_keyboard())


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
    if not item.get("allow_preorder"):
        await callback.answer("Предзаказ для этого товара недоступен.", show_alert=True)
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
        f"{escape(str(item['name']))} — {price}.\n\n"
        "Предзаказ действует по <b>предоплате 100%</b>: внеси полную сумму у хранителя лавки. "
        "Когда товар поступит, бот <b>сначала выдаст оплаченные предзаказы по очереди</b> "
        "и только потом выставит остаток на полку. Товар придёт тебе в этот чат автоматически. "
        "Следить за заказом можно в профиле → «⏳ Предзаказы».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [blue_button(" Оплатить предоплату", url=f"https://t.me/{SUPPORT_USERNAME}")],
            [blue_button("⏳ Мои предзаказы", callback_data="preorders")],
            [blue_button("В меню", callback_data="menu:home")],
        ]),
    )
    await callback.answer("Предзаказ оформлен.")


@dp.callback_query(F.data.startswith("review:"))
async def review_callback(callback: CallbackQuery):
    """Оценка после покупки: звезда сохраняется сразу, текст можно добавить следом."""
    parts = callback.data.split(":")
    if len(parts) == 2 and parts[1] == "later":
        await callback.answer("Хорошо, оценишь позже.")
        return
    # This handler is registered before the exact `review:done` handler, so
    # handle the "Готово" button here instead of rejecting it as invalid data.
    if len(parts) == 2 and parts[1] == "done":
        review_state.pop(callback.from_user.id, None)
        await callback.answer("Отзыв сохранён.")
        await replace_message(
            callback.message,
            "<b>🙏 Спасибо за отзыв!</b>\n\nОн уже виден в карточке товара.",
            back_keyboard(),
        )
        return
    if len(parts) != 4 or not all(part.isdecimal() for part in parts[1:]):
        await callback.answer()
        return
    order_id, product_id, rating = (int(part) for part in parts[1:])
    user = callback.from_user
    try:
        review = await store.add_review(user.id, order_id, product_id, rating)
    except ApiError as error:
        await callback.answer(error.message, show_alert=True)
        return
    bot = active_bot.get("bot")
    if bot and REVIEWS_GROUP_CHAT_ID:
        try:
            quantity = int(review.get("quantity") or 1)
            product_name = escape(str(review.get("product_name") or "Товар"))
            buyer = (
                f"@{callback.from_user.username}"
                if callback.from_user.username
                else (callback.from_user.full_name or "Покупатель")
            )
            await bot.send_message(
                REVIEWS_GROUP_CHAT_ID,
                "⭐ <b>Новый отзыв</b>\n\n"
                f"Покупатель: <b>{escape(buyer)}</b>\n"
                f"Купили: <b>{product_name}</b>\n"
                f"Количество: <b>{quantity} шт.</b>\n"
                f"Оценка: <b>{rating}/5</b>",
                parse_mode="HTML",
            )
        except Exception as error:
            log.warning("Review group notification failed: %s", error)
    review_state[user.id] = {"order_id": order_id, "product_id": product_id, "rating": rating}
    await callback.answer(f"Спасибо! Оценка ⭐ {rating} сохранена.")
    item = await store.product(product_id)
    await replace_message(
        callback.message,
        f"<b>⭐ {rating} — спасибо!</b>\n\n"
        f"{escape(str((item or {}).get('name', 'Товар')))} получил твою оценку.\n\n"
        "Хочешь добавить пару слов о товаре? Напиши сообщением — или нажми «Готово».",
        review_comment_keyboard(),
    )


@dp.callback_query(F.data == "review:done")
async def review_done_callback(callback: CallbackQuery):
    review_state.pop(callback.from_user.id, None)
    await callback.answer("Отзыв сохранён.")
    await replace_message(callback.message, "<b>🙏 Спасибо за отзыв!</b>\n\nОн уже виден в карточке товара.",
                          back_keyboard())


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
    add_state.pop(user.id, None)
    upload_state.pop(user.id, None)
    edit_state.pop(user.id, None)
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
    state["data"]["allow_preorder"] = not state["data"]["is_test"]
    state["step"] = "name"
    await callback.answer()
    await prompt_add(state["message"], state)


@dp.callback_query(F.data.startswith("admprod:"))
async def admin_product_callback(callback: CallbackQuery):
    """Карточка товара с правкой полей — открывается из админ-меню."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or len(value) > 19:
        await callback.answer()
        return
    item = await store.product(int(value))
    if not item:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await callback.answer()
    await replace_message(callback.message, admin_product_caption(item), admin_product_keyboard(item))


@dp.callback_query(F.data.startswith("admedit:"))
async def admin_edit_callback(callback: CallbackQuery):
    _, raw_id, field = (callback.data.split(":", 2) + ["", ""])[:3]
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    item = await store.product(int(raw_id)) if raw_id.isdecimal() else None
    if not item or field not in FIELD_TITLES:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    await callback.answer()
    if field == "category":
        catalog = await store.catalog(admin=True)
        rows = [[blue_button(str(category.get("name") or category["slug"])[:50],
                             callback_data=f"admcat:{item['id']}:{category['slug']}")]
                for category in catalog["categories"]]
        rows.append([styled_button("Отмена", "danger", callback_data=f"admprod:{item['id']}")])
        await replace_message(callback.message,
                              f"<b> Категория товара</b>\n\n{escape(str(item['name']))}\n\nВыбери новую полку:",
                              InlineKeyboardMarkup(inline_keyboard=rows))
        return
    edit_state[callback.from_user.id] = {"product_id": item["id"], "field": field, "message": callback.message}
    current = item.get(field)
    shown = f"{current:,} ₽" if field == "price" and isinstance(current, int) else escape(str(current or "—"))
    await replace_message(
        callback.message,
        f"<b>✏️ {FIELD_TITLES[field].capitalize()}</b>\n\n{escape(str(item['name']))}\n"
        f"Сейчас: <b>{shown}</b>\n\n{EDIT_PROMPTS[field]}\n\nОтмена — /cancel или кнопка ниже.",
        InlineKeyboardMarkup(inline_keyboard=[[styled_button("Отмена", "danger", callback_data=f"admprod:{item['id']}")]]),
    )


@dp.callback_query(F.data.startswith("admcat:"))
async def admin_category_callback(callback: CallbackQuery):
    _, raw_id, slug = (callback.data.split(":", 2) + ["", ""])[:3]
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    edit_state.pop(callback.from_user.id, None)
    try:
        await store.patch_product(int(raw_id), {"category": slug})
    except (ApiError, ValueError) as error:
        await callback.answer(getattr(error, "message", "Не удалось сохранить."), show_alert=True)
        return
    await callback.answer("Категория обновлена.")
    item = await store.product(int(raw_id))
    await replace_message(callback.message, admin_product_caption(item), admin_product_keyboard(item))


@dp.callback_query(F.data.startswith("admtoggle:"))
async def admin_toggle_callback(callback: CallbackQuery):
    _, raw_id, field = (callback.data.split(":", 2) + ["", ""])[:3]
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    if field not in ("allow_preorder", "is_test", "active") or not raw_id.isdecimal():
        await callback.answer()
        return
    item = await store.product(int(raw_id))
    if not item:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    try:
        await store.patch_product(item["id"], {field: not bool(item[field])})
    except ApiError as error:
        await callback.answer(error.message, show_alert=True)
        return
    item = await store.product(item["id"])
    hint = {
        "allow_preorder": "Предзаказ включён: пока товара нет, кнопка «Купить» становится «Предзаказ».",
        "is_test": "Тестовый товар видят только тестеры и админ.",
        "active": "Товар скрыт с полки." if not item["active"] else "Товар снова виден на полке.",
    }[field]
    await callback.answer(hint[:190], show_alert=False)
    await replace_message(callback.message, admin_product_caption(item), admin_product_keyboard(item))


@dp.callback_query(F.data.startswith("admdel:"))
async def admin_delete_callback(callback: CallbackQuery):
    user = callback.from_user
    if user.id not in ADMIN_IDS:
        await callback.answer("Удалять товары может только хранитель лавки.", show_alert=True)
        return
    value = callback.data.split(":", 1)[1]
    if not value.isdecimal() or len(value) > 19:
        await callback.answer()
        return
    product_id = int(value)
    key = (user.id, product_id)
    if key not in pending_delete:
        for other in [item for item in pending_delete if item[0] == user.id]:
            pending_delete.discard(other)
        pending_delete.add(key)
        await callback.answer("Нажми ещё раз, чтобы подтвердить удаление.")
        item = await store.product(product_id)
        if item:
            await replace_message(
                callback.message,
                f"<b>⚠️ Удалить товар?</b>\n\n{escape(str(item['name']))}\n\n"
                "Вместе с ним удалятся остатки автовыдачи и отзывы. Нажми «Точно удалить», чтобы подтвердить.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [styled_button("🗑 Точно удалить", "danger", callback_data=f"admdel:{product_id}")],
                    [blue_button("Отмена", callback_data=f"admprod:{product_id}")],
                ]),
            )
        return
    pending_delete.discard(key)
    try:
        await store.delete_product(product_id)
    except ApiError as error:
        await callback.answer(error.message, show_alert=True)
        return
    await callback.answer("Товар удалён с полки.")
    await replace_message(callback.message, "<b>🗑 Товар удалён</b>", admin_keyboard())


@dp.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery):
    user = callback.from_user
    if user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    catalog = await store.catalog(admin=True)
    products = catalog["products"]
    await callback.answer()
    if action == "home":
        add_state.pop(user.id, None)
        edit_state.pop(user.id, None)
        upload_state.pop(user.id, None)
        await replace_message(callback.message, ADMIN_HELP, admin_keyboard())
    elif action == "add":
        rows = [[blue_button(str(category.get("name") or category["slug"])[:50], callback_data=f"add:{category['slug']}")]
                for category in catalog["categories"] if category.get("active")]
        rows.append([blue_button("⬅️ В админ-меню", callback_data="adm:home")])
        await replace_message(callback.message,
                              "<b>➕ Новый товар</b>\n\nВыбери полку, на которую его выложить.",
                              InlineKeyboardMarkup(inline_keyboard=rows))
    elif action == "edit":
        await replace_message(callback.message,
                              "<b>✏️ Редактировать товар</b>\n\nВыбери товар — дальше можно изменить название, цену, "
                              "остаток, описание, категорию и предзаказ.",
                              admin_products_keyboard(products, "admprod"))
    elif action == "topup":
        await replace_message(callback.message,
                              "<b>📤 Пополнить товар</b>\n\nВыбери товар и отправь список автовыдачи "
                              "(одна строка = одна единица).",
                              admin_products_keyboard(products, "upload"))
    elif action == "delete":
        await replace_message(callback.message,
                              "<b>🗑 Удалить товар</b>\n\nВыбери товар — удаление подтверждается вторым нажатием.",
                              admin_products_keyboard(products, "admdel"))
    elif action == "stats":
        data = await store.stats()
        await replace_message(callback.message,
                              "<b>📊 Статистика лавки</b>\n\n"
                              f"Заказов оплачено/выдано: <b>{data['orders']}</b>\n"
                              f"Выручка: <b>{data['revenue']:,} ₽</b>\n"
                              f"Покупателей: <b>{data['users']}</b>\n"
                              f"Товаров на полке: <b>{data['products']}</b>\n"
                              f"Отзывов: <b>{data['reviews']}</b> · средняя оценка: <b>⭐ {data['rating']}</b>",
                              admin_keyboard())
    elif action == "orders":
        await replace_message(callback.message, *await admin_orders_view())
    elif action == "users":
        data = await store.users_overview()
        lines = ["<b>👥 Покупатели</b>", ""]
        if data["users"]:
            for profile in data["users"][:15]:
                handle = f" @{profile['username']}" if profile["username"] else ""
                lines.append(
                    f"№{profile['traveler_no']} · {escape(str(profile['name']))}{escape(handle)} · "
                    f"баланс <b>{profile['balance']:,} ₽</b> · покупок {profile['orders']}"
                    + (f" · предзаказов {profile['preorders']}" if profile["preorders"] else "")
                )
        else:
            lines.append("Покупателей пока нет.")
        lines.extend(["", "Пополнение баланса — в панели на сайте."])
        await replace_message(callback.message, "\n".join(lines), admin_keyboard())
    elif action == "reviews":
        data = await store.recent_reviews()
        lines = ["<b>⭐ Последние отзывы</b>", ""]
        if data["reviews"]:
            for review in data["reviews"]:
                lines.append(f"⭐ {review['rating']} · {escape(str(review.get('author') or 'Покупатель'))} · "
                             f"{escape(str(review['product']))} · заказ № {review['order_id']}")
                if review["text"]:
                    lines.append(f"<i>{escape(review['text'][:200])}</i>")
        else:
            lines.append("Отзывов пока нет.")
        await replace_message(callback.message, "\n".join(lines), admin_keyboard())


def orders_caption(data):
    lines = ["<b>📦 Последние заказы</b>", ""]
    if not data["orders"]:
        lines.append("Заказов пока нет.")
    for order in data["orders"][:10]:
        names = ", ".join(str(item.get("name", "Товар")) for item in order["items"]) or "Товар"
        lines.append(f"№ {order['id']} · {escape(names[:60])} · {order['total']:,} ₽ — <b>{order_status_title(order['status'])}</b>")
    lines.extend(["", "Отметь оплату после получения денег — бот сразу выдаст товар."])
    return "\n".join(lines)


async def admin_orders_view():
    """Заказы вместе с кнопками управления — один запрос к базе."""
    data = await store.orders()
    return orders_caption(data), admin_orders_keyboard(data["orders"])


def admin_orders_keyboard(orders):
    """Кнопки по каждому заказу: зачислить оплату, выдать товар или отменить."""
    rows = []
    for order in orders[:8]:
        order_id = order["id"]
        if order["status"] in ("new", "preorder"):
            rows.append([blue_button(f"💰 Заказ № {order_id}: оплата получена", callback_data=f"admorder:{order_id}:paid")])
            rows.append([styled_button(f"✖️ Заказ № {order_id}: отменить", "danger", callback_data=f"admorder:{order_id}:cancelled")])
        elif order["status"] == "paid":
            rows.append([blue_button(f" Заказ № {order_id}: выдать товар", callback_data=f"admorder:{order_id}:done")])
    rows.append([blue_button("️ В админ-меню", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("admorder:"))
async def admin_order_callback(callback: CallbackQuery):
    _, raw_id, status = (callback.data.split(":", 2) + ["", ""])[:3]
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Это раздел хранителя лавки.", show_alert=True)
        return
    if not raw_id.isdecimal() or status not in ("paid", "done", "cancelled"):
        await callback.answer()
        return
    try:
        result = await store.change_order(int(raw_id), status)
    except ApiError as error:
        await callback.answer(error.message, show_alert=True)
        return
    if result.get("issued"):
        await notify_deliveries(result["issued"])
    await callback.answer(f"Заказ № {raw_id}: {order_status_title(result['status'])}")
    await replace_message(callback.message, *await admin_orders_view())


# ------------------------------------------------- автоматические уведомления ---

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
            continue
        if event.get("kind") != "test":
            reviewable = [entry for entry in event.get("items", []) if entry.get("id")]
            if reviewable:
                await ask_review(bot, event["user_id"], event["order_id"], reviewable)


async def notify_restock(product, delivered=0):
    """Announce a restock in the channel and notify travelers waiting for it."""
    bot = active_bot.get("bot")
    if not bot or not product:
        return 0
    stock = int(product.get("stock") or 0)
    if stock <= 0:
        return 0
    filename = CATEGORY_PHOTOS.get(product["category"], "shop.jpg")
    markup = InlineKeyboardMarkup(inline_keyboard=[[blue_button("🛒 Купить", callback_data=f"product:{product['id']}")]])
    if PRODUCTS_CHANNEL_CHAT_ID:
        try:
            caption = (
                "🟢 <b>Товар пополнен</b>\n\n"
                f"<b>{escape(str(product['name']))}</b>\n"
                f"Цена: {int(product.get('price') or 0):,} ₽\n"
                f"В наличии: {stock} шт."
            )
            await bot.send_photo(PRODUCTS_CHANNEL_CHAT_ID, photo_ref(filename), caption=caption,
                                 parse_mode="HTML", reply_markup=markup)
        except Exception as error:
            log.warning("Restock channel notification failed: %s", error)
    recipients = [user_id for user_id in await store.restock_recipients() if user_id not in ADMIN_IDS]
    task = asyncio.create_task(_broadcast(bot, recipients, filename, product, markup))
    broadcast_tasks.add(task)
    task.add_done_callback(broadcast_tasks.discard)
    return len(recipients)


async def notify_new_product(product):
    """Announce a newly created visible product in the configured channel."""
    bot = active_bot.get("bot")
    if not bot or not product or not PRODUCTS_CHANNEL_CHAT_ID:
        return False
    if not product.get("active") or product.get("is_test"):
        return False
    try:
        filename = CATEGORY_PHOTOS.get(product.get("category"), "shop.jpg")
        raw_price = product.get("price")
        price = f"{int(raw_price):,} ₽" if isinstance(raw_price, int) and raw_price > 0 else "Цена уточняется"
        caption = (
            "🆕 <b>Новый товар в лавке</b>\n\n"
            f"<b>{escape(str(product.get('name') or 'Товар'))}</b>\n"
            f"Цена: <b>{price}</b>\n"
            f"В наличии: <b>{int(product.get('stock') or 0)} шт.</b>"
        )
        await bot.send_photo(PRODUCTS_CHANNEL_CHAT_ID, photo_ref(filename), caption=caption, parse_mode="HTML")
        return True
    except Exception as error:
        log.warning("New product channel notification failed: %s", error)
        return False


async def _broadcast(bot, recipients, filename, product, markup):
    """Рассылка с ограничением параллельности: не блокирует polling и переживает 429."""
    caption = (
        "🟢 <b>Товар пополнен</b>\n\n"
        f"<b>{escape(str(product['name']))}</b>\n"
        f"Цена: {product['price']:,} ₽\n"
        f"В наличии: {int(product['stock'])} шт"
    )
    limited = asyncio.Semaphore(20)
    sent = 0

    async def one(user_id):
        nonlocal sent
        async with limited:
            for attempt in (1, 2):
                try:
                    await bot.send_photo(user_id, photo_ref(filename), caption=caption, parse_mode="HTML",
                                         reply_markup=markup)
                    sent += 1
                    return
                except TelegramRetryAfter as error:
                    await asyncio.sleep(min(int(error.retry_after) + 1, 30))
                except TelegramForbiddenError:
                    await store.mark_blocked(user_id)
                    return
                except Exception as error:
                    log.info("Restock notice to %s failed: %s", user_id, error)
                    return

    await asyncio.gather(*(one(recipient) for recipient in recipients))
    log.info("Restock notice for %s delivered to %s of %s travelers", product["id"], sent, len(recipients))


# ------------------------------------------------------------------ запуск ---

async def make_app():
    application = web.Application(client_max_size=64 * 1024)
    register_api(application, store, BOT_TOKEN, ADMIN_IDS, SUPPORT_USERNAME, testers=TESTER_IDS,
                 notifier=notify_deliveries, order_notifier=web_order_notice,
                 product_notifier=notify_new_product, panel_token=ADMIN_PANEL_TOKEN)

    async def index(request):
        return web.FileResponse(BASE_DIR / "webapp" / "index.html", headers={"Cache-Control": "no-cache"})

    async def admin_page(request):
        """Full-size panel for a normal browser; the API behind it needs X-Admin-Token."""
        return web.FileResponse(BASE_DIR / "webapp" / "admin.html", headers={"Cache-Control": "no-cache"})

    async def health(request):
        return web.json_response({"ok": True})

    application.router.add_get("/", index)
    application.router.add_get("/admin", admin_page)
    application.router.add_get("/health", health)
    application.router.add_static("/static", BASE_DIR / "webapp", show_index=False)
    return application


async def main():
    web_only = os.environ.get("WEB_ONLY", "").lower() in ("1", "true", "yes")
    if not web_only and not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN on the hosting service. For a local catalog preview use WEB_ONLY=1.")
    await store.connect()
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
                dp.callback_query.outer_middleware(SubscriptionMiddleware())
                # Only /menu is listed: /admin, /add, /id and /cancel stay hidden
                # and answer administrators alone.
                await bot.set_my_commands([BotCommand(command="menu", description="Открыть меню лавки")])
                await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
                await dp.start_polling(bot, close_bot_session=False)
    finally:
        await runner.cleanup()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
