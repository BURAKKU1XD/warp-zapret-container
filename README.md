# warp-zapret-container

Локальный HTTP/SOCKS5-прокси через Cloudflare WARP + zapret.
Проект экспериментальный: подключение зависит от сети.

Нужны Docker с Compose v2, Linux amd64 (или Docker Desktop с Linux-контейнерами),
`/dev/net/tun` и поддержка ядром iptables/NFQUEUE.

## Запуск

Одна команда создаёт папку `warp-zapret`, скачивает из GitHub только
[compose.yaml](compose.yaml) и [.env.example](.env.example) под именем `.env`,
затем скачивает готовый образ из GHCR и запускает контейнер.
Нужны Docker Compose и `curl` (`curl.exe` в Windows).

**Linux / Bash:**

```bash
mkdir warp-zapret && cd warp-zapret && curl -fsSL https://raw.githubusercontent.com/BURAKKU1XD/warp-zapret-container/main/compose.yaml -o compose.yaml && curl -fsSL https://raw.githubusercontent.com/BURAKKU1XD/warp-zapret-container/main/.env.example -o .env && docker compose up -d --pull always
```

**Windows / PowerShell:**

```powershell
cmd /c "mkdir warp-zapret && cd warp-zapret && curl.exe -fsSL https://raw.githubusercontent.com/BURAKKU1XD/warp-zapret-container/main/compose.yaml -o compose.yaml && curl.exe -fsSL https://raw.githubusercontent.com/BURAKKU1XD/warp-zapret-container/main/.env.example -o .env && docker compose up -d --pull always"
```

Команда предназначена для первой установки: папка `warp-zapret` должна отсутствовать.
При ошибке скачивания запуск не выполняется. Последующие команды выполняйте
из созданной папки `warp-zapret`.

По умолчанию Compose использует `ghcr.io/burakku1xd/warp-zapret-container:latest`.
Стратегии, списки и fake-пакеты находятся в образе. В `.env` выбирается стратегия:

```dotenv
ZAPRET_STRATEGY=general
```

Посмотреть запуск:

```bash
docker compose logs -f warp-zapret
```

Образ должен быть опубликован в GHCR и доступен для скачивания. Для обновления
повторите `docker compose pull` и `docker compose up -d`. Для фиксации версии
добавьте в `.env` переменную `WARP_ZAPRET_IMAGE` с полным именем образа и
опубликованным тегом версии или `sha-<полный SHA коммита>`.

При переходе с `docker-compose.yml` используйте прежнюю папку проекта и замените
старый файл на `compose.yaml`. Если старый контейнер называется `warp-zapret1`,
сначала остановите его командой `docker stop warp-zapret1`. Compose может сообщить
об оставшемся старом сервисе. Имя тома `warp_zapret1_state` сохранено для
совместимости: в прежнем Compose-проекте новый контейнер использует прежнюю
регистрацию WARP. Если ранее задавали имя проекта через `-p` или
`COMPOSE_PROJECT_NAME`, сохраните и его.

Дождитесь `[+] ready`. `Ctrl+C` закроет логи, но не остановит контейнер.

Укажите прокси в нужном приложении:

| Тип | Адрес |
|---|---|
| HTTP / HTTPS CONNECT | `http://127.0.0.1:8888` |
| SOCKS5 | `socks5h://127.0.0.1:1080` |

Проверка обоих прокси:

```bash
docker exec warp-zapret /healthcheck.sh
```

Успешный результат: `healthy: HTTP and SOCKS HTTPS verified, warp=on`.

Встроенный watchdog запускается независимо от WARP. Если запуск не завершился
за 120 секунд, он завершает контейнер с ошибкой после остановки текущей команды.
Каждый вызов `warp-cli` ограничен 10 секундами, затем даётся 2 секунды на
завершение перед принудительной остановкой.

