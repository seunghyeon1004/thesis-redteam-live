"""Fiscal-period selection for WATCH, isolated from legacy screening inputs.

SEC ``fy`` labels the filing, including its comparative columns. Match each
column to its own duration before assigning a fiscal year. Never add overlapping
quarters: TTM is an annual total plus current YTD minus comparable prior YTD.
"""
from __future__ import annotations

from .extract import (_annual_record, _day, _duration_days, _is_annual_duration,
                      _quarter_ytd_records, _ttm_record)

ANNUAL = {'10-K', '10-K/A'}
QUARTERLY = {'10-Q', '10-Q/A'}


class PeriodPending(ValueError):
    pass


def fiscal_cohorts(raw: dict, filings: list | None = None) -> list[dict]:
    groups: dict[str, list] = {}
    for rows in raw.values():
        for row in rows or []:
            if row.get('accession') and row.get('form') in ANNUAL | QUARTERLY:
                groups.setdefault(row['accession'], []).append(row)
    result = []
    metadata = {f.get('accession') or f.get('accessionNumber'): f for f in filings or []}
    for accession, meta in metadata.items():
        if accession and meta.get('form') in ANNUAL | QUARTERLY:
            groups.setdefault(accession, [])
    for accession, rows in groups.items():
        meta = metadata.get(accession, {})
        form = meta.get('form') or rows[0]['form']
        durations = [r for r in rows if r.get('period_start') and r.get('period_end')
                     and (_is_annual_duration(r) if form in ANNUAL else 0 < _duration_days(r) < 350)]
        # History-inferred reportDate may be a later cover-page share count.
        end = (meta.get('reportDate') if not meta.get('history_source') else None) or max(
            (r['period_end'] for r in durations), default=None)
        if end is None:
            continue
        current = [r for r in durations if r['period_end'] == end]
        labels = [r['fy'] for r in (current or rows) if r.get('fy') is not None]
        invalid_label = any(isinstance(label, bool) or not str(label).isdigit() for label in labels)
        years = {int(label) for label in labels} if not invalid_label else set()
        if invalid_label or len(years) > 1:
            year = None
        elif years:
            year = years.pop()
        else:
            # Calendar-year fixtures/filers are unambiguous; early-calendar
            # fiscal closes need their SEC fiscal label rather than a guess.
            year = int(end[:4]) if end[5:7] == '12' else None
        cohort = dict(accession=accession, form=form, period_end=end,
                           period_start=min((r['period_start'] for r in current), default=None),
                           period_year=year, filed=max((r.get('filed') or '' for r in rows),
                                                       default=meta.get('filing_date') or meta.get('filingDate') or ''))
        result.append(cohort)
        if form in ANNUAL and year is not None:
            # Later annual filings can be the only visible source of a target
            # year's full comparative. Its fy still names the later filing.
            for prior_end in sorted({r['period_end'] for r in durations if r['period_end'] < end}):
                gap = (_day(end) - _day(prior_end)).days
                elapsed = round(gap / 365.25)
                if elapsed < 1 or not 350 * elapsed <= gap <= 380 * elapsed:
                    continue
                prior = [r for r in durations if r['period_end'] == prior_end]
                result.append({**cohort, 'period_end': prior_end,
                               'period_start': min(r['period_start'] for r in prior),
                               'period_year': year - elapsed})
    return result


def ledger_view(fields: dict) -> tuple[dict, list[dict]]:
    ledger = fields.get('commitment_ledger')
    if ledger is not None:
        return ledger['fields'], ledger['periods']
    raw = {key: value for key, value in fields.items()
           if isinstance(value, list) and not key.endswith('_comparative')}
    # Fixtures/callers may already provide assembled TTM. Derive period
    # visibility from its original filing components, not its synthetic start.
    expanded = {key: [c for r in rows for c in
        ((r.get('ttm_components') or {}).values() or [r])] for key, rows in raw.items()}
    return raw, fiscal_cohorts(expanded)


def choose_period(periods: list[dict], claim: dict) -> dict:
    horizon = claim.get('horizon', 'fy' if claim.get('period_year') else 'ttm')
    if horizon not in {'fy', 'ttm'}:
        raise ValueError('horizon must be fy or ttm')
    year = claim.get('period_year')
    if year not in (None, ''):
        if isinstance(year, bool) or not str(year).isdigit():
            raise ValueError('대상 회계연도 형식 오류')
        candidates = [p for p in periods if p['period_year'] == int(year)
                      and (horizon == 'ttm' or p['form'] in ANNUAL)]
        if not candidates:
            # Data may have been truncated or fiscal labels may be unknown.
            # Only declare pending when there is no evidence the year closed.
            later = [p for p in periods if p['period_year'] is not None
                     and p['period_year'] > int(year)
                     and (horizon == 'ttm' or p['form'] in ANNUAL)]
            if later or any(p['period_year'] is None for p in periods):
                raise ValueError('대상 회계기간 공시 식별 불가')
            raise PeriodPending('대상 기간 공시 전')
    else:
        candidates = [p for p in periods if horizon == 'ttm' or p['form'] in ANNUAL]
        if not candidates:
            raise ValueError('공시 회계기간 미확인')
    return max(candidates, key=lambda p: (p['period_end'], p.get('filed') or '', p['accession']))


def flow_for_period(raw: dict, periods: list[dict], field: str, period: dict, horizon: str) -> dict:
    rows = raw.get(field) or []
    filing = {**period, 'reportDate': period['period_end']}
    if period['form'] in ANNUAL:
        row = _annual_record(rows, filing)
        if row is None:
            raise ValueError(f'{field}: 대상 FY 입력 미확인')
        return row
    if horizon != 'ttm':
        raise ValueError('연간 공시 필요')
    # A supplied TTM still undergoes arithmetic/provenance validation in facts.
    assembled = [r for r in rows if r.get('ttm_components') and
                 r.get('accession') == period['accession'] and r.get('period_end') == period['period_end']]
    if assembled:
        return max(assembled, key=lambda r: r.get('filed') or '')
    annuals = [p for p in periods if p['form'] in ANNUAL
               and p['period_end'] < period['period_end']]
    if not annuals:
        raise ValueError(f'{field}: TTM 연간 입력 미확인')
    annual_period = max(annuals, key=lambda p: (p['period_end'], p.get('filed') or ''))
    annual = _annual_record(rows, {**annual_period, 'reportDate': annual_period['period_end']})
    pair = _quarter_ytd_records(rows, filing, annual=annual) if annual else None
    if not pair or pair[0].get('period_end') != period['period_end']:
        raise ValueError(f'{field}: TTM FY·당기 YTD·전년 YTD 입력 미확인')
    result = _ttm_record(annual, *pair, method='fy_plus_ytd')
    if result is None:
        raise ValueError(f'{field}: TTM 조립 불가')
    return result


def balance_for_period(raw: dict, field: str, period: dict) -> dict:
    rows = [r for r in raw.get(field) or [] if r.get('accession') == period['accession']
            and r.get('period_end') == period['period_end'] and r.get('period_start') is None]
    if not rows:
        raise ValueError(f'{field}: 대상 기말 입력 미확인')
    return max(rows, key=lambda r: r.get('filed') or '')
