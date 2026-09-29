"""Прогон настоящих aiogram-обновлений через Dispatcher с подменённой сессией Telegram."""

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from helpers import codex_prompts, make_config, make_repo

from codex_telegram_bot.bot import AccessMiddleware, Engine, bot_commands, build_router
from codex_telegram_bot.state import StateStore


class MockedSession(BaseSession):
    """Записывает вызовы Bot API и возвращает правдоподобные ответы без сети."""

    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._next_id = 1000

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: Optional[int] = None) -> Any:
        self.calls.append(method)
        name = type(method).__name__
        if name in {"SendMessage", "EditMessageText"}:
            self._next_id += 1
            # Настоящая сессия валидирует ответ с context={"bot": bot}, что привязывает
            # объект к боту; воспроизводим это через as_(bot).
            return Message(
                message_id=self._next_id,
                date=datetime.now(timezone.utc),
                chat=Chat(id=getattr(method, "chat_id", 1), type="private"),
                text=getattr(method, "text", None),
            ).as_(bot)
        if name == "GetMe":
            return User(id=42, is_bot=True, first_name="bot", username="testbot")
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True) -> AsyncGenerator[bytes, None]:  # noqa: D401
        yield b""

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.calls if type(m).__name__ in {"SendMessage", "EditMessageText"}]


def make_update(
    update_id: int,
    text: str,
    user_id: int = 1,
    chat_id: int = 1,
    message_id: int = 1,
    chat_type: str = "private",
    title: Optional[str] = None,
    thread_id: Optional[int] = None,
    reply_to_bot: bool = False,
) -> Update:
    chat = Chat(id=chat_id, type=chat_type, title=title)
    reply = None
    if reply_to_bot:
        reply = Message(
            message_id=500,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=User(id=42, is_bot=True, first_name="bot", username="testbot"),
            text="сообщение бота",
        )
    return Update(
        update_id=update_id,
        message=Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=User(id=user_id, is_bot=False, first_name="Пользователь"),
            text=text,
            message_thread_id=thread_id,
            is_topic_message=True if thread_id else None,
            reply_to_message=reply,
        ),
    )


