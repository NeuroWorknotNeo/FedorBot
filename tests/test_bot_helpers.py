from codex_telegram_bot.bot import (
    _looks_like_missing_session,
    normalize_repo_url,
    render_progress,
    repo_name_from_url,
    safe_filename,
)
from codex_telegram_bot.codex_runner import RunProgress, RunResult, ToolCall


def test_normalize_repo_url():
    assert normalize_repo_url("owner/repo") == "https://github.com/owner/repo.git"
    assert normalize_repo_url("https://github.com/owner/repo") == "https://github.com/owner/repo"
    assert normalize_repo_url("git@github.com:owner/repo.git") == "git@github.com:owner/repo.git"


def test_repo_name_from_url():
    assert repo_name_from_url("https://github.com/owner/My-Repo.git") == "My-Repo"
    assert repo_name_from_url("git@github.com:owner/repo.git") == "repo"
    assert repo_name_from_url("https://github.com/owner/repo/") == "repo"


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("отчёт (1).pdf") == "отчёт_1_.pdf"
    assert safe_filename("") == "file"


def test_render_progress_escapes_html():
    progress = RunProgress(model="gpt-6-sol", turns=2)
    progress.add_tool_call(ToolCall("Shell", "echo <b>&"))
    progress.last_text = "думаю <i>"
    progress.reasoning = "Проверяю <тесты>"
    progress.plan = [("Прочитать <код>", True), ("Починить", False)]
    progress.add_notice("⚠️ <x>")
    text = render_progress(progress)
    assert "gpt-6-sol" in text
    assert "💻 Shell: echo &lt;b&gt;&amp;" in text
    assert "<i>думаю &lt;i&gt;</i>" in text
    assert "🤔 Проверяю &lt;тесты&gt;" in text
    assert "📋 План: 1/2 · сейчас: Починить" in text
    assert "⚠️ &lt;x&gt;" in text
    assert text.endswith("/stop — остановить")


def test_missing_session_detection():
    missing = "Error: thread/resume: thread/resume failed: no rollout found for thread id x (code -32600)"
    assert _looks_like_missing_session(
        RunResult(text=missing, session_id=None, is_error=True, subtype="no_result", exit_code=1, stderr=missing)
    )
    assert not _looks_like_missing_session(
        RunResult(text="We’re currently experiencing high demand", session_id=None, is_error=True, subtype="turn_failed", exit_code=1)
    )
    # ответ модели про «session not found» при таймауте — не повод выбрасывать сессию
    timed_out = RunResult(
        text="The user session was not found in Redis", session_id="s", is_error=True, subtype="no_result",
        exit_code=1, timed_out=True, stderr="",
    )
    assert not _looks_like_missing_session(timed_out)
    # тот же текст ошибки, но ход уже шёл — сессия была
    started = RunResult(text="x", session_id="s", is_error=True, subtype="no_result", exit_code=1, stderr=missing)
    started.progress.turns = 1
    assert not _looks_like_missing_session(started)


from datetime import datetime, timezone

from aiogram.types import Chat, Message, User

from codex_telegram_bot.bot import addressed_to_bot, command_name, conv_key, strip_mention, thread_of


def make_message(text, chat_id=1, chat_type="private", thread_id=None, reply_from_id=None):
    chat = Chat(id=chat_id, type=chat_type)
    reply = None
    if reply_from_id is not None:
        reply = Message(
            message_id=5, date=datetime.now(timezone.utc), chat=chat,
            from_user=User(id=reply_from_id, is_bot=reply_from_id == 42, first_name="x"), text="ранее",
        )
    return Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat,
        from_user=User(id=1, is_bot=False, first_name="u"), text=text,
        message_thread_id=thread_id, is_topic_message=bool(thread_id) or None, reply_to_message=reply,
    )


def test_strip_mention_and_command_name():
    assert strip_mention("@TestBot привет, @testbot", "testbot") == "привет,"
    assert strip_mention("без упоминания", "testbot") == "без упоминания"
    assert strip_mention("  текст  ", None) == "текст"
    assert command_name(make_message("/task@testbot сделай")) == "/task"
    assert command_name(make_message("/ID")) == "/id"
    assert command_name(make_message("просто текст")) == ""


def test_conv_key_and_thread():
    assert conv_key(make_message("x")) == "1"
    assert thread_of(make_message("x")) is None
    topic = make_message("x", chat_id=-100123, chat_type="supergroup", thread_id=7)
    assert conv_key(topic) == "-100123:7"
    assert thread_of(topic) == 7


def test_addressed_to_bot():
    assert addressed_to_bot(make_message("@TestBot привет"), "testbot", 42)
    assert addressed_to_bot(make_message("ответ", reply_from_id=42), "testbot", 42)
    assert not addressed_to_bot(make_message("ответ человеку", reply_from_id=7), "testbot", 42)
    assert not addressed_to_bot(make_message("просто текст"), "testbot", 42)
    assert not addressed_to_bot(make_message("@testbot"), None, None)


def test_context_report():
    from pathlib import Path

    from codex_telegram_bot.bot import context_report
    from codex_telegram_bot.sessions import SessionInfo

    info = SessionInfo(
        path=Path("/x"), model="gpt-6-sol", effort="high", context_window=258400, context_tokens=129200,
        total_input_tokens=1_500_000, total_cached_tokens=1_200_000, total_output_tokens=35_000,
        total_reasoning_tokens=20_000, user_messages=12, compactions=1,
    )
    report = context_report(info, model_hint=None)
    assert "**Модель:** gpt-6-sol · рассуждения: high" in report
    assert "**Контекст:** 129.2k из 258.4k токенов (50%), свободно ~50%" in report
    assert "↑1.50M (из них кэш 1.20M) ↓35.0k (рассуждения 20.0k)" in report
    assert "**Сообщений в сессии:** 12" in report and "**Сжатий истории:** 1" in report
    assert "не найден" in context_report(None, model_hint="gpt-6-sol")
    fresh = context_report(None, model_hint="gpt-6-sol", has_session=False)
    assert "после первого ответа" in fresh and "gpt-6-sol" in fresh


def test_concurrency_warning_fires_when_memory_is_short():
    from codex_telegram_bot.bot import concurrency_warning

    note = concurrency_warning(6, 3.3, 0.0)
    assert note is not None
    assert "MAX_CONCURRENT_RUNS=6" in note and "3.3 ГБ" in note
    assert "около 2" in note
    assert "Swap не настроен" in note


def test_concurrency_warning_silent_when_memory_is_enough():
    from codex_telegram_bot.bot import concurrency_warning

    assert concurrency_warning(2, 3.3, 0.0) is None
    assert concurrency_warning(6, 8.0, 4.0) is None
    assert concurrency_warning(6, None, None) is None  # /proc/meminfo недоступен


def test_memory_totals_gb_reads_something():
    from codex_telegram_bot.bot import memory_totals_gb

    ram, swap = memory_totals_gb()
    assert ram is None or ram > 0
    assert swap is None or swap >= 0
