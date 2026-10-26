"""WATCH-only extraction of facts and commitments, separate from screening."""
from __future__ import annotations

import copy
import json
import math
from typing import Any

from ._schema import validate
from .planner import PLANNER_SCHEMA, literal_plan
from ..facts import FACT_CHECKS


COMMITMENT_NAMES = {
    'repurchase_at_least', 'revenue_in_range', 'operating_margin_at_least',
    'capex_in_range', 'cfo_at_least', 'leverage_at_most',
}
LEDGER_FIELDS = {'expect', 'min', 'max', 'horizon', 'kind', 'source_quote'}
WATCH_PLANNER_SCHEMA = copy.deepcopy(PLANNER_SCHEMA)
WATCH_PLANNER_SCHEMA['title'] = 'watch_planner_output'
_fact_schema = WATCH_PLANNER_SCHEMA['properties']['fact_claims']['items']
_fact_schema['properties'].update({
    'check': {'type': 'string', 'enum': sorted(set(FACT_CHECKS) | COMMITMENT_NAMES)},
    'expect': {'type': 'boolean'},
    'min': {'type': 'number'}, 'max': {'type': 'number'},
    'period_year': {'anyOf': [{'type': 'integer'}, {'type': 'string', 'enum': ['']}]},
    'horizon': {'type': 'string', 'enum': ['ttm', 'fy']},
    'kind': {'type': 'string', 'enum': ['fact', 'commitment']},
    'source_quote': {'type': 'string'},
})
_fact_schema['required'] = ['id', 'check', 'statement_ko']
WATCH_PLANNER_OUTPUT_SCHEMA = WATCH_PLANNER_SCHEMA


