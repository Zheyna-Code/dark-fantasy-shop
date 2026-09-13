# 🕯️ Лавка Странника — Dark Fantasy Telegram Shop

Telegram-бот + Web App в атмосфере тёмного фэнтези: свечи, зелья, реликвии, золотые монеты.

## Структура

- `bot.py` — бот (aiogram 3) + HTTP-сервер (aiohttp), отдаёт Web App и API
- `webapp/index.html` — Web App: каталог, категории, корзина, оформление заказа
- `shop.db` — SQLite (создаётся автоматически, засевается 8 товарами)
- `requirements.txt`

## Локальный запуск

1. Создай бота у @BotFather → получи токен.
2. Установи зависимости: `pip install -r requirements.txt`
3. Установи переменные окружения:
   - `BOT_TOKEN` — токен бота
   - `WEBAPP_URL` — публичный HTTPS-URL (локально: подними `ngrok http 8080` и возьми https-ссылку)
   - `PORT` — порт (по умолчанию 8080)
4. `python bot.py`
5. Открой бота → `/start` → кнопка «Войти в Лавку».

## Деплой на Infrlo (24/7)

1. Залей папку в GitHub-репозиторий.
2. На [dash.infrlo.com/create/app](https://dash.infrlo.com/create/app):
   - Источник: GitHub → выбери репозиторий
   - Start command: `python bot.py`
   - Тип: Web Service (Web App должен быть доступен по HTTPS)
   - Env vars: `BOT_TOKEN=...`, `WEBAPP_URL=https://<твой-домен>.infrlo.app` (URL приложения, который выдаст Infrlo), `PORT` — если Infrlo задаёт сам, оставь как есть
3. После деплоя скопируй публичный URL приложения в `WEBAPP_URL` и перезапусти.

## Как это работает

- Каталог: `GET /api/products` (JSON из SQLite)
- Заказ: `POST /api/order` — проверяет подпись Telegram (`initData`, HMAC-SHA256),
  списывает остатки, сохраняет заказ и шлёт пользователю уведомление в бота
- Оплата: пока «золотые монеты» как витрина. Для реальной оплаты —
  подключить Telegram Payments (provider token у @BotFather) или ЮKassa/CryptoBot.

## Важно для 24/7

- SQLite на серверах с эфемерной ФС (Infrlo и аналоги) **сбрасывается при редеплое**.
  Для продакшена — вынести в PostgreSQL (Neon/Supabase бесплатно).
- Секреты только через env-переменные, токен не коммитить.