После начального периода 120 секунд watchdog проверяет оба прокси, делая паузу
30 секунд между проверками. Пять ошибок подряд завершают контейнер с очисткой
процессов и правил; успешная проверка обнуляет счётчик. Compose перезапускает
его через `restart: unless-stopped`, Kubernetes — через политику Pod.
Регистрация в томе сохраняется. Длительное отключение интернета тоже вызывает
перезапуски. Ручной запуск healthcheck и проверки Docker/Kubernetes не меняют
счётчик watchdog.

Логи сервисов идут в stdout/stderr без накопления файлов в `/var/log`.
В Compose включена ротация Docker (`10m`, 3 файла); в Kubernetes её настраивает
администратор кластера. Вывод команд регистрации WARP скрыт, строки WARP
с полями регистрации, ключей и токенов отфильтровываются.

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

## DNS

При запуске контейнер настраивает системный DNS через `DNS_SERVERS`.
По умолчанию: `1.1.1.1 8.8.8.8`. Для Compose измените `.env`:

```dotenv
DNS_SERVERS="1.1.1.1 8.8.8.8"
```

При `docker run` передайте `-e DNS_SERVERS="1.1.1.1 8.8.8.8"`.
Поддерживаются от одного до трёх IPv4-адресов через пробел. Пустое значение
использует дефолт; некорректный список останавливает запуск до старта WARP.
Entrypoint записывает `/etc/resolv.conf` до запуска сервисов; файл должен быть
доступен для записи. Исходный файл сохраняется в
`/run/warp-zapret.resolv.conf.original`; для восстановления внутри контейнера:
`cat /run/warp-zapret.resolv.conf.original > /etc/resolv.conf`.

В Helm задайте список в своём values-файле:

```yaml
dnsPolicy: None
dnsConfig:
  nameservers:
    - "1.1.1.1"
    - "8.8.8.8"
```

Chart задаёт Pod `dnsPolicy: None` с `dnsConfig.nameservers`.
В контейнер передаётся `DNS_SERVERS=inherit`: entrypoint сохраняет DNS,
поисковые домены и опции, которые настроил Kubernetes. Это значение также
можно использовать при `docker run`, чтобы сохранить DNS Docker.
Эта настройка заменяет DNS Docker/кластера для самого прокси: внутренние
имена Docker и Kubernetes (`*.svc.cluster.local`) через публичные DNS
не разрешаются. Клиентские Pod сохраняют собственные настройки DNS.
Для удалённого разрешения имён через SOCKS используйте `socks5h://`;
локальный DNS клиента и приложения с собственным DoH эта переменная не меняет.
После изменения настроек пересоздайте контейнер или обновите Helm release;
для изменения дефолта в образе нужна сборка нового образа.

## Kubernetes / Helm

Chart [charts/warp-zapret](charts/warp-zapret) запускает **отдельный общий прокси**:
приложения подключаются к Service, а не получают sidecar. Только запросы,
явно отправленные приложением в прокси, идут через WARP.

Нужны Helm 3/4, Kubernetes 1.26+, Linux amd64-нода с `/dev/net/tun`,
iptables/NFQUEUE/connbytes, доступ к Cloudflare и StorageClass для PVC.
Политика кластера должна разрешать root, `NET_ADMIN`, `NET_RAW` и hostPath
для TUN: Pod Security Baseline/Restricted этот Pod не допускают.
Chart **не включает privileged и hostNetwork**. Runtime должен разрешать
доступ к устройству TUN; если он запрещён, одного hostPath недостаточно —
потребуется настройка выдачи устройства администратором кластера.

Из корня клонированного репозитория:

```bash
helm upgrade --install warp-zapret ./charts/warp-zapret \
  --namespace proxy --create-namespace --wait --timeout 6m
kubectl -n proxy exec deployment/warp-zapret -- /healthcheck.sh
```

