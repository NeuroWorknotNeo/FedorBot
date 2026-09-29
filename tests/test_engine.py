"""Сквозная проверка движка: очередь → фиктивный codex → доставка ответа в «чат»."""

import asyncio
from types import SimpleNamespace

from helpers import app_server_log, codex_calls, codex_prompts, make_config, make_repo

from codex_telegram_bot import git_tasks
from codex_telegram_bot.bot import COMPACT_PROMPT, Engine, Job
from codex_telegram_bot.state import StateStore


class FakeBot:
    def __init__(self):
        self.sent: list[tuple] = []
        self.actions = 0
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(("send", chat_id, text))
        return FakeMessage(self, chat_id, self._new_id())

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.actions += 1

    async def send_document(self, chat_id, document, **kwargs):
        # document — FSInputFile; сохраняем путь для проверки
        self.sent.append(("document", chat_id, str(getattr(document, "path", document))))
        return FakeMessage(self, chat_id, self._new_id())

    async def set_message_reaction(self, chat_id, message_id, reaction=None, **kwargs):
        self.sent.append(("react", message_id, [r.emoji for r in (reaction or [])]))

    async def get_me(self):
        class Me:
            id = 42
            username = "testbot"

        return Me()

    def _new_id(self):
        self._next_id += 1
        return self._next_id


class FakeMessage:
    def __init__(self, bot: FakeBot, chat_id: int, message_id: int = 1):
        self.bot = bot
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.message_id = message_id

    async def answer(self, text, **kwargs):
        self.bot.sent.append(("answer", self.chat_id, text))
        return FakeMessage(self.bot, self.chat_id, self.bot._new_id())

    async def edit_text(self, text, **kwargs):
        self.bot.sent.append(("edit", self.message_id, text))


def texts(bot: FakeBot, kind: str) -> list[str]:
    return [item[2] for item in bot.sent if item[0] == kind]


async def run_job(engine: Engine, job: Job) -> None:
    await engine.submit(job)
    runtime = engine.runtime_info(job.chat_id)
    assert runtime is not None
    await asyncio.wait_for(runtime.queue.join(), timeout=30)


