from filters import classify, choose_details, parse_card, specifications, seller_info
from notify import render
import run_once


def item(title="MSI RTX 3050", price=60000, identifier=1):
    return {"id": identifier, "title": title, "urlPath": "/kazan/noutbuki/test_1",
            "priceDetailed": {"value": price}}


def test_budget_gpu_and_unconfirmed_title():
    assert classify(item())["candidate"]
    assert classify(item(price=60001))["candidate"]
    assert classify(item(price=65000))["candidate"]
    assert not classify(item(price=65001))["candidate"]
    assert not classify(item(price=65000), max_price=60000)["candidate"]
    for gpu in ("GTX 1050 Ti", "GTX 1650", "RTX 2050", "Intel UHD"):
        assert not classify(item(), card={"parameters": "Видеокарта: " + gpu})["candidate"]
    assert not classify(item(), card={"parsed": True, "parameters": "ОЗУ: 16"})["notify_ready"]
    assert classify(item(), card={"parsed": True, "parameters": "Видеокарта: RTX 4050"})["notify_ready"]


def test_examples_are_rejected_even_with_supported_gpu():
    for description in ("Например. Ноутбук RTX 3060", "Пример ноутбука: RTX 3050",
                        "Подберем лучший вариант индивидуально для Вас"):
        assert not classify(item(), card={"parsed": True, "description": description,
                                         "parameters": "Видеокарта: RTX 3050"})["candidate"]


def test_fields_from_flat_avito_params_and_seller_evidence():
    card = parse_card('<h1>MSI</h1><div data-marker="seller-info">NPC Центр электроники Компания 900 отзывов</div>'
                      '<div data-marker="item-view/item-params">Процессор : Intel Core i5-12500H '
                      'Оперативная память, ГБ : 16 Конфигурация накопителей : HDD+SSD '
                      'Объем накопителей, ГБ : 1000 Видеокарта : NVIDIA GeForce RTX 3050 '
                      'Объем видеопамяти, ГБ : 4 Все характеристики</div>')
    specs = specifications(card)
    assert specs["gpu"] == "NVIDIA GeForce RTX 3050"
    assert specs["vram_gb"] == 4
    assert specs["ram"] == "16 ГБ"
    assert specs["cpu"] == "Intel Core i5-12500H"
    assert specs["storage"] == "HDD+SSD — 1000 ГБ суммарно; разбивка неизвестна"
    assert seller_info(card)["type"] == "shop"
    assert seller_info({"seller_text": "Иван 900 отзывов"})["type"] == "unknown"
    message = render({"title": "MSI", "price": 42000, "card": card,
                      "url": "https://www.avito.ru/kazan/noutbuki/test_1", "flags": {"condition": "unknown"}})
    assert message.startswith("Новый кандидат — MSI")
    assert "Продавец: Магазин / компания" in message
    assert "\nВидеокарта:" in message and "\nОперативная память:" in message
    assert "\nПроцессор:" in message and "\nНакопитель:" in message


def test_vram_render_with_legacy_cached_specs():
    card = {"parameters": "Видеокарта: RTX 3060 Объём видеопамяти, ГБ: 6"}
    specs = specifications(card)
    specs.pop("vram_gb")  # Existing state written before VRAM was exposed.
    row = {"title": "MSI Katana", "price": 50000, "card": card,
           "url": "https://www.avito.ru/kazan/noutbuki/msi_katana_gf76_11uek_8314571670",
           "flags": {"specs": specs}}
    assert "Видеокарта: RTX 3060 6 GB\n" in render(row)
    for value in ("", "Объем видеопамяти, ГБ: Не указано", "Объем видеопамяти, ГБ: 0"):
        card["parameters"] = "Видеокарта: RTX 3060 Оперативная память, ГБ: 16 " + value
        assert "Видеокарта: RTX 3060 — объём видеопамяти не указан\n" in render(row)


