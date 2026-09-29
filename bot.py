"""@cuefa_robot — «Камень, Ножницы, Бумага» в инлайн-режиме + админ-панель (aiogram 3.x)."""
import asyncio
import html
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import closing
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
                           Message, User)
from dotenv import load_dotenv

# ───────────────────────────── Конфиг ─────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
SETTINGS_FILE = "settings.json"
DB_FILE = "bot.db"
GAME_TIMEOUT = 10 * 60  # автоочистка зависших игр, сек.

# ─────────────────── Настройки (JSON, переживают перезапуск) ───────────────────
DEFAULT_SETTINGS: Dict[str, str] = {
    "rock_emoji": "🪨", "rock_name": "Камень",
    "scissors_emoji": "✂️", "scissors_name": "Ножницы",
    "paper_emoji": "📄", "paper_name": "Бумага",
    "btn_accept": "Принять", "btn_decline": "Отклонить",
    "challenge_text": "⚔️ <b>{p1}</b> вызывает на дуэль в «Камень, Ножницы, Бумага»!\nКто примет вызов?",
    "playing_text": "🎮 Дуэль: <b>{p1}</b> ⚔️ <b>{p2}</b>\nСделайте выбор — он скрыт от остальных!",
    "win_text": "🏆 Победитель — <b>{winner}</b>!\n\n{p1}: {c1}\n{p2}: {c2}",
    "draw_text": "🤝 Ничья!\n\n{p1}: {c1}\n{p2}: {c2}",
    "declined_text": "❌ Дуэль отклонена",
}
# Допустимые плейсхолдеры для проверки шаблонов при сохранении
SAMPLE = {"p1": "A", "p2": "B", "c1": "🪨", "c2": "📄", "winner": "B", "loser": "A"}

LABELS = {
    "rock_emoji": "Эмодзи «Камень»", "rock_name": "Название «Камень»",
    "scissors_emoji": "Эмодзи «Ножницы»", "scissors_name": "Название «Ножницы»",
    "paper_emoji": "Эмодзи «Бумага»", "paper_name": "Название «Бумага»",
    "btn_accept": "Кнопка «Принять»", "btn_decline": "Кнопка «Отклонить»",
    "challenge_text": "Текст вызова ({p1})", "playing_text": "Текст игры ({p1} {p2})",
    "win_text": "Шаблон победы ({p1} {p2} {c1} {c2} {winner} {loser})",
    "draw_text": "Шаблон ничьей ({p1} {p2} {c1} {c2})",
    "declined_text": "Текст отклонения",
}
DESIGN_KEYS = list(LABELS)[:8]
TEXT_KEYS = list(LABELS)[8:]


class Settings:
    """Хранит настройки в settings.json; недостающие ключи берутся из значений по умолчанию."""

    def __init__(self, path: str):
        self.path = path
        self.data = dict(DEFAULT_SETTINGS)
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
        except (FileNotFoundError, ValueError):
            pass

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value
        self.save()

    def reset(self) -> None:
        self.data = dict(DEFAULT_SETTINGS)
        self.save()

    def __getitem__(self, key: str) -> str:
        return self.data[key]

    def render(self, key: str, **kw: Any) -> str:
        """Подставляет значения в шаблон; при ошибке в шаблоне — берёт шаблон по умолчанию."""
        try:
            return self.data[key].format(**kw)
        except (KeyError, IndexError, ValueError):
            return DEFAULT_SETTINGS[key].format(**kw)

    def choice_label(self, choice: str) -> str:
        return f"{self[choice + '_emoji']} {self[choice + '_name']}"


settings = Settings(SETTINGS_FILE)

