"""Literal factual claims, independent of research screening thresholds."""
from __future__ import annotations

import copy
from decimal import Decimal, DecimalException
from typing import Any

from .templates import earnings_quality as eq, shareholder_return as sr
from .verifier import decimal, decimal_string, issuance_notes, require_same_period, require_same_unit

FACT_CHECKS = {
    'repurchase_positive': ('F1', '기간 중 자사주를 매입했다', ['repurchase']),
    'net_buyback_positive': ('F2', '순자사주 환원이 양수다(매입 > 보통주 발행)', ['repurchase', 'common_issuance']),
    'shares_decreased': ('F3', '기말 주식수가 줄었다', ['shares']),
    'net_income_increased': ('F1', '순이익이 증가했다', ['net_income']),
    'cfo_supports_income': ('F2', '영업현금흐름이 이익을 뒷받침한다', ['net_income', 'cfo']),
}


def fact_verdict(facts: list[dict], unsupported_claims: list[str] | None = None) -> str:
    statuses = [fact['status'] for fact in facts]
    if 'FALSE' in statuses:
        return 'REFUTED'
    if statuses and all(status == 'TRUE' for status in statuses) and not unsupported_claims:
        return 'SUPPORTED'
    return 'INCONCLUSIVE'


def _number(value: Any) -> Decimal:
    result = decimal(value)
    if not result.is_finite():
        raise ValueError('non-finite fact input')
    return result


def _money(*rows: dict) -> None:
    unit = require_same_unit(rows)
    if len(unit) != 3 or not unit.isalpha():
        raise ValueError('monetary unit required')
    for row in rows:
        _number(row['value'])


def _scoped_fields(fields: dict, claim: dict) -> dict:
    """An explicitly asserted year must never silently become the latest TTM."""
    year = claim.get('period_year')
    if not year:
        return fields
    result = copy.deepcopy(fields)
    # Preserve comparative priors for YoY checks, but restrict the current year.
    for key, rows in list(result.items()):
        if isinstance(rows, list):
            result[key] = [row for row in rows if not isinstance(row, dict) or
                           str(row.get('period_end', ''))[:4] <= str(year)]
    return result


