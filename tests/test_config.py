import pytest

from codex_telegram_bot.config import (
    DEFAULT_MODEL_BUTTONS,
    MODEL_FAMILIES,
    Config,
    ConfigError,
    default_extra_instructions,
    is_model_name,
)


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(0o755)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1, 2;3")
    monkeypatch.setenv("CODEX_BIN", str(fake))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    for name in (
        "CODEX_EXTRA_ARGS", "CODEX_EXTRA_INSTRUCTIONS", "CODEX_SANDBOX", "CODEX_MODEL", "CODEX_EFFORT",
        "MODEL_BUTTONS", "CODEX_TIMEOUT_SECONDS", "CODEX_NETWORK_ACCESS", "CODEX_WEB_SEARCH",
        "ALLOWED_CHAT_IDS", "TEAM_CHAT_IDS", "ALLOW_PRIVATE_CHATS", "WORKSPACE_PER_CHAT", "GROUP_REQUIRE_MENTION",
        "TIMEZONE", "REACTION_WORKING", "REACTION_DONE",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_defaults(base_env):
    cfg = Config.from_env()
    assert cfg.allowed_user_ids == frozenset({1, 2, 3})
    assert cfg.sandbox == "danger-full-access"
    assert cfg.network_access is True and cfg.web_search == "live"
    assert cfg.workspace_dir == (base_env / "ws").resolve()
    assert cfg.state_file == cfg.workspace_dir / ".codex-telegram-bot" / "state.json"
    assert cfg.extra_instructions == default_extra_instructions(10800)
    assert cfg.timeout_seconds == 10800
    assert cfg.extra_args == ()
    assert cfg.show_tokens is True
    assert cfg.allowed_chat_ids == frozenset()
    assert cfg.allow_private_chats is True
    assert cfg.group_require_mention is False
    assert cfg.default_model is None and cfg.default_effort is None


def test_missing_allowlist(base_env, monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "")
    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        Config.from_env()


def test_invalid_sandbox(base_env, monkeypatch):
    monkeypatch.setenv("CODEX_SANDBOX", "yolo")
    with pytest.raises(ConfigError, match="CODEX_SANDBOX"):
        Config.from_env()
    monkeypatch.setenv("CODEX_SANDBOX", "workspace-write")
    assert Config.from_env().sandbox == "workspace-write"


def test_empty_instructions_disable_them(base_env, monkeypatch):
    monkeypatch.setenv("CODEX_EXTRA_INSTRUCTIONS", "")
    assert Config.from_env().extra_instructions is None


def test_missing_binary(base_env, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_BIN", str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="CODEX_BIN"):
        Config.from_env()


def test_group_settings(base_env, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "-1001234567890, -100555")
    monkeypatch.setenv("ALLOW_PRIVATE_CHATS", "false")
    monkeypatch.setenv("GROUP_REQUIRE_MENTION", "true")
    cfg = Config.from_env()
    assert cfg.allowed_chat_ids == frozenset({-1001234567890, -100555})
    assert cfg.allow_private_chats is False
    assert cfg.group_require_mention is True


def test_team_and_workspace_settings(base_env, monkeypatch):
    monkeypatch.setenv("TEAM_CHAT_IDS", "-100222")
    monkeypatch.setenv("WORKSPACE_PER_CHAT", "true")
    cfg = Config.from_env()
    assert cfg.team_chat_ids == frozenset({-100222})
    assert cfg.workspace_per_chat is True


def test_effort_setting(base_env, monkeypatch):
    monkeypatch.setenv("CODEX_EFFORT", "High")
    assert Config.from_env().default_effort == "high"
    monkeypatch.setenv("CODEX_EFFORT", "turbo")
    with pytest.raises(ConfigError, match="CODEX_EFFORT"):
        Config.from_env()


def test_model_setting(base_env, monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "gpt-6-sol")
    assert Config.from_env().default_model == "gpt-6-sol"
    monkeypatch.setenv("CODEX_MODEL", "gpt 6")
    with pytest.raises(ConfigError, match="CODEX_MODEL"):
        Config.from_env()


def test_reaction_settings(base_env, monkeypatch):
    cfg = Config.from_env()
    assert (cfg.reaction_working, cfg.reaction_done) == ("👀", "👍")
    monkeypatch.setenv("REACTION_WORKING", "off")
    monkeypatch.setenv("REACTION_DONE", "🔥")
    cfg = Config.from_env()
    assert (cfg.reaction_working, cfg.reaction_done) == (None, "🔥")


def test_timezone_setting(base_env, monkeypatch):
    assert Config.from_env().timezone == "UTC"
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow")
    assert Config.from_env().timezone == "Europe/Moscow"
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus")
    with pytest.raises(ConfigError, match="TIMEZONE"):
        Config.from_env()


def test_inline_comments_and_quotes_are_tolerated(base_env, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "-1001111111111       # личная группа")
    monkeypatch.setenv("TEAM_CHAT_IDS", "'-1002222222222'  # командная")
    monkeypatch.setenv("ALLOW_PRIVATE_CHATS", "false             # в личку не отвечать")
    monkeypatch.setenv("WORKSPACE_PER_CHAT", "true               # свои копии")
    monkeypatch.setenv("TIMEZONE", "Europe/Moscow                # время по Москве")
    monkeypatch.setenv("REACTION_DONE", "🔥 # огонь")
    monkeypatch.setenv("CODEX_MODEL", '"gpt-6-sol"')
    cfg = Config.from_env()
    assert cfg.allowed_chat_ids == frozenset({-1001111111111})
    assert cfg.team_chat_ids == frozenset({-1002222222222})
    assert cfg.allow_private_chats is False
    assert cfg.workspace_per_chat is True
    assert cfg.timezone == "Europe/Moscow"
    assert cfg.reaction_done == "🔥"
    assert cfg.default_model == "gpt-6-sol"


def test_model_buttons_default(base_env):
    assert Config.from_env().model_buttons == DEFAULT_MODEL_BUTTONS


def test_default_model_buttons_one_per_family():
    """По одной кнопке на семейство, у каждой точное имя модели, а не алиас."""
    families = []
    for value, label in DEFAULT_MODEL_BUTTONS:
        if value == "default":
            continue
        assert is_model_name(value) and value.startswith("gpt-"), f"на кнопке должно быть точное имя модели: {value}"
        family = value.rsplit("-", 1)[-1]
        assert family in MODEL_FAMILIES, f"неизвестное семейство в {value}"
        assert family.capitalize() in label, f"подпись {label!r} не называет семейство {family}"
        families.append(family)
    assert sorted(families) == sorted(MODEL_FAMILIES), f"нужна ровно одна кнопка на семейство, получено {families}"
    assert DEFAULT_MODEL_BUTTONS[0] == ("default", "По умолчанию")


def test_model_buttons_custom(base_env, monkeypatch):
    monkeypatch.setenv("MODEL_BUTTONS", "default,gpt-6-sol:GPT-6 Sol,gpt-5.5")
    assert Config.from_env().model_buttons == (
        ("default", "default"),
        ("gpt-6-sol", "GPT-6 Sol"),
        ("gpt-5.5", "gpt-5.5"),
    )


def test_model_buttons_reject_nonsense(base_env, monkeypatch):
    monkeypatch.setenv("MODEL_BUTTONS", "default,модель с пробелами")
    with pytest.raises(ConfigError):
        Config.from_env()
    monkeypatch.setenv("MODEL_BUTTONS", "default," + "x" * 70)
    with pytest.raises(ConfigError, match="слишком длинное"):
        Config.from_env()


def test_turn_limit_reaches_instructions(base_env, monkeypatch):
    """Агент должен видеть настоящий предел хода."""
    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "7200")
    cfg = Config.from_env()
    assert cfg.timeout_seconds == 7200
    assert "7200 seconds" in (cfg.extra_instructions or "")
    assert "TG_SEND_FILE" in (cfg.extra_instructions or "")


def test_timeout_minimum(base_env, monkeypatch):
    monkeypatch.setenv("CODEX_TIMEOUT_SECONDS", "10")
    with pytest.raises(ConfigError, match="CODEX_TIMEOUT_SECONDS"):
        Config.from_env()


def test_is_model_name():
    assert is_model_name("gpt-6-astra") and is_model_name("gpt-5.5") and is_model_name("o3")
    assert not is_model_name("") and not is_model_name("gpt 6") and not is_model_name("a:b")


def test_web_search_modes(base_env, monkeypatch):
    for raw, expected in (("true", "live"), ("false", "disabled"), ("cached", "cached"), ("default", None), ("LIVE", "live")):
        monkeypatch.setenv("CODEX_WEB_SEARCH", raw)
        assert Config.from_env().web_search == expected
    monkeypatch.setenv("CODEX_WEB_SEARCH", "everything")
    with pytest.raises(ConfigError, match="CODEX_WEB_SEARCH"):
        Config.from_env()


def test_extra_args_reject_exec_only_flags(base_env, monkeypatch):
    monkeypatch.setenv("CODEX_EXTRA_ARGS", '-c model_reasoning_summary="auto" --enable goals')
    assert Config.from_env().extra_args == ("-c", "model_reasoning_summary=auto", "--enable", "goals")
    for bad in ("-s workspace-write", "--sandbox=read-only", "-C /tmp", "--add-dir /x"):
        monkeypatch.setenv("CODEX_EXTRA_ARGS", bad)
        with pytest.raises(ConfigError, match="CODEX_EXTRA_ARGS"):
            Config.from_env()