# ───────────────────────── База (пользователи, счётчик игр) ─────────────────────────
def db_init() -> None:
    with closing(sqlite3.connect(DB_FILE)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS stats (k TEXT PRIMARY KEY, v INTEGER)")
        db.execute("INSERT OR IGNORE INTO stats VALUES ('games', 0)")
        db.commit()


def db_exec(sql: str, args: tuple = ()) -> list:
    with closing(sqlite3.connect(DB_FILE)) as db:
        rows = db.execute(sql, args).fetchall()
        db.commit()
        return rows


def track_user(user: User) -> None:
    if not user.is_bot:
        db_exec("INSERT OR IGNORE INTO users VALUES (?)", (user.id,))


class TrackUsersMiddleware(BaseMiddleware):
    """Запоминает каждого, кто взаимодействует с ботом (для статистики и рассылки)."""

    async def __call__(self, handler: Callable, event: Any, data: Dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user:
            track_user(user)
        return await handler(event, data)


# ───────────────────────────── Игровая логика ─────────────────────────────
CHOICES = ("rock", "scissors", "paper")
BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}  # ключ бьёт значение


class Duel:
    """Одна дуэль. Живёт только в оперативной памяти."""

    def __init__(self, creator: User):
        self.p1_id = creator.id
        self.p1_name = html.escape(creator.first_name)
        self.p2_id: int | None = None
        self.p2_name = ""
        self.inline_id: str | None = None  # inline_message_id, известен после первого нажатия
        self.choices: Dict[int, str] = {}
        self.touched = time.time()


duels: Dict[str, Duel] = {}  # token -> Duel


def challenge_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=settings["btn_accept"], callback_data=f"acc:{token}"),
        InlineKeyboardButton(text=settings["btn_decline"], callback_data=f"dec:{token}"),
    ]])


def moves_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=settings.choice_label(c), callback_data=f"mv:{token}:{c}")
        for c in CHOICES
    ]])


game_router = Router()


@game_router.inline_query()
async def on_inline_query(q: InlineQuery) -> None:
    """Предлагаем отправить карточку вызова на дуэль."""
    token = uuid.uuid4().hex[:10]
    duels[token] = Duel(q.from_user)
    duel = duels[token]
    text = settings.render("challenge_text", p1=duel.p1_name, p2="")
    result = InlineQueryResultArticle(
        id=token,
        title="⚔️ Вызвать на дуэль",
        description="Камень, Ножницы, Бумага",
        input_message_content=InputTextMessageContent(message_text=text, parse_mode=ParseMode.HTML),
        reply_markup=challenge_kb(token),
    )
    # is_personal + cache_time=0: у каждого пользователя и запроса свой токен
    await q.answer([result], cache_time=0, is_personal=True)


async def get_duel(cb: CallbackQuery, token: str) -> Duel | None:
    duel = duels.get(token)
    if not duel:
        await cb.answer("⌛ Эта дуэль устарела или завершена.", show_alert=True)
        return None
    if cb.inline_message_id:
        duel.inline_id = cb.inline_message_id
    duel.touched = time.time()
    return duel


@game_router.callback_query(F.data.startswith("dec:"))
async def on_decline(cb: CallbackQuery, bot: Bot) -> None:
    token = cb.data.split(":")[1]
    duel = await get_duel(cb, token)
    if not duel:
        return
    if cb.from_user.id != duel.p1_id:
        await cb.answer("⚠️ Отклонить может только создатель дуэли!", show_alert=True)
        return
    duels.pop(token, None)
    await bot.edit_message_text(settings["declined_text"], inline_message_id=cb.inline_message_id)
    await cb.answer()


@game_router.callback_query(F.data.startswith("acc:"))
async def on_accept(cb: CallbackQuery, bot: Bot) -> None:
    token = cb.data.split(":")[1]
    duel = await get_duel(cb, token)
    if not duel:
        return
    if cb.from_user.id == duel.p1_id:
        await cb.answer("⚠️ Нельзя играть с самим собой!", show_alert=True)
        return
    if duel.p2_id is not None:  # уже принята другим (защита от гонки)
        await cb.answer("⚠️ Дуэль уже принята другим игроком.", show_alert=True)
        return
    duel.p2_id = cb.from_user.id
    duel.p2_name = html.escape(cb.from_user.first_name)
    await bot.edit_message_text(
        settings.render("playing_text", p1=duel.p1_name, p2=duel.p2_name),
        inline_message_id=cb.inline_message_id, reply_markup=moves_kb(token))
    await cb.answer()


