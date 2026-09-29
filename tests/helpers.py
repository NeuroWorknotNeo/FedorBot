"""Общие заготовки для тестов: фиктивный codex и конфигурация."""

import subprocess
from pathlib import Path

from codex_telegram_bot.config import Config

# Фиктивный codex печатает те же JSONL-события, что `codex exec --json` 0.159
# (формат сверен с настоящим CLI), и, как настоящий, ведёт журнал сессии
# $CODEX_HOME/sessions/ГГГГ/ММ/ДД/rollout-…-<id>.jsonl с моделью, расходом
# токенов и лимитами подписки.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
day_dir = home / "sessions" / "2026" / "09" / "29"


def rollout_path(sid):
    found = sorted(home.glob("sessions/*/*/*/rollout-*-%s.jsonl" % sid))
    return found[0] if found else day_dir / ("rollout-2026-09-29T10-00-00-%s.jsonl" % sid)


def record(path, kind, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        handle.write(json.dumps({"timestamp": stamp, "type": kind, "payload": payload}, ensure_ascii=False) + "\n")


def last_token_info(path):
    info = None
    if path.exists():
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            payload = rec.get("payload") or {}
            if rec.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload["info"]
    return info


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


if args[:1] == ["--version"]:
    print("codex-cli 9.9.9")
    sys.exit(0)
if args[:2] == ["login", "status"]:
    if os.environ.get("FAKE_LOGGED_OUT") == "1":
        print("Not logged in", file=sys.stderr)
        sys.exit(1)
    print("Logged in using ChatGPT", file=sys.stderr)
    sys.exit(0)

if args[:2] == ["delete", "--force"]:
    sid = args[2]
    with open(os.environ["FAKE_ARGS_LOG"] + ".delete", "a") as log:
        log.write(sid + "\n")
    found = list(home.glob("sessions/*/*/*/rollout-*-%s.jsonl" % sid))
    if not found:
        print("Error: failed to delete session", file=sys.stderr)
        sys.exit(1)
    for path in found:
        path.unlink()
    print("Deleted session %s." % sid)
    sys.exit(0)

if args[:1] == ["app-server"]:
    # Минимальный app-server: initialize → thread/resume → thread/compact/start.
    mode = os.environ.get("FAKE_APP_SERVER", "ok")
    with open(os.environ["FAKE_ARGS_LOG"] + ".appserver", "a") as log:
        log.write(json.dumps(args) + "\n")
    for line in sys.stdin:
        msg = json.loads(line)
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        with open(os.environ["FAKE_ARGS_LOG"] + ".appserver", "a") as log:
            log.write(json.dumps(msg, ensure_ascii=False) + "\n")
        if method == "initialize":
            emit({"id": mid, "result": {"userAgent": "fake/9.9.9", "codexHome": str(home)}})
        elif method == "thread/resume":
            tid = params["threadId"]
            path = rollout_path(tid)
            if mode == "crash":
                sys.exit(3)
            if not path.exists():
                emit({"id": mid, "error": {"code": -32600, "message": "no rollout found for thread id %s" % tid}})
                continue
            emit({"id": mid, "result": {"thread": {"id": tid, "path": str(path)}, "model": params.get("model") or "gpt-6-sol"}})
            info = last_token_info(path) or {"last_token_usage": {"total_tokens": 0}, "model_context_window": 258400}
            emit({"method": "thread/tokenUsage/updated", "params": {"threadId": tid, "tokenUsage": {
                "last": {"totalTokens": info["last_token_usage"]["total_tokens"]}, "modelContextWindow": info["model_context_window"]}}})
        elif method == "thread/compact/start":
            tid = params["threadId"]
            path = rollout_path(tid)
            if mode == "early":
                emit({"method": "item/completed", "params": {"item": {"type": "contextCompaction", "id": "c1"}, "threadId": tid}})
                emit({"method": "turn/completed", "params": {"threadId": tid, "turn": {"id": "t1", "status": "completed", "error": None}}})
                emit({"id": mid, "result": {}})
                continue
            emit({"id": mid, "result": {}})
            emit({"method": "turn/started", "params": {"threadId": tid, "turn": {"id": "t1", "status": "inProgress"}}})
            if mode == "hang":
                time.sleep(60)
            if mode == "fail":
                emit({"method": "turn/completed", "params": {"threadId": tid, "turn": {"id": "t1", "status": "failed",
                                                                                   "error": {"message": "remote compaction failed"}}}})
                continue
            emit({"method": "item/started", "params": {"item": {"type": "contextCompaction", "id": "c1"}, "threadId": tid}})
            emit({"method": "thread/tokenUsage/updated", "params": {"threadId": tid, "tokenUsage": {
                "last": {"totalTokens": 5000}, "modelContextWindow": 258400}}})
            emit({"method": "item/completed", "params": {"item": {"type": "contextCompaction", "id": "c1"}, "threadId": tid}})
            info = last_token_info(path)
            record(path, "compacted", {"message": ""})
            if info:
                info = dict(info, last_token_usage=dict(info["last_token_usage"], total_tokens=5000))
                record(path, "event_msg", {"type": "token_count", "info": info, "rate_limits": None})
            emit({"method": "turn/completed", "params": {"threadId": tid, "turn": {"id": "t1", "status": "completed", "error": None}}})
    sys.exit(0)

if args[:1] != ["exec"]:
    print("unexpected args: %r" % (args,), file=sys.stderr)
    sys.exit(2)

with open(os.environ["FAKE_ARGS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")

resume = args[1] == "resume"
positional = args[args.index("--") + 1:]
session = positional[0] if resume else None
assert positional[-1] == "-", positional
prompt = sys.stdin.read()
with open(os.environ["FAKE_ARGS_LOG"] + ".prompts", "a") as log:
    log.write(json.dumps(prompt, ensure_ascii=False) + "\n")

config = {}
for i, arg in enumerate(args):
    if arg == "-c":
        key, _, value = args[i + 1].partition("=")
        config[key] = value
model = args[args.index("--model") + 1] if "--model" in args else "gpt-6-sol"
effort = config.get("model_reasoning_effort", "").strip('"') or None

if resume and (session.startswith("gone") or not rollout_path(session).exists()):
    print("Error: thread/resume: thread/resume failed: no rollout found for thread id %s (code -32600)" % session, file=sys.stderr)
    sys.exit(1)

sid = session or "sess-123"
if resume and os.environ.get("FAKE_NEW_THREAD") == "1":
    sid, resume, session = "sess-new", False, None  # как `resume --last` без совпадений: молча новая сессия
path = rollout_path(sid)
if not resume and path.exists():
    path.unlink()  # новая сессия всегда начинается с чистого журнала
totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
previous = last_token_info(path)
if previous:
    totals = {k: v for k, v in previous["total_token_usage"].items() if k != "total_tokens"}

if not resume:
    record(path, "session_meta", {"id": sid, "cwd": os.getcwd(), "cli_version": "9.9.9", "source": "exec"})
record(path, "turn_context", {"model": model, "cwd": os.getcwd(), "collaboration_mode": {"mode": "default", "settings": {"model": model, "reasoning_effort": effort}}})
record(path, "event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "content": [{"type": "text", "text": prompt}]}})


def record_usage(run_input=32000, run_cached=30000, run_output=250):
    for key, value in (("input_tokens", run_input), ("cached_input_tokens", run_cached), ("output_tokens", run_output), ("reasoning_output_tokens", 40)):
        totals[key] = totals.get(key, 0) + value
    last = {"input_tokens": run_input, "cached_input_tokens": run_cached, "output_tokens": run_output,
            "reasoning_output_tokens": 40, "total_tokens": run_input + run_output}
    total = dict(totals, total_tokens=totals["input_tokens"] + totals["output_tokens"])
    record(path, "event_msg", {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": last, "model_context_window": 258400},
                               "rate_limits": {"limit_id": "codex", "primary": {"used_percent": 91.0, "window_minutes": 300, "resets_at": int(time.time()) + 1800},
                                               "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": int(time.time()) + 3 * 86400},
                                               "credits": {"has_credits": False, "unlimited": False, "balance": None}, "plan_type": "plus"}})


def finish(run_input=32000, run_cached=30000, run_output=250):
    record_usage(run_input, run_cached, run_output)
    emit({"type": "turn.completed", "usage": {"input_tokens": totals["input_tokens"], "cached_input_tokens": totals["cached_input_tokens"],
                                              "cache_write_input_tokens": 0, "output_tokens": totals["output_tokens"],
                                              "reasoning_output_tokens": totals["reasoning_output_tokens"]}})


emit({"type": "thread.started", "thread_id": sid})
emit({"type": "turn.started"})
if prompt.startswith("sleep"):
    text = "The user session was not found in Redis" if prompt.startswith("sleep-notfound") else "sleeping"
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
    record_usage(run_input=7000, run_cached=6000, run_output=70)  # уже потрачено до остановки
    time.sleep(60)
    sys.exit(0)
if prompt.startswith("nologin"):
    message = "unexpected status 401 Unauthorized: Your access token could not be refreshed. Please log out and sign in again."
    emit({"type": "error", "message": message})
    emit({"type": "turn.failed", "error": {"message": message}})
    sys.exit(1)
if prompt.startswith("linger"):
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "ответ готов"}})
    finish()
    time.sleep(60)  # процесс живёт дальше, будто фоновая задача держит stdout
    sys.exit(0)
