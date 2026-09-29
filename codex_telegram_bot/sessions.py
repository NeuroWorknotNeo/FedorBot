"""Журналы сессий Codex: модель, расход контекста и лимиты подписки.

Codex записывает каждую сессию в ``$CODEX_HOME/sessions/ГГГГ/ММ/ДД/rollout-<время>-<id>.jsonl``
(``CODEX_HOME`` по умолчанию ``~/.codex``). Строка журнала — JSON вида
``{"timestamp": …, "type": …, "payload": {…}}``. Боту из него нужны:

* ``turn_context`` — точная модель и уровень рассуждений хода
  (``collaboration_mode.settings.reasoning_effort``, пусто — по умолчанию модели);
* ``event_msg`` с ``payload.type == "token_count"`` — расход токенов
  (``info.last_token_usage`` — последний запрос к модели, то есть занятость
  контекста; ``info.total_token_usage`` — сумма за сессию;
  ``info.model_context_window`` — размер окна) и ``rate_limits`` — снимок
  лимитов подписки (проценты и время сброса окон);
* ``event_msg`` с ``item_completed`` элемента ``UserMessage`` — сообщения пользователя.

Всё это Codex пишет сам; бот журнал только читает (и удаляет в /forget).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# Журнал с большим выводом команд может весить десятки мегабайт: читаем только хвост.
MAX_READ_BYTES = 16 * 1024 * 1024


@dataclass
class SessionInfo:
    path: Path
    model: Optional[str] = None
    effort: Optional[str] = None
    context_window: Optional[int] = None
    context_tokens: Optional[int] = None      # занято в контексте (последний запрос к модели)
    total_input_tokens: Optional[int] = None
    total_cached_tokens: Optional[int] = None
    total_output_tokens: Optional[int] = None
    total_reasoning_tokens: Optional[int] = None
    rate_limits: Optional[dict] = None
    rate_limits_at: Optional[float] = None  # когда Codex записал этот снимок (секунды эпохи)
    user_messages: int = 0
    compactions: int = 0


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def _safe_id(session_id: str) -> bool:
    return bool(session_id) and all(ch.isalnum() or ch in "-_" for ch in session_id)


def find_session_files(session_id: str, home: Optional[Path] = None) -> list[Path]:
    """Все файлы журнала сессии (обычные и архивные), новые первыми."""
    if not _safe_id(session_id):
        return []
    home = home or codex_home()
    found: list[Path] = []
    pattern = f"rollout-*{session_id}*.jsonl*"
    for base, glob in (
        (home / "sessions", f"*/*/*/{pattern}"),
        (home / "sessions", pattern),
        (home / "archived_sessions", pattern),
        (home / "archived_sessions", f"*/*/*/{pattern}"),
    ):
        if base.is_dir():
            found.extend(path for path in base.glob(glob) if path.is_file())
    unique = {path.resolve(): path for path in found}

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:  # файл удалили между поиском и сортировкой
            return 0.0

    return sorted(unique.values(), key=mtime, reverse=True)


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _read_lines(path: Path) -> Iterable[str]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > MAX_READ_BYTES:
            handle.seek(size - MAX_READ_BYTES)
            handle.readline()  # первая строка может быть обрезана
        for raw in handle:
            yield raw.decode("utf-8", "replace")


def parse_session_file(path: Path) -> SessionInfo:
    info = SessionInfo(path=path)
    for line in _read_lines(path):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if kind == "turn_context":
            info.model = str(payload.get("model") or "") or info.model
            info.effort = _turn_effort(payload)
            continue
        if kind == "compacted":
            info.compactions += 1
            continue
        if kind != "event_msg":
            continue
        event_type = payload.get("type")
        if event_type == "user_message":
            info.user_messages += 1
        elif event_type == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") in {"UserMessage", "user_message"}:
                info.user_messages += 1
        elif event_type in {"context_compacted", "compaction"}:
            info.compactions += 1
        elif event_type == "token_count":
            _apply_token_count(info, payload, record.get("timestamp"))
    return info


def _turn_effort(payload: dict) -> Optional[str]:
    """Уровень рассуждений хода: в Codex 0.159 он лежит в collaboration_mode.settings."""
    for value in (
        payload.get("effort"),
        payload.get("reasoning_effort"),
        ((payload.get("collaboration_mode") or {}).get("settings") or {}).get("reasoning_effort")
        if isinstance(payload.get("collaboration_mode"), dict) else None,
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _timestamp(value: Any) -> Optional[float]:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _apply_token_count(info: SessionInfo, payload: dict, timestamp: Any = None) -> None:
    usage_info = payload.get("info")
    if isinstance(usage_info, dict):
        window = _int(usage_info.get("model_context_window"))
        if window:
            info.context_window = window
        last = usage_info.get("last_token_usage")
        if isinstance(last, dict):
            total = _int(last.get("total_tokens"))
            if total is None:
                total = (_int(last.get("input_tokens")) or 0) + (_int(last.get("output_tokens")) or 0)
            info.context_tokens = total
        total_usage = usage_info.get("total_token_usage")
        if isinstance(total_usage, dict):
            info.total_input_tokens = _int(total_usage.get("input_tokens"))
            info.total_cached_tokens = _int(total_usage.get("cached_input_tokens"))
            info.total_output_tokens = _int(total_usage.get("output_tokens"))
            info.total_reasoning_tokens = _int(total_usage.get("reasoning_output_tokens"))
    limits = payload.get("rate_limits")
    if isinstance(limits, dict) and any(isinstance(limits.get(k), dict) for k in ("primary", "secondary")):
        # Лимитов может быть несколько семейств; основное — «codex». Снимок другого
        # семейства берём, только если основного ещё не было.
        is_main = limits.get("limit_id") in (None, "", "codex")
        current_main = info.rate_limits is not None and info.rate_limits.get("limit_id") in (None, "", "codex")
        if is_main or not current_main:
            info.rate_limits = limits
            info.rate_limits_at = _timestamp(timestamp)


def read_session_info(session_id: str, home: Optional[Path] = None) -> Optional[SessionInfo]:
    """Сведения о сессии из её журнала; None, если журнал не найден или не читается."""
    try:
        files = find_session_files(session_id, home)
    except OSError as exc:
        log.warning("Не удалось найти журнал сессии %s: %s", session_id, exc)
        return None
    if not files:
        return None
    try:
        return parse_session_file(files[0])
    except OSError as exc:
        log.warning("Не удалось прочитать журнал сессии %s: %s", files[0], exc)
        return None


def delete_session_files(session_id: str, home: Optional[Path] = None) -> int:
    """Удаляет файлы журнала сессии (для /forget). Возвращает число удалённых файлов."""
    removed = 0
    for path in find_session_files(session_id, home):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("Не удалось удалить %s: %s", path, exc)
    return removed
