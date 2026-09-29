"""Запуск ``codex exec`` как подпроцесса и разбор потока событий ``--json``.

Бот не ходит в API OpenAI сам: официальный Codex CLI в неинтерактивном режиме
(``codex exec``) работает по подписке ChatGPT (вход ``codex login``), а все
инструменты агента (команды, правки файлов, git, субагенты) живут внутри него.
Бот лишь запускает ``codex exec --json``, показывает события в чате и
продолжает сессию через ``codex exec resume <id>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import app_server, sessions
from .config import Config

log = logging.getLogger(__name__)

# Строки JSONL содержат вывод команд целиком, поэтому лимит буфера
# StreamReader поднят до 64 МБ.
STREAM_LIMIT = 64 * 1024 * 1024

# После завершения хода (turn.completed / turn.failed) ждём закрытия stdout не
# дольше этого: фоновый процесс, запущенный агентом, может держать поток
# открытым, хотя ответ уже получен.
RESULT_GRACE_SECONDS = float(os.environ.get("RESULT_GRACE_SECONDS", "15"))

TOOL_ICONS = {
    "Shell": "💻",
    "Edit": "✏️",
    "Add": "✏️",
    "Delete": "🗑",
    "Move": "✏️",
    "WebSearch": "🌐",
    "MCP": "🔌",
    "Agent": "🤖",
    "Image": "🖼",
}

ProgressCallback = Callable[["RunProgress"], Any]


@dataclass
class ToolCall:
    name: str
    summary: str
    item_id: Optional[str] = None
    nested: bool = False
    failed: bool = False

    def label(self) -> str:
        icon = "❌" if self.failed else TOOL_ICONS.get(self.name, "🔧")
        prefix = "↳ " if self.nested else ""
        return f"{prefix}{icon} {self.name}" + (f": {self.summary}" if self.summary else "")


@dataclass
class RunProgress:
    session_id: Optional[str] = None
    model: Optional[str] = None
    turns: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    total_tool_calls: int = 0
    last_text: str = ""            # последняя реплика агента (agent_message)
    reasoning: str = ""            # заголовок последнего рассуждения (если модель их присылает)
    notices: list[str] = field(default_factory=list)
    plan: list[tuple[str, bool]] = field(default_factory=list)  # список дел агента (todo_list)
    errors: list[str] = field(default_factory=list)  # тексты событий error
    started_at: float = field(default_factory=time.monotonic)
    max_tool_calls: int = 6
    max_notices: int = 3
    # Субагенты: id агента -> {"desc", "done", "failed"}. Для живой картины
    # многоагентной работы: сколько работает и кто уже закончил.
    agents: dict = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def add_tool_call(self, call: ToolCall) -> None:
        self.tool_calls.append(call)
        self.total_tool_calls += 1
        del self.tool_calls[: -self.max_tool_calls]

    def find_call(self, item_id: Optional[str]) -> Optional[ToolCall]:
        if not item_id:
            return None
        for call in reversed(self.tool_calls):
            if call.item_id == item_id:
                return call
        return None

    def agent_started(self, agent_id: Optional[str], desc: str) -> None:
        if agent_id:
            self.agents.setdefault(agent_id, {"desc": desc, "done": False, "failed": False})

    def agent_finished(self, agent_id: Optional[str], failed: bool = False) -> None:
        agent = self.agents.get(agent_id) if agent_id else None
        if agent:
            agent["done"] = True
            agent["failed"] = agent["failed"] or failed

    @property
    def agents_running(self) -> int:
        return sum(1 for a in self.agents.values() if not a["done"])

    @property
    def agents_done(self) -> int:
        return sum(1 for a in self.agents.values() if a["done"])

    def add_notice(self, text: str) -> None:
        if self.notices and self.notices[-1] == text:
            return
        self.notices.append(text)
        del self.notices[: -self.max_notices]


@dataclass
class RunResult:
    text: str
    session_id: Optional[str]
    is_error: bool
    subtype: str
    exit_code: Optional[int]
    duration_ms: Optional[int] = None
    input_tokens: Optional[int] = None   # вход целиком (вместе с кэшированной частью)
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    stderr: str = ""
    cancelled: bool = False
    timed_out: bool = False
    session_reset: bool = False  # пришлось начать новую сессию: прежняя не нашлась
    used_compact_summary: bool = False  # в промпт подставлен конспект после /compact
    new_thread: bool = False  # просили продолжить сессию, а Codex начал другую
    model: Optional[str] = None  # точная модель запуска (из журнала сессии)
    rate_limits: Optional[dict] = None  # последний снимок лимитов подписки (из журнала сессии)
    rate_limits_at: Optional[float] = None  # когда этот снимок записан
    progress: RunProgress = field(default_factory=RunProgress)


def _minus(total: Optional[int], before: Optional[int]) -> Optional[int]:
    """Расход за запуск: накопленный итог минус итог до запуска (если он известен)."""
    if total is None or before is None or before > total:
        return total
    return total - before


def _short(text: str, limit: int) -> str:
    text = text.strip().splitlines()[0] if text.strip() else ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _relative(path: str, cwd: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(cwd.resolve()))
    except (ValueError, OSError):
        return path


_SHELL_WRAPPER_RE = re.compile(r"^(?:/usr)?(?:/bin/)?(?:ba|z|da)?sh\s+-l?c\s+")


def summarize_command(command: Any) -> str:
    """Команда для строки прогресса без обёртки ``bash -lc '…'``."""
    if isinstance(command, list):
        parts = [str(part) for part in command]
        if len(parts) >= 3 and Path(parts[0]).name in {"bash", "sh", "zsh", "dash"} and parts[1] in {"-lc", "-c"}:
            text = parts[2]
        else:
            text = shlex.join(parts)
    else:
        text = str(command or "")
        match = _SHELL_WRAPPER_RE.match(text)
        if match:
            rest = text[match.end():].strip()
            try:
                split = shlex.split(rest)
            except ValueError:
                split = []
            text = split[0] if len(split) == 1 else rest
    return _short(text, 90)


def toml_string(value: str) -> str:
    """Строка в синтаксисе TOML (basic string) для ``-c ключ=значение``."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def reasoning_headline(text: str) -> str:
    """Заголовок рассуждения: Codex присылает конспекты вида «**Планирую правку**\\n\\n…»."""
    text = text.strip()
    match = re.match(r"^\*\*(.+?)\*\*", text)
    if match:
        return match.group(1).strip()
    return _short(text, 150)


