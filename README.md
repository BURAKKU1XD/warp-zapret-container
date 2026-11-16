# warp-zapret1-container

Минимальный Docker-контейнер с Cloudflare WARP + zapret/nfqws, который поднимает локальный HTTP и SOCKS proxy через WARP.

## Для чего
Подходит, когда нужно дать приложениям локальный proxy через Cloudflare WARP:

```text
HTTP:  127.0.0.1:8888
SOCKS: 127.0.0.1:1080
```

## Скачать

```bash
git clone https://github.com/BURAKKU1XD/warp-zapret1-container.git
cd warp-zapret1-container
```

## Собрать

```bash
docker compose build --no-cache --progress=plain
```

## Запустить

```bash
docker compose up -d
docker logs -f warp-zapret1
```

Успешный запуск:

```text
Status update: Connected
Tunnel Protocol: MASQUE
[+] ready
```

## Использовать/проверить HTTP/SOCKS proxy

```bash
curl -x http://127.0.0.1:8888 https://www.cloudflare.com/cdn-cgi/trace | grep warp=
curl --socks5-hostname 127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace | grep warp=
```

Должно быть:
```text
warp=on
```

## Проверить состояние

```bash
docker exec warp-zapret1 /usr/bin/bash -lc '
warp-cli --accept-tos status
warp-cli --accept-tos tunnel stats || true
ss -lntup | grep -E "40000|8888|1080" || true
'
```

## Как поменять порты

`docker-compose.yml`

Сейчас:

```yaml
ports:
  - "127.0.0.1:8888:8888"
  - "127.0.0.1:1080:1080"
```

Например, поменять HTTP на `8889`, SOCKS на `1081`:

```yaml
ports:
  - "127.0.0.1:8889:8888"
  - "127.0.0.1:1081:1080"
```

После изменения:

```bash
docker compose down
docker compose up -d --no-build
```

Проверка:

```bash
curl -x http://127.0.0.1:8889 https://www.cloudflare.com/cdn-cgi/trace | grep warp=
curl --socks5-hostname 127.0.0.1:1081 https://www.cloudflare.com/cdn-cgi/trace | grep warp=
```

## Важно

### Память и аллокатор

В `docker-compose.yml` по умолчанию установлены:

- `MALLOC_TRIM_THRESHOLD_=1048576` и `MALLOC_MMAP_THRESHOLD_=1048576` —
  пороги glibc по 1 МиБ для компромисса между повторным использованием
  памяти и её возвратом ОС. Это не гарантия отсутствия утечек.
- `mem_limit: 256m` — жёсткий лимит RAM 256 МиБ для всего контейнера.
- `memswap_limit: 256m` — общий лимит RAM+swap равен лимиту RAM,
  поэтому дополнительный swap контейнеру не предоставляется.

При превышении доступной памяти возможен OOM kill. Лимит не заменяет
диагностику роста памяти. Эти настройки относятся к запуску через Compose;
для обычного `docker run` нужны соответствующие `--env`, `--memory` и
`--memory-swap`.

После изменения окружения/лимитов контейнер нужно пересоздать, а не просто
выполнить `docker restart`. Не удаляйте state volume при пересоздании.

WARP state лежит в Docker volume:

```text
warp_zapret1_state:/var/lib/cloudflare-warp
```
По умолчанию proxy доступен только с localhost.

Используется zapret1, если конфиг умрет, поменяйте в /config

## Стратегия zapret

`config/zapret1-warp.conf` адаптирован из `general.bat` проекта
`zapret-discord-youtube` версии `1.10.4`: семь основных профилей в исходном
порядке, без дополнительных профилей Game Filter (в исходном архиве он выключен).
Параметры WinDivert `--wf-tcp`/`--wf-udp` заменены существующими правилами
Linux OUTPUT/NFQUEUE; границы перехвата пакетов для WARP сохранены.
Списки доменов/IP и пользовательские исключения не заменялись.

Штатный вариант совпадает с проверенным `general`: UDP fake/repeats=6,
TCP multisplit с Google TLS/overlap=681 и 4pda TLS/overlap=568.
Payload-файлы сверяются по SHA-256 в регрессионных тестах.
Перед NFQUEUE устанавливается ACCEPT для пакетов с меткой nfqws
`0x40000000/0x40000000`, чтобы сгенерированные пакеты не попадали
на повторную обработку. Правило удаляется при завершении; повторная
установка не создаёт дублей.
Смена стратегии не является подтверждённым исправлением утечки памяти.

Конфиг и payload-файлы включены в образ: после изменения нужна пересборка.
До запуска WARP можно проверить аргументы настоящим nfqws без регистрации,
сетевого перехвата и изменения состояния:

```bash
docker compose build
docker compose run --rm --no-deps --entrypoint /usr/bin/bash warp-zapret1 -lc \
  'set -e; source /config/zapret1-warp.conf; exec /usr/local/bin/nfqws --dry-run --qnum="$NFQUEUE_NUM" "${NFQWS_ARGS[@]}"'
```

Регрессионная проверка профилей и payload-файлов (Python 3 и Docker):

```bash
python -m unittest discover -s tests -v
```

### Перенос на сервер

Проверенный локально туннель использовал сохранённую регистрацию.
Успешная новая регистрация через API и суточная стабильность пока не доказаны.
Сборка использует неприкреплённые версии upstream: проверяйте фактический
образ и версии WARP/nfqws, а не только имя стратегии.

Перед обновлением сохраните прежний образ, Compose/config и резервную копию
state volume при остановленном WARP. Не используйте `docker compose down -v`.
Начните с контролируемого запуска на целевом сервере и проверьте оба прокси,
память, OOM и журнал. Для отката нужны прежние образ/config и сохранённый state;
не запускайте одновременно два туннеля с копиями одной регистрации.
