# Установка с нуля: от пустого сервера до работающего бота

Пошаговая инструкция для человека, у которого есть только что купленный VPS (в стране, где доступен ChatGPT, 2+ vCPU, 4 GB RAM, Ubuntu 22.04/24.04/26.04 или Debian 12/13) и на нём ничего не установлено. Каждый шаг: что сделать, что должно получиться, что делать, если не получилось. Обзор проекта и все настройки — в [README.md](README.md).

Время: 20–30 минут, из них большая часть — ожидание установки.

## Что должно быть под рукой

- IP-адрес сервера и пароль root (или SSH-ключ), которые прислал провайдер.
- Компьютер с терминалом: на Windows 10/11 это PowerShell (`ssh` уже встроен), на macOS — Terminal, на Linux — любой терминал.
- Telegram (на телефоне или компьютере).
- Браузер, в котором вы залогинены в ChatGPT (аккаунт с подпиской Plus, Pro или Business) и на github.com.

Как копировать и вставлять в терминале: в PowerShell вставка — правая кнопка мыши или Ctrl+V; в терминале Linux/macOS — Ctrl+Shift+V или Cmd+V. Пароль при вводе не отображается, это нормально. Все команды ниже выполняются на сервере от имени root, если не сказано иное.

## Шаг 1. Подключиться к серверу

```bash
ssh root@ВАШ_IP
```

При первом подключении спросят `Are you sure you want to continue connecting (yes/no)?` — напишите `yes`. Затем введите пароль.

Если провайдер выдал не root, а пользователя вроде `ubuntu` или `debian`: подключитесь под ним и станьте root командой `sudo -i`.

Проверьте систему:

```bash
cat /etc/os-release | head -2
```

Ожидается `Ubuntu 22.04`, `24.04` или `26.04`, либо `Debian GNU/Linux 12`/`13`. Если у вас что-то другое (Alma, Rocky, Alpine), установщик из шага 4 не подойдёт.

## Шаг 2. Обновить систему и поставить git

```bash
apt update && DEBIAN_FRONTEND=noninteractive apt upgrade -y
apt install -y git curl nano
```

Если всё же появился синий диалог (`Configuring keyboard-configuration`, `Which services should be restarted?`): стрелками выберите любой пункт, Enter; на экране со списком служб нажмите Tab до `<Ok>` и Enter. Если в конце написано `*** System restart required ***`, перезагрузите сервер (`reboot`), подождите минуту и подключитесь снова.

Необязательно, но полезно: закрыть все входящие порты, кроме SSH (боту входящие соединения не нужны):

```bash
apt install -y ufw
ufw allow OpenSSH
ufw enable
```

На вопрос `Proceed with operation (y|n)?` ответьте `y`. Порядок важен: сначала `allow OpenSSH`, потом `enable`.

## Шаг 3. Скачать бота

Репозиторий открытый, логин на GitHub для скачивания не нужен:

```bash
git clone https://github.com/NeuroWorknotNeo/FedorBot.git /root/FedorBot
ls /root/FedorBot
```

В выводе должны быть `README.md`, `INSTALL.md`, `codex_telegram_bot`, `deploy`, `tests`.

## Шаг 4. Запустить установщик

```bash
bash /root/FedorBot/deploy/install.sh
```

Скрипт работает 2–5 минут и делает всё сам:

- ставит системные пакеты и GitHub CLI (`gh`);
- создаёт отдельного пользователя `codexbot` без прав root — от него будут работать и бот, и Codex;
- копирует бота в `/opt/codex-telegram-bot`, создаёт Python-окружение, ставит зависимости;
- устанавливает Codex CLI официальным установщиком OpenAI в `/home/codexbot/.local/bin/codex`;
- создаёт каталог проектов `/home/codexbot/workspace`;
- регистрирует сервис `codex-telegram-bot` в systemd (автозапуск после перезагрузки).

В конце он печатает блок «Установка завершена» с напоминанием оставшихся ручных шагов — это шаги 6–9 ниже. Скрипт можно запускать повторно, ничего не сломается.

Как понять, что скрипт работает, а не завис: этапы помечены синими строками `==> [секунды] Название`, внутри этапа вывод apt и pip скрыт, так что экран может не меняться 1–3 минуты. Если хочется убедиться, откройте второе окно терминала, подключитесь ещё раз и выполните `top`: там должны быть `apt`, `dpkg`, `pip`, `python3` или `curl` (выход — `q`).

Проверка:

```bash
sudo -iu codexbot codex --version
```

Ожидается строка вида `codex-cli 0.159.0`. Если `command not found` — запустите `bash /root/FedorBot/deploy/install-codex.sh` и посмотрите, на чём он споткнулся (чаще всего — нет доступа к `chatgpt.com`/`github.com` из сети сервера).

