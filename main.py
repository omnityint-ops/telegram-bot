import os
import time
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, Mapping, Any

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

# ==================== GAME CONSTANTS ====================
GAME_SLOTS = "slots"   # 🎰
GAME_DICE  = "dice"    # 🎲 (каждый кидает 3 раза; сумма больше — победа)

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


# ==================== DB LAYER (Postgres) ====================
class DB:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self.init()

    def init(self):
        with self.conn.cursor() as cur:
            # users
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            # queue (добавим поле game)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    user_id BIGINT PRIMARY KEY,
                    stake   INTEGER NOT NULL,
                    game    TEXT    NOT NULL DEFAULT 'slots'
                );
                """
            )
            cur.execute("ALTER TABLE queue ADD COLUMN IF NOT EXISTS game TEXT NOT NULL DEFAULT 'slots';")
            # matches (добавим поле game)
            cur.execute(
                """
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
                    winner_id BIGINT,
                    game TEXT NOT NULL DEFAULT 'slots'
                );
                """
            )
            cur.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS game TEXT NOT NULL DEFAULT 'slots';")

    # ---- Users / Balance ----
    def get_balance(self, user_id: int) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return int(row["balance"]) if row else 0

    def add_balance(self, user_id: int, delta: int):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users(user_id, balance) VALUES(%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET balance = users.balance + EXCLUDED.balance
                """,
                (user_id, delta),
            )

    # ---- Queue ----
    def in_queue(self, user_id: int) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM queue WHERE user_id=%s", (user_id,))
            return cur.fetchone() is not None

    def add_to_queue(self, user_id: int, stake: int, game: str):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO queue(user_id, stake, game) VALUES(%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET stake=EXCLUDED.stake, game=EXCLUDED.game
                """,
                (user_id, stake, game),
            )

    def pop_any_from_queue(self, exclude_user_id: Optional[int], stake: int, game: str) -> Optional[int]:
        with self.conn.cursor() as cur:
            if exclude_user_id:
                cur.execute(
                    "SELECT user_id FROM queue WHERE user_id <> %s AND stake=%s AND game=%s LIMIT 1",
                    (exclude_user_id, stake, game),
                )
            else:
                cur.execute("SELECT user_id FROM queue WHERE stake=%s AND game=%s LIMIT 1", (stake, game))
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
    def create_match(self, p1_id: int, p2_id: int, stake: int, game: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO matches(p1_id, p2_id, stake, game) VALUES(%s,%s,%s,%s) RETURNING id",
                (p1_id, p2_id, stake, game),
            )
            return int(cur.fetchone()["id"])

    def get_match_by_user(self, user_id: int) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM matches
                WHERE (p1_id=%s OR p2_id=%s) AND (winner_id IS NULL)
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, user_id),
            )
            return cur.fetchone()

    def mark_paid_invoice(self, match_id: int, user_id: int):
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
            cur.execute(
                f"UPDATE matches SET {col_paid}=TRUE, {col_src}='balance', {col_deb}=%s WHERE id=%s",
                (amount, match_id,),
            )

    def add_partial_debit(self, match_id: int, user_slot: int, amount: int):
        col_deb = "p1_balance_debited" if user_slot == 1 else "p2_balance_debited"
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE matches SET {col_deb} = {col_deb} + %s WHERE id=%s", (amount, match_id,))

    def get_match_payment_state(self, match_id: int) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT stake, p1_paid, p2_paid, p1_balance_debited, p2_balance_debited, game
                FROM matches WHERE id=%s
                """,
                (match_id,),
            )
            row = cur.fetchone()
            return row if row else None

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
    game: str


def row_to_match(row: Optional[Mapping[str, Any]]) -> Optional['MatchView']:
    if not row:
        return None
    return MatchView(
        id=row["id"], p1_id=row["p1_id"], p2_id=row["p2_id"],
        stake=int(row["stake"]),
        p1_paid=bool(row["p1_paid"]), p2_paid=bool(row["p2_paid"]),
        p1_paid_src=row["p1_paid_src"], p2_paid_src=row["p2_paid_src"],
        active=bool(row["active"]), winner_id=row["winner_id"],
        game=row.get("game", GAME_SLOTS),
    )


def link_user(user_id: int) -> str:
    return f"<a href='tg://user?id={user_id}'>игрок</a>"


