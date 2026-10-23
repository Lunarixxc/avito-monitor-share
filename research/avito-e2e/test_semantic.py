from unittest.mock import Mock
import pytest
from semantic import validate, enrich_result, config, request_analysis, text_input


def payload():
    return {'facts': [{'field': 'vram_gb', 'value': 4, 'evidence': 'RTX 3050 4 GB'}], 'risks': []}


def test_evidence_required_and_ram_not_vram():
    assert validate(payload(), 'RTX 3050 4 GB')['facts'][0]['value'] == 4
    with pytest.raises(ValueError):
        validate(payload(), 'RTX 3050 6 GB')
    data = payload(); data['facts'][0]['evidence'] = 'RAM 4 GB'
    with pytest.raises(ValueError):
        validate(data, 'RAM 4 GB')
    with pytest.raises(ValueError):
        validate({'facts': [], 'risks': [], 'price': 1}, 'test')


def test_disabled_means_no_network(tmp_path):
    request = Mock()
    cfg = config(); cfg['DEEPSEEK_ENABLED'] = '0'
    enrich_result({'cards': []}, tmp_path, request, cfg)
    request.assert_not_called()


def test_cache_and_daily_budget(tmp_path):
    cfg = config(); cfg.update(DEEPSEEK_ENABLED='1', DEEPSEEK_API_KEY='fixture', DEEPSEEK_MAX_CALLS_PER_DAY='1')
    request = Mock(return_value=(payload(), {'total_tokens': 10}))
    result = {'cards': [{'parsed': True, 'availability': 'active', 'description': 'RTX 3050 4 GB'}]}
    enrich_result(result, tmp_path, request, cfg)
    assert result['cards'][0]['semantic_specs']['vram_gb'] == 4
    enrich_result(result, tmp_path, request, cfg)
    assert request.call_count == 1
    result['cards'][0]['description'] += ' Новый текст'
    enrich_result(result, tmp_path, request, cfg)
    assert request.call_count == 1


def test_error_is_safe_and_main_work_continues(tmp_path):
    cfg = config(); cfg.update(DEEPSEEK_ENABLED='1', DEEPSEEK_API_KEY='fixture')
    result = {'cards': [{'parsed': True, 'availability': 'active', 'description': 'RTX 3050 4 GB'}]}
    enrich_result(result, tmp_path, Mock(side_effect=RuntimeError('secret')), cfg)
    assert result['semantic_status']['errors'] == 1
    assert 'secret' not in str(result)


def test_no_credentials_to_arbitrary_provider():
    cfg = config(); cfg['DEEPSEEK_BASE_URL'] = 'https://example.com'
    with pytest.raises(ValueError):
        request_analysis('text', cfg)


def test_contact_redaction():
    source = text_input({'description': 'RTX 3050 4 GB +7 (999) 123-45-67 test@example.com https://example.com'})
    assert '123-45-67' not in source and 'test@example' not in source
    assert 'RTX 3050 4 GB' in source


def test_partial_response_keeps_grounded_facts_without_weakening_quotes():
    from semantic import accept_supported
    data = payload()
    data['facts'] += [{'field': 'storage', 'value': 'SSD — 512 ГБ',
                      'evidence': 'Объем накопителей, ГБ : 512'},
                     {'field': 'cpu', 'value': 'Intel Core i9-12900H', 'evidence': 'invented quote'}]
    data['risks'] = [{'code': 'repair', 'evidence': 'Не работает подсветка'},
                     {'code': 'defect', 'evidence': 'Мелкие царапины'}]
    source = 'RTX 3050 4 GB Объем накопителей, ГБ : 512 Не работает подсветка Мелкие царапины'
    accepted, rejected = accept_supported(data, source)
    assert accepted == payload()
    assert len(rejected) == 4


def test_storage_requires_type_and_capacity_in_quote():
    quote = 'Конфигурация накопителей : SSD Объем накопителей, ГБ : 512'
    data = {'facts': [{'field': 'storage', 'value': 'SSD — 512 ГБ', 'evidence': quote}], 'risks': []}
    assert validate(data, quote)['facts'][0]['value'] == 'SSD — 512 ГБ'


def test_correct_risk_category_and_negation():
    quote = 'Не работает подсветка'
    assert validate({'facts': [], 'risks': [{'code': 'defect', 'evidence': quote}]}, quote)['risks']
    quote = 'Без ремонта'
    with pytest.raises(ValueError):
        validate({'facts': [], 'risks': [{'code': 'repair', 'evidence': quote}]}, quote)


def test_rejected_claims_cached_and_billed_tokens_counted(tmp_path):
    cfg = config(); cfg.update(DEEPSEEK_ENABLED='1', DEEPSEEK_API_KEY='fixture')
    data = payload(); data['facts'][0]['evidence'] = 'invented quote'
    request = Mock(return_value=(data, {'total_tokens': 123}))
    result = {'cards': [{'parsed': True, 'availability': 'active', 'description': 'RTX 3050 4 GB'}]}
    enrich_result(result, tmp_path, request, cfg)
    assert result['semantic_status']['rejected_claims'] == 1
    assert result['cards'][0]['semantic_specs'] == {}
    enrich_result(result, tmp_path, request, cfg)
    assert request.call_count == 1
    import json
    usage = json.loads(next((tmp_path / 'semantic').glob('usage-*.json')).read_text())
    assert usage['tokens'] == 123


def test_thermal_paste_is_not_a_repair_penalty():
    from semantic import accept_supported
    quote = 'заменил термопасту'
    accepted, rejected = accept_supported({'facts': [], 'risks': [{'code': 'repair', 'evidence': quote}]}, quote)
    assert accepted['risks'] == []
    assert rejected[0]['reason'] == 'maintenance_is_not_repair'
    quote = 'ремонт платы, заменил термопасту'
    assert validate({'facts': [], 'risks': [{'code': 'repair', 'evidence': quote}]}, quote)['risks']


def test_nonfunctional_keys_and_cracks_are_defects():
    for quote in ('не функционируют 4 клавиши F3, F4, 3, Е(у)', 'По корпусу имеется трещины'):
        data = {'facts': [], 'risks': [{'code': 'defect', 'evidence': quote}]}
        assert validate(data, quote)['risks']


def test_single_storage_number_does_not_prove_a_conflict():
    from semantic import accept_supported
    quote = 'Объем накопителей, ГБ : 1000'
    data = {'facts': [], 'risks': [{'code': 'contradiction', 'evidence': quote}]}
    accepted, rejected = accept_supported(data, quote + ' Ссд на терабайт')
    assert not accepted['risks']
    assert rejected[0]['reason'] == 'single_value_not_conflict'