По умолчанию скачивается `ghcr.io/burakku1xd/warp-zapret-container:latest`;
образ должен быть опубликован и доступен нодам. Для приватного GHCR
укажите существующий Secret через `imagePullSecrets` в values.
Для воспроизводимых обновлений задайте `image.tag` опубликованной версией
или `sha-<полный SHA коммита>`; стратегию — через `strategy`.
Все параметры: [values.yaml](charts/warp-zapret/values.yaml).
Пользовательский файл values передавайте при каждом обновлении через `-f`.

По умолчанию Pod прокси использует публичные DNS `1.1.1.1` и `8.8.8.8`
с `dnsPolicy: None`. Настройки DNS применяются только к Pod прокси:

```yaml
dnsPolicy: None
dnsConfig:
  nameservers:
    - 1.1.1.1
    - 8.8.8.8
  options:
    - name: ndots
      value: "1"
```

Сначала проверьте доступность выбранных DNS из сети Pod. Этот override не меняет
CoreDNS, DNS ноды и DNS клиентских приложений. Pod прокси с такой настройкой
не использует кластерный resolver для имён `*.svc`; приложения продолжают
находить Service прокси через собственный Kubernetes DNS. Изменение DNS
пересоздаёт Pod согласно стратегии Recreate и прерывает активные соединения.

В **шаблоне Pod приложения** задайте метку и переменные окружения
(структура настроек в Helm chart конкретного приложения может отличаться):

```yaml
spec:
  template:
    metadata:
      labels:
        warp-zapret-client: "true"
    spec:
      containers:
        - name: app
          # image и остальные настройки вашего приложения
          env:
            - name: HTTP_PROXY
              value: http://warp-zapret.proxy.svc:8888
            - name: HTTPS_PROXY
              value: http://warp-zapret.proxy.svc:8888
            - name: NO_PROXY
              value: localhost,127.0.0.1,::1,.svc,.cluster.local
```

Приложение должно поддерживать эти переменные (некоторым нужны
`http_proxy`/`https_proxy`/`no_proxy` в нижнем регистре или настройка прокси
в самом приложении). Для SOCKS5 адрес — `socks5h://warp-zapret.proxy.svc:1080`.
Дополните `NO_PROXY` короткими именами внутренних сервисов и адресами своей
сети; поддержку CIDR и суффиксов определяет клиент.

По умолчанию NetworkPolicy разрешает прокси только помеченным Pod'ам
**в namespace `proxy`** и только на TCP 8888/1080. Для приложений, например,
в namespace `apps`, добавьте в свой values-файл правило, сохранив
`podSelector` и `namespaceSelector` **в одном элементе списка**:

```yaml
networkPolicy:
  ingressFrom:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: apps
      podSelector:
        matchLabels:
          warp-zapret-client: "true"
```

Список заменяет стандартное правило. Ограничение работает только при
поддержке NetworkPolicy вашим CNI; другие политики могут дополнительно
разрешать доступ. Исходящий трафик chart не ограничивает; существующие
egress-политики должны пропускать DNS и соединения WARP.
Прокси без авторизации — не публикуйте его через Ingress/NodePort/LoadBalancer.

Chart использует одну реплику и `Recreate`: обновление временно прерывает
доступ. Регистрация хранится в PVC (по умолчанию 1 GiB), который **остаётся
после `helm uninstall`**. Не удаляйте PVC и не подключайте его одновременно
к нескольким WARP-процессам. Можно задать `persistence.existingClaim`;
при `persistence.enabled=false` регистрация теряется при замене Pod.
Startup/readiness проверяют HTTPS через оба прокси и `warp=on`;
при сбое readiness Service перестаёт направлять новые подключения в Pod.

CPU, память, рестарты и готовность доступны через существующий мониторинг
кластера. Собственного `/metrics`, exporter и ServiceMonitor пока нет.
Логи: `kubectl -n proxy logs deployment/warp-zapret` — вывод WARP может
содержать приватные данные, не публикуйте его без очистки.

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