def inline_menu(in_queue: bool, in_match: bool) -> InlineKeyboardMarkup:
    buttons = []
    if not in_match:
        buttons.append([InlineKeyboardButton(text="🎰 Слоты", callback_data="mode_slots"),
                        InlineKeyboardButton(text="🎲 Кости", callback_data="mode_dice")])
        if not in_queue:
            buttons.append([InlineKeyboardButton(text="🟢 В очередь", callback_data="queue_join")])
        else:
            buttons.append([InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="queue_leave")])
    else:
        buttons.append([InlineKeyboardButton(text="🚪 Выйти (до старта)", callback_data="queue_leave")])
    buttons.append([InlineKeyboardButton(text="ℹ️ Правила", callback_data="rules")])
    buttons.append([InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="topup_open")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def stake_keyboard(game: str) -> InlineKeyboardMarkup:
    def mk(cb_suffix: str) -> InlineKeyboardMarkup:
        rows = [
            [("10⭐", f"stake_10_{cb_suffix}"), ("25⭐", f"stake_25_{cb_suffix}"), ("50⭐", f"stake_50_{cb_suffix}")],
            [("100⭐", f"stake_100_{cb_suffix}"), ("250⭐", f"stake_250_{cb_suffix}")],
            [("500⭐", f"stake_500_{cb_suffix}"), ("1000⭐", f"stake_1000_{cb_suffix}")],
        ]
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in rows]
        )
    return mk(GAME_SLOTS if game == GAME_SLOTS else GAME_DICE)


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
    input_field_placeholder="Жми /spin или отправь 🎰",
)

roll_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="/roll")]],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Жми /roll или отправь 🎲",
)


def is_forwarded(msg: Message) -> bool:
    return bool(getattr(msg, "forward_date", None) or getattr(msg, "forward_origin", None))


# ==================== GLOBALS ====================
db = DB(DATABASE_URL)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
payments_router = Router()
dp.include_router(payments_router)

last_spin_time: Dict[int, float] = {}
cooldown_tasks: Dict[int, asyncio.Task] = {}

# Состояние для игры КОСТИ: {match_id: {p1:{"cnt":int,"sum":int}, p2:{...}}}
dice_state: Dict[int, Dict[str, Dict[str, int]]] = {}

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
                    await msg.edit_text(f"⏳ Осталось: {remain} сек" if remain else "✅ Можно бросать!")
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


# ==================== SLOT DECODER ====================
SYMBOLS = ("bar", "grapes", "lemon", "seven")


