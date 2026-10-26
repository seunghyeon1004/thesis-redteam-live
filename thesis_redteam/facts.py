"""Literal factual claims, independent of research screening thresholds."""
from __future__ import annotations

import copy
from decimal import Decimal, DecimalException
from typing import Any

from .templates import earnings_quality as eq, shareholder_return as sr
from .extract import _day, _duration_days, _is_annual_duration
from .verifier import decimal, decimal_string, issuance_notes, require_same_period, require_same_unit

FACT_CHECKS = {
    'repurchase_positive': ('F1', '기간 중 자사주를 매입했다(프로그램 현금매입; 직원세금원천징수 취득 제외)', ['repurchase']),
    'net_buyback_positive': ('F2', '순자사주 환원이 양수다(매입 > 보통주 발행; 매입프로그램 현금, 직원세금원천징수 제외)', ['repurchase', 'common_issuance']),
    'shares_decreased': ('F3', '최근 회계연도 말 전체 보통주 주식수가 전년 말보다 줄었다(동일 10-K; 모든 종류 합산; 이후 표지일 제외)', ['shares']),
    'net_income_increased': ('F1', '순이익이 증가했다', ['net_income']),
    'cfo_supports_income': ('F2', '영업현금흐름이 이익을 뒷받침한다', ['net_income', 'cfo']),
    'revenue_increased': ('F4', '매출이 증가했다', ['revenue']),
    'operating_margin_improved': ('F5', '영업이익률이 개선됐다', ['operating_income', 'revenue']),
    'fcf_positive': ('F6', '잉여현금흐름이 양수다', ['cfo', 'capex']),
    'leverage_not_rising': ('F7', '총자산 대비 이자부 부채비율이 상승하지 않았다', ['interest_bearing_debt', 'assets']),
    'dividends_paid': ('F8', '배당금을 지급했다', ['dividends']),
    'shares_not_diluting': ('F9', '기말 보통주 주식수가 희석되지 않았다', ['shares']),
    'capex_increased': ('F10', '설비투자가 증가했다', ['capex']),
}

COMMITMENT_CHECKS = {
    'repurchase_at_least': ('W1', '자사주 매입액이 약속한 하한 이상이다', ['repurchase']),
    'revenue_in_range': ('W2', '매출이 약속한 범위 안이다', ['revenue']),
    'operating_margin_at_least': ('W3', 'GAAP 영업이익률이 약속한 하한 이상이다', ['operating_income', 'revenue']),
    'capex_in_range': ('W4', '설비투자가 약속한 범위 안이다', ['capex']),
    'cfo_at_least': ('W5', '영업현금흐름이 약속한 하한 이상이다', ['cfo']),
    'leverage_at_most': ('W6', '총부채/총자산이 약속한 상한 이하이다', ['interest_bearing_debt', 'assets']),
}
FACT_CHECKS.update(COMMITMENT_CHECKS)
LEDGER_CHECKS = frozenset(COMMITMENT_CHECKS) | {'repurchase_positive', 'dividends_paid'}

PREMISE_CHECKS = frozenset({
    'revenue_increased', 'operating_margin_improved', 'fcf_positive',
    'leverage_not_rising', 'dividends_paid', 'shares_not_diluting', 'capex_increased',
})


def _usd(*rows: dict) -> None:
    """New premises require USD, including every derived source component."""
    if require_same_unit(rows) != 'USD':
        raise ValueError('USD monetary inputs required')
    for row in rows:
        _number(row['value'])
        for key in ('ttm_components', 'components'):
            components = row.get(key) or {}
            for source in components.values() if isinstance(components, dict) else components:
                _usd(source)


def _premise_rows(fields: dict, field: str, *, comparative: bool = False) -> list[dict]:
    value = fields.get(f'{field}_comparative') if comparative else None
    return value if isinstance(value, list) else fields.get(field) or []


