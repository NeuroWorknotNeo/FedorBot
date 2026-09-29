"""Telegram-бот: команды, очередь заданий на каждый разговор, отображение прогресса.

Разговор — это личный чат, группа или тема в группе с темами. У каждого
разговора свой проект, своя сессия Codex и своя очередь заданий.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
    ReplyParameters,
    TelegramObject,
)

from . import git_tasks, quota, sessions
from .app_server import AppServerError
from .codex_runner import (
    AUTH_HINT,
    CONTEXT_OVERFLOW_HINT,
    USAGE_LIMIT_HINT,
    CodexRunner,
    RunProgress,
    RunResult,
    codex_login_status,
    codex_version,
    describe_auth,
    looks_like_auth_error,
    looks_like_context_overflow,
    looks_like_usage_limit,
)
from .config import ULTRA_ALIASES, ULTRA_EFFORT, VALID_EFFORT_LEVELS, Config, is_model_name
from .formatting import escape, format_bytes, format_duration, format_tokens, render_chunks, strip_html, truncate
from .redaction import redact
from .state import ChatState, StateStore

log = logging.getLogger(__name__)

EFFORT_CHOICES = [("default", "По умолчанию")] + [(level, level) for level in VALID_EFFORT_LEVELS]
GROUP_TYPES = {"group", "supergroup"}
# Базовая пауза перед повтором при обрыве связи с api.telegram.org (удваивается).
NETWORK_RETRY_BASE_DELAY = 1.0
CHAT_TYPE_RU = {"private": "личные сообщения", "group": "группа", "supergroup": "супергруппа", "channel": "канал"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

BOT_COMMANDS = [
    ("t", "Сообщение для людей: бот его не читает (/t текст)"),
    ("ask", "Отправить сообщение Codex: /ask текст"),
    ("help", "Справка"),
    ("status", "Проект, сессия, модель, что выполняется"),
    ("new", "Начать новый диалог с Codex"),
    ("context", "Сколько занято в контексте текущего диалога"),
    ("compact", "Сжать историю диалога: /compact [что сохранить]"),
    ("projects", "Список проектов в workspace"),
    ("project", "Выбрать проект: /project имя"),
    ("clone", "Клонировать репозиторий: /clone url [имя]"),
    ("task", "Задача для GitHub: ветка, коммит, push, PR"),
    ("model", "Модель: кнопки или /model имя|default"),
    ("effort", "Рассуждения: /effort low|medium|high|xhigh|max|default"),
    ("stop", "Остановить текущую задачу и очистить очередь"),
    ("forget", "Забыть этот разговор и удалить его файлы на сервере (перед удалением темы)"),
    ("sh", "Команда в каталоге проекта: /sh git status"),
    ("id", "Показать свой Telegram ID и ID чата"),
]

HELP_TEXT = """🤖 <b>Codex в Telegram</b>

Напишите сообщение — оно уйдёт в Codex (агент OpenAI), запущенный в текущем проекте на сервере. Диалог продолжается (сессия сохраняется между сообщениями), пока вы не отправите /new.

<b>Проекты</b>
/projects — каталоги в workspace
/project имя — выбрать проект (сессия сбрасывается)
/clone url [имя] — клонировать репозиторий в workspace

<b>Задачи для GitHub</b>
/task описание — Codex создаст ветку от основной, внесёт изменения, прогонит тесты, сделает коммит, push и pull request. Следующие обычные сообщения продолжают работу в той же ветке и сессии.

<b>В группе</b>
Добавьте бота в группу и сделайте его администратором: без этого Telegram не передаёт ботам обычные сообщения. Дальше пишите как обычно: всё уходит в Codex. Хотите сказать что-то людям, а не боту — начните сообщение с /t. Реакция 👀 значит, что бот принял сообщение в работу, 👍 — ответил. В группах с темами у каждой темы свой проект и своя сессия.

<b>Прочее</b>
/status — что выбрано и что выполняется
/context — сколько занято в контексте диалога (модель, токены, окно контекста)
/compact — сжать историю диалога средствами Codex, чтобы освободить контекст; диалог продолжается в той же сессии
/compact что сохранить — Codex напишет конспект с учётом просьбы, и диалог продолжится в новой сессии с этим конспектом
/model — выбрать модель (кнопки) или /model имя, /model default
/effort low|medium|high|xhigh|max|default — сколько «думать» над ответом (выше — точнее, но дольше и дороже по лимитам)
/stop — остановить текущее задание
/forget — забыть этот разговор: удалить его каталог проектов, журнал сессии и настройки (делайте перед удалением темы в группе)
/sh команда — выполнить команду в каталоге проекта (например, /sh git log -5)
/id — ваш Telegram ID и ID чата

Файлы и фото тоже можно присылать: они сохраняются на сервере, а Codex получает путь к ним (картинки он ещё и видит). И наоборот — попросите сделать файл (PDF, картинку, архив), и Codex пришлёт его прямо в чат."""

# Промпт для /compact: Codex в режиме exec не выполняет слеш-команды, поэтому
# сжатие делается так: модель пишет конспект разговора, бот начинает новую
# сессию и отправляет конспект первым сообщением вместе со следующим запросом.
COMPACT_PROMPT = """The conversation so far is about to be compacted: its history will be replaced with the summary you write now, so another run of you can continue seamlessly in a fresh session.

Write a handoff summary of this whole conversation. Include:
- the user's goals and requests, and any preferences, constraints or decisions made;
- what has been done so far (files created or changed with their paths, commands run and their outcome, branches, commits, PRs);
- the current state and what remains to be done (clear next steps), including open questions;
- any critical data, identifiers, snippets or references needed to continue.