def build(tmp_path, monkeypatch, **overrides):
    cfg = make_config(tmp_path, monkeypatch, **overrides)
    session = MockedSession()
    bot = Bot(cfg.telegram_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    engine = Engine(bot, cfg, StateStore(tmp_path / "state.json"))
    dp = Dispatcher()
    access = AccessMiddleware(cfg.allowed_user_ids, cfg.allowed_chat_ids, cfg.allow_private_chats, cfg.team_chat_ids)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(build_router(engine))
    return cfg, session, bot, engine, dp


async def wait_done(engine: Engine, key) -> None:
    runtime = engine.runtime_info(key)
    assert runtime is not None, f"для {key!r} не создана очередь"
    await asyncio.wait_for(runtime.queue.join(), timeout=30)


def test_commands_and_auth(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)
    repo = make_repo(tmp_path)

    async def go():
        await engine.startup()
        assert len(bot_commands()) >= 10
        await dp.feed_update(bot, make_update(1, "/start"))
        await dp.feed_update(bot, make_update(2, "/status"))
        await dp.feed_update(bot, make_update(3, "/projects"))
        await dp.feed_update(bot, make_update(4, f"/project {repo}"))
        await dp.feed_update(bot, make_update(5, "/model gpt-6-astra"))
        await dp.feed_update(bot, make_update(6, "/model не модель"))
        await dp.feed_update(bot, make_update(60, "/effort high"))
        await dp.feed_update(bot, make_update(61, "/effort turbo"))
        await dp.feed_update(bot, make_update(7, "/task"))
        await dp.feed_update(bot, make_update(8, "/unknowncmd"))
        await dp.feed_update(bot, make_update(9, "/sh echo hello-from-shell"))
        # чужой пользователь: на /id получает свой ID, обычный текст игнорируется
        await dp.feed_update(bot, make_update(10, "/id", user_id=999, chat_id=999))
        await dp.feed_update(bot, make_update(11, "привет", user_id=999, chat_id=999))
        # обычный текст от разрешённого пользователя уходит в codex
        await dp.feed_update(bot, make_update(12, "привет, codex", message_id=12))
        runtime = engine.runtime_info(1)
        await asyncio.wait_for(runtime.queue.join(), timeout=30)
        await dp.feed_update(bot, make_update(13, "/stop"))
        await dp.feed_update(bot, make_update(15, "/status"))
        await dp.feed_update(bot, make_update(14, "/new"))

    asyncio.run(go())
    texts = session.sent_texts()
    assert any("Codex в Telegram" in t for t in texts)                             # /start
    assert any("Codex: codex-cli 9.9.9" in t for t in texts)                        # /status
    assert any("вход через аккаунт ChatGPT" in t for t in texts)                    # /status: авторизация
    assert any("последний запуск: <code>gpt-6-astra</code>" in t for t in texts)    # /status после запуска
    assert any("Токенов за этот разговор: ↑32.0k ↓250" in t for t in texts)
    assert any("Workspace:" in t for t in texts)                                    # /projects
    assert any("Каталог вне workspace" in t for t in texts)                         # /project с абсолютным путём вне workspace
    assert any("Модель: <code>gpt-6-astra</code>" in t for t in texts)              # /model gpt-6-astra
    assert any("Неизвестная модель" in t for t in texts)                            # /model nonsense
    assert any("Использование: /task" in t for t in texts)                          # /task без текста
    assert any("Неизвестная команда" in t for t in texts)                           # /unknowncmd
    assert any("hello-from-shell" in t and t.startswith("<pre>") for t in texts)   # /sh
    assert any("Ваш Telegram ID: <code>999</code>" in t and "Доступ запрещён" in t for t in texts)
    assert any(t == "Готово: привет, codex | resume=none" for t in texts)           # ответ codex
    assert any(t.startswith("✅ Готово") for t in texts)
    assert any("ничего не выполняется" in t for t in texts)                         # /stop без задач
    assert any("Сессия сброшена" in t for t in texts)                               # /new
    # чужой текст не породил ни запуска, ни ответа
    assert engine.runtime_info(999) is None
    assert engine.state.get(1).model == "gpt-6-astra"
    assert engine.state.get(1).effort == "high"
    assert any("Рассуждения: <code>high</code>" in t for t in texts)
    assert any("Неизвестный уровень" in t for t in texts)
    assert engine.state.get(1).session_id is None
    # ответ codex отправлен как reply на исходное сообщение
    replies = [m for m in session.calls if type(m).__name__ == "SendMessage" and m.reply_parameters is not None]
    assert replies and replies[0].reply_parameters.message_id == 12


def test_task_command_through_dispatcher(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)
    repo = make_repo(cfg.workspace_dir, name="proj")

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "/project proj"))
        await dp.feed_update(bot, make_update(2, "/task Добавить тесты"))
        runtime = engine.runtime_info(1)
        await asyncio.wait_for(runtime.queue.join(), timeout=30)

    asyncio.run(go())
    texts = session.sent_texts()
    assert any("Создана ветка <code>tg/" in t for t in texts)
    assert any("Ветка задачи" in t for t in texts)
    assert engine.state.get(1).project == str(repo)
    assert engine.state.get(1).branch.startswith("tg/")


