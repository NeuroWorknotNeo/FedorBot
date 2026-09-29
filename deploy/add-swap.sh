#!/usr/bin/env bash
# Создаёт файл подкачки (swap), чтобы скачок памяти не заканчивался убийством
# процесса OOM-killer'ом: система отложит редко используемые страницы на диск,
# а задача продолжит работать (медленнее, но живой).
#
# Запускать от root:
#   sudo bash /root/FedorBot/deploy/add-swap.sh        # 4 ГБ по умолчанию
#   sudo bash /root/FedorBot/deploy/add-swap.sh 2G     # другой размер
#
# Скрипт идемпотентен: если swap уже включён, он ничего не меняет.
set -euo pipefail

SIZE="${1:-4G}"
SWAPFILE="${SWAPFILE:-/swapfile}"

if [ "$(id -u)" != "0" ]; then
  echo "Запустите от root: sudo bash $0 [размер]" >&2
  exit 1
fi

if swapon --show --noheadings 2>/dev/null | grep -q .; then
  echo "Swap уже включён, ничего не меняю:"
  swapon --show
  exit 0
fi

case "$SIZE" in
  *[Gg]) mib=$(( ${SIZE%[Gg]} * 1024 )) ;;
  *[Mm]) mib=${SIZE%[Mm]} ;;
  *) echo "Размер укажите как 4G или 512M, получено: $SIZE" >&2; exit 1 ;;
esac

# Файл подкачки занимает место на диске, поэтому оставляем ещё ~1 ГБ запаса.
avail_mib="$(df -Pm / | awk 'NR==2 {print $4}')"
if [ "$avail_mib" -lt $(( mib + 1024 )) ]; then
  echo "На / свободно ${avail_mib} МБ — мало для swap ${SIZE}: нужен ещё примерно 1 ГБ запаса." >&2
  echo "Возьмите размер поменьше, например: sudo bash $0 2G" >&2
  exit 1
fi

if [ -e "$SWAPFILE" ]; then
  echo "$SWAPFILE уже существует, но не включён. Разберитесь с ним вручную:" >&2
  ls -lh "$SWAPFILE" >&2
  exit 1
fi

echo "Создаю $SWAPFILE размером $SIZE (свободно на диске: ${avail_mib} МБ)…"
if ! fallocate -l "$SIZE" "$SWAPFILE" 2>/dev/null; then
  echo "fallocate не поддержан файловой системой, создаю через dd (это дольше)…"
  dd if=/dev/zero of="$SWAPFILE" bs=1M count="$mib" status=none
fi
chmod 600 "$SWAPFILE"
mkswap "$SWAPFILE" >/dev/null
swapon "$SWAPFILE"

if ! grep -qs "^${SWAPFILE} " /etc/fstab; then
  printf '%s none swap sw 0 0\n' "$SWAPFILE" >> /etc/fstab
  echo "Добавлено в /etc/fstab — swap включится и после перезагрузки."
fi

# Swap нужен как подушка на скачки, а не для постоянной работы: с низким
# swappiness система предпочитает оперативку и уходит на диск лишь под давлением.
sysctl -q -w vm.swappiness=10
printf 'vm.swappiness=10\n' > /etc/sysctl.d/99-codex-bot-swappiness.conf

echo
echo "Готово."
free -h
swapon --show
