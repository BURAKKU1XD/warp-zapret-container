# Сторонние компоненты

Лицензия [MIT](LICENSE) относится к собственному коду этого репозитория.
Она не заменяет лицензии зависимостей, WARP или сторонних файлов.

## zapret / nfqws

- Автор: bol-van.
- Исходники: <https://github.com/bol-van/zapret>.
- Лицензия MIT: [локальная копия](licenses/zapret-MIT.txt),
  [источник](https://github.com/bol-van/zapret/blob/master/docs/LICENSE.txt).
- `nfqws` собирается из исходников при сборке Docker-образа.
  Версия zapret1 находится в режиме EOL; переход на zapret2 здесь не выполнен.

## zapret-discord-youtube 1.10.4

- Авторы: Flowseal и bol-van.
- Релиз: <https://github.com/Flowseal/zapret-discord-youtube/releases/tag/1.10.4>.
- Лицензия: [полный текст из релиза](licenses/zapret-discord-youtube-1.10.4.txt),
  [источник](https://github.com/Flowseal/zapret-discord-youtube/blob/1.10.4/LICENSE.txt).
- 22 исходных BAT сохранены как тестовые fixtures; Linux-адаптации находятся
  в `config/strategies/` и `config/zapret1-warp.conf`.
- Восемь используемых payload зафиксированы SHA-256 в
  `tests/fixtures/strategies-1.10.4/manifest.json`. Остальные файлы в
  `assets/fake/` и списки в `assets/lists/` унаследованы из ранней версии
  контейнера; их точная исходная ревизия отдельно не зафиксирована.
  Manifest стратегий не является provenance-манифестом всех assets.
- В upstream license упоминается WinDivert. В этом репозитории и образе
  **нет WinDivert, winws.exe или Windows DLL/драйверов**; текст upstream
  сохранён целиком, а не является описанием состава данного контейнера.

## Cloudflare WARP

Клиент устанавливается из официального APT-репозитория:
<https://pkg.cloudflareclient.com/>.
Это стороннее ПО, а не код под MIT-лицензией этого проекта.
Действуют условия Cloudflare: <https://www.cloudflare.com/application/terms/>.
Регистрация и ключи WARP хранятся в отдельном томе состояния.

## Linux-пакеты в образе

Образ также содержит Bash, BusyBox, socat, curl, D-Bus, iptables, ipset
и динамические библиотеки из Ubuntu. Их лицензии различаются.
Dockerfile сохраняет доступные package copyright-файлы в `/usr/share/doc/`,
а уведомления этого проекта — в `/usr/share/doc/warp-zapret-container/`.

Workflow поддерживает публикацию проверенного образа в GHCR после тестов
для `main` и версионных тегов. Pull request не публикует образ.
Условия распространения определяются лицензиями установленных пакетов
и условиями WARP. Для компонентов под GPL/LGPL могут действовать требования
к предоставлению соответствующих исходников; ссылки и copyright-файлы
содержат сведения о лицензиях.
