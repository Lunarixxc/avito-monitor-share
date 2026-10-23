"""Persistent cooldowns and small, retryable collection health notifications."""
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

from tracking import timestamp

HOURS = (9, 11, 13, 15, 17, 19, 21)


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def retry_at(pause):
    # Migrate the former indefinite pause on read, without erasing its evidence.
    return timestamp(pause.get('retry_after') or pause.get('at')) + (
        dt.timedelta() if pause.get('retry_after') else dt.timedelta(hours=2))


def retry_due(pause, now):
    return not pause or timestamp(now) >= retry_at(pause)


def next_retry_slot(pause):
    after = retry_at(pause).astimezone(ZoneInfo('Europe/Moscow'))
    for offset in (0, 1):
        day = after + dt.timedelta(days=offset)
        for hour in HOURS:
            slot = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot >= after:
                return slot


def queue_health(folder, text, now):
    path = folder / 'collection_health.json'
    events = read(path, [])
    events.append({'at': now, 'text': text, 'sent': False})
    save(path, events)


def fail(folder, now, reason):
    path = folder / 'paused.json'
    previous = read(path, {})
    attempts = int(previous.get('attempts', 0)) + 1
    base = max(1, int(os.environ.get('AVITO_RETRY_BASE_HOURS', '2')))
    cap = max(base, int(os.environ.get('AVITO_RETRY_MAX_HOURS', '24')))
    hours = min(cap, base * 2 ** min(attempts - 1, 10))
    started = timestamp(now)
    local = started.astimezone(ZoneInfo('Europe/Moscow'))
    # Scheduled runs start a few seconds after the hour. Anchor their cooldown
    # to the scheduled slot so the next timer firing is not skipped.
    if local.hour in HOURS and local.minute < 5:
        started = local.replace(minute=0, second=0, microsecond=0)
    after = started + dt.timedelta(hours=hours)
    pause = {'at': now, 'reason': reason, 'attempts': attempts,
             'retry_after': after.isoformat()}
    save(path, pause)
    local = next_retry_slot(pause)
    queue_health(folder, '⚠️ Обновление Авито приостановлено: ' + reason + '.\n'
                 f'Следующая контрольная попытка: '
                 f'{local:%d.%m %H:%M} МСК. Старые варианты будут помечены в топе.', now)
    return pause


def recover(folder, now):
    path = folder / 'paused.json'
    if not path.exists():
        return
    archive = folder / 'pause-history.jsonl'
    with archive.open('a') as stream:
        stream.write(json.dumps({'pause': read(path), 'recovered_at': now}) + '\n')
    path.unlink()
    queue_health(folder, '✅ Доступ к Авито восстановлен: выдача и карточки прочитаны. '
                 'Плановый сбор продолжается.', now)


def deliver_health(folder, sender=None):
    import notify
    values = notify.config()
    if not notify.ready(values):
        return {'status': 'not_configured', 'sent': 0}
    sender = sender or notify.send
    path = folder / 'collection_health.json'
    events = read(path, [])
    count = 0
    for event in events:
        if event['sent']:
            continue
        try:
            sender(event['text'], values)
        except Exception as exc:
            return {'status': 'error', 'error_type': type(exc).__name__, 'sent': count}
        event['sent'] = True
        save(path, events)
        count += 1
    return {'status': 'ok', 'sent': count}
