import logging

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import common
from common import get_settings, is_chat_admin
from protection import apply_punishment, is_anon_admin, write_log
from textfilters import FloodTracker, find_stopword, normalize_text

router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
MAX_WORDS = 200
tracker = FloodTracker()


# ---------- Проверка каждого сообщения ----------
class ModerationMiddleware(BaseMiddleware):
    def __init__(self, check_flood: bool = True):
        self.check_flood = check_flood  # правки сообщений на флуд не проверяем

    async def __call__(self, handler, event: Message, data):
        if await self._violates(event, data["bot"]):
            return None  # нарушитель: дальше сообщение не идёт
        return await handler(event, data)

    async def _violates(self, message: Message, bot: Bot) -> bool:
        if message.chat.type not in GROUPS:
            return False
        if message.sender_chat or message.is_automatic_forward:
            return False
        if message.new_chat_members or message.left_chat_member:
            return False
        user = message.from_user
        if user is None or user.is_bot:
            return False

        chat_id = message.chat.id
        s = await get_settings(chat_id)
        if not s or not (s["antiflood"] or s["stopwords"]):
            return False
        if await is_chat_admin(bot, chat_id, user.id):
            return False

        reason = details = None
        first = True

        if self.check_flood and s["antiflood"]:
            key = (chat_id, user.id)
            repeat = bool(message.media_group_id) and tracker.is_album_repeat(key, message.media_group_id)
            if not repeat:
                count = tracker.hit(key, s["flood_limit"], s["flood_window"])
                if count > s["flood_limit"]:
                    reason = "flood"
                    first = count == s["flood_limit"] + 1  # наказываем и логируем один раз
                    details = f"{s['flood_limit']}+ сообщений за {s['flood_window']}с"

        if reason is None and s["stopwords"]:
            text = message.text or message.caption
            if text:
                word = find_stopword(text, tuple(s["stopword_list"]))
                if word:
                    reason, details = "stopword", word

        if reason is None:
            return False

        try:
            await message.delete()
        except TelegramAPIError as e:
            logging.warning("Нет прав удалить сообщение в %s: %s", chat_id, e)
            return False

        if first:
            try:
                await apply_punishment(bot, chat_id, user.id, s["punishment"])
            except TelegramAPIError as e:
                logging.warning("Не удалось наказать %s в %s: %s", user.id, chat_id, e)
            await write_log(chat_id, user.id, reason, details or "")
        return True


router.message.outer_middleware(ModerationMiddleware(check_flood=True))
router.edited_message.outer_middleware(ModerationMiddleware(check_flood=False))


# ---------- Команды (только админы, в группе) ----------
async def _admin_only(message: Message, bot: Bot) -> bool:
    if is_anon_admin(message):
        return True
    user = message.from_user
    return bool(user and await is_chat_admin(bot, message.chat.id, user.id, fresh=True))


@router.message(F.chat.type.in_(GROUPS), Command("addword", "delword", "words"))
async def words_cmd(message: Message, command: CommandObject, bot: Bot):
    if not await _admin_only(message, bot):
        return
    chat_id = message.chat.id
    s = await get_settings(chat_id)
    if not s:
        return await message.reply("Чат не зарегистрирован. Добавьте бота заново.")

    if command.command == "words":
        items = s["stopword_list"]
        return await message.reply("Стоп-слова:\n" + "\n".join(items) if items else "Список пуст.")

    word = normalize_text(command.args or "").strip()
    if not 2 <= len(word) <= 64:
        return await message.reply(f"Использование: /{command.command} слово или фраза (2-64 символа)")

    if command.command == "addword":
        result = await common.pool.execute(
            """
            UPDATE chat_settings SET stopword_list = array_append(stopword_list, $2)
            WHERE chat_id = $1 AND NOT ($2 = ANY(stopword_list))
              AND coalesce(array_length(stopword_list, 1), 0) < $3
            """,
            chat_id, word, MAX_WORDS,
        )
        text = "Добавлено." if result.endswith("1") else f"Уже в списке или достигнут лимит ({MAX_WORDS})."
    else:
        await common.pool.execute(
            "UPDATE chat_settings SET stopword_list = array_remove(stopword_list, $2) WHERE chat_id = $1",
            chat_id, word,
        )
        text = "Удалено."

    common.invalidate_settings(chat_id)
    await message.reply(text)


@router.message(F.chat.type.in_(GROUPS), Command("flood"))
async def flood_cmd(message: Message, command: CommandObject, bot: Bot):
    if not await _admin_only(message, bot):
        return
    chat_id = message.chat.id
    s = await get_settings(chat_id)
    if not s:
        return await message.reply("Чат не зарегистрирован. Добавьте бота заново.")

    args = (command.args or "").split()
    if not args:
        return await message.reply(
            f"Лимит: {s['flood_limit']} сообщений за {s['flood_window']} сек.\n"
            "Изменить: /flood 5 10 (сообщений, секунд)"
        )
    try:
        limit, window = int(args[0]), int(args[1])
    except (ValueError, IndexError):
        return await message.reply("Использование: /flood 5 10 (сообщений, секунд)")
    if not (2 <= limit <= 30 and 3 <= window <= 60):
        return await message.reply("Допустимо: сообщений 2-30, секунд 3-60.")

    await common.pool.execute(
        "UPDATE chat_settings SET flood_limit = $2, flood_window = $3 WHERE chat_id = $1",
        chat_id, limit, window,
    )
    common.invalidate_settings(chat_id)
    await message.reply(f"Готово: {limit} сообщений за {window} сек.")
