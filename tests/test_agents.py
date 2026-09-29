"""Тесты живого отслеживания субагентов Codex (collab_tool_call) в прогрессе."""

import asyncio

from helpers import make_config

from codex_telegram_bot.bot import render_progress
from codex_telegram_bot.codex_runner import CodexRunner, RunProgress


def _runner(tmp_path, monkeypatch):
    return CodexRunner(make_config(tmp_path, monkeypatch))


def _collab(phase, item_id, tool, receivers, states=None, prompt=None, status="completed"):
    return {
        "type": f"item.{phase}",
        "item": {
            "id": item_id, "type": "collab_tool_call", "tool": tool, "sender_thread_id": "main",
            "receiver_thread_ids": receivers, "prompt": prompt, "agents_states": states or {}, "status": status,
        },
    }


def test_agents_lifecycle(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    p = RunProgress()
    runner._handle_event(_collab("started", "i1", "spawn_agent", [], prompt="research", status="in_progress"), p, tmp_path)
    assert p.agents == {}  # агент появится, когда Codex сообщит его id
    runner._handle_event(_collab("completed", "i1", "spawn_agent", ["a1"], {"a1": {"status": "running"}}, prompt="research"), p, tmp_path)
    runner._handle_event(_collab("completed", "i2", "spawn_agent", ["a2"], {"a2": {"status": "pending_init"}}, prompt="write"), p, tmp_path)
    assert p.agents_running == 2 and p.agents_done == 0
    assert p.agents["a1"]["desc"] == "research"
    runner._handle_event(_collab("completed", "i3", "wait", ["a1"], {"a1": {"status": "completed", "message": "ok"}}), p, tmp_path)
    assert p.agents_running == 1 and p.agents_done == 1
    assert p.agents["a1"]["done"] and not p.agents["a1"]["failed"]
    runner._handle_event(_collab("completed", "i4", "wait", ["a2"], {"a2": {"status": "errored", "message": "boom"}}), p, tmp_path)
    assert p.agents_running == 0 and p.agents_done == 2
    assert p.agents["a2"]["failed"]


def test_unknown_agent_in_states_is_ignored(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    p = RunProgress()
    runner._handle_event(_collab("completed", "i1", "wait", ["zz"], {"zz": {"status": "completed"}}), p, tmp_path)
    assert p.agents == {}


def test_agents_scenario_end_to_end(tmp_path, monkeypatch):
    result = asyncio.run(_runner(tmp_path, monkeypatch).run("agents", tmp_path))
    assert not result.is_error
    agents = result.progress.agents
    assert set(agents) == {"agent-a", "agent-b"}
    assert agents["agent-a"]["done"] and not agents["agent-a"]["failed"]
    assert agents["agent-b"]["done"] and agents["agent-b"]["failed"]
    assert result.text == "Агенты закончили"


def test_render_progress_shows_agents():
    p = RunProgress()
    p.agents = {
        "a1": {"desc": "research topic", "done": True, "failed": False},
        "a2": {"desc": "write draft", "done": False, "failed": False},
        "a3": {"desc": "broken", "done": True, "failed": True},
    }
    out = render_progress(p)
    assert "🤖 Агенты: 1 работают · 2 готово" in out
    assert "✅ research topic" in out
    assert "⏳ write draft" in out
    assert "❌ broken" in out


def test_interrupted_agent_is_not_left_running(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    p = RunProgress()
    runner._handle_event(_collab("completed", "i1", "spawn_agent", ["a1"], {"a1": {"status": "running"}}, prompt="x"), p, tmp_path)
    runner._handle_event(_collab("completed", "i2", "wait", ["a1"], {"a1": {"status": "interrupted"}}), p, tmp_path)
    assert p.agents_running == 0 and p.agents["a1"]["failed"]