def combo_parts(value: int) -> Tuple[str, str, str]:
    v = value - 1
    left = SYMBOLS[(v // 1) % 4]
    center = SYMBOLS[(v // 4) % 4]
    right = SYMBOLS[(v // 16) % 4]
    return (left, center, right)


def is_triple_bar(value: int) -> bool:
    return value == 1


def is_triple_lemon(value: int) -> bool:
    return value == 43


def is_jackpot_777(value: int) -> bool:
    return value == 64


# ==================== PAY / AUTO-DEBIT ====================
async def try_auto_pay_and_invoice(match_id: int, uid: int, stake: int):
    p1_id, p2_id = db.get_match_players(match_id)
    slot = 1 if uid == p1_id else 2

    state = db.get_match_payment_state(match_id)
    already_debited = int(state["p1_balance_debited"] if slot == 1 else state["p2_balance_debited"]) if state else 0
    need_total = stake - already_debited
    if need_total <= 0:
        db.mark_paid_balance(match_id, slot, stake)
        await bot.send_message(uid, f"✅ Ставка {stake} ⭐ уже оплачена с баланса.")
        return

    bal = db.get_balance(uid)
    use_from_balance = min(bal, need_total)
    if use_from_balance > 0:
        db.add_balance(uid, -use_from_balance)
        db.add_partial_debit(match_id, slot, use_from_balance)

    remaining = need_total - use_from_balance
    if remaining <= 0:
        db.mark_paid_balance(match_id, slot, stake)
        await bot.send_message(uid, f"✅ Ставка {stake} ⭐ списана с твоего баланса.")
        return

    title = f"Доплата до ставки (матч #{match_id})"
    description = f"Автодоплата недостающей части ставки: {remaining} ⭐."
    prices = [LabeledPrice(label=f"+{remaining}⭐", amount=remaining)]
    await bot.send_invoice(
        chat_id=uid,
        title=title,
        description=description,
        payload=f"deficit:{match_id}:{slot}:{remaining}",
        provider_token="",
        currency="XTR",
        prices=prices,
        request_timeout=45,
    )
    await bot.send_message(uid, f"💳 Выставлен счёт на недостающие {remaining} ⭐. После оплаты матч стартует автоматически.")


# ==================== COMMANDS ====================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    mv = row_to_match(db.get_match_by_user(m.from_user.id))
    kb = inline_menu(db.in_queue(m.from_user.id), bool(mv and not mv.winner_id))
    text = (
        "PVP-Арена 1v1!\n\n"
        "Режимы:\n"
        "• 🎰 Слоты — первый, кто выбьет 777, побеждает. BAR-BAR-BAR — проигрыш бросившего.\n"
        "• 🎲 Кости — каждый кидает по 3 раза, у кого сумма больше — тот выиграл.\n\n"
        f"Комиссия — {FEE_PCT}%. Пополнение: /topup. Вывод: /withdraw.\n\n"
        "Жми «🎰 Слоты» или «🎲 Кости», потом выбери ставку."
    )
    await m.answer(text, reply_markup=kb, disable_web_page_preview=True)


@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    bal = db.get_balance(m.from_user.id)
    await m.answer(f"Твой баланс: {bal} ⭐️")



@dp.message(Command("withdraw"))
async def cmd_withdraw(m: Message):
    bal = db.get_balance(m.from_user.id)
    text = (
        f"Твой баланс: {bal} ⭐️

"
        f"Вывод доступен от {MIN_WITHDRAW} ⭐️

"
        "💎 *Уважаемые игроки!*

"
        "Вывод баланса станет доступен с *1 октября*.

"
        "Пожалуйста, проявите немного терпения 🙏
"
        "Все ваши выигрыши надёжно сохраняются на внутреннем счёте и будут доступны для вывода в полном объёме.

"
        "Спасибо за понимание и доверие к нашей платформе!"
    )
    await m.answer(text, parse_mode="Markdown")


@dp.message(Command("paysupport"))
async def cmd_support(m: Message):
    await m.answer("Поддержка по платежам: опиши проблему, приложи ID оплаты и скрин. Возможен refund по правилам Stars.")


@dp.message(Command("join"))
async def cmd_join(m: Message):
    await m.answer(
        "Выбери режим игры:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎰 Слоты", callback_data="mode_slots"),
             InlineKeyboardButton(text="🎲 Кости", callback_data="mode_dice")]
        ])
    )


@dp.message(Command("leave"))
async def cmd_leave(m: Message):
    await queue_leave_impl(m.from_user.id, m)


@dp.message(Command("topup"))
async def cmd_topup(m: Message):
    await m.answer("Выбери сумму пополнения:", reply_markup=topup_keyboard())


# ==================== CALLBACKS: RULES / QUEUE / TOPUP ====================

def parse_stake_game(data: str) -> Tuple[int, str]:
    # stake_100_slots  / stake_50_dice
    _, stake, game = data.split("_")
    return int(stake), game


@dp.callback_query(F.data == "rules")
async def cb_rules(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer(
        "Правила:
"
        f"• Комиссия бота {FEE_PCT}%.
"
        "• 🎰 Слоты: Победа — 777; BAR BAR BAR — проигрыш бросившего; 🍋🍋🍋 — ничья (возврат - комиссия).
"
        "• 🎲 Кости: каждый кидает по 3 раза /roll или 🎲; сумма больше — победа; при равенстве — ничья (возврат - комиссия).
"
        f"• Кулдаун — {COOLDOWN_SEC} сек.
"
        "• Участие — с внутреннего баланса; недостающее авто-доплачивается счётом Stars.
"
        "• Приз зачисляется на внутренний баланс."
    )


@dp.callback_query(F.data == "mode_slots")
async def cb_mode_slots(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer("Выбери ставку для 🎰:", reply_markup=stake_keyboard(GAME_SLOTS))


@dp.callback_query(F.data == "mode_dice")
async def cb_mode_dice(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer("Выбери ставку для 🎲:", reply_markup=stake_keyboard(GAME_DICE))


@dp.callback_query(F.data.startswith("stake_"))
async def cb_stake(cq: CallbackQuery):
    await cq.answer()
    stake, game = parse_stake_game(cq.data)
    if stake not in ALLOWED_STAKES:
        return await cq.message.answer("Некорректная ставка.")

    uid = cq.from_user.id
    mv = row_to_match(db.get_match_by_user(uid))
    if mv and mv.active:
        return await cq.message.answer("Ты уже в активном матче.")

    if db.in_queue(uid):
        db.remove_from_queue(uid)

    opp = db.pop_any_from_queue(exclude_user_id=uid, stake=stake, game=game)
    if opp:
        match_id = db.create_match(p1_id=opp, p2_id=uid, stake=stake, game=game)
        await cq.message.answer(
            f"Найден соперник: {link_user(opp)}.
Готовим старт матча…",
            parse_mode="HTML",
        )
        await bot.send_message(
            opp,
            f"Подключился соперник: {link_user(uid)}.
Готовим старт матча…",
            parse_mode="HTML",
        )
        for pid in (uid, opp):
            await try_auto_pay_and_invoice(match_id, pid, stake)

        if db.can_start(match_id):
            await start_match_flow(match_id)
    else:
        db.add_to_queue(uid, stake, game)
        await cq.message.answer(f"Ты в очереди на матч со ставкой {stake} ⭐ ({'🎰' if game==GAME_SLOTS else '🎲'}). Ждём соперника!")


@dp.callback_query(F.data == "topup_open")
async def cb_topup_open(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer("Выбери сумму пополнения:", reply_markup=topup_keyboard())


# ==================== QUEUE LEAVE ====================
async def queue_leave_impl(uid: int, where: Message):
    mv = row_to_match(db.get_match_by_user(uid))
    if db.in_queue(uid):
        db.remove_from_queue(uid)
        kb = inline_menu(False, bool(mv and not mv.winner_id))
        return await where.answer("Ок, убрал из очереди.", reply_markup=kb)

    if mv and not mv.active and not mv.winner_id:
        state = db.get_match_payment_state(mv.id)
        p1_deb = int(state["p1_balance_debited"]) if state else 0
        p2_deb = int(state["p2_balance_debited"]) if (state and mv.p2_id) else 0

        if p1_deb > 0:
            db.add_balance(mv.p1_id, p1_deb)
        if mv.p2_id and p2_deb > 0:
            db.add_balance(mv.p2_id, p2_deb)

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

        row = db.get_match_by_user(uid)
        mv = row_to_match(row)
        if mv and not mv.active and not mv.winner_id:
            slot = 1 if uid == mv.p1_id else 2
            bal = db.get_balance(uid)
            if bal >= mv.stake:
                db.add_balance(uid, -mv.stake)
                db.mark_paid_balance(mv.id, slot, mv.stake)
                await m.answer(f"✅ Ставка {mv.stake} ⭐ списана с баланса. Ожидаем соперника.")
                if db.can_start(mv.id):
                    await start_match_flow(mv.id)
        return

    if payload.startswith("deficit:" ):
        try:
            _, match_id_str, slot_str, amt_str = payload.split(":")
            match_id = int(match_id_str)
            slot = int(slot_str)
            amt = int(amt_str)
        except Exception:
            return await m.answer("Оплата получена, но не удалось распознать назначение. Обратись в /paysupport.")

        db.add_partial_debit(match_id, slot, amt)
        db.mark_paid_invoice(match_id, m.from_user.id)

        if db.can_start(match_id):
            await start_match_flow(match_id)
        else:
            await m.answer("✅ Оплата принята. Ожидаем соперника.")
        return

    await m.answer("Оплата получена. Если это пополнение — пожалуйста, используйте /topup в следующий раз.")


# ==================== MATCH START (common) ====================
async def start_match_flow(match_id: int):
    db.start_match(match_id)
    p1_id, p2_id = db.get_match_players(match_id)
    last_spin_time.pop(p1_id, None)
    if p2_id:
        last_spin_time.pop(p2_id, None)

    state = db.get_match_payment_state(match_id)
    stake = int(state["stake"]) if state else 0
    game = state["game"] if state else GAME_SLOTS

    if game == GAME_SLOTS:
        text = (
            f"Матч начался (🎰 Слоты)! Ставка {stake} ⭐ (комиссия {FEE_PCT}%). "
            f"Приз: {prize_after_fee(stake)} ⭐. /spin или отправляй 🎰."
        )
        await bot.send_message(p1_id, text, reply_markup=spin_kb)
        if p2_id:
            await bot.send_message(p2_id, text, reply_markup=spin_kb)
    else:
        # Инициализируем состояние костей
        dice_state[match_id] = {
            "p1": {"cnt": 0, "sum": 0},
            "p2": {"cnt": 0, "sum": 0},
        }
        text = (
            f"Матч начался (🎲 Кости)! Ставка {stake} ⭐ (комиссия {FEE_PCT}%). "
            f"Правила: каждый кидает /roll (или отправь 🎲) ровно 3 раза. Больше сумма — победа."
        )
        await bot.send_message(p1_id, text, reply_markup=roll_kb)
        if p2_id:
            await bot.send_message(p2_id, text, reply_markup=roll_kb)


# ==================== GAME: 🎰 /spin ====================
@dp.message(Command("spin"))
async def cmd_spin(m: Message):
    uid = m.from_user.id
    mv = row_to_match(db.get_match_by_user(uid))
    if not mv or mv.winner_id:
        return await m.reply("Матч не найден или уже завершён. /join")
    if not mv.active:
        return await m.reply("Матч ещё не стартовал. Ждём оплату обоих.")
    if mv.game != GAME_SLOTS:
        return await m.reply("Сейчас активен режим 🎲 Кости. Используй /roll или отправь 🎲.")

    if not cooldown_ready(uid):
        await show_cooldown(m.chat.id, uid, COOLDOWN_SEC - int(time.time() - last_spin_time.get(uid, 0)))
        return

    mark_cooldown(uid)
    await show_cooldown(m.chat.id, uid, COOLDOWN_SEC)

    my_msg = await bot.send_dice(m.chat.id, emoji="🎰")

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
            await on_win(opponent_id, mv)
        elif is_triple_lemon(val):
            await on_draw_lemon(mv)


# ==================== GAME: 🎲 /roll ====================
@dp.message(Command("roll"))
async def cmd_roll(m: Message):
    uid = m.from_user.id
    mv = row_to_match(db.get_match_by_user(uid))
    if not mv or mv.winner_id:
        return await m.reply("Матч не найден или уже завершён. /join")
    if not mv.active:
        return await m.reply("Матч ещё не стартовал. Ждём оплату обоих.")
    if mv.game != GAME_DICE:
        return await m.reply("Сейчас активен режим 🎰 Слоты. Используй /spin или отправь 🎰.")

    if not cooldown_ready(uid):
        await show_cooldown(m.chat.id, uid, COOLDOWN_SEC - int(time.time() - last_spin_time.get(uid, 0)))
        return

    mark_cooldown(uid)
    await show_cooldown(m.chat.id, uid, COOLDOWN_SEC)

    # кинем кость
    dmsg = await bot.send_dice(m.chat.id, emoji="🎲")

    opponent_id = mv.p2_id if uid == mv.p1_id else mv.p1_id
    if opponent_id:
        try:
            await bot.send_message(opponent_id, f"{link_user(uid)} бросает кость…", parse_mode="HTML")
            await bot.forward_message(chat_id=opponent_id, from_chat_id=m.chat.id, message_id=dmsg.message_id)
        except Exception:
            pass

    if dmsg.dice:
        await apply_dice_value(uid, mv, dmsg.dice.value)


@dp.message(F.dice)
async def handle_any_dice(m: Message):
    # Слоты/Кости — обрабатываем по активному режиму
    uid = m.from_user.id
    mv = row_to_match(db.get_match_by_user(uid))
    if not mv or mv.winner_id:
        return
    if not mv.active:
        return

    # запрет «читов» — пересланные броски не считаем
    if is_forwarded(m):
        return await m.reply("❌ Пересылать чужие броски запрещено.")

    if mv.game == GAME_SLOTS and m.dice.emoji == DiceEmoji.SLOT_MACHINE:
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
                pass
        val = m.dice.value
        if val == 64:
            await on_win(uid, mv)
        elif val == 1 and opponent_id:
            await on_win(opponent_id, mv)
        elif val == 43:
            await on_draw_lemon(mv)

    elif mv.game == GAME_DICE and m.dice.emoji == DiceEmoji.DICE:
        if not cooldown_ready(uid):
            await show_cooldown(m.chat.id, uid, COOLDOWN_SEC - int(time.time() - last_spin_time.get(uid, 0)))
            return
        mark_cooldown(uid)
        await show_cooldown(m.chat.id, uid, COOLDOWN_SEC)

        opponent_id = mv.p2_id if uid == mv.p1_id else mv.p1_id
        if opponent_id:
            try:
                await bot.send_message(opponent_id, f"{link_user(uid)} бросает кость…", parse_mode="HTML")
                await bot.forward_message(chat_id=opponent_id, from_chat_id=m.chat.id, message_id=m.message_id)
            except Exception:
                try:
                    await bot.send_message(opponent_id, f"{link_user(uid)} выбил: {m.dice.value}", parse_mode="HTML")
                except Exception:
                    pass
        await apply_dice_value(uid, mv, m.dice.value)


async def apply_dice_value(uid: int, mv: MatchView, value: int):
    # инициализация если надо
    st = dice_state.setdefault(mv.id, {"p1": {"cnt": 0, "sum": 0}, "p2": {"cnt": 0, "sum": 0}})
    key = "p1" if uid == mv.p1_id else "p2"
    st[key]["cnt"] += 1
    st[key]["sum"] += int(value)

    left = 3 - st[key]["cnt"]
    try:
        await bot.send_message(uid, f"🎲 Выпало {value}. Прогресс: {st[key]['cnt']}/3 (сумма {st[key]['sum']}).{'' if left<=0 else f' Осталось {left} броска.'}")
    except Exception:
        pass

    # если оба закончили 3/3 — подводим итоги
    if st["p1"]["cnt"] >= 3 and st["p2"]["cnt"] >= 3:
        s1, s2 = st["p1"]["sum"], st["p2"]["sum"]
        winner = None
        if s1 > s2:
            winner = mv.p1_id
        elif s2 > s1:
            winner = mv.p2_id

        if winner:
            await on_win(winner, mv)
        else:
            # ничья (полный матч) — возврат минус комиссия
            await on_draw_dice(mv)
        # очистим состояние
        dice_state.pop(mv.id, None)


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
        f"🎉 Победитель: {link_user(winner_id)}!
"
        f"Приз зачислён: {prize_after_fee(mv.stake)} ⭐️ (ставка {mv.stake}⭐, комиссия {FEE_PCT}%)."
    )
    try:
        await bot.send_message(mv.p1_id, announce, parse_mode="HTML")
        if mv.p2_id:
            await bot.send_message(mv.p2_id, announce, parse_mode="HTML")
    except Exception:
        pass


async def on_draw_lemon(mv: MatchView):
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
                f"Матч завершён ничьёй 🍋🍋🍋.
Возврат: {refund} ⭐ каждому (комиссия {FEE_PCT}% удержана).",
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            pass

    try:
        txt = (
            f"🤝 Ничья: 🍋🍋🍋
"
            f"Каждый получил обратно по {refund} ⭐ (ставка {mv.stake}⭐, комиссия {FEE_PCT}%)."
        )
        await bot.send_message(mv.p1_id, txt)
        if mv.p2_id:
            await bot.send_message(mv.p2_id, txt)
    except Exception:
        pass


async def on_draw_dice(mv: MatchView):
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
                f"Матч завершён ничьёй 🎲.
Возврат: {refund} ⭐ каждому (комиссия {FEE_PCT}% удержана).",
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            pass

    try:
        txt = (
            f"🤝 Ничья в костях
"
            f"Каждый получил обратно по {refund} ⭐ (ставка {mv.stake}⭐, комиссия {FEE_PCT}%)."
        )
        await bot.send_message(mv.p1_id, txt)
        if mv.p2_id:
            await bot.send_message(mv.p2_id, txt)
    except Exception:
        pass


@dp.message(Command("addstars"))
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
    await m.answer(f"✅ Игрок {link_user(uid)} получил {amt} ⭐.
Новый баланс: {new_bal} ⭐", parse_mode="HTML")
    try:
        await bot.send_message(uid, f"💎 Тебе начислено {amt} ⭐.
Текущий баланс: {new_bal} ⭐")
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