def test_group_chat_basic(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)
    group = -1001234567890

    async def go():
        await engine.startup()
        assert engine.bot_username == "testbot" and engine.bot_id == 42
        await dp.feed_update(bot, make_update(1, "/id", chat_id=group, chat_type="supergroup", title="Команда"))
        await dp.feed_update(bot, make_update(2, "/status@testbot", chat_id=group, chat_type="supergroup", title="Команда"))
        await dp.feed_update(bot, make_update(3, "/weather@otherbot", chat_id=group, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(4, "/weather", chat_id=group, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(50, "/t это людям, не боту", chat_id=group, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(51, "/t@testbot и так тоже", chat_id=group, chat_type="supergroup"))
        assert engine.runtime_info(group) is None
        await dp.feed_update(bot, make_update(5, "привет из группы", chat_id=group, chat_type="supergroup", message_id=5))
        await wait_done(engine, group)
        await dp.feed_update(bot, make_update(6, "@TestBot ещё раз", chat_id=group, chat_type="supergroup", message_id=6))
        await wait_done(engine, group)
        await dp.feed_update(bot, make_update(7, "/id", user_id=777, chat_id=group, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(8, "чужой текст", user_id=777, chat_id=group, chat_type="supergroup"))

    asyncio.run(go())
    texts = session.sent_texts()
    assert any(f"ID чата: <code>{group}</code> (супергруппа)" in t and f"ALLOWED_CHAT_IDS={group}" in t for t in texts)
    assert any("💬 Чат: Команда" in t for t in texts)
    assert not any("Неизвестная команда" in t for t in texts)
    assert any(t == "Готово: привет из группы | resume=none" for t in texts)
    assert any(t == "Готово: ещё раз | resume=sess-123" for t in texts)
    assert any("Доступ запрещён" in t and "<code>777</code>" in t for t in texts)
    assert not any("чужой текст" in t for t in texts)
    assert not any("людям" in t or "так тоже" in t or "Неизвестная команда" in t for t in texts)
    reactions = [m for m in session.calls if type(m).__name__ == "SetMessageReaction"]
    assert [(m.message_id, [r.emoji for r in (m.reaction or [])]) for m in reactions][:2] == [(5, ["👀"]), (5, ["👍"])]


def test_group_requires_mention(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, GROUP_REQUIRE_MENTION="true")
    group = -100777

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "просто болтаем", chat_id=group, chat_type="supergroup"))
        assert engine.runtime_info(group) is None
        await dp.feed_update(bot, make_update(2, "/ask вопрос боту", chat_id=group, chat_type="supergroup", message_id=2))
        await wait_done(engine, group)
        await dp.feed_update(bot, make_update(3, "а это ответом", chat_id=group, chat_type="supergroup", message_id=3, reply_to_bot=True))
        await wait_done(engine, group)
        await dp.feed_update(bot, make_update(4, "@testbot и с упоминанием", chat_id=group, chat_type="supergroup", message_id=4))
        await wait_done(engine, group)

    asyncio.run(go())
    texts = session.sent_texts()
    assert not any("просто болтаем" in t for t in texts)
    assert any(t.startswith("Готово: вопрос боту") for t in texts)
    assert any(t.startswith("Готово: а это ответом") for t in texts)
    assert any(t.startswith("Готово: и с упоминанием") for t in texts)


def test_private_disabled_and_chat_whitelist(tmp_path, monkeypatch):
    group = -100555
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, ALLOW_PRIVATE_CHATS="false", ALLOWED_CHAT_IDS=str(group))

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "привет в личку"))
        await dp.feed_update(bot, make_update(2, "/id"))
        await dp.feed_update(bot, make_update(3, "привет", chat_id=-100999, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(4, "/id", chat_id=-100999, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(5, "привет в группе", chat_id=group, chat_type="supergroup", message_id=5))
        await wait_done(engine, group)

    asyncio.run(go())
    texts = session.sent_texts()
    assert engine.runtime_info(1) is None and engine.runtime_info(-100999) is None
    assert any("только в группе" in t for t in texts)
    assert any("не в списке ALLOWED_CHAT_IDS" in t and "-100999" in t for t in texts)
    assert any(t == "Готово: привет в группе | resume=none" for t in texts)


def test_forum_topics_are_separate_conversations(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)
    group = -100321

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "/model gpt-6-astra", chat_id=group, chat_type="supergroup", thread_id=7))
        await dp.feed_update(bot, make_update(2, "/model gpt-6-luna", chat_id=group, chat_type="supergroup", thread_id=9))
        await dp.feed_update(bot, make_update(3, "в теме 7", chat_id=group, chat_type="supergroup", thread_id=7, message_id=3))
        await wait_done(engine, f"{group}:7")

    asyncio.run(go())
    assert engine.state.get(f"{group}:7").model == "gpt-6-astra"
    assert engine.state.get(f"{group}:9").model == "gpt-6-luna"
    assert engine.state.get(f"{group}:7").session_id == "sess-123"
    assert engine.state.get(str(group)).session_id is None
    sends = [m for m in session.calls if type(m).__name__ == "SendMessage"]
    assert any(m.message_thread_id == 7 and m.text.startswith("Готово: в теме 7") for m in sends)
    assert any(m.message_thread_id == 7 and m.text.startswith("⏳") for m in sends)
    assert all(m.message_thread_id in (7, 9) for m in sends)


