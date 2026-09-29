"""Конфигурация бота.

Все параметры читаются из переменных окружения (обычно из файла ``.env``,
который подхватывает systemd через ``EnvironmentFile`` или ``python-dotenv``
при ручном запуске). Описание каждой переменной — в ``.env.example``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Уровни рассуждений Codex (config-ключ model_reasoning_effort), как в каталоге
# моделей Codex 0.159 (`codex debug models`, поле supported_reasoning_levels).
# Какие из них поддерживает конкретная модель, решает сама модель: лишний
# уровень Codex отклонит с понятной ошибкой.
VALID_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Скрытый уровень (только для ALLOWED_USER_IDS, на кнопки не выносится):
# «ultra» у Codex — максимум рассуждений с автоматическим делегированием задач
# субагентам. Дорого по лимитам подписки. «ultracode» принимается как синоним
# (так этот режим называется в боте для Claude Code).
ULTRA_EFFORT = "ultra"
ULTRA_ALIASES = ("ultra", "ultracode")

# Кнопки команды /model: по одной на семейство и у каждой ТОЧНОЕ имя последней
# версии (Astra — самая сильная, Sol — рабочая лошадка для кода, Luna — быстрая
# и экономная). «По умолчанию» — модель, которую выбирает сам Codex. Когда
# выйдет новая версия семейства, ЗАМЕНИТЕ (не добавляйте рядом) строку этого
# семейства; список сверять с каталогом `codex debug models` (модели с
# "visibility": "list"), не по памяти. Тест test_default_model_buttons_one_per_family
# ловит дубли. MODEL_BUTTONS в .env перекрывает этот список целиком (update.sh
# .env не трогает).
DEFAULT_MODEL_BUTTONS = (
    ("default", "По умолчанию"),
    ("gpt-6-astra", "GPT-6 Astra"),
    ("gpt-6-sol", "GPT-6 Sol"),
    ("gpt-6-luna", "GPT-6 Luna"),
)
MODEL_FAMILIES = ("astra", "sol", "luna")

VALID_SANDBOX_MODES = ("danger-full-access", "workspace-write", "read-only")
WEB_SEARCH_MODES = ("live", "cached", "indexed", "disabled")

_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}")

# Флаги `codex exec`, которых нет у `codex exec resume`: с ними продолжение
# сессии падало бы с «unexpected argument». Песочница задаётся CODEX_SANDBOX.
_EXEC_ONLY_FLAGS = (
    "-s", "--sandbox", "-C", "--cd", "--add-dir", "-p", "--profile", "--oss",
    "--local-provider", "--approve-for-me", "--color",
)

DEFAULT_EXTRA_INSTRUCTIONS_TEMPLATE = (
    "You are being driven through a Telegram bot on a server; nobody can answer "
    "questions interactively. If something is ambiguous, make a reasonable "
    "assumption and mention it. Telegram messages are short, so keep answers "
    "concise and put long details into files in the repository when appropriate. "
    "To deliver a file (PDF, image, archive, etc.) to the user's Telegram chat, "
    "output a line by itself `TG_SEND_FILE: <path>` for each file, where <path> is a "
    "file inside the working directory; the bot attaches those files to its reply. "
    "This is a one-shot headless session: the process ends the moment you send your "
    "final reply, so finish all work before replying. You may use sub-agents for complex or "
    "parallel work, but wait for every one of them to finish and collect their results "
    "before you give the final reply — anything still running when your turn ends is "
    "killed and lost. A turn also has a hard wall-clock limit "
    "({turn_limit_seconds} seconds on this server); when it expires the bot kills the "
    "whole process session. If you "
    "must start a long computation that has to outlive the turn, detach it into its own "
    "session with setsid (e.g. `setsid nohup ./run.sh >run.log 2>&1 </dev/null &`), then "
    "finish the turn promptly and tell the user where the log is; a plain `cmd &` or "
    "`nohup cmd &` stays in the same session and dies with it. Such a detached job "
    "still dies if the bot service itself is restarted (systemd kills the whole "
    "control group), so warn the user not to restart the bot while it runs. Never sit "
    "polling a background job inside one turn. "
    "Always answer in the language of the user's message."
)


def default_extra_instructions(timeout_seconds: int) -> str:
    """Инструкции по умолчанию с настоящим пределом хода: агент должен знать реальный лимит."""
    return DEFAULT_EXTRA_INSTRUCTIONS_TEMPLATE.format(turn_limit_seconds=timeout_seconds)


def is_model_name(value: str) -> bool:
    """Похоже на имя модели (gpt-…, o…, codex-…): без пробелов, двоеточий и кавычек."""
    return bool(_MODEL_NAME_RE.fullmatch(value or ""))


class ConfigError(RuntimeError):
    """Ошибка в настройках (отсутствует обязательная переменная и т. п.)."""


_INLINE_COMMENT = re.compile(r"\s+#.*$")


def _quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"


def _clean(raw: str) -> str:
    """Убирает комментарий после значения и кавычки: systemd передаёт «value  # note» целиком."""
    value = raw.strip()
    if not _quoted(value):
        value = _INLINE_COMMENT.sub("", value).strip()
    if _quoted(value):
        value = value[1:-1].strip()
    return value