class CodexRunner:
    def __init__(self, config: Config):
        self.config = config

    # ------------------------------------------------------------------ команда
    def build_command(
        self,
        *,
        resume: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        images: Optional[list[str]] = None,
    ) -> list[str]:
        """Команда запуска; сам промпт передаётся через stdin (аргумент «-»)."""
        cfg = self.config
        cmd = [cfg.codex_bin, "exec"]
        if resume:
            cmd.append("resume")
        cmd += ["--json", "--skip-git-repo-check"]
        # CODEX_EXTRA_ARGS идут первыми: явные настройки ниже (модель, усилия, песочница) их перекрывают.
        cmd += list(cfg.extra_args)
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={toml_string(effort)}"]
        if cfg.sandbox == "danger-full-access":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            cmd += ["-c", f"sandbox_mode={toml_string(cfg.sandbox)}", "-c", 'approval_policy="never"']
            if cfg.sandbox == "workspace-write":
                network = "true" if cfg.network_access else "false"
                cmd += ["-c", f"sandbox_workspace_write.network_access={network}"]
        if cfg.web_search:
            cmd += ["-c", f"web_search={toml_string(cfg.web_search)}"]
        # Одно и то же значение в каждом запуске: при resume Codex новое значение
        # не применяет до ближайшего сжатия истории, а после сжатия берёт текущее.
        if cfg.extra_instructions:
            cmd += ["-c", f"developer_instructions={toml_string(cfg.extra_instructions)}"]
        for image in images or []:
            cmd += ["--image", str(image)]
        # «--» отделяет позиционные аргументы: id сессии и «-» (промпт из stdin).
        cmd.append("--")
        if resume:
            cmd.append(resume)
        cmd.append("-")
        return cmd

    def build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Секреты самого бота дочернему процессу не нужны.
        env.pop("TELEGRAM_BOT_TOKEN", None)
        local_bin = str(Path.home() / ".local" / "bin")
        path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
        if local_bin not in path_parts:
            env["PATH"] = os.pathsep.join([local_bin, *path_parts]) if path_parts else local_bin
        env.setdefault("TERM", "dumb")
        env.setdefault("NO_COLOR", "1")
        return env

    # ------------------------------------------------------------------- запуск
    async def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        resume: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        images: Optional[list[str]] = None,
        on_progress: Optional[ProgressCallback] = None,
        cancel: Optional[asyncio.Event] = None,
        timeout: Optional[float] = None,
    ) -> RunResult:
        cmd = self.build_command(resume=resume, model=model, effort=effort, images=images)
        timeout = timeout or self.config.timeout_seconds
        progress = RunProgress()
        progress.session_id = resume
        progress.model = model  # точное имя модели бот узнает из журнала сессии после запуска
        # В turn.completed Codex сообщает расход токенов НАКОПЛЕННЫМ итогом по всей
        # сессии, поэтому запоминаем итог до запуска и потом вычитаем его.
        baseline = await asyncio.to_thread(sessions.read_session_info, resume) if resume else None
        log.info("codex: cwd=%s resume=%s model=%s effort=%s prompt=%r", cwd, resume, model, effort, _short(prompt, 80))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_env(),
                limit=STREAM_LIMIT,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return RunResult(
                text=f"Не удалось запустить codex ({self.config.codex_bin}): {exc}",
                session_id=resume,
                is_error=True,
                subtype="spawn_error",
                exit_code=None,
                progress=progress,
            )

        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        final_event: Optional[dict] = None
        raw_lines: list[str] = []
        got_result = asyncio.Event()

        async def feed_stdin() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        async def read_stdout() -> None:
            nonlocal final_event
            assert proc.stdout is not None
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:  # строка длиннее STREAM_LIMIT
                    log.warning("Пропущена слишком длинная строка вывода codex")
                    continue
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except ValueError:
                    raw_lines.append(text[:2000])
                    del raw_lines[:-20]
                    continue
                if not isinstance(event, dict):
                    continue
                if self._handle_event(event, progress, cwd):
                    final_event = event
                    got_result.set()
                if on_progress is not None:
                    try:
                        outcome = on_progress(progress)
                        if asyncio.iscoroutine(outcome):
                            await outcome
                    except Exception:  # noqa: BLE001 — прогресс не должен ронять запуск
                        log.exception("Ошибка в обработчике прогресса")

        stdin_task = asyncio.create_task(feed_stdin())
        stderr_task = asyncio.create_task(proc.stderr.read())
        reader_task = asyncio.create_task(read_stdout())
        cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None
        cancelled = False
        timed_out = False
        result_task = asyncio.create_task(got_result.wait())
        waiting = {reader_task, result_task} | ({cancel_task} if cancel_task else set())
        try:
            done, _ = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if reader_task not in done and result_task in done:
                # Ход завершён: даём процессу немного времени закрыть поток и выйти.
                try:
                    await asyncio.wait_for(asyncio.shield(reader_task), timeout=RESULT_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    log.warning("codex: ход завершён, но поток не закрылся за %s с — завершаю процесс (pid=%s)", RESULT_GRACE_SECONDS, proc.pid)
                    await self._terminate(proc)
                    try:
                        await asyncio.wait_for(reader_task, timeout=5)
                    except asyncio.TimeoutError:
                        reader_task.cancel()
            elif reader_task not in done:
                if cancel_task is not None and cancel_task in done:
                    cancelled = True
                    log.info("codex: остановка по запросу пользователя (pid=%s)", proc.pid)
                else:
                    timed_out = True
                    log.warning("codex: превышен таймаут %s с (pid=%s)", timeout, proc.pid)
                await self._terminate(proc)
                try:
                    await asyncio.wait_for(reader_task, timeout=10)
                except asyncio.TimeoutError:
                    reader_task.cancel()
        except asyncio.CancelledError:
            # Задачу запуска отменили снаружи (например, /forget отменил воркер):
            # процесс codex и его команды не должны жить дальше без присмотра.
            log.warning("codex: запуск отменён, завершаю процесс (pid=%s)", proc.pid)
            await asyncio.shield(self._terminate(proc))
            reader_task.cancel()
            stderr_task.cancel()
            raise
        finally:
            result_task.cancel()
            if cancel_task is not None:
                cancel_task.cancel()
            if not stdin_task.done():
                stdin_task.cancel()

        try:
            exit_code: Optional[int] = await asyncio.wait_for(proc.wait(), timeout=60)
        except asyncio.TimeoutError:
            log.warning("codex: процесс не завершился после закрытия stdout, убиваю")
            await self._terminate(proc, force=True)
            exit_code = await proc.wait()

        try:
            stderr_text = (await asyncio.wait_for(stderr_task, timeout=5)).decode("utf-8", "replace")
        except asyncio.TimeoutError:
            stderr_text = ""
        stderr_text = clean_stderr(stderr_text)[-4000:]

        switched = bool(resume and progress.session_id and progress.session_id != resume)
        if switched:
            log.warning("codex: просили продолжить сессию %s, а начата %s", resume, progress.session_id)
            baseline = None  # итог прежней сессии к новой не относится
        if final_event is not None:
            result = self._result_from_event(final_event, progress, exit_code, stderr_text)
            if baseline is not None:
                result.input_tokens = _minus(result.input_tokens, baseline.total_input_tokens)
                result.output_tokens = _minus(result.output_tokens, baseline.total_output_tokens)
                result.cache_read_tokens = _minus(result.cache_read_tokens, baseline.total_cached_tokens)
        else:
            fallback = progress.last_text or "\n".join(progress.errors[-3:]) or "\n".join(raw_lines[-5:])
            if not cancelled:
                fallback = fallback or stderr_text or f"Процесс codex завершился без результата (код {exit_code})."
            result = RunResult(
                text=fallback,
                session_id=progress.session_id,
                is_error=not cancelled,
                subtype="no_result",
                exit_code=exit_code,
                stderr=stderr_text,
                progress=progress,
            )
        result.cancelled = cancelled
        result.timed_out = timed_out
        result.new_thread = switched
        if cancelled:
            result.is_error = False
        result.duration_ms = int(progress.elapsed * 1000)
        if result.session_id:
            info = await asyncio.to_thread(sessions.read_session_info, result.session_id)
            if info is not None:
                result.model = info.model
                result.rate_limits = info.rate_limits
                result.rate_limits_at = info.rate_limits_at
                progress.model = info.model or progress.model
                if final_event is None and info.total_input_tokens is not None:
                    # Прерванный/упавший запуск тоже расходовал токены: считаем по журналу
                    # сессии — итог после запуска минус итог до него.
                    before = baseline if (baseline is not None and not switched) else None
                    result.input_tokens = _minus(info.total_input_tokens, before.total_input_tokens if before else 0)
                    result.output_tokens = _minus(info.total_output_tokens, before.total_output_tokens if before else 0)
                    result.cache_read_tokens = _minus(info.total_cached_tokens, before.total_cached_tokens if before else 0)
        log.info(
            "codex: завершено subtype=%s error=%s tools=%s tokens=%s/%s exit=%s",
            result.subtype, result.is_error, progress.total_tool_calls, result.input_tokens, result.output_tokens, exit_code,
        )
        return result

    async def compact(
        self,
        session_id: str,
        cwd: Path,
        *,
        model: Optional[str] = None,
        cancel: Optional[asyncio.Event] = None,
    ) -> "app_server.CompactOutcome":
        """Сжимает историю сессии средствами Codex (app-server); AppServerError — не вышло."""
        log.info("codex: сжатие истории сессии %s (cwd=%s)", session_id, cwd)
        return await app_server.compact_thread(
            self.config.codex_bin,
            session_id,
            cwd=cwd,
            env=self.build_env(),
            model=model,
            developer_instructions=self.config.extra_instructions,
            extra_flags=app_server.app_server_flags(self.config.extra_args),
            timeout=min(self.config.timeout_seconds, 1800),
            cancel=cancel,
        )

    async def delete_session(self, session_id: str) -> bool:
        """``codex delete --force <id>``: удаляет сессию целиком (журнал и индексы Codex). Best-effort."""
        if not session_id or not all(ch.isalnum() or ch in "-_" for ch in session_id):
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.config.codex_bin, "delete", "--force", session_id,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=self.build_env(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("codex delete %s не выполнен: %s", session_id, exc)
            return False
        if proc.returncode != 0:
            log.info("codex delete %s: %s", session_id, clean_stderr(out.decode("utf-8", "replace"))[-300:])
        return proc.returncode == 0

    async def _terminate(self, proc: asyncio.subprocess.Process, force: bool = False) -> None:
        """SIGINT прерывает ход корректно (как Ctrl+C), дальше SIGTERM и SIGKILL."""
        signals = [(signal.SIGKILL, 5)] if force else [(signal.SIGINT, 15), (signal.SIGTERM, 10), (signal.SIGKILL, 5)]
        for sig, grace in signals:
            if proc.returncode is not None:
                return
            self._signal(proc, sig)
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
                return
            except asyncio.TimeoutError:
                continue

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Сигнал всей группе процессов запуска (codex и запущенные им команды)."""
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            proc.send_signal(sig)

    # ------------------------------------------------------------------ события
    def _handle_event(self, event: dict, progress: RunProgress, cwd: Path) -> bool:
        """Обновляет прогресс; True — событие завершает ход (turn.completed / turn.failed)."""
        kind = str(event.get("type") or "")
        if kind == "thread.started":
            progress.session_id = str(event.get("thread_id") or "") or progress.session_id
            return False
        if kind == "turn.started":
            progress.turns += 1
            return False
        if kind in {"turn.completed", "turn.failed"}:
            return True
        if kind == "error":
            message = str(event.get("message") or "").strip()
            if message:
                progress.errors.append(message)
                del progress.errors[:-10]
                progress.add_notice(f"⚠️ {_short(message, 200)}")
            return False
        if kind.startswith("item."):
            item = event.get("item")
            if isinstance(item, dict):
                self._handle_item(kind[len("item."):], item, progress, cwd)
        return False

    def _handle_item(self, phase: str, item: dict, progress: RunProgress, cwd: Path) -> None:
        item_type = str(item.get("type") or item.get("item_type") or "")
        item_id = str(item.get("id") or "") or None
        status = str(item.get("status") or "")
        failed = status in {"failed", "declined", "error"}

        if item_type == "agent_message":
            text = str(item.get("text") or "").strip()
            if text and phase == "completed":
                progress.last_text = text
            return
        if item_type == "reasoning":
            text = str(item.get("text") or "").strip()
            if text:
                progress.reasoning = reasoning_headline(text)
            return
        if item_type == "command_execution":
            call = progress.find_call(item_id)
            if call is None:
                call = ToolCall("Shell", summarize_command(item.get("command")), item_id=item_id)
                progress.add_tool_call(call)
            if phase == "completed":
                exit_code = item.get("exit_code")
                if failed or (isinstance(exit_code, int) and exit_code != 0):
                    call.failed = True
            return
        if item_type == "file_change":
            if phase != "completed":
                return
            changes = item.get("changes")
            for change in changes if isinstance(changes, list) else []:
                if not isinstance(change, dict):
                    continue
                change_kind = str(change.get("kind") or "update")
                name = {"add": "Add", "delete": "Delete", "update": "Edit"}.get(change_kind, "Edit")
                if isinstance(change.get("kind"), dict):  # на случай формата {"type": "update", "move_path": …}
                    name = {"add": "Add", "delete": "Delete"}.get(str(change["kind"].get("type")), "Edit")
                path = _relative(str(change.get("path") or ""), cwd)
                progress.add_tool_call(ToolCall(name, path, item_id=item_id, failed=failed))
            return
        if item_type == "mcp_tool_call":
            call = progress.find_call(item_id)
            if call is None:
                server = str(item.get("server") or "")
                tool = str(item.get("tool") or "")
                call = ToolCall("MCP", f"{server}.{tool}".strip("."), item_id=item_id)
                progress.add_tool_call(call)
            if phase == "completed" and (failed or item.get("error")):
                call.failed = True
            return
        if item_type == "web_search":
            call = progress.find_call(item_id)
            query = str(item.get("query") or "")
            if call is None:
                progress.add_tool_call(ToolCall("WebSearch", _short(query, 80), item_id=item_id))
            elif query and not call.summary:
                call.summary = _short(query, 80)
            return
        if item_type == "todo_list":
            items = item.get("items")
            plan = []
            for entry in items if isinstance(items, list) else []:
                if isinstance(entry, dict) and str(entry.get("text") or "").strip():
                    plan.append((str(entry["text"]).strip(), bool(entry.get("completed"))))
            progress.plan = plan
            return
        if item_type == "collab_tool_call":
            self._handle_collab(phase, item, progress, failed)
            return
        if item_type == "error":
            message = str(item.get("message") or "").strip()
            if message:
                progress.add_notice(f"⚠️ {_short(message, 200)}")
            return
        if item_type and phase == "started":
            # Неизвестный инструмент новой версии Codex: покажем хотя бы его тип.
            progress.add_tool_call(ToolCall(item_type, "", item_id=item_id))

    @staticmethod
    def _handle_collab(phase: str, item: dict, progress: RunProgress, failed: bool) -> None:
        """Субагенты: spawn_agent запускает, wait/close — ждут; статусы приходят в agents_states."""
        tool = str(item.get("tool") or "")
        receivers = item.get("receiver_thread_ids")
        receivers = [str(r) for r in receivers] if isinstance(receivers, list) else []
        if tool == "spawn_agent":
            desc = _short(str(item.get("prompt") or ""), 80) or "агент"
            if phase == "started" and not receivers:
                progress.add_tool_call(ToolCall("Agent", desc, item_id=str(item.get("id") or "") or None))
            for receiver in receivers:
                if receiver not in progress.agents:
                    progress.agent_started(receiver, desc)
            if phase == "completed" and failed and not receivers:
                call = progress.find_call(str(item.get("id") or "") or None)
                if call is not None:
                    call.failed = True
        states = item.get("agents_states")
        if isinstance(states, dict):
            for agent_id, state in states.items():
                status = str(state.get("status") if isinstance(state, dict) else state or "").lower()
                if agent_id not in progress.agents:
                    continue
                if status in {"completed", "shutdown", "done", "finished", "closed"}:
                    progress.agent_finished(agent_id)
                elif status in {"errored", "error", "failed", "not_found", "notfound", "interrupted"}:
                    progress.agent_finished(agent_id, failed=True)

    @staticmethod
    def _result_from_event(
        event: dict, progress: RunProgress, exit_code: Optional[int], stderr_text: str
    ) -> RunResult:
        kind = event.get("type")
        if kind == "turn.failed":
            error = event.get("error")
            message = str(error.get("message") if isinstance(error, dict) else error or "").strip()
            text = message or "\n".join(progress.errors[-3:]) or "Codex сообщил об ошибке без описания."
            return RunResult(
                text=text,
                session_id=progress.session_id,
                is_error=True,
                subtype="turn_failed",
                exit_code=exit_code,
                stderr=stderr_text,
                progress=progress,
            )
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}

        def _tok(name: str) -> int:
            value = usage.get(name)
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

        has_usage = bool(usage)
        return RunResult(
            text=progress.last_text,
            session_id=progress.session_id,
            is_error=False,
            subtype="success",
            exit_code=exit_code,
            input_tokens=_tok("input_tokens") if has_usage else None,
            output_tokens=_tok("output_tokens") if has_usage else None,
            cache_read_tokens=_tok("cached_input_tokens") if has_usage else None,
            stderr=stderr_text,
            progress=progress,
        )


_NOISE_STDERR = (
    "could not create PATH aliases",
    "Reading prompt from stdin",
    "Reading additional input from stdin",
)


def clean_stderr(text: str) -> str:
    """Убирает из stderr служебный шум codex, который пользователю ничего не говорит."""
    lines = [line for line in text.splitlines() if line.strip() and not any(noise in line for noise in _NOISE_STDERR)]
    return "\n".join(lines).strip()


async def codex_version(codex_bin: str, env: Optional[dict[str, str]] = None) -> str:
    """Возвращает строку версии (``codex --version``) или текст ошибки."""
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "--version", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (OSError, asyncio.TimeoutError) as exc:
        return f"недоступен ({exc})"
    lines = [line.strip() for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
    return lines[-1] if lines else "неизвестно"


# Признаки конкретных ошибок ищем только в тексте самой ошибки Codex (turn.failed
# и строки «Error: …» в stderr), а не в последней реплике модели: иначе ответ про
# «authentication middleware» при таймауте превращался бы в «не авторизован».
AUTH_ERROR_MARKERS = (
    "not logged in",
    "401 unauthorized",
    "access token could not be refreshed",
    "refresh token",
    "please log out and sign in again",
    "sign in again",
    "log in again",
    "invalid_api_key",
    "incorrect api key",
    "missing bearer",
)

AUTH_HINT = (
    "🔑 Codex на сервере не авторизован или вход устарел. Войдите заново под пользователем сервиса: "
    "`sudo bash /root/FedorBot/deploy/codex-login.sh` (ссылка и одноразовый код для браузера на вашем компьютере). "
    "Проверить: `sudo -iu codexbot codex login status`. Перезапускать бота после входа не нужно."
)

# Тексты Codex 0.159: «You’ve hit your usage limit. … try again at 5:56 PM.» и
# «Codex ran out of room in the model's context window. Start a new thread …».
USAGE_LIMIT_MARKERS = ("hit your usage limit", "usage_limit_reached", "usage limit reached")
CONTEXT_OVERFLOW_MARKERS = ("ran out of room in the model's context window", "context_length_exceeded")

USAGE_LIMIT_HINT = (
    "⛔ Исчерпан лимит подписки ChatGPT на Codex. Когда он обновится, покажет /quota; "
    "до тех пор запросы будут отклоняться."
)
CONTEXT_OVERFLOW_HINT = (
    "📐 Контекст диалога переполнен. Сожмите историю командой /compact или начните новый диалог: /new."
)


def codex_error_text(result: RunResult) -> str:
    """Текст ошибки самого Codex: сообщение turn.failed и фатальные строки «Error: …» из stderr."""
    if not result.is_error or result.cancelled or result.timed_out:
        return ""
    parts = []
    if result.subtype == "turn_failed":
        parts.append(result.text)
    parts.extend(line for line in result.stderr.splitlines() if line.startswith("Error"))
    return "\n".join(parts).lower().replace("’", "'")


def looks_like_auth_error(result: RunResult) -> bool:
    text = codex_error_text(result)
    return any(marker in text for marker in AUTH_ERROR_MARKERS)


def looks_like_usage_limit(result: RunResult) -> bool:
    text = codex_error_text(result)
    return any(marker in text for marker in USAGE_LIMIT_MARKERS)


def looks_like_context_overflow(result: RunResult) -> bool:
    text = codex_error_text(result)
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


async def codex_login_status(codex_bin: str, env: Optional[dict[str, str]] = None) -> dict:
    """Результат ``codex login status``: {"loggedIn", "method", "text"} или {"error": ...}.

    Команда сообщает, какие учётные данные сохранены (вход через ChatGPT или
    API-ключ); действительность токена она не проверяет.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "login", "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (OSError, asyncio.TimeoutError) as exc:
        return {"error": str(exc)}
    lines = [line.strip() for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
    status_line = next((line for line in lines if line.lower().startswith(("logged in", "not logged in"))), "")
    if not status_line:
        tail = clean_stderr("\n".join(lines))
        return {"error": tail[-300:] or f"нет вывода (код {proc.returncode})"}
    lowered = status_line.lower()
    if lowered.startswith("not logged in"):
        return {"loggedIn": False, "method": None, "text": status_line}
    if "chatgpt" in lowered:
        method = "chatgpt"
    elif "api key" in lowered:
        method = "api_key"
    elif "access token" in lowered:
        method = "access_token"
    else:
        method = "other"
    return {"loggedIn": True, "method": method, "text": status_line}


def describe_auth(status: dict) -> str:
    """Короткое описание состояния авторизации для логов и /status."""
    if status.get("api_key_env"):
        # `codex login status` переменную не видит, а `codex exec` берёт её первой.
        return "⚠️ в окружении задан CODEX_API_KEY: codex exec работает по ключу API (оплата по тарифам API, не подписка)"
    if "error" in status:
        return f"неизвестно ({status['error']})"
    if not status.get("loggedIn"):
        return "❌ не авторизован (Not logged in): выполните вход — deploy/codex-login.sh"
    method = status.get("method")
    if method == "chatgpt":
        return "✅ вход через аккаунт ChatGPT (подписка)"
    if method == "api_key":
        detail = status.get("text", "").split(" - ", 1)
        key = f" ({detail[1]})" if len(detail) == 2 else ""
        return f"✅ API-ключ OpenAI{key} — оплата по тарифам API, не подписка"
    return f"✅ {status.get('text') or 'выполнен вход'}"