def _filing_pair(fields: dict, field: str, *, annual: bool = False) -> tuple[dict, dict]:
    rows = _premise_rows(fields, field, comparative=True)
    forms = {'10-K', '10-K/A'} if annual else {'10-K', '10-K/A', '10-Q', '10-Q/A'}
    rows = [r for r in rows if r.get('accession') and r.get('form') in forms and r.get('period_end')]
    if field == 'shares' and fields.get('share_latest_accession'):
        rows = [r for r in rows if r['accession'] == fields['share_latest_accession']]
    if annual and fields.get('premise_latest_annual_accession'):
        rows = [r for r in rows if r['accession'] == fields['premise_latest_annual_accession']]
    if not rows:
        raise ValueError(f'{field}: 동일 공시 비교열 미확인')
    current = max(rows, key=lambda r: (r['period_end'], r.get('filed') or '', r['accession']))
    if field == 'shares' and fields.get('share_latest_period_end') not in (None, current['period_end']):
        raise ValueError('최신 공시의 기말 보통주 입력 미확인')
    if annual and fields.get('premise_latest_annual_period_end') not in (None, current['period_end']):
        raise ValueError(f'{field}: 최신 회계연도 당기 비교열 미확인')
    cohort = [r for r in rows if r['accession'] == current['accession']
              and r['period_end'] < current['period_end'] and r.get('tag') == current.get('tag')]
    if field == 'shares' and fields.get('share_prior_period_end'):
        cohort = [r for r in cohort if r['period_end'] == fields['share_prior_period_end']]
    if not cohort:
        raise ValueError(f'{field}: 동일 공시의 전기 비교열 미확인')
    prior = max(cohort, key=lambda r: r['period_end'])
    if annual:
        if (not all(_is_annual_duration(r) for r in (current, prior))
                or abs(_duration_days(current) - _duration_days(prior)) > 8
                or not 350 <= (_day(current['period_end']) - _day(prior['period_end'])).days <= 380
                or not 0 < (_day(current['period_start']) - _day(prior['period_end'])).days <= 8):
            raise ValueError('동일 길이의 연속 회계연도 비교 필요')
    else:
        if any(r.get('period_start') is not None or _day(r['period_end']) is None for r in (current, prior)):
            raise ValueError('기말 시점 입력 필요')
    return current, prior


def _share_pair(fields: dict, claim: dict) -> tuple[dict, dict]:
    """Same-filing annual endpoints; explicit TTM uses year-apart instants."""
    annual = claim.get('horizon') != 'ttm'
    scoped = dict(fields)
    rows = _premise_rows(fields, 'shares', comparative=True)
    scoped.pop('shares_comparative', None)
    scoped.pop('share_prior_period_end', None)
    if annual:
        scoped['shares'] = [r for r in rows if r.get('form') in {'10-K', '10-K/A'}]
        scoped['share_latest_accession'] = fields.get('share_annual_accession')
        scoped['share_latest_period_end'] = fields.get('share_annual_period_end')
    else:
        scoped['shares'] = rows
    # Filter the prior before _filing_pair chooses it: notes may also contain
    # intervening quarters. TTM means a full year, not latest quarter vs FY.
    current, _ = _filing_pair(scoped, 'shares')
    scoped['shares'] = [r for r in scoped['shares'] if r['period_end'] == current['period_end']
        or 350 <= (_day(current['period_end']) - _day(r['period_end'])).days <= 380]
    return _filing_pair(scoped, 'shares')


def _latest_flow(fields: dict, field: str) -> dict:
    rows = fields.get(field) or []
    if not rows:
        raise ValueError(f'missing {field}')
    current = max(rows, key=lambda r: (r.get('period_end') or '', r.get('filed') or ''))
    if not _is_annual_duration(current):
        raise ValueError('TTM 또는 연간 입력 필요')
    if not current.get('accession'):
        raise ValueError('공시 식별자 미확인')
    components = current.get('ttm_components') or {}
    if components:
        annual = components['fy']
        if not _is_annual_duration(annual):
            raise ValueError('TTM의 연간 입력 기간 불일치')
        if set(components) == {'fy'}:
            require_same_period((current, annual))
            if _number(current['value']) != _number(annual['value']):
                raise ValueError('연간 값과 원공시 값 불일치')
        elif set(components) == {'fy', 'current_ytd', 'prior_ytd'}:
            now, prior = components['current_ytd'], components['prior_ytd']
            _same_filing(current, now, prior)
            if (not 0 < _duration_days(now) < 350 or not 0 < _duration_days(prior) < 350
                    or abs(_duration_days(now) - _duration_days(prior)) > 8
                    or prior.get('period_start') != annual.get('period_start')
                    or (_day(now.get('period_start')) - _day(annual.get('period_end'))).days != 1
                    or (_day(current.get('period_start')) - _day(prior.get('period_end'))).days != 1
                    or current.get('period_end') != now.get('period_end')):
                raise ValueError('TTM의 FY·당기 YTD·전년 YTD 기간 불일치')
            if _number(current['value']) != _number(annual['value']) + _number(now['value']) - _number(prior['value']):
                raise ValueError('TTM 조립 값 불일치')
        else:
            raise ValueError('TTM 구성 입력 미확인')
    elif current.get('form') not in {'10-K', '10-K/A'}:
        raise ValueError('연간 공시 또는 TTM 구성 입력 필요')
    return current


