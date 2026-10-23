# Один опыт: Windows Google Chrome → CDP → Linux Playwright

**Результат 2026-09-20, 00:36 МСК:** ручная выдача работала; один переход через
CDP в той же Windows-сессии → HTTP 200, 50 DOM-элементов, сохранено 5 объявлений,
признаков ограничения за время проверки нет. [Результат и следующий этап](RESULT.md).
Ниже — исходная инструкция подготовки; первый запуск уже выполнен, claim сохранён.

Подготовлено 2026-09-20. Запросы к Avito при подготовке не выполнялись.
Это отдельный experimental transport; основной collector не меняется.

## Исходный факт и границы вывода

По подтверждению владельца, Windows Chrome открывает Avito с тем же публичным
IP 178.205.51.86 (Таттелеком AS28840), с которого Linux-клиенты получают ограничения.
Глобальная блокировка IP маловероятна; текст IP_BLOCKED сам по себе её не доказывает.
Работоспособность нового выделенного профиля ещё нужно подтвердить вручную.
Исторические рекомендации купить SPFA/прокси и вывод об однозначном IP-ban
не являются текущим направлением. Не покупаем, не меняем сеть, не решаем CAPTCHA.

Старый `avito-cooldown-once-20260920.timer` остановлен при подготовке:
timer и service inactive. Он не должен выполнять прежний API-тест параллельно.

`probe.py` основного проекта запускает persistent Chromium через локальный proxy
18081, задаёт locale/timezone/viewport, блокирует service workers и отключает QUIC.
Его capture сохраняет полный HTML/текст/снимок, context закрывается при выходе.
`extract.py` читает сохранённый DOM. Эти механизмы не подходят для чистого опыта
с чужим уже открытым Chrome и ограниченной диагностикой; новый скрипт изолирован.
Переиспользованы DOM-маркеры catalog-serp/item/item-title и проверка ID/ссылки.

## Схема и безопасность

```text
Linux 127.0.0.1:9222 → SSH reverse forwarding → Windows 127.0.0.1:9222
Linux Playwright       SSH соединение инициирует Windows       Google Chrome → Avito
```

На Windows не нужен SSH-сервер. SSH к Linux идёт через существующий Tailscale
(или LAN-адрес Linux). Tailscale не используется как exit node. DNS/TLS/HTTP к
Avito исполняет Windows Chrome. Одинаковый NAT не делает browser fingerprints одинаковыми.
Никаких port forwarding в роутере, `0.0.0.0:9222`, firewall allow 9222,
`--remote-allow-origins=*`, stealth, эмуляции fingerprint или импорта cookies.
CDP даёт управление всем выделенным профилем; не открывать в нём личные сайты,
не включать Chrome Sync. Доступ остаётся у локальных пользователей обеих машин.

Проверено на Linux: Tailscale IP 100.71.133.6, порт 9222 свободен,
`sshd -T`: `gatewayports no`, `allowtcpforwarding yes`, `disableforwarding no`.
После создания туннеля обязательно проверить реальный listener: Match/ключевые
ограничения SSH могут отличаться от общих параметров. Конфигурация SSH не менялась.

## Windows: запуск и ручной контроль

Скопировать `start-chrome.ps1` на Windows, прочитать, запустить в PowerShell:

```powershell
powershell.exe -NoProfile -File .\start-chrome.ps1
```

Если локальная execution policy запрещает скрипт, выполнить его проверенные
команды вручную в PowerShell; менять системную policy не требуется.
Скрипт найдёт обычный установленный Google Chrome и запустит:

```powershell
# Эквивалент для стандартной установки; не выполнять второй раз после скрипта.
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  "--user-data-dir=$env:LOCALAPPDATA\Avito-CDP-Research" `
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 about:blank
```

Личный Chrome может оставаться открытым. Если выделенный профиль уже открыт без
debugging, закрыть только его окна и запустить скрипт; не завершать все chrome.exe.
В `chrome://version` проверить Profile Path: внутри `Avito-CDP-Research`.

1. В выделенном Chrome вручную открыть `https://www.avito.ru/kazan/noutbuki`.
2. Дождаться видимых объявлений. Записать время, точный адрес и работает ли поиск.
   Не считать CAPTCHA/экран проверки успешной загрузкой, даже при HTTP 200.
3. Оставить ровно одну вкладку с этим **точным адресом без query/fragment**.
   Закрыть `chrome://version` и другие вкладки выделенного профиля.
4. Не перезапускать Chrome, не менять cookies/настройки и не трогать вкладку во
   время опыта. Переход CDP произойдёт в этой же вкладке и контексте.

Если ручной поиск не работает, остановиться: вариант `--manual-result blocked`
только сохраняет результат без подключения CDP и без обращений к Avito.

Проверка Windows listener и метаданных CDP (не обращается к Avito):

```powershell
Get-NetTCPConnection -State Listen -LocalPort 9222 | Select-Object LocalAddress,LocalPort,OwningProcess
Invoke-RestMethod http://127.0.0.1:9222/json/version | Select-Object Browser,Protocol-Version
```

Допустимы только 127.0.0.1 или ::1. Иначе закрыть выделенный Chrome, не продолжать.

## Windows: защищённый туннель

