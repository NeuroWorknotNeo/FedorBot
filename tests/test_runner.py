import asyncio
import dataclasses
import time
from pathlib import Path

from helpers import codex_calls, make_config

from codex_telegram_bot.codex_runner import (
    CodexRunner,
    looks_like_context_overflow,
    looks_like_usage_limit,
    RunProgress,
    RunResult,
    clean_stderr,
    codex_login_status,
    describe_auth,
    looks_like_auth_error,
    reasoning_headline,
    summarize_command,
    toml_string,
)


def test_build_command_new_session(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch, CODEX_EXTRA_ARGS='-c model_reasoning_summary="auto"')
    cmd = CodexRunner(cfg).build_command(model="gpt-6-sol", effort="high", images=["/tmp/a.png"])
    assert cmd[:4] == [cfg.codex_bin, "exec", "--json", "--skip-git-repo-check"]
    assert cmd[-2:] == ["--", "-"]  # промпт идёт через stdin
    assert "resume" not in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-6-sol"
    assert 'model_reasoning_effort="high"' in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert 'web_search="live"' in cmd
    assert cmd[cmd.index("--image") + 1] == "/tmp/a.png"
    assert cmd.index("--image") < cmd.index("--")
    # CODEX_EXTRA_ARGS идут раньше явных настроек
    # (кавычки снимает shlex, как в shell; codex примет auto и без них)
    assert cmd.index("model_reasoning_summary=auto") < cmd.index("--model")
    instructions = next(a for a in cmd if a.startswith("developer_instructions="))
    assert "You are being driven through a Telegram bot" in instructions
    assert "10800 seconds" in instructions