def evaluate_facts(claims: list[dict], fields: dict) -> list[dict]:
    facts = []
    for claim in claims:
        check = claim.get('check')
        fact = {**copy.deepcopy(claim), 'status': '판정불가', 'inputs': {}, 'outputs': {},
                'reason_ko': '해당 주장을 판정할 원공시 입력이 부족합니다.'}
        facts.append(fact)
        selected = _scoped_fields(fields, claim)
        inputs, outputs = fact['inputs'], fact['outputs']
        try:
            if check == 'repurchase_positive':
                rows = selected.get('repurchase') or []
                if not rows:
                    raise ValueError('missing repurchase')
                row = max(rows, key=lambda r: (r.get('period_end') or '', r.get('filed') or ''))
                inputs['repurchase'] = row
                _money(row)
                outputs.update(repurchase=row['value'], unit=row['unit'])
                current = row
                truth = _number(row['value']) > 0
                reason = f"매입 {row['value']} {row['unit']} > 0"
            elif check == 'net_buyback_positive':
                if selected.get('issuance_unconfirmed'):
                    raise ValueError('보통주 발행 현금 교차확인 실패')
                pair = sr._by_period(selected, ('repurchase', 'common_issuance'))
                if pair is None:
                    raise ValueError('같은 기간의 매입·보통주 발행 현금 미확인')
                current, issuance = pair
                inputs.update(repurchase=current, common_issuance=issuance)
                _money(*pair)
                require_same_period(pair)
                net = _number(current['value']) - _number(issuance['value'])
                outputs.update(repurchase=current['value'], common_issuance=issuance['value'],
                               net_return=decimal_string(net), unit=current['unit'])
                truth = net > 0
                reason = f"매입 {current['value']} − 보통주 발행 {issuance['value']} = 순자사주 환원 {net} {current['unit']}"
                if issuance_notes(issuance):
                    reason += " · " + " · ".join(issuance_notes(issuance))
            elif check == 'shares_decreased':
                rows = [row for row in selected.get('shares', [])
                        if row.get('tag') == 'CommonStockSharesOutstanding' or row.get('derived') == 'issued - treasury']
                if not rows:
                    raise ValueError('기말 주식수 공시 미확인')
                pair = sr._latest_two_from_filing(rows, sr._latest_10k_accession(*rows))
                if pair is None:
                    raise ValueError('동일 공시의 기말 주식수 비교열 미확인')
                current, prior = pair
                inputs.update(current=current, prior=prior)
                if require_same_unit(pair) != 'shares':
                    raise ValueError('기말 주식수 단위 불일치')
                now, before = _number(current['value']), _number(prior['value'])
                if before <= 0 or now < 0:
                    raise ValueError('invalid share counts')
                outputs.update(current_shares=current['value'], prior_shares=prior['value'],
                               yoy_change=decimal_string(now / before - 1))
                truth = now < before
                reason = f"기말 주식수 {prior['value']} → {current['value']} shares"
            elif check == 'net_income_increased':
                pair = eq._pair(selected, 'net_income', eq._NET_INCOME_NAMES)
                if pair is None:
                    raise ValueError('동일 공시의 순이익 비교열 미확인')
                current, prior = pair
                inputs.update(current=current, prior=prior)
                _money(*pair)
                eq._same_filing_if_present(*pair)
                change = _number(current['value']) - _number(prior['value'])
                outputs.update(net_income_current=current['value'], net_income_prior=prior['value'],
                               change=decimal_string(change), unit=current['unit'])
                truth = change > 0
                reason = f"순이익 {prior['value']} → {current['value']} {current['unit']} (변화 {change})"
            elif check == 'cfo_supports_income':
                current = eq._current(selected, eq._NET_INCOME_NAMES)
                cfo = eq._current(selected, eq._CFO_NAMES)
                if current is None or cfo is None:
                    raise ValueError('순이익·영업현금흐름 미확인')
                inputs.update(net_income=current, cfo=cfo)
                _money(current, cfo)
                require_same_period((current, cfo))
                eq._same_filing_if_present(current, cfo)
                if _number(current['value']) <= 0:
                    raise ValueError('순이익이 0 이하이므로 CFO/NI 비율 무의미')
                ratio = _number(cfo['value']) / _number(current['value'])
                threshold = _number(claim.get('threshold') or '1')
                outputs.update(net_income=current['value'], cfo=cfo['value'],
                               ratio=decimal_string(ratio), threshold=decimal_string(threshold))
                truth = ratio >= threshold
                reason = f"CFO {cfo['value']} / 순이익 {current['value']} = {ratio}; 주장 기준 ≥ {threshold}"
            else:
                raise ValueError('지원하지 않는 사실 검증')
            if claim.get('period_year') and str(current.get('period_end', ''))[:4] != str(claim['period_year']):
                raise ValueError('주장한 연도의 공시 입력 미확인')
            fact.update(status='TRUE' if truth else 'FALSE', reason_ko=reason)
        except (ValueError, KeyError, TypeError, DecimalException) as exc:
            fact['reason_ko'] = f'판정불가: {exc}'
            fact['outputs'] = {}
    return facts


def attach_layers(evaluation: dict, *, fields: dict, claims: list[dict], template: str,
                  criteria: str = 'default', unsupported_claims: list[str] | None = None) -> None:
    if criteria not in {'default', 'none'}:
        raise ValueError(f'unsupported criteria: {criteria}')
    source = '원 연구 B01 규칙' if template == 'earnings_quality' else '원 연구 B03 규칙'
    evaluation['criteria'] = {'mode': criteria, 'user_approved': criteria == 'default',
                              'source': source, 'label_ko': '사용자가 승인한 기준' if criteria == 'default' else '선별 조건 미적용'}
    if criteria == 'none':
        evaluation.update(claims=[], extra_checks=[], verdict='INCONCLUSIVE')
    else:
        for item in evaluation.get('claims', []):
            item.update(layer='screening', source=source, user_approved=True)
    facts = evaluate_facts(claims, fields)
    if evaluation.get('verdict') == 'NOT_APPLICABLE':
        # Out-of-scope filers (financials, 20-F/40-F) get the scope reason, not a parser message.
        scope = next((c.get('reason_ko') for c in evaluation.get('claims', []) if c.get('reason_ko')),
                     '범위 밖 발행인(금융업 또는 20-F/40-F)')
        for fact in facts:
            if fact['status'] == '판정불가':
                fact['reason_ko'] = f'판정불가: {scope}'
    evaluation.update(fact_claims=facts, fact_verdict=fact_verdict(facts, unsupported_claims),
                      unsupported_claims=unsupported_claims or [])
    evaluation['scope_note_ko'] = '배당은 이 가설의 범위 밖입니다. 순자사주 환원은 매입 − 보통주 발행 현금입니다.' if template == 'shareholder_return' else ''