Для разработки клонируйте репозиторий и соберите локальный образ командой
`docker build -t warp-zapret:latest .`. Чтобы запустить его через Compose,
задайте `WARP_ZAPRET_IMAGE=warp-zapret:latest` в `.env` и выполните
`docker compose up -d --pull never`.

Можно проверить локально собранный образ без скачивания и только выбранные
стратегии:

```bash
bash ./test-strategies.sh \
  --image warp-zapret:latest \
  --pull never --strategies general,alt2 --rounds 3
```

```powershell
.\test-strategies.ps1 `
  -Image warp-zapret:latest `
  -Pull never -Strategies general,alt2 -Rounds 3
```

Скрипт выполняет три этапа в указанном порядке:

1. `direct`: запросы прямо с хоста с отключённым использованием прокси curl.
   Это референс до запуска тестовых контейнеров.
2. `warp-control`: те же запросы через WARP с `--without-zapret`, без работающего
   nfqws и правил NFQUEUE. Показывает, работает ли WARP без zapret.
3. Выбранные стратегии: запросы через HTTP и SOCKS каждого тестового контейнера.

Во всех этапах проверяются 12 адресов: `discord.com`, `gateway.discord.gg`,
`cdn.discordapp.com`, `updates.discord.com`, `www.youtube.com`, `youtu.be`,
`i.ytimg.com`, `redirector.googlevideo.com`, `www.google.com`, `www.gstatic.com`,
`www.cloudflare.com`, `cdnjs.cloudflare.com`. Каждый адрес проверяется GET-запросами
с HTTP/1.1, TLS 1.2 и TLS 1.3; отдельно запрашивается Cloudflare trace.
Успех trace требует HTTP 200 и `warp=on` через WARP, `warp=off` напрямую.
Редиректы разрешены только по HTTPS, максимум три. Успех сайта требует curl exit 0
и конечного HTTP 2xx. `HTTP_ERROR`, `TLS_ERROR`, `TIMEOUT`, `UNSUPPORTED` и прочие
ошибки остаются отдельными строками; ошибка TLS не считается доказательством
отсутствия поддержки протокола. Запросы сайтов выполняются и при неудаче trace.
Ответ 403/404 на корневом URL CDN может быть нормальным: это ответ сервера,
но не успешная проверка сайта. Такой `PARTIAL` сам по себе не доказывает отказ стратегии.

По умолчанию контроль и каждая стратегия запускаются в **двух новых контейнерах**
(`--connections 2` / `-Connections 2`), по три раунда в каждом
(`--rounds 3` / `-Rounds 3`). Прямой референс выполняет три раунда один раз.
Для стандартного набора это 111 проверок напрямую и по 444 на контроль и стратегию;
защитные проверки прокси в этот счёт не входят. Каждый контейнер заново подключает
WARP: активные соединения не переносятся. Временный том регистрации общий для
прогона, поэтому часть состояния клиента сохраняется. Сравнение может зависеть
от времени, endpoint и состояния сети. При таймаутах полный перебор может занять часы.

В `summary.tsv` раздельно выводятся:

- `status`: `AVAILABLE`, если все запрошенные проверки успешны; иначе `PARTIAL`
  либо `STARTUP_FAIL`. Это доступность URL, не доказательство обхода DPI.
- `stability`: `PASS` только при успехе минимум двух подключений и двух раундов;
  при меньшей выборке — `INSUFFICIENT_SAMPLES`. Это не гарантия длительной стабильности.
- `effect`: `WARP_WORKS_WITHOUT_ZAPRET`, если контроль прошёл полностью;
  `OBSERVED_GAIN`, если одинаковые проверки всегда не проходили в запущенном
  контроле и всегда проходят со стратегией, nfqws жив и счётчики очереди растут;
  иначе `NOT_ESTABLISHED`. Ошибка запуска контроля не доказывает выигрыш стратегии.