## Шаг 5. Создать бота в Telegram и узнать свой ID

1. Откройте в Telegram [@BotFather](https://t.me/BotFather), отправьте `/newbot`.
2. Придумайте отображаемое имя (любое, например «Мой Codex»).
3. Придумайте username: он должен быть уникальным и заканчиваться на `bot`, например `fedor_codex_bot`.
4. BotFather пришлёт токен вида `1234567890:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Сохраните его, это пароль от бота.

Свой числовой Telegram ID можно узнать у [@userinfobot](https://t.me/userinfobot) (отправьте ему `/start`, в ответе будет `Id: 123456789`). Другой способ: после шага 9 написать своему боту `/id` — он покажет ID даже тому, кто ещё не в белом списке.

## Шаг 6. Войти в Codex аккаунтом ChatGPT

На сервере нет браузера, поэтому вход проходит через ваш компьютер по одноразовому коду:

```bash
bash /root/FedorBot/deploy/codex-login.sh
```

1. Codex напечатает ссылку `https://auth.openai.com/codex/device` и одноразовый код. Откройте ссылку в браузере на компьютере (или телефоне).
2. Войдите в аккаунт ChatGPT с подпиской и введите код (он действует 15 минут).
3. Вернитесь в терминал: вход завершится сам. Скрипт покажет `Logged in using ChatGPT`, затем проверит всё коротким запросом и напечатает `✅ Codex ответил: ок`.

Если вход по коду устройства не проходит (Codex пишет, что он недоступен, или сайт не принимает код), используйте запасной способ:

- **Браузер + SSH-туннель:** `bash /root/FedorBot/deploy/codex-login.sh --browser` — скрипт объяснит, как открыть второе окно с `ssh -L 1455:localhost:1455 root@ВАШ_IP`, после чего обычный вход через браузер завершится на сервере.
- **Перенос входа с другого компьютера** (крайний случай): если на своём компьютере вы уже вошли в Codex, скопируйте файл `~/.codex/auth.json` на сервер в `/home/codexbot/.codex/auth.json`, затем `chown codexbot:codexbot /home/codexbot/.codex/auth.json && chmod 600 /home/codexbot/.codex/auth.json`. Это именно перенос: две копии одного входа мешают друг другу (токен обновления одноразовый). После копирования удалите `~/.codex/auth.json` на компьютере вручную — **не** командой `codex logout`, она отзовёт токен и на сервере — и при необходимости войдите там заново.
- **API-ключ** (оплата по тарифам API, а не подписка): `bash /root/FedorBot/deploy/codex-login.sh --api-key`.

Учётные данные хранятся в `/home/codexbot/.codex/auth.json` и обновляются Codex автоматически. Проверить в любой момент: `sudo -iu codexbot codex login status` или команда бота `/status` (строка «🔑 Авторизация»).

## Шаг 7. Подключить GitHub

Нужно, чтобы Codex мог делать `git push` и открывать pull request.

```bash
sudo -iu codexbot gh auth login
```

Ответы на вопросы:

- `Where do you use GitHub?` → **GitHub.com**
- `What is your preferred protocol for Git operations?` → **HTTPS**
- `Authenticate Git with your GitHub credentials?` → **Yes**
- `How would you like to authenticate GitHub CLI?` → **Login with a web browser**

Терминал покажет одноразовый код вида `XXXX-XXXX` и попросит нажать Enter. Нажмите; браузер на сервере не откроется, это нормально. На компьютере откройте https://github.com/login/device, введите код и подтвердите. В терминале появится `Logged in as ваш_логин`.

Затем представьтесь git (имя и e-mail попадут в коммиты; e-mail лучше взять из настроек GitHub → Emails, там есть и скрытый вариант вида `12345+login@users.noreply.github.com`):

```bash
sudo -iu codexbot gh auth setup-git
sudo -iu codexbot git config --global user.name "Ваше имя"
sudo -iu codexbot git config --global user.email "ваш@email"
sudo -iu codexbot gh auth status
```

Последняя команда должна написать `Logged in to github.com account …` и `Token scopes: … repo …`.

## Шаг 8. Заполнить настройки бота

```bash
nano /opt/codex-telegram-bot/.env
```

В файле нужно изменить две строки (остальное можно не трогать):

```
TELEGRAM_BOT_TOKEN=1234567890:AAF...      ← токен из шага 5
ALLOWED_USER_IDS=123456789                ← ваш Telegram ID из шага 5
```

Без пробелов вокруг `=`, без кавычек. Комментарий после значения (`… # заметка`) допустим, бот его отбрасывает. Полезно ещё указать часовой пояс: `TIMEZONE=Europe/Moscow`. Сохранить: Ctrl+O, Enter. Выйти: Ctrl+X.

## Шаг 9. Запустить и проверить

```bash
systemctl start codex-telegram-bot
systemctl status codex-telegram-bot --no-pager
journalctl -u codex-telegram-bot -f
```

В логах должна появиться строка `Бот @ваш_бот запущен; разрешённые пользователи: [123456789]` и `Авторизация Codex: ✅ вход через аккаунт ChatGPT (подписка)`. Просмотр логов закрывается по Ctrl+C, бот при этом продолжает работать.

Если в логах `Ошибка конфигурации: …` — там написано, какая переменная не так; поправьте `.env` (шаг 8) и выполните `systemctl restart codex-telegram-bot`.

Теперь в Telegram откройте своего бота и отправьте:

- `/start` — придёт справка;
- `/status` — версия Codex, каталог проектов, песочница, авторизация.

## Шаг 9б. Перевести бота в группу (если нужно)

Пока `ALLOWED_CHAT_IDS` пуст, бот отвечает и в личке, и в любой группе, куда его добавили, но только пользователям из белого списка. Чтобы он работал в вашей группе:

1. Добавьте бота в группу и сделайте администратором («Управление группой → Администраторы → Добавить администратора», права можно минимальные) — иначе Telegram не передаёт ему обычные сообщения.
2. В группе отправьте `/id`. Бот ответит строкой вида `ID чата: -1001234567890 (супергруппа)`.
3. Впишите ID в `.env` и, если личка не нужна, отключите её:
   ```
   ALLOWED_CHAT_IDS=-1001234567890
   ALLOW_PRIVATE_CHATS=false
   ```
4. `systemctl restart codex-telegram-bot`. В группе `/status` покажет строку `💬 Чат: <название группы>`.

В группах с темами каждая тема — отдельный разговор со своим проектом и сессией (удобно завести по теме на проект). Сообщение людям, а не боту, начинайте с `/t`.

## Шаг 10. Первый диалог и первая задача для GitHub

1. Напишите боту обычное сообщение: «Привет! Что ты умеешь и в каком каталоге ты сейчас находишься?». Появится сообщение «⏳ Работаю…», которое обновляется по ходу дела, затем ответ.
2. Клонируйте свой репозиторий в рабочий каталог бота:
   ```
   /clone владелец/репозиторий
   ```
   Бот ответит «✅ Клонировано … и выбрано как проект». (Для приватного репозитория нужен шаг 7.)
3. Спросите о проекте: «Расскажи, что лежит в этом репозитории, по папкам». Это тот же диалог, Codex помнит предыдущие сообщения (сброс — `/new`).
4. Поставьте задачу в GitHub:
   ```
   /task добавь в корень репозитория файл README.md с кратким описанием того, что лежит в каждой папке
   ```
   Бот создаст ветку `tg/…`, Codex внесёт изменения, сделает коммит, `git push` и откроет pull request; в конце пришлёт отчёт со ссылкой на PR. Откройте её на GitHub, посмотрите изменения и нажмите Merge, если всё устраивает.
5. Хотите доработать — просто напишите следующее сообщение, оно продолжит ту же ветку и тот же PR. Новая независимая задача — снова `/task`.

## Шаг 11. Обслуживание

| Что | Команда |
|---|---|
| Логи | `journalctl -u codex-telegram-bot -n 100` (или `-f` для живого просмотра) |
| Перезапуск / остановка | `systemctl restart codex-telegram-bot`, `systemctl stop codex-telegram-bot` |
| Обновить бота и Codex | `bash /root/FedorBot/deploy/update.sh` (без обновления Codex: `SKIP_CODEX_UPDATE=1 bash …`) |
| Только обновить Codex | `bash /root/FedorBot/deploy/install-codex.sh` |
| Версия Codex | `sudo -iu codexbot codex --version` |
| Чем авторизован Codex | `sudo -iu codexbot codex login status` |
| Войти в Codex заново | `bash /root/FedorBot/deploy/codex-login.sh` |
| Диагностика Codex | `sudo -iu codexbot codex doctor` |
| Какие модели знает Codex | см. команду под таблицей |
| Кто авторизован в GitHub | `sudo -iu codexbot gh auth status` |
| Изменить настройки | `nano /opt/codex-telegram-bot/.env`, затем `systemctl restart codex-telegram-bot` |
| Добавить swap (запас памяти) | `bash /root/FedorBot/deploy/add-swap.sh` (по умолчанию 4 ГБ, можно `… 2G`) |
| Сколько памяти и диска | `free -h` (строка Swap — запас), `df -h /` |

Список моделей, которые знает ваш Codex (имя — описание):

```bash
sudo -iu codexbot codex debug models | python3 -c 'import json,sys; [print(m["slug"], "—", m["description"]) for m in json.load(sys.stdin)["models"] if m.get("visibility") == "list"]'
```

Присланные боту файлы лежат в `/home/codexbot/workspace/.codex-telegram-bot/uploads`, журналы сессий Codex — в `/home/codexbot/.codex/sessions/`.

## Безопасность

Codex выполняет команды **от имени пользователя `codexbot`** и по умолчанию без песочницы. Значит, **любой, кому разрешено писать боту, фактически может читать и запускать что угодно от имени `codexbot`** — включая `.env` с токеном Telegram и `~/.codex/auth.json`. Бот сделан для одного человека, поэтому держите `ALLOWED_USER_IDS` из одного ID и не храните на сервере посторонних секретов.

Если токен мог утечь — сначала отзовите: токен Telegram — @BotFather → `/revoke` → новый токен в `.env` → `systemctl restart codex-telegram-bot`; вход Codex — `sudo -iu codexbot codex logout` и снова шаг 6 (плюс «выйти на всех устройствах» в настройках безопасности ChatGPT).

## Если что-то пошло не так

- **`Permission denied (publickey)` при ssh** — провайдер выдал доступ по ключу, а не по паролю. Используйте ключ, указанный при заказе, или веб-консоль провайдера.
- **Установщик ругается `Could not get lock /var/lib/dpkg/lock`** — система ставит обновления в фоне; подождите пару минут и повторите шаг 4.
- **Бот молчит везде** — `journalctl -u codex-telegram-bot -n 50`. Если там `Ошибка конфигурации: …`, в `.env` опечатка в названной переменной; поправьте и перезапустите сервис. Другие частые причины: неверный `TELEGRAM_BOT_TOKEN` (`Unauthorized`) или сервис не запущен (`systemctl status codex-telegram-bot`).
- **Бот отвечает «⛔ Доступ запрещён. Ваш Telegram ID: …»** — этого ID нет в `ALLOWED_USER_IDS`. Добавьте и перезапустите сервис.
- **В группе бот отвечает на команды, но молчит на обычный текст** — у бота нет прав администратора. Пока это не исправлено, работают `/ask текст` и ответы на сообщения бота.
- **Ответ Codex: `Not logged in`, `401 Unauthorized`, `refresh token`, `sign in again`** — вход не выполнен или устарел: повторите шаг 6. Перезапуск бота не нужен.
- **`codex-login.sh` не может открыть ссылку / сайт не принимает код** — проверьте, что входите в тот же аккаунт ChatGPT, где есть подписка, и что код не старше 15 минут; иначе используйте `--browser` (туннель) или перенос `auth.json` (шаг 6).
- **Запросы отклоняются с `unsupported_country_region_territory` или `403`** — сервер в стране, где OpenAI недоступен. Нужен VPS в поддерживаемом регионе.
- **`usage limit` / «You've hit your usage limit»** — исчерпан лимит подписки. Скрытая команда `/quota` покажет, когда он обновится.
- **`/task` пишет, что не удалось запушить или создать PR** — `sudo -iu codexbot gh auth status`, при необходимости шаг 7. Убедитесь, что у аккаунта есть право писать в репозиторий.
- **`/task` пишет «Рабочее дерево не чистое»** — от прошлой задачи остались незакоммиченные файлы. Напишите боту «закоммить все изменения» или выполните `/git stash`.
- **Долгие задачи обрываются через 3 часа** — это предел `CODEX_TIMEOUT_SECONDS`; при необходимости увеличьте его в `.env`.
- **Что-то ещё** — пришлите вывод `journalctl -u codex-telegram-bot -n 100` и текст ошибки.

## Шпаргалка: все команды подряд

```bash
ssh root@ВАШ_IP
apt update && DEBIAN_FRONTEND=noninteractive apt upgrade -y && apt install -y git curl nano
git clone https://github.com/NeuroWorknotNeo/FedorBot.git /root/FedorBot
bash /root/FedorBot/deploy/install.sh
bash /root/FedorBot/deploy/codex-login.sh       # ссылка + код → браузер на компьютере → ✅ Codex ответил
sudo -iu codexbot gh auth login                 # GitHub.com → HTTPS → Yes → browser → код на github.com/login/device
sudo -iu codexbot gh auth setup-git
sudo -iu codexbot git config --global user.name "Имя"
sudo -iu codexbot git config --global user.email "email"
nano /opt/codex-telegram-bot/.env               # TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS
systemctl start codex-telegram-bot && journalctl -u codex-telegram-bot -f
```