@game_router.callback_query(F.data.startswith("mv:"))
async def on_move(cb: CallbackQuery, bot: Bot) -> None:
    _, token, choice = cb.data.split(":")
    duel = await get_duel(cb, token)
    if not duel:
        return
    uid = cb.from_user.id
    if uid not in (duel.p1_id, duel.p2_id):
        await cb.answer("⚠️ Это не ваша дуэль!", show_alert=True)
        return
    if uid in duel.choices:
        await cb.answer("Вы уже сделали свой выбор!", show_alert=True)
        return
    duel.choices[uid] = choice  # выбор скрыт: сообщение в чате не меняем
    if len(duel.choices) < 2:
        await cb.answer(f"Вы выбрали {settings.choice_label(choice)}. Ждём ход соперника...",
                        show_alert=True)
        return

    # Оба сделали ход — определяем победителя
    c1, c2 = duel.choices[duel.p1_id], duel.choices[duel.p2_id]
    fields = dict(p1=duel.p1_name, p2=duel.p2_name,
                  c1=settings.choice_label(c1), c2=settings.choice_label(c2))
    if c1 == c2:
        text = settings.render("draw_text", **fields)
    else:
        p1_wins = BEATS[c1] == c2
        winner, loser = (duel.p1_name, duel.p2_name) if p1_wins else (duel.p2_name, duel.p1_name)
        text = settings.render("win_text", winner=winner, loser=loser, **fields)
    duels.pop(token, None)  # сессия удаляется из памяти
    db_exec("UPDATE stats SET v = v + 1 WHERE k = 'games'")
    await bot.edit_message_text(text, inline_message_id=cb.inline_message_id)
    await cb.answer()


async def cleanup_loop(bot: Bot) -> None:
    """Раз в 30 секунд удаляет зависшие дуэли (таймаут 10 минут)."""
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for token, duel in list(duels.items()):
            if now - duel.touched > GAME_TIMEOUT:
                duels.pop(token, None)
                if duel.inline_id:  # карточка уже «привязана» к сообщению — сообщаем об истечении
                    try:
                        await bot.edit_message_text("⌛ Дуэль устарела (нет ответа 10 минут)",
                                                    inline_message_id=duel.inline_id)
                    except TelegramAPIError:
                        pass


# ───────────────────────────── Админ-панель ─────────────────────────────
admin_router = Router()
admin_router.message.filter(F.chat.type == "private", F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS), F.data.startswith("adm:"))


class Admin(StatesGroup):
    edit_value = State()
    broadcast = State()


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


MAIN_KB = kb([[("🎨 Кнопки и дизайн", "adm:design")], [("📝 Тексты и шаблоны", "adm:texts")],
              [("📊 Статистика", "adm:stats")], [("📢 Рассылка", "adm:bc")],
              [("♻️ Сбросить настройки", "adm:reset")]])
BACK_KB = kb([[("⬅️ В меню", "adm:menu")]])


def keys_kb(keys: list[str]) -> InlineKeyboardMarkup:
    rows = [[(f"{LABELS[k].split(' (')[0]}: {settings[k][:20]}", f"adm:edit:{k}")] for k in keys]
    return kb(rows + [[("⬅️ В меню", "adm:menu")]])


@admin_router.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext) -> None:
    await state.clear()
    await m.answer("🛠 <b>Админ-панель @cuefa_robot</b>", reply_markup=MAIN_KB)


@admin_router.callback_query(F.data == "adm:menu")
async def adm_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("🛠 <b>Админ-панель @cuefa_robot</b>", reply_markup=MAIN_KB)
    await cb.answer()


@admin_router.callback_query(F.data == "adm:design")
async def adm_design(cb: CallbackQuery) -> None:
    await cb.message.edit_text("🎨 Выберите, что изменить:", reply_markup=keys_kb(DESIGN_KEYS))
    await cb.answer()


