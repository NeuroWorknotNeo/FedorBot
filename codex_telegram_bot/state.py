"""Состояние разговоров (проект, сессия Codex, модель, ветка) в JSON-файле.

Ключ разговора — строка: ``"<chat_id>"`` для обычного чата или
``"<chat_id>:<thread_id>"`` для темы в группе с темами.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger(__name__)


@dataclass
class ChatState:
    project: Optional[str] = None      # абсолютный путь к каталогу проекта
    session_id: Optional[str] = None   # id сессии (thread_id) Codex для `codex exec resume`
    model: Optional[str] = None        # модель (gpt-…) или None — по умолчанию Codex
    effort: Optional[str] = None       # уровень рассуждений (low/medium/high/xhigh/…) или None
    last_model_id: Optional[str] = None  # точная модель последнего запуска (из журнала сессии)
    total_input_tokens: int = 0        # накопленный вход по разговору
    total_output_tokens: int = 0       # накопленный выход по разговору
    branch: Optional[str] = None       # ветка последней задачи /task
    base_branch: Optional[str] = None  # от какой ветки она создана
    compact_summary: Optional[str] = None  # конспект после /compact: уйдёт первым сообщением новой сессии
    updated_at: float = 0.0


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._chats: dict[str, ChatState] = {}
        self.meta: dict = {}  # общие данные бота (например, последние снимки лимитов)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Не удалось прочитать %s (%s), начинаю с пустого состояния", self.path, exc)
            return
        if isinstance(data.get("meta"), dict):
            self.meta = data["meta"]
        known = {f.name for f in fields(ChatState)}
        for chat_id, raw in (data.get("chats") or {}).items():
            if not isinstance(raw, dict):
                continue
            try:
                self._chats[str(chat_id)] = ChatState(**{k: v for k, v in raw.items() if k in known})
            except (TypeError, ValueError):
                log.warning("Пропущена повреждённая запись состояния для чата %s", chat_id)

    def get(self, key: Union[int, str]) -> ChatState:
        """Состояние разговора; обращение считается активностью (дата в /conversations)."""
        key = str(key)
        state = self._chats.get(key)
        if state is None:
            state = ChatState()
            self._chats[key] = state
        state.updated_at = time.time()
        return state

    def all(self) -> dict[str, ChatState]:
        return dict(self._chats)

    def remove(self, key: Union[int, str]) -> Optional[ChatState]:
        return self._chats.pop(str(key), None)

    def save(self) -> None:
        payload = {"chats": {str(k): asdict(v) for k, v in self._chats.items()}, "meta": self.meta}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self.path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
