# warp-zapret-container

Локальный HTTP- и SOCKS5-прокси через Cloudflare WARP с zapret/nfqws.
WARP работает по MASQUE; через него идут только приложения с настроенным
прокси, а не весь трафик хоста.

**Проект экспериментальный.** Подключение зависит от сети: первая регистрация
WARP без внешнего VPN и длительная стабильность не гарантируются.

| Прокси | Адрес |
|---|---|
| HTTP / HTTPS CONNECT | `http://127.0.0.1:8888` |
| SOCKS5 | `socks5h://127.0.0.1:1080` |

## Требования

- Linux x86-64/amd64 с Docker Engine и Compose v2 либо Docker Desktop
  в режиме Linux-контейнеров. ARM64 не поддерживается.
- Доступный `/dev/net/tun`, поддержка ядром iptables/NFQUEUE и connbytes.
  Capabilities `NET_ADMIN` и `NET_RAW` уже заданы в Compose.
- Доступ к GitHub, Ubuntu и Cloudflare APT при сборке, к WARP API
  и серверам WARP при запуске.

## Как запустить

Команды рассчитаны на Bash. Скачайте проект и создайте `.env`:

```bash
git clone https://github.com/BURAKKU1XD/warp-zapret-container.git
cd warp-zapret-container
cp .env.example .env
```

В `.env` выберите стратегию. По умолчанию:

```dotenv
ZAPRET_STRATEGY=general
```

Соберите и запустите контейнер:

```bash
docker compose up -d --build
docker compose logs -f warp-zapret1
```

Дождитесь `[+] ready`, затем проверьте прокси командами ниже.
`Ctrl+C` закрывает просмотр логов, контейнер продолжает работать.
При первом запуске создаётся регистрация WARP, затем используется сохранённая.

## Список стратегий

В образ включены **22 стратегии** из `zapret-discord-youtube` версии `1.10.4`:

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

Начните с `general`. `alt5` помечен автором исходника как **NOT RECOMMENDED**,
`exp` — экспериментальный. Универсально рабочей стратегии нет,
автоматического перебора тоже нет.

### Как сменить стратегию

Измените существующую строку в `.env`, например:

```dotenv
ZAPRET_STRATEGY=alt12
```

Пересоздайте контейнер:

```bash
docker compose up -d --no-build --force-recreate
```

Пересборка не нужна: все стратегии уже в образе.
`docker compose restart` изменения `.env` не применяет.
Конфиги находятся в [config/strategies](config/strategies).

## Проверка

Проверьте HTTP и SOCKS5 с хоста:

```bash
curl --fail --show-error --max-time 15 --noproxy "" -x http://127.0.0.1:8888 https://www.cloudflare.com/cdn-cgi/trace
curl --fail --show-error --max-time 15 --noproxy "" --socks5-hostname 127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace
```

В обоих ответах должна быть строка `warp=on`. В PowerShell при необходимости
используйте `curl.exe`. Полный ответ содержит внешний IP — не публикуйте его.

Состояние контейнера и встроенная проверка обоих прокси:

```bash
docker compose ps
docker exec warp-zapret1 /healthcheck.sh
```

`Connected` и `[+] ready` сами по себе не доказывают доступность сайтов.
Healthcheck проверяет HTTPS через оба прокси каждые 30 секунд.
**Статус `unhealthy` не вызывает автоматический перезапуск контейнера.**

## Запуск без сборки: GHCR

Если образ опубликован и доступен вам, задайте в `.env`:

```dotenv
WARP_ZAPRET_IMAGE=ghcr.io/burakku1xd/warp-zapret-container:latest
ZAPRET_STRATEGY=general
```

Затем вместо локальной сборки:

```bash
docker compose pull
docker compose up -d --no-build
```

Это настроенный адрес публикации, а не гарантия доступности пакета.
Для анонимного скачивания владелец должен сделать пакет Public;
приватный пакет требует входа в GHCR. Токены в `.env` и Git не сохраняйте.

## Управление

```bash
# Остановить.
docker compose stop

# Запустить остановленный контейнер.
docker compose start

# Удалить контейнер и сеть, сохранив регистрацию.
docker compose down
```

Чтобы изменить порты, замените левую часть проброса в `docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:8889:8888"
  - "127.0.0.1:1081:1080"
```

Примените через `docker compose up -d --no-build --force-recreate`.
В этом примере подключайтесь к HTTP на `8889`, SOCKS5 на `1081`.

## Важно

- Прокси **без аутентификации**. Сохраняйте привязку портов к `127.0.0.1`;
  другие контейнеры в общей Docker-сети также могут иметь к нему доступ.
- Регистрация хранится в томе `warp_zapret1_state` в `/var/lib/cloudflare-warp`.
  **Не выполняйте `docker compose down -v`**: это удалит том регистрации.
  При смене имени Compose-проекта убедитесь, что подключается прежний том.
- Перед обновлением сохраните прежний образ, конфиги и резервную копию
  тома при остановленном WARP. Не публикуйте state, `.env` и сырые логи.
- В Compose установлен лимит RAM **256 МиБ**, без дополнительного swap.
  При нехватке памяти возможен OOM kill.
- Обход через IPv6 и SOCKS5 UDP relay не реализованы.
- Конфиги и списки включены в образ; после их правки нужна пересборка.
  Пользовательский `CONFIG` исполняется как Bash-код: используйте только
  доверенные файлы.

Подробнее о безопасности — [SECURITY.md](SECURITY.md).

## Для разработки

После сборки образа запустите тесты (Python 3.10+ и Docker):

```bash
python -m unittest discover -s tests -v
```

Тесты не регистрируют WARP и не доказывают работоспособность обхода
у конкретного провайдера.

## Лицензия

Собственный код — [MIT](LICENSE). Сторонние компоненты и условия
их распространения — [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
MIT не распространяется автоматически на клиент WARP и Linux-пакеты.