def build_prompt(*, thesis: str, ticker: str, document_date: str | None,
                 templates: list[str]) -> tuple[str, str]:
    system = (
        'stage:planner\n'
        '당신은 WATCH 모드의 commitment ledger planner다. 숫자를 계산하거나 참거짓을 판정하지 말라. '
        '과거 사실뿐 아니라 대상 회계연도나 명시적인 상시 정책이 있는 미래 약속도 추출한다. '
        'claims=[]로 두고 검증할 원문 주장만 fact_claims에 기록한다. 선별조건을 추가하지 말라. '
        '각 주장에는 고유 id, check, statement_ko, 원문에 실제로 있는 source_quote와 original_quote, '
        'expect(기본 true), kind(fact 또는 commitment), horizon(ttm 또는 fy), '
        'period_year(대상 회계연도 정수, 상시 정책이면 빈 문자열)를 기록한다. '
        'min/max는 문자열이 아닌 USD 금액 또는 0~1 비율의 JSON 숫자다. 임의 허용오차를 만들지 말라. '
        '기간이 정해진 미래 금액 목표는 horizon=fy다. 누락된 연도를 현재 연도로 채우지 말라. '
        '문서 날짜로 다음 회계연도나 24개월 뒤를 해석할 때 assumptions_ko에 추론을 명시한다. '
        '지원 checks: repurchase_positive(매입액>0), net_buyback_positive(매입>보통주발행), '
        'shares_decreased(주식수 감소), net_income_increased(순이익 증가), '
        'cfo_supports_income(CFO/순이익>=1 또는 명시 threshold), revenue_increased(매출 증가), '
        'operating_margin_improved(영업이익률 개선), fcf_positive(CFO-capex>0), '
        'leverage_not_rising(총부채/총자산 변화<=threshold %p), dividends_paid(배당 지급액>0), '
        'shares_not_diluting(주식수 증가율<=threshold %), capex_increased(설비투자 증가), '
        'repurchase_at_least(FY 또는 TTM 보통주 자사주매입 합계>=min), '
        'revenue_in_range(FY 매출 min<=매출<=max), '
        'operating_margin_at_least(FY GAAP OperatingIncomeLoss/매출>=min), '
        'capex_in_range(FY 또는 TTM capex min<=금액<=max), '
        'cfo_at_least(FY 또는 TTM CFO>=min), leverage_at_most(총이자부부채/총자산<=max). '
        'expect=false는 관측 명제가 거짓이어야 충족한다. 배당 중단은 dividends_paid expect=false, '
        '매입 중단은 repurchase_positive expect=false다. '
        '매입 authorization은 지출 최소 약속이 아니다: Apple $110B, Meta $50B, Alphabet $70B, '
        'Microsoft $60B, Nvidia $50B 모두 repurchase_positive, kind=commitment, horizon=ttm, '
        'period_year="", min/max 없이 매입 지속 정책으로만 매핑한다. '
        '배당 선언/인상/도입은 dividends_paid expect=true의 상시 정책으로 매핑하며 주당 금액·인상률을 '
        '총배당액 최소치로 바꾸지 말라. Intel Q4 2024부터 배당 중단은 첫 온전한 FY2025부터 '
        'dividends_paid expect=false, horizon=ttm, period_year=2025로 매핑한다. '
        'Intel 2024 capex 25~27B 및 2025 capex 20~23B는 각각 FY 범위다. '
        'UPS 2026 매출 108~114B와 영업이익률 13%는 period_year=2026, horizon=fy로 묶는다. '
        'adjusted/non-GAAP 영업이익률도 operating_margin_at_least GAAP 대용치로 매핑할 수 있으나 '
        'statement_ko와 assumptions_ko에 조정 목표와 GAAP 수치가 다를 수 있음을 명시한다. '
        'Home Depot 매입 중단은 repurchase_positive expect=false의 TTM 정책이며, '
        '2024-03-28에서 24개월 뒤 ~2.0x 목표는 FY2026 leverage_at_most max=2.0으로 '
        '매핑하되 원래 debt/EBITDA 대비 총부채/총자산 대용치라는 한계를 명시한다. '
        'Disney 2024-11 문서의 다음 FY 전망은 FY2025로 해석했다는 가정을 명시한다: '
        '매입 3B는 repurchase_at_least min=3000000000, CFO 약15B는 cfo_at_least min=15000000000, '
        'capex 약8B는 capex_in_range min=max=8000000000, 임의 오차 없이 원문의 약 표현을 보존한다. '
        'Meta 2024 capex 30~37B는 FY2024 capex_in_range다. '
        '분기 매출 목표, 총비용 범위, 매출총이익률, EPS, 조정 EPS, EBITDA, R&D/MG&A, '
        '경쟁사 비교 및 지원하지 않는 다년 누적 FCF 범위는 unsupported_claims에 원문과 한국어 이유를 보존한다. '
        '비GAAP만 존재하고 지원된 GAAP 대용치로도 매핑할 수 없는 항목은 지원하지 않는다. '
        '지원 항목과 미지원 항목이 섞이면 각각 보존한다. '
        '지원 claim이 없으면 template=unsupported다. 배당/매입 정책은 shareholder_return, '
        '그 외 항목은 earnings_quality로 라우팅하되 claims는 항상 빈 배열로 둔다.'
    )
    return system, json.dumps(dict(thesis=thesis, ticker=ticker, document_date=document_date,
                                    available_templates=templates), ensure_ascii=False)


def _ledger_claim(fact: dict) -> bool:
    return fact.get('check') in COMMITMENT_NAMES or bool(LEDGER_FIELDS.intersection(fact))