def _same_filing(*rows: dict) -> None:
    accessions = {r.get('accession') for r in rows}
    if None in accessions or len(accessions) != 1:
        raise ValueError('동일 공시 입력 필요')


def _evaluate_premise(check: str, claim: dict, fields: dict, inputs: dict, outputs: dict) -> tuple[dict, bool, str]:
    """FY YoY comparisons use one filing's columns; levels use latest TTM/FY.

    Leverage thresholds are percentage points; dilution thresholds are percent.
    Both default to zero. No threshold is inherited from screening criteria.
    """
    if claim.get('period_year'):
        fields = {key: value for key, value in fields.items()
                  if key not in {'premise_latest_annual_accession', 'premise_latest_annual_period_end'}}
    if check in {'revenue_increased', 'capex_increased'}:
        field = 'revenue' if check == 'revenue_increased' else 'capex'
        current, prior = _filing_pair(fields, field, annual=True)
        inputs.update(current=current, prior=prior)
        _usd(current, prior)
        change = _number(current['value']) - _number(prior['value'])
        outputs.update({f'{field}_current': current['value'], f'{field}_prior': prior['value'],
                        'change': decimal_string(change), 'unit': 'USD', 'basis': 'latest_fy'})
        truth = change > 0
        reason = f"{FACT_CHECKS[check][1]}: {prior['value']} → {current['value']} USD"
    elif check == 'operating_margin_improved':
        current, prior = _filing_pair(fields, 'operating_income', annual=True)
        revenue, revenue_prior = _filing_pair(fields, 'revenue', annual=True)
        inputs.update(operating_income=current, operating_income_prior=prior,
                      revenue=revenue, revenue_prior=revenue_prior)
        _usd(current, prior, revenue, revenue_prior)
        _same_filing(current, prior, revenue, revenue_prior)
        require_same_period((current, revenue))
        require_same_period((prior, revenue_prior))
        if min(_number(revenue['value']), _number(revenue_prior['value'])) <= 0:
            raise ValueError('매출이 0 이하이므로 영업이익률 판정 불가')
        now = _number(current['value']) / _number(revenue['value'])
        before = _number(prior['value']) / _number(revenue_prior['value'])
        outputs.update(margin_current=decimal_string(now), margin_prior=decimal_string(before),
                       change_pp=decimal_string((now - before) * 100), basis='latest_fy')
        truth = now > before
        reason = f'영업이익률 {before} → {now}'
    elif check in {'fcf_positive', 'dividends_paid'}:
        field = 'cfo' if check == 'fcf_positive' else 'dividends'
        current = _latest_flow(fields, field)
        inputs[field] = current
        _usd(current)
        value = _number(current['value'])
        if check == 'fcf_positive':
            capex = _latest_flow(fields, 'capex')
            inputs['capex'] = capex
            _usd(capex)
            require_same_period((current, capex))
            _same_filing(current, capex)
            if _number(capex['value']) < 0:
                raise ValueError('설비투자 현금 지급액 부호 미확인')
            value -= _number(capex['value'])
        outputs.update({('fcf' if check == 'fcf_positive' else 'dividends'): decimal_string(value),
                        'unit': 'USD', 'basis': current.get('ttm_method') or 'fy_only'})
        truth = value > 0
        reason = f'{FACT_CHECKS[check][1]}: {value} USD > 0'
    else:
        threshold = _number(claim.get('threshold') if claim.get('threshold') not in (None, '') else '0')
        if check == 'shares_not_diluting':
            current, prior = _share_pair(fields, claim)
            inputs.update(current=current, prior=prior)
            if (require_same_unit((current, prior)) != 'shares'
                    or any(r.get('tag') != 'CommonStockSharesOutstanding' and r.get('derived') != 'issued - treasury'
                           for r in (current, prior))):
                raise ValueError('기말 보통주 주식수(shares) 필요')
            now, before = _number(current['value']), _number(prior['value'])
            if before <= 0 or now < 0:
                raise ValueError('invalid share counts')
            change = (now / before - 1) * 100
            outputs.update(current_shares=current['value'], prior_shares=prior['value'],
                           change_pct=decimal_string(change), threshold_pct=decimal_string(threshold))
        else:
            current, prior = _filing_pair(fields, 'interest_bearing_debt')
            assets, assets_prior = _filing_pair(fields, 'assets')
            inputs.update(debt=current, debt_prior=prior, assets=assets, assets_prior=assets_prior)
            _usd(current, prior, assets, assets_prior)
            _same_filing(current, prior, assets, assets_prior)
            require_same_period((current, assets))
            require_same_period((prior, assets_prior))
            if min(_number(assets['value']), _number(assets_prior['value'])) <= 0:
                raise ValueError('총자산이 0 이하')
            if min(_number(current['value']), _number(prior['value'])) < 0:
                raise ValueError('부채가 음수')
            now = _number(current['value']) / _number(assets['value'])
            before = _number(prior['value']) / _number(assets_prior['value'])
            change = (now - before) * 100
            outputs.update(leverage_current=decimal_string(now), leverage_prior=decimal_string(before),
                           change_pp=decimal_string(change), threshold_pp=decimal_string(threshold))
        truth = change <= threshold
        reason = f'{FACT_CHECKS[check][1]}: 변화 {change}; 주장 기준 ≤ {threshold}'
    return current, truth, reason


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


