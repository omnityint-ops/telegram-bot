# server.py
import os
import traceback
from fastapi import FastAPI, Request, HTTPException
from aiogram.types import Update

# ВАЖНО: модуль, из которого импортируем bot/dp, не должен запускать polling при импорте!
from main import dp, bot  # только инициализация, без executor/polling

app = FastAPI()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

@app.get("/")
async def home():
    return {"ok": True}

@app.get("/debug/ping")
async def ping():
    return {"ok": True}

@app.get("/debug/me")
async def me():
    m = await bot.get_me()
    return m.model_dump()

@app.post("/webhook")
async def telegram_webhook(request: Request):
    # 1) проверка секрета
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # 2) читаем тело и логируем безопасно
    data = await request.json()
    try:
        chat_id = data.get("message", {}).get("chat", {}).get("id")
        text = data.get("message", {}).get("text")
        print(f"WEBHOOK: chat={chat_id} text={text!r}")
    except Exception:
        pass

    # 3) «мягкий» парсинг и обработка, чтобы не отдавать 500
    try:
        update = Update.model_validate(data)  # aiogram v3
        await dp.feed_update(bot, update)
    except Exception:
        traceback.print_exc()

    # Всегда 200, чтобы Telegram не ретраил
    return {"ok": True}