def make_callback(update_id: int, data: str, user_id: int = 1, chat_id: int = 1, chat_type: str = "private") -> Update:
    chat = Chat(id=chat_id, type=chat_type)
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=User(id=user_id, is_bot=False, first_name="Пользователь"),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=700,
                date=datetime.now(timezone.utc),
                chat=chat,
                from_user=User(id=42, is_bot=True, first_name="bot", username="testbot"),
                text="🧠 Текущая модель",
            ),
        ),
    )


def test_model_and_effort_buttons(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "/model"))
        await dp.feed_update(bot, make_callback(2, "model:gpt-6-astra"))
        await dp.feed_update(bot, make_callback(3, "effort:high"))
        await dp.feed_update(bot, make_callback(4, "model:не модель"))
        await dp.feed_update(bot, make_callback(5, "model:gpt-6-sol", user_id=999))  # чужой — отклоняется
        await dp.feed_update(bot, make_callback(6, "effort:default"))

    asyncio.run(go())
    sends = [m for m in session.calls if type(m).__name__ == "SendMessage"]
    keyboard = [m for m in sends if m.reply_markup is not None]
    assert keyboard, "/model без аргумента должен прислать кнопки"
    labels = [b.text for row in keyboard[0].reply_markup.inline_keyboard for b in row]
    # на кнопках — точные имена моделей, по одной на семейство
    assert labels == ["✅ По умолчанию", "GPT-6 Astra", "GPT-6 Sol", "GPT-6 Luna"]
    assert [b.callback_data for row in keyboard[0].reply_markup.inline_keyboard for b in row if b.text == "GPT-6 Sol"] == ["model:gpt-6-sol"]
    answers = [m for m in session.calls if type(m).__name__ == "AnswerCallbackQuery"]
    assert [a.text for a in answers] == ["Модель: gpt-6-astra", "Рассуждения: high", "Неизвестная модель", "⛔ Нет доступа", "Рассуждения: по умолчанию"]
    edits = [m for m in session.calls if type(m).__name__ == "EditMessageText"]
    assert any("Модель: <code>gpt-6-astra</code>" in m.text for m in edits)
    marked = [b.text for row in edits[0].reply_markup.inline_keyboard for b in row]
    assert "✅ GPT-6 Astra" in marked
    assert engine.state.get(1).model == "gpt-6-astra"
    assert engine.state.get(1).effort is None