def _evaluate_commitment(check: str, claim: dict, fields: dict, inputs: dict, outputs: dict) -> tuple[dict, bool, str]:
    from .commitment_periods import balance_for_period, choose_period, flow_for_period, ledger_view
    raw, periods = ledger_view(fields)
    horizon = claim.get('horizon', 'fy' if claim.get('period_year') else 'ttm')
    if check in {'revenue_in_range', 'operating_margin_at_least'} and horizon != 'fy':
        raise ValueError('매출 범위와 영업이익률 목표는 FY 입력 필요')
    period = choose_period(periods, claim)
    def flow(field: str) -> dict:
        row = flow_for_period(raw, periods, field, period, horizon)
        # Reuse the strict USD, component arithmetic and TTM duration guards.
        _latest_flow({field: [row]}, field)
        _usd(row)
        inputs[field] = row
        return row
    if check == 'leverage_at_most':
        current = balance_for_period(raw, 'interest_bearing_debt', period)
        assets = balance_for_period(raw, 'assets', period)
        inputs.update(debt=current, assets=assets)
        _usd(current, assets)
        _same_filing(current, assets)
        require_same_period((current, assets))
        if _number(current['value']) < 0 or _number(assets['value']) <= 0:
            raise ValueError('부채는 0 이상, 총자산은 양수여야 한다')
        value = _number(current['value']) / _number(assets['value'])
        metric, unit = 'leverage', 'ratio'
    elif check == 'operating_margin_at_least':
        current, revenue = flow('operating_income'), flow('revenue')
        _same_filing(current, revenue)
        require_same_period((current, revenue))
        if _number(revenue['value']) <= 0:
            raise ValueError('매출이 0 이하이므로 영업이익률 판정 불가')
        value = _number(current['value']) / _number(revenue['value'])
        metric, unit = 'operating_margin', 'ratio'
    else:
        metric = {'repurchase_at_least': 'repurchase', 'repurchase_positive': 'repurchase',
                  'dividends_paid': 'dividends', 'revenue_in_range': 'revenue',
                  'capex_in_range': 'capex', 'cfo_at_least': 'cfo'}[check]
        current = flow(metric)
        value, unit = _number(current['value']), 'USD'
        if metric in {'repurchase', 'dividends', 'capex', 'revenue'} and value < 0:
            raise ValueError('현금 지급액 또는 매출 부호 미확인')
    if check in {'repurchase_positive', 'dividends_paid'}:
        truth = value > 0
        comparison = '> 0'
    elif check in {'revenue_in_range', 'capex_in_range'}:
        lower, upper = _number(claim['min']), _number(claim['max'])
        if lower > upper:
            raise ValueError('min must not exceed max')
        truth = lower <= value <= upper
        comparison = f'범위 [{lower}, {upper}]'
    elif check == 'leverage_at_most':
        upper = _number(claim['max'])
        truth, comparison = value <= upper, f'≤ {upper}'
    else:
        lower = _number(claim['min'])
        truth, comparison = value >= lower, f'≥ {lower}'
    outputs.update({metric: decimal_string(value), 'unit': unit, 'basis': horizon,
                    'period_year': period['period_year']})
    for bound in ('min', 'max'):
        if bound in claim:
            outputs[bound] = decimal_string(_number(claim[bound]))
    reason = f'{metric} {value} {unit}; {comparison}'
    if check == 'operating_margin_at_least':
        reason += ' · GAAP OperatingIncomeLoss/매출 기준; adjusted/non-GAAP 목표와 다를 수 있습니다.'
    if check == 'leverage_at_most':
        reason += ' · 이자부 총부채/총자산 proxy; 원문의 debt/EBITDA와 다를 수 있습니다.'
    return current, truth, reason


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
    result.pop('share_latest_accession', None)
    result.pop('share_latest_period_end', None)
    result.pop('share_prior_period_end', None)
    result.pop('share_annual_accession', None)
    result.pop('share_annual_period_end', None)
    # Preserve comparative priors for YoY checks, but restrict the current year.
    for key, rows in list(result.items()):
        if isinstance(rows, list):
            result[key] = [row for row in rows if not isinstance(row, dict) or
                           str(row.get('period_end', ''))[:4] <= str(year)]
    return result


