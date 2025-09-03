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
    ReplyKeyboardRemove
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
ALLOWED_STAKES = [15, 25, 50, 100, 250, 500, 1000]
TOPUP_AMOUNTS = [15, 25, 50, 100, 250, 500, 1000]
MIN_WITHDRAW = 400
REF_REWARD = 10


def prize_after_fee(stake: int) -> int:
    # победитель получает 2*stake за вычетом комиссии
    return int(round((stake * 2) * (1 - FEE_PCT / 100)))


def refund_each_after_fee(stake: int) -> int:
    # ничья: каждому возвращаем stake минус комиссия
    return int(round(stake * (1 - FEE_PCT / 100)))


# ==================== GAME MODES ====================
DEFAULT_MODE = "slots"  # 'slots' (🎰 777) или 'dice3' (🎲 3 броска)
MODE_LABEL = {
    "slots": "🎰 777",
    "dice3": "🎲 3 броска",
}


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
            # ---- миграции для режимов и 3-бросков ----
            cur.execute("ALTER TABLE queue   ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'slots';")
            cur.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS game_mode TEXT NOT NULL DEFAULT 'slots';")
            cur.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS p1_dice_sum INTEGER NOT NULL DEFAULT 0;")
            cur.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS p2_dice_sum INTEGER NOT NULL DEFAULT 0;")
            cur.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS p1_dice_cnt INTEGER NOT NULL DEFAULT 0;")
            cur.execute("ALTER TABLE matches ADD COLUMN IF NOT EXISTS p2_dice_cnt INTEGER NOT NULL DEFAULT 0;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT;")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_rewarded BOOLEAN NOT NULL DEFAULT FALSE;")

    # ---- Users / Balance ----
    def get_balance(self, user_id: int) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return int(row["balance"]) if row else 0

    def add_balance(self, user_id: int, delta: int):
        with self.conn.cursor() as cur:
            # upsert
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

    def add_to_queue(self, user_id: int, stake: int, mode: str = "slots"):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO queue(user_id, stake, mode) VALUES(%s,%s,%s)
                ON CONFLICT (user_id) DO UPDATE SET stake=EXCLUDED.stake, mode=EXCLUDED.mode
                """,
                (user_id, stake, mode),
            )

    def pop_any_from_queue(self, exclude_user_id: Optional[int], stake: int, mode: str = "slots") -> Optional[int]:
        with self.conn.cursor() as cur:
            if exclude_user_id:
                cur.execute(
                    "SELECT user_id FROM queue WHERE user_id <> %s AND stake=%s AND mode=%s LIMIT 1",
                    (exclude_user_id, stake, mode),
                )
            else:
                cur.execute("SELECT user_id FROM queue WHERE stake=%s AND mode=%s LIMIT 1", (stake, mode))
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
    def create_match(self, p1_id: int, p2_id: int, stake: int, game_mode: str = "slots") -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO matches(p1_id, p2_id, stake, game_mode) VALUES(%s,%s,%s,%s) RETURNING id",
                (p1_id, p2_id, stake, game_mode),
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
        # отмечаем как оплачено инвойсом
        with self.conn.cursor() as cur:
            cur.execute("SELECT p1_id, p2_id FROM matches WHERE id=%s", (match_id,))
            row = cur.fetchone()
            if not row:
                return
            if user_id == row["p1_id"]:
                cur.execute(
                    "UPDATE matches SET p1_paid=TRUE, p1_paid_src='invoice' WHERE id=%s",
                    (match_id,),
                )
            elif user_id == row["p2_id"]:
                cur.execute(
                    "UPDATE matches SET p2_paid=TRUE, p2_paid_src='invoice' WHERE id=%s",
                    (match_id,),
                )

    def mark_paid_balance(self, match_id: int, user_slot: int, amount: int):
        col_paid = "p1_paid" if user_slot == 1 else "p2_paid"
        col_src = "p1_paid_src" if user_slot == 1 else "p2_paid_src"
        col_deb = "p1_balance_debited" if user_slot == 1 else "p2_balance_debited"
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE matches SET {col_paid}=TRUE, {col_src}='balance', {col_deb}=%s WHERE id=%s",
                (amount, match_id,),
            )

    def add_partial_debit(self, match_id: int, user_slot: int, amount: int):
        col_deb = "p1_balance_debited" if user_slot == 1 else "p2_balance_debited"
        with self.conn.cursor() as cur:
            cur.execute(
                f"UPDATE matches SET {col_deb} = {col_deb} + %s WHERE id=%s",
                (amount, match_id,),
            )

    def get_match_payment_state(self, match_id: int) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT stake, p1_paid, p2_paid, p1_balance_debited, p2_balance_debited
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
            return (
                bool(row["p1_paid"]),
                bool(row["p2_paid"]),
                bool(row["active"]),
            ) if row else (False, False, False)

    def can_start(self, match_id: int) -> bool:
        p1, p2, active = self.get_flags(match_id)
        return p1 and p2 and not active

    def start_match(self, match_id: int):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE matches SET active=TRUE WHERE id=%s", (match_id,))

    def set_winner_and_close(self, match_id: int, winner_id: int):
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE matches SET winner_id=%s, active=FALSE WHERE id=%s",
                (winner_id, match_id),
            )

    def get_paid_sources(self, match_id: int) -> Tuple[Optional[str], Optional[str], int]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT p1_paid_src, p2_paid_src, stake FROM matches WHERE id=%s",
                (match_id,),
            )
            row = cur.fetchone()
            if not row:
                return None, None, 0
            return row["p1_paid_src"], row["p2_paid_src"], int(row["stake"])

    def get_match_players(self, match_id: int) -> Tuple[int, Optional[int]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT p1_id, p2_id FROM matches WHERE id=%s", (match_id,))
            row = cur.fetchone()
            return int(row["p1_id"]), (int(row["p2_id"]) if row["p2_id"] is not None else None)

    # ------- Dice3 helpers -------
    def get_dice_state(self, match_id: int) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT p1_dice_sum, p2_dice_sum, p1_dice_cnt, p2_dice_cnt, game_mode
                FROM matches WHERE id=%s
            """, (match_id,))
            return cur.fetchone()

    def add_dice_throw(self, match_id: int, slot: int, value: int) -> bool:
        """
        Добавляет бросок ТОЛЬКО если у игрока ещё < 3 бросков.
        Возвращает True, если апдейт произошёл (бросок засчитан), иначе False.
        """
        sum_col = "p1_dice_sum" if slot == 1 else "p2_dice_sum"
        cnt_col = "p1_dice_cnt" if slot == 1 else "p2_dice_cnt"
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE matches
                   SET {sum_col} = {sum_col} + %s,
                       {cnt_col} = {cnt_col} + 1
                 WHERE id = %s AND {cnt_col} < 3
                """,
                (value, match_id),
            )
            return cur.rowcount > 0

    def set_referrer_if_empty(self, user_id: int, referrer_id: int):
        # нельзя рефералить самого себя
        if user_id == referrer_id:
            return
        with self.conn.cursor() as cur:
            # создаём пользователя, если его ещё нет (баланс 0)
            cur.execute("""
                INSERT INTO users(user_id, balance, referrer_id)
                VALUES(%s, 0, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET referrer_id = COALESCE(users.referrer_id, EXCLUDED.referrer_id)
            """, (user_id, referrer_id))

    def get_referral_state(self, user_id: int) -> Tuple[Optional[int], bool]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT referrer_id, ref_rewarded FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            if not row:
                return None, False
            return (row["referrer_id"], bool(row["ref_rewarded"]))

    def mark_ref_rewarded(self, user_id: int):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE users SET ref_rewarded=TRUE WHERE user_id=%s", (user_id,))

    def count_referrals(self, referrer_id: int) -> Tuple[int, int]:
        # (всего с меткой, из них вознаграждённых)
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE referrer_id=%s", (referrer_id,))
            total = int(cur.fetchone()["c"])
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE referrer_id=%s AND ref_rewarded=TRUE", (referrer_id,))
            rewarded = int(cur.fetchone()["c"])
            return total, rewarded


    def award_referral_if_eligible(self, user_id: int) -> Optional[int]:
        """
        Если у user_id есть referrer_id и ещё не платили награду — платим рефереру +10⭐ и помечаем.
        Возвращает ID реферера, если выплатили; иначе None.
        """
        ref_id, rewarded = self.get_referral_state(user_id)
        if not ref_id or rewarded:
            return None
        # платим
        self.add_balance(ref_id, REF_REWARD)
        self.mark_ref_rewarded(user_id)
        return ref_id


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
    game_mode: str
    p1_dice_sum: int = 0
    p2_dice_sum: int = 0
    p1_dice_cnt: int = 0
    p2_dice_cnt: int = 0


def row_to_match(row: Optional[Mapping[str, Any]]) -> Optional['MatchView']:
    if not row:
        return None
    return MatchView(
        id=row["id"],
        p1_id=row["p1_id"],
        p2_id=row["p2_id"],
        stake=int(row["stake"]),
        p1_paid=bool(row["p1_paid"]),
        p2_paid=bool(row["p2_paid"]),
        p1_paid_src=row["p1_paid_src"],
        p2_paid_src=row["p2_paid_src"],
        active=bool(row["active"]),
        winner_id=row["winner_id"],
        game_mode=row.get("game_mode", "slots"),
        p1_dice_sum=int(row.get("p1_dice_sum", 0)),
        p2_dice_sum=int(row.get("p2_dice_sum", 0)),
        p1_dice_cnt=int(row.get("p1_dice_cnt", 0)),
        p2_dice_cnt=int(row.get("p2_dice_cnt", 0)),
    )


def link_user(user_id: int) -> str:
    return f"<a href='tg://user?id={user_id}'>игрок</a>"


# выбранный режим на пользователя
user_mode: Dict[int, str] = {}  # uid -> 'slots' | 'dice3'


def get_user_mode(uid: int) -> str:
    return user_mode.get(uid, DEFAULT_MODE)


def set_user_mode(uid: int, mode: str):
    user_mode[uid] = mode


def inline_menu(in_queue: bool, in_match: bool, mode: str = "slots") -> InlineKeyboardMarkup:
    buttons = []
    if not in_match:
        if not in_queue:
            buttons.append([InlineKeyboardButton(text="🟢 В очередь", callback_data="queue_join")])
        else:
            buttons.append([InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="queue_leave")])
    else:
        buttons.append([InlineKeyboardButton(text="🚪 Выйти (до старта)", callback_data="queue_leave")])
    buttons.append([InlineKeyboardButton(text=f"🎮 Режим: {MODE_LABEL.get(mode, mode)}", callback_data="modes_open")])
    buttons.append([InlineKeyboardButton(text="ℹ️ Правила", callback_data="rules")])
    buttons.append([InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="topup_open")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def stake_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [("15⭐", "stake_15"), ("25⭐", "stake_25"), ("50⭐", "stake_50")],
        [("100⭐", "stake_100"), ("250⭐", "stake_250")],
        [("500⭐", "stake_500"), ("1000⭐", "stake_1000")],
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in rows]
    )


def topup_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [("15⭐", "topup_15"), ("25⭐", "topup_25"), ("50⭐", "topup_50")],
        [("100⭐", "topup_100"), ("250⭐", "topup_250")],
        [("500⭐", "topup_500"), ("1000⭐", "topup_1000")],
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in rows]
    )


async def edit_or_send(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """
    Пытаемся отредактировать текущее сообщение (с инлайн-кнопками).
    Если редактировать нельзя (не наше/удалено/тот же текст) — отправляем новое.
    Всегда используем HTML, чтобы корректно показывать link_user().
    """
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        await message.answer(
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )




def is_forwarded(msg: Message) -> bool:
    # если сообщение переслано — у него есть forward_date / forward_origin
    return bool(getattr(msg, "forward_date", None) or getattr(msg, "forward_origin", None))


# ==================== GLOBALS ====================
from aiogram.client.session.aiohttp import AiohttpSession

db = DB(DATABASE_URL)

# увеличим таймаут HTTP запросов к Telegram (например, до 30 сек)
session = AiohttpSession(timeout=30)
bot = Bot(BOT_TOKEN, session=session)

dp = Dispatcher()
payments_router = Router()
dp.include_router(payments_router)

# кэш username бота (чтобы не вызывать get_me() в хэндлерах)
BOT_USERNAME: Optional[str] = None
# таймеры/кулдауны — должны существовать на уровне модуля ДО первых хэндлеров
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
                    await msg.edit_text(f"⏳ Осталось: {remain} сек" if remain else "✅ Можно крутить/бросать!")
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
    left = SYMBOLS[(v // 1) % 4]
    center = SYMBOLS[(v // 4) % 4]
    right = SYMBOLS[(v // 16) % 4]
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
    Новая логика:
      1) Списываем доступное с баланса (частичная предоплата возможна).
      2) На недостающую часть мгновенно отправляем счёт в Stars.
      3) Если хватает баланса полностью — сразу помечаем оплату.
    """
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

    # 1) используем баланс
    use_from_balance = min(bal, need_total)
    if use_from_balance > 0:
        db.add_balance(uid, -use_from_balance)
        db.add_partial_debit(match_id, slot, use_from_balance)

    remaining = need_total - use_from_balance
    if remaining <= 0:
        db.mark_paid_balance(match_id, slot, stake)
        await bot.send_message(uid, f"✅ Ставка {stake} ⭐ списана с твоего баланса.")
        return

    # 2) выставляем счёт на недостающую часть
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
    await bot.send_message(uid,
                           f"💳 Выставлен счёт на недостающие {remaining} ⭐. После оплаты матч стартует автоматически.")


# ==================== COMMANDS ====================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    # --- разбор payload вида: /start ref_<id> ---
    payload = ""
    try:
        parts = m.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()
    except Exception:
        pass

    if payload.startswith("ref_"):
        try:
            ref_id = int(payload[4:])
            if ref_id != m.from_user.id:
                db.set_referrer_if_empty(m.from_user.id, ref_id)
                # мягкое подтверждение (не мешаем UI)
                try:
                    await m.answer(
                        f"✅ Реферальная метка установлена. Сыграй первый оплаченный матч — и другу начислятся +{REF_REWARD}⭐.")
                except Exception:
                    pass
        except Exception:
            pass

    mv = row_to_match(db.get_match_by_user(m.from_user.id))
    kb = inline_menu(db.in_queue(m.from_user.id), bool(mv and not mv.winner_id), mode=get_user_mode(m.from_user.id))
    text = (
        "🎰 PVP-Game 1v1!\n\n"
        "Режимы:\n"
        "• 🎰 777 \n"
        "• 🎲 3 броска \n\n"
        "Как играть:\n"
        "1) Выбери режим кнопкой «🎮 Режим» в меню.\n"
        "2) Жми «🟢 В очередь» и выбери ставку.\n"
        "3) После старта матча:\n"
        "   • в режиме 🎰 — просто отправляй эмодзи 🎰;\n"
        "   • в режиме 🎲 — просто отправляй эмодзи 🎲.\n"
        "4) КД между бросками — 5 сек.\n\n"
        "Удачи на Арене!"
    )


    await m.answer(text, reply_markup=kb, disable_web_page_preview=True)


@dp.message(Command("ref"))
async def cmd_ref(m: Message):
    global BOT_USERNAME

    # если по какой-то причине не закэшировался — пробуем один раз получить здесь,
    # но не падаем, если опять таймаут
    if not BOT_USERNAME:
        try:
            me = await bot.get_me()
            BOT_USERNAME = me.username
        except Exception:
            BOT_USERNAME = None

    if BOT_USERNAME:
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{m.from_user.id}"
    else:
        link = "— не удалось получить ссылку, попробуйте позже —"

    total, rewarded = db.count_referrals(m.from_user.id)
    text = (
        "👥 Реферальная программа\n"
        "Пригласите друга по вашей ссылке. Как только он ОДИН РАЗ оплатит и начнёт матч — вы получаете бонус.\n\n"
        f"🔗 Ваша ссылка:\n{link}\n\n"
        f"📈 Пришло: {total} • Начисления получены: {rewarded}\n"
        f"Награда: {REF_REWARD}⭐ за каждого активированного друга."
    )
    await m.answer(text, disable_web_page_preview=True)


@dp.message(Command("mode"))
async def cmd_mode(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=MODE_LABEL["slots"], callback_data="mode_slots")],
        [InlineKeyboardButton(text=MODE_LABEL["dice3"], callback_data="mode_dice3")],
    ])
    await m.answer("Выбери игровой режим:", reply_markup=kb)


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
    await m.answer(
        "Поддержка по платежам: опиши проблему, приложи ID оплаты и скрин. Возможен refund по правилам Stars.")


