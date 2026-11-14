from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from app.macro.extensions.config import ExtensionConfig, ExtensionError, load_extension_config
from app.macro.extensions.plan import build_extension_plan, validate_extension_plan
from backend.tests.macro.test_extension_plan import payload

NOW = datetime(2026, 10, 25, 2, tzinfo=timezone.utc)
DOCUMENTS = [
    {'year': 2022, 'url': 'https://www.pbc.gov.cn/diaochatongjisi/attachDir/2025/11/2025110416580445143.htm'},
    {'year': 2023, 'url': 'https://www.pbc.gov.cn/diaochatongjisi/attachDir/2025/11/2025110415542195680.htm'},
    {'year': 2024, 'url': 'https://www.pbc.gov.cn/diaochatongjisi/attachDir/2025/11/2025111416250469423.htm'},
    {'year': 2025, 'url': 'https://www.pbc.gov.cn/diaochatongjisi/attachDir/2026/02/2026021416101512219.htm'},
    {'year': 2026, 'url': 'https://www.pbc.gov.cn/diaochatongjisi/attachDir/2026/10/2026101417443343908.htm'},
]


def fiscal_payload():
    raw = payload()
    raw['groups']['governmentBonds'] = {'collectionEnabled': True,
        'schedule': {'frequency': 'weekly', 'weekday': 0, 'time': '09:00', 'timezone': 'Asia/Shanghai'},
        'lookbackPeriods': 48, 'quoteSide': None, 'documents': deepcopy(DOCUMENTS),
        'limits': {'maxHttpPerRun': 5, 'maxHttpPerPeriod': 5, 'requestsPerMinute': 10,
            'requestTimeoutSeconds': 20, 'maxRetries': 0, 'maxResponseBytes': 5242880, 'maxResponseRows': 12}}
    return raw


def fiscal_config(tmp_path):
    f = tmp_path / 'extensions.json'
    f.write_text(json.dumps(fiscal_payload()), encoding='utf-8')
    return load_extension_config(f)


def fiscal_plan(tmp_path):
    return build_extension_plan('governmentBonds', fiscal_config(tmp_path), NOW)


def test_government_plan_freezes_previous_complete_48_months_and_five_official_years(tmp_path):
    p = fiscal_plan(tmp_path)
    assert p['scheduledAt'] == '2026-10-19T09:00:00+08:00'
    assert (p['startPeriod'], p['endPeriod']) == ('2022-10-01', '2026-09-01')
    assert p['documents'] == DOCUMENTS
    assert (p['source'], p['seriesId'], p['unit'], p['frequency']) == ('pboc', 'cn.fiscal.government_bond_net_financing', '亿元', 'monthly')
    validate_extension_plan(p)
    assert build_extension_plan('governmentBonds', fiscal_config(tmp_path), NOW)['fingerprint'] == p['fingerprint']


def test_fiscal_does_not_change_existing_two_frozen_identities(tmp_path):
    c = fiscal_config(tmp_path)
    at = datetime.fromisoformat('2026-10-24T18:00:00+08:00')
    assert build_extension_plan('usdcnh', c, at)['fingerprint'] == 'db78e871d689b07c12ded998856c9dc9075f26e3c0bcb820824d640a8e22c2f5'
    assert build_extension_plan('nominalGdp', c, at)['fingerprint'] == '3cb50ae0547b29d7c0bf7f0d942b637ffc19eb39ccf47a1fcda689097f71c4c1'


@pytest.mark.parametrize('url', ['http://www.pbc.gov.cn/test.htm', 'https://evil.invalid/202610.htm',
    DOCUMENTS[0]['url'] + '?token=x', DOCUMENTS[0]['url'] + '#x', DOCUMENTS[0]['url'].replace('www.', 'user@www.')])
def test_non_closed_document_urls_rejected(url):
    raw = fiscal_payload(); raw['groups']['governmentBonds']['documents'][0]['url'] = url
    with pytest.raises(ValueError): ExtensionConfig.model_validate(raw)


@pytest.mark.parametrize('field,value', [('maxHttpPerRun', 6), ('maxHttpPerPeriod', 6), ('maxRetries', 1),
    ('maxResponseBytes', 5242881), ('maxResponseRows', 13)])
def test_fiscal_limits_cannot_be_widened(field, value):
    raw = fiscal_payload(); raw['groups']['governmentBonds']['limits'][field] = value
    with pytest.raises(ValueError): ExtensionConfig.model_validate(raw)


def test_missing_year_and_duplicate_year_fail_without_network():
    raw = fiscal_payload(); raw['groups']['governmentBonds']['documents'].pop(0)
    with pytest.raises(ExtensionError):
        build_extension_plan('governmentBonds', ExtensionConfig.model_validate(raw), NOW)
    raw = fiscal_payload(); raw['groups']['governmentBonds']['documents'][1] = raw['groups']['governmentBonds']['documents'][0]
    with pytest.raises(ValueError): ExtensionConfig.model_validate(raw)


def test_rehashed_fiscal_source_or_window_tampering_is_rejected(tmp_path):
    from app.macro.extensions.plan import digest
    p = fiscal_plan(tmp_path)
    p['documents'][0]['url'] = p['documents'][1]['url']
    p['fingerprint'] = digest({k: v for k, v in p.items() if k != 'fingerprint'})
    with pytest.raises(ExtensionError): validate_extension_plan(p)
