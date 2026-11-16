# warp-zapret-container

Локальный HTTP/SOCKS5-прокси через Cloudflare WARP + zapret.
Проект экспериментальный: подключение зависит от сети.

Нужны Docker с Compose v2, Linux amd64 (или Docker Desktop с Linux-контейнерами),
`/dev/net/tun` и поддержка ядром iptables/NFQUEUE.

## Запуск

```bash
git clone https://github.com/BURAKKU1XD/warp-zapret-container.git
cd warp-zapret-container
cp .env.example .env
docker compose up -d --build
docker compose logs -f warp-zapret1
```

Дождитесь `[+] ready`. `Ctrl+C` закроет логи, но не остановит контейнер.

Укажите прокси в нужном приложении:

| Тип | Адрес |
|---|---|
| HTTP / HTTPS CONNECT | `http://127.0.0.1:8888` |
| SOCKS5 | `socks5h://127.0.0.1:1080` |

Проверка обоих прокси:

```bash
docker exec warp-zapret1 /healthcheck.sh
```

Успешный результат: `healthy: HTTP and SOCKS HTTPS verified, warp=on`.

## Стратегии

По умолчанию — `general`. Доступны 22 варианта:

```text
general
alt
alt2
alt3
alt4
alt5
alt6
alt7
alt8
alt9
alt10
alt11
alt12
alt13
exp
fake-tls-auto
fake-tls-auto-alt
fake-tls-auto-alt2
fake-tls-auto-alt3
simple-fake
simple-fake-alt
simple-fake-alt2
```

Чтобы сменить стратегию, измените строку в `.env`, например:

```dotenv
ZAPRET_STRATEGY=alt12
```

Примените без пересборки:

```bash
docker compose up -d --no-build --force-recreate
```

`alt5` не рекомендован автором исходника, `exp` — экспериментальный.

## Автоматическая проверка стратегий

В корне есть два независимых скрипта: [Linux/Bash](test-strategies.sh)
и [Windows/PowerShell](test-strategies.ps1). Нужны локальный Docker с
Linux-контейнерами amd64 и `curl` (`curl.exe` в Windows); Python не нужен.
Для Windows подходит PowerShell 5.1 или новее. Требования к TUN/NFQUEUE
те же, что при обычном запуске.

По умолчанию скрипт скачивает образ из GHCR, если его ещё нет локально,
и последовательно проверяет все стратегии из этого образа.

### Скачать и запустить одной командой

Клонировать репозиторий не нужно. Выполните команду в отдельной пустой папке:
скрипт сохранится в ней, отчёты появятся рядом в `test-results/`.
Команды сразу исполняют код из ветки `main` — используйте только доверенный
источник. При ошибке скачивания запуск не выполняется.

**Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/BURAKKU1XD/warp-zapret-container/main/test-strategies.sh -o test-strategies.sh && bash ./test-strategies.sh
```

**Windows (PowerShell):**

```powershell
curl.exe -fsSL https://raw.githubusercontent.com/BURAKKU1XD/warp-zapret-container/main/test-strategies.ps1 -o test-strategies.ps1; if ($LASTEXITCODE -eq 0) { .\test-strategies.ps1 }
```

### Запуск из скачанного репозитория

```bash
# Linux
bash ./test-strategies.sh
```

```powershell
# Windows
.\test-strategies.ps1
```

Если политика PowerShell запрещает запуск скачанного файла, проверьте его
содержимое и разблокируйте только этот файл: `Unblock-File .\test-strategies.ps1`.
Доступность GHCR-пакета зависит от его публикации и прав доступа.

Можно проверить локально собранный образ без скачивания и только выбранные
стратегии (предварительно выполните `docker compose build`):

```bash
bash ./test-strategies.sh \
  --image warp-zapret1-container-warp-zapret1:latest \
  --pull never --strategies general,alt2 --rounds 3
```

```powershell
.\test-strategies.ps1 `
  -Image warp-zapret1-container-warp-zapret1:latest `
  -Pull never -Strategies general,alt2 -Rounds 3
```

Для каждой стратегии скрипт ждёт запуска до 150 секунд, затем делает
3 раунда HTTPS-проверок с хоста через **оба опубликованных прокси**:
подтверждает `warp=on` и проверяет HEAD-запросами Discord, YouTube, Google
и Gstatic. Успех сайта — HTTP 2xx/3xx без ошибки TLS; при сбое WARP
проверки сайтов в этом раунде пропускаются. Это проверка связки zapret + WARP,
не прямого обхода DPI, воспроизведения видео или голосовой связи Discord.

Все проверки успешны — `PASS`; часть не прошла — `PARTIAL`;
контейнер не запустился вовремя — `STARTUP_FAIL`. Скрипт перечисляет все
полностью прошедшие стратегии, но **не меняет `.env` и не выбирает их за вас**.
Один прогон не гарантирует длительную стабильность. Перебор может занять
десятки минут; ошибка одной стратегии не останавливает остальные.

Рабочий контейнер и его том не затрагиваются: используются случайные порты
на `127.0.0.1` и отдельный временный том WARP, общий для стратегий одного
прогона. При запуске принимаются условия Cloudflare и создаётся новая
тестовая регистрация WARP. После теста контейнеры и временный том удаляются.
При `Ctrl+C` выполняется очистка и остаются частичные отчёты. Принудительное
завершение процесса/закрытие терминала может оставить тестовые ресурсы;
их имена начинаются с `warp-zapret-test-`. Не удаляйте рабочий том.

Отчёты: `test-results/<имя-прогона>/summary.tsv`, `checks.tsv` и `run.txt`
(версия образа и параметры). В них нет тела Cloudflare trace, внешнего IP,
данных регистрации или сырых логов WARP. Каталог исключён из Git.

Дополнительные параметры: `--pull always`, `--startup-timeout`,
`--request-timeout`, `--targets-file`, `--output`; в PowerShell:
`-Pull always`, `-StartupTimeout`, `-RequestTimeout`, `-TargetsFile`,
`-OutputDirectory`. Файл целей — по одному публичному HTTPS URL в строке,
без авторизации и query-параметров; пустые строки и строки с `#` пропускаются.
Цели попадут в отчёт — не указывайте приватные адреса. Справка:
`bash ./test-strategies.sh --help` / `.\test-strategies.ps1 -Help`.
Коды выхода: `0` — есть полный `PASS`, `1` — ни одного, `2` — ошибка скрипта.

**Важно:** прокси без пароля — не открывайте порты наружу.
Не используйте `docker compose down -v`: это удалит регистрацию WARP.

[Безопасность](SECURITY.md) · [MIT](LICENSE) · [Сторонние компоненты](THIRD_PARTY_NOTICES.md)
