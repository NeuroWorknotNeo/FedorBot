"""Точка входа: ``python -m codex_telegram_bot``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("codex_telegram_bot")


def _load_dotenv() -> None:
    """Подхватывает .env при ручном запуске (systemd передаёт переменные сам)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    candidates = [
        os.environ.get("BOT_ENV_FILE"),
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            load_dotenv(candidate, override=False)
            log.info("Загружен файл окружения %s", candidate)
            return


async def run() -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from .bot import AccessMiddleware, Engine, bot_commands, build_router
    from .config import Config
    from .state import StateStore

    config = Config.from_env()
    logging.getLogger().setLevel(config.log_level)

    from .redaction import configure_redactor, default_auth_file

    # Затирание секретов в исходящем тексте: точное значение токена бота,
    # стандартные переменные с секретами, токены входа Codex из auth.json
    # и всё, что перечислено в REDACT_ENV_NAMES.
    configure_redactor(
        literals=[config.telegram_token], env_names=config.redact_env_names, auth_file=default_auth_file()
    )

    bot = Bot(config.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    engine = Engine(bot, config, StateStore(config.state_file))
    dispatcher = Dispatcher()
    access = AccessMiddleware(
        config.allowed_user_ids, config.allowed_chat_ids, config.allow_private_chats, config.team_chat_ids,
        is_open_group=engine.is_open_group,
    )
    dispatcher.message.outer_middleware(access)
    dispatcher.callback_query.outer_middleware(access)
    dispatcher.include_router(build_router(engine))
    dispatcher.shutdown.register(engine.shutdown)

    await engine.startup()
    await bot.set_my_commands(bot_commands())
    log.info("Бот @%s запущен; разрешённые пользователи: %s", engine.bot_username, sorted(config.allowed_user_ids))
    try:
        # my_chat_member — бота добавили в группу или убрали: так бот узнаёт группы, открытые владельцем.
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])
    finally:
        await bot.session.close()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _load_dotenv()
    from .config import ConfigError

    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