def test_two_owners_share_group_and_have_own_private_chats(tmp_path, monkeypatch):
    """Два ID в ALLOWED_USER_IDS (Фёдор и друг): оба владельцы, общая группа — один разговор."""
    group = -1004444444444
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, ALLOWED_USER_IDS="1, 2", ALLOWED_CHAT_IDS=str(group))

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "вопрос от первого", user_id=1, chat_id=group, chat_type="supergroup", message_id=1))
        await wait_done(engine, group)
        await dp.feed_update(bot, make_update(2, "вопрос от второго", user_id=2, chat_id=group, chat_type="supergroup", message_id=2))
        await wait_done(engine, group)
        await dp.feed_update(bot, make_update(3, "личка второго", user_id=2, chat_id=2, message_id=3))
        await wait_done(engine, 2)
        await dp.feed_update(bot, make_update(4, "/sh echo second-owner-shell", user_id=2, chat_id=group, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(5, "чужой текст", user_id=777, chat_id=group, chat_type="supergroup"))

    asyncio.run(go())
    texts = session.sent_texts()
    assert any(t == "Готово: вопрос от первого | resume=none" for t in texts)
    assert any(t == "Готово: вопрос от второго | resume=sess-123" for t in texts)  # та же сессия группы
    assert any(t == "Готово: личка второго | resume=none" for t in texts)          # в личке — свой разговор
    assert any("second-owner-shell" in t for t in texts)
    assert not any("чужой текст" in t for t in texts)


def test_team_chat_admits_any_member(tmp_path, monkeypatch):
    personal, team = -100111, -100222
    cfg, session, bot, engine, dp = build(
        tmp_path, monkeypatch, ALLOWED_CHAT_IDS=str(personal), TEAM_CHAT_IDS=str(team), ALLOW_PRIVATE_CHATS="false"
    )

    async def go():
        await engine.startup()
        # коллега (555) не в белом списке: в командной группе работает
        await dp.feed_update(bot, make_update(1, "привет от коллеги", user_id=555, chat_id=team, chat_type="supergroup", message_id=1))
        await wait_done(engine, team)
        await dp.feed_update(bot, make_callback(2, "model:gpt-6-luna", user_id=555, chat_id=team, chat_type="supergroup"))
        # …а в личке и в личной группе владельца — нет
        await dp.feed_update(bot, make_update(3, "привет в личку", user_id=555, chat_id=555))
        await dp.feed_update(bot, make_update(4, "пробрался", user_id=555, chat_id=personal, chat_type="supergroup"))
        await dp.feed_update(bot, make_callback(5, "model:gpt-6-astra", user_id=555, chat_id=personal, chat_type="supergroup"))
        # владелец работает в обеих группах
        await dp.feed_update(bot, make_update(6, "личная группа", chat_id=personal, chat_type="supergroup", message_id=6))
        await wait_done(engine, personal)

    asyncio.run(go())
    texts = session.sent_texts()
    assert any(t == "Готово: привет от коллеги | resume=none" for t in texts)
    assert engine.state.get(team).model == "gpt-6-luna"
    assert engine.runtime_info(555) is None
    assert not any("пробрался" in t for t in texts)
    assert engine.state.get(personal).model is None
    answers = [m.text for m in session.calls if type(m).__name__ == "AnswerCallbackQuery"]
    assert answers == ["Модель: gpt-6-luna", "⛔ Нет доступа"]
    assert any(t == "Готово: личная группа | resume=none" for t in texts)


def test_workspace_per_chat_isolates_projects(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, WORKSPACE_PER_CHAT="true")
    group = -100333
    ws_group = engine.workspace_for(group)
    ws_private = engine.workspace_for(1)
    assert ws_group != ws_private and ws_group.is_dir() and ws_private.is_dir()
    assert ws_group.parent == cfg.workspace_dir and ws_group.name == "chat_m100333"
    (ws_group / "repo").mkdir()
    assert engine.resolve_project("repo", group) == ws_group / "repo"
    try:
        engine.resolve_project("repo", 1)
    except ValueError:
        pass
    else:
        raise AssertionError("проект другого чата не должен быть виден")

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "/projects", chat_id=group, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(2, "/projects"))

    asyncio.run(go())
    texts = session.sent_texts()
    assert any("chat_m100333" in t and "<code>repo</code>" in t for t in texts)
    assert any("chat_1" in t and "(пусто" in t for t in texts)


def test_quota_command_is_hidden_and_owner_only(tmp_path, monkeypatch):
    from codex_telegram_bot.bot import bot_commands
    from codex_telegram_bot.state import StateStore

    team = -100444
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, TEAM_CHAT_IDS=str(team), TIMEZONE="Europe/Moscow")
    assert all(c.command not in {"quota", "limits"} for c in bot_commands())

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "/quota"))                      # до запусков — нет данных
        await dp.feed_update(bot, make_update(2, "привет", message_id=2))        # запуск пишет лимиты в журнал сессии
        await wait_done(engine, 1)
        await dp.feed_update(bot, make_update(3, "/quota"))
        await dp.feed_update(bot, make_update(4, "/quota", user_id=555, chat_id=team, chat_type="supergroup"))  # коллега — молчание
        await dp.feed_update(bot, make_update(5, "/limits"))

    asyncio.run(go())
    replies = [t for t in session.sent_texts() if t.startswith("⏳ <b>Лимиты подписки</b>")]
    assert len(replies) == 3
    assert "Пока нет данных" in replies[0]
    assert "5-часовое окно: использовано 91%, обновление через 30м (" in replies[1] and "MSK)" in replies[1]
    assert "лимит близок к исчерпанию" in replies[1] and "данные 0м назад" in replies[1]
    assert "недельное окно: использовано 40%, обновление через 3д 0ч" in replies[1]
    assert "план: plus" in replies[1]
    assert "primary" in StateStore(tmp_path / "state.json").meta.get("rate_limits", {})


