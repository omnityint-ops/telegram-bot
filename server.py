# server.py
import os
from fastapi import FastAPI, Request, HTTPException
from aiogram.types import Update

from main import dp, bot  # переиспользуем готовые bot, dp

app = FastAPI()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    # Проверка секрета из заголовка Telegram (рекомендуется)
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}
