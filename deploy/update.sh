#!/usr/bin/env bash
# Обновляет код бота из репозитория, Codex CLI — до последней версии, и
# перезапускает сервис.
#   sudo bash /root/FedorBot/deploy/update.sh
#   SKIP_CODEX_UPDATE=1 sudo bash /root/FedorBot/deploy/update.sh   # не трогать Codex
#
# Код бота живёт в ветке main (переопределить: BOT_BRANCH=имя). Если серверная
# копия стоит на другой ветке, скрипт сам переключится на main.
#
# Тело целиком завёрнуто в функцию: bash дочитывает её до конца прежде, чем
# выполнить, поэтому git pull не подменяет скрипт у него под ногами.
set -euo pipefail

main() {
  local repo_dir install_dir bot_user bot_home service branch current unit
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  install_dir="${INSTALL_DIR:-/opt/codex-telegram-bot}"
  bot_user="${BOT_USER:-codexbot}"
  service="codex-telegram-bot"
  branch="${BOT_BRANCH:-main}"

  if [[ $EUID -ne 0 ]]; then
    echo "Запустите от root: sudo bash $0" >&2
    exit 1
  fi

  git -C "$repo_dir" fetch --prune origin
  current="$(git -C "$repo_dir" rev-parse --abbrev-ref HEAD)"
  if [[ "$current" != "$branch" ]]; then
    echo "Переключаюсь с ветки $current на $branch"
    git -C "$repo_dir" checkout "$branch" 2>/dev/null || git -C "$repo_dir" checkout -b "$branch" --track "origin/$branch"
  fi
  git -C "$repo_dir" pull --ff-only origin "$branch"

  rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '.env' --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'tests' --exclude 'docs' \
    "$repo_dir/" "$install_dir/"
  chown -R "$bot_user:$bot_user" "$install_dir"
  sudo -u "$bot_user" "$install_dir/.venv/bin/pip" install -q --disable-pip-version-check -r "$install_dir/requirements.txt"

  # Codex выходит часто, а новые модели требуют свежей версии CLI.
  if [[ "${SKIP_CODEX_UPDATE:-0}" != "1" ]]; then
    echo "Обновляю Codex CLI…"
    BOT_USER="$bot_user" bash "$repo_dir/deploy/install-codex.sh" || echo "⚠️ Codex не обновился, бот продолжит работать на прежней версии." >&2
  fi

  # Юнит systemd тоже берём из репозитория: так до сервера доходят и его изменения.
  bot_home="$(getent passwd "$bot_user" | cut -d: -f6)"
  unit="/etc/systemd/system/$service.service"
  sed -e "s|codexbot|$bot_user|g" -e "s|/home/$bot_user|$bot_home|g" -e "s|/opt/codex-telegram-bot|$install_dir|g" \
    "$repo_dir/deploy/codex-telegram-bot.service" > "$unit.new"
  if ! cmp -s "$unit.new" "$unit"; then
    mv "$unit.new" "$unit"
    systemctl daemon-reload
    echo "Обновлён $unit"
  else
    rm -f "$unit.new"
  fi
  systemctl restart "$service"
  systemctl --no-pager --lines=5 status "$service"
}

main "$@"
exit $?
