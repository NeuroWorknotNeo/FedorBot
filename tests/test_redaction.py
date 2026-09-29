"""Тесты затирания секретов в исходящем тексте."""

import json
import os

from codex_telegram_bot.redaction import PLACEHOLDER, Redactor, auth_file_secrets, configure_redactor, redact


def test_exact_literal_redacted():
    token = "8471234567:AAExampleExampleExampleExample_ab"
    r = Redactor(literals=[token], include_default_env=False)
    out = r(f"вот токен: {token} — держи")
    assert token not in out
    assert PLACEHOLDER in out


def test_short_literal_not_redacted():
    """Короткие значения не затираем, чтобы не портить обычный текст."""
    r = Redactor(literals=["12:ab"], include_default_env=False)
    assert r("код 12:ab тут") == "код 12:ab тут"


def test_env_value_redacted(monkeypatch):
    secret = "some-opaque-openai-secret-value-123"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    r = Redactor()
    assert secret not in r(f"мой ключ {secret}")


def test_telegram_token_shape_redacted():
    """Токен из чужого файла (не из переменной) ловится по форме."""
    r = Redactor(include_default_env=False)
    text = "содержимое: 8740000000:AAdummydummydummydummydummydummy-xx"
    out = r(text)
    assert "8740000000" not in out
    assert PLACEHOLDER in out


def test_openai_jwt_and_github_shapes_redacted():
    r = Redactor(include_default_env=False)
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    out = r(
        "sk-proj-AbCdEf0123456789AbCdEf0123456789xyzXYZ_-abc and "
        f"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 and {jwt}"
    )
    assert "sk-proj-AbCdEf" not in out
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out
    assert "eyJhbGci" not in out


def test_normal_text_untouched():
    r = Redactor(include_default_env=False)
    for sample in (
        "git commit 3f2a1b9", "сегодня 12:30", "порт 8080:80", "обычный ответ модели",
        "ветка sk-some-very-long-branch-name-here-2026", "модель gpt-6-sol",
    ):
        assert r(sample) == sample


def test_encode_trick_plaintext_caught():
    """Приём «покажи было/стало»: строка «было» с открытым токеном затирается."""
    token = "8740594767:AAdummydummydummydummydummydummy-1J"
    r = Redactor(literals=[token], include_default_env=False)
    out = r(f"было:  {token}\nстало: HGD0EIDGFG:AA...")
    assert token not in out


def test_codex_auth_file_tokens_redacted(tmp_path):
    """Токены входа ChatGPT из ~/.codex/auth.json затираются, в том числе после обновления файла."""
    auth = tmp_path / "auth.json"
    refresh = "rt_opaque_refresh_token_value_0001"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": refresh, "account_id": "acc-1"}}))
    assert refresh in auth_file_secrets(auth)
    r = Redactor(include_default_env=False, auth_file=auth)
    assert refresh not in r(f"refresh: {refresh}")
    assert r("режим chatgpt") == "режим chatgpt"  # безобидные поля не трогаем
    rotated = "rt_opaque_refresh_token_value_0002"
    auth.write_text(json.dumps({"tokens": {"refresh_token": rotated}}))
    stat = auth.stat()
    os.utime(auth, (stat.st_atime, stat.st_mtime + 5))
    assert rotated not in r(f"новый: {rotated}")


def test_module_level_configure_and_redact():
    token = "9001234567:AAmodulelevelmodulelevelmodulele_xy"
    configure_redactor(literals=[token])
    assert token not in redact(f"secret {token} end")
    # сбрасываем к значениям по умолчанию, чтобы не влиять на другие тесты
    configure_redactor()