Do not call any tools or run any commands for this; answer only with the summary itself, concise but complete, in the language of the conversation.{extra}"""

COMPACT_CONTEXT_PREFIX = (
    "[Summary of the earlier part of this conversation. The previous history was compacted "
    "to free up context; treat this summary as what you already know.]\n\n{summary}\n\n"
    "[End of summary. The user's new message follows.]\n\n{prompt}"
)


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command=name, description=desc) for name, desc in BOT_COMMANDS]


# --------------------------------------------------------------------------- разговоры
def conv_key(message: Message) -> str:
    """Ключ разговора: чат, а для темы в группе с темами — чат:тема."""
    if message.is_topic_message and message.message_thread_id:
        return f"{message.chat.id}:{message.message_thread_id}"
    return str(message.chat.id)


def thread_of(message: Message) -> Optional[int]:
    return message.message_thread_id if message.is_topic_message else None


def is_group(message: Message) -> bool:
    return message.chat.type in GROUP_TYPES


def command_name(message: Message) -> str:
    """«/task@bot args» → «/task»; пустая строка, если это не команда."""
    text = (message.text or message.caption or "").strip()
    if not text.startswith("/"):
        return ""
    return text.split()[0].split("@", 1)[0].lower()


def addressed_to_bot(message: Message, bot_username: Optional[str], bot_id: Optional[int]) -> bool:
    """Сообщение обращено к боту: ответ на его сообщение или упоминание @бота."""
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and bot_id is not None and reply.from_user.id == bot_id:
        return True
    if bot_username:
        text = message.text or message.caption or ""
        return f"@{bot_username.lower()}" in text.lower()
    return False


def strip_mention(text: str, bot_username: Optional[str]) -> str:
    if not bot_username:
        return text.strip()
    return re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE).strip()


def choice_keyboard(kind: str, choices: list[tuple[str, str]], current: Optional[str]) -> InlineKeyboardMarkup:
    """Кнопки выбора модели/усилий; текущий вариант отмечен галочкой."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for value, label in choices:
        mark = "✅ " if (current or "default") == value else ""
        row.append(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"{kind}:{value}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- доступ
class AccessMiddleware(BaseMiddleware):
    """Белый список пользователей и, если задан, чатов; запрет личных сообщений."""

    def __init__(
        self,
        allowed_user_ids: frozenset[int],
        allowed_chat_ids: frozenset[int] = frozenset(),
        allow_private_chats: bool = True,
        team_chat_ids: frozenset[int] = frozenset(),
    ):
        self.allowed_user_ids = allowed_user_ids
        self.allowed_chat_ids = allowed_chat_ids
        self.allow_private_chats = allow_private_chats
        self.team_chat_ids = team_chat_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            user = event.from_user
            chat = event.message.chat if isinstance(event.message, Message) else None
            if chat is None or not self.chat_allowed(chat) or not self.user_allowed(user.id, chat):
                await event.answer("⛔ Нет доступа")
                return None
            return await handler(event, data)
        if not isinstance(event, Message):
            return await handler(event, data)
        user = event.from_user
        chat = event.chat
        command = command_name(event)
        user_ok = user is not None and self.user_allowed(user.id, chat)

        if chat.type == "private" and not self.allow_private_chats:
            if user_ok and command in {"/start", "/id"}:
                await event.answer(
                    "Бот настроен отвечать только в группе (ALLOW_PRIVATE_CHATS=false).\n"
                    f"Ваш Telegram ID: <code>{user.id}</code>. Напишите /id в нужной группе, чтобы узнать её ID."
                )
            return None

        if not self.chat_allowed(chat):
            if user_ok and command == "/id":
                await event.answer(
                    f"Этот чат не в списке ALLOWED_CHAT_IDS. ID чата: <code>{chat.id}</code>, ваш ID: <code>{user.id}</code>.\n"
                    "Добавьте ID чата в .env и перезапустите сервис."
                )
            else:
                log.info("Игнорирую сообщение из чата %s (%s): чат не в белом списке", chat.id, chat.type)
            return None

        if not user_ok:
            log.warning(
                "Отклонено сообщение от пользователя %s (%s) в чате %s",
                getattr(user, "id", None), getattr(user, "username", None), chat.id,
            )
            if user is not None and (command == "/id" or (command == "/start" and chat.type == "private")):
                await event.answer(
                    f"⛔ Доступ запрещён. Ваш Telegram ID: <code>{user.id}</code>\n"
                    "Добавьте его в ALLOWED_USER_IDS в файле .env бота и перезапустите сервис."
                )
            return None

        return await handler(event, data)

    def chat_allowed(self, chat: Any) -> bool:
        if chat.type == "private":
            return self.allow_private_chats
        if chat.id in self.team_chat_ids:
            return True
        return not self.allowed_chat_ids or chat.id in self.allowed_chat_ids

    def user_allowed(self, user_id: int, chat: Any) -> bool:
        """Белый список пользователей, а в командных чатах — любой участник."""
        if user_id in self.allowed_user_ids:
            return True
        return chat.type in GROUP_TYPES and chat.id in self.team_chat_ids


# --------------------------------------------------------------------------- задания
@dataclass
class Job:
    chat_id: int
    prompt: str
    source: Message
    kind: str = "chat"  # chat | task | compact
    new_session: bool = False
    task_text: Optional[str] = None
    key: str = ""
    thread_id: Optional[int] = None
    images: list[str] = field(default_factory=list)  # картинки, которые Codex получит вместе с промптом
    instructions: Optional[str] = None  # /compact: что сохранить (тогда сжатие через конспект)
    note: Optional[str] = None  # пояснение, которое бот допишет перед ответом
    created_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.key:
            self.key = str(self.chat_id)


def make_job(message: Message, prompt: str, **kwargs: Any) -> Job:
    return Job(
        chat_id=message.chat.id,
        prompt=prompt,
        source=message,
        key=conv_key(message),
        thread_id=thread_of(message),
        **kwargs,
    )


@dataclass
class ChatRuntime:
    queue: "asyncio.Queue[Job]" = field(default_factory=asyncio.Queue)
    worker: Optional[asyncio.Task] = None
    run_task: Optional[asyncio.Task] = None
    cancel: Optional[asyncio.Event] = None
    current: Optional[Job] = None
    started_at: Optional[float] = None
    waiting: bool = False  # ждём свободный слот/замок проекта, запуск ещё не начался


def render_progress(progress: RunProgress) -> str:
    head = f"⏳ Работаю… {format_duration(progress.elapsed)}"
    if progress.total_tool_calls:
        head += f" · инструментов: {progress.total_tool_calls}"
    if progress.model:
        head += f" · {escape(progress.model)}"
    lines = [head]
    for call in progress.tool_calls:
        lines.append(escape(truncate(call.label(), 120)))
    if progress.plan:
        done = sum(1 for _, completed in progress.plan if completed)
        current = next((text for text, completed in progress.plan if not completed), None)
        line = f"📋 План: {done}/{len(progress.plan)}"
        if current:
            line += f" · сейчас: {current}"
        lines.append(escape(truncate(line, 150)))
    if progress.agents:
        lines.append(f"🤖 Агенты: {progress.agents_running} работают · {progress.agents_done} готово")
        for agent in list(progress.agents.values())[-6:]:
            if agent["done"] and agent["failed"]:
                icon = "❌"
            elif agent["done"]:
                icon = "✅"
            else:
                icon = "⏳"
            lines.append(escape(truncate(f"{icon} {agent.get('desc') or 'агент'}", 90)))
    if progress.reasoning:
        lines.append(f"🤔 {escape(truncate(progress.reasoning, 150))}")
    if progress.last_text:
        lines.append(f"💬 <i>{escape(truncate(progress.last_text, 300))}</i>")
    for notice in progress.notices:
        lines.append(escape(notice))
    lines.append("")
    lines.append("/stop — остановить")
    return "\n".join(lines)


class ProgressReporter:
    """Периодически редактирует одно сообщение с ходом выполнения."""

    def __init__(self, bot: Bot, message: Message, interval: float):
        self.bot = bot
        self.message = message
        self.interval = interval
        self._progress: Optional[RunProgress] = None
        self._last_text: Optional[str] = None
        self._task = asyncio.create_task(self._loop())

    def update(self, progress: RunProgress) -> None:
        self._progress = progress

    def reset(self) -> None:
        self._progress = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Не удалось обновить сообщение о прогрессе")

    async def _flush(self) -> None:
        if self._progress is None:
            return
        text = render_progress(self._progress)
        if text == self._last_text:
            return
        if await self._edit(text):
            self._last_text = text

    async def _edit(self, text: str) -> bool:
        text = redact(text)  # прогресс и итоговая строка тоже могут содержать секрет
        try:
            await self.message.edit_text(text)
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            return False
        except TelegramNetworkError as exc:
            # Связь с Telegram пропала: прогресс не критичен, ответ отправится позже с повторами.
            log.warning("Сеть Telegram недоступна при обновлении сообщения: %s", exc)
            return False
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return True
            log.debug("edit_text отклонён: %s", exc)
            return False

    async def stop(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def finish(self, text: str) -> None:
        await self._edit(text)


async def send_long(
    bot: Bot,
    chat_id: int,
    markdown: str,
    reply_to: Optional[Message] = None,
    thread_id: Optional[int] = None,
) -> None:
    """Отправляет Markdown-текст любой длины несколькими HTML-сообщениями."""
    markdown = redact(markdown)  # секреты не должны утечь в чат
    for index, chunk in enumerate(render_chunks(markdown)):
        reply = None
        if index == 0 and reply_to is not None:
            reply = ReplyParameters(message_id=reply_to.message_id, allow_sending_without_reply=True)
        await _send_chunk(bot, chat_id, chunk, reply, thread_id)


async def _send_chunk(
    bot: Bot, chat_id: int, chunk: str, reply: Optional[ReplyParameters], thread_id: Optional[int]
) -> None:
    delay = NETWORK_RETRY_BASE_DELAY
    for attempt in range(5):
        try:
            await bot.send_message(chat_id, chunk, reply_parameters=reply, message_thread_id=thread_id)
            return
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
        except TelegramNetworkError as exc:
            # Обрыв связи с api.telegram.org: ответ терять нельзя, ждём и пробуем снова.
            if _network_error_is_final(exc):
                raise
            log.warning("Сеть Telegram недоступна (%s), повтор через %.0f с", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "parse" in text or "entit" in text or "tag" in text:
                await bot.send_message(
                    chat_id, strip_html(chunk), parse_mode=None, reply_parameters=reply, message_thread_id=thread_id
                )
                return
            if "replied message not found" in text and reply is not None:
                reply = None
                continue
            raise
    await bot.send_message(chat_id, chunk, reply_parameters=reply, message_thread_id=thread_id)


# --------------------------------------------------------------- отправка файлов
SEND_FILE_MARKER = "TG_SEND_FILE:"
MAX_SEND_BYTES = 50 * 1024 * 1024  # лимит Telegram Bot API на файл от бота
# Имена, которые не отправляем никогда, даже если оказались в рабочем каталоге.
SENSITIVE_SEND_BASENAMES = {
    ".env", ".env.local", ".credentials.json", "credentials.json", "auth.json",
    "id_rsa", "id_ed25519", ".netrc", ".git-credentials", ".pgpass",
}
_SEND_FILE_RE = re.compile(r"^\s*`?\s*TG_SEND_FILE:\s*(.+?)\s*`?\s*$")


def parse_send_files(text: str) -> tuple[str, list[str]]:
    """Вырезает строки-маркеры ``TG_SEND_FILE:`` и возвращает (текст без них, пути)."""
    kept: list[str] = []
    paths: list[str] = []
    for line in text.splitlines():
        match = _SEND_FILE_RE.match(line)
        if match:
            paths.append(match.group(1).strip().strip("`").strip())
        else:
            kept.append(line)
    return "\n".join(kept).strip(), paths


def resolve_outbound_file(raw: str, roots: list[Path]) -> Optional[Path]:
    """Путь к файлу для отправки — только внутри разрешённых каталогов и не секрет.

    Это защита от вывода произвольных файлов сервера (``.env``, учётные данные)
    в обход затирания текста: отправлять можно лишь то, что лежит в рабочем
    каталоге разговора.
    """
    if not raw or not roots:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    safe_roots = []
    for root in roots:
        try:
            safe_roots.append(root.resolve())
        except OSError:
            pass
    if not any(resolved == sr or sr in resolved.parents for sr in safe_roots):
        return None
    if resolved.name in SENSITIVE_SEND_BASENAMES:
        return None
    if not resolved.is_file():
        return None
    return resolved


def _network_error_is_final(exc: Exception) -> bool:
    """Соединение закрыто вместе с сессией бота (остановка сервиса).

    Повторять бессмысленно: HTTP-клиент уже закрыт, а ожидание только затягивает
    остановку. В логе это видно как «ClientConnectionError: Connector is closed».
    """
    return "connector is closed" in str(exc).lower()


async def answer_with_retry(message: Message, text: str, **kwargs: Any) -> Message:
    """``message.answer`` с повтором при обрыве связи с Telegram и затиранием секретов.

    Служебные сообщения бота («⏳ Запускаю…», очередь, текст ошибки) отправляются
    отдельно от ``send_long``: без повтора короткий сетевой сбой превращался бы в
    «❌ Внутренняя ошибка бота», а сам ответ Codex терялся. Затирание нужно ещё и
    потому, что текст исключения aiohttp может содержать URL вида
    ``api.telegram.org/bot<ТОКЕН>/sendMessage``.
    """
    text = redact(text)
    delay = NETWORK_RETRY_BASE_DELAY
    for _ in range(5):
        try:
            return await message.answer(text, **kwargs)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
        except TelegramNetworkError as exc:
            if _network_error_is_final(exc):
                raise
            log.warning("Сеть Telegram недоступна (%s), повтор через %.0f с", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    return await message.answer(text, **kwargs)


def memory_totals_gb() -> tuple[Optional[float], Optional[float]]:
    """(всего ОЗУ, всего swap) в ГБ из /proc/meminfo; (None, None) если недоступно."""
    values: dict[str, float] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name in {"MemTotal", "SwapTotal"}:
                    values[name] = int(rest.split()[0]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None, None
    return values.get("MemTotal"), values.get("SwapTotal")


def concurrency_warning(runs: int, ram_gb: Optional[float], swap_gb: Optional[float]) -> Optional[str]:
    """Предупреждение, если одновременных запусков больше, чем вынесет память.

    Сам codex лёгкий (100–300 МБ), но команды, которые он запускает (тесты,
    сборки, расчёты), легко берут гигабайт и больше. При нехватке памяти
    OOM-killer убивает процессы запуска или самого бота, и работа обрывается.
    """
    if not ram_gb:
        return None
    safe = max(1, int(ram_gb - 1.2))
    if runs <= safe:
        return None
    note = (
        f"MAX_CONCURRENT_RUNS={runs} велик для {ram_gb:.1f} ГБ ОЗУ (безопасно около {safe}): "
        "запуск codex вместе с командами, которые он выполняет (тесты, сборки), берёт до 1 ГБ и больше, "
        "при нехватке памяти OOM-killer убьёт процессы запуска или самого бота, а текущие запуски оборвутся."
    )
    if not swap_gb:
        note += " Swap не настроен, запаса нет."
    return note


# Точные тексты Codex о пропавшей сессии (0.159: «Error: thread/resume: thread/resume
# failed: no rollout found for thread id <id> (code -32600)»). Искать только их:
# широкие совпадения ловили бы и обычный ответ модели со словами «session not found».
MISSING_SESSION_MARKERS = ("no rollout found for thread id", "no saved session found")


def _looks_like_missing_session_text(text: str) -> bool:
    text = text.lower()
    return any(marker in text for marker in MISSING_SESSION_MARKERS)


def _looks_like_missing_session(result: RunResult) -> bool:
    """Codex не нашёл сессию для resume: запуск упал сразу, ни одного хода, текст ошибки — в stderr."""
    if not result.is_error or result.timed_out or result.cancelled:
        return False
    if result.subtype != "no_result" or result.progress.turns:
        return False
    return _looks_like_missing_session_text(result.stderr)


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._")
    return cleaned[:120] or "file"


def normalize_repo_url(url: str) -> str:
    url = url.strip()
    if re.fullmatch(r"[\w.\-]+/[\w.\-]+", url):
        return f"https://github.com/{url}.git"
    return url


def repo_name_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return safe_filename(tail) or "repo"


def context_report(
    info: Optional[sessions.SessionInfo], *, model_hint: Optional[str], has_session: bool = True
) -> str:
    """Отчёт /context по журналу сессии Codex (без обращения к модели)."""
    if info is None:
        if has_session:
            lines = [
                "Журнал этой сессии на сервере не найден (удалён или ещё не записан). "
                "Если его нет, следующее сообщение начнёт новую сессию."
            ]
        else:
            lines = ["Данные о контексте появятся после первого ответа Codex."]
        if model_hint:
            lines.append(f"**Модель:** {model_hint}")
        return "\n".join(lines)
    lines = []
    model = info.model or model_hint
    if model:
        lines.append(f"**Модель:** {model}" + (f" · рассуждения: {info.effort}" if info.effort else ""))
    if info.context_tokens is not None and info.context_window:
        percent = 100 * info.context_tokens / info.context_window
        lines.append(
            f"**Контекст:** {format_tokens(info.context_tokens)} из {format_tokens(info.context_window)} "
            f"токенов ({percent:.0f}%), свободно ~{max(0, 100 - percent):.0f}%"
        )
    elif info.context_tokens is not None:
        lines.append(f"**Контекст:** {format_tokens(info.context_tokens)} токенов (размер окна неизвестен)")
    elif info.context_window:
        lines.append(f"**Окно контекста:** {format_tokens(info.context_window)} токенов")
    if info.total_input_tokens is not None:
        total = f"**Всего за сессию:** ↑{format_tokens(info.total_input_tokens)}"
        if info.total_cached_tokens:
            total += f" (из них кэш {format_tokens(info.total_cached_tokens)})"
        total += f" ↓{format_tokens(info.total_output_tokens or 0)}"
        if info.total_reasoning_tokens:
            total += f" (рассуждения {format_tokens(info.total_reasoning_tokens)})"
        lines.append(total)
    if info.user_messages:
        lines.append(f"**Сообщений в сессии:** {info.user_messages}")
    if info.compactions:
        lines.append(f"**Сжатий истории:** {info.compactions}")
    if not lines:
        lines.append("Codex ещё не сообщил расход токенов в этой сессии.")
    lines.append("")
    lines.append("Когда контекст подходит к пределу, Codex сжимает историю сам; вручную — /compact.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- движок
class Engine:
    def __init__(self, bot: Bot, config: Config, state: StateStore):
        self.bot = bot
        self.config = config
        self.state = state
        self.runner = CodexRunner(config)
        self._runtimes: dict[str, ChatRuntime] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent_runs)
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._shutting_down = False
        self.concurrency_note: Optional[str] = None
        self.codex_version_str = "неизвестно"
        self.bot_username: Optional[str] = None
        self.bot_id: Optional[int] = None
        self.tz = ZoneInfo(config.timezone)

    async def startup(self) -> None:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.config.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.codex_version_str = await codex_version(self.config.codex_bin, env=self.runner.build_env())
        try:
            me = await self.bot.get_me()
            self.bot_username = me.username
            self.bot_id = me.id
        except Exception:  # noqa: BLE001
            log.exception("Не удалось получить данные бота (get_me)")
        log.info("Codex: %s (%s)", self.codex_version_str, self.config.codex_bin)
        auth = await self.auth_status()
        if auth.get("loggedIn"):
            log.info("Авторизация Codex: %s", describe_auth(auth))
        else:
            log.warning("Авторизация Codex: %s", describe_auth(auth))
        if os.environ.get("CODEX_API_KEY"):
            log.warning(
                "В окружении задан CODEX_API_KEY: codex exec предпочтёт его входу через ChatGPT, "
                "и запросы будут оплачиваться по тарифам API. Уберите ключ из .env, если нужна подписка."
            )
        log.info("Workspace: %s; песочница Codex: %s", self.config.workspace_dir, self.config.sandbox)
        ram_gb, swap_gb = memory_totals_gb()
        self.concurrency_note = concurrency_warning(self.config.max_concurrent_runs, ram_gb, swap_gb)
        if self.concurrency_note:
            log.warning("%s", self.concurrency_note)
        if self.config.allowed_chat_ids:
            log.info("Разрешённые чаты: %s", sorted(self.config.allowed_chat_ids))
        if not self.config.allow_private_chats:
            log.info("Личные сообщения отключены (ALLOW_PRIVATE_CHATS=false)")

    async def auth_status(self) -> dict:
        status = await codex_login_status(self.config.codex_bin, self.runner.build_env())
        if os.environ.get("CODEX_API_KEY"):
            status["api_key_env"] = True
        return status

    def remember_rate_limits(self, rate_limits: dict, seen_at: Optional[float] = None) -> None:
        """Запоминает снимок лимитов по окнам (более старый не перекрывает новый); хранится в state.json."""
        self.state.meta["rate_limits"] = quota.merge_snapshot(
            self.state.meta.get("rate_limits"), rate_limits, seen_at if seen_at is not None else time.time()
        )

    def rate_limit_snapshots(self) -> dict:
        snapshots = self.state.meta.get("rate_limits")
        return snapshots if isinstance(snapshots, dict) else {}

    def conversation_dir(self, key: Union[int, str]) -> Path:
        return self.config.workspace_dir / ("chat_" + str(key).replace(":", "_").replace("-", "m"))

    def conversation_dir_size(self, key: Union[int, str]) -> int:
        """Размер каталога проектов разговора в байтах (0, если workspace общий)."""
        if not self.config.workspace_per_chat:
            return 0
        path = self.conversation_dir(key)
        if not path.is_dir():
            return 0
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
        return total

    async def forget_conversation(self, key: Union[int, str]) -> dict[str, Any]:
        """Останавливает задания разговора, удаляет его каталог проектов, журнал сессии и запись состояния."""
        key = str(key)
        summary: dict[str, Any] = {"key": key, "workspace": None, "workspace_bytes": 0, "transcripts": 0, "state": False}
        self.cancel(key)
        runtime = self._runtimes.pop(key, None)
        if runtime is not None and runtime.run_task is not None and not runtime.run_task.done():
            # Даём запуску остановиться штатно (SIGINT → SIGTERM → SIGKILL), и только
            # потом убираем воркер: иначе codex продолжил бы писать в удаляемый каталог.
            await asyncio.wait({runtime.run_task}, timeout=45)
        if runtime is not None and runtime.worker is not None:
            runtime.worker.cancel()
        state = self.state.remove(key)
        summary["state"] = state is not None
        if self.config.workspace_per_chat:
            path = self.conversation_dir(key)
            if path.is_dir() and path.parent == self.config.workspace_dir and path.name.startswith("chat_"):
                summary["workspace_bytes"] = self.conversation_dir_size(key)
                await asyncio.to_thread(shutil.rmtree, path, True)
                summary["workspace"] = str(path)
        if state is not None and state.session_id:
            # `codex delete --force` убирает и журнал, и записи сессии в базах Codex;
            # оставшиеся файлы (архив, старые форматы) удаляем сами.
            deleted = await self.runner.delete_session(state.session_id)
            summary["transcripts"] = int(deleted) + await asyncio.to_thread(sessions.delete_session_files, state.session_id)
        self.state.save()
        log.info("Разговор %s забыт: %s", key, summary)
        return summary

    async def shutdown(self) -> None:
        # Отличаем остановку из-за перезапуска сервиса от /stop: иначе пользователь
        # видит «⏹ Остановлено», хотя ничего не останавливал, и ждёт ответа впустую.
        self._shutting_down = True
        running = []
        for runtime in self._runtimes.values():
            self._drain(runtime)
            if runtime.cancel is not None:
                runtime.cancel.set()
            if runtime.run_task is not None and not runtime.run_task.done():
                running.append(runtime.run_task)
        if running:
            log.info("Останавливаю %d запущенных процессов codex…", len(running))
            await asyncio.wait(running, timeout=30)

    # ------------------------------------------------------------- проекты
    def workspace_for(self, key: Union[int, str]) -> Path:
        """Каталог проектов разговора: общий или свой на каждый чат (WORKSPACE_PER_CHAT)."""
        if not self.config.workspace_per_chat:
            return self.config.workspace_dir
        path = self.conversation_dir(key)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def project_dir(self, state: ChatState, key: Union[int, str] = "") -> Path:
        if state.project:
            path = Path(state.project)
            if path.is_dir():
                return path
        return self.workspace_for(key) if key != "" else self.config.workspace_dir

    def resolve_project(self, name: str, key: Union[int, str] = "") -> Path:
        name = name.strip()
        workspace = (self.workspace_for(key) if key != "" else self.config.workspace_dir).resolve()
        if name in {"", ".", "/", "~", "workspace"}:
            return workspace
        candidate = Path(name).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / name
        candidate = candidate.resolve()
        if not self.config.allow_any_dir and candidate != workspace and workspace not in candidate.parents:
            raise ValueError(f"Каталог вне workspace ({workspace}). Разрешить: ALLOW_ANY_DIR=true")
        if not candidate.is_dir():
            raise ValueError(f"Каталог не найден: {candidate}")
        return candidate

    def _project_lock(self, project: Path) -> asyncio.Lock:
        key = str(project.resolve())
        lock = self._project_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[key] = lock
        return lock

    # ------------------------------------------------------------- очередь
    def runtime_info(self, key: Union[int, str]) -> Optional[ChatRuntime]:
        return self._runtimes.get(str(key))

    def _runtime(self, key: str) -> ChatRuntime:
        runtime = self._runtimes.get(key)
        if runtime is None:
            runtime = ChatRuntime()
            self._runtimes[key] = runtime
        if runtime.worker is None or runtime.worker.done():
            # Воркер мог умереть (например, от неожиданного исключения) — без перезапуска
            # очередь этого разговора встала бы навсегда.
            if runtime.worker is not None:
                log.warning("Воркер разговора %s не работал, запускаю заново", key)
            runtime.worker = asyncio.create_task(self._worker(key, runtime), name=f"chat-worker-{key}")
        return runtime

    async def react(self, message: Message, emoji: Optional[str]) -> None:
        """Ставит реакцию на сообщение пользователя (None — снимает). Ошибки игнорируются."""
        try:
            reaction = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
            await self.bot.set_message_reaction(message.chat.id, message.message_id, reaction=reaction)
        except Exception as exc:  # noqa: BLE001 — реакции могут быть запрещены в чате
            log.debug("Не удалось поставить реакцию: %s", exc)

    async def submit(self, job: Job) -> None:
        runtime = self._runtime(job.key)
        ahead = runtime.queue.qsize() + (1 if runtime.current is not None else 0)
        await runtime.queue.put(job)
        if self.config.reaction_working:
            await self.react(job.source, self.config.reaction_working)
        if ahead:
            await answer_with_retry(job.source, f"📥 Поставлено в очередь, перед вами заданий: {ahead}. /stop — отменить всё.")

    def cancel(self, key: Union[int, str]) -> tuple[bool, int]:
        """Останавливает текущее задание и очищает очередь. Возвращает (было_запущено, снято_из_очереди)."""
        runtime = self._runtimes.get(str(key))
        if runtime is None:
            return False, 0
        dropped = self._drain(runtime)
        running = runtime.current is not None and runtime.cancel is not None
        if running and runtime.cancel is not None:
            runtime.cancel.set()
        return running, dropped

    @staticmethod
    def _drain(runtime: ChatRuntime) -> int:
        dropped = 0
        while True:
            try:
                runtime.queue.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            runtime.queue.task_done()
            dropped += 1

    async def _worker(self, key: str, runtime: ChatRuntime) -> None:
        while True:
            job = await runtime.queue.get()
            runtime.current = job
            runtime.cancel = asyncio.Event()
            runtime.started_at = time.monotonic()
            try:
                await self._execute(job, runtime)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка выполнения задания в разговоре %s", key)
                try:
                    await answer_with_retry(job.source, f"❌ Внутренняя ошибка бота: {escape(str(exc))[:1000]}")
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось отправить сообщение об ошибке")
            finally:
                runtime.current = None
                runtime.cancel = None
                runtime.started_at = None
                runtime.waiting = False
                runtime.run_task = None
                runtime.queue.task_done()

    # ------------------------------------------------------------- выполнение
    async def _execute(self, job: Job, runtime: ChatRuntime) -> None:
        state = self.state.get(job.key)
        project = self.project_dir(state, job.key)
        lock = self._project_lock(project)
        # Слот и замок проекта могут быть заняты другим запуском. Сообщение
        # отправляем сразу, до ожидания, иначе пользователь видит тишину.
        runtime.waiting = self._semaphore.locked() or lock.locked()
        progress_message = await answer_with_retry(
            job.source,
            "⏳ Жду свободного слота: сейчас выполняются другие запуски. /stop — отменить."
            if runtime.waiting else "⏳ Запускаю Codex…"
        )
        async with self._semaphore, lock:
            was_waiting, runtime.waiting = runtime.waiting, False
            if runtime.cancel is not None and runtime.cancel.is_set():
                await answer_with_retry(job.source, "⏹ Задание отменено, не успев начаться.")
                await self.react(job.source, None)
                return
            prompt = job.prompt
            if job.kind == "task":
                try:
                    branch, base = await self._prepare_task(job, state, project)
                except git_tasks.GitError as exc:
                    await send_long(
                        self.bot, job.chat_id, f"❌ Не удалось подготовить ветку.\n```\n{exc}\n```",
                        reply_to=job.source, thread_id=job.thread_id,
                    )
                    await self.react(job.source, None)
                    return
                prompt = git_tasks.build_task_prompt(
                    job.task_text or job.prompt,
                    repo=project,
                    branch=branch,
                    base=base,
                    remote=await git_tasks.remote_url(project),
                    auto_pr=self.config.task_auto_pr,
                )
            if job.kind == "compact" and not state.session_id:
                await answer_with_retry(job.source, "Сжимать нечего: диалог уже начат заново.")
                await self.react(job.source, None)
                return
            if job.kind == "compact" and not job.instructions:
                # Сначала — сжатие средствами самого Codex (та же сессия); если не
                # вышло, ниже сработает запасной путь через конспект.
                if await self._compact_native(job, state, project, progress_message, runtime):
                    return
            if job.new_session:
                state.session_id = None
                state.compact_summary = None
                self.state.save()

            if was_waiting:
                try:
                    await progress_message.edit_text("⏳ Запускаю Codex…")
                except Exception:  # noqa: BLE001 — сообщение не критично
                    pass
            reporter = ProgressReporter(self.bot, progress_message, self.config.progress_interval)
            typing = asyncio.create_task(self._typing_loop(job.chat_id, job.thread_id))
            try:
                runtime.run_task = asyncio.ensure_future(
                    self._run_with_retry(
                        prompt, project, state, reporter, runtime, images=job.images,
                        # конспект пропавшей сессии в новой сессии бессмыслен — не повторяем
                        retry_missing=job.kind != "compact",
                    )
                )
                result = await runtime.run_task
            finally:
                typing.cancel()
                await reporter.stop()

            if result.session_id:
                state.session_id = result.session_id
                if result.used_compact_summary:
                    state.compact_summary = None  # конспект ушёл первым сообщением новой сессии
            if result.model:
                state.last_model_id = result.model
            if result.rate_limits:
                self.remember_rate_limits(result.rate_limits, result.rate_limits_at)
            state.total_input_tokens += result.input_tokens or 0
            state.total_output_tokens += result.output_tokens or 0
            summary_text = parse_send_files(result.text)[0].strip() if job.kind == "compact" else ""
            if job.kind == "compact" and not (result.is_error or result.cancelled or result.timed_out) and summary_text:
                state.compact_summary = summary_text  # без строк TG_SEND_FILE: они не для новой сессии
                state.session_id = None
            self.state.save()
            await self._deliver(job, result, reporter, state, project)

    async def _compact_native(
        self, job: Job, state: ChatState, project: Path, progress_message: Message, runtime: ChatRuntime
    ) -> bool:
        """Сжатие истории через app-server. True — задание завершено (успехом или отказом), False — нужен конспект."""
        started = time.monotonic()
        try:
            await progress_message.edit_text("🗜 Сжимаю историю средствами Codex… /stop — отменить.")
        except Exception:  # noqa: BLE001 — сообщение не критично
            pass
        typing = asyncio.create_task(self._typing_loop(job.chat_id, job.thread_id))
        try:
            outcome = await self.runner.compact(
                state.session_id or "", project, model=state.model or self.config.default_model, cancel=runtime.cancel
            )
        except AppServerError as exc:
            if _looks_like_missing_session_text(str(exc)):
                state.session_id = None
                self.state.save()
                await self._finish_status(progress_message, "❌ Ошибка", started)
                await send_long(
                    self.bot, job.chat_id,
                    "Журнал этой сессии на сервере не найден — сжимать нечего. Следующее сообщение начнёт новую сессию.",
                    reply_to=job.source, thread_id=job.thread_id,
                )
                await self.react(job.source, None)
                return True
            log.warning("Сжатие через app-server не удалось (%s), делаю конспект", exc)
            job.note = f"ℹ️ Сжатие средствами Codex не удалось ({exc}), поэтому история сжата через конспект."
            return False
        finally:
            typing.cancel()
        if outcome.cancelled or outcome.timed_out:
            if outcome.cancelled and self._shutting_down:
                status, text = "⏹ Прервано перезапуском бота", "Сжатие прервал перезапуск бота. Отправьте /compact заново."
            elif outcome.cancelled:
                status, text = "⏹ Остановлено", "Сжатие прервано. Если Codex успел его сохранить, история уже сжата — проверьте /context."
            else:
                status, text = (
                    "⏱ Остановлено по таймауту",
                    "Сжатие не уложилось в отведённое время. Если Codex успел его сохранить, история уже сжата — проверьте /context.",
                )
            await self._finish_status(progress_message, status, started)
            await send_long(self.bot, job.chat_id, text, reply_to=job.source, thread_id=job.thread_id)
            await self.react(job.source, None)
            return True
        if outcome.status != "completed" or not outcome.compacted:
            reason = outcome.error or f"статус {outcome.status or 'неизвестен'}"
            log.warning("Сжатие через app-server не удалось (%s), делаю конспект", reason)
            job.note = f"ℹ️ Сжатие средствами Codex не удалось ({reason}), поэтому история сжата через конспект."
            return False
        sizes = ""
        if outcome.before_tokens is not None and outcome.after_tokens is not None:
            sizes = f"контекст {format_tokens(outcome.before_tokens)} → {format_tokens(outcome.after_tokens)}"
        await self._finish_status(progress_message, "✅ Готово", started, sizes)
        text = "🗜 История сжата средствами Codex"
        if sizes:
            text += f": {sizes} токенов"
            if outcome.context_window:
                text += f" из {format_tokens(outcome.context_window)}"
        text += ". Диалог продолжается в той же сессии."
        await send_long(self.bot, job.chat_id, text, reply_to=job.source, thread_id=job.thread_id)
        if self.config.reaction_working or self.config.reaction_done:
            await self.react(job.source, self.config.reaction_done)
        return True

    @staticmethod
    async def _finish_status(message: Message, status: str, started: float, extra: str = "") -> None:
        parts = [format_duration(time.monotonic() - started)] + ([extra] if extra else [])
        try:
            await message.edit_text(redact(f"{status} · {' · '.join(parts)}"))
        except Exception:  # noqa: BLE001 — сообщение не критично
            pass

    async def _run_with_retry(
        self,
        prompt: str,
        project: Path,
        state: ChatState,
        reporter: ProgressReporter,
        runtime: ChatRuntime,
        images: Optional[list[str]] = None,
        retry_missing: bool = True,
    ) -> RunResult:
        model = state.model or self.config.default_model
        effort = state.effort or self.config.default_effort

        async def attempt(resume: Optional[str]) -> RunResult:
            text = prompt
            with_summary = resume is None and bool(state.compact_summary)
            if with_summary:
                text = COMPACT_CONTEXT_PREFIX.format(summary=state.compact_summary, prompt=prompt)
            outcome = await self.runner.run(
                text, project, resume=resume, model=model, effort=effort, images=images or [],
                on_progress=reporter.update, cancel=runtime.cancel,
            )
            outcome.used_compact_summary = with_summary
            return outcome

        result = await attempt(state.session_id)
        if result.new_thread:
            result.session_reset = True  # Codex молча начал новую сессию вместо прежней
        if result.is_error and state.session_id and _looks_like_missing_session(result):
            log.warning("Сессия %s не найдена, начинаю новую", state.session_id)
            state.session_id = None
            self.state.save()
            result.session_id = None
            if retry_missing:
                reporter.reset()
                result = await attempt(None)
                result.session_reset = True
        return result

    async def _prepare_task(self, job: Job, state: ChatState, project: Path) -> tuple[str, str]:
        if not await git_tasks.is_git_repo(project):
            raise git_tasks.GitError(
                f"Текущий проект не является git-репозиторием: {project}\n"
                "Выберите репозиторий командой /project или клонируйте его: /clone url"
            )
        branch = git_tasks.make_branch_name(self.config.task_branch_prefix, job.task_text or job.prompt)
        base = await git_tasks.prepare_task_branch(project, branch, self.config.task_base_branch)
        state.branch = branch
        state.base_branch = base
        state.session_id = None
        state.compact_summary = None
        self.state.save()
        await answer_with_retry(
            job.source,
            f"🌿 Создана ветка <code>{escape(branch)}</code> от <code>{escape(base)}</code>. Codex приступает к задаче…"
        )
        return branch, base

    async def _typing_loop(self, chat_id: int, thread_id: Optional[int]) -> None:
        while True:
            try:
                await self.bot.send_chat_action(chat_id, ChatAction.TYPING, message_thread_id=thread_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(4.5)

    async def _deliver(
        self, job: Job, result: RunResult, reporter: ProgressReporter, state: ChatState, project: Path
    ) -> None:
        elapsed = result.duration_ms / 1000 if result.duration_ms else result.progress.elapsed
        parts = [format_duration(elapsed)]
        if result.progress.total_tool_calls:
            parts.append(f"инструментов: {result.progress.total_tool_calls}")
        if self.config.show_tokens and result.input_tokens is not None:
            parts.append(f"токены ↑{format_tokens(result.input_tokens)} ↓{format_tokens(result.output_tokens or 0)}")
        if result.model:
            parts.append(result.model)
        if result.progress.agents:
            parts.append(f"агентов: {len(result.progress.agents)}")
        limit_note = quota.is_near_limit(result.rate_limits)
        if limit_note:
            parts.append(limit_note)
        if result.cancelled and self._shutting_down:
            status = "⏹ Прервано перезапуском бота"
        elif result.cancelled:
            status = "⏹ Остановлено"
        elif result.timed_out:
            status = "⏱ Остановлено по таймауту"
        elif result.is_error:
            status = "❌ Ошибка"
        else:
            status = "✅ Готово"
        await reporter.finish(f"{status} · {' · '.join(parts)}")

        text, send_paths = parse_send_files(result.text.strip())
        if job.kind == "compact" and not (result.is_error or result.cancelled or result.timed_out) and text:
            text = (
                "🗜 История сжата. Следующее сообщение начнёт новую сессию Codex, "
                "и первым в неё уйдёт этот конспект:\n\n" + text
            )
        if job.note:
            text = f"{job.note}\n\n{text}" if text else job.note
        if result.is_error:
            details = []
            if result.subtype and result.subtype not in {"success", "no_result"}:
                details.append(f"Тип ошибки: `{result.subtype}`")
            if result.exit_code not in (None, 0):
                details.append(f"Код выхода: {result.exit_code}")
            if result.stderr and result.stderr not in text:
                details.append("stderr:\n```\n" + result.stderr[-1500:].replace("```", "'''") + "\n```")
            if details:
                text = (text + "\n\n" if text else "") + "\n".join(details)
        if looks_like_auth_error(result):
            text += "\n\n" + AUTH_HINT
        if looks_like_usage_limit(result):
            text += "\n\n" + USAGE_LIMIT_HINT
        if looks_like_context_overflow(result):
            text += "\n\n" + CONTEXT_OVERFLOW_HINT
        if result.cancelled and self._shutting_down:
            text += (
                "\n\n♻️ Запрос прервал перезапуск бота (обновление или рестарт сервиса), "
                "а не команда /stop. Отправьте его заново — контекст разговора сохранён."
            )
        if result.timed_out:
            limit = format_duration(self.config.timeout_seconds)
            text += (
                f"\n\n⏱ Сработал предел на один запрос ({limit}, настройка CODEX_TIMEOUT_SECONDS). "
                "Ответ не сохранён, но файлы, которые Codex успел записать на диск, на месте. "
                "Отправьте запрос заново или попросите продолжить: если прежняя сессия не найдётся, "
                "бот сам начнёт новую и скажет об этом."
            )
        if result.session_reset:
            text += (
                "\n\n♻️ Предыдущая сессия Codex не нашлась на сервере (журнал удалён или повреждён). "
                "Начал новую сессию: контекст прежней беседы потерян."
            )
        if result.cancelled and not text:
            text = (
                "Запуск остановлен до того, как Codex ответил. Сделанное на диске сохранено, "
                "сессия тоже: следующее сообщение продолжит диалог."
            )
        if not text and not send_paths:
            text = "(пустой ответ)"
        if text:
            await send_long(self.bot, job.chat_id, text, reply_to=job.source, thread_id=job.thread_id)
        await self._send_outbound_files(job, project, send_paths)
        success = not (result.is_error or result.cancelled or result.timed_out)
        if self.config.reaction_working or self.config.reaction_done:
            await self.react(job.source, self.config.reaction_done if success else None)

        if job.kind == "task" and state.branch:
            await send_long(self.bot, job.chat_id, await self._task_summary(project, state), thread_id=job.thread_id)

    async def _send_outbound_files(self, job: Job, project: Path, raw_paths: list[str]) -> None:
        """Прикрепляет в чат файлы, помеченные Codex через TG_SEND_FILE (только из рабочего каталога)."""
        if not raw_paths:
            return
        if not self.config.allow_send_files:
            await send_long(
                self.bot, job.chat_id,
                "📎 Codex просил отправить файл, но отправка файлов отключена (ALLOW_SEND_FILES=false).",
                thread_id=job.thread_id,
            )
            return
        roots = [project, self.config.workspace_dir]
        seen: set[Path] = set()
        for raw in raw_paths[:10]:
            path = resolve_outbound_file(raw, roots)
            if path is None:
                await send_long(
                    self.bot, job.chat_id,
                    f"⚠️ Не отправляю <code>{escape(raw)}</code>: файл не найден или вне рабочего каталога.",
                    thread_id=job.thread_id,
                )
                continue
            if path in seen:
                continue
            seen.add(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > MAX_SEND_BYTES:
                await send_long(
                    self.bot, job.chat_id,
                    f"⚠️ Файл <code>{escape(path.name)}</code> слишком большой для Telegram ({format_bytes(size)}, лимит 50 МБ).",
                    thread_id=job.thread_id,
                )
                continue
            try:
                await self.bot.send_document(
                    job.chat_id,
                    FSInputFile(str(path)),
                    caption=escape(path.name),
                    message_thread_id=job.thread_id,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось отправить файл %s: %s", path, exc)
                await send_long(
                    self.bot, job.chat_id,
                    f"⚠️ Не удалось отправить <code>{escape(path.name)}</code>: {escape(str(exc))}",
                    thread_id=job.thread_id,
                )

    async def _task_summary(self, project: Path, state: ChatState) -> str:
        lines = [f"🌿 Ветка задачи: `{state.branch}`"]
        commits = await git_tasks.recent_commits(project, state.base_branch, state.branch or "HEAD")
        if commits:
            lines.append("Коммиты:\n```\n" + commits + "\n```")
        else:
            lines.append("Коммитов в ветке пока нет.")
        try:
            dirty = await git_tasks.dirty_status(project)
        except git_tasks.GitError:
            dirty = ""
        if dirty:
            lines.append("⚠️ Есть незакоммиченные изменения:\n```\n" + dirty[-800:] + "\n```")
        lines.append("Следующие сообщения продолжат работу в этой же ветке и сессии. Новая задача — /task, новый диалог — /new.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- команды
def build_router(engine: Engine) -> Router:
    router = Router(name="codex-telegram-bot")
    cfg = engine.config

    def chat_line(message: Message) -> str:
        chat = message.chat
        if chat.type == "private":
            return "💬 Чат: личные сообщения"
        line = f"💬 Чат: {escape(chat.title or CHAT_TYPE_RU.get(chat.type, chat.type))} (<code>{chat.id}</code>)"
        if message.is_topic_message and message.message_thread_id:
            line += f", тема <code>{message.message_thread_id}</code>"
        return line

    async def submit_text(message: Message, text: str, *, explicit: bool) -> None:
        """Отправляет текст в Codex с учётом правил обращения к боту в группах."""
        if is_group(message):
            if not explicit and cfg.group_require_mention and not addressed_to_bot(message, engine.bot_username, engine.bot_id):
                return
            text = strip_mention(text, engine.bot_username)
        text = text.strip()
        if not text:
            return
        await engine.submit(make_job(message, text))

    def is_owner(user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in cfg.allowed_user_ids

    @router.message(Command("quota", "limits"))
    async def cmd_quota(message: Message) -> None:
        """Скрытая команда: расход и обновление лимитов. Только для ALLOWED_USER_IDS, в меню не показывается."""
        if not is_owner(message.from_user.id if message.from_user else None):
            return
        summary = quota.format_rate_limits(engine.rate_limit_snapshots(), tz=engine.tz)
        await message.answer("⏳ <b>Лимиты подписки</b>\n" + escape(summary))

    def forget_keyboard(key: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Да, забыть", callback_data=f"forget:{key}"),
            InlineKeyboardButton(text="Отмена", callback_data="forget:cancel"),
        ]])

    @router.message(Command("forget"))
    async def cmd_forget(message: Message, command: CommandObject) -> None:
        # Свой разговор может забыть любой допущенный участник (обычно перед
        # удалением темы). Чужой разговор по ключу — только владелец.
        own_key = conv_key(message)
        key = (command.args or "").strip() or own_key
        if key != own_key and not is_owner(message.from_user.id if message.from_user else None):
            await message.answer(
                "🗑 Забыть можно только текущий разговор — отправьте /forget без аргументов. "
                "Удалять чужие разговоры по ключу может только владелец бота."
            )
            return
        state = engine.state.all().get(key)
        size = engine.conversation_dir_size(key)
        details = []
        if state is not None:
            details.append(f"проект: <code>{escape(state.project or 'не выбран')}</code>")
            details.append("сессия: " + ("есть" if state.session_id else "нет"))
        if cfg.workspace_per_chat:
            details.append(f"каталог проектов: {format_bytes(size)}")
        if state is None and size == 0:
            await message.answer(f"Разговор <code>{escape(key)}</code> боту неизвестен, удалять нечего. Список: /conversations")
            return
        await message.answer(
            f"🗑 Забыть разговор <code>{escape(key)}</code>?\n" + "\n".join(f"• {d}" for d in details)
            + "\n\nБудут удалены его каталог проектов (со всеми незапушенными изменениями!), журнал сессии Codex и настройки.",
            reply_markup=forget_keyboard(key),
        )

    @router.callback_query(F.data.startswith("forget:"))
    async def on_forget(callback: CallbackQuery) -> None:
        key = (callback.data or "")[len("forget:"):]
        message = callback.message if isinstance(callback.message, Message) else None
        if key == "cancel":
            await callback.answer("Отменено")
            if message is not None:
                try:
                    await message.edit_text("Удаление отменено.")
                except TelegramBadRequest:
                    pass
            return
        # Кнопку выдал сам бот после проверки в /forget, здесь остаётся защита от
        # подделки: не владелец может подтверждать только разговоры своего чата
        # (сравниваем чат, а не полный ключ — у сообщения с кнопкой в теме форума
        # может не быть пометки темы, и свой же разговор тогда бы не совпал).
        own_chat = str(message.chat.id) if message is not None else None
        if key.split(":")[0] != own_chat and not is_owner(callback.from_user.id):
            await callback.answer("⛔ Чужой разговор может забыть только владелец")
            return
        summary = await engine.forget_conversation(key)
        parts = [f"✅ Разговор <code>{escape(key)}</code> забыт."]
        if summary["workspace"]:
            parts.append(f"Удалён каталог <code>{escape(summary['workspace'])}</code> ({format_bytes(summary['workspace_bytes'])}).")
        if summary["transcripts"]:
            parts.append(f"Удалены файлы журнала сессии: {summary['transcripts']}.")
        if summary["state"]:
            parts.append("Настройки разговора сброшены.")
        parts.append("Теперь тему можно удалять в Telegram.")
        await callback.answer("Готово")
        if message is not None:
            try:
                await message.edit_text("\n".join(parts))
            except TelegramBadRequest:
                pass

    @router.message(Command("conversations"))
    async def cmd_conversations(message: Message) -> None:
        """Скрытая команда: все известные боту разговоры с датой активности и размером на диске."""
        if not is_owner(message.from_user.id if message.from_user else None):
            return
        items = sorted(engine.state.all().items(), key=lambda kv: kv[1].updated_at, reverse=True)
        if not items:
            await message.answer("Бот пока не знает ни одного разговора.")
            return
        lines = ["🗂 <b>Разговоры</b> (ключ · последняя активность · проект · каталог на диске)"]
        for key, state in items:
            when = time.strftime("%d.%m %H:%M", time.localtime(state.updated_at)) if state.updated_at else "?"
            project = Path(state.project).name if state.project else "не выбран"
            size = format_bytes(engine.conversation_dir_size(key)) if cfg.workspace_per_chat else "общий"
            lines.append(f"• <code>{escape(key)}</code> · {when} · {escape(project)} · {size}")
        lines.append("\nЗабыть разговор: /forget ключ (или /forget внутри самой темы)")
        await message.answer("\n".join(lines))

    @router.message(Command("t"))
    async def cmd_human_only(message: Message) -> None:
        """/t текст — сообщение людям, бот его не читает и не отвечает."""
        return

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @router.message(Command("id"))
    async def cmd_id(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else "?"
        chat = message.chat
        lines = [
            f"Ваш Telegram ID: <code>{user_id}</code>",
            f"ID чата: <code>{chat.id}</code> ({CHAT_TYPE_RU.get(chat.type, chat.type)})",
        ]
        if message.is_topic_message and message.message_thread_id:
            lines.append(f"ID темы: <code>{message.message_thread_id}</code>")
        if is_group(message):
            lines.append(f"Чтобы бот работал только здесь: <code>ALLOWED_CHAT_IDS={chat.id}</code> в .env")
        await message.answer("\n".join(lines))

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        key = conv_key(message)
        state = engine.state.get(key)
        project = engine.project_dir(state, key)
        lines = [chat_line(message), f"📁 Проект: <code>{escape(str(project))}</code>"]
        if await git_tasks.is_git_repo(project):
            branch = await git_tasks.current_branch(project)
            lines.append(f"🌿 Ветка: <code>{escape(branch or '?')}</code>")
        else:
            lines.append("🌿 Не git-репозиторий")
        model_line = f"🧠 Модель: {escape(state.model or cfg.default_model or 'по умолчанию Codex')}"
        if state.last_model_id:
            model_line += f" (последний запуск: <code>{escape(state.last_model_id)}</code>)"
        lines.append(model_line)
        lines.append(f"🎯 Рассуждения: {escape(state.effort or cfg.default_effort or 'по умолчанию')}")
        if state.session_id:
            lines.append(f"🔗 Сессия: продолжается (<code>{escape(state.session_id[:8])}…</code>), /new — начать заново")
        elif state.compact_summary:
            lines.append("🔗 Сессия: новая, начнётся с конспекта после /compact")
        else:
            lines.append("🔗 Сессия: новая (начнётся со следующего сообщения)")
        lines.append(f"🔐 Песочница Codex: <code>{escape(cfg.sandbox)}</code>")
        if engine.concurrency_note:
            lines.append(f"⚠️ {escape(engine.concurrency_note)}")
        if state.total_input_tokens or state.total_output_tokens:
            lines.append(
                f"🔤 Токенов за этот разговор: ↑{format_tokens(state.total_input_tokens)} ↓{format_tokens(state.total_output_tokens)} "
                "(↑ вход вместе с кэшем, ↓ ответы)"
            )
        runtime = engine.runtime_info(key)
        if runtime is not None and runtime.current is not None:
            elapsed = format_duration(time.monotonic() - (runtime.started_at or time.monotonic()))
            head = "⏳ Ждёт свободного слота" if runtime.waiting else "▶️ Выполняется"
            lines.append(
                f"{head}: «{escape(truncate(runtime.current.task_text or runtime.current.prompt, 60))}» "
                f"({elapsed}), в очереди: {runtime.queue.qsize()}"
            )
        else:
            lines.append("💤 Сейчас ничего не выполняется")
        lines.append(f"🤖 Codex: {escape(engine.codex_version_str)}")
        lines.append(f"🔑 Авторизация: {escape(describe_auth(await engine.auth_status()))}")
        await message.answer("\n".join(lines))

    @router.message(Command("new"))
    async def cmd_new(message: Message) -> None:
        state = engine.state.get(conv_key(message))
        state.session_id = None
        state.compact_summary = None
        engine.state.save()
        await message.answer("🆕 Сессия сброшена. Следующее сообщение начнёт новый диалог в текущем проекте.")

    @router.message(Command("context"))
    async def cmd_context(message: Message) -> None:
        key = conv_key(message)
        state = engine.state.get(key)
        model_hint = state.last_model_id or state.model or cfg.default_model
        info = None
        if state.session_id:
            info = await asyncio.to_thread(sessions.read_session_info, state.session_id)
        if state.session_id:
            header = "📐 Контекст текущего диалога"
        elif state.compact_summary:
            header = (
                "📐 История сжата (/compact): новая сессия начнётся со следующего сообщения, "
                f"конспект — {format_tokens(len(state.compact_summary) // 3)} токенов (оценка)"
            )
        else:
            header = "📐 Контекст нового диалога (сессии пока нет)"
        report = context_report(info, model_hint=model_hint, has_session=bool(state.session_id))
        await send_long(engine.bot, message.chat.id, f"{header}\n\n{report}", thread_id=thread_of(message))

    @router.message(Command("compact"))
    async def cmd_compact(message: Message, command: CommandObject) -> None:
        state = engine.state.get(conv_key(message))
        if not state.session_id:
            await message.answer("Сжимать нечего: диалог ещё не начат. Сжатие имеет смысл после долгой переписки.")
            return
        instructions = (command.args or "").strip()
        extra = f"\n\nThe user asked to make sure the summary preserves: {instructions}" if instructions else ""
        await engine.submit(make_job(
            message, COMPACT_PROMPT.format(extra=extra), kind="compact",
            instructions=instructions or None, task_text=f"/compact {instructions}".strip(),
        ))

    @router.message(Command("projects"))
    async def cmd_projects(message: Message) -> None:
        key = conv_key(message)
        state = engine.state.get(key)
        workspace = engine.workspace_for(key)
        current = engine.project_dir(state, key)
        entries = sorted(p for p in workspace.iterdir() if p.is_dir() and not p.name.startswith("."))
        lines = [f"📂 Workspace: <code>{escape(str(workspace))}</code>"]
        for path in entries:
            mark = "👉" if path.resolve() == current.resolve() else "•"
            git_mark = " 🌿" if (path / ".git").exists() else ""
            lines.append(f"{mark} <code>{escape(path.name)}</code>{git_mark}")
        if not entries:
            lines.append("(пусто — клонируйте репозиторий: /clone url)")
        lines.append("\nВыбрать: /project имя · корень workspace: /project .")
        await message.answer("\n".join(lines))

    @router.message(Command("project"))
    async def cmd_project(message: Message, command: CommandObject) -> None:
        key = conv_key(message)
        state = engine.state.get(key)
        if not command.args:
            await message.answer(
                f"Текущий проект: <code>{escape(str(engine.project_dir(state, key)))}</code>\n"
                "Использование: /project имя (список — /projects)"
            )
            return
        try:
            path = engine.resolve_project(command.args, key)
        except ValueError as exc:
            await message.answer(f"❌ {escape(str(exc))}")
            return
        state.project = str(path)
        state.session_id = None
        state.compact_summary = None
        state.branch = None
        state.base_branch = None
        engine.state.save()
        extra = ""
        if await git_tasks.is_git_repo(path):
            extra = f"\n🌿 Ветка: <code>{escape(await git_tasks.current_branch(path) or '?')}</code>"
        await message.answer(f"✅ Проект: <code>{escape(str(path))}</code>{extra}\nСессия сброшена.")

    @router.message(Command("clone"))
    async def cmd_clone(message: Message, command: CommandObject) -> None:
        try:
            args = shlex.split(command.args or "")
        except ValueError:
            args = []
        if not args:
            await message.answer("Использование: /clone https://github.com/owner/repo [имя]\nили /clone owner/repo")
            return
        url = normalize_repo_url(args[0])
        name = safe_filename(args[1]) if len(args) > 1 else repo_name_from_url(url)
        workspace = engine.workspace_for(conv_key(message))
        dest = workspace / name
        if dest.exists():
            await message.answer(f"❌ Каталог уже существует: <code>{escape(str(dest))}</code>. Выбрать его: /project {escape(name)}")
            return
        progress = await message.answer(f"⏳ Клонирую <code>{escape(url)}</code>…")
        code, out = await git_tasks.run_cmd(["git", "clone", "--", url, str(dest)], cwd=workspace, timeout=900)
        if code != 0:
            await progress.edit_text(f"❌ git clone завершился с кодом {code}:\n<pre>{escape(out.strip()[-1500:])}</pre>")
            return
        state = engine.state.get(conv_key(message))
        state.project = str(dest)
        state.session_id = None
        state.compact_summary = None
        state.branch = None
        state.base_branch = None
        engine.state.save()
        branch = await git_tasks.current_branch(dest)
        await progress.edit_text(
            f"✅ Клонировано в <code>{escape(str(dest))}</code> (ветка <code>{escape(branch or '?')}</code>) и выбрано как проект.\n"
            "Теперь можно писать сообщения или ставить задачу: /task описание"
        )

    @router.message(Command("model"))
    async def cmd_model(message: Message, command: CommandObject) -> None:
        state = engine.state.get(conv_key(message))
        arg = (command.args or "").strip()
        if not arg:
            last = f"\nПоследний запуск выполняла: <code>{escape(state.last_model_id)}</code>" if state.last_model_id else ""
            await message.answer(
                f"🧠 Текущая модель: <code>{escape(state.model or cfg.default_model or 'по умолчанию')}</code>{last}\n"
                "«По умолчанию» — модель, которую выбирает сам Codex (обычно самая новая для программирования). "
                "Любую другую модель можно задать точным именем: /model gpt-…\n"
                "Выберите кнопкой или напишите /model имя:",
                reply_markup=choice_keyboard("model", list(cfg.model_buttons), state.model),
            )
            return
        if arg in {"default", "reset"}:
            state.model = None
        elif is_model_name(arg):
            state.model = arg
        else:
            await message.answer("❌ Неизвестная модель: укажите точное имя модели OpenAI (например, из кнопок /model) или default")
            return
        engine.state.save()
        await message.answer(f"🧠 Модель: <code>{escape(state.model or 'по умолчанию')}</code>")

    @router.message(Command("effort"))
    async def cmd_effort(message: Message, command: CommandObject) -> None:
        state = engine.state.get(conv_key(message))
        arg = (command.args or "").strip().lower()
        if not arg:
            await message.answer(
                f"🎯 Текущий уровень рассуждений: <code>{escape(state.effort or cfg.default_effort or 'по умолчанию')}</code>\n"
                "Чем выше, тем тщательнее Codex думает, но тем дольше ответ и больше расход лимита подписки.",
                reply_markup=choice_keyboard("effort", EFFORT_CHOICES, state.effort),
            )
            return
        if arg in ULTRA_ALIASES and not is_owner(message.from_user.id if message.from_user else None):
            return  # скрытый режим: только для владельцев из ALLOWED_USER_IDS
        if arg in {"default", "reset"}:
            state.effort = None
        elif arg in ULTRA_ALIASES:
            state.effort = ULTRA_EFFORT
        elif arg in VALID_EFFORT_LEVELS:
            state.effort = arg
        else:
            await message.answer(f"❌ Неизвестный уровень. Варианты: {', '.join(VALID_EFFORT_LEVELS)}, default")
            return
        engine.state.save()
        await message.answer(f"🎯 Рассуждения: <code>{escape(state.effort or 'по умолчанию')}</code>")

    @router.callback_query(F.data.startswith("model:") | F.data.startswith("effort:"))
    async def on_choice(callback: CallbackQuery) -> None:
        kind, _, value = (callback.data or "").partition(":")
        message = callback.message
        if not isinstance(message, Message):
            await callback.answer("Сообщение устарело, отправьте команду заново.")
            return
        state = engine.state.get(conv_key(message))
        if kind == "model":
            if value == "default":
                state.model = None
            elif is_model_name(value):
                state.model = value
            else:
                await callback.answer("Неизвестная модель")
                return
            engine.state.save()
            label = state.model or "по умолчанию"
            await callback.answer(f"Модель: {label}")
            new_text = f"🧠 Модель: <code>{escape(label)}</code>"
            markup = choice_keyboard("model", list(cfg.model_buttons), state.model)
        else:
            if value in ULTRA_ALIASES and not is_owner(callback.from_user.id):
                await callback.answer("⛔ Нет доступа")
                return
            if value == "default":
                state.effort = None
            elif value in ULTRA_ALIASES:
                state.effort = ULTRA_EFFORT
            elif value in VALID_EFFORT_LEVELS:
                state.effort = value
            else:
                await callback.answer("Неизвестный уровень")
                return
            engine.state.save()
            label = state.effort or "по умолчанию"
            await callback.answer(f"Рассуждения: {label}")
            new_text = f"🎯 Рассуждения: <code>{escape(label)}</code>"
            markup = choice_keyboard("effort", EFFORT_CHOICES, state.effort)
        try:
            await message.edit_text(new_text, reply_markup=markup)
        except TelegramBadRequest:
            pass

    @router.message(Command("task"))
    async def cmd_task(message: Message, command: CommandObject) -> None:
        text = (command.args or "").strip()
        if not text:
            await message.answer(
                "Использование: /task описание задачи\n"
                "Пример: /task добавь в README раздел про установку и напиши тесты для parse_config"
            )
            return
        key = conv_key(message)
        state = engine.state.get(key)
        project = engine.project_dir(state, key)
        if not await git_tasks.is_git_repo(project):
            await message.answer(
                f"❌ Текущий проект не git-репозиторий: <code>{escape(str(project))}</code>\n"
                "Сначала /clone url или /project имя"
            )
            return
        await engine.submit(make_job(message, text, kind="task", new_session=True, task_text=text))

    @router.message(Command("ask", "c", "codex"))
    async def cmd_ask(message: Message, command: CommandObject) -> None:
        text = (command.args or "").strip()
        if not text:
            await message.answer("Использование: /ask текст сообщения для Codex")
            return
        await submit_text(message, text, explicit=True)

    @router.message(Command("stop"))
    async def cmd_stop(message: Message) -> None:
        running, dropped = engine.cancel(conv_key(message))
        if not running and not dropped:
            await message.answer("Сейчас ничего не выполняется.")
            return
        parts = []
        if running:
            parts.append("останавливаю текущее задание")
        if dropped:
            parts.append(f"снято из очереди: {dropped}")
        await message.answer("⏹ " + ", ".join(parts).capitalize() + ".")

    @router.message(Command("sh"))
    @router.message(Command("git"))
    async def cmd_shell(message: Message, command: CommandObject) -> None:
        if not is_owner(message.from_user.id if message.from_user else None):
            return  # /sh и /git — прямое выполнение команд, только для ALLOWED_USER_IDS
        if not cfg.allow_shell:
            await message.answer("❌ Выполнение команд отключено (ALLOW_SHELL=false).")
            return
        args = (command.args or "").strip()
        if command.command == "git":
            args = f"git {args}".strip()
        if not args or args == "git":
            await message.answer("Использование: /sh команда (например, /sh git status) или /git log -5")
            return
        key = conv_key(message)
        state = engine.state.get(key)
        project = engine.project_dir(state, key)
        code, out = await git_tasks.run_cmd(["bash", "-lc", args], cwd=project, timeout=cfg.shell_timeout_seconds)
        body = (out.strip() or "(нет вывода)").replace("```", "'''")
        header = f"$ {args}" + (f"\n[код выхода {code}]" if code != 0 else "")
        await send_long(engine.bot, message.chat.id, f"```\n{header}\n{body[-3500:]}\n```", thread_id=thread_of(message))

    @router.message(F.document | F.photo)
    async def on_file(message: Message) -> None:
        if command_name(message) == "/t":
            return
        if is_group(message) and cfg.group_require_mention and not addressed_to_bot(message, engine.bot_username, engine.bot_id):
            return
        dest_dir = cfg.uploads_dir / str(message.chat.id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if message.document is not None:
            downloadable: Any = message.document
            name = safe_filename(message.document.file_name or "file")
        else:
            downloadable = message.photo[-1]
            name = f"photo_{int(time.time())}.jpg"
        path = dest_dir / f"{int(time.time())}_{name}"
        try:
            await engine.bot.download(downloadable, destination=path)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ Не удалось скачать файл: {escape(str(exc))}")
            return
        caption = strip_mention((message.caption or "").strip(), engine.bot_username)
        prompt = caption or "Посмотри присланный файл и кратко опиши, что в нём."
        prompt += f"\n\n[Файл от пользователя сохранён на сервере: {path}]"
        # Картинку Codex получает и напрямую (--image), чтобы увидеть её без лишних шагов.
        images = [str(path)] if path.suffix.lower() in IMAGE_SUFFIXES else []
        await engine.submit(make_job(message, prompt, images=images))

    @router.message(F.text.startswith("/"))
    async def on_unknown_command(message: Message) -> None:
        if is_group(message):
            mention = (message.text or "").split()[0].partition("@")[2]
            if not mention or (engine.bot_username and mention.lower() != engine.bot_username.lower()):
                return  # команда для другого бота или просто «/» в группе
        await message.answer("Неизвестная команда. Список команд: /help")

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        await submit_text(message, message.text or "", explicit=False)

    return router

