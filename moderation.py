import logging

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import common
from common import is_chat_admin
from protection import (
    apply_punishment,
    get_settings,
    is_anon_admin,
    write_log,
)
from flood import record_flood_message
from textfilters import find_stopword, normalize_text

router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
MAX_WORDS = 200
MAX_REPLY_LENGTH = 3500


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
        settings = await get_settings(chat_id)
        if not settings or not (settings["antiflood"] or settings["stopwords"]):
            return False
        try:
            if await is_chat_admin(bot, chat_id, user.id):
                return False
        except TelegramAPIError:
            # Не наказываем участника, если Telegram не подтвердил его статус.
            logging.warning(
                "Не удалось проверить права автора сообщения в %s; модерацию пропускаем",
                chat_id,
                exc_info=True,
            )
            return False

        reason = details = None
        log_and_punish = True

        if self.check_flood and settings["antiflood"]:
            try:
                count = await record_flood_message(
                    common.pool,
                    chat_id=chat_id,
                    user_id=user.id,
                    message_id=message.message_id,
                    media_group_id=message.media_group_id,
                    window=settings["flood_window"],
                )
            except Exception:
                # При недоступности БД не применяем наказание на основании
                # неполного счётчика; остальные фильтры продолжают работать.
                logging.exception(
                    "Не удалось зарегистрировать сообщение для антифлуда "
                    "(chat_id=%s, user_id=%s)",
                    chat_id,
                    user.id,
                )
            else:
                if count > settings["flood_limit"]:
                    reason = "flood"
                    log_and_punish = count == settings["flood_limit"] + 1
                    details = (
                        f"{settings['flood_limit']}+ сообщений "
                        f"за {settings['flood_window']}с"
                    )

        if reason is None and settings["stopwords"]:
            message_text = message.text or message.caption
            if message_text:
                word = find_stopword(
                    message_text,
                    tuple(settings["stopword_list"]),
                )
                if word:
                    reason, details = "stopword", word

        if reason is None:
            return False

        try:
            await message.delete()
        except TelegramAPIError as exc:
            logging.warning(
                "Нет прав удалить сообщение в %s: %s",
                chat_id,
                exc,
            )
            return False

        if log_and_punish:
            try:
                await apply_punishment(
                    bot,
                    chat_id,
                    user.id,
                    settings["punishment"],
                )
            except TelegramAPIError as exc:
                logging.warning(
                    "Не удалось наказать %s в %s: %s",
                    user.id,
                    chat_id,
                    exc,
                )

            await write_log(chat_id, user.id, reason, details or "")

        return True


router.message.outer_middleware(ModerationMiddleware(check_flood=True))
router.edited_message.outer_middleware(ModerationMiddleware(check_flood=False))


# ---------- Команды (только админы, в группе) ----------
async def _admin_only(message: Message, bot: Bot) -> bool:
    if is_anon_admin(message):
        return True
    user = message.from_user
    if user is None:
        return False
    try:
        return await is_chat_admin(
            bot,
            message.chat.id,
            user.id,
            fresh=True,
        )
    except TelegramAPIError:
        logging.warning(
            "Не удалось проверить права для команды модерации в %s",
            message.chat.id,
            exc_info=True,
        )
        return False


@router.message(F.chat.type.in_(GROUPS), Command("addword", "delword", "words"))
async def words_cmd(message: Message, command: CommandObject, bot: Bot):
    if not await _admin_only(message, bot):
        return

    chat_id = message.chat.id
    settings = await get_settings(chat_id)
    if not settings:
        return await message.reply("Чат не зарегистрирован. Добавьте бота заново.")

    if command.command == "words":
        items = settings["stopword_list"]
        if not items:
            return await message.reply("Список пуст.")

        reply = "Стоп-слова:\n" + "\n".join(items)
        if len(reply) > MAX_REPLY_LENGTH:
            reply = reply[:MAX_REPLY_LENGTH - 24].rsplit("\n", 1)[0]
            reply += "\n…Список сокращён."
        return await message.reply(reply)

    word = normalize_text(command.args or "").strip()
    if not 2 <= len(word) <= 64:
        return await message.reply(
            f"Использование: /{command.command} слово или фраза (2-64 символа)"
        )

    if command.command == "addword":
        result = await common.pool.execute(
            """
            UPDATE chat_settings
            SET stopword_list = array_append(stopword_list, $2)
            WHERE chat_id = $1
              AND NOT ($2 = ANY(stopword_list))
              AND coalesce(array_length(stopword_list, 1), 0) < $3
            """,
            chat_id,
            word,
            MAX_WORDS,
        )
        if result == "UPDATE 1":
            reply_text = "Добавлено."
        elif result == "UPDATE 0":
            reply_text = f"Уже в списке или достигнут лимит ({MAX_WORDS})."
        else:
            logging.error("Неожиданный результат добавления стоп-слова: %s", result)
            reply_text = "Не удалось обновить список."
    else:
        result = await common.pool.execute(
            """
            UPDATE chat_settings
            SET stopword_list = array_remove(stopword_list, $2)
            WHERE chat_id = $1
            """,
            chat_id,
            word,
        )
        if result == "UPDATE 1":
            reply_text = "Удалено."
        elif result == "UPDATE 0":
            reply_text = "Такого слова нет в списке."
        else:
            logging.error("Неожиданный результат удаления стоп-слова: %s", result)
            reply_text = "Не удалось обновить список."

    await message.reply(reply_text)


@router.message(F.chat.type.in_(GROUPS), Command("flood"))
async def flood_cmd(message: Message, command: CommandObject, bot: Bot):
    if not await _admin_only(message, bot):
        return

    chat_id = message.chat.id
    settings = await get_settings(chat_id)
    if not settings:
        return await message.reply("Чат не зарегистрирован. Добавьте бота заново.")

    args = (command.args or "").split()
    if not args:
        return await message.reply(
            f"Лимит: {settings['flood_limit']} сообщений "
            f"за {settings['flood_window']} сек.\n"
            "Изменить: /flood 5 10 (сообщений, секунд)"
        )
    if len(args) != 2:
        return await message.reply(
            "Использование: /flood 5 10 (сообщений, секунд)"
        )

    try:
        limit, window = map(int, args)
    except ValueError:
        return await message.reply(
            "Использование: /flood 5 10 (сообщений, секунд)"
        )

    if not (2 <= limit <= 30 and 3 <= window <= 60):
        return await message.reply(
            "Допустимо: сообщений 2-30, секунд 3-60."
        )

    result = await common.pool.execute(
        """
        UPDATE chat_settings
        SET flood_limit = $2, flood_window = $3
        WHERE chat_id = $1
        """,
        chat_id,
        limit,
        window,
    )
    if result != "UPDATE 1":
        logging.error(
            "Неожиданный результат обновления flood-настроек для чата %s: %s",
            chat_id,
            result,
        )
        return await message.reply("Не удалось сохранить настройки.")

    await message.reply(
        f"Готово: {limit} сообщений за {window} сек."
    )
