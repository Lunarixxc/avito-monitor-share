"""Scheduled laptop category snapshots, price history and Telegram notifications."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from filters import classify, MIN_PRICE
import recovery

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "results" / "monitor"
MOSCOW = ZoneInfo("Europe/Moscow")
HOURS = recovery.HOURS


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


from lifecycle import reconcile


def save_report(report):
    lines = ["# Мониторинг ноутбуков Казани", "", f"Снимок: {report['snapshot']}", "",
             f"Просмотрено объявлений: {report['observed']}; страниц: {report.get('pages', 0)}; "
             f"карточек проверено в этом цикле: {report.get('cards_checked', 0)}.",
             f"Бюджет {MIN_PRICE:_}–65 000 ₽, RTX 3050+ и RTX 2070, приоритет RTX 4050. Казань по URL.".replace('_', ' '),
             "Кандидаты без прочитанной карточки ещё не готовы к отправке. Состояние unknown требует проверки.",
             "Ассортимент и примерные комплектации исключаются; полнота и наличие не гарантированы.", "",
             "| Объявление | ₽ | Состояние |", "|---|---:|---|"]
    for item in report["candidates"]:
        title = str(item["title"]).replace("|", " ").replace("[", "(").replace("]", ")")
        lines.append(f"| [{title}]({item['url']}) | {item['price']} | {item['flags']['condition']} |")
    lines += ["", f"Изменений подходящих объявлений: {len(report['changes'])}.",
              "Отсутствие на проверенных страницах не считается продажей или снятием объявления."]
    if report.get('search_coverage'):
        coverage = report['search_coverage']
        pages = ', '.join(str(page) for page in coverage['pages'])
        lines += ["", f"Поиск по категории без ключевых слов; страницы: {pages}.",
                  f"Следующая страница глубокого обхода: {coverage['next_page']}; "
                  f"карточек ожидают первичного чтения: {report['pending_details']}.",
                  "Полный охват категории не подтверждён."]
        if coverage['fresh_window'] == 'no_overlap':
            lines.append("Свежие страницы не пересеклись с предыдущим циклом: возможен пропуск объявлений.")
        if coverage['sweep_status'] == 'page_unavailable':
            lines.append("Глубокая страница отклонена HTTP 400/404; первые три обработаны, обход вернётся к странице 4.")
    from notify import render
    for item in report["candidates"]:
        if item.get("analysis") and (item.get("card") or {}).get("parsed"):
            lines += ["", "---", "", render(item)]
    (STATE / "latest.md").write_text("\n".join(lines) + "\n")


def scheduled_window(now):
    return now.hour in HOURS and now.minute <= 5


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scheduled", action="store_true")
    group.add_argument("--import-result", type=Path, help="Offline import of a successful snapshot")
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.scheduled and not args.import_result:
        print(json.dumps({"schedule": "09,11,13,15,17,19,21:00; топ 20:00 Europe/Moscow", "pages": 4,
                          "min_price": MIN_PRICE, "max_price": int(os.environ.get("AVITO_MAX_PRICE", "65000")),
                          "paused": (STATE / "paused.json").exists(), "mode": "offline"}))
        return 0
    with (STATE / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another collection is active; skipped")
            return 0
        if args.scheduled:
            now = dt.datetime.now(MOSCOW)
            if not scheduled_window(now):
                print("Outside scheduled window; skipped")
                return 0
            recovery.deliver_health(STATE)
            pause = recovery.read(STATE / 'paused.json')
            if not recovery.retry_due(pause, now.isoformat()):
                print('Collection cooling down; no requests')
                return 0
            run_id = now.strftime("scheduled-%Y%m%d-%H")
            run_dir = ROOT / "results" / run_id
            if (run_dir / "live.claim").exists():
                print("This slot was already attempted; skipped")
                return 0
            command = [sys.executable, str(ROOT / "run_once.py"), "--live", "--run-id", run_id,
                       "--pages", "1" if pause else "4", "--details", "3" if pause else "12"]
            if pause or now.hour in (17, 19):
                command.append('--prepare-digest')
            try:
                env = dict(os.environ)
                env["AVITO_SEARCH_QUERY"] = ""
                env["AVITO_SORT"] = "104"
                run = subprocess.run(command, cwd=ROOT, timeout=170, check=False, env=env)
                result = json.loads((run_dir / "summary.json").read_text())
                if run.returncode or result.get("status") != "success":
                    raise ValueError("collection_failed")
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                recovery.fail(STATE, now.isoformat(), 'ошибка сбора (' + type(exc).__name__ + ')')
                recovery.deliver_health(STATE)
                print("Collection failed; cooldown scheduled")
                return 1
        else:
            result = json.loads(args.import_result.read_text())
        if args.scheduled:
            from semantic import enrich_result
            try:
                enrich_result(result, STATE)
            except Exception as exc:
                result["semantic_status"] = {"error_type": type(exc).__name__}
        previous = json.loads((STATE / "state.json").read_text()) if (STATE / "state.json").exists() else {}
        if previous and result["started"] <= previous["updated"]:
            print("Snapshot already processed or older; skipped")
            return 0
        if result.get("details_blocked"):
            recovery.fail(STATE, result["started"], 'Авито ограничил чтение карточек')
        elif args.scheduled and pause:
            cards = result.get('cards', [])
            if cards and all(c.get('parsed') or c.get('availability') in ('sold', 'removed', 'missing') for c in cards):
                recovery.recover(STATE, result['started'])
            else:
                recovery.fail(STATE, result['started'], 'контрольная проверка карточек не завершена')
        state, report = reconcile(previous, result, int(os.environ.get("AVITO_MAX_PRICE", "65000")))
        state['collection_status'] = 'partial' if result.get('details_blocked') else 'ok'
        from analysis import attach_assessments
        attach_assessments(state, report, previous)
        atomic_json(STATE / "latest.json", report)
        with (STATE / "history.jsonl").open("a") as history:
            history.write(json.dumps(report, ensure_ascii=False) + "\n")
        save_report(report)
        atomic_json(STATE / "state.json", state)
        if args.scheduled:
            recovery.deliver_health(STATE)
            from notifications import deliver_events
            delivery = deliver_events(state, STATE, report["snapshot"])
            atomic_json(STATE / "telegram_status.json", delivery)
            print(json.dumps({"telegram": delivery}))
        print(json.dumps({"observed": report["observed"], "candidates": len(report["candidates"]),
                          "changes": len(report["changes"]), "baseline": report["baseline"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
