"""Railway-friendly application entry point with audit UI and message cleanup."""

import asyncio
from aiogram.types import KeyboardButton

import bot as application
import audit_ui
import moderation
import protection
from autodelete import AutoDeleteMiddleware

application.router.include_router(audit_ui.router)

_original_keyboard = application.private_main_keyboard


def private_main_keyboard_with_logs():
    keyboard = _original_keyboard()
    return keyboard.model_copy(update={
        "keyboard": [*keyboard.keyboard, [KeyboardButton(text="📜 Логи")]],
    })


application.private_main_keyboard = private_main_keyboard_with_logs
application.TOGGLE_DESCRIPTIONS["antiflood"] = (
    "Базовая защита всегда включена: не более 5 сообщений за 5 секунд. "
    "При флуде бот удаляет пачку сообщений и применяет мут или бан. "
    "Порог можно только ужесточить кнопкой «Антифлуд» в панели группы."
)

_original_apply_punishment = protection.apply_punishment


async def audited_apply_punishment(bot, chat_id, user_id, punishment):
    await _original_apply_punishment(bot, chat_id, user_id, punishment)
    await protection.write_log(
        chat_id, user_id, f"punishment_{punishment}",
        f"Наказание успешно применено: {punishment}",
    )


# Both routers resolve the function from their module globals.
moderation.apply_punishment = audited_apply_punishment
protection.apply_punishment = audited_apply_punishment

_OriginalBot = application.Bot


class AutoDeleteBot(_OriginalBot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session.middleware.register(AutoDeleteMiddleware(delay=4))


application.Bot = AutoDeleteBot

if __name__ == "__main__":
    asyncio.run(application.main())
