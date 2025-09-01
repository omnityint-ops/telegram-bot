import os
import time
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, Mapping, Any  # добавь Mapping, Any

import psycopg
from psycopg.rows import dict_row

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.enums.dice_emoji import DiceEmoji
from dotenv import load_dotenv

# ==================== ENV ====================
load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]


# несколько админов
raw_admins = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "")
ADMIN_IDS = {
    int(x) for x in (a.strip() for a in raw_admins.split(","))
    if x.strip().isdigit()
}
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ==================== CONSTANTS ====================
FEE_PCT = 10
COOLDOWN_SEC = 5
ALLOWED_STAKES = [10, 25, 50, 100, 250, 500, 1000]
TOPUP_AMOUNTS = [10, 25, 50, 100, 250, 500, 1000]
MIN_WITHDRAW = 400

def prize_after_fee(stake: int) -> int:
    # победитель получает 2*stake за вычетом комиссии
    return int(round((stake * 2) * (1 - FEE_PCT / 100)))

def refund_each_after_fee(stake: int) -> int:
    # ничья: каждому возвращаем stake минус комиссия
    return int(round(stake * (1 - FEE_PCT / 100)))

# ==================== DB LAYER ====================
# ==================== DB LAYER (Postgres) ====================
class DB:
    def __init__(self, dsn: str):
        # единое подключение; курсоры словарные
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self.init()

    def init(self):
        with self.conn.cursor() as cur:
            # users
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0
            );
            """)
            # queue
            cur.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                user_id BIGINT PRIMARY KEY,
                stake   INTEGER NOT NULL
            );
            """)
            # matches
            cur.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id BIGSERIAL PRIMARY KEY,
                p1_id BIGINT NOT NULL,
                p2_id BIGINT,
                stake INTEGER NOT NULL DEFAULT 10,
                p1_paid BOOLEAN NOT NULL DEFAULT FALSE,
                p2_paid BOOLEAN NOT NULL DEFAULT FALSE,
                p1_paid_src TEXT,
                p2_paid_src TEXT,
                p1_balance_debited INTEGER NOT NULL DEFAULT 0,
                p2_balance_debited INTEGER NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT FALSE,
                winner_id BIGINT
            );
            """)

    # ---- Users / Balance ----
    def get_balance(self, user_id: int) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return int(row["balance"]) if row else 0

    def add_balance(self, user_id: int, delta: int):
        with self.conn.cursor() as cur:
            # upsert
            cur.execute("""
                INSERT INTO users(user_id, balance) VALUES(%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET balance = users.balance + EXCLUDED.balance
            """, (user_id, delta))

    # ---- Queue ----
    def in_queue(self, user_id: int) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM queue WHERE user_id=%s", (user_id,))
            return cur.fetchone() is not None

    def add_to_queue(self, user_id: int, stake: int):
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO queue(user_id, stake) VALUES(%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET stake=EXCLUDED.stake
            """, (user_id, stake))

    def pop_any_from_queue(self, exclude_user_id: Optional[int], stake: int) -> Optional[int]:
        with self.conn.cursor() as cur:
            if exclude_user_id:
                cur.execute("SELECT user_id FROM queue WHERE user_id <> %s AND stake=%s LIMIT 1",
                            (exclude_user_id, stake))
            else:
                cur.execute("SELECT user_id FROM queue WHERE stake=%s LIMIT 1", (stake,))
            row = cur.fetchone()
            if not row:
                return None
            opp = int(row["user_id"])
            cur.execute("DELETE FROM queue WHERE user_id=%s", (opp,))
            return opp

    def remove_from_queue(self, user_id: int):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM queue WHERE user_id=%s", (user_id,))

    # ---- Matches ----
    def create_match(self, p1_id: int, p2_id: int, stake: int) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO matches(p1_id, p2_id, stake) VALUES(%s,%s,%s) RETURNING id",
                (p1_id, p2_id, stake)
            )
            return int(cur.fetchone()["id"])

    def get_match_by_user(self, user_id: int) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM matches
                WHERE (p1_id=%s OR p2_id=%s) AND (winner_id IS NULL)
                ORDER BY id DESC LIMIT 1
            """, (user_id, user_id))
            return cur.fetchone()

    def mark_paid_invoice(self, match_id: int, user_id: int):
        # на будущее — совместимость
        with self.conn.cursor() as cur:
            cur.execute("SELECT p1_id, p2_id FROM matches WHERE id=%s", (match_id,))
            row = cur.fetchone()
            if not row:
                return
            if user_id == row["p1_id"]:
                cur.execute("UPDATE matches SET p1_paid=TRUE, p1_paid_src='invoice' WHERE id=%s", (match_id,))
            elif user_id == row["p2_id"]:
                cur.execute("UPDATE matches SET p2_paid=TRUE, p2_paid_src='invoice' WHERE id=%s", (match_id,))

    def mark_paid_balance(self, match_id: int, user_slot: int, amount: int):
        col_paid = "p1_paid" if user_slot == 1 else "p2_paid"
        col_src  = "p1_paid_src" if user_slot == 1 else "p2_paid_src"
        col_deb  = "p1_balance_debited" if user_slot == 1 else "p2_balance_debited"
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE matches SET {col_paid}=TRUE, {col_src}='balance', {col_deb}=%s WHERE id=%s",
                        (amount, match_id,))

    def get_flags(self, match_id: int) -> Tuple[bool, bool, bool]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT p1_paid, p2_paid, active FROM matches WHERE id=%s", (match_id,))
            row = cur.fetchone()
            return (bool(row["p1_paid"]), bool(row["p2_paid"]), bool(row["active"])) if row else (False, False, False)

    def can_start(self, match_id: int) -> bool:
        p1, p2, active = self.get_flags(match_id)
        return p1 and p2 and not active

    def start_match(self, match_id: int):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE matches SET active=TRUE WHERE id=%s", (match_id,))

    def set_winner_and_close(self, match_id: int, winner_id: int):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE matches SET winner_id=%s, active=FALSE WHERE id=%s", (winner_id, match_id))

    def get_paid_sources(self, match_id: int) -> Tuple[Optional[str], Optional[str], int]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT p1_paid_src, p2_paid_src, stake FROM matches WHERE id=%s", (match_id,))
            row = cur.fetchone()
            if not row:
                return None, None, 0
            return row["p1_paid_src"], row["p2_paid_src"], int(row["stake"])

    def get_match_players(self, match_id: int) -> Tuple[int, Optional[int]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT p1_id, p2_id FROM matches WHERE id=%s", (match_id,))
            row = cur.fetchone()
            return int(row["p1_id"]), (int(row["p2_id"]) if row["p2_id"] is not None else None)


# ==================== MODELS / HELPERS ====================
@dataclass
class MatchView:
    id: int
    p1_id: int
    p2_id: Optional[int]
    stake: int
    p1_paid: bool
    p2_paid: bool
    p1_paid_src: Optional[str]
    p2_paid_src: Optional[str]
    active: bool
    winner_id: Optional[int]

def row_to_match(row: Optional[Mapping[str, Any]]) -> Optional['MatchView']:
    if not row:
        return None
    return MatchView(
        id=row["id"], p1_id=row["p1_id"], p2_id=row["p2_id"],
        stake=int(row["stake"]),
        p1_paid=bool(row["p1_paid"]), p2_paid=bool(row["p2_paid"]),
        p1_paid_src=row["p1_paid_src"], p2_paid_src=row["p2_paid_src"],
        active=bool(row["active"]), winner_id=row["winner_id"],
    )

def link_user(user_id: int) -> str:
    return f"<a href='tg://user?id={user_id}'>игрок</a>"

def inline_menu(in_queue: bool, in_match: bool) -> InlineKeyboardMarkup:
    buttons = []
    if not in_match:
        if not in_queue:
            buttons.append([InlineKeyboardButton(text="🟢 В очередь", callback_data="queue_join")])
        else:
            buttons.append([InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="queue_leave")])
    else:
        buttons.append([InlineKeyboardButton(text="🚪 Выйти (до старта)", callback_data="queue_leave")])
    buttons.append([InlineKeyboardButton(text="ℹ️ Правила", callback_data="rules")])
    buttons.append([InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="topup_open")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def stake_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [("10⭐", "stake_10"), ("25⭐", "stake_25"), ("50⭐", "stake_50")],
        [("100⭐", "stake_100"), ("250⭐", "stake_250")],
        [("500⭐", "stake_500"), ("1000⭐", "stake_1000")],
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in rows]
    )

def topup_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [("10⭐", "topup_10"), ("25⭐", "topup_25"), ("50⭐", "topup_50")],
        [("100⭐", "topup_100"), ("250⭐", "topup_250")],
        [("500⭐", "topup_500"), ("1000⭐", "topup_1000")],
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in rows]
    )

spin_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="/spin")]],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Жми /spin или отправь 🎰"
)

def is_forwarded(msg: Message) -> bool:
    # если сообщение переслано — у него есть forward_date / forward_origin
    return bool(getattr(msg, "forward_date", None) or getattr(msg, "forward_origin", None))

# ==================== GLOBALS ====================
db = DB(DATABASE_URL)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
payments_router = Router()
dp.include_router(payments_router)

last_spin_time: Dict[int, float] = {}
cooldown_tasks: Dict[int, asyncio.Task] = {}

# ==================== VISUAL COOLDOWN ====================
async def show_cooldown(chat_id: int, user_id: int, seconds: int = COOLDOWN_SEC):
    task = cooldown_tasks.get(user_id)
    if task and not task.done():
        task.cancel()

    async def _runner():
        try:
            msg = await bot.send_message(chat_id, f"⏳ Осталось: {seconds} сек")
            remain = seconds
            while remain > 0:
                await asyncio.sleep(1)
                remain -= 1
                try:
                    await msg.edit_text(f"⏳ Осталось: {remain} сек" if remain else "✅ Можно крутить!")
                except Exception:
                    pass
            await asyncio.sleep(1)
            try:
                await msg.delete()
            except Exception:
                pass
        except asyncio.CancelledError:
            try:
                await msg.delete()
            except Exception:
                pass
            raise

    cooldown_tasks[user_id] = asyncio.create_task(_runner())

def cooldown_ready(user_id: int) -> bool:
    now = time.time()
    return (now - last_spin_time.get(user_id, 0)) >= COOLDOWN_SEC

def mark_cooldown(user_id: int):
    last_spin_time[user_id] = time.time()

# ==================== SLOT DECODER (правильный) ====================
# Telegram slot value: 1..64, где value-1 раскладывается в base-4
# 0=bar, 1=grapes, 2=lemon, 3=seven
SYMBOLS = ("bar", "grapes", "lemon", "seven")

def combo_parts(value: int) -> Tuple[str, str, str]:
    v = value - 1
    left   = SYMBOLS[(v // 1)  % 4]
    center = SYMBOLS[(v // 4)  % 4]
    right  = SYMBOLS[(v // 16) % 4]
    return (left, center, right)

def is_triple_bar(value: int) -> bool:
    return value == 1  # bar-bar-bar

def is_triple_lemon(value: int) -> bool:
    return value == 43  # lemon-lemon-lemon

def is_jackpot_777(value: int) -> bool:
    return value == 64  # seven-seven-seven

# ==================== PAY / AUTO-DEBIT ====================
async def try_auto_pay_and_invoice(match_id: int, uid: int, stake: int):
    """
    Новая логика: участие только с внутреннего баланса.
    Если хватает — списываем и помечаем как оплачено.
    Если не хватает — показываем клавиатуру пополнения /topup.
    """
    p1_id, p2_id = db.get_match_players(match_id)
    slot = 1 if uid == p1_id else 2

    bal = db.get_balance(uid)
    if bal >= stake:
        db.add_balance(uid, -stake)
        db.mark_paid_balance(match_id, slot, stake)
        await bot.send_message(uid, f"✅ Ставка {stake} ⭐ списана с твоего баланса.")
    else:
        need = stake - bal
        await bot.send_message(
            uid,
            "Недостаточно средств на балансе для участия в матче.\n"
            f"Нужно: {stake} ⭐, у тебя: {bal} ⭐.\n"
            "Пополнить можно от 10⭐ через /topup.",
            reply_markup=topup_keyboard()
        )

# ==================== COMMANDS ====================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    mv = row_to_match(db.get_match_by_user(m.from_user.id))
    kb = inline_menu(db.in_queue(m.from_user.id), bool(mv and not mv.winner_id))
    text = (
        "🎰 PVP-Game 1v1!\n\n"
        "Правила:\n"
        "• Побеждает тот, кто первым выбьет 777.\n"
        "• Bar-Bar-Bar — мгновенный проигрыш того, кто выбил.\n"
        f"• Комиссия — {FEE_PCT}%.\n\n"
        "Как играть:\n"
        "1. Жми «🟢 В очередь» и выбери ставку, при подборе соперника обоим приходит оплата: "
        "списание с баланса или инвойс.\n"
        "2. 🎰 После старта крути /spin (или отправляй свой 🎰). "
        "Твой бросок у тебя справа, у соперника слева.\n"
        f"3. КД между спинами — {COOLDOWN_SEC} сек (бот покажет таймер).\n\n"
        "Удачи на Арене!"
    )
    await m.answer(text, reply_markup=kb, disable_web_page_preview=True)

@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    bal = db.get_balance(m.from_user.id)
    await m.answer(f"Твой призовой баланс: {bal} ⭐️")

@dp.message(Command("withdraw"))
async def cmd_withdraw(m: Message):
    bal = db.get_balance(m.from_user.id)
    text = (
        f"Твой баланс: {bal} ⭐️\n\n"
        f"Вывод доступен от {MIN_WITHDRAW} ⭐️\n\n"
        "💎 *Уважаемые игроки!*\n\n"
        "Вывод баланса станет доступен с *1 октября*.\n\n"
        "Пожалуйста, проявите немного терпения 🙏\n"
        "Все ваши выигрыши надёжно сохраняются на внутреннем счёте и будут доступны для вывода в полном объёме.\n\n"
        "Спасибо за понимание и доверие к нашей платформе!"
    )
    await m.answer(text, parse_mode="Markdown")

@dp.message(Command("paysupport"))
async def cmd_support(m: Message):
    await m.answer("Поддержка по платежам: опиши проблему, приложи ID оплаты и скрин. Возможен refund по правилам Stars.")

@dp.message(Command("join"))
async def cmd_join(m: Message):
    await m.answer("Выбери ставку для матча:", reply_markup=stake_keyboard())

@dp.message(Command("leave"))
async def cmd_leave(m: Message):
    await queue_leave_impl(m.from_user.id, m)

@dp.message(Command("topup"))
async def cmd_topup(m: Message):
    await m.answer("Выбери сумму пополнения:", reply_markup=topup_keyboard())

# ==================== CALLBACKS: RULES / QUEUE / TOPUP ====================
def stake_from_cb(data: str) -> int:
    return int(data.split("_")[1])

@dp.callback_query(F.data == "rules")
async def cb_rules(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer(
        "Правила:\n"
        f"• Комиссия бота {FEE_PCT}%.\n"
        "• Победа — 777; BAR BAR BAR — проигрыш бросившего.\n"
        "• Lemon–Lemon–Lemon — ничья: каждому возврат ставки за вычетом комиссии.\n"
        f"• Кулдаун — {COOLDOWN_SEC} сек.\n"
        "• Участие — только с внутреннего баланса (пополнение /topup).\n"
        "• Приз зачисляется на внутренний баланс."
    )

@dp.callback_query(F.data == "queue_join")
async def cb_queue_join(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer("Выбери ставку для матча:", reply_markup=stake_keyboard())

@dp.callback_query(F.data == "queue_leave")
async def cb_queue_leave(cq: CallbackQuery):
    await cq.answer()
    await queue_leave_impl(cq.from_user.id, cq.message)

@dp.callback_query(F.data.startswith("stake_"))
async def cb_stake(cq: CallbackQuery):
    await cq.answer()
    stake = stake_from_cb(cq.data)
    if stake not in ALLOWED_STAKES:
        return await cq.message.answer("Некорректная ставка.")

    uid = cq.from_user.id
    mv = row_to_match(db.get_match_by_user(uid))
    if mv and mv.active:
        return await cq.message.answer("Ты уже в активном матче.")

    if db.in_queue(uid):
        db.remove_from_queue(uid)

    opp = db.pop_any_from_queue(exclude_user_id=uid, stake=stake)
    if opp:
        match_id = db.create_match(p1_id=opp, p2_id=uid, stake=stake)
        await cq.message.answer(
            f"Найден соперник: {link_user(opp)}.\nПроизведите оплату для начала матча…",
            parse_mode="HTML"
        )
        await bot.send_message(
            opp,
            f"Подключился соперник: {link_user(uid)}.\nПроизведите оплату для начала матча…",
            parse_mode="HTML"
        )
        # Пытаемся списать у обоих; если не хватает — покажем клаву пополнения
        for pid in (uid, opp):
            await try_auto_pay_and_invoice(match_id, pid, stake)

        # Если оба оплатили — стартуем
        if db.can_start(match_id):
            db.start_match(match_id)
            mrow = db.get_match_by_user(uid)
            mv2 = row_to_match(mrow)
            last_spin_time.pop(mv2.p1_id, None)
            if mv2.p2_id:
                last_spin_time.pop(mv2.p2_id, None)
            text = (
                f"Матч начался! Ставка {mv2.stake} ⭐ (комиссия {FEE_PCT}%). "
                f"Приз: {prize_after_fee(mv2.stake)} ⭐. /spin или отправляй 🎰."
            )
            await bot.send_message(mv2.p1_id, text, reply_markup=spin_kb)
            if mv2.p2_id:
                await bot.send_message(mv2.p2_id, text, reply_markup=spin_kb)
    else:
        db.add_to_queue(uid, stake)
        await cq.message.answer(f"Ты в очереди на матч со ставкой {stake} ⭐. Ждём соперника!")

@dp.callback_query(F.data == "topup_open")
async def cb_topup_open(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer("Выбери сумму пополнения:", reply_markup=topup_keyboard())

def parse_topup_amount(data: str) -> Optional[int]:
    try:
        return int(data.split("_")[1])
    except Exception:
        return None

@dp.callback_query(F.data.startswith("topup_"))
async def cb_topup(cq: CallbackQuery):
    await cq.answer()
    amt = parse_topup_amount(cq.data)
    if not amt or amt not in TOPUP_AMOUNTS:
        return await cq.message.answer("Некорректная сумма пополнения.")
    uid = cq.from_user.id
    title = f"Пополнение баланса (+{amt}⭐)"
    description = f"Пополнение внутреннего баланса на {amt} ⭐."
    prices = [LabeledPrice(label=f"{amt}⭐", amount=amt)]
    await bot.send_invoice(
        chat_id=uid, title=title, description=description,
        payload=f"topup:{uid}:{amt}",
        provider_token="", currency="XTR", prices=prices, request_timeout=45
    )

# ==================== QUEUE LEAVE ====================
async def queue_leave_impl(uid: int, where: Message):
    mv = row_to_match(db.get_match_by_user(uid))
    if db.in_queue(uid):
        db.remove_from_queue(uid)
        kb = inline_menu(False, bool(mv and not mv.winner_id))
        return await where.answer("Ок, убрал из очереди.", reply_markup=kb)

    if mv and not mv.active and not mv.winner_id:
        p1_src, p2_src, stake = db.get_paid_sources(mv.id)
        if p1_src == 'balance':
            db.add_balance(mv.p1_id, stake)
        if p2_src == 'balance' and mv.p2_id:
            db.add_balance(mv.p2_id, stake)
        db.set_winner_and_close(mv.id, winner_id=0)
        await where.answer("Матч отменён до старта. Средства с баланса возвращены.")
        other = mv.p2_id if uid == mv.p1_id else mv.p1_id
        if other:
            await bot.send_message(other, "Соперник покинул матч до старта. Вернись в очередь: /join")
        return

    if mv and mv.active and not mv.winner_id:
        return await where.answer("Матч уже идёт — выход невозможен.")
    await where.answer("Ты не в очереди и не в матче.")

# ==================== PAYMENTS (STARS) ====================
@payments_router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@payments_router.message(F.successful_payment)
async def on_success_payment(m: Message):
    uid = m.from_user.id
    payload = (m.successful_payment and m.successful_payment.invoice_payload) or ""

    # 1) Пополнение
    if payload.startswith("topup:"):
        try:
            _, uid_str, amt_str = payload.split(":")
            amt = int(amt_str)
            if amt not in TOPUP_AMOUNTS:
                return await m.answer("Получено пополнение с некорректной суммой.")
            db.add_balance(uid, amt)
            new_bal = db.get_balance(uid)
            await m.answer(f"🎉 Баланс пополнен на {amt} ⭐. Текущий баланс: {new_bal} ⭐.")
        except Exception:
            return await m.answer("Оплата получена, но не удалось обработать пополнение. Обратись в /paysupport.")

        # Если есть матч в ожидании — пробуем списать ставку и, при возможности, стартовать
        row = db.get_match_by_user(uid)
        mv = row_to_match(row)
        if mv and not mv.active and not mv.winner_id:
            # определим слот
            slot = 1 if uid == mv.p1_id else 2
            bal = db.get_balance(uid)
            if bal >= mv.stake:
                db.add_balance(uid, -mv.stake)
                db.mark_paid_balance(mv.id, slot, mv.stake)
                await m.answer(f"✅ Ставка {mv.stake} ⭐ списана с баланса. Ожидаем соперника.")
                if db.can_start(mv.id):
                    db.start_match(mv.id)
                    last_spin_time.pop(mv.p1_id, None)
                    if mv.p2_id:
                        last_spin_time.pop(mv.p2_id, None)
                    text = (
                        f"Матч начался! Ставка {mv.stake} ⭐ (комиссия {FEE_PCT}%). "
                        f"Приз: {prize_after_fee(mv.stake)} ⭐. /spin или отправляй 🎰."
                    )
                    await bot.send_message(mv.p1_id, text, reply_markup=spin_kb)
                    if mv.p2_id:
                        await bot.send_message(mv.p2_id, text, reply_markup=spin_kb)
        return

    # 2) Старые payload-ы (совместимость). Сейчас мы не используем инвойсы на «доплату» для матчей.
    # Просто сообщим пользователю.
    await m.answer("Оплата получена. Если это пополнение — пожалуйста, используйте /topup в следующий раз.")

# ==================== GAME: /spin ====================
@dp.message(Command("spin"))
async def cmd_spin(m: Message):
    uid = m.from_user.id
    mv = row_to_match(db.get_match_by_user(uid))
    if not mv or mv.winner_id:
        return await m.reply("Матч не найден или уже завершён. /join")
    if not mv.active:
        return await m.reply("Матч ещё не стартовал. Ждём оплату обоих со внутреннего баланса.")

    if not cooldown_ready(uid):
        await show_cooldown(m.chat.id, uid, COOLDOWN_SEC - int(time.time() - last_spin_time.get(uid, 0)))
        return

    mark_cooldown(uid)
    await show_cooldown(m.chat.id, uid, COOLDOWN_SEC)

    # отправляем 🎰 В ЧАТ ИГРОКА -> видно СПРАВА у него
    my_msg = await bot.send_dice(m.chat.id, emoji="🎰")

    # пересылаем ОППОНЕНТУ -> у него это будет слева
    opponent_id = mv.p2_id if uid == mv.p1_id else mv.p1_id
    if opponent_id:
        try:
            await bot.send_message(opponent_id, f"{link_user(uid)} крутит барабан…", parse_mode="HTML")
            await bot.forward_message(chat_id=opponent_id, from_chat_id=m.chat.id, message_id=my_msg.message_id)
        except Exception:
            pass

    if my_msg.dice:
        val = my_msg.dice.value
        if is_jackpot_777(val):
            await on_win(uid, mv)
        elif is_triple_bar(val) and opponent_id:
            # BAR BAR BAR — мгновенный проигрыш бросившего ⇒ победитель соперник
            await on_win(opponent_id, mv)
        elif is_triple_lemon(val):
            # 🍋🍋🍋 — ничья с удержанием комиссии
            await on_draw_lemon(mv)

# ==================== GAME: user-sent 🎰 ====================
@dp.message(F.dice)
async def handle_any_dice(m: Message):
    if m.dice.emoji != DiceEmoji.SLOT_MACHINE:
        return

    # запрет «читов» — пересланные броски не считаем
    if is_forwarded(m):
        return await m.reply("❌ Пересылать чужие броски запрещено. Отправь свой 🎰 или используй /spin.")

    uid = m.from_user.id
    mv = row_to_match(db.get_match_by_user(uid))
    if not mv or mv.winner_id:
        return await m.reply("Матч не найден или уже завершён. /join")
    if not mv.active:
        return await m.reply("Матч ещё не стартовал. Ждём оплату обоих со внутреннего баланса.")

    if not cooldown_ready(uid):
        await show_cooldown(m.chat.id, uid, COOLDOWN_SEC - int(time.time() - last_spin_time.get(uid, 0)))
        return

    mark_cooldown(uid)
    await show_cooldown(m.chat.id, uid, COOLDOWN_SEC)

    opponent_id = mv.p2_id if uid == mv.p1_id else mv.p1_id
    if opponent_id:
        try:
            await bot.send_message(opponent_id, f"{link_user(uid)} крутит барабан…", parse_mode="HTML")
            await bot.forward_message(chat_id=opponent_id, from_chat_id=m.chat.id, message_id=m.message_id)
        except Exception:
            try:
                await bot.send_message(opponent_id, f"{link_user(uid)} выбил значение: {m.dice.value}", parse_mode="HTML")
            except Exception:
                pass

    if m.dice:
        val = m.dice.value
        if is_jackpot_777(val):
            await on_win(uid, mv)
        elif is_triple_bar(val) and opponent_id:
            await on_win(opponent_id, mv)
        elif is_triple_lemon(val):
            await on_draw_lemon(mv)

# ==================== WIN / DRAW LOGIC ====================
async def on_win(winner_id: int, mv: MatchView):
    db.set_winner_and_close(mv.id, winner_id)
    db.add_balance(winner_id, prize_after_fee(mv.stake))

    for pid in (mv.p1_id, mv.p2_id):
        last_spin_time.pop(pid, None)
        task = cooldown_tasks.get(pid)
        if task and not task.done():
            task.cancel()
        try:
            await bot.send_message(pid, "Игра окончена. /join чтобы сыграть ещё.", reply_markup=ReplyKeyboardRemove())
        except Exception:
            pass

    announce = (
        f"🎉 Победитель: {link_user(winner_id)}!\n"
        f"Приз зачислён: {prize_after_fee(mv.stake)} ⭐️ (ставка {mv.stake}⭐, комиссия {FEE_PCT}%)."
    )
    try:
        await bot.send_message(mv.p1_id, announce, parse_mode="HTML")
        if mv.p2_id:
            await bot.send_message(mv.p2_id, announce, parse_mode="HTML")
    except Exception:
        pass

async def on_draw_lemon(mv: MatchView):
    # ничья на 🍋🍋🍋: закрываем матч и возвращаем каждому stake - fee%
    db.set_winner_and_close(mv.id, winner_id=0)
    refund = refund_each_after_fee(mv.stake)

    if mv.p1_id:
        db.add_balance(mv.p1_id, refund)
    if mv.p2_id:
        db.add_balance(mv.p2_id, refund)

    for pid in (mv.p1_id, mv.p2_id):
        last_spin_time.pop(pid, None)
        task = cooldown_tasks.get(pid)
        if task and not task.done():
            task.cancel()
        try:
            await bot.send_message(
                pid,
                f"Матч завершён ничьёй 🍋🍋🍋.\nВозврат: {refund} ⭐ каждому (комиссия {FEE_PCT}% удержана).",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception:
            pass

    try:
        txt = (f"🤝 Ничья: 🍋🍋🍋\n"
               f"Каждый получил обратно по {refund} ⭐ (ставка {mv.stake}⭐, комиссия {FEE_PCT}%).")
        await bot.send_message(mv.p1_id, txt)
        if mv.p2_id:
            await bot.send_message(mv.p2_id, txt)
    except Exception:
        pass


@dp.message(Command("addstars")))
async def cmd_addstars(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Нет доступа")
    parts = m.text.split()
    if len(parts) != 3:
        return await m.answer("Формат: /addstars <user_id> <amount>")
    try:
        uid = int(parts[1]); amt = int(parts[2])
    except ValueError:
        return await m.answer("user_id и amount должны быть числами")
    db.add_balance(uid, amt)
    new_bal = db.get_balance(uid)
    await m.answer(f"✅ Игрок {link_user(uid)} получил {amt} ⭐.\nНовый баланс: {new_bal} ⭐", parse_mode="HTML")
    try:
        await bot.send_message(uid, f"💎 Тебе начислено {amt} ⭐.\nТекущий баланс: {new_bal} ⭐")
    except Exception:
        pass




# ==================== ADMIN ====================
@dp.message(Command("allbalances"))
async def cmd_allbalances(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Доступ запрещён.")
    with db.conn.cursor() as cur:
        cur.execute("SELECT user_id, balance FROM users ORDER BY balance DESC")
        rows = cur.fetchall()
    if not rows:
        return await m.answer("Балансов пока нет.")
    lines = ["📊 <b>Баланс всех игроков:</b>", ""]
    for r in rows:
        lines.append(f"👤 {link_user(r['user_id'])} — {r['balance']} ⭐️")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("whoami"))
async def cmd_whoami(m: Message):
    await m.answer(f"Твой user_id: {m.from_user.id}\nАдмины: {sorted(ADMIN_IDS)}")

@dp.message(Command("envcheck"))
async def cmd_envcheck(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Доступ запрещён.")
    await m.answer(f"Загружены ADMIN_IDS: {sorted(ADMIN_IDS)}")

# ==================== ENTRY ====================
if __name__ == "__main__":
    async def main():
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    asyncio.run(main())