class _FactArtifact(ValueError):
    def __init__(self, kind: str, reason: str):
        self.kind = kind
        super().__init__(reason)


def _presentation_problems(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [str(value['presentation_change'])] if value.get('presentation_change') else []
        return own + [p for child in value.values() for p in _presentation_problems(child)]
    if isinstance(value, list):
        return [p for child in value for p in _presentation_problems(child)]
    return []


def _guard_cash_presentation(check: str, fields: dict, fact: dict, claim: dict) -> None:
    names = ('repurchase', 'common_issuance') if check == 'net_buyback_positive' else ('repurchase',)
    if check not in {'net_buyback_positive', 'repurchase_positive', 'repurchase_at_least'}:
        return
    problems = []
    anchors = [max(fields[name], key=lambda r:(r.get('period_end') or '', r.get('filed') or ''))
               for name in names if fields.get(name)]
    def identity(row):
        return row.get('accession'), row.get('period_start'), row.get('period_end')
    source_ids = set()
    for anchor in anchors:
        source_ids.add(identity(anchor))
        for component in (anchor.get('ttm_components') or {}).values():
            source_ids.add(identity(component))
    latest_end = max((r.get('period_end') or '' for r in anchors), default='')
    for name in names:
        rows = fields.get(name) or []
        if rows:
            row = max(rows, key=lambda r: (r.get('period_end') or '', r.get('filed') or ''))
            found = _presentation_problems(row)
            if name == 'repurchase' and any(_number(r['value']) < 0 for r in
                    [row, *(row.get('ttm_components') or {}).values()]):
                found.append('negative gross repurchase: source sign or inconsistent FY/YTD components')
            if found:
                fact['inputs'][name] = row
                problems.extend(found)
        missing = [r for r in (fields.get('presentation_changes') or {}).get(name, [])
                   if not anchors or identity(r) in source_ids
                   or (r.get('period_end') or '') > latest_end]
        year = claim.get('period_year')
        if year:
            missing = [r for r in missing if str(r.get('period_end', ''))[:4] == str(year)]
        if missing:
            fact['inputs'].setdefault('presentation_evidence', []).extend(copy.deepcopy(missing))
            problems.extend(_presentation_problems(missing))
    if problems:
        raise _FactArtifact('presentation_change', 'presentation_change: ' + '; '.join(dict.fromkeys(problems)))


def _excluded_tax_output(fact: dict, fields: dict) -> None:
    rows = fields.get('tax_withholding_acquisitions') or []
    purchase = fact['inputs'].get('repurchase')
    matched = [r for r in rows if not purchase or
               (r.get('period_start'), r.get('period_end')) ==
               (purchase.get('period_start'), purchase.get('period_end'))]
    fact['outputs'].update(tax_withholding_acquisitions=None, tax_withholding_excluded=True)
    if matched:
        row = max(matched, key=lambda r: (r.get('period_end') or '', r.get('filed') or ''))
        fact['inputs']['tax_withholding_acquisitions'] = row
        fact['outputs']['tax_withholding_acquisitions'] = row['value']
        fact['outputs']['tax_withholding_period'] = {k:row.get(k) for k in ('period_start', 'period_end', 'unit')}
    if fact['status'] == '대기':
        return
    fact['reason_ko'] += ' · 자사주 매입=프로그램 현금 매입; 직원 세금 원천징수 취득은 제외(별도 표시, 미확인≠0).'


def evaluate_facts(claims: list[dict], fields: dict) -> list[dict]:
    facts = []
    for claim in claims:
        check = claim.get('check')
        fact = {**copy.deepcopy(claim), 'status': '판정불가', 'inputs': {}, 'outputs': {},
                'reason_ko': '해당 주장을 판정할 원공시 입력이 부족합니다.'}
        fact.setdefault('statement_ko', FACT_CHECKS.get(check, ('', check, []))[1])
        facts.append(fact)
        selected = _scoped_fields(fields, claim)
        inputs, outputs = fact['inputs'], fact['outputs']
        try:
            _guard_cash_presentation(check, selected, fact, claim)
            ledger = check in COMMITMENT_CHECKS or any(
                key in claim for key in ('expect', 'min', 'max', 'horizon', 'kind', 'source_quote'))
            if not isinstance(claim.get('expect', True), bool):
                raise ValueError('expect must be boolean')
            if ledger and check not in LEDGER_CHECKS and claim.get('period_year'):
                from .commitment_periods import choose_period, ledger_view
                raw, periods = ledger_view(fields)
                ledger_period = choose_period(periods, claim)
                # Existing checks keep their comparison formulas. Opted-in
                # fiscal claims select the target filing's own columns.
                selected = {**selected}
                for field, rows in raw.items():
                    cohort = [r for r in rows if r.get('accession') == ledger_period['accession']]
                    selected[field] = cohort
                    if field + '_comparative' in selected:
                        selected[field + '_comparative'] = cohort
            if ledger and check in LEDGER_CHECKS:
                current, truth, reason = _evaluate_commitment(check, claim, fields, inputs, outputs)
            elif check == 'repurchase_positive':
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
                current, prior = _share_pair({**selected, 'shares':rows}, claim)
                pair = (current, prior)
                if pair is None:
                    raise ValueError('동일 공시의 기말 주식수 비교열 미확인')
                current, prior = pair
                inputs.update(current=current, prior=prior)
                if require_same_unit(pair) != 'shares':
                    raise ValueError('기말 주식수 단위 불일치')
                if any(r.get('class_aggregation_incomplete') or r.get('precision_conflict') for r in pair):
                    raise _FactArtifact('rounding', 'share_count_detail: 보통주 종류 누락 또는 정확한 기말 수치 상충')
                now, before = _number(current['value']), _number(prior['value'])
                if before <= 0 or now < 0:
                    raise ValueError('invalid share counts')
                outputs.update(current_shares=current['value'], prior_shares=prior['value'],
                               yoy_change=decimal_string(now / before - 1))
                from .share_counts import share_decrease_uncertain
                if share_decrease_uncertain(current, prior):
                    raise _FactArtifact('rounding', 'rounding: 기말 보통주 주식수의 반올림 범위가 겹쳐 엄격한 감소를 증명할 수 없음')
                truth = now < before
                reason = f"기말 전체 보통주(모든 종류 합산, 이후 표지일 제외) {prior['value']} → {current['value']} shares"
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
            elif check in PREMISE_CHECKS:
                current, truth, reason = _evaluate_premise(check, claim, selected, inputs, outputs)
            else:
                raise ValueError('지원하지 않는 사실 검증')
            if claim.get('period_year') and not (ledger and check in LEDGER_CHECKS):
                if ledger:
                    if current.get('period_end') != ledger_period['period_end']:
                        raise ValueError('주장한 회계기간의 공시 입력 미확인')
                elif str(current.get('period_end', ''))[:4] != str(claim['period_year']):
                    raise ValueError('주장한 연도의 공시 입력 미확인')
            expect = claim.get('expect', True)
            if not expect:
                reason += ' · 부정 주장(expect=false): 관측 조건이 FALSE여야 충족'
            fact.update(status='TRUE' if truth == expect else 'FALSE', reason_ko=reason)
        except (ValueError, KeyError, TypeError, DecimalException) as exc:
            from .commitment_periods import PeriodPending
            if isinstance(exc, _FactArtifact):
                fact.update(artifact_reason=exc.kind, artifact_details=str(exc))
            if isinstance(exc, PeriodPending):
                fact.update(status='대기', reason_ko='대상 기간 공시 전')
            else:
                fact['reason_ko'] = f'판정불가: {exc}'
            fact['outputs'] = {}
        if check in {'repurchase_positive', 'net_buyback_positive', 'repurchase_at_least'}:
            _excluded_tax_output(fact, selected)
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
