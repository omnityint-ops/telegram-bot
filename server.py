# server.py
import os, traceback
from fastapi import FastAPI, Request, HTTPException
from aiogram.types import Update
from main import dp, bot  # важно: в main.py polling запускается ТОЛЬКО под if __name__ == "__main__"

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
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")

    data = await request.json()
    # лёгкий лог входа
    try:
        chat_id = data.get("message", {}).get("chat", {}).get("id")
        text = data.get("message", {}).get("text")
        print(f"WEBHOOK: chat={chat_id} text={text!r}")
    except Exception:
        pass

    # НЕ роняем 500 ни при парсинге, ни при обработке
    try:
        update = Update.model_validate(data)  # aiogram v3 + pydantic v2
        await dp.feed_update(bot, update)
    except Exception:
        traceback.print_exc()

    return {"ok": True}
