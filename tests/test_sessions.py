"""Чтение журналов сессий Codex (формат сверен с Codex 0.159)."""

import json

from codex_telegram_bot import sessions

SID = "01a0edb0-b845-7982-bf2a-ace833e9dc14"


def _write(home, lines, sid=SID, day="2026/09/29"):
    path = home / "sessions" / day / f"rollout-2026-09-29T15-02-56-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n")
    return path


def _token_count(total_in, last_total, window=258400, used=12.5):
    return {"timestamp": "t", "type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"total_token_usage": {"input_tokens": total_in, "cached_input_tokens": 200, "output_tokens": 50,
                                       "reasoning_output_tokens": 10, "total_tokens": total_in + 50},
                 "last_token_usage": {"input_tokens": 1200, "output_tokens": 50, "total_tokens": last_total},
                 "model_context_window": window},
        "rate_limits": {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": 300, "resets_at": 1790697760},
                        "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": 1790953360}},
    }}


def test_parse_real_like_rollout(tmp_path):
    lines = [
        {"timestamp": "t", "type": "session_meta", "payload": {"id": SID, "cwd": "/w", "cli_version": "0.159.0"}},
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "task_started", "model_context_window": 258400}},
        {"timestamp": "t", "type": "turn_context", "payload": {"model": "gpt-6-astra", "collaboration_mode": {
            "mode": "default", "settings": {"model": "gpt-6-astra", "reasoning_effort": None}}}},
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": []}}},
        _token_count(1200, 1250),
        {"timestamp": "t", "type": "turn_context", "payload": {"model": "gpt-5.5", "collaboration_mode": {
            "mode": "default", "settings": {"model": "gpt-5.5", "reasoning_effort": "high"}}}},
        {"timestamp": "t", "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": []}}},
        {"timestamp": "t", "type": "compacted", "payload": {"message": "summary"}},
        _token_count(2400, 900, used=55.0),
        "not json at all",
    ]
    path = _write(tmp_path, lines[:-1])
    with path.open("a") as handle:
        handle.write("not json at all\n")
    info = sessions.read_session_info(SID, home=tmp_path)
    assert info is not None and info.path == path
    assert (info.model, info.effort) == ("gpt-5.5", "high")
    assert (info.context_window, info.context_tokens) == (258400, 900)
    assert (info.total_input_tokens, info.total_cached_tokens, info.total_output_tokens, info.total_reasoning_tokens) == (2400, 200, 50, 10)
    assert info.user_messages == 2 and info.compactions == 1
    assert info.rate_limits["primary"]["used_percent"] == 55.0


def test_find_and_delete_including_archived(tmp_path):
    _write(tmp_path, [_token_count(1, 1)])
    archived = tmp_path / "archived_sessions" / f"rollout-2026-09-28T10-00-00-{SID}.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_text("{}\n")
    other = _write(tmp_path, [_token_count(1, 1)], sid="other-session")
    assert len(sessions.find_session_files(SID, home=tmp_path)) == 2
    assert sessions.delete_session_files(SID, home=tmp_path) == 2
    assert sessions.find_session_files(SID, home=tmp_path) == []
    assert other.exists()


def test_unknown_or_unsafe_id(tmp_path):
    assert sessions.read_session_info("missing", home=tmp_path) is None
    assert sessions.find_session_files("../../etc", home=tmp_path) == []
    assert sessions.find_session_files("", home=tmp_path) == []


def test_large_file_reads_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_READ_BYTES", 2000)
    filler = [{"timestamp": "t", "type": "response_item", "payload": {"type": "message", "text": "x" * 200}} for _ in range(50)]
    _write(tmp_path, [*filler, _token_count(5000, 4000)])
    info = sessions.read_session_info(SID, home=tmp_path)
    assert info is not None and info.context_tokens == 4000


def test_main_rate_limit_family_preferred(tmp_path):
    other = _token_count(10, 10, used=99.0)
    other["payload"]["rate_limits"]["limit_id"] = "codex_bengalfox"
    _write(tmp_path, [_token_count(10, 10, used=20.0), other])
    info = sessions.read_session_info(SID, home=tmp_path)
    assert info.rate_limits["limit_id"] == "codex" and info.rate_limits["primary"]["used_percent"] == 20.0


def test_rate_limit_snapshot_time_is_taken_from_log(tmp_path):
    record = _token_count(10, 10)
    record["timestamp"] = "2026-09-29T14:55:24.013Z"
    _write(tmp_path, [record])
    info = sessions.read_session_info(SID, home=tmp_path)
    assert abs(info.rate_limits_at - 1790693724.013) < 0.01