def _get(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = _clean(raw)
    return value if value != "" else default


def _bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int | None) -> int | None:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc


def _float(name: str, default: float | None) -> float | None:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом, получено: {raw!r}") from exc


def _ids(name: str) -> frozenset[int]:
    raw = _get(name, "") or ""
    ids: set[int] = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name}: '{part}' не похоже на Telegram ID") from exc
    return frozenset(ids)


def _names(name: str) -> tuple[str, ...]:
    """Разбирает список имён переменных окружения через запятую/пробел."""
    raw = _get(name, "") or ""
    parts = [p.strip() for p in raw.replace(";", ",").replace(" ", ",").split(",")]
    return tuple(p for p in parts if p)


def _model_buttons(name: str) -> tuple[tuple[str, str], ...]:
    """Разбирает MODEL_BUTTONS: «значение» или «значение:Подпись» через запятую."""
    raw = _get(name)
    if not raw:
        return DEFAULT_MODEL_BUTTONS
    buttons: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        value, _, label = part.partition(":")
        value = value.strip()
        label = label.strip() or value
        if value != "default" and not is_model_name(value):
            raise ConfigError(
                f"{name}: '{value}' не похоже на модель. Допустимы default или точное имя модели, например gpt-5.5"
            )
        if len(f"model:{value}".encode()) > 64:
            raise ConfigError(f"{name}: имя модели '{value}' слишком длинное для кнопки Telegram (до 58 символов)")
        buttons.append((value, label))
    return tuple(buttons) or DEFAULT_MODEL_BUTTONS


def _web_search(name: str) -> str | None:
    """Режим поиска в интернете: live/cached/indexed/disabled или true/false; «default» — как решит Codex."""
    raw = (_get(name, "live") or "live").lower()
    if raw in {"1", "true", "yes", "on", "да"}:
        return "live"
    if raw in {"0", "false", "no", "off", "нет"}:
        return "disabled"
    if raw == "default":
        return None
    if raw not in WEB_SEARCH_MODES:
        raise ConfigError(f"{name}={raw!r} недопустим. Варианты: true, false, default, {', '.join(WEB_SEARCH_MODES)}")
    return raw


