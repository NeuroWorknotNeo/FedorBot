#!/usr/bin/env bash
# Вход в Codex под пользователем бота и проверка одним коротким запросом.
#
#   sudo bash deploy/codex-login.sh             # аккаунт ChatGPT по коду устройства (рекомендуется)
#   sudo bash deploy/codex-login.sh --browser   # аккаунт ChatGPT через браузер и SSH-туннель (если код устройства недоступен)
#   sudo bash deploy/codex-login.sh --api-key   # API-ключ OpenAI (оплата по тарифам API, не подписка)
#   sudo bash deploy/codex-login.sh --status    # только показать, чем авторизован Codex, и проверить запросом
#
# Учётные данные Codex хранит сам в ~/.codex/auth.json пользователя бота и сам
# их обновляет. Перезапускать бота после входа не нужно: каждый запуск codex
# читает их заново.
set -euo pipefail

BOT_USER="${BOT_USER:-codexbot}"
MODE="${1:---device}"

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo bash $0 $*" >&2
  exit 1
fi
if ! id -u "$BOT_USER" >/dev/null 2>&1; then
  echo "Нет пользователя $BOT_USER — сначала выполните install.sh" >&2
  exit 1
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
CODEX="${CODEX_BIN:-$BOT_HOME/.local/bin/codex}"
if [[ ! -x "$CODEX" ]]; then
  echo "Не найден $CODEX — установите Codex: sudo bash $(dirname "$0")/install-codex.sh" >&2
  exit 1
fi

as_bot() { sudo -iu "$BOT_USER" "$@"; }

case "$MODE" in
  --device|-d)
    cat <<'TXT'
Сейчас Codex покажет ссылку (https://auth.openai.com/codex/device) и одноразовый код.
  1. Откройте ссылку в браузере на своём компьютере или телефоне.
  2. Войдите в аккаунт ChatGPT с подпиской (Plus/Pro/Business) и введите код.
     Код действует 15 минут.
  3. Вернитесь сюда: вход завершится сам, как только вы подтвердите его в браузере.
Прежний вход Codex (если был) при этом заменяется новым.
Если Codex ответит, что вход по коду устройства недоступен, используйте запасной
способ:  sudo bash deploy/codex-login.sh --browser
TXT
    as_bot "$CODEX" login --device-auth
    ;;
  --browser|-b)
    cat <<TXT
Вход через браузер с SSH-туннелем. Codex ждёт ответа браузера на порту 1455
сервера, поэтому порт нужно пробросить на ваш компьютер:
  1. На СВОЁМ компьютере откройте второе окно терминала и выполните
       ssh -L 1455:localhost:1455 root@АДРЕС_СЕРВЕРА
     (не закрывайте это окно до конца входа).
  2. Здесь Codex напечатает длинную ссылку https://auth.openai.com/… —
     откройте её в браузере на компьютере и войдите в ChatGPT.
  3. Браузер перейдёт на http://localhost:1455/… — туннель доставит ответ
     на сервер, и вход завершится. (Если Codex напишет другой порт, например
     1457, пробросьте в шаге 1 именно его.)
Нажмите Enter, когда туннель готов.
TXT
    read -r _
    as_bot "$CODEX" login
    ;;
  --api-key|-k)
    echo "Вставьте API-ключ OpenAI (sk-…) и нажмите Enter (ввод не отображается)."
    echo "Внимание: с ключом Codex работает по тарифам API, а не по подписке ChatGPT."
    IFS= read -rs KEY
    echo
    KEY="${KEY//[[:space:]]/}"
    if [[ "$KEY" != sk-* || ${#KEY} -lt 20 ]]; then
      echo "Это не похоже на ключ OpenAI: он начинается с sk- (получено ${#KEY} символов)." >&2
      exit 1
    fi
    printf '%s' "$KEY" | sudo -u "$BOT_USER" -H "$CODEX" login --with-api-key
    unset KEY
    ;;
  --status|-s)
    ;;
  *)
    echo "Неизвестный режим $MODE. Варианты: --device (по умолчанию), --browser, --api-key, --status" >&2
    exit 1
    ;;
esac

echo
echo "Статус входа:"
if ! as_bot "$CODEX" login status; then
  echo "❌ Codex не авторизован. Повторите: sudo bash $0" >&2
  exit 1
fi

echo
echo "Проверяю одним коротким запросом к модели (без сохранения сессии)…"
TMP="$(sudo -u "$BOT_USER" mktemp -d)"
set +e
OUT="$(cd "$TMP" && printf 'Ответь одним словом: ок' | timeout 300 sudo -u "$BOT_USER" -H env PATH="$BOT_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  "$CODEX" exec --json --skip-git-repo-check --ephemeral -- - 2>&1)"
set -e
sudo -u "$BOT_USER" rm -rf "$TMP"
VERDICT="$(printf '%s' "$OUT" | python3 -c '
import json, sys
answer, failure, completed, notes = "", "", False, []
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        event = json.loads(line)
    except ValueError:
        continue
    kind = event.get("type")
    item = event.get("item") or {}
    if kind == "item.completed" and item.get("type") == "agent_message":
        answer = item.get("text") or answer
    elif kind == "turn.completed":
        completed = True
    elif kind == "turn.failed":
        failure = str((event.get("error") or {}).get("message") or "turn.failed")
    elif kind == "error":
        notes.append(str(event.get("message") or ""))  # «Reconnecting… N/5» и т. п. — не провал
if completed and not failure:
    print("OK " + (answer or "(пустой ответ)").replace("\n", " ")[:200])
else:
    print("ERROR " + (failure or (notes[-1] if notes else "") or "Codex не завершил ответ")[:400])
')"
case "$VERDICT" in
  OK*)
    echo "✅ Codex ответил: ${VERDICT#OK }"
    echo "Готово. Бот подхватит вход сам, перезапуск не нужен."
    ;;
  *)
    echo "❌ Запрос не прошёл: ${VERDICT#ERROR }" >&2
    echo "Последние строки вывода codex:" >&2
    printf '%s\n' "$OUT" | tail -n 5 >&2
    exit 1
    ;;
esac