def _normalize_fact(fact: dict, thesis: str) -> dict:
    item = copy.deepcopy(fact)
    if not isinstance(item.get('check'), str):
        raise ValueError('fact check must be a string')
    quote = item.get('source_quote', item.get('original_quote'))
    if not isinstance(quote, str) or not quote or quote not in thesis:
        raise ValueError('fact source_quote must occur in thesis')
    if item.get('original_quote') is not None and item['original_quote'] != quote:
        raise ValueError('source_quote and original_quote must match')
    if not _ledger_claim(item):
        # Existing callers receive the old fact shape unchanged.
        return item
    item.setdefault('source_quote', quote)
    item.setdefault('original_quote', quote)
    item.setdefault('expect', True)
    item.setdefault('period_year', '')
    if isinstance(item['period_year'], str) and item['period_year'].isdigit():
        item['period_year'] = int(item['period_year'])
    item.setdefault('horizon', 'fy' if item['period_year'] != '' else 'ttm')
    item.setdefault('kind', 'commitment' if item['check'] in COMMITMENT_NAMES else 'fact')
    year = item['period_year']
    if year != '' and (isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 9999):
        raise ValueError('period_year must be a fiscal year integer or empty string')
    for key in ('min', 'max'):
        if key in item and (isinstance(item[key], bool) or not isinstance(item[key], (int, float))
                            or not math.isfinite(item[key])):
            raise ValueError(f'{key} must be a finite numeric bound')
    if 'min' in item and 'max' in item and item['min'] > item['max']:
        raise ValueError('min cannot exceed max')
    required = {
        'repurchase_at_least': ('min',), 'revenue_in_range': ('min', 'max'),
        'operating_margin_at_least': ('min',), 'capex_in_range': ('min', 'max'),
        'cfo_at_least': ('min',), 'leverage_at_most': ('max',),
    }.get(item['check'], ())
    if any(key not in item for key in required):
        raise ValueError('claim is missing required numeric bounds')
    if item['check'] in COMMITMENT_NAMES and item['kind'] == 'commitment' and year == '':
        raise ValueError('numeric commitment requires a target fiscal year')
    if item['check'] in {'revenue_in_range', 'operating_margin_at_least'} and item['horizon'] != 'fy':
        raise ValueError('revenue and operating margin targets require horizon=fy')
    return item


def plan(*, thesis: str, ticker: str, document_date: str | None, templates: list[str],
         provider: Any, model: str = 'sonnet') -> dict:
    system, user = build_prompt(thesis=thesis, ticker=ticker, document_date=document_date,
                                templates=templates)
    raw = provider.complete_json(system=system, user=user, schema=WATCH_PLANNER_SCHEMA, model=model)
    if not isinstance(raw, dict):
        raise ValueError('watch planner output must be an object')
    result = copy.deepcopy(raw)
    result.setdefault('unsupported_claims', [])
    if 'fact_claims' not in result:
        # Old providers can still return screening-only plans; never promote
        # screening conditions into facts or invent future commitments.
        literal = literal_plan(thesis, preferred_template=result.get('template', 'unsupported'))
        result['fact_claims'] = literal['fact_claims']
        result['unsupported_claims'] = list(dict.fromkeys(result['unsupported_claims'] + literal['unsupported_claims']))
        if literal['template'] == 'unsupported':
            result['template'] = 'unsupported'
    facts = result.get('fact_claims')
    if not isinstance(facts, list) or any(not isinstance(fact, dict) for fact in facts):
        raise ValueError('watch fact_claims must be an array of objects')
    result['fact_claims'] = [_normalize_fact(fact, thesis) for fact in facts]
    checked = copy.deepcopy(result)
    # Legacy planner used year strings. Validate their meaning without changing
    # the shape returned to old WATCH callers.
    for fact in checked['fact_claims']:
        year = fact.get('period_year')
        if isinstance(year, str) and year.isdigit():
            fact['period_year'] = int(year)
    errors = validate(checked, WATCH_PLANNER_SCHEMA)
    if errors:
        raise ValueError('watch planner schema error: ' + '; '.join(errors))
    ids = [fact['id'] for fact in result['fact_claims']]
    if any(not identifier.strip() for identifier in ids) or len(set(ids)) != len(ids):
        raise ValueError('watch fact ids must be nonempty and unique')
    if result['template'] not in templates and result['template'] != 'unsupported':
        raise ValueError('watch planner chose unavailable template')
    if not result['fact_claims'] and result['unsupported_claims']:
        result['template'] = 'unsupported'
    if result['template'] == 'unsupported':
        result['fact_claims'] = []
        result['unsupported_claims'] = result['unsupported_claims'] or [thesis]
    return result