def test_chat_flow_keeps_session(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        assert engine.codex_version_str == "codex-cli 9.9.9"
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        await run_job(engine, Job(1, "again", FakeMessage(bot, 1, message_id=2)))
        return engine

    engine = asyncio.run(go())
    sent = texts(bot, "send")
    assert sent[0] == "Готово: hello | resume=none"
    assert sent[1] == "Готово: again | resume=sess-123"
    assert any(t.startswith("⏳ Запускаю Codex") for t in texts(bot, "answer"))
    assert any(
        t.startswith("✅ Готово") and "инструментов: 3" in t and "gpt-6-sol" in t and "токены ↑32.0k ↓250" in t
        and "Лимит подписки близок к исчерпанию (91%)" in t
        for t in texts(bot, "edit")
    )
    assert engine.state.get(1).last_model_id == "gpt-6-sol"
    # в каждом запуске — расход именно этого запуска, а не накопленный итог сессии
    assert (engine.state.get(1).total_input_tokens, engine.state.get(1).total_output_tokens) == (64000, 500)
    reactions = [item for item in bot.sent if item[0] == "react"]
    assert reactions[:2] == [("react", 1, ["👀"]), ("react", 1, ["👍"])]
    assert engine.state.get(1).session_id == "sess-123"
    assert bot.actions >= 1
    saved = StateStore(tmp_path / "state.json").get(1)
    assert saved.session_id == "sess-123"
    # лимиты подписки запомнены из журнала сессии
    assert engine.rate_limit_snapshots()["primary"]["info"]["used_percent"] == 91.0


def test_task_flow_creates_branch(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        state = engine.state.get(1)
        state.project = str(repo)
        state.session_id = "stale"
        await run_job(engine, Job(1, "add file", FakeMessage(bot, 1), kind="task", new_session=True, task_text="Добавить файл"))
        return engine

    engine = asyncio.run(go())
    state = engine.state.get(1)
    assert state.branch and state.branch.startswith("tg/") and state.branch.endswith("-dobavit-fayl")
    assert state.base_branch == "main"
    assert asyncio.run(git_tasks.current_branch(repo)) == state.branch
    answers = texts(bot, "answer")
    assert any("Создана ветка" in t for t in answers)
    sent = texts(bot, "send")
    assert sent[0].startswith("Готово: You are working in the git repository")
    assert "resume=none" in sent[0]  # задача всегда начинает новую сессию
    assert any("Ветка задачи" in t and "Коммитов в ветке пока нет" in t for t in sent)
    assert engine.state.get(1).session_id == "sess-123"


def test_task_in_non_git_project_is_rejected(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "x", FakeMessage(bot, 1), kind="task", new_session=True, task_text="x"))

    asyncio.run(go())
    assert any("Не удалось подготовить ветку" in t for t in texts(bot, "send"))
    assert not any(t.startswith("Готово") for t in texts(bot, "send"))


def test_stop_cancels_running_job_and_queue(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await engine.submit(Job(1, "sleep", FakeMessage(bot, 1)))
        await engine.submit(Job(1, "queued", FakeMessage(bot, 1, message_id=2)))
        await asyncio.sleep(0.8)
        running, dropped = engine.cancel(1)
        assert running and dropped == 1
        runtime = engine.runtime_info(1)
        await asyncio.wait_for(runtime.queue.join(), timeout=30)
        assert engine.cancel(1) == (False, 0)
        return engine

    engine = asyncio.run(go())
    assert any("Поставлено в очередь" in t for t in texts(bot, "answer"))
    assert any(t.startswith("⏹ Остановлено") for t in texts(bot, "edit"))
    assert not any("queued" in t for t in texts(bot, "send"))
    # сессия прерванного запуска сохранена: следующее сообщение продолжит диалог
    assert engine.state.get(1).session_id == "sess-123"


def test_resolve_project_stays_inside_workspace(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    engine = Engine(FakeBot(), cfg, StateStore(tmp_path / "state.json"))
    (cfg.workspace_dir / "proj").mkdir(parents=True)
    assert engine.resolve_project("proj") == cfg.workspace_dir / "proj"
    assert engine.resolve_project(".") == cfg.workspace_dir
    for bad in ("../", "/etc", "missing"):
        try:
            engine.resolve_project(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} должен быть отклонён")


def test_auth_error_gets_hint(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "nologin", FakeMessage(bot, 1)))

    asyncio.run(go())
    sent = texts(bot, "send")
    assert sent and sent[0].startswith("unexpected status 401 Unauthorized")
    reactions = [item for item in bot.sent if item[0] == "react"]
    assert reactions == [("react", 1, ["👀"]), ("react", 1, [])]  # при ошибке реакция снимается
    assert "codex-login.sh" in sent[0] and "codex login status" in sent[0]
    assert any(t.startswith("❌ Ошибка") for t in texts(bot, "edit"))


def test_missing_session_is_reset_and_retried(tmp_path, monkeypatch):
    """Журнал сессии пропал: бот начинает новую сессию, а не падает на каждом сообщении."""
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        engine.state.get(1).session_id = "gone-1"
        await run_job(engine, Job(1, "сделай коммит", FakeMessage(bot, 1)))
        return engine

    engine = asyncio.run(go())
    sent = texts(bot, "send")
    assert any("Готово: сделай коммит | resume=none" in t for t in sent)
    assert any("Начал новую сессию" in t for t in sent)
    assert engine.state.get(1).session_id == "sess-123"


def test_waiting_for_free_slot_is_announced(tmp_path, monkeypatch):
    """Пока слот занят другим запуском, бот сразу говорит об ожидании, а не молчит."""
    cfg = make_config(tmp_path, monkeypatch, MAX_CONCURRENT_RUNS="1")
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await engine.submit(Job(1, "sleep", FakeMessage(bot, 1)))
        await asyncio.sleep(0.6)
        await engine.submit(Job(2, "вопрос", FakeMessage(bot, 2)))
        await asyncio.sleep(0.6)
        waiting = engine.runtime_info(2)
        assert waiting is not None and waiting.waiting is True
        engine.cancel(1)
        engine.cancel(2)
        for key in (1, 2):
            await asyncio.wait_for(engine.runtime_info(key).queue.join(), timeout=30)

    asyncio.run(go())
    answers = texts(bot, "answer")
    assert any("Жду свободного слота" in t for t in answers)


def test_restart_cancel_is_labelled_and_explained(tmp_path, monkeypatch):
    """Перезапуск сервиса не должен выглядеть как чей-то /stop."""
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await engine.submit(Job(1, "sleep", FakeMessage(bot, 1)))
        await asyncio.sleep(0.8)
        await engine.shutdown()  # как при systemctl restart
        await asyncio.wait_for(engine.runtime_info(1).queue.join(), timeout=30)

    asyncio.run(go())
    assert any("Прервано перезапуском бота" in t for t in texts(bot, "edit"))
    assert any("перезапуск бота" in t and "отправьте его заново" in t.lower() for t in texts(bot, "send"))


def test_timeout_explains_what_happened(tmp_path, monkeypatch):
    """На таймауте пользователь должен понимать, что делать дальше."""
    cfg = make_config(tmp_path, monkeypatch)
    # Config — frozen dataclass, а минимум из .env равен 30 с: правим экземпляр,
    # чтобы тест не ждал полминуты (фиктивный codex на «sleep» висит 60 с).
    object.__setattr__(cfg, "timeout_seconds", 1)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "sleep", FakeMessage(bot, 1)))

    asyncio.run(go())
    sent = texts(bot, "send")
    assert any("Сработал предел на один запрос" in t for t in sent)
    assert any("файлы, которые Codex успел записать" in t for t in sent)
    assert any(t.startswith("⏱ Остановлено по таймауту") for t in texts(bot, "edit"))