- `recovered_checks`: число улучшившихся сочетаний прокси/адрес/протокол.
  `queue_packets`: пакеты TCP/UDP в NFQUEUE с запуска новых контейнеров,
  включая установление туннеля. Счётчики не доказывают применение конкретного
  профиля desync: `profile_evidence=NOT_ESTABLISHED` записывается явно.

Настроен MASQUE `h3-with-h2-fallback`. Фактически согласованный H3/H2 пока
записывается как `UNKNOWN`: настройки и UDP-сокет не доказывают выбранный транспорт.
HTTP/1.1/TLS в запросах описывают соединение с сайтом внутри WARP, не транспорт туннеля.

Для проверки загрузки задайте все три параметра:
`--download-url URL --download-bytes N --download-sha256 SHA256`
(PowerShell: `-DownloadUrl`, `-DownloadBytes`, `-DownloadSha256`).
Файл должен совпасть по размеру и SHA256. Для проверки отправки задайте
`--upload-url URL` / `-UploadUrl URL`: endpoint должен принимать POST и возвращать
ровно отправленные 65536 нулевых байт; ответ сверяется по размеру и SHA256.
Без endpoint соответствующая проверка отмечается `NOT_RUN` в `run.txt`.
Эти проверки не заменяют проверку видео YouTube, WebSocket и голосового UDP Discord.

Curl игнорирует `curlrc`; напрямую используется `--noproxy '*'`, через WARP —
только явно указанный тестовый прокси. В контейнерах обнуляются `HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY`, `FTP_PROXY`, `NO_PROXY` в обоих регистрах.
Перед тестом запросы через закрытые HTTP/SOCKS-порты обязаны дать ошибку подключения.
Проверка обоих портов повторяется после остановки каждого контейнера.
Тело trace, внешние IP, регистрация и сырые логи не сохраняются;
временные файлы передачи удаляются при штатной очистке.

VPN/TUN и прозрачный прокси на хосте этим не отключаются:
`host_route_isolation=NOT_ESTABLISHED`. Если прямой trace показывает WARP или
не подтверждает `warp=off`, референс не считается чистым для `OBSERVED_GAIN`.
Прямые запросы идут с хоста, WARP — из Docker; их DNS и маршрут могут отличаться.

Дополнительные параметры: `--pull always`, `--startup-timeout`, `--request-timeout`,
`--targets-file`, `--output`; PowerShell: `-Pull always`, `-StartupTimeout`,
`-RequestTimeout`, `-TargetsFile`, `-OutputDirectory`. Файл целей содержит по одному
публичному HTTPS URL без авторизации и query string на строку; пустые строки
и комментарии `#` пропускаются. Скрипты требуют образ с `--without-zapret`
и `--test-diagnostics`; старый образ отклоняется до запуска тестов.
Скачивание скрипта само по себе образ не обновляет.

Отчёты в `test-results/<имя-прогона>/`: `summary.tsv` — сравнение этапов,
`checks.tsv` — каждый запрос (подключение, раунд, прокси, протокол, HTTP-код,
curl exit, объём, время, TTFB), `connections.tsv` — запуск и счётчики контейнера,
`run.txt` — образ и параметры. Каталог исключён из Git.
Скрипты не меняют `.env` и рабочие контейнеры. Удаляются только собственные
временные контейнеры и том; при прерывании остаются частичные отчёты.
Коды выхода: `0` — есть `AVAILABLE` стратегия, `1` — таких нет,
`2` — ошибка тестировщика/очистки. Код `0` не означает доказанный эффект zapret.


**Важно:** прокси без пароля — не открывайте порты наружу.
Не используйте `docker compose down -v`: это удалит регистрацию WARP.

[Безопасность](SECURITY.md) · [MIT](LICENSE) · [Сторонние компоненты](THIRD_PARTY_NOTICES.md)
