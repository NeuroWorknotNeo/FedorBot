"""Тесты отправки файлов в чат по маркеру TG_SEND_FILE (с ограничением рабочим каталогом)."""

import asyncio

from helpers import make_config
from test_engine import FakeBot, FakeMessage

from codex_telegram_bot.bot import Engine, Job, parse_send_files, resolve_outbound_file
from codex_telegram_bot.state import StateStore


def test_parse_extracts_and_strips():
    text = "Готово, вот отчёт.\nTG_SEND_FILE: /w/report.pdf\nещё текст\n`TG_SEND_FILE: /w/a.png`"
    kept, paths = parse_send_files(text)
    assert paths == ["/w/report.pdf", "/w/a.png"]
    assert "TG_SEND_FILE" not in kept
    assert "Готово, вот отчёт." in kept and "ещё текст" in kept


def test_parse_no_markers():
    kept, paths = parse_send_files("обычный ответ без файлов")
    assert paths == [] and kept == "обычный ответ без файлов"


def test_resolve_inside_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    f = root / "report.pdf"
    f.write_text("x")
    assert resolve_outbound_file("report.pdf", [root]) == f.resolve()
    assert resolve_outbound_file(str(f), [root]) == f.resolve()


def test_resolve_outside_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    assert resolve_outbound_file(str(outside), [root]) is None
    assert resolve_outbound_file("../secret.txt", [root]) is None


def test_resolve_sensitive_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    env = root / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x")
    assert resolve_outbound_file(str(env), [root]) is None
    auth = root / "auth.json"  # учётные данные Codex
    auth.write_text("{}")
    assert resolve_outbound_file(str(auth), [root]) is None


def test_resolve_missing_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert resolve_outbound_file("nope.pdf", [root]) is None


def _engine(tmp_path, monkeypatch, **overrides):
    cfg = make_config(tmp_path, monkeypatch, **overrides)
    bot = FakeBot()
    return cfg, bot, Engine(bot, cfg, StateStore(tmp_path / "state.json"))


def _docs(bot):
    return [item for item in bot.sent if item[0] == "document"]


def _sends(bot):
    return [item[2] for item in bot.sent if item[0] == "send"]


def test_send_document_from_workspace(tmp_path, monkeypatch):
    cfg, bot, engine = _engine(tmp_path, monkeypatch)
    project = cfg.workspace_dir
    project.mkdir(parents=True, exist_ok=True)
    (project / "report.pdf").write_bytes(b"%PDF-1.4 test")
    asyncio.run(engine._send_outbound_files(Job(1, "p", FakeMessage(bot, 1)), project, ["report.pdf"]))
    docs = _docs(bot)
    assert len(docs) == 1 and docs[0][2].endswith("report.pdf")


def test_send_rejects_outside_workspace(tmp_path, monkeypatch):
    cfg, bot, engine = _engine(tmp_path, monkeypatch)
    project = cfg.workspace_dir
    project.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    asyncio.run(engine._send_outbound_files(Job(1, "p", FakeMessage(bot, 1)), project, [str(outside)]))
    assert not _docs(bot)
    assert any("вне рабочего каталога" in t for t in _sends(bot))


def test_send_disabled(tmp_path, monkeypatch):
    cfg, bot, engine = _engine(tmp_path, monkeypatch, ALLOW_SEND_FILES="false")
    project = cfg.workspace_dir
    project.mkdir(parents=True, exist_ok=True)
    (project / "r.pdf").write_bytes(b"x")
    asyncio.run(engine._send_outbound_files(Job(1, "p", FakeMessage(bot, 1)), project, ["r.pdf"]))
    assert not _docs(bot)
    assert any("отключена" in t for t in _sends(bot))
