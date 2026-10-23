"""Export the saved live result; never makes network requests."""
import csv
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent / "results"


def main():
    result = json.loads((ROOT / "summary.json").read_text())
    rows = []
    for item in result["listings"]:
        path = urlsplit(item.get("urlPath") or "").path
        price = (item.get("priceDetailed") or {}).get("value")
        rows.append(dict(id=item["id"], title=item.get("title"), price_rub=price,
                         url="https://www.avito.ru" + path,
                         kazan_url=path.startswith("/kazan/")))
    with (ROOT / "listings.csv").open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=("id", "title", "price_rub", "url", "kazan_url"))
        writer.writeheader()
        writer.writerows(rows)
    sample = [row for row in rows if row["kazan_url"] and
              isinstance(row["price_rub"], (int, float)) and 0 < row["price_rub"] <= 50000][:20]
    lines = ["# Свежая выдача Авито — 2026-09-20, 17:09 МСК", "",
             "Выборка по URL Казани и цене до 50 000 ₽. Б/у, игровые характеристики,",
             "фактический город и доступность не проверены; это не рекомендации к покупке.", "",
             "| Объявление | Цена, ₽ |", "|---|---:|"]
    for row in sample:
        title = str(row["title"]).replace("|", " ").replace("[", "(").replace("]", ")")
        lines.append(f"| [{title}]({row['url']}) | {row['price_rub']} |")
    (ROOT / "sample.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"csv_rows": len(rows), "unique_ids": len({r['id'] for r in rows}),
                      "kazan_urls": sum(r["kazan_url"] for r in rows),
                      "sample_rows": len(sample)}))


if __name__ == "__main__":
    main()