def test_review_priority_rejects_old_bad_cards_and_prefers_4050():
    listings = [item(identifier=1), item(identifier=2), item("MSI RTX 4050", identifier=3)]
    seen = {"1": {"card": {"parsed": True, "reviewed_price": 60000,
                           "description": "Пример ноутбука", "parameters": "Видеокарта: RTX 3050"}}}
    assert [x["id"] for x in choose_details(listings, seen, 8)] == [3, 2]


def test_search_query_and_pagination_configuration(monkeypatch):
    monkeypatch.setenv("AVITO_SEARCH_QUERY", "игровой ноутбук")
    run_once.configure(4)
    run_once.configure(4)
    params = run_once.upstream.ITEMS_QUERY_PARAMETERS
    assert [v for k, v in params if k == "query"] == ["игровой ноутбук"]
    assert dict(params)["locationId"] == "650400"
    assert run_once.upstream.PAGES_TO_REQUEST == 4


def test_mixed_script_gpu_and_storage_conflict():
    assert classify(item(), card={"parsed": True, "description": "Видеокарта RTХ 3050 4 GB"})["notify_ready"]
    flags = classify(item(), card={"parsed": True, "description": "RTX 3050, SSD нa 512 ГБ",
                                  "parameters": "Видеокарта: RTX 3050 Конфигурация накопителей: SSD Объем накопителей, ГБ: 256"})
    assert not flags["candidate"]
    assert "объём SSD" in flags["rejection_reasons"][0]


def test_assortment_gpu_lists_are_not_one_laptop():
    from filters import gpu_models
    description = "В НАЛИЧИИ МОЩНЫЕ КОНФИГУРАЦИИ: GТХ 1650/1660 Тi, RТХ 3050/3060/4050/4060."
    assert gpu_models(description) == ["GTX 1650", "GTX 1660 TI", "RTX 3050", "RTX 3060", "RTX 4050", "RTX 4060"]
    card = {"parsed": True, "description": description,
            "seller_text": "LapStore | Игровые ноутбуки Казань 5,0 234 отзыва Частное лицо",
            "parameters": "Оперативная память, ГБ: 16 Конфигурация накопителей: SSD Объем накопителей, ГБ: 512"}
    result = classify(item("Игровой ноутбук RTX/GTX с гарантией и тест-драйвом", 25990), card=card)
    assert result["seller"]["type"] == "shop"
    assert not result["candidate"] and not result["notify_ready"]
    assert any("комплектации" in r for r in result["rejection_reasons"])
    for gpu in ("RTX 3050/3060", "GTX 1650 / RTX 3050", "RTX 999"):
        assert not classify(item(), card={"parsed": True, "parameters": "Видеокарта: " + gpu})["candidate"]


def test_specific_shop_laptop_is_still_allowed():
    card = {"parsed": True, "seller_text": "Магазин Компания", "parameters": "Видеокарта: RTX 3050",
            "description": "Продаём MSI Pulse, RTX 3050. Цена за этот ноутбук. Гарантия 60 дней."}
    assert classify(item(), card=card)["notify_ready"]


def test_parts_and_nonworking_laptops_are_not_candidates():
    for title in ('Ноутбук Asus TUF Gaming запчасти', 'Ноутбук RTX 3050 не включается'):
        flags = classify(item(title, 5000), card={'parsed': True, 'parameters': 'Видеокарта: RTX 3050'})
        assert flags['excluded_title']
        assert not flags['candidate'] and not flags['notify_ready']


def test_shop_footer_is_not_assortment_but_gpu_header_conflict_is_real():
    from filters import description_warnings
    params = 'Видеокарта: NVIDIA GeForce RTX 3050 Объем видеопамяти, ГБ: 4'
    description = 'Конкретный ноутбук HP, RTX 3050. Ссылка на все ноутбуки (более 60шт.) в наличии магазина ниже'
    assert not description_warnings(description, params)
    description = 'HP 17 RTX2050-4/17.3 FHD. Видеокарта NVIDIA GeForce RTX 3050 4Гб.'
    assert any('в начале описания' in x for x in description_warnings(description, params))
