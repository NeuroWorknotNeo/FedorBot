"""Лимиты подписки ChatGPT: сколько израсходовано и через сколько обновится.

Источник — снимки ``rate_limits``, которые Codex получает от OpenAI вместе с
ответами модели и записывает в журнал сессии (``rollout-….jsonl``, событие
``token_count``). В снимке два окна: основное (``primary``, обычно 5 часов) и
второе (``secondary``, обычно неделя) — у каждого процент использования, длина
окна и время сброса. Бот запоминает последний снимок каждого окна в
``state.json``, поэтому ``/quota`` работает и после перезапуска.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional

WINDOW_KEYS = ("primary", "secondary")

FALLBACK_WINDOW_RU = {
    "primary": "основное окно",
    "secondary": "второе окно",
}

NO_DATA_TEXT = (
    "Пока нет данных: Codex узнаёт состояние лимитов вместе с ответами модели. "
    "Отправьте боту любое сообщение и повторите /quota."
)


def parse_time(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:  # миллисекунды
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def human_delta(seconds: float) -> str:
    """Грубая длительность без секунд: «45м», «3ч 29м», «1д 19ч»."""
    total = int(max(0, seconds))
    minutes = (total + 30) // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes:02d}м"
    return f"{minutes}м"


def window_name(kind: str, window_minutes: Any) -> str:
    """«5-часовое окно», «недельное окно» — по длине окна, иначе по его роли."""
    if isinstance(window_minutes, (int, float)) and not isinstance(window_minutes, bool) and window_minutes > 0:
        minutes = int(window_minutes)
        if minutes == 7 * 24 * 60:
            return "недельное окно"
        if minutes == 24 * 60:
            return "суточное окно"
        if minutes % (24 * 60) == 0:
            return f"окно {minutes // (24 * 60)} дн."
        if minutes % 60 == 0:
            return f"{minutes // 60}-часовое окно"
        return f"окно {minutes} мин"
    return FALLBACK_WINDOW_RU.get(kind, kind or "лимит")


def reset_time(window: dict, seen_at: Any) -> Optional[datetime]:
    """Когда окно обновится: ``resets_at`` (момент) или ``resets_in_seconds`` (от момента снимка)."""
    when = parse_time(window.get("resets_at"))
    if when is not None:
        return when
    relative = window.get("resets_in_seconds")
    if isinstance(relative, (int, float)) and not isinstance(relative, bool) and isinstance(seen_at, (int, float)):
        return datetime.fromtimestamp(float(seen_at), tz=timezone.utc) + timedelta(seconds=float(relative))
    return None


def describe_reset(when: Optional[datetime], now: datetime, tz: Optional[tzinfo] = None) -> str:
    if when is None:
        return ""
    delta = (when - now).total_seconds()
    stamp = when.astimezone(tz or timezone.utc).strftime("%d.%m %H:%M %Z")
    if delta <= 0:
        return f"обновление уже наступило ({stamp})"
    return f"обновление через {human_delta(delta)} ({stamp})"


def used_percent(window: dict) -> Optional[float]:
    value = window.get("used_percent")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def describe_window(kind: str, window: dict, seen_at: Any, now: datetime, tz: Optional[tzinfo] = None) -> str:
    parts = [window_name(kind, window.get("window_minutes")) + ":"]
    used = used_percent(window)
    when = reset_time(window, seen_at)
    details = []
    if when is not None and when <= now:
        # Снимок сделан до сброса окна: прежний процент уже не действует.
        stamp = when.astimezone(tz or timezone.utc).strftime("%d.%m %H:%M %Z")
        details.append(f"окно обновилось {stamp}, свежих данных ещё нет")
        used = None
    else:
        if used is not None:
            details.append(f"использовано {used:.0f}%")
        details.append(describe_reset(when, now, tz) or "время обновления неизвестно")
    if used is not None and used >= 100:
        details.append("⛔ лимит исчерпан")
    elif used is not None and used >= 90:
        details.append("⚠️ лимит близок к исчерпанию")
    if isinstance(seen_at, (int, float)):
        details.append(f"данные {human_delta(now.timestamp() - float(seen_at))} назад")
    return parts[0] + " " + ", ".join(details)


def describe_credits(credits: Any) -> str:
    """Кредиты сверх подписки (если OpenAI их сообщает)."""
    if not isinstance(credits, dict):
        return ""
    if credits.get("unlimited"):
        return "кредиты: без ограничения"
    if credits.get("has_credits") is False:
        return ""
    balance = credits.get("balance")
    if balance not in (None, ""):
        return f"кредиты: остаток {balance}"
    return ""


def merge_snapshot(stored: Any, rate_limits: dict, seen_at: float) -> dict:
    """Обновляет сохранённые окна снимком, записанным в момент ``seen_at``.

    Окно без данных в снимке не затирается, а более старый снимок не перекрывает
    более новый: журнал сессии хранит последний снимок этой сессии, и он может
    оказаться старше того, что бот уже видел в другом разговоре.
    """
    result = dict(stored) if isinstance(stored, dict) else {}

    def newer(key: str) -> bool:
        current = result.get(key)
        known = current.get("seen_at") if isinstance(current, dict) else None
        return not isinstance(known, (int, float)) or seen_at >= float(known)

    for kind in WINDOW_KEYS:
        window = rate_limits.get(kind)
        if isinstance(window, dict) and window and newer(kind):
            result[kind] = {"info": window, "seen_at": seen_at}
    credits = rate_limits.get("credits")
    if isinstance(credits, dict) and newer("credits"):
        result["credits"] = {"info": credits, "seen_at": seen_at}
    plan = rate_limits.get("plan_type")
    if isinstance(plan, str) and plan:
        result["plan_type"] = plan
    return result


def format_rate_limits(snapshots: dict, now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> str:
    """Сводка по окнам из сохранённых снимков: {"primary": {"info": …, "seen_at": …}, …}."""
    now = now or datetime.now(timezone.utc)
    lines: list[str] = []
    for kind in WINDOW_KEYS:
        snapshot = snapshots.get(kind) if isinstance(snapshots, dict) else None
        if isinstance(snapshot, dict) and isinstance(snapshot.get("info"), dict):
            lines.append(describe_window(kind, snapshot["info"], snapshot.get("seen_at"), now, tz))
    if not lines:
        return NO_DATA_TEXT
    credits = snapshots.get("credits")
    if isinstance(credits, dict):
        text = describe_credits(credits.get("info"))
        if text:
            lines.append(text)
    plan = snapshots.get("plan_type")
    if isinstance(plan, str) and plan:
        lines.append(f"план: {plan}")
    return "\n".join(lines)


def is_near_limit(rate_limits: Any, now: Optional[datetime] = None) -> Optional[str]:
    """Предупреждение для итоговой строки: окно почти или полностью израсходовано (и ещё не сброшено)."""
    if not isinstance(rate_limits, dict):
        return None
    now = now or datetime.now(timezone.utc)
    worst: Optional[float] = None
    for kind in WINDOW_KEYS:
        window = rate_limits.get(kind)
        if isinstance(window, dict):
            when = parse_time(window.get("resets_at"))
            if when is not None and when <= now:
                continue  # окно уже обновилось, старый процент не в счёт
            used = used_percent(window)
            if used is not None and (worst is None or used > worst):
                worst = used
    if worst is None:
        return None
    if worst >= 100:
        return "⛔ Лимит подписки исчерпан"
    if worst >= 90:
        return f"⚠️ Лимит подписки близок к исчерпанию ({worst:.0f}%)"
    return None