def _compact_job(bot, message_id, instructions=None):
    extra = f"\n\nThe user asked to make sure the summary preserves: {instructions}" if instructions else ""
    return Job(1, COMPACT_PROMPT.format(extra=extra), FakeMessage(bot, 1, message_id=message_id), kind="compact",
               instructions=instructions, task_text="/compact")


def test_compact_native_keeps_session(tmp_path, monkeypatch):
    """/compact без пожеланий: сжатие средствами Codex (app-server), сессия та же."""
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        await run_job(engine, _compact_job(bot, 2))
        await run_job(engine, Job(1, "дальше", FakeMessage(bot, 1, message_id=3)))
        return engine

    engine = asyncio.run(go())
    sent = texts(bot, "send")
    assert any(t == "🗜 История сжата средствами Codex: контекст 32.2k → 5.0k токенов из 258.4k. Диалог продолжается в той же сессии." for t in sent)
    assert any(t.startswith("✅ Готово") and "контекст 32.2k → 5.0k" in t for t in texts(bot, "edit"))
    assert any(t == "Готово: дальше | resume=sess-123" for t in sent)  # та же сессия
    assert len(codex_calls(tmp_path)) == 2  # конспект моделью не писали
    messages = [m for m in app_server_log(tmp_path) if isinstance(m, dict)]
    resume = next(m for m in messages if m.get("method") == "thread/resume")
    assert resume["params"]["threadId"] == "sess-123"
    assert "Telegram bot" in resume["params"]["developerInstructions"]  # иначе после сжатия инструкции потеряются
    assert any(m.get("method") == "thread/compact/start" for m in messages)
    state = engine.state.get(1)
    assert state.session_id == "sess-123" and state.compact_summary is None


def test_compact_with_instructions_uses_summary(tmp_path, monkeypatch):
    """/compact что сохранить: конспект текущей сессии, затем новая сессия с конспектом впереди."""
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        await run_job(engine, _compact_job(bot, 2, instructions="список файлов"))
        state = engine.state.get(1)
        assert state.session_id is None
        assert state.compact_summary and state.compact_summary.startswith("Готово: The conversation so far")
        await run_job(engine, Job(1, "дальше", FakeMessage(bot, 1, message_id=3)))
        return engine

    engine = asyncio.run(go())
    sent = texts(bot, "send")
    assert any(t.startswith("🗜 История сжата. Следующее сообщение начнёт новую сессию") for t in sent)
    assert app_server_log(tmp_path) == []  # app-server не нужен
    calls = codex_calls(tmp_path)
    assert calls[1][:2] == ["exec", "resume"]          # конспект пишет прежняя сессия
    assert calls[2][:2] == ["exec", "--json"]          # после сжатия — новая сессия
    assert "список файлов" in codex_prompts(tmp_path)[1]
    assert codex_prompts(tmp_path)[2].startswith("[Summary of the earlier part of this conversation")
    state = engine.state.get(1)
    assert state.compact_summary is None and state.session_id == "sess-123"


def test_compact_native_failure_falls_back_to_summary(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch, FAKE_APP_SERVER="fail")
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        await run_job(engine, _compact_job(bot, 2))
        return engine

    engine = asyncio.run(go())
    sent = texts(bot, "send")
    assert any(t.startswith("ℹ️ Сжатие средствами Codex не удалось (remote compaction failed)") and "🗜 История сжата" in t for t in sent)
    assert engine.state.get(1).compact_summary and engine.state.get(1).session_id is None


