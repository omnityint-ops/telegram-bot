# main.py
# -*- coding: utf-8 -*-
"""
PVP Slots Bot — структурированная версия:
- Слои: DB (работа с БД) + GameService (бизнес-логика) + Telegram-хэндлеры.
- Простая FSM матчей: WAITING_OPPONENT -> WAITING_PAYMENT -> ACTIVE -> FINISHED.
- Транзакции на критичных операциях (списание/возврат/закрытие матча).
- «Мягкая» обработка ошибок в хэндлерах (бот не падает).
- Совместимо с вебхуком (dp.feed_update) и polling (if __main__).
"""

import os
import time
import sqlite3
import asyncio
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.enums.dice_emoji import DiceEmoji
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv

# ===================== ENV =====================
load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]

raw_admins = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "")
ADMIN_IDS = {
    int(x) for x in (a.strip() for a in raw_admins.split(","))
    if x.strip().isdigit()
}
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ===================== CONSTANTS =====================
FEE_PCT = 10
DB_PATH = os.getenv("DB_PATH", "game.db")
COOLDOWN_SEC = 5
ALLOWED_STAKES = [10, 25, 50, 100, 250, 500, 1000]
TOPUP_AMOUNTS = [10, 25, 50, 100, 250, 500, 1000]
MIN_WITHDRAW = 400

def prize_after_fee(stake: int) -> int:
    return int(round((stake * 2) * (1 - FEE_PCT / 100)))

def refund_each_after_fee(stake: int) -> int:
    return int(round(stake * (1 - FEE_PCT / 100)))

# ===================== GAME / SLOTS =====================
SYMBOLS = ("bar", "grapes", "lemon", "seven")

def is_triple_bar(value: int) -> bool:   # value 1..64
    return value == 1

def is_triple_lemon(value: int) -> bool:
    return value == 43

def is_jackpot_777(value: int) -> bool:
    return value == 64

# ===================== DB LAYER =====================
class MatchState(IntEnum):
    WAITING_OPPONENT = 0
    WAITING_PAYMENT  = 1
    ACTIVE           = 2
    FINISHED         = 3

@dataclass
class Match:
    id: int
    p1_id: int
    p2_id: Optional[int]
    stake: int
    p1_paid: bool
    p2_paid: bool
    p1_paid_src: Optional[str]
    p2_paid_src: Optional[str]
    state: MatchState
    winner_id: Optional[int]

