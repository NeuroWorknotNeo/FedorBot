#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Установка Telegram-бота для Codex (OpenAI) на чистый VPS
# (Ubuntu 22.04/24.04/26.04, Debian 12/13).
#
# Запускать от root:   sudo bash FedorBot/deploy/install.sh
# Переменные (необязательно):
#   BOT_USER=codexbot                    системный пользователь, от которого работают бот и codex
#   INSTALL_DIR=/opt/codex-telegram-bot  куда копируется код бота
#
# Скрипт идемпотентен: его можно запускать повторно после обновления кода.
# ---------------------------------------------------------------------------
set -euo pipefail

BOT_USER="${BOT_USER:-codexbot}"
INSTALL_DIR="${INSTALL_DIR:-/opt/codex-telegram-bot}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="codex-telegram-bot"

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo bash $0" >&2
  exit 1
fi

START_TS=$(date +%s)
log() { printf '\n\033[1;34m==> [%3d с] %s\033[0m\n' "$(( $(date +%s) - START_TS ))" "$*"; }
note() { printf '    %s\n' "$*"; }

echo "Установка начата: $(date '+%H:%M:%S'). Обычно занимает 2–5 минут, на медленном сервере до 10."
echo "Этапы помечены синими строками '==>'; внутри этапа вывод apt и pip скрыт, тишина в это время — нормально."

log "Системные пакеты"
note "apt ставит ~15 пакетов, обычно 1–3 минуты без вывода"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq sudo nano git curl ca-certificates gnupg python3 python3-venv python3-pip jq unzip ripgrep rsync >/dev/null

if ! command -v gh >/dev/null 2>&1; then
  log "GitHub CLI (gh)"
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update -qq
  apt-get install -y -qq gh >/dev/null
fi

if ! id -u "$BOT_USER" >/dev/null 2>&1; then
  log "Пользователь $BOT_USER"
  useradd --create-home --shell /bin/bash "$BOT_USER"
fi
BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"

log "Код бота → $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '.env' --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'tests' --exclude 'docs' \
  "$SRC_DIR/" "$INSTALL_DIR/"
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cp "$SRC_DIR/.env.example" "$INSTALL_DIR/.env"
  echo "Создан $INSTALL_DIR/.env из шаблона — его нужно заполнить."
fi
chown -R "$BOT_USER:$BOT_USER" "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/.env"

log "Python-окружение"
note "pip скачивает aiogram и зависимости, обычно 20–60 секунд без вывода"
sudo -u "$BOT_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$BOT_USER" "$INSTALL_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"

log "Codex CLI (официальный установщик, от пользователя $BOT_USER)"
note "скачивается ~150 МБ; codex ставится в $BOT_HOME/.local/bin/codex"
BOT_USER="$BOT_USER" bash "$SRC_DIR/deploy/install-codex.sh" || {
  echo "Установка Codex не удалась. Проверьте доступ в интернет и повторите: sudo bash $SRC_DIR/deploy/install-codex.sh" >&2
}

log "Workspace"
sudo -u "$BOT_USER" mkdir -p "$BOT_HOME/workspace"

log "systemd-сервис $SERVICE_NAME"
sed -e "s|codexbot|$BOT_USER|g" -e "s|/home/$BOT_USER|$BOT_HOME|g" -e "s|/opt/codex-telegram-bot|$INSTALL_DIR|g" \
  "$SRC_DIR/deploy/codex-telegram-bot.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
if systemctl is-active --quiet "$SERVICE_NAME"; then
  systemctl restart "$SERVICE_NAME"
  echo "Сервис перезапущен."
fi

cat <<NEXT

========================================================================
Установка завершена за $(( $(date +%s) - START_TS )) с. Осталось сделать вручную (один раз):

1) Войти в Codex аккаунтом ChatGPT с подпиской (нужен браузер на вашем компьютере):
     sudo bash $SRC_DIR/deploy/codex-login.sh
   Скрипт покажет ссылку и одноразовый код: откройте ссылку на компьютере,
   войдите в ChatGPT и введите код. (Подробно и запасные способы — INSTALL.md, шаг 6.)

2) Доступ к GitHub для push и pull request:
     sudo -iu $BOT_USER gh auth login        # GitHub.com → HTTPS → Login with a web browser
     sudo -iu $BOT_USER gh auth setup-git
     sudo -iu $BOT_USER git config --global user.name  "Ваше имя"
     sudo -iu $BOT_USER git config --global user.email "you@example.com"

3) Заполнить $INSTALL_DIR/.env: TELEGRAM_BOT_TOKEN (от @BotFather) и
   ALLOWED_USER_IDS (Telegram ID через запятую: ваш и, если боту отвечать двоим,
   второго человека; ID покажет бот на /id или @userinfobot).

4) Запустить и посмотреть логи:
     sudo systemctl start $SERVICE_NAME
     sudo journalctl -u $SERVICE_NAME -f
========================================================================
NEXT
