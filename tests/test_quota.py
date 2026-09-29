from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from codex_telegram_bot import quota

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def test_human_delta():
    assert quota.human_delta(20) == "0м"
    assert quota.human_delta(45 * 60 + 40) == "46м"
    assert quota.human_delta(3 * 3600 + 29 * 60 + 39) == "3ч 30м"
    assert quota.human_delta(43 * 3600) == "1д 19ч"


def test_parse_time_variants():
    assert quota.parse_time(None) is None
    assert quota.parse_time("garbage") is None
    assert quota.parse_time(True) is None
    epoch = int(NOW.timestamp())
    assert quota.parse_time(epoch) == NOW
    assert quota.parse_time(epoch * 1000) == NOW
    assert quota.parse_time("2026-09-20T12:00:00Z") == NOW


def test_window_names():
    assert quota.window_name("primary", 300) == "5-часовое окно"
    assert quota.window_name("secondary", 10080) == "недельное окно"
    assert quota.window_name("secondary", 1440) == "суточное окно"
    assert quota.window_name("primary", 90) == "окно 90 мин"
    assert quota.window_name("secondary", None) == "второе окно"


def test_describe_window_and_timezone():
    window = {"used_percent": 23.4, "window_minutes": 300, "resets_at": int((NOW + timedelta(hours=3, minutes=30)).timestamp())}
    assert quota.describe_window("primary", window, NOW.timestamp() - 7 * 60, NOW) == (
        "5-часовое окно: использовано 23%, обновление через 3ч 30м (20.09 15:30 UTC), данные 7м назад"
    )
    assert quota.describe_window("primary", window, None, NOW, tz=ZoneInfo("Europe/Moscow")) == (
        "5-часовое окно: использовано 23%, обновление через 3ч 30м (20.09 18:30 MSK)"
    )
    near = {"used_percent": 95, "window_minutes": 10080, "resets_at": "2026-09-22T07:00:00Z"}
    assert quota.describe_window("secondary", near, None, NOW) == (
        "недельное окно: использовано 95%, обновление через 1д 19ч (22.09 07:00 UTC), ⚠️ лимит близок к исчерпанию"
    )
    spent = {"used_percent": 100}
    assert quota.describe_window("primary", spent, None, NOW) == (
        "основное окно: использовано 100%, время обновления неизвестно, ⛔ лимит исчерпан"
    )
    # старый формат: сброс через N секунд от момента снимка
    relative = {"used_percent": 10, "window_minutes": 300, "resets_in_seconds": 3600}
    assert quota.describe_window("primary", relative, NOW.timestamp(), NOW) == (
        "5-часовое окно: использовано 10%, обновление через 1ч 00м (20.09 13:00 UTC), данные 0м назад"
    )


def test_merge_and_format():
    assert quota.format_rate_limits({}, now=NOW).startswith("Пока нет данных")
    first = {
        "limit_id": "codex",
        "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": NOW.timestamp() + 2 * 3600},
        "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": "2026-09-22T07:00:00Z"},
        "credits": {"has_credits": True, "unlimited": False, "balance": "12.50"},
        "plan_type": "pro",
    }
    stored = quota.merge_snapshot(None, first, NOW.timestamp() - 3600)
    # новый снимок без второго окна не затирает сохранённое
    stored = quota.merge_snapshot(stored, {"primary": {"used_percent": 20, "window_minutes": 300,
                                                        "resets_at": NOW.timestamp() + 2 * 3600}}, NOW.timestamp() - 65)
    lines = quota.format_rate_limits(stored, now=NOW).split("\n")
    assert lines == [
        "5-часовое окно: использовано 20%, обновление через 2ч 00м (20.09 14:00 UTC), данные 1м назад",
        "недельное окно: использовано 40%, обновление через 1д 19ч (22.09 07:00 UTC), данные 1ч 00м назад",
        "кредиты: остаток 12.50",
        "план: pro",
    ]


def test_is_near_limit():
    assert quota.is_near_limit(None) is None
    assert quota.is_near_limit({"primary": {"used_percent": 50}}) is None
    assert quota.is_near_limit({"primary": {"used_percent": 50}, "secondary": {"used_percent": 93}}) == (
        "⚠️ Лимит подписки близок к исчерпанию (93%)"
    )
    assert quota.is_near_limit({"primary": {"used_percent": 100}}) == "⛔ Лимит подписки исчерпан"


def test_older_snapshot_does_not_override_newer():
    fresh = {"primary": {"used_percent": 10.0, "window_minutes": 300, "resets_at": NOW.timestamp() + 3600}}
    stale = {"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": NOW.timestamp() + 600}}
    stored = quota.merge_snapshot(None, fresh, NOW.timestamp() - 60)
    stored = quota.merge_snapshot(stored, stale, NOW.timestamp() - 3 * 3600)  # журнал старой сессии
    assert stored["primary"]["info"]["used_percent"] == 10.0


def test_window_after_reset_is_not_reported_as_spent():
    spent = {"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": NOW.timestamp() - 60}}
    assert quota.is_near_limit(spent, now=NOW) is None
    line = quota.describe_window("primary", spent["primary"], NOW.timestamp() - 7200, NOW)
    assert line.startswith("5-часовое окно: окно обновилось 20.09 11:59 UTC") and "исчерпан" not in line and "100%" not in line
