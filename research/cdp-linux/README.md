# Linux Chrome + CDP: разовая попытка 2026-09-20

**Последний результат, 2026-09-20 13:28 МСК:** [автономный запуск после паузы](STARTUP-RESULT.md)
с сохранённым ручным профилем и 20 сек до CDP снова дал ограничение
(Navigation Timing: 429), 0 объявлений. Процессы остановлены, повторов нет.
Целевая архитектура только серверная. [Ручной контроль](MANUAL-RESULT.md)
и история первой автоматической попытки ниже сохранены для сравнения.

По новому указанию владельца выполнена одна попытка на Linux без предварительного
ручного контроля. Windows-контроль остаётся следующим шагом, не заменён этим опытом.

## Результат

20 сентября 2026, 00:30:14–00:30:15 МСК:

- Обычный Google Chrome Stable 153.0.8010.52, headful под Xvfb, sandbox включён.
- Отдельный свежий профиль `private/profile`, без импорта cookies, stealth и UA-подмен.
- Chrome запущен отдельным процессом, Playwright подключён через CDP
  `http://127.0.0.1:9222`, существующий контекст, `no_defaults=True`.
- Один `goto(https://www.avito.ru/kazan/noutbuki)` → **HTTP 403**.
- DOM-маркеры: «проблема с ip», «доступ ограничен». Объявлений: **0**.
- `navigator.webdriver=false` до/после навигации; это не доказательство отсутствия
  других признаков автоматизации. Не менялись language=en-US и viewport=1050×893.
- Повторов, API-вызовов из Python, карточек, пагинации и CAPTCHA-solving нет.
- [Диагностика](results/result.json); `results/attempt.claim` запрещает повтор.

Схема технически подключилась к Chrome, но доступ к выдаче не дала. Из этого
нельзя заключить ни глобальный IP-ban, ни обнаружение именно CDP: отсутствует
успешный ручной контроль того же профиля, а профиль новый и ОС другая.
Дальше — [подготовленный Windows-контроль](../cdp-windows/README.md).

## Окружение и ограничения

Пакет скачан с официального HTTPS URL Google:
`https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb`.
Распакован через dpkg-deb; пакетные pre/post-install скрипты не выполнялись.
Исходный deb и распакованная копия оставлены в `runtime/`.

Ubuntu AppArmor не позволил локальной копии из research использовать штатную
песочницу. До обращений к Avito проверен только about:blank; браузер прервался
с сообщением о sandbox. Затем Chrome скопирован в стандартный `/opt/google/chrome`,
владелец root:root, chrome-sandbox mode 4755. Существующий профиль AppArmor для
этого пути использован без изменений. `--no-sandbox` не применялся.
APT-репозитории, firewall, глобальные маршруты и системные настройки защиты не менялись.
Это локально распакованная установка: автоматические обновления не настроены.

Запросы шли через существующий CONNECT proxy `127.0.0.1:18081`, который использует
eno1/mark 0x2024 и не расшифровывает TLS. Журнал подтвердил CONNECT www.avito.ru
176.114.124.24 по этому транспорту. Публичный IP в этой попытке заново не измерялся.
Прокси разрешает только домены Avito и отклоняет прочие фоновые обращения Chrome;
это также отличие от обычной Windows-сессии. На уровне TCP Chrome создал два
CONNECT-соединения, но Python инициировал только одну навигацию документа.

После опыта Chrome/Xvfb завершены, transport service inactive,
порты 9222/18081 закрыты. Старый cooldown timer остаётся inactive.
Профиль и результат сохранены локально с закрытыми правами. Cookies, storage,
HTML, screenshots, trace и сообщения браузерной консоли в отчёт не попадали.

## Файлы и команды

Добавлены `run_once.py`, `.gitignore`, этот отчёт, runtime/private/results.
Код читает безопасные DOM-хелперы из `../cdp-windows/probe.py`, не изменяя его.
Основной collector также не изменён.

Безопасная проверка и чтение результата:

```bash
cd /home/asat/codex-work/avito-monitor
.venv/bin/python -m py_compile research/cdp-linux/run_once.py
cat research/cdp-linux/results/result.json
ss -ltn '( sport = :9222 or sport = :18081 )'
```

Фактически использованная команда (не повторять, claim сохранён):

```bash
bash start-direct.sh
.venv/bin/python research/cdp-linux/run_once.py --run-once
sudo systemctl stop avito-research-direct-v2.service
```

Новых unit-тестов не добавляли; проверены синтаксис, фактическое CDP-подключение,
однократная навигация и завершение процессов. Чтобы отключить runtime, достаточно
не запускать скрипт: фоновых служб Chrome/мониторинга нет. При будущем удалении
сначала сохранить result.json и проверить отсутствие процессов; автоматически
профиль и Chrome не удалялись.

Официальные документы:
[Chrome user-data-dir для CDP](https://developer.chrome.com/blog/remote-debugging-port),
[Playwright connect_over_cdp](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp).