В отдельном PowerShell оставить работающую команду:

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:9222:127.0.0.1:9222 asat@100.71.133.6
```

Использовать обычную проверку SSH host key, не отключать её. При занятом порте
команда должна завершиться ошибкой; не подключаться к неизвестному listener.
Tailscale нужен на обеих машинах; в домашней сети можно заменить адрес сервера
на его известный LAN IP. Не использовать публичный IP роутера для CDP.

## Linux: проверка туннеля и единственный запуск

```bash
cd /home/asat/codex-work/avito-monitor
ss -ltn '( sport = :9222 )'
.venv/bin/python research/cdp-windows/probe.py --check-endpoint
```

Listener должен быть только loopback. `--check-endpoint` один раз читает локальный
`/json/version`, печатает только версию/протокол и проверку WebSocket адреса.
Он не подключает Playwright к вкладкам и не делает запросов к Avito.
Если проверка не прошла, исправить Chrome/SSH, не запускать collector.

После успешного ручного контроля **один раз**:

```bash
.venv/bin/python research/cdp-windows/probe.py --run-once --manual-result works
cat research/cdp-windows/results/result.json
```

При неуспешном ручном контроле вместо этого:

```bash
.venv/bin/python research/cdp-windows/probe.py --run-once --manual-result blocked
```

`results/attempt.claim` атомарно запрещает повтор/параллельный запуск, в том
числе после ошибок подключения. Не удалять его для повторных проб: сначала
разобрать результат. Exit 0 только при успехе; остальные исходы exit 2.
Установлен Playwright 1.63.0 из основного requirements.txt; дополнительных
пакетов и установки Chromium на Linux для этого опыта не требуется.

## Что делает и сохраняет тест

`connect_over_cdp(no_defaults=True, is_local=False)` → `browser.contexts[0]` →
контроль существующей вкладки без навигации → один `page.goto()` → ограниченное
ожидание DOM (до 15 секунд после DOMContentLoaded, navigation timeout 30 секунд).
Проверяет ограничения до goto; если после attach уже CAPTCHA, goto не выполняется.
Не создаёт новый контекст; не задаёт headers, UA, viewport, proxy, init scripts.

Максимум пять объявлений из DOM: ID, название, цена, ссылка без query/fragment.
Никаких прямых API-запросов, пагинации, scroll, кликов, карточек, retries или
циклических запусков. Сама страница Chrome может загружать свои API/ресурсы:
одна навигация не равна одному HTTP-запросу. DOM polling не повторяет navigation.
Chrome и вкладка остаются открытыми после отключения, для визуального осмотра.
После опыта записать наблюдение и закрыть выделенный Chrome, чтобы остановить
его фоновые запросы; Ctrl+C в окне SSH закроет туннель.

JSON: время, ручной исход, версии Chrome/Playwright, статус основного документа,
цепочка document statuses, безопасные URL, число DOM-элементов/извлечённых строк,
признаки ограничения, счётчики 403/429/439, readyState, viewport/language/webdriver,
ошибки только по имени класса. Query/fragment и неизвестные пути редиректов скрыты.
Cookies и LocalStorage вообще не читаются; нет HTML/screenshot/HAR/trace/console
или exception text. Не запускать с DEBUG=pw:protocol или сторонним сетевым логированием.

Ручной HTTP status до attach неизвестен: наблюдение владельца — отдельное
свидетельство. Снимок `after_attach` уже сделан через CDP, не является измерением
«до CDP». Подключение и чтение DOM сами могут быть заметны. При различии нельзя
по этому одному опыту назвать конкретную причину (TLS, cookies или детектор CDP).

## Критерии и следующий этап

| Ручной контроль | CDP | Вывод |
|---|---|---|
| Не работает | Не запускать | Контрольная среда не подтверждена |
| Работает | После attach уже CAPTCHA | Отдельный исход manual_ok_attach_blocked; goto отсутствует |
| Работает | Единственный goto → CAPTCHA/403/429/439 | manual_ok_cdp_navigation_blocked; повторов нет |
| Работает | HTTP 200, нужный URL, каталог, ≥1 валидная строка, нет ограничений | success: гипотеза подтверждена для одного опыта |
| Работает | Таймаут, нет DOM/строк, частичные ограничения ресурсов | Неопределённо; не объявлять IP-ban или успех |

При успехе следующий **отдельно запускаемый** этап: две ручные по запуску проверки
той же выдачи с паузами минимум 60 минут, тем же профилем и теми же ограничениями.
Подготовить отдельный протокол/guard, сохранить первый claim и результат, сначала
сравнить с ними. Сейчас этот этап не реализован и не назначен; мониторинга нет.
При блокировке сохранить текущий экран визуально, не перезагружать и не решать
CAPTCHA автоматически; проанализировать attach/navigation и document statuses.

## Безопасные локальные проверки

```bash
cd /home/asat/codex-work/avito-monitor
.venv/bin/python -m py_compile research/cdp-windows/probe.py research/cdp-windows/test_probe.py
.venv/bin/python -m unittest discover -s research/cdp-windows -p 'test_*.py' -v
.venv/bin/python research/cdp-windows/probe.py --help
```

Тесты используют mocks/tempdir, не подключаются к реальному Chrome или Avito.
При подготовке прошли py_compile, шесть unit-тестов и дополнительная локальная
проверка настоящего DOM в offline Chromium: извлечение синтетического объявления,
удаление query с тестовым секретом, распознавание IP_BLOCKED. Это проверка
JavaScript-парсера на set_content, не перенос контрольного опыта на Linux.
Windows запуск и настоящий SSH→Chrome опыт требуют ручной работы на ПК и ещё
не подтверждены. Git-репозитория в avito-monitor нет; commit не создавался.

## Официальные источники

- [Playwright connect_over_cdp](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp): существующий default context, `no_defaults` (с 1.60), удалённый `is_local=False`; CDP имеет ограничения совместимости.
- [Chrome: remote debugging с версии 136](https://developer.chrome.com/blog/remote-debugging-port): необходим нестандартный user-data-dir; личный профиль не использовать.
- [OpenSSH -R](https://man.openbsd.org/ssh#R), [GatewayPorts](https://man.openbsd.org/sshd_config#GatewayPorts): reverse forwarding и loopback binding.