@admin_router.callback_query(F.data == "adm:texts")
async def adm_texts(cb: CallbackQuery) -> None:
    await cb.message.edit_text("📝 Выберите текст (поддерживается HTML):",
                               reply_markup=keys_kb(TEXT_KEYS))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:edit:"))
async def adm_edit(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":")[2]
    await state.set_state(Admin.edit_value)
    await state.update_data(key=key)
    await cb.message.edit_text(
        f"✏️ <b>{html.escape(LABELS[key])}</b>\nСейчас:\n<pre>{html.escape(settings[key])}</pre>\n"
        "Отправьте новое значение сообщением.", reply_markup=BACK_KB)
    await cb.answer()


@admin_router.message(Admin.edit_value, F.text)
async def adm_edit_save(m: Message, state: FSMContext) -> None:
    key = (await state.get_data())["key"]
    value = m.html_text  # сохраняет форматирование, которое админ применил в Telegram
    if key in TEXT_KEYS:  # проверяем, что шаблон корректен
        try:
            value.format(**SAMPLE)
        except (KeyError, IndexError, ValueError) as e:
            await m.answer(f"⚠️ Ошибка в шаблоне ({html.escape(repr(e))}). Используйте только "
                           "разрешённые {плейсхолдеры}. Попробуйте ещё раз.")
            return
    settings.set(key, value)
    await state.clear()
    await m.answer("✅ Сохранено!", reply_markup=BACK_KB)


@admin_router.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery) -> None:
    games = db_exec("SELECT v FROM stats WHERE k='games'")[0][0]
    users = db_exec("SELECT COUNT(*) FROM users")[0][0]
    await cb.message.edit_text(
        f"📊 <b>Статистика</b>\n\nСыграно игр: <b>{games}</b>\n"
        f"Активных дуэлей: <b>{len(duels)}</b>\nУникальных пользователей: <b>{users}</b>",
        reply_markup=BACK_KB)
    await cb.answer()


@admin_router.callback_query(F.data == "adm:reset")
async def adm_reset(cb: CallbackQuery) -> None:
    settings.reset()
    await cb.answer("♻️ Настройки сброшены", show_alert=True)


@admin_router.callback_query(F.data == "adm:bc")
async def adm_bc(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.broadcast)
    await cb.message.edit_text("📢 Отправьте текст рассылки (HTML допустим). "
                               "Он уйдёт всем пользователям из БД.", reply_markup=BACK_KB)
    await cb.answer()


@admin_router.message(Admin.broadcast, F.text)
async def adm_bc_send(m: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    ids = [r[0] for r in db_exec("SELECT id FROM users")]
    ok = fail = 0
    status = await m.answer(f"⏳ Рассылка: 0/{len(ids)}")
    for i, uid in enumerate(ids, 1):
        try:
            await bot.send_message(uid, m.html_text)
            ok += 1
        except TelegramRetryAfter as e:  # лимиты Telegram: ждём и повторяем
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(uid, m.html_text)
                ok += 1
            except TelegramAPIError:
                fail += 1
        except (TelegramForbiddenError, TelegramAPIError):  # бот заблокирован / нет ЛС
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Рассылка завершена.\nДоставлено: {ok}\nНе доставлено: {fail}",
                           reply_markup=BACK_KB)


# ───────────────────────────── Запуск ─────────────────────────────
common_router = Router()


@common_router.message(CommandStart(), F.chat.type == "private")
async def start(m: Message) -> None:
    await m.answer("👋 Я играю в «Камень, Ножницы, Бумага». В любом чате напишите "
                   "<code>@cuefa_robot</code> и выберите «Вызвать на дуэль».")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в .env")
    db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    for ev in (dp.message, dp.callback_query, dp.inline_query):
        ev.outer_middleware(TrackUsersMiddleware())
    dp.include_routers(admin_router, game_router, common_router)
    cleaner = asyncio.create_task(cleanup_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        cleaner.cancel()


if __name__ == "__main__":
    asyncio.run(main())