def test_compact_native_crash_falls_back_to_summary(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch, FAKE_APP_SERVER="crash")
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        await run_job(engine, _compact_job(bot, 2))
        return engine

    engine = asyncio.run(go())
    assert any("app-server завершился" in t for t in texts(bot, "send"))
    assert engine.state.get(1).compact_summary


def test_compact_with_missing_journal(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        engine.state.get(1).session_id = "gone-2"
        await run_job(engine, _compact_job(bot, 1))
        return engine

    engine = asyncio.run(go())
    assert codex_calls(tmp_path) == []  # ни конспекта, ни новой сессии
    state = engine.state.get(1)
    assert state.session_id is None and state.compact_summary is None
    assert any("Журнал этой сессии на сервере не найден" in t for t in texts(bot, "send"))


def test_image_is_attached(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"\x89PNG")

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "что на картинке?", FakeMessage(bot, 1), images=[str(picture)]))

    asyncio.run(go())
    call = codex_calls(tmp_path)[0]
    assert call[call.index("--image") + 1] == str(picture)


def test_silent_new_thread_on_resume_is_reported(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        monkeypatch.setenv("FAKE_NEW_THREAD", "1")
        await run_job(engine, Job(1, "again", FakeMessage(bot, 1, message_id=2)))
        return engine

    engine = asyncio.run(go())
    sent = texts(bot, "send")
    assert any("Начал новую сессию" in t for t in sent)
    assert engine.state.get(1).session_id == "sess-new"
    # расход новой сессии не уменьшается на итог прежней
    assert (engine.state.get(1).total_input_tokens, engine.state.get(1).total_output_tokens) == (64000, 500)


def test_forget_stops_running_codex(tmp_path, monkeypatch):
    """/forget посреди запуска: процесс codex не должен остаться жить без присмотра."""
    import subprocess

    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()
    marker = str(tmp_path / "fake-codex")

    def alive() -> bool:
        out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
        return bool(out.strip())

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await engine.submit(Job(1, "sleep", FakeMessage(bot, 1)))
        await asyncio.sleep(0.8)
        assert alive()
        await engine.forget_conversation("1")
        await asyncio.sleep(0.3)
        return alive()

    assert asyncio.run(go()) is False


def test_timed_out_run_mentioning_missing_session_keeps_session(tmp_path, monkeypatch):
    """Таймаут, а в последней реплике модели «session not found» — сессию не выбрасываем."""
    cfg = make_config(tmp_path, monkeypatch)
    object.__setattr__(cfg, "timeout_seconds", 1)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        await run_job(engine, Job(1, "sleep-notfound", FakeMessage(bot, 1, message_id=2)))
        return engine

    engine = asyncio.run(go())
    assert engine.state.get(1).session_id == "sess-123"
    sent = texts(bot, "send")
    assert any("Сработал предел на один запрос" in t for t in sent)
    assert not any("Начал новую сессию" in t for t in sent)
    assert len(codex_calls(tmp_path)) == 2  # без повторного запуска


def test_compact_summary_drops_file_markers(tmp_path, monkeypatch):
    """Строки TG_SEND_FILE из конспекта не должны попасть в первый запрос новой сессии."""
    cfg = make_config(tmp_path, monkeypatch)
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        original = engine.runner.run

        async def with_marker(*args, **kwargs):
            result = await original(*args, **kwargs)
            result.text += "\nTG_SEND_FILE: summary.pdf"
            return result

        engine.runner.run = with_marker
        await run_job(engine, _compact_job(bot, 2, instructions="всё"))
        return engine

    engine = asyncio.run(go())
    summary = engine.state.get(1).compact_summary
    assert summary and "TG_SEND_FILE" not in summary


def test_forget_uses_codex_delete(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, monkeypatch, WORKSPACE_PER_CHAT="true")
    bot = FakeBot()

    async def go():
        engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
        await engine.startup()
        await run_job(engine, Job(1, "hello", FakeMessage(bot, 1)))
        return await engine.forget_conversation("1")

    summary = asyncio.run(go())
    assert summary["transcripts"] == 1 and summary["state"]
    assert (tmp_path / "codex-args.jsonl.delete").read_text().split() == ["sess-123"]
    assert not list((tmp_path / "codex-home").rglob("rollout-*sess-123*"))