def _reaction(name: str, default: str) -> str | None:
    """Эмодзи реакции; пустая строка или off/none/false отключает."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = _clean(raw)
    if raw == "" or raw.lower() in {"off", "none", "false", "0", "no"}:
        return None
    return raw


def find_codex_binary(explicit: str | None) -> str:
    """Находит исполняемый файл ``codex``.

    Порядок: явный путь из CODEX_BIN → PATH → ~/.local/bin/codex (установщик
    бота) → /usr/local/bin/codex → /usr/bin/codex.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists():
            return str(path)
        raise ConfigError(f"CODEX_BIN указывает на несуществующий файл: {path}")
    found = shutil.which("codex")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        Path("/usr/bin/codex"),
    ):
        if candidate.exists():
            return str(candidate)
    raise ConfigError(
        "Не найден исполняемый файл codex. Установите Codex CLI (bash deploy/install-codex.sh) или задайте CODEX_BIN."
    )


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    team_chat_ids: frozenset[int]
    allow_private_chats: bool
    workspace_per_chat: bool
    group_require_mention: bool
    workspace_dir: Path
    state_file: Path
    uploads_dir: Path
    codex_bin: str
    sandbox: str
    network_access: bool
    web_search: str | None
    default_model: str | None
    model_buttons: tuple[tuple[str, str], ...]
    default_effort: str | None
    timeout_seconds: int
    max_concurrent_runs: int
    extra_args: tuple[str, ...]
    extra_instructions: str | None
    show_tokens: bool
    reaction_working: str | None
    reaction_done: str | None
    allow_shell: bool
    allow_send_files: bool
    allow_any_dir: bool
    shell_timeout_seconds: int
    task_branch_prefix: str
    task_base_branch: str | None
    task_auto_pr: bool
    progress_interval: float
    log_level: str
    timezone: str
    redact_env_names: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Config":
        token = _get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN не задан (получите токен у @BotFather)")

        allowed = _ids("ALLOWED_USER_IDS")
        if not allowed:
            raise ConfigError(
                "ALLOWED_USER_IDS пуст. Бот отказывается работать без белого списка: "
                "иначе любой человек в Telegram сможет выполнять команды на вашем сервере. "
                "Узнать свой ID можно, написав боту /id."
            )

        workspace = Path(_get("WORKSPACE_DIR", "~/workspace") or "~/workspace").expanduser().resolve()
        state_file = Path(_get("STATE_FILE", str(workspace / ".codex-telegram-bot" / "state.json")) or "").expanduser()
        uploads_dir = Path(_get("UPLOADS_DIR", str(workspace / ".codex-telegram-bot" / "uploads")) or "").expanduser()

        sandbox = _get("CODEX_SANDBOX", "danger-full-access") or "danger-full-access"
        if sandbox not in VALID_SANDBOX_MODES:
            raise ConfigError(
                f"CODEX_SANDBOX={sandbox!r} недопустим. Варианты: {', '.join(VALID_SANDBOX_MODES)}"
            )

        timeout = _int("CODEX_TIMEOUT_SECONDS", 10800) or 10800
        if timeout < 30:
            raise ConfigError("CODEX_TIMEOUT_SECONDS слишком мал (минимум 30)")

        concurrency = _int("MAX_CONCURRENT_RUNS", 2) or 2
        if concurrency < 1:
            raise ConfigError("MAX_CONCURRENT_RUNS должен быть >= 1")

        effort = (_get("CODEX_EFFORT") or "").lower() or None
        if effort and effort not in VALID_EFFORT_LEVELS:
            raise ConfigError(f"CODEX_EFFORT={effort!r} недопустим. Варианты: {', '.join(VALID_EFFORT_LEVELS)}")

        web_search = _web_search("CODEX_WEB_SEARCH")

        model = _get("CODEX_MODEL")
        if model and not is_model_name(model):
            raise ConfigError(f"CODEX_MODEL={model!r} не похоже на имя модели (пример: gpt-5.5)")

        extra_raw = _get("CODEX_EXTRA_ARGS", "") or ""
        try:
            extra_args = tuple(shlex.split(extra_raw))
        except ValueError as exc:
            raise ConfigError(f"CODEX_EXTRA_ARGS не разбирается как командная строка: {exc}") from exc
        for arg in extra_args:
            flag = arg.split("=", 1)[0]
            if flag in _EXEC_ONLY_FLAGS:
                raise ConfigError(
                    f"CODEX_EXTRA_ARGS: флаг {flag} не поддерживается при продолжении сессии (codex exec resume). "
                    "Используйте -c ключ=значение; песочница задаётся CODEX_SANDBOX."
                )

        tz_name = _get("TIMEZONE", "UTC") or "UTC"
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"TIMEZONE={tz_name!r} не найден (пример: Europe/Moscow)") from exc

        instructions_raw = os.environ.get("CODEX_EXTRA_INSTRUCTIONS")
        if instructions_raw is None:
            instructions: str | None = default_extra_instructions(timeout)
        else:
            instructions = _clean(instructions_raw) or None

        return cls(
            telegram_token=token,
            allowed_user_ids=allowed,
            allowed_chat_ids=_ids("ALLOWED_CHAT_IDS"),
            team_chat_ids=_ids("TEAM_CHAT_IDS"),
            allow_private_chats=_bool("ALLOW_PRIVATE_CHATS", True),
            workspace_per_chat=_bool("WORKSPACE_PER_CHAT", False),
            group_require_mention=_bool("GROUP_REQUIRE_MENTION", False),
            workspace_dir=workspace,
            state_file=state_file,
            uploads_dir=uploads_dir,
            codex_bin=find_codex_binary(_get("CODEX_BIN")),
            sandbox=sandbox,
            network_access=_bool("CODEX_NETWORK_ACCESS", True),
            web_search=web_search,
            default_model=model,
            model_buttons=_model_buttons("MODEL_BUTTONS"),
            default_effort=effort,
            timeout_seconds=timeout,
            max_concurrent_runs=concurrency,
            extra_args=extra_args,
            extra_instructions=instructions,
            show_tokens=_bool("SHOW_TOKENS", True),
            reaction_working=_reaction("REACTION_WORKING", "👀"),
            reaction_done=_reaction("REACTION_DONE", "👍"),
            allow_shell=_bool("ALLOW_SHELL", True),
            allow_send_files=_bool("ALLOW_SEND_FILES", True),
            allow_any_dir=_bool("ALLOW_ANY_DIR", False),
            shell_timeout_seconds=_int("SHELL_TIMEOUT_SECONDS", 120) or 120,
            task_branch_prefix=_get("TASK_BRANCH_PREFIX", "tg/") or "tg/",
            task_base_branch=_get("TASK_BASE_BRANCH"),
            task_auto_pr=_bool("TASK_AUTO_PR", True),
            progress_interval=float(_float("PROGRESS_INTERVAL_SECONDS", 3.0) or 3.0),
            log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
            timezone=tz_name,
            redact_env_names=_names("REDACT_ENV_NAMES"),
        )
