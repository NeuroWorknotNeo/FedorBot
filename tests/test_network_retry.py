"""Обрыв связи с api.telegram.org не должен терять ответ Codex."""

import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError

from codex_telegram_bot import bot as botmod
from codex_telegram_bot.bot import ProgressReporter, send_long

NET_ERROR = "ClientConnectorError: Cannot connect to host api.telegram.org:443 ssl:default [Connection reset by peer]"


def _net_error():
    return TelegramNetworkError(method=None, message=NET_ERROR)


class FlakyBot:
    """Падает с сетевой ошибкой заданное число раз, потом отправляет."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _net_error()
        self.sent.append(text)
        return None


class FlakyMessage:
    def __init__(self):
        self.calls = 0

    async def edit_text(self, text, **kwargs):
        self.calls += 1
        raise _net_error()


def test_send_long_retries_and_delivers(monkeypatch):
    monkeypatch.setattr(botmod, "NETWORK_RETRY_BASE_DELAY", 0)
    bot = FlakyBot(fail_times=2)
    asyncio.run(send_long(bot, 1, "важный ответ"))
    assert bot.calls == 3
    assert any("важный ответ" in text for text in bot.sent)


def test_send_long_raises_after_retries(monkeypatch):
    """Если связь так и не вернулась — ошибка всплывает, бот сообщит о ней, а не промолчит."""
    monkeypatch.setattr(botmod, "NETWORK_RETRY_BASE_DELAY", 0)
    bot = FlakyBot(fail_times=99)
    with pytest.raises(TelegramNetworkError):
        asyncio.run(send_long(bot, 1, "ответ"))
    assert bot.calls == 6  # пять попыток в цикле плюс последняя


def test_progress_edit_swallows_network_error():
    """Обновление прогресса не должно ронять доставку ответа."""

    async def go():
        message = FlakyMessage()
        reporter = ProgressReporter(FlakyBot(0), message, interval=100)
        ok = await reporter._edit("⏳ Работаю…")
        await reporter.stop()
        return ok, message.calls

    ok, calls = asyncio.run(go())
    assert ok is False and calls == 1


class FlakyAnswerMessage:
    """Сообщение Telegram, чей .answer падает сетевой ошибкой заданное число раз."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0
        self.sent: list[str] = []

    async def answer(self, text, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _net_error()
        self.sent.append(text)
        return self


def test_answer_with_retry_delivers(monkeypatch):
    """«⏳ Запускаю Codex…» не должно превращаться в «Внутренняя ошибка бота»."""
    monkeypatch.setattr(botmod, "NETWORK_RETRY_BASE_DELAY", 0)
    message = FlakyAnswerMessage(fail_times=2)
    asyncio.run(botmod.answer_with_retry(message, "⏳ Запускаю Codex…"))
    assert message.calls == 3
    assert message.sent == ["⏳ Запускаю Codex…"]


def test_answer_with_retry_redacts_token_in_url(monkeypatch):
    """Текст исключения aiohttp может содержать URL с токеном бота — его надо затереть."""
    monkeypatch.setattr(botmod, "NETWORK_RETRY_BASE_DELAY", 0)
    token = "8740000000:AAtokentokentokentokentokentoken_x"
    message = FlakyAnswerMessage()
    asyncio.run(
        botmod.answer_with_retry(
            message, f"❌ Внутренняя ошибка бота: Cannot connect to api.telegram.org/bot{token}/sendMessage"
        )
    )
    assert token not in message.sent[0]
    assert "8740000000" not in message.sent[0]


def test_closed_connector_is_not_retried(monkeypatch):
    """При остановке сервиса клиент закрыт: повторы только затягивают выключение."""
    monkeypatch.setattr(botmod, "NETWORK_RETRY_BASE_DELAY", 0)

    class ClosedBot(FlakyBot):
        async def send_message(self, chat_id, text, **kwargs):
            self.calls += 1
            raise TelegramNetworkError(method=None, message="ClientConnectionError: Connector is closed.")

    bot = ClosedBot(fail_times=0)
    with pytest.raises(TelegramNetworkError):
        asyncio.run(send_long(bot, 1, "ответ"))
    assert bot.calls == 1  # ни одного повтора

    message = FlakyAnswerMessage()

    async def closed(text, **kwargs):
        message.calls += 1
        raise TelegramNetworkError(method=None, message="ClientConnectionError: Connector is closed.")

    message.answer = closed
    with pytest.raises(TelegramNetworkError):
        asyncio.run(botmod.answer_with_retry(message, "⏳ Запускаю Codex…"))
    assert message.calls == 1