if prompt.startswith("crash"):
    print("garbage line", flush=True)
    sys.exit(3)
if prompt.startswith("agents"):
    emit({"type": "item.started", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": sid,
                                           "receiver_thread_ids": [], "prompt": "research topic", "agents_states": {}, "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": sid,
                                             "receiver_thread_ids": ["agent-a"], "prompt": "research topic",
                                             "agents_states": {"agent-a": {"status": "running", "message": None}}, "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": sid,
                                             "receiver_thread_ids": ["agent-b"], "prompt": "write draft",
                                             "agents_states": {"agent-b": {"status": "running", "message": None}}, "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "collab_tool_call", "tool": "wait", "sender_thread_id": sid,
                                             "receiver_thread_ids": ["agent-a", "agent-b"], "prompt": None,
                                             "agents_states": {"agent-a": {"status": "completed", "message": "ok"}, "agent-b": {"status": "errored", "message": "boom"}},
                                             "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": "Агенты закончили"}})
    finish()
    sys.exit(0)

emit({"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"})
emit({"type": "item.started", "item": {"id": "item_0", "type": "todo_list", "items": [
    {"text": "Прочитать README", "completed": True}, {"text": "Прогнать тесты", "completed": False}]}})
emit({"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": "**Смотрю файлы проекта**\n\nНужно понять структуру."}})
emit({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "Смотрю файл"}})
emit({"type": "item.started", "item": {"id": "item_3", "type": "command_execution", "command": "/bin/bash -lc 'pytest -q\nsecond line'",
                                       "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
emit({"type": "item.completed", "item": {"id": "item_3", "type": "command_execution", "command": "/bin/bash -lc 'pytest -q\nsecond line'",
                                         "aggregated_output": "1 failed", "exit_code": 1, "status": "failed"}})
emit({"type": "item.completed", "item": {"id": "item_4", "type": "file_change", "status": "completed",
                                         "changes": [{"path": os.path.join(os.getcwd(), "README.md"), "kind": "update"}]}})
emit({"type": "item.started", "item": {"id": "ws_1", "type": "web_search", "query": "", "action": {"type": "other"}}})
emit({"type": "item.completed", "item": {"id": "ws_1", "type": "web_search", "query": "aiogram docs", "action": {"type": "search", "query": "aiogram docs"}}})
emit({"type": "item.completed", "item": {"id": "item_5", "type": "agent_message",
                                         "text": "Готово: " + prompt[:60] + " | resume=" + (session or "none")}})
finish()
'''

_CLEAR = (
    "CODEX_SANDBOX", "CODEX_NETWORK_ACCESS", "CODEX_WEB_SEARCH", "CODEX_EXTRA_ARGS", "CODEX_EXTRA_INSTRUCTIONS",
    "STATE_FILE", "UPLOADS_DIR", "CODEX_MODEL", "TASK_BASE_BRANCH", "ALLOWED_CHAT_IDS", "ALLOW_PRIVATE_CHATS",
    "GROUP_REQUIRE_MENTION", "CODEX_EFFORT", "SHOW_TOKENS", "TEAM_CHAT_IDS", "WORKSPACE_PER_CHAT",
    "REACTION_WORKING", "REACTION_DONE", "TIMEZONE", "REDACT_ENV_NAMES", "ALLOW_SEND_FILES", "MODEL_BUTTONS",
    "CODEX_TIMEOUT_SECONDS", "CODEX_API_KEY", "FAKE_LOGGED_OUT", "FAKE_APP_SERVER", "FAKE_NEW_THREAD",
)


def make_config(tmp_path: Path, monkeypatch, **overrides) -> Config:
    script = tmp_path / "fake-codex"
    script.write_text(FAKE_CODEX)
    script.chmod(0o755)
    env = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "ALLOWED_USER_IDS": "1",
        "CODEX_BIN": str(script),
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "FAKE_ARGS_LOG": str(tmp_path / "codex-args.jsonl"),
        "WORKSPACE_DIR": str(tmp_path / "ws"),
        "PROGRESS_INTERVAL_SECONDS": "0.05",
        **overrides,
    }
    for name in _CLEAR:
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def codex_calls(tmp_path: Path) -> list[list[str]]:
    """Аргументы всех запусков `codex exec` фиктивного codex (по порядку)."""
    import json

    log = tmp_path / "codex-args.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def app_server_log(tmp_path: Path) -> list:
    """Аргументы и сообщения, полученные фиктивным app-server (по порядку)."""
    import json

    log = tmp_path / "codex-args.jsonl.appserver"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def codex_prompts(tmp_path: Path) -> list[str]:
    """Промпты (stdin) всех запусков фиктивного codex (по порядку)."""
    import json

    log = tmp_path / "codex-args.jsonl.prompts"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


GIT_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]


def git(*args, cwd):
    subprocess.run(["git", *GIT_IDENTITY, *args], cwd=cwd, check=True, capture_output=True)


def make_repo(tmp_path: Path, name: str = "work") -> Path:
    """Создаёт bare-«удалённый» репозиторий и рабочий клон с одним коммитом в main."""
    remote = tmp_path / f"{name}-remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    work = tmp_path / name
    git("clone", str(remote), str(work), cwd=tmp_path)
    git("checkout", "-b", "main", cwd=work)
    (work / "README.md").write_text("hi\n")
    git("add", "README.md", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    return work