@dp.message(Command("join"))
async def cmd_join(m: Message):
    await m.answer("Выбери ставку для матча:", reply_markup=stake_keyboard())


@dp.message(Command("leave"))
async def cmd_leave(m: Message):
    await queue_leave_impl(m.from_user.id, m)


@dp.message(Command("topup"))
async def cmd_topup(m: Message):
    await m.answer("Выбери сумму пополнения:", reply_markup=topup_keyboard())


# ==================== CALLBACKS: RULES / QUEUE / TOPUP / MODES ====================
def stake_from_cb(data: str) -> int:
    return int(data.split("_")[1])


@dp.callback_query(F.data == "rules")
async def cb_rules(cq: CallbackQuery):
    await cq.answer()
    await edit_or_send(
        cq.message,
        "Правила:\n"
        f"• Комиссия бота {FEE_PCT}%.\n"
        "• Режим 1 — 🎰 777: Победа — 777; BAR BAR BAR — проигрыш бросившего; 🍋🍋🍋 — ничья (возврат минус комиссия).\n"
        "• Режим 2 — 🎲 3 броска: каждый делает 3 броска; больше сумма — победа; равенство — ничья (возврат минус комиссия).\n"
        f"• Кулдаун — {COOLDOWN_SEC} сек.\n"
        "• Участие — с внутреннего баланса; если не хватает, недостающее автоматически оплачивается счётом в Stars.\n"
        "• Приз зачисляется на внутренний баланс."
    )