class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self):
    cur = self.conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA foreign_keys=ON;")

    # --- базовые таблицы ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS queue (
        user_id INTEGER PRIMARY KEY,
        stake   INTEGER NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        p1_id INTEGER NOT NULL,
        p2_id INTEGER,
        stake INTEGER NOT NULL DEFAULT 10,
        p1_paid INTEGER NOT NULL DEFAULT 0,
        p2_paid INTEGER NOT NULL DEFAULT 0,
        p1_paid_src TEXT,
        p2_paid_src TEXT,
        state INTEGER NOT NULL DEFAULT 0,   -- MatchState
        winner_id INTEGER
    );
    """)

    # --- миграции ---
    def ensure_col(table, col, ddl):
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        if col not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    # 1) добавляем колонку state при необходимости
    ensure_col("matches", "state", "INTEGER NOT NULL DEFAULT 0")

    # ВАЖНО: сначала зафиксировать ALTER TABLE,
    # чтобы дальше UPDATE точно видел новую колонку
    self.conn.commit()

    # 2) проставляем корректные состояния для старых записей
    #    (если колонка только что добавлена или была с default=0)
    cur.execute("""
        UPDATE matches
        SET state = CASE
            WHEN winner_id IS NOT NULL        THEN 3  -- FINISHED
            WHEN p2_id IS NULL                THEN 0  -- WAITING_OPPONENT
            WHEN p1_paid=1 AND p2_paid=1      THEN 2  -- ACTIVE
            ELSE 1                                  -- WAITING_PAYMENT
        END
        WHERE state IS NULL OR state=0
    """)

    self.conn.commit()


    # ---------- helpers ----------
    def tx(self):
        """Контекстный менеджер транзакции."""
        return self.conn

    # ---------- users ----------
    def upsert_user(self, user_id: int):
        self.conn.execute(
            "INSERT INTO users(user_id, balance) VALUES(?, 0) ON CONFLICT(user_id) DO NOTHING",
            (user_id,)
        )

    def get_balance(self, user_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return int(row["balance"]) if row else 0

    def add_balance(self, user_id: int, delta: int):
        with self.tx():
            self.upsert_user(user_id)
            self.conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))

    def all_user_ids(self) -> List[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT user_id FROM users")
        return [int(r["user_id"]) for r in cur.fetchall()]

    def delete_user(self, user_id: int):
        self.conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))

    # ---------- queue ----------
    def in_queue(self, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT 1 FROM queue WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None

    def queue_add(self, user_id: int, stake: int):
        self.conn.execute("INSERT OR REPLACE INTO queue(user_id, stake) VALUES(?,?)", (user_id, stake))

    def queue_remove(self, user_id: int):
        self.conn.execute("DELETE FROM queue WHERE user_id=?", (user_id,))

    def queue_pop_opponent(self, exclude_user_id: int, stake: int) -> Optional[int]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT user_id FROM queue WHERE user_id != ? AND stake=? LIMIT 1",
            (exclude_user_id, stake)
        )
        row = cur.fetchone()
        if not row:
            return None
        opp = int(row["user_id"])
        self.queue_remove(opp)
        return opp

    # ---------- matches ----------
    def create_match(self, p1_id: int, p2_id: Optional[int], stake: int) -> int:
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO matches(p1_id, p2_id, stake, state) VALUES(?,?,?,?)""",
                    (p1_id, p2_id, stake, int(MatchState.WAITING_OPPONENT)))
        self.conn.commit()
        return int(cur.lastrowid)

    def match_for_user(self, user_id: int) -> Optional[Match]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT * FROM matches
            WHERE (p1_id=? OR p2_id=?) AND state != ?
            ORDER BY id DESC LIMIT 1
        """, (user_id, user_id, int(MatchState.FINISHED)))
        row = cur.fetchone()
        return self._row_to_match(row) if row else None

    def load_match(self, match_id: int) -> Optional[Match]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM matches WHERE id=?", (match_id,))
        row = cur.fetchone()
        return self._row_to_match(row) if row else None

    def assign_opponent_and_wait_payment(self, match_id: int, p2_id: int):
        self.conn.execute(
            "UPDATE matches SET p2_id=?, state=? WHERE id=?",
            (p2_id, int(MatchState.WAITING_PAYMENT), match_id)
        )

    def mark_paid_balance(self, match_id: int, user_slot: int, amount: int):
        if user_slot == 1:
            self.conn.execute("UPDATE matches SET p1_paid=1, p1_paid_src='balance' WHERE id=?", (match_id,))
        else:
            self.conn.execute("UPDATE matches SET p2_paid=1, p2_paid_src='balance' WHERE id=?", (match_id,))

    def can_start(self, match_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT p1_paid, p2_paid, state FROM matches WHERE id=?", (match_id,))
        r = cur.fetchone()
        if not r:
            return False
        return bool(r["p1_paid"]) and bool(r["p2_paid"]) and int(r["state"]) == int(MatchState.WAITING_PAYMENT)

    def start_match(self, match_id: int):
        self.conn.execute("UPDATE matches SET state=? WHERE id=?", (int(MatchState.ACTIVE), match_id))

    def finish_match(self, match_id: int, winner_id: int):
        self.conn.execute("UPDATE matches SET state=?, winner_id=? WHERE id=?",
                          (int(MatchState.FINISHED), winner_id, match_id))

    def cancel_match_refund_if_needed(self, match_id: int):
        """Возврат средств, если успели списать с баланса до старта."""
        cur = self.conn.cursor()
        cur.execute("SELECT p1_id, p2_id, p1_paid, p2_paid, stake, state FROM matches WHERE id=?", (match_id,))
        r = cur.fetchone()
        if not r:
            return
        stake = int(r["stake"])
        if int(r["state"]) == int(MatchState.FINISHED):
            return
        if r["p1_paid"]:
            self.add_balance(int(r["p1_id"]), stake)
        if r["p2_paid"] and r["p2_id"]:
            self.add_balance(int(r["p2_id"]), stake)
        self.conn.execute("UPDATE matches SET state=?, winner_id=? WHERE id=?",
                          (int(MatchState.FINISHED), 0, match_id))

    def _row_to_match(self, row: sqlite3.Row) -> Match:
        return Match(
            id=int(row["id"]),
            p1_id=int(row["p1_id"]),
            p2_id=(int(row["p2_id"]) if row["p2_id"] is not None else None),
            stake=int(row["stake"]),
            p1_paid=bool(row["p1_paid"]),
            p2_paid=bool(row["p2_paid"]),
            p1_paid_src=row["p1_paid_src"],
            p2_paid_src=row["p2_paid_src"],
            state=MatchState(int(row["state"])),
            winner_id=(int(row["winner_id"]) if row["winner_id"] is not None else None)
        )

# ===================== SERVICE LAYER =====================
class GameService:
    def __init__(self, db: DB, bot: Bot):
        self.db = db
        self.bot = bot
        self.last_spin_time: Dict[int, float] = {}
        self.cooldown_tasks: Dict[int, asyncio.Task] = {}

    # ----- UI helpers -----
    @staticmethod
    def link_user(user_id: int) -> str:
        return f"<a href='tg://user?id={user_id}'>игрок</a>"

    @staticmethod
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

    @staticmethod
    def stake_keyboard() -> InlineKeyboardMarkup:
        rows = [
            [("10⭐", "stake_10"), ("25⭐", "stake_25"), ("50⭐", "stake_50")],
            [("100⭐", "stake_100"), ("250⭐", "stake_250")],
            [("500⭐", "stake_500"), ("1000⭐", "stake_1000")],
        ]
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in rows]
        )

    @staticmethod
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

    # ----- cooldown -----
    def cooldown_ready(self, user_id: int) -> bool:
        now = time.time()
        return (now - self.last_spin_time.get(user_id, 0)) >= COOLDOWN_SEC

    def mark_cooldown(self, user_id: int):
        self.last_spin_time[user_id] = time.time()

    async def show_cooldown(self, chat_id: int, user_id: int, seconds: int = COOLDOWN_SEC):
        task = self.cooldown_tasks.get(user_id)
        if task and not task.done():
            task.cancel()

        async def _runner():
            try:
                msg = await self.bot.send_message(chat_id, f"⏳ Осталось: {seconds} сек")
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

        self.cooldown_tasks[user_id] = asyncio.create_task(_runner())

    # ----- queue / matching -----
    async def join_queue(self, user_id: int, stake: int, message: Message):
        # если уже есть активный/ожидающий матч — запрещаем
        mv = self.db.match_for_user(user_id)
        if mv and mv.state == MatchState.ACTIVE:
            await message.answer("Ты уже в активном матче.")
            return

        # убираем из очереди и ищем соперника
        if self.db.in_queue(user_id):
            self.db.queue_remove(user_id)

        opp = self.db.queue_pop_opponent(exclude_user_id=user_id, stake=stake)
        if opp is None:
            # создаём «полуматч» (p2_id = NULL) и ставим в ожидание соперника
            match_id = self.db.create_match(p1_id=user_id, p2_id=None, stake=stake)
            self.db.queue_add(user_id, stake)
            await message.answer(f"Ты в очереди на матч со ставкой {stake} ⭐. Ждём соперника!")
            return

        # соперник найден: создаём матч и переводим в WAITING_PAYMENT
        match_id = self.db.create_match(p1_id=opp, p2_id=user_id, stake=stake)
        self.db.assign_opponent_and_wait_payment(match_id, user_id)
        await message.answer(f"Найден соперник: {self.link_user(opp)}.\nОплатите участие (спишем с баланса).", parse_mode="HTML")
        await self.bot.send_message(opp, f"Подключился соперник: {self.link_user(user_id)}.\nОплатите участие (спишем с баланса).", parse_mode="HTML")

        # автоматическое списание, если хватает баланса
        await self.try_auto_debit(match_id, user_id, stake)
        await self.try_auto_debit(match_id, opp, stake)

        if self.db.can_start(match_id):
            self.db.start_match(match_id)
            m = self.db.load_match(match_id)
            await self.notify_match_start(m)

    async def leave_queue_or_cancel(self, user_id: int, where: Message):
        mv = self.db.match_for_user(user_id)

        if self.db.in_queue(user_id):
            self.db.queue_remove(user_id)
            kb = self.inline_menu(False, bool(mv and mv.state != MatchState.FINISHED))
            await where.answer("Ок, убрал из очереди.", reply_markup=kb)
            return

        if mv and mv.state == MatchState.WAITING_PAYMENT and mv.winner_id is None:
            # матч не стартовал — отменяем и возвращаем, если списывали
            self.db.cancel_match_refund_if_needed(mv.id)
            await where.answer("Матч отменён до старта. Средства (если списывались) возвращены.")
            other = mv.p2_id if user_id == mv.p1_id else mv.p1_id
            if other:
                try:
                    await self.bot.send_message(other, "Соперник покинул матч до старта. Вернись в очередь: /join")
                except Exception:
                    pass
            return

        if mv and mv.state == MatchState.ACTIVE:
            await where.answer("Матч уже идёт — выход невозможен.")
            return

        await where.answer("Ты не в очереди и не в матче.")

    # ----- payments -----
    async def try_auto_debit(self, match_id: int, uid: int, stake: int):
        bal = self.db.get_balance(uid)
        if bal >= stake:
            with self.db.tx():
                self.db.add_balance(uid, -stake)
                # определить слот
                m = self.db.load_match(match_id)
                slot = 1 if uid == m.p1_id else 2
                self.db.mark_paid_balance(match_id, slot, stake)
            try:
                await self.bot.send_message(uid, f"✅ Ставка {stake} ⭐ списана с твоего баланса.")
            except Exception:
                pass
        else:
            try:
                await self.bot.send_message(
                    uid,
                    "Недостаточно средств на балансе для участия в матче.\n"
                    f"Нужно: {stake} ⭐, у тебя: {bal} ⭐.\n"
                    "Пополнить можно от 10⭐ через /topup.",
                    reply_markup=self.topup_keyboard()
                )
            except Exception:
                pass

    async def handle_success_topup(self, uid: int, amt: int, message: Message):
        self.db.add_balance(uid, amt)
        new_bal = self.db.get_balance(uid)
        await message.answer(f"🎉 Баланс пополнен на {amt} ⭐. Текущий баланс: {new_bal} ⭐.")

        mv = self.db.match_for_user(uid)
        if mv and mv.state == MatchState.WAITING_PAYMENT and mv.winner_id is None:
            stake = mv.stake
            bal = self.db.get_balance(uid)
            if bal >= stake:
                with self.db.tx():
                    self.db.add_balance(uid, -stake)
                    slot = 1 if uid == mv.p1_id else 2
                    self.db.mark_paid_balance(mv.id, slot, stake)
                await message.answer(f"✅ Ставка {stake} ⭐ списана с баланса. Ожидаем соперника.")
                if self.db.can_start(mv.id):
                    self.db.start_match(mv.id)
                    m2 = self.db.load_match(mv.id)
                    await self.notify_match_start(m2)

    async def notify_match_start(self, m: Match):
        # сброс кулдауна
        self.last_spin_time.pop(m.p1_id, None)
        if m.p2_id:
            self.last_spin_time.pop(m.p2_id, None)

        text = (
            f"Матч начался! Ставка {m.stake} ⭐ (комиссия {FEE_PCT}%). "
            f"Приз: {prize_after_fee(m.stake)} ⭐. /spin или отправляй 🎰."
        )
        await self.bot.send_message(m.p1_id, text, reply_markup=self.spin_kb)
        if m.p2_id:
            await self.bot.send_message(m.p2_id, text, reply_markup=self.spin_kb)

    # ----- game flow -----
    async def spin(self, uid: int, message: Message):
        mv = self.db.match_for_user(uid)
        if not mv or mv.winner_id is not None or mv.state != MatchState.ACTIVE:
            await message.reply("Матч не найден или уже завершён. /join")
            return

        if not self.cooldown_ready(uid):
            remain = COOLDOWN_SEC - int(time.time() - self.last_spin_time.get(uid, 0))
            await self.show_cooldown(message.chat.id, uid, max(1, remain))
            return

        self.mark_cooldown(uid)
        await self.show_cooldown(message.chat.id, uid, COOLDOWN_SEC)

        # свой бросок
        my_msg = await self.bot.send_dice(message.chat.id, emoji="🎰")

        opponent_id = mv.p2_id if uid == mv.p1_id else mv.p1_id
        if opponent_id:
            try:
                await self.bot.send_message(opponent_id, f"{self.link_user(uid)} крутит барабан…", parse_mode="HTML")
                await self.bot.forward_message(chat_id=opponent_id, from_chat_id=message.chat.id, message_id=my_msg.message_id)
            except Exception:
                pass

        if my_msg.dice:
            v = my_msg.dice.value
            if is_jackpot_777(v):
                await self.on_win(uid, mv)
            elif is_triple_bar(v) and opponent_id:
                await self.on_win(opponent_id, mv)
            elif is_triple_lemon(v):
                await self.on_draw_lemon(mv)

    async def spin_from_user_dice(self, m: Message):
        if m.dice.emoji != DiceEmoji.SLOT_MACHINE:
            return
        # запрет пересылок
        if bool(getattr(m, "forward_date", None) or getattr(m, "forward_origin", None)):
            await m.reply("❌ Пересылать чужие броски запрещено. Отправь свой 🎰 или используй /spin.")
            return

        uid = m.from_user.id
        mv = self.db.match_for_user(uid)
        if not mv or mv.winner_id is not None or mv.state != MatchState.ACTIVE:
            await m.reply("Матч не найден или уже завершён. /join")
            return

        if not self.cooldown_ready(uid):
            remain = COOLDOWN_SEC - int(time.time() - self.last_spin_time.get(uid, 0))
            await self.show_cooldown(m.chat.id, uid, max(1, remain))
            return

        self.mark_cooldown(uid)
        await self.show_cooldown(m.chat.id, uid, COOLDOWN_SEC)

        opponent_id = mv.p2_id if uid == mv.p1_id else mv.p1_id
        if opponent_id:
            try:
                await self.bot.send_message(opponent_id, f"{self.link_user(uid)} крутит барабан…", parse_mode="HTML")
                await self.bot.forward_message(chat_id=opponent_id, from_chat_id=m.chat.id, message_id=m.message_id)
            except Exception:
                try:
                    await self.bot.send_message(opponent_id, f"{self.link_user(uid)} выбил значение: {m.dice.value}", parse_mode="HTML")
                except Exception:
                    pass

        if m.dice:
            v = m.dice.value
            if is_jackpot_777(v):
                await self.on_win(uid, mv)
            elif is_triple_bar(v) and opponent_id:
                await self.on_win(opponent_id, mv)
            elif is_triple_lemon(v):
                await self.on_draw_lemon(mv)

    async def on_win(self, winner_id: int, mv: Match):
        with self.db.tx():
            self.db.finish_match(mv.id, winner_id)
            self.db.add_balance(winner_id, prize_after_fee(mv.stake))

        # гашим кулдауны
        for pid in (mv.p1_id, mv.p2_id):
            self.last_spin_time.pop(pid, None)
            task = self.cooldown_tasks.get(pid)
            if task and not task.done():
                task.cancel()

        try:
            await self.bot.send_message(mv.p1_id, "Игра окончена. /join чтобы сыграть ещё.", reply_markup=ReplyKeyboardRemove())
            if mv.p2_id:
                await self.bot.send_message(mv.p2_id, "Игра окончена. /join чтобы сыграть ещё.", reply_markup=ReplyKeyboardRemove())
        except Exception:
            pass

        announce = (
            f"🎉 Победитель: {self.link_user(winner_id)}!\n"
            f"Приз зачислён: {prize_after_fee(mv.stake)} ⭐️ (ставка {mv.stake}⭐, комиссия {FEE_PCT}%)."
        )
        try:
            await self.bot.send_message(mv.p1_id, announce, parse_mode="HTML")
            if mv.p2_id:
                await self.bot.send_message(mv.p2_id, announce, parse_mode="HTML")
        except Exception:
            pass

    async def on_draw_lemon(self, mv: Match):
        refund = refund_each_after_fee(mv.stake)
        with self.db.tx():
            self.db.finish_match(mv.id, 0)
            if mv.p1_id:
                self.db.add_balance(mv.p1_id, refund)
            if mv.p2_id:
                self.db.add_balance(mv.p2_id, refund)

        for pid in (mv.p1_id, mv.p2_id):
            self.last_spin_time.pop(pid, None)
            task = self.cooldown_tasks.get(pid)
            if task and not task.done():
                task.cancel()

        for pid in (mv.p1_id, mv.p2_id):
            try:
                await self.bot.send_message(
                    pid,
                    f"Матч завершён ничьёй 🍋🍋🍋.\nВозврат: {refund} ⭐ каждому (комиссия {FEE_PCT}% удержана).",
                    reply_markup=ReplyKeyboardRemove()
                )
            except Exception:
                pass

        try:
            txt = (f"🤝 Ничья: 🍋🍋🍋\n"
                   f"Каждый получил обратно по {refund} ⭐ (ставка {mv.stake}⭐, комиссия {FEE_PCT}%).")
            await self.bot.send_message(mv.p1_id, txt)
            if mv.p2_id:
                await self.bot.send_message(mv.p2_id, txt)
        except Exception:
            pass

# ===================== TELEGRAM BOT =====================
db = DB(DB_PATH)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
svc = GameService(db, bot)

router = Router()
dp.include_router(router)

@router.message(Command("start"))
async def cmd_start(m: Message):
    mv = db.match_for_user(m.from_user.id)
    kb = svc.inline_menu(db.in_queue(m.from_user.id), bool(mv and mv.state != MatchState.FINISHED))
    text = (
        "🎰 PVP-Game 1v1!\n\n"
        "Правила:\n"
        "• Побеждает тот, кто первым выбьет 777.\n"
        "• BAR BAR BAR — мгновенный проигрыш бросившего.\n"
        f"• Комиссия — {FEE_PCT}%.\n\n"
        "Как играть:\n"
        "1) Жми «🟢 В очередь» и выбери ставку. При подборе соперника обоим спишем ставку с внутреннего баланса.\n"
        "2) После старта крути /spin (или отправляй свой 🎰). "
        "Твой бросок у тебя справа, у соперника слева.\n"
        f"3) КД между спинами — {COOLDOWN_SEC} сек (бот покажет таймер)."
    )
    await m.answer(text, reply_markup=kb, disable_web_page_preview=True)

@router.message(Command("balance"))
async def cmd_balance(m: Message):
    await m.answer(f"Твой призовой баланс: {db.get_balance(m.from_user.id)} ⭐️")

@router.message(Command("withdraw"))
async def cmd_withdraw(m: Message):
    bal = db.get_balance(m.from_user.id)
    text = (
        f"Твой баланс: {bal} ⭐️\n\n"
        f"Вывод доступен от {MIN_WITHDRAW} ⭐️\n\n"
        "💎 *Уважаемые игроки!*\n\n"
        "Вывод баланса станет доступен с *1 октября*.\n\n"
        "Пожалуйста, проявите немного терпения 🙏\n"
        "Все ваши выигрыши надёжно сохраняются на внутреннем счёте и будут доступны для вывода в полном объёме."
    )
    await m.answer(text, parse_mode="Markdown")

@router.message(Command("paysupport"))
async def cmd_support(m: Message):
    await m.answer("Поддержка по платежам: опиши проблему, приложи ID оплаты и скрин. Возможен refund по правилам Stars.")

@router.message(Command("join"))
async def cmd_join(m: Message):
    await m.answer("Выбери ставку для матча:", reply_markup=svc.stake_keyboard())

@router.message(Command("leave"))
async def cmd_leave(m: Message):
    await svc.leave_queue_or_cancel(m.from_user.id, m)

@router.message(Command("topup"))
async def cmd_topup(m: Message):
    await m.answer("Выбери сумму пополнения:", reply_markup=svc.topup_keyboard())

def _stake_from_cb(data: str) -> Optional[int]:
    try:
        return int(data.split("_")[1])
    except Exception:
        return None

@router.callback_query(F.data == "rules")
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

@router.callback_query(F.data == "queue_join")
async def cb_queue_join(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer("Выбери ставку для матча:", reply_markup=svc.stake_keyboard())

@router.callback_query(F.data == "queue_leave")
async def cb_queue_leave(cq: CallbackQuery):
    await cq.answer()
    await svc.leave_queue_or_cancel(cq.from_user.id, cq.message)

@router.callback_query(F.data.startswith("stake_"))
async def cb_stake(cq: CallbackQuery):
    await cq.answer()
    stake = _stake_from_cb(cq.data)
    if not stake or stake not in ALLOWED_STAKES:
        return await cq.message.answer("Некорректная ставка.")
    await svc.join_queue(cq.from_user.id, stake, cq.message)

@router.callback_query(F.data == "topup_open")
async def cb_topup_open(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer("Выбери сумму пополнения:", reply_markup=svc.topup_keyboard())

def _parse_topup_amount(data: str) -> Optional[int]:
    try:
        return int(data.split("_")[1])
    except Exception:
        return None

@router.callback_query(F.data.startswith("topup_"))
async def cb_topup(cq: CallbackQuery):
    await cq.answer()
    amt = _parse_topup_amount(cq.data)
    if not amt or amt not in TOPUP_AMOUNTS:
        return await cq.message.answer("Некорректная сумма пополнения.")
    title = f"Пополнение баланса (+{amt}⭐)"
    description = f"Пополнение внутреннего баланса на {amt} ⭐."
    prices = [LabeledPrice(label=f"{amt}⭐", amount=amt)]
    await bot.send_invoice(
        chat_id=cq.from_user.id, title=title, description=description,
        payload=f"topup:{cq.from_user.id}:{amt}",
        provider_token="", currency="XTR", prices=prices, request_timeout=45
    )

payments_router = Router()
dp.include_router(payments_router)

@payments_router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@payments_router.message(F.successful_payment)
async def on_success_payment(m: Message):
    uid = m.from_user.id
    payload = (m.successful_payment and m.successful_payment.invoice_payload) or ""
    if payload.startswith("topup:"):
        try:
            _, uid_str, amt_str = payload.split(":")
            amt = int(amt_str)
            if amt not in TOPUP_AMOUNTS:
                return await m.answer("Получено пополнение с некорректной суммой.")
            await svc.handle_success_topup(uid, amt, m)
        except Exception:
            return await m.answer("Оплата получена, но не удалось обработать пополнение. Обратись в /paysupport.")
        return
    await m.answer("Оплата получена. Если это пополнение — используй /topup в следующий раз.")

@router.message(Command("spin"))
async def cmd_spin(m: Message):
    await svc.spin(m.from_user.id, m)

@router.message(F.dice)
async def handle_any_dice(m: Message):
    # только слот-машина
    if m.dice.emoji != DiceEmoji.SLOT_MACHINE:
        return
    await svc.spin_from_user_dice(m)

# ----- admin -----
@router.message(Command("allbalances"))
async def cmd_allbalances(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Доступ запрещён.")
    ids = db.all_user_ids()
    if not ids:
        return await m.answer("Балансов пока нет.")
    # простая выдача (чтобы не спамить)
    parts = []
    for uid in ids:
        parts.append(f"👤 {svc.link_user(uid)} — {db.get_balance(uid)} ⭐️")
    await m.answer("📊 <b>Баланс всех игроков:</b>\n\n" + "\n".join(parts), parse_mode="HTML")

@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Нет доступа")
    text = m.text.partition(" ")[2].strip()
    if not text:
        return await m.answer("Формат: /broadcast <текст>")
    user_ids = db.all_user_ids()
    sent = 0
    removed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except (TelegramForbiddenError, TelegramBadRequest):
            db.delete_user(uid)
            removed += 1
        except Exception:
            pass
    await m.answer(f"✅ Рассылка завершена. Отправлено: {sent}, удалено неактивных: {removed}")

@router.message(Command("whoami"))
async def cmd_whoami(m: Message):
    await m.answer(f"Твой user_id: {m.from_user.id}\nАдмины: {sorted(ADMIN_IDS)}")

@router.message(Command("envcheck"))
async def cmd_envcheck(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Доступ запрещён.")
    await m.answer(f"ADMIN_IDS: {sorted(ADMIN_IDS)}")

# ===================== ENTRY (polling для локалки) =====================
if __name__ == "__main__":
    async def main():
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    asyncio.run(main())