def test_ultracode_is_owner_only(tmp_path, monkeypatch):
    team = -100555
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, TEAM_CHAT_IDS=str(team))

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "/effort ultra", user_id=555, chat_id=team, chat_type="supergroup"))
        await dp.feed_update(bot, make_callback(2, "effort:ultra", user_id=555, chat_id=team, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(3, "/effort high", user_id=555, chat_id=team, chat_type="supergroup"))
        await dp.feed_update(bot, make_update(4, "/effort ultracode", chat_id=team, chat_type="supergroup"))

    asyncio.run(go())
    texts = session.sent_texts()
    assert engine.state.get(team).effort == "ultra"
    assert texts == ["🎯 Рассуждения: <code>high</code>", "🎯 Рассуждения: <code>ultra</code>"]
    answers = [m.text for m in session.calls if type(m).__name__ == "AnswerCallbackQuery"]
    assert answers == ["⛔ Нет доступа"]


def test_effort_buttons_hide_ultra(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "/effort"))

    asyncio.run(go())
    sends = [m for m in session.calls if type(m).__name__ == "SendMessage" and m.reply_markup is not None]
    labels = [b.text for row in sends[0].reply_markup.inline_keyboard for b in row]
    assert labels == ["✅ По умолчанию", "low", "medium", "high", "xhigh", "max"]


def test_context_and_compact_commands(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "/compact"))                # сессии ещё нет
        await dp.feed_update(bot, make_update(2, "/context"))                # без сессии
        await dp.feed_update(bot, make_update(3, "привет", message_id=3))
        await wait_done(engine, 1)
        await dp.feed_update(bot, make_update(4, "/context"))                # с сессией sess-123
        await dp.feed_update(bot, make_update(5, "/compact сохрани список файлов", message_id=5))
        await wait_done(engine, 1)
        await dp.feed_update(bot, make_update(6, "/context"))                # после сжатия
        await dp.feed_update(bot, make_update(7, "/status"))

    asyncio.run(go())
    texts = session.sent_texts()
    assert any("Сжимать нечего" in t for t in texts)
    context_replies = [t for t in texts if t.startswith("📐")]
    assert len(context_replies) == 3
    assert "нового диалога" in context_replies[0] and "после первого ответа" in context_replies[0]
    assert "текущего диалога" in context_replies[1]
    assert "<b>Модель:</b> gpt-6-sol" in context_replies[1]
    assert "из 258.4k токенов (12%)" in context_replies[1]
    assert "<b>Сообщений в сессии:</b> 1" in context_replies[1]
    assert "История сжата" in context_replies[2]
    compacted = [t for t in texts if t.startswith("🗜 История сжата")]
    assert compacted
    prompts = codex_prompts(tmp_path)
    assert prompts[1].startswith("The conversation so far is about to be compacted")
    assert "сохрани список файлов" in prompts[1]
    assert engine.state.get(1).session_id is None and engine.state.get(1).compact_summary
    assert any("начнётся с конспекта после /compact" in t for t in texts)


def test_context_with_missing_journal(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)

    async def go():
        await engine.startup()
        engine.state.get(1).session_id = "gone"
        await dp.feed_update(bot, make_update(1, "/context"))

    asyncio.run(go())
    assert any(t.startswith("📐 Контекст текущего диалога") and "не найден" in t for t in session.sent_texts())