@dp.callback_query(F.data == "modes_open")
async def cb_modes_open(cq: CallbackQuery):
    await cq.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=MODE_LABEL["slots"], callback_data="mode_slots")],
        [InlineKeyboardButton(text=MODE_LABEL["dice3"], callback_data="mode_dice3")],
    ])
    await edit_or_send(cq.message, "Выбери режим игры:", kb)



@dp.callback_query(F.data.in_(["mode_slots", "mode_dice3"]))
async def cb_set_mode(cq: CallbackQuery):
    await cq.answer()
    mode = "slots" if cq.data == "mode_slots" else "dice3"
    set_user_mode(cq.from_user.id, mode)
    mv = row_to_match(db.get_match_by_user(cq.from_user.id))
    kb = inline_menu(db.in_queue(cq.from_user.id), bool(mv and not mv.winner_id), mode=mode)
    await edit_or_send(cq.message, f"Режим установлен: {MODE_LABEL[mode]}", kb)



@dp.callback_query(F.data == "queue_join")
async def cb_queue_join(cq: CallbackQuery):
    await cq.answer()
    await edit_or_send(cq.message, "Выбери ставку для матча:", stake_keyboard())



@dp.callback_query(F.data == "queue_leave")
async def cb_queue_leave(cq: CallbackQuery):
    await cq.answer()
    await queue_leave_impl(cq.from_user.id, cq.message)


