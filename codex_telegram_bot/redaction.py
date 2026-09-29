"""Затирание секретов в исходящем тексте.

Это защита в глубину, а НЕ граница безопасности. Codex выполняется от имени
того же пользователя, что и бот, поэтому в принципе может прочитать любой файл
этого пользователя (в том числе ``.env`` и собственные учётные данные в
``~/.codex/auth.json``). Настоящая защита — держать список тех, кто может
писать боту, минимальным. См. раздел «Безопасность» в README.

Здесь мы ловим самый частый способ утечки: модель по просьбе пользователя
печатает секрет в ответ. Мы затираем:

* точные значения известных боту секретов (токен Telegram, ключи OpenAI,
  токены GitHub, а также токены входа Codex из ``auth.json``) — самый надёжный
  случай;
* строки, похожие по форме на известные типы токенов, — на случай, если Codex
  прочитает секрет из чужого файла, а не из переменной окружения.

Затирание не ловит секрет, который модель преобразовала (перекодировала,
перевернула, разбила на части). Поэтому оно не заменяет отзыв утёкшего токена,
а лишь снижает вероятность случайной утечки.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, Optional

PLACEHOLDER = "‹секрет скрыт ботом›"

# Переменные окружения, значения которых никогда нельзя показывать в чате.
_SECRET_ENV_NAMES: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
)

# Секрет короче этого мы не затираем по значению: слишком велик риск задеть
# обычный текст.
_MIN_SECRET_LEN = 12

# Характерные формы секретов (на случай чтения чужого файла, а не переменной).
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Без \b в начале: токен встречается и внутри URL вида api.telegram.org/bot<ТОКЕН>/…
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}\b"),                  # токен Telegram-бота
    # ключи OpenAI API (sk-…, sk-proj-…): длинная строка с цифрами и заглавными,
    # чтобы не задеть обычные слова через дефис вроде имён веток
    re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{32,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT (токены входа ChatGPT)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                 # токены GitHub (ghp_/gho_/…)
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),               # тонкие токены GitHub
)


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def _json_strings(value: object) -> Iterable[str]:
    """Все строковые значения из JSON-структуры (для токенов из auth.json)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def auth_file_secrets(path: Path) -> set[str]:
    """Секреты из ``auth.json`` Codex: API-ключ и токены входа через ChatGPT.

    Берём только длинные значения из полей с ключом и токенами, а не всё подряд
    (там есть и безобидные поля вроде способа входа и даты обновления).
    """
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    values: set[str] = set()
    for key, value in data.items():
        lowered = str(key).lower()
        if "key" in lowered or "token" in lowered:
            values.update(v for v in _json_strings(value) if len(v) >= 20)
    return values


class Redactor:
    """Затирает секреты в тексте. Дешёвая операция: строится один раз.

    Токены из ``auth.json`` Codex периодически обновляет сам, поэтому файл
    перечитывается, когда меняется время его изменения.
    """

    def __init__(
        self,
        literals: Iterable[str] = (),
        env_names: Iterable[str] = (),
        *,
        include_default_env: bool = True,
        auth_file: Optional[Path] = None,
    ) -> None:
        values: set[str] = set()
        names: list[str] = []
        if include_default_env:
            names.extend(_SECRET_ENV_NAMES)
        names.extend(env_names)
        for name in names:
            raw = os.environ.get(name)
            if raw:
                values.add(raw.strip())
        for literal in literals:
            if literal:
                values.add(literal.strip())
        self._static = values
        self._auth_file = auth_file
        self._auth_mtime: Optional[float] = None
        self._auth_values: set[str] = set()
        self._literals: list[str] = []
        self._rebuild()

    def _rebuild(self) -> None:
        # Длинные значения затираем первыми, чтобы не разрезать секрет по общему
        # с другим секретом префиксу.
        self._literals = sorted(
            (value for value in self._static | self._auth_values if len(value) >= _MIN_SECRET_LEN),
            key=len,
            reverse=True,
        )

    def _refresh_auth(self) -> None:
        if self._auth_file is None:
            return
        try:
            mtime = self._auth_file.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._auth_mtime:
            return
        self._auth_mtime = mtime
        self._auth_values = auth_file_secrets(self._auth_file) if mtime is not None else set()
        self._rebuild()

    def __call__(self, text: str) -> str:
        if not text:
            return text
        self._refresh_auth()
        for value in self._literals:
            if value in text:
                text = text.replace(value, PLACEHOLDER)
        for pattern in _PATTERNS:
            text = pattern.sub(PLACEHOLDER, text)
        return text


_redactor: Optional[Redactor] = None


def configure_redactor(
    literals: Iterable[str] = (),
    env_names: Iterable[str] = (),
    auth_file: Optional[Path] = None,
) -> None:
    """Настраивает глобальный редактор (вызывается при старте бота)."""
    global _redactor
    _redactor = Redactor(literals=literals, env_names=env_names, auth_file=auth_file)


def default_auth_file() -> Path:
    """Файл учётных данных Codex: ``$CODEX_HOME/auth.json`` (по умолчанию ``~/.codex``)."""
    return _codex_home() / "auth.json"


def redact(text: str) -> str:
    """Затирает секреты в исходящем тексте. Безопасно вызывать до configure_redactor."""
    global _redactor
    if _redactor is None:
        _redactor = Redactor()
    return _redactor(text)
