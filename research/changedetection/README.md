# changedetection.io: разовая проверка Авито

2026-09-20, 13:43:29–13:43:45 МСК. **HTTP 429, 0 объявлений.**
Штатный браузерный загрузчик changedetection.io не обеспечил доступ в этом опыте.

## Что проверено

- Официальный образ changedetection.io **0.60.7**, закреплённый digest:
  `sha256:096dae27b5d677b89f0e810fff95a70403271aa3ff3b6437952d2db9be7e74c5`.
- Оригинальный `changedetectionio.content_fetchers.puppeteer.fetcher` из образа,
  без планировщика, веб-интерфейса, уведомлений и покупки внешних услуг.
- Существующий Google Chrome **153.0.8010.52** под Xvfb, sandbox включён,
  CDP только `127.0.0.1:9225`; контейнер с host network, UID 1000,
  `cap-drop=ALL`, `no-new-privileges`.
- Отдельная копия `cdp-linux/private/manual-profile` в `private/live-profile`.
  Исходный профиль не изменялся. Windows не использовался.
- Прямой транспорт `127.0.0.1:18081`, eno1/SO_MARK `0x2024`.
  Журнал подтвердил CONNECT www.avito.ru к 176.114.120.24 по этому пути.
  Глобальная маршрутизация не менялась; публичный IP повторно не измерялся.
- Один `goto` на `https://www.avito.ru/kazan/noutbuki`.
  Зарегистрирован один document response: 429. Перед извлечением штатное
  ожидание загрузчика — 15 секунд; каталог отсутствует, маркеры
  «доступ ограничен», «проблема с ip». Ошибка `Non200ErrorCodeReceived`.

## Ограничения интерпретации

Это проверка реального загрузчика changedetection.io с локальным Chrome,
а не полного варианта Compose с sockpuppetbrowser. Развёртывание UI и
проверка sockpuppetbrowser не выполнялись. Результат нельзя переносить
на все настройки changedetection.io или считать доказательством постоянного IP-ban.

Пауза относительно предыдущего известного запроса в 13:27 составляла около
16 минут. Длительный cooldown этим опытом не проверен. Копия сохраняет
дисковый профиль, но не живое состояние прошлой ручной сессии.
Puppeteer-режим использует собственную штатную настройку браузерного окружения
(в том числе установленный в образе pyppeteerstealth); это не чистый контроль
единственного признака. Sockpuppetbrowser не нужен для самого CDP-подключения.

## Защита и фактические проверки

- Сначала локальный HTTP fixture: 200, один элемент, один goto; внешний
  CONNECT-прокси остановлен. Первоначальные ошибки путей импорта контейнера
  исправлены до обращения к Авито.
- Ограничитель перехватывает `Page.goto` и запрещает второй вызов до браузера;
  отдельная offline-проверка запрета прошла. Самопереходы сайта не являются
  вызовами goto; document responses учитываются отдельно.
- `results/live/attempt.claim` запрещает повторный live-запуск. Повторная
  команда действительно отклонена до запуска Chrome и сети.
- `py_compile` прошёл. Контейнер, Chrome/Xvfb и прямой транспорт завершены;
  9225/18081 закрыты. Активных таймеров Авито не обнаружено.
- Секреты, HTML и cookies не выводились в журналы/отчёт. Результаты и
  профиль доступны владельцу в каталогах с закрытыми правами.

## Чтение результата и проверка

```bash
cd /home/asat/codex-work/avito-monitor
cat research/changedetection/results/live/result.json
python3 -m py_compile research/changedetection/run_once.py research/changedetection/fetch_once.py
ss -ltn '( sport = :9225 or sport = :18081 )'
```

`python3 research/changedetection/run_once.py` запускает только локальный
fixture и обновляет его offline-результат. Фактический онлайн-опыт выполнен
с `--live`; claim не удалять и для новых попыток не обходить.
Откат не требуется: постоянных служб нет. Образ Docker и исследовательские
файлы сохранены; скачанный образ занимает около 1.66 GB по Docker image ls.

Следующий полезный опыт: наблюдать успешное ручное открытие на Linux с
диагностикой прямого транспорта, затем читать уже готовый DOM без новой
навигации (см. `../cdp-linux/LOCAL-AUDIT.md`). Автоматизация этого успешного
пути пока не доказана; менять весь проект на changedetection.io оснований нет.

Источники: [официальный Compose](https://github.com/dgtlmoon/changedetection.io/blob/master/docker-compose.yml),
[Puppeteer fetcher](https://github.com/dgtlmoon/changedetection.io/blob/master/changedetectionio/content_fetchers/puppeteer.py),
[sockpuppetbrowser: профили](https://github.com/dgtlmoon/sockpuppetbrowser#profile-directories---user-data-dir).
