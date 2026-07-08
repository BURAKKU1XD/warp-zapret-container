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

WARP state лежит в Docker volume:

```text
warp_zapret1_state:/var/lib/cloudflare-warp
```
По умолчанию proxy доступен только с localhost.

Используется zapret1, если конфиг умрет, поменяйте в /config