def test_forget_and_conversations(tmp_path, monkeypatch):
    team = -100666
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, WORKSPACE_PER_CHAT="true", TEAM_CHAT_IDS=str(team))
    codex_home = tmp_path / "codex-home"
    from codex_telegram_bot.bot import bot_commands
    assert any(c.command == "forget" for c in bot_commands())
    assert all(c.command != "conversations" for c in bot_commands())

    async def go():
        await engine.startup()
        # разговор в теме 7 командной группы: запуск создаёт сессию (и её журнал) и каталог с «репозиторием»
        await dp.feed_update(bot, make_update(1, "привет", chat_id=team, chat_type="supergroup", thread_id=7, message_id=1))
        await wait_done(engine, f"{team}:7")
        key = f"{team}:7"
        ws = engine.workspace_for(key)
        (ws / "repo").mkdir()
        (ws / "repo" / "big.bin").write_bytes(b"x" * 2048)
        archived = codex_home / "archived_sessions"
        archived.mkdir(parents=True)
        (archived / "rollout-2026-09-28T10-00-00-sess-123.jsonl").write_text("{}")
        # коллега не может забыть ЧУЖОЙ разговор и не видит список
        await dp.feed_update(bot, make_update(2, "/forget -100777", user_id=555, chat_id=team, chat_type="supergroup", thread_id=7))
        await dp.feed_update(bot, make_update(3, "/conversations", user_id=555, chat_id=team, chat_type="supergroup"))
        await dp.feed_update(bot, make_callback(4, "forget:-100777", user_id=555, chat_id=team, chat_type="supergroup"))
        # владелец: список, запрос, отмена, подтверждение
        await dp.feed_update(bot, make_update(5, "/conversations"))
        await dp.feed_update(bot, make_update(6, "/forget", chat_id=team, chat_type="supergroup", thread_id=7))
        await dp.feed_update(bot, make_callback(7, "forget:cancel"))
        await dp.feed_update(bot, make_callback(8, f"forget:{key}"))
        await dp.feed_update(bot, make_update(9, "/forget nope:1"))
        return key, ws

    key, ws = asyncio.run(go())
    texts = session.sent_texts()
    assert any("🗂 <b>Разговоры</b>" in t and key in t and "2.0 КБ" in t for t in texts)
    assert any(t.startswith("🗑 Забыть разговор") and "2.0 КБ" in t for t in texts)
    assert any(t == "Удаление отменено." for t in texts)
    assert any(t.startswith(f"✅ Разговор <code>{key}</code> забыт.") and "2.0 КБ" in t and "журнала сессии: 2" in t for t in texts)
    assert any("боту неизвестен" in t for t in texts)
    assert not ws.exists()
    assert not list(codex_home.rglob("rollout-*sess-123*"))
    assert key not in engine.state.all()
    assert engine.runtime_info(key) is None
    answers = [m.text for m in session.calls if type(m).__name__ == "AnswerCallbackQuery"]
    assert answers == ["⛔ Чужой разговор может забыть только владелец", "Отменено", "Готово"]
    # у коллеги ни одного ответа на /forget и /conversations
    assert sum(1 for t in texts if "Разговоры" in t) == 1
    assert sum(1 for t in texts if t.startswith("🗑 Забыть разговор")) == 1
    assert any("только текущий разговор" in t for t in texts)  # коллеге отказали по ключу


def test_photo_is_saved_and_attached(tmp_path, monkeypatch):
    """Фото сохраняется на сервере, а Codex получает и путь, и саму картинку (--image)."""
    from aiogram.types import PhotoSize

    from helpers import codex_calls

    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, is_bot=False, first_name="Пользователь"),
            caption="что тут?",
            photo=[PhotoSize(file_id="f1", file_unique_id="u1", width=10, height=10)],
        ),
    )

    async def fake_download(file, destination=None, **kwargs):
        with open(destination, "wb") as handle:
            handle.write(b"\x89PNG")

    async def go():
        await engine.startup()
        bot.download = fake_download
        await dp.feed_update(bot, update)
        await wait_done(engine, 1)

    asyncio.run(go())
    call = codex_calls(tmp_path)[0]
    image = call[call.index("--image") + 1]
    assert image.startswith(str(cfg.uploads_dir)) and image.endswith(".jpg")
    assert any(t.startswith("Готово: что тут?") for t in session.sent_texts())


