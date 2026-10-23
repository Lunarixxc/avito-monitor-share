# Проверка Duff89/parser_avito — 2026-09-19

**Цель по последующему указанию владельца: полный переход на parser_avito, если
он обеспечит рабочий сбор.** Разрешена смена реализации целиком. Рабочее получение
свежей выдачи пока не подтверждено: без cookies HTTP 439, с cookies профиля HTTP 403.
Ниже сохранён исходный анализ; новые результаты и запуск — в FOLLOWUP.md.

Исследован master, commit `a3ebe93579bd25e2b3e8ccf7529c65d920c85ac8`,
версия 3.2.22, commit от 2026-08-20. README описывает ещё 3.2.21;
документация местами упоминает httpx, но фактический транспорт — curl_cffi.
Исходный checkout не изменён. В avito-monitor нет Git-репозитория.

## Реальная цепочка получения search results

В текущем `AvitoParse.parse()`:

1. Для каждой обычной поисковой ссылки вызывается `AvitoUrlConverter.convert()`.
2. При отсутствии локального кеша выполняется POST
   `https://spfa.pro/api/avito-url/`, JSON `{"url": "<search URL>"}`.
   Ключ для этой операции код не передаёт. Кеш: `storage/avito_api_urls.json`.
   Между конвертациями выдерживается минимум 31 секунда.
3. Полученный `api_url` запрашивается `HttpClient` с `page=1,2,...`.
   `_api_url_for_page()` удаляет `p` и `page`, добавляет именно `page`.
4. Ответ читается как JSON. `_extract_api_catalog()` ищет список `items`
   в `catalog`, `result.catalog`, `result` или корне ответа.
5. `ItemsResponse`/`Item` валидируют ответ; затем фильтрация, уведомления,
   отметки просмотренных в SQLite и экспорт. Получение просмотров/телефонов
   вынесено в дополнительные операции и для search collector не требуется.

**Конвертер вызывается безусловно**, даже при `use_bypass_api=False` и отсутствии
cookies. Поэтому текущая цепочка не является полностью автономной от SPFA.
Кеш устраняет повторные конвертации уже известных URL, но не зависимость новых.

Код и changelog называют это мобильным API. Фактически 19 сентября конвертер для
`https://www.avito.ru/kazan/noutbuki` вернул **`/web/1/js/items`** на www.avito.ru.
Это наблюдавшийся внутренний endpoint, а не подтверждённый публичный API.
Ключи query: categoryId, locationId, localPriority, presentationType, sort.
Значения categoryId=98, locationId=650400, localPriority=0; соответствие строгой
географии и сортировке не проверено успешной выдачей.

