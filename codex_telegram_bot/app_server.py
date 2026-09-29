"""Сжатие истории сессии средствами самого Codex через ``codex app-server``.

``codex exec`` не выполняет слеш-команды (``/compact`` уходит модели обычным
текстом), а у app-server (JSON-RPC построчно через stdin/stdout) есть метод
``thread/compact/start`` — тот же механизм, что ``/compact`` в интерактивном
Codex: история заменяется сжатой, сессия (thread id) остаётся прежней.

Протокол app-server помечен как экспериментальный, поэтому бот при любой
ошибке здесь откатывается на сжатие через конспект (см. ``bot.py``).

Последовательность (проверена на Codex 0.159):
``initialize`` → ``initialized`` → ``thread/resume {threadId, excludeTurns}``
→ ``thread/compact/start {threadId}`` → уведомление ``turn/completed``.
Инструкции бота передаются в ``thread/resume`` (``developerInstructions``):
после сжатия Codex заново вставляет в историю то значение, что задано в этом
процессе, и без него инструкции бы потерялись.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import __version__

log = logging.getLogger(__name__)

STREAM_LIMIT = 64 * 1024 * 1024
# Флаги из CODEX_EXTRA_ARGS, которые понимает и app-server (остальные — только exec).
_PASS_FLAGS = {"-c", "--config", "--enable", "--disable"}


class AppServerError(RuntimeError):
    """app-server ответил ошибкой или повёл себя не по протоколу."""


@dataclass
class CompactOutcome:
    compacted: bool = False          # Codex подтвердил сжатие (элемент contextCompaction)
    status: str = ""                 # completed | failed | interrupted
    error: str = ""
    model: Optional[str] = None
    before_tokens: Optional[int] = None   # занято в контексте до сжатия
    after_tokens: Optional[int] = None    # и после
    context_window: Optional[int] = None
    cancelled: bool = False
    timed_out: bool = False


def app_server_flags(extra_args: tuple[str, ...] | list[str]) -> list[str]:
    """Из CODEX_EXTRA_ARGS оставляет пары ``-c ключ=значение``, ``--enable x``, ``--disable x``."""
    result: list[str] = []
    args = list(extra_args)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _PASS_FLAGS and i + 1 < len(args):
            result += [arg, args[i + 1]]
            i += 2
            continue
        if any(arg.startswith(flag + "=") for flag in _PASS_FLAGS if flag.startswith("--")):
            result.append(arg)
        i += 1
    return result


class _Session:
    """Один процесс app-server и последовательные запросы к нему."""

    def __init__(self, proc: asyncio.subprocess.Process, outcome: CompactOutcome):
        self.proc = proc
        self.outcome = outcome
        self._next_id = 0
        self._compaction_started = False  # расход токенов до этого момента — «до сжатия»
        self.turn_completed: Optional[dict] = None  # уведомление turn/completed, если уже пришло

    async def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method: str, params: Optional[dict] = None) -> Any:
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self.send(message)
        response = await self.read_until(lambda m: m.get("id") == request_id and "method" not in m)
        if "error" in response:
            error = response.get("error") or {}
            text = error.get("message") if isinstance(error, dict) else str(error)
            raise AppServerError(f"{method}: {text or 'ошибка без описания'}")
        return response.get("result")

    async def read_until(self, predicate) -> dict:
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                raise AppServerError("app-server завершился, не ответив")
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("{"):
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message:
                self._on_notification(str(message["method"]), message.get("params") or {})
            if predicate(message):
                return message

    def _on_notification(self, method: str, params: dict) -> None:
        outcome = self.outcome
        if method == "turn/completed":
            self.turn_completed = params
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            last = usage.get("last") or {}
            tokens = last.get("totalTokens")
            window = usage.get("modelContextWindow")
            if isinstance(window, int):
                outcome.context_window = window
            if isinstance(tokens, int):
                if self._compaction_started:
                    outcome.after_tokens = tokens
                else:
                    outcome.before_tokens = tokens
        elif method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            if str(item.get("type") or "").lower() in {"contextcompaction", "context_compaction"}:
                self._compaction_started = True
                if method == "item/completed":
                    outcome.compacted = True
        elif method == "error":
            error = params.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            if message:
                outcome.error = str(message)


async def compact_thread(
    codex_bin: str,
    thread_id: str,
    *,
    cwd: Path,
    env: dict[str, str],
    model: Optional[str] = None,
    developer_instructions: Optional[str] = None,
    extra_flags: Optional[list[str]] = None,
    timeout: float = 900,
    cancel: Optional[asyncio.Event] = None,
) -> CompactOutcome:
    """Сжимает историю сессии ``thread_id``. Исключение AppServerError — сжатие не выполнено."""
    outcome = CompactOutcome()
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "app-server", *(extra_flags or []),
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=STREAM_LIMIT,
            start_new_session=True,
        )
    except OSError as exc:
        raise AppServerError(f"не удалось запустить codex app-server: {exc}") from exc
    session = _Session(proc, outcome)
    stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr is not None else None

    async def dialogue() -> None:
        await session.request(
            "initialize",
            {"clientInfo": {"name": "codex_telegram_bot", "title": "Codex Telegram bot", "version": __version__}},
        )
        await session.send({"method": "initialized"})
        params: dict[str, Any] = {"threadId": thread_id, "excludeTurns": True, "cwd": str(cwd)}
        if model:
            params["model"] = model
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        resumed = await session.request("thread/resume", params)
        if isinstance(resumed, dict) and isinstance(resumed.get("model"), str):
            outcome.model = resumed["model"]
        await session.request("thread/compact/start", {"threadId": thread_id})
        # Ответ на запрос и события хода app-server шлёт из разных задач: turn/completed
        # мог прийти раньше ответа — тогда он уже запомнен.
        params_done = session.turn_completed
        if params_done is None:
            done = await session.read_until(lambda m: m.get("method") == "turn/completed")
            params_done = done.get("params") or {}
        turn = params_done.get("turn") or {}
        outcome.status = str(turn.get("status") or "")
        error = turn.get("error")
        if isinstance(error, dict) and error.get("message"):
            outcome.error = str(error["message"])

    work = asyncio.create_task(dialogue())
    waiters: set[asyncio.Future] = {work}
    cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None
    if cancel_task is not None:
        waiters.add(cancel_task)
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if work not in done:
            work.cancel()
            if cancel_task is not None and cancel_task in done:
                outcome.cancelled = True
            else:
                outcome.timed_out = True
        else:
            work.result()  # пробрасывает AppServerError
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
        await _shutdown(proc)
        stderr_text = ""
        if stderr_task is not None:
            try:
                stderr_text = (await asyncio.wait_for(stderr_task, timeout=5)).decode("utf-8", "replace")
            except (asyncio.TimeoutError, asyncio.CancelledError):
                stderr_task.cancel()
        if stderr_text.strip():
            log.debug("codex app-server stderr: %s", stderr_text.strip()[-2000:])
    return outcome


async def _shutdown(proc: asyncio.subprocess.Process) -> None:
    """Закрытие stdin завершает app-server; если он не вышел — SIGTERM, затем SIGKILL группе."""
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
    for sig, grace in ((None, 3), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        if proc.returncode is not None:
            return
        if sig is not None:
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                proc.send_signal(sig)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except asyncio.TimeoutError:
            continue
