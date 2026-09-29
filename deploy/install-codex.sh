#!/usr/bin/env bash
# Ставит или обновляет Codex CLI для пользователя бота официальным установщиком
# OpenAI (тот же, что в README openai/codex): бинарник попадает в
# ~/.local/bin/codex пользователя бота, служебные файлы — в ~/.codex/packages.
#
#   sudo bash FedorBot/deploy/install-codex.sh            # последняя стабильная версия
#   sudo bash FedorBot/deploy/install-codex.sh 0.159.0    # конкретная версия
#
# Установщик сначала качает с releases.openai.com, при недоступности — с GitHub.
set -euo pipefail

BOT_USER="${BOT_USER:-codexbot}"
RELEASE="${1:-latest}"
INSTALLER_URLS=(
  "https://chatgpt.com/codex/install.sh"
  "https://github.com/openai/codex/releases/latest/download/install.sh"
)

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo bash $0" >&2
  exit 1
fi
if ! id -u "$BOT_USER" >/dev/null 2>&1; then
  echo "Нет пользователя $BOT_USER — сначала выполните install.sh" >&2
  exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
fetched=""
for url in "${INSTALLER_URLS[@]}"; do
  if curl -fsSL --connect-timeout 15 --max-time 120 "$url" -o "$tmp" && [[ -s "$tmp" ]]; then
    fetched="$url"
    break
  fi
  echo "Не удалось скачать установщик с $url, пробую следующий адрес…" >&2
done
if [[ -z "$fetched" ]]; then
  echo "Установщик Codex недоступен. Проверьте интернет: curl -I https://github.com" >&2
  exit 1
fi
chmod 0644 "$tmp"

echo "Установщик: $fetched (версия: $RELEASE)"
sudo -iu "$BOT_USER" env CODEX_NON_INTERACTIVE=1 CODEX_RELEASE="$RELEASE" sh "$tmp"

BOT_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
if [[ -x "$BOT_HOME/.local/bin/codex" ]]; then
  echo "Готово: $(sudo -iu "$BOT_USER" "$BOT_HOME/.local/bin/codex" --version 2>/dev/null | tail -1)"
else
  echo "Установщик отработал, но $BOT_HOME/.local/bin/codex не найден — посмотрите вывод выше." >&2
  exit 1
fi