Источники: [основной цикл](https://github.com/Duff89/parser_avito/blob/a3ebe93579bd25e2b3e8ccf7529c65d920c85ac8/parser_cls.py#L135),
[конвертер](https://github.com/Duff89/parser_avito/blob/a3ebe93579bd25e2b3e8ccf7529c65d920c85ac8/parser/url_converter.py),
[changelog](https://github.com/Duff89/parser_avito/blob/a3ebe93579bd25e2b3e8ccf7529c65d920c85ac8/docs/CHANGELOG.md).

## data-mfe-state / loaderData

`find_json_on_page()` всё ещё существует, но основной цикл её **не вызывает**.
Она ищет `<script type="mime/invalid" data-mfe-state="true">`, исключает скрипты,
содержащие строку `sandbox`, выполняет HTML unescape и JSON decode, требует
`i18n.hasMessages`, возвращает `loaderData.data`. Обычно каталог искали бы
в `loaderData.data.catalog.items`. Это данные внутри HTML, не отдельный способ
доступа, позволяющий получить выдачу из страницы блокировки.

Точный метод upstream исполнен отдельно через AST без импорта всего приложения:

- На нашем успешном сохранённом HTML возвращает `{}`; DOM-парсер даёт 50 ID,
  из них 36 с URL Казани и 14 других городов.
- В этой копии **0 элементов data-mfe-state**. Есть
  `window.__staticRouterHydrationData`, но его `loaderData` содержит маршруты
  `0` и `catalog-or-main-or-item`; в последнем только `topBanner` и
  `buyerLocationProps`, а не список объявлений.
- На сохранённой странице блокировки метод также возвращает `{}`.
- На синтетическом HTML ожидаемого формата метод работает.
- `{}` нельзя трактовать как успешный поиск с нулём объявлений.

Ограничение сравнения: наша копия получена через Playwright `page.content()`.
Это DOM после выполнения JavaScript. Исходный HTTP response body успешной
загрузки не сохранён; нельзя утверждать, что MFE-state отсутствовал до hydration.

Источник: [метод извлечения](https://github.com/Duff89/parser_avito/blob/a3ebe93579bd25e2b3e8ccf7529c65d920c85ac8/parser_cls.py#L230).

## HTTP client, cookies, блокировки

- `curl_cffi.requests.Session(impersonate=...)`, по умолчанию `chrome`.
  Сессия переиспользует соединения и cookie jar. Собственный JS не исполняется.
  По умолчанию UA Chrome/150, sec-ch-ua Chrome/149; фактический профиль curl
  зависит от версии незакреплённой зависимости. Совпадение fingerprint не доказано.
- Прокси задаётся в сессии отдельно для http/https. Глобальные маршруты
  менять для этого не требуется. `NoProxy` не гарантирует прямой выход при
  системном TUN или переменных окружения; наш прямой переходник нужен явно.
- Нет provider: начальных cookies нет, ответные cookies живут в сессии.
- `OwnCookiesProvider`: JSON `storage/own_cookies.json`, словарь name/value,
  объединение Set-Cookie и сохранение на диск. Domain/path/expiry браузерного
  профиля не сохраняются. Не переопределяет методы UA/fingerprint. При блокировке
  только логирование и ожидание 10 секунд; разблокировки сам не выполняет.
- Playwright присутствует в вспомогательных сценариях получения cookies/входа,
  но не загружает search results в текущем основном цикле. Постоянный Chromium
  profile нашего проекта автоматически не импортируется.
- `ExternalApiCookiesProvider`: отдельный сервис SPFA, покупка через
  `/api/cookies/mobile/`, восстановление через `/api/unblock/`, сохранение cookies,
  UA и fingerprint. Сервису передаются API key и строка прокси. CLI требует
  proxy_string для этого режима. Локальный 127.0.0.1:18081 не является внешним
  прокси, доступным SPFA. Этот режим не подключался, покупок не было.
- На 403/429/**439** — повторы; при threshold=3 вызываются обработчики cookies
  и proxy, сессия пересоздаётся. MobileProxy вызывает URL смены IP; ServerProxy
  и NoProxy ничего не меняют. По умолчанию 5 попыток с паузами по 5 секунд.
- После исчерпания попыток RuntimeError; fetch ловит исключение и возвращает
  None. Верхний CLI способен продолжать циклы. Нет постоянного stop-флага,
  анализа Retry-After или надёжного распознавания challenge с HTTP 200.
  В проверенном config паузы между ссылками/циклами по 1 секунде.

Источники: [HTTP client](https://github.com/Duff89/parser_avito/blob/a3ebe93579bd25e2b3e8ccf7529c65d920c85ac8/parser/http/client.py),
[собственные cookies](https://github.com/Duff89/parser_avito/blob/a3ebe93579bd25e2b3e8ccf7529c65d920c85ac8/parser/cookies/own_cookies.py),
[SPFA provider](https://github.com/Duff89/parser_avito/blob/a3ebe93579bd25e2b3e8ccf7529c65d920c85ac8/parser/cookies/external_api.py).

## Результат тестов

| Проверка | Фактический результат |
|---|---|
| Сохранённая реальная выдача, наш DOM parser | 50 объявлений |
| Тот же HTML, точный MFE-метод upstream | 0; отсутствует ожидаемый state |
| Синтетический MFE-state | Успех |
| Четыре JSON-обёртки каталога и параметр page=2 | Успех, синтетические данные |
| Pydantic Item на минимальном JSON | Принимает ID/title без цены/URL: нужна наша дополнительная валидация |
| Mock HTTP 403, 429, 439 | По 5 запросов, 1 вызов block handler, RuntimeError |
| Mock HTTP 200 | 1 запрос, возвращён response |
| Живой SPFA URL converter, 18:57 MSK | HTTP 200, api_url получен |
| Живой GET полученного API, напрямую | HTTP 439, HTML «Доступ ограничен: проверка безопасности» |

Онлайн-запрос исполнен оригинальным HttpClient с max_retries=1, retry_delay=0,
block_threshold=2 и allow_redirects=False. Начальных cookies не было; рабочий
browser profile не открывался. Ответ сохранён до того, как клиент выбросил
RuntimeError. **Свежая выдача не получена.** HTTP 439 не назван IP_BLOCKED:
в теле есть проверка безопасности, но нет сообщения «проблема с IP».

Транспорт: существующий direct_proxy.py, CONNECT www.avito.ru →
176.114.120.24, interface=eno1, SO_MARK=0x2024. Это подтверждено journalctl;
маршрут с этой меткой через eno1. После теста временный unit остановлен,
порт 18081 закрыт. Глобальные маршруты, VPN и firewall не изменялись.

Это тест отдельных компонентов, **не полный успешный запуск upstream**.
Не проверены: действующие cookies нашего профиля в curl, SPFA cookies/fingerprint,
реальная валидация API-items, пагинация, карточки и стабильные повторные циклы.
Нельзя по одному запросу без cookies доказать невозможность всех вариантов.

## Что можно встроить

| Аспект | Наш avito-monitor | parser_avito |
|---|---|---|
| Получение | Chromium + постоянный профиль | HTTP session + API URL converter |
| Извлечение | catalog-serp в DOM | JSON items; MFE-метод не используется |
| Состояние | Полный профиль | Cookie jar, опционально JSON/внешний provider |
| Блокировка | Stop-флаг, остановка | Повторы, обработчики cookies/прокси |
| Зависимости collector | Playwright, bs4 | curl_cffi, converter, models и провайдеры |

Рекомендуемый следующий технический шаг — отдельный экспериментальный адаптер
`fetch → classify → parse → normalize`, если появится проверенный способ доступа
к endpoint. Оставить наш stop-флаг; добавить классификацию 439/HTML challenge;
выдавать отдельно blocked, schema_error, confirmed_empty и success. Нормализовать
ID/URL/цену/город/время проверки в наш формат. Сначала требовать одну успешную
выдачу и совпадение фильтров, затем проверять пагинацию и повторяемость.

Импортировать весь `AvitoParse` не рекомендуется: он создаёт SQLite, notifier,
exporter и файловый logger. Малый collector выделить технически возможно;
полностью переносить GUI, Telegram, Excel и цикл приложения не нужно.
В проверенном checkout нет LICENSE/COPYING; условия копирования кода нужно
уточнить перед распространением заимствованного кода.

**Без изменения глобального маршрута — технически совместимо с нашей сетью.**
**На текущем прямом выходе — успешный сбор не подтверждён.** Замена движка
и чтение JSON сами по себе не устраняют проверку доступа Avito.

## Артефакты и воспроизведение

- `upstream/`: неизменённый checkout указанного commit.
- `check_offline.py`: реальные сохранённые HTML + синтетические/mock проверки.
- `probe_http.py`: явно включаемый однократный сетевой эксперимент.
- `requirements-tested.txt`: точные версии минимального исследовательского venv.
- `evidence/offline.json`: результат офлайн-проверок.
- `evidence/2026-09-19T15-57-02.158081+00-00/summary.json`: сетевой результат.
- Сырые ответы/API URL находятся только в закрытой evidence-директории.

Повторить безопасные проверки без обращений к Avito:

```bash
cd /home/asat/codex-work/avito-monitor/research/parser-avito
.venv/bin/python check_offline.py
.venv/bin/python -m py_compile check_offline.py probe_http.py
.venv/bin/python -m pip check
git -C upstream status --short
```

Автоматических live-повторов не настроено. Для нового окружения:
`python3 -m venv .venv`, затем
`.venv/bin/python -m pip install --only-binary=:all: -r requirements-tested.txt`.
Полный requirements upstream не устанавливался: в нём старые GUI/browser
зависимости, ненужные для этого эксперимента. Python исследования — 3.14.4.
