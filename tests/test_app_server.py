"""Клиент codex app-server для /compact (протокол сверен с Codex 0.159)."""

import asyncio
import time

import pytest
from helpers import app_server_log, make_config

from codex_telegram_bot.app_server import AppServerError, app_server_flags, compact_thread
from codex_telegram_bot.codex_runner import CodexRunner


def test_app_server_flags_keep_only_config_overrides():
    args = ["-c", "a=1", "--model", "x", "--enable", "goals", "--disable=plugins", "--config", "b=2", "--json"]
    assert app_server_flags(args) == ["-c", "a=1", "--enable", "goals", "--disable=plugins", "--config", "b=2"]


def _session(tmp_path, monkeypatch, **env):
    cfg = make_config(tmp_path, monkeypatch, **env)
    runner = CodexRunner(cfg)
    first = asyncio.run(runner.run("hello", tmp_path))
    return cfg, runner, first.session_id


def test_compact_thread_ok(tmp_path, monkeypatch):
    cfg, runner, sid = _session(tmp_path, monkeypatch, CODEX_EXTRA_ARGS='-c openai_base_url="http://x"')
    outcome = asyncio.run(runner.compact(sid, tmp_path, model="gpt-6-sol"))
    assert outcome.compacted and outcome.status == "completed" and not outcome.error
    assert (outcome.before_tokens, outcome.after_tokens, outcome.context_window) == (32250, 5000, 258400)
    assert outcome.model == "gpt-6-sol"
    log = app_server_log(tmp_path)
    assert log[0] == ["app-server", "-c", "openai_base_url=http://x"]  # CODEX_EXTRA_ARGS доходят и сюда
    methods = [m.get("method") for m in log[1:]]
    assert methods == ["initialize", "initialized", "thread/resume", "thread/compact/start"]
    resume = log[3]["params"]
    assert resume["threadId"] == sid and resume["excludeTurns"] is True and resume["model"] == "gpt-6-sol"
    assert "Telegram bot" in resume["developerInstructions"]


def test_compact_thread_missing_session(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    with pytest.raises(AppServerError, match="no rollout found"):
        asyncio.run(CodexRunner(cfg).compact("gone-1", tmp_path))


def test_compact_thread_failed_turn(tmp_path, monkeypatch):
    cfg, runner, sid = _session(tmp_path, monkeypatch, FAKE_APP_SERVER="fail")
    outcome = asyncio.run(runner.compact(sid, tmp_path))
    assert not outcome.compacted and outcome.status == "failed" and outcome.error == "remote compaction failed"


def test_compact_thread_server_crash(tmp_path, monkeypatch):
    cfg, runner, sid = _session(tmp_path, monkeypatch, FAKE_APP_SERVER="crash")
    with pytest.raises(AppServerError, match="завершился"):
        asyncio.run(runner.compact(sid, tmp_path))


def test_compact_thread_cancel_and_timeout(tmp_path, monkeypatch):
    cfg, runner, sid = _session(tmp_path, monkeypatch, FAKE_APP_SERVER="hang")

    async def cancelled():
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.5, cancel.set)
        return await runner.compact(sid, tmp_path, cancel=cancel)

    start = time.monotonic()
    outcome = asyncio.run(cancelled())
    assert outcome.cancelled and not outcome.compacted
    assert time.monotonic() - start < 20

    start = time.monotonic()
    outcome = asyncio.run(compact_thread(cfg.codex_bin, sid, cwd=tmp_path, env=runner.build_env(), timeout=1))
    assert outcome.timed_out and not outcome.compacted
    assert time.monotonic() - start < 20


def test_turn_completed_before_reply_is_not_missed(tmp_path, monkeypatch):
    """turn/completed может прийти раньше ответа на thread/compact/start — ждать его нельзя."""
    cfg, runner, sid = _session(tmp_path, monkeypatch, FAKE_APP_SERVER="early")
    start = time.monotonic()
    outcome = asyncio.run(compact_thread(cfg.codex_bin, sid, cwd=tmp_path, env=runner.build_env(), timeout=30))
    assert outcome.compacted and outcome.status == "completed"
    assert time.monotonic() - start < 15