@dp.callback_query(F.data.startswith("stake_"))
async def cb_stake(cq: CallbackQuery):
    await cq.answer()
    stake = stake_from_cb(cq.data)
    if stake not in ALLOWED_STAKES:
        return await edit_or_send(cq.message, "Некорректная ставка.")

    uid = cq.from_user.id
    mode = get_user_mode(uid)

    mv = row_to_match(db.get_match_by_user(uid))
    if mv and mv.active:
        return await edit_or_send(cq.message, "Ты уже в активном матче.")

    if db.in_queue(uid):
        db.remove_from_queue(uid)

    opp = db.pop_any_from_queue(exclude_user_id=uid, stake=stake, mode=mode)
    if opp:
        match_id = db.create_match(p1_id=opp, p2_id=uid, stake=stake, game_mode=mode)

        # статус подбора — редактируем ТЕКУЩЕЕ сообщение
        await edit_or_send(
            cq.message,
            f"Найден соперник: {link_user(opp)}.\nРежим: {MODE_LABEL[mode]}\nГотовим старт матча…",
        )

        # оповещаем соперника отдельным сообщением
        await bot.send_message(
            opp,
            f"Подключился соперник: {link_user(uid)}.\nРежим: {MODE_LABEL[mode]}\nГотовим старт матча…",
            parse_mode="HTML",
        )

        # Выставляем оплату обоим
        for pid in (uid, opp):
            await try_auto_pay_and_invoice(match_id, pid, stake)

        # Если оба оплатили — стартуем матч
        if db.can_start(match_id):
            db.start_match(match_id)
            row2 = db.get_match_by_user(uid)
            mv2 = row_to_match(row2)

            # сброс кулдаунов
            last_spin_time.pop(mv2.p1_id, None)
            if mv2.p2_id:
                last_spin_time.pop(mv2.p2_id, None)

            text = (
                    f"Матч начался! Режим {MODE_LABEL[mode]}. "
                    f"Ставка {mv2.stake} ⭐ (комиссия {FEE_PCT}%). "
                    f"Приз: {prize_after_fee(mv2.stake)} ⭐. "
                    + ("Отправляй 🎰." if mode == "slots" else "Отправляй 🎲.")
            )

            # обновим ЭТО ЖЕ сообщение у инициатора
            await edit_or_send(cq.message, text)

            # уведомим только второго игрока (чтобы не дублировать инициатору)
            notify_uid = mv2.p2_id if uid == mv2.p1_id else mv2.p1_id
            if notify_uid:
                await bot.send_message(notify_uid, text)

            # реферальные бонусы
            for pid in (mv2.p1_id, mv2.p2_id):
                if not pid:
                    continue
                referrer = db.award_referral_if_eligible(pid)
                if referrer:
                    try:
                        await bot.send_message(
                            referrer,
                            f"🎁 +{REF_REWARD}⭐ за друга {link_user(pid)}! Спасибо за приглашение.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

        else:
            # Матч создан, ждём оплату (сообщение сопернику/тебе — отдельные уведомления)
            await bot.send_message(uid, "🧾 Счёт выставлен. Оплати ставку — матч стартует автоматически.")
            await bot.send_message(opp, "🧾 Счёт выставлен. Оплати ставку — матч стартует автоматически.")
    else:
        # Соперника нет — ставим в очередь и обновляем текущее сообщение
        db.add_to_queue(uid, stake, mode=mode)
        await edit_or_send(cq.message, f"Ты в очереди на {MODE_LABEL[mode]} со ставкой {stake} ⭐. Ждём соперника!")



@dp.callback_query(F.data == "topup_open")
async def cb_topup_open(cq: CallbackQuery):
    await cq.answer()
    await edit_or_send(cq.message, "Выбери сумму пополнения:", reply_markup=topup_keyboard())



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
        return await edit_or_send(cq.message, "Некорректная сумма пополнения.")

    uid = cq.from_user.id
    title = f"Пополнение баланса (+{amt}⭐)"
    description = f"Пополнение внутреннего баланса на {amt} ⭐."
    prices = [LabeledPrice(label=f"{amt}⭐", amount=amt)]

    await bot.send_invoice(
        chat_id=uid,
        title=title,
        description=description,
        payload=f"topup:{uid}:{amt}",
        provider_token="",
        currency="XTR",
        prices=prices,
        request_timeout=45,
    )



# ==================== QUEUE LEAVE ====================
async def queue_leave_impl(uid: int, where: Message):
    mv = row_to_match(db.get_match_by_user(uid))

    # Если в очереди — просто убираем и редактируем текущее сообщение
    if db.in_queue(uid):
        db.remove_from_queue(uid)
        kb = inline_menu(False, bool(mv and not mv.winner_id), mode=get_user_mode(uid))
        return await edit_or_send(where, "Ок, убрал из очереди.", reply_markup=kb)

    # Если матч создан, но ещё не стартовал — отменяем и возвращаем частичные списания
    if mv and not mv.active and not mv.winner_id:
        state = db.get_match_payment_state(mv.id)
        p1_deb = int(state["p1_balance_debited"]) if state else 0
        p2_deb = int(state["p2_balance_debited"]) if (state and mv.p2_id) else 0

        if p1_deb > 0:
            db.add_balance(mv.p1_id, p1_deb)
        if mv.p2_id and p2_deb > 0:
            db.add_balance(mv.p2_id, p2_deb)

        db.set_winner_and_close(mv.id, winner_id=0)

        await edit_or_send(where, "Матч отменён до старта. Средства с баланса возвращены.")
        other = mv.p2_id if uid == mv.p1_id else mv.p1_id
        if other:
            await bot.send_message(other, "Соперник покинул матч до старта. Вернись в очередь: /join")
        return

    # Уже идёт — нельзя
    if mv and mv.active and not mv.winner_id:
        return await edit_or_send(where, "Матч уже идёт — выход невозможен.")

    # Вообще ни там, ни тут
    await edit_or_send(where, "Ты не в очереди и не в матче.")


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
                            f"Матч начался! Режим {MODE_LABEL[mv.game_mode]}. "
                            f"Ставка {mv.stake} ⭐ (комиссия {FEE_PCT}%). "
                            f"Приз: {prize_after_fee(mv.stake)} ⭐. "
                            + ("Отправляй 🎰." if mv.game_mode == "slots" else "Отправляй 🎲.")
                    )
                    await bot.send_message(mv.p1_id, text)
                    if mv.p2_id:
                        await bot.send_message(mv.p2_id, text)

                    # 👇 Реферальная награда при старте после пополнения
                    for pid in (mv.p1_id, mv.p2_id):
                        if not pid:
                            continue
                        referrer = db.award_referral_if_eligible(pid)
                        if referrer:
                            try:
                                await bot.send_message(
                                    referrer,
                                    f"🎁 +{REF_REWARD}⭐ за друга {link_user(pid)}! Спасибо за приглашение.",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass

        return

    # 2) Доплата дефицита для матча
    if payload.startswith("deficit:"):
        try:
            _, match_id_str, slot_str, amt_str = payload.split(":")
            match_id = int(match_id_str)
            slot = int(slot_str)
            amt = int(amt_str)
        except Exception:
            return await m.answer("Оплата получена, но не удалось распознать назначение. Обратись в /paysupport.")

        # Фиксируем сумму как погашенную (для консистентности с частичным дебетом)
        db.add_partial_debit(match_id, slot, amt)
        # Отмечаем источник оплаты
        db.mark_paid_invoice(match_id, m.from_user.id)

        # Если оба оплатили — стартуем матч
        if db.can_start(match_id):
            db.start_match(match_id)
            p1_id, p2_id = db.get_match_players(match_id)
            # Сбросим КД
            last_spin_time.pop(p1_id, None)
            if p2_id:
                last_spin_time.pop(p2_id, None)
            # Узнаем ставку
            state = db.get_match_payment_state(match_id)
            stake = int(state["stake"]) if state else 0
            # Узнаем режим
            row = db.get_match_by_user(p1_id)
            mv = row_to_match(row)
            mode = mv.game_mode if mv else "slots"
            text = (
                    f"Матч начался! Режим {MODE_LABEL.get(mode, mode)}. "
                    f"Ставка {stake} ⭐ (комиссия {FEE_PCT}%). "
                    f"Приз: {prize_after_fee(stake)} ⭐. "
                    + ("Отправляй 🎰." if mode == "slots" else "Отправляй 🎲.")
            )
            await bot.send_message(p1_id, text)
            if p2_id:
                await bot.send_message(p2_id, text)

            # 👇 Реферальная награда при старте после доплаты
            for pid in (p1_id, p2_id):
                if not pid:
                    continue
                referrer = db.award_referral_if_eligible(pid)
                if referrer:
                    try:
                        await bot.send_message(
                            referrer,
                            f"🎁 +{REF_REWARD}⭐ за друга {link_user(pid)}! Спасибо за приглашение.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass


        else:
            await m.answer("✅ Оплата принята. Ожидаем соперника.")
        return

    # 3) Старые payload-ы (совместимость)
    await m.answer("Оплата получена. Если это пополнение — пожалуйста, используйте /topup в следующий раз.")


# ==================== GAME: user-sent 🎰 (SLOTS) ====================
@dp.message(F.dice)
async def handle_any_dice(m: Message):
    # запрет на пересыл
    if is_forwarded(m):
        return await m.reply("❌ Пересылать чужие броски запрещено. Отправь свой 🎰/🎲.")

    uid = m.from_user.id
    mv = row_to_match(db.get_match_by_user(uid))
    if not mv or mv.winner_id:
        return await m.reply("Матч не найден или уже завершён. /join")
    if not mv.active:
        return await m.reply("Матч ещё не стартовал. Ждём оплату обоих.")

    emoji = m.dice.emoji

    # режим/эмодзи должны совпадать
    if mv.game_mode == "slots" and emoji != DiceEmoji.SLOT_MACHINE:
        return await m.reply("Сейчас идёт режим 🎰. Отправляй 🎰.")
    if mv.game_mode == "dice3" and emoji != DiceEmoji.DICE:
        return await m.reply("Сейчас идёт режим 🎲. Отправляй 🎲.")

    # КД (общий для обоих режимов)
    if not cooldown_ready(uid):
        remain = COOLDOWN_SEC - int(time.time() - last_spin_time.get(uid, 0))
        if remain < 0: remain = 0
        await show_cooldown(m.chat.id, uid, remain)
        return

    mark_cooldown(uid)
    await show_cooldown(m.chat.id, uid, COOLDOWN_SEC)

    opponent_id = mv.p2_id if uid == mv.p1_id else mv.p1_id
    val = int(m.dice.value)

    # ======== 🎰 SLOTS ========
    if mv.game_mode == "slots":
        try:
            # Ничего не пишем цифрами. Отправляем сопернику сам "бросок" как стикер/анимацию.
            if opponent_id:
                await bot.forward_message(
                    chat_id=opponent_id,
                    from_chat_id=m.chat.id,
                    message_id=m.message_id
                )

        except Exception:
            pass


        if is_jackpot_777(val):
            return await on_win(uid, mv)
        if is_triple_bar(val) and opponent_id:
            return await on_win(opponent_id, mv)
        if is_triple_lemon(val):
            return await on_draw_lemon(mv)
        return  # обычный спин — игра продолжается

    # ======== 🎲 DICE3 ========
    # проверка лимита бросков ДО записи
    slot = 1 if uid == mv.p1_id else 2
    st0 = db.get_dice_state(mv.id)
    my_cnt0 = int(st0["p1_dice_cnt"] if slot == 1 else st0["p2_dice_cnt"])
    if my_cnt0 >= 3:
        return await m.reply("У тебя уже 3/3 бросков. Ждём соперника.")

    # условная запись броска (атомарно)
    accepted = db.add_dice_throw(mv.id, slot, val)
    if not accepted:
        return await m.reply("Бросок не засчитан: у тебя уже 3/3.")

    st = db.get_dice_state(mv.id)
    p1_sum, p2_sum = int(st["p1_dice_sum"]), int(st["p2_dice_sum"])
    p1_cnt, p2_cnt = int(st["p1_dice_cnt"]), int(st["p2_dice_cnt"])

    try:
        await bot.send_message(uid,
                               f"🎲 тебе выпало: {val}. Броски: {p1_cnt}/3 vs {p2_cnt}/3. Сумма: {p1_sum} vs {p2_sum}.")
        if opponent_id:
            await bot.send_message(opponent_id,
                                   f"🎲 у соперника выпало: {val}. Броски: {p1_cnt}/3 vs {p2_cnt}/3. Сумма: {p1_sum} vs {p2_sum}.",
                                   parse_mode="HTML")
    except Exception:
        pass

    if p1_cnt >= 3 and p2_cnt >= 3:
        if p1_sum > p2_sum:
            return await on_win(mv.p1_id, mv)
        elif p2_sum > p1_sum:
            return await on_win(mv.p2_id, mv)
        else:
            return await on_draw_sum(mv, p1_sum)


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
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            pass

    try:
        txt = (
            f"🤝 Ничья: 🍋🍋🍋\n"
            f"Каждый получил обратно по {refund} ⭐ (ставка {mv.stake}⭐, комиссия {FEE_PCT}%)."
        )
        await bot.send_message(mv.p1_id, txt)
        if mv.p2_id:
            await bot.send_message(mv.p2_id, txt)
    except Exception:
        pass


async def on_draw_sum(mv: MatchView, total: int):
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
                f"🤝 Ничья по сумме ({total} : {total}). Возврат: {refund} ⭐ каждому (комиссия {FEE_PCT}% удержана).",
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            pass


# ==================== ADMIN ====================
@dp.message(Command("addstars"))
async def cmd_addstars(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("⛔ Нет доступа")
    parts = m.text.split()
    if len(parts) != 3:
        return await m.answer("Формат: /addstars <user_id> <amount>")
    try:
        uid = int(parts[1])
        amt = int(parts[2])
    except ValueError:
        return await m.answer("user_id и amount должны быть числами")
    db.add_balance(uid, amt)
    new_bal = db.get_balance(uid)
    await m.answer(
        f"✅ Игрок {link_user(uid)} получил {amt} ⭐.\nНовый баланс: {new_bal} ⭐",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(uid, f"💎 Тебе начислено {amt} ⭐.\nТекущий баланс: {new_bal} ⭐")
    except Exception:
        pass


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
        global BOT_USERNAME
        # получим и закэшируем username бота один раз при старте
        try:
            me = await bot.get_me()
            BOT_USERNAME = me.username
        except Exception:
            BOT_USERNAME = None  # на всякий случай, чтобы не падать при старте

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    asyncio.run(main())