def test_shell_is_owner_only(tmp_path, monkeypatch):
    """/sh и /git — только для ALLOWED_USER_IDS, даже в командном чате."""
    group = -1002222222222
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, TEAM_CHAT_IDS=str(group))

    async def go():
        await engine.startup()
        # участник команды (не владелец): /sh молча игнорируется
        await dp.feed_update(
            bot, make_update(1, "/sh echo secret-from-team", user_id=777, chat_id=group, chat_type="supergroup")
        )
        await dp.feed_update(
            bot, make_update(2, "/git status", user_id=777, chat_id=group, chat_type="supergroup")
        )
        # владелец: /sh работает
        await dp.feed_update(
            bot, make_update(3, "/sh echo owner-shell-ok", user_id=1, chat_id=group, chat_type="supergroup")
        )

    asyncio.run(go())
    texts = session.sent_texts()
    assert not any("secret-from-team" in t for t in texts)
    assert any("owner-shell-ok" in t for t in texts)


def test_forget_own_conversation_allowed_for_team_member(tmp_path, monkeypatch):
    """Участник командного чата может забыть свой разговор, но не чужой по ключу."""
    group = -1003333333333
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, TEAM_CHAT_IDS=str(group))

    async def go():
        await engine.startup()
        engine.state.get(str(group)).session_id = "sess-123"
        engine.state.save()
        # не владелец (777) просит забыть текущий разговор — получает подтверждение
        await dp.feed_update(bot, make_update(1, "/forget", user_id=777, chat_id=group, chat_type="supergroup"))
        # и подтверждает кнопкой
        await dp.feed_update(bot, make_callback(2, f"forget:{group}", user_id=777, chat_id=group, chat_type="supergroup"))
        # чужой разговор по ключу ему недоступен
        await dp.feed_update(bot, make_update(3, "/forget -100999", user_id=777, chat_id=group, chat_type="supergroup"))

    asyncio.run(go())
    texts = session.sent_texts()
    assert any("Забыть разговор" in t for t in texts)
    assert any("забыт" in t for t in texts)
    assert any("только текущий разговор" in t for t in texts)


def test_forget_foreign_key_still_owner_only(tmp_path, monkeypatch):
    """Кнопку подтверждения для чужого разговора не-владелец нажать не может."""
    group = -1004444444444
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch, TEAM_CHAT_IDS=str(group))

    async def go():
        await engine.startup()
        engine.state.get("-100999").session_id = "sess-123"
        engine.state.save()
        await dp.feed_update(bot, make_callback(1, "forget:-100999", user_id=777, chat_id=group, chat_type="supergroup"))

    asyncio.run(go())
    assert engine.state.all().get("-100999") is not None  # разговор не тронут


def test_plain_compact_uses_codex_compaction(tmp_path, monkeypatch):
    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)

    async def go():
        await engine.startup()
        await dp.feed_update(bot, make_update(1, "привет", message_id=1))
        await wait_done(engine, 1)
        await dp.feed_update(bot, make_update(2, "/compact", message_id=2))
        await wait_done(engine, 1)
        await dp.feed_update(bot, make_update(3, "/context"))

    asyncio.run(go())
    texts = session.sent_texts()
    assert any(t.startswith("🗜 История сжата средствами Codex") and "в той же сессии" in t for t in texts)
    context = [t for t in texts if t.startswith("📐")][-1]
    assert "текущего диалога" in context and "5.0k из 258.4k" in context and "<b>Сжатий истории:</b> 1" in context
    assert engine.state.get(1).session_id == "sess-123"


def test_document_is_saved_without_image_flag(tmp_path, monkeypatch):
    """Не картинка (PDF) — только путь в тексте, без --image."""
    from aiogram.types import Document

    from helpers import codex_calls, codex_prompts

    cfg, session, bot, engine, dp = build(tmp_path, monkeypatch)
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, is_bot=False, first_name="Пользователь"),
            document=Document(file_id="d1", file_unique_id="u1", file_name="отчёт, итог.pdf"),
        ),
    )

    async def fake_download(file, destination=None, **kwargs):
        with open(destination, "wb") as handle:
            handle.write(b"%PDF-1.4")

    async def go():
        await engine.startup()
        bot.download = fake_download
        await dp.feed_update(bot, update)
        await wait_done(engine, 1)

    asyncio.run(go())
    assert "--image" not in codex_calls(tmp_path)[0]
    prompt = codex_prompts(tmp_path)[0]
    assert prompt.startswith("Посмотри присланный файл") and "отчёт_итог.pdf]" in prompt