def test_build_command_resume_and_sandbox(tmp_path, monkeypatch):
    cfg = make_config(
        tmp_path, monkeypatch, CODEX_SANDBOX="workspace-write", CODEX_NETWORK_ACCESS="false",
        CODEX_WEB_SEARCH="false", CODEX_EXTRA_INSTRUCTIONS="",
    )
    cmd = CodexRunner(cfg).build_command(resume="sess-1")
    assert cmd[:3] == [cfg.codex_bin, "exec", "resume"]
    assert cmd[-3:] == ["--", "sess-1", "-"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert 'sandbox_mode="workspace-write"' in cmd and 'approval_policy="never"' in cmd
    assert "sandbox_workspace_write.network_access=false" in cmd
    assert 'web_search="disabled"' in cmd
    assert not any(a.startswith("developer_instructions=") for a in cmd)
    assert "--model" not in cmd and not any(a.startswith("model_reasoning_effort") for a in cmd)


def test_ultra_effort_keeps_same_instructions(tmp_path, monkeypatch):
    """ultra — уровень самой модели (рассуждения + делегирование); инструкции бота не меняются.

    Codex не применяет изменённые developer_instructions при resume до ближайшего
    сжатия, поэтому они должны быть одинаковыми во всех запусках.
    """
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    ultra = runner.build_command(effort="ultra")
    plain = runner.build_command()
    assert 'model_reasoning_effort="ultra"' in ultra
    pick = lambda cmd: next(a for a in cmd if a.startswith("developer_instructions="))  # noqa: E731
    assert pick(ultra) == pick(plain)


def test_child_env_hides_bot_token(tmp_path, monkeypatch):
    env = CodexRunner(make_config(tmp_path, monkeypatch)).build_env()
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert str(Path.home() / ".local" / "bin") in env["PATH"]
    assert env["NO_COLOR"] == "1"


def test_run_parses_stream(tmp_path, monkeypatch):
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    snapshots = []
    result = asyncio.run(
        runner.run("hello", tmp_path, model="gpt-6-luna", effort="low", on_progress=lambda p: snapshots.append(p.total_tool_calls))
    )
    assert not result.is_error and not result.cancelled and not result.timed_out
    assert result.session_id == "sess-123"
    assert result.text == "Готово: hello | resume=none"
    assert result.exit_code == 0 and result.subtype == "success"
    assert (result.input_tokens, result.output_tokens, result.cache_read_tokens) == (32000, 250, 30000)
    assert result.model == "gpt-6-luna"  # точная модель — из журнала сессии
    assert result.rate_limits and result.rate_limits["primary"]["used_percent"] == 91.0
    progress = result.progress
    assert progress.turns == 1
    assert progress.total_tool_calls == 3
    assert [c.label() for c in progress.tool_calls] == [
        "❌ Shell: pytest -q",
        "✏️ Edit: README.md",
        "🌐 WebSearch: aiogram docs",
    ]
    assert progress.last_text == "Готово: hello | resume=none"
    assert progress.reasoning == "Смотрю файлы проекта"
    assert progress.plan == [("Прочитать README", True), ("Прогнать тесты", False)]
    assert progress.notices == ["⚠️ Reconnecting... 1/5 (stream disconnected before completion)"]
    assert snapshots and snapshots[-1] == 3
    # промпт ушёл через stdin, а не аргументом
    assert "hello" not in codex_calls(tmp_path)[0]


def test_resume_counts_only_this_run(tmp_path, monkeypatch):
    """Codex присылает накопленный итог по сессии; бот показывает расход именно запуска."""
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    first = asyncio.run(runner.run("hello", tmp_path))
    second = asyncio.run(runner.run("again", tmp_path, resume=first.session_id))
    assert second.text == "Готово: again | resume=sess-123"
    assert (second.input_tokens, second.output_tokens) == (32000, 250)
    assert codex_calls(tmp_path)[1][:2] == ["exec", "resume"]


def test_run_cancel(tmp_path, monkeypatch):
    runner = CodexRunner(make_config(tmp_path, monkeypatch))

    async def go():
        cancel = asyncio.Event()

        async def trigger():
            await asyncio.sleep(0.7)
            cancel.set()

        asyncio.create_task(trigger())
        start = time.monotonic()
        result = await runner.run("sleep", tmp_path, cancel=cancel)
        return result, time.monotonic() - start

    result, elapsed = asyncio.run(go())
    assert result.cancelled and not result.is_error
    assert elapsed < 20
    assert result.text == "sleeping"
    assert result.session_id == "sess-123"
    # остановленный запуск тоже расходовал токены — они считаются по журналу сессии
    assert (result.input_tokens, result.output_tokens) == (7000, 70)


def test_run_timeout(tmp_path, monkeypatch):
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    result = asyncio.run(runner.run("sleep", tmp_path, timeout=1))
    assert result.timed_out and result.is_error and not result.cancelled


def test_run_without_result_event(tmp_path, monkeypatch):
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    result = asyncio.run(runner.run("crash", tmp_path))
    assert result.is_error and result.exit_code == 3 and result.subtype == "no_result"
    assert "garbage line" in result.text
    assert result.session_id == "sess-123"


def test_turn_failed_is_error(tmp_path, monkeypatch):
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    result = asyncio.run(runner.run("nologin", tmp_path))
    assert result.is_error and result.subtype == "turn_failed"
    assert "401 Unauthorized" in result.text
    assert looks_like_auth_error(result)


def test_missing_session_on_resume(tmp_path, monkeypatch):
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    result = asyncio.run(runner.run("hi", tmp_path, resume="gone-1"))
    assert result.is_error and result.exit_code == 1
    assert "no rollout found for thread id gone-1" in result.text


def test_missing_binary(tmp_path, monkeypatch):
    cfg = dataclasses.replace(make_config(tmp_path, monkeypatch), codex_bin=str(tmp_path / "nope"))
    result = asyncio.run(CodexRunner(cfg).run("hi", tmp_path))
    assert result.is_error and result.subtype == "spawn_error"


def test_result_without_stream_close_is_delivered(tmp_path, monkeypatch):
    import codex_telegram_bot.codex_runner as runner_module

    monkeypatch.setattr(runner_module, "RESULT_GRACE_SECONDS", 1.0)
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    start = time.monotonic()
    result = asyncio.run(runner.run("linger", tmp_path))
    assert time.monotonic() - start < 20
    assert result.text == "ответ готов" and not result.is_error and not result.timed_out and not result.cancelled
    assert result.session_id == "sess-123"


def test_summarize_command():
    assert summarize_command("/bin/bash -lc 'pytest -q'") == "pytest -q"
    assert summarize_command("bash -lc \"git log --oneline -5\"") == "git log --oneline -5"
    assert summarize_command(["bash", "-lc", "ls -la"]) == "ls -la"
    assert summarize_command(["git", "status"]) == "git status"
    assert summarize_command("x" * 200).endswith("…")
    assert summarize_command(None) == ""


def test_toml_string_escapes():
    assert toml_string('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'
    assert toml_string("😀 ё\x07") == '"😀 ё\\u0007"'


def test_reasoning_headline():
    assert reasoning_headline("**Планирую правку**\n\nДетали") == "Планирую правку"
    assert reasoning_headline("просто текст\nвторая строка") == "просто текст"


def test_clean_stderr():
    noisy = (
        'WARNING: proceeding, even though we could not create PATH aliases: Refusing …\n'
        "Reading prompt from stdin...\n"
        "Error: real problem\n"
    )
    assert clean_stderr(noisy) == "Error: real problem"


def test_login_status_and_description(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    status = asyncio.run(codex_login_status(cfg.codex_bin))
    assert status == {"loggedIn": True, "method": "chatgpt", "text": "Logged in using ChatGPT"}
    assert describe_auth(status) == "✅ вход через аккаунт ChatGPT (подписка)"
    monkeypatch.setenv("FAKE_LOGGED_OUT", "1")
    logged_out = asyncio.run(codex_login_status(cfg.codex_bin))
    assert logged_out["loggedIn"] is False
    assert describe_auth(logged_out).startswith("❌ не авторизован")
    assert describe_auth({"error": "boom"}) == "неизвестно (boom)"
    api = {"loggedIn": True, "method": "api_key", "text": "Logged in using an API key - sk-proj-***fFAKE"}
    assert describe_auth(api) == "✅ API-ключ OpenAI (sk-proj-***fFAKE) — оплата по тарифам API, не подписка"
    missing = asyncio.run(codex_login_status(str(tmp_path / "nope")))
    assert "error" in missing


def test_looks_like_auth_error():
    def res(text, err=True, subtype="turn_failed", **kw):
        return RunResult(text=text, session_id=None, is_error=err, subtype=subtype, exit_code=1, **kw)

    assert looks_like_auth_error(res("unexpected status 401 Unauthorized: Missing bearer or basic authentication"))
    assert looks_like_auth_error(res("Your refresh token was already used. Please log out and sign in again."))
    assert looks_like_auth_error(res("", subtype="no_result", stderr="Error: Not logged in"))
    assert not looks_like_auth_error(res("401 Unauthorized", err=False))
    assert not looks_like_auth_error(res("We’re currently experiencing high demand"))
    # слова из ответа модели при таймауте — не ошибка входа
    assert not looks_like_auth_error(res("fixed the authentication middleware, 401 Unauthorized now handled",
                                         subtype="no_result", timed_out=True))
    assert not looks_like_auth_error(res("401 Unauthorized in logs", subtype="no_result"))


def test_result_without_usage_has_no_tokens():
    result = CodexRunner._result_from_event({"type": "turn.completed"}, RunProgress(), 0, "")
    assert result.input_tokens is None and result.output_tokens is None and not result.is_error


def test_usage_limit_and_context_overflow_detection():
    def res(text, err=True):
        return RunResult(text=text, session_id=None, is_error=err, subtype="turn_failed", exit_code=1)

    limit = "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro) or try again at 5:56 PM."
    overflow = "Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying."
    assert looks_like_usage_limit(res(limit)) and not looks_like_usage_limit(res(limit, err=False))
    assert looks_like_context_overflow(res(overflow)) and not looks_like_context_overflow(res(limit))
    assert not looks_like_usage_limit(res("stream disconnected before completion"))


def test_describe_auth_warns_about_api_key_env():
    text = describe_auth({"loggedIn": True, "method": "chatgpt", "text": "Logged in using ChatGPT", "api_key_env": True})
    assert "CODEX_API_KEY" in text and "тарифам API" in text


def test_delete_session(tmp_path, monkeypatch):
    runner = CodexRunner(make_config(tmp_path, monkeypatch))
    first = asyncio.run(runner.run("hello", tmp_path))
    assert asyncio.run(runner.delete_session(first.session_id)) is True
    assert asyncio.run(runner.delete_session(first.session_id)) is False  # уже удалена
    assert asyncio.run(runner.delete_session("../etc")) is False
