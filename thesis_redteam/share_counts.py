"""Period-end common-share scope, class aggregation and precision evidence.

Companyfacts omits dimensions and precision. Inline XBRL in the allowed filing
set can restore them; DEI cover-page shares remain visible but never supply the
period-end balance. Missing HTML never implies an exact count.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
from typing import Any, Callable

from .sec_client import OfflineDataUnavailable, SecNetworkError


_CLASS_AXIS = re.compile(r'(?:classofstock|classesofstock|stockclass|classofcommonstock|classesofsharecapital)', re.I)
_COMMON_SHARES = re.compile(r'^CommonStock(?:Class[A-Za-z0-9]+)?SharesOutstanding$', re.I)


def _rounding_unit(row: dict) -> Decimal:
    if row.get('exact') is True:
        return Decimal(0)
    if 'rounding_unit' in row:
        return Decimal(str(row['rounding_unit']))
    value = abs(Decimal(str(row['value'])))
    for unit in (1_000_000, 1_000):
        if value >= unit and value % unit == 0:
            return Decimal(unit)
    return Decimal(0)


def share_decrease_uncertain(current: dict, prior: dict) -> bool:
    """Whether a displayed decrease could vanish within source rounding.

    Each rounded value describes an interval of half its stated rounding unit.
    Touching intervals do not prove a strict decrease. Exact source counts,
    including exact round numbers, disable the divisibility fallback.
    """
    now, before = Decimal(str(current['value'])), Decimal(str(prior['value']))
    uncertainty = (_rounding_unit(current) + _rounding_unit(prior)) / 2
    return uncertainty > 0 and abs(before - now) <= uncertainty


class _ShareFacts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.contexts: dict[str, dict] = {}
        self.facts: list[dict] = []
        self.context = None
        self.field = None
        self.member = None
        self.active_facts: list[dict] = []
        self.units: dict[str, str] = {}
        self.unit = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'xbrli:context':
            self.context = {'dimensions': [], 'instant': ''}
            self.contexts[attrs.get('id')] = self.context
        elif self.context is not None:
            if tag in {'xbrli:instant', 'xbrli:startdate', 'xbrli:enddate'}:
                self.field = tag.split(':')[-1]
                self.context[self.field] = ''
            elif tag in {'xbrldi:explicitmember', 'xbrldi:typedmember'}:
                self.member = {'axis': attrs.get('dimension', ''), 'member': '',
                               'typed': tag == 'xbrldi:typedmember'}
                self.context['dimensions'].append(self.member)
        if tag == 'xbrli:unit':
            self.unit = attrs.get('id')
            self.units[self.unit] = ''
        if tag == 'ix:nonfraction':
            self.active_facts.append({**attrs, 'text': ''})

    def handle_data(self, data):
        if self.context is not None and self.field:
            self.context[self.field] += data.strip()
        if self.member is not None:
            self.member['member'] += data.strip()
        if self.unit is not None:
            self.units[self.unit] += data.strip()
        for fact in self.active_facts:
            fact['text'] += data

    def handle_endtag(self, tag):
        if tag == 'xbrli:context':
            self.context = None
        if tag in {'xbrli:instant', 'xbrli:startdate', 'xbrli:enddate'}:
            self.field = None
        if tag in {'xbrldi:explicitmember', 'xbrldi:typedmember'}:
            self.member = None
        if tag == 'xbrli:unit':
            self.unit = None
        if tag == 'ix:nonfraction' and self.active_facts:
            self.facts.append(self.active_facts.pop())


def _numeric(fact: dict) -> tuple[str, int] | None:
    text = re.sub(r'[\s,\u00a0]', '', fact['text'])
    if text in {'-', '—', '–'}:
        text = '0'
    try:
        value = Decimal(text) * (Decimal(10) ** int(fact.get('scale', 0)))
        if fact.get('sign') == '-':
            value = -value
        if not value.is_finite() or value < 0:
            return None
        decimals = fact.get('decimals')
        if decimals is not None:
            unit = 0 if decimals.upper() == 'INF' or int(decimals) >= 0 else 10 ** -int(decimals)
        else:
            unit = int(_rounding_unit({'value': str(value)}))
        places = len(text.split('.', 1)[1]) if '.' in text else 0
        display_power = int(fact.get('scale', 0)) - places
        if display_power > 0:
            unit = max(unit, 10 ** display_power)
        if value == value.to_integral_value():
            value = value.quantize(Decimal(1))
        return format(value, 'f'), unit
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _parse(body: bytes, filing: dict, cik: str | int) -> tuple[list[dict], list[dict]]:
    parser = _ShareFacts()
    parser.feed(body.decode('utf-8', errors='replace'))
    parser.close()
    groups: dict[str, list[dict]] = {}
    cover = []
    for fact in parser.facts:
        tag = fact.get('name', '').split(':')[-1]
        is_cover = tag == 'EntityCommonStockSharesOutstanding'
        if (not is_cover and not _COMMON_SHARES.fullmatch(tag)
                and tag not in {'CommonStockSharesIssued', 'TreasuryStockShares', 'TreasuryStockCommonShares'}):
            continue
        ctx = parser.contexts.get(fact.get('contextref'), {})
        end = ctx.get('instant')
        if not end or ctx.get('startdate') or ctx.get('enddate'):
            continue
        unit = parser.units.get(fact.get('unitref'), fact.get('unitref', ''))
        if 'shares' not in unit.lower():
            continue
        number = _numeric(fact)
        if number is None:
            continue
        value, rounding = number
        dimensions = ctx.get('dimensions', [])
        item = {'value': value, 'period_end': end, 'tag': tag,
                'context': fact.get('contextref'), 'rounding_unit': rounding,
                'exact': rounding == 0, 'dimensions': dimensions}
        if is_cover or (filing.get('reportDate') and end > filing['reportDate']):
            cover.append(item)
            continue
        def common_equity(d):
            return (d['axis'].split(':')[-1] == 'StatementEquityComponentsAxis'
                    and d['member'].split(':')[-1] in {'CommonStockMember', 'TreasuryStockCommonMember'})
        if any(d['typed'] or (not _CLASS_AXIS.search(d['axis']) and not common_equity(d)) for d in dimensions):
            continue
        item['class'] = '|'.join(d['member'] for d in dimensions if _CLASS_AXIS.search(d['axis'])) or 'all_common'
        groups.setdefault(end, []).append(item)
    result = []
    for end, candidates in groups.items():
        known_classes = {item['class'] for item in candidates if item['class'] != 'all_common'}
        outstanding = [item for item in candidates if _COMMON_SHARES.fullmatch(item['tag'])]
        issued = [item for item in candidates if item['tag'] == 'CommonStockSharesIssued']
        treasury = [item for item in candidates if item['tag'] in {'TreasuryStockShares', 'TreasuryStockCommonShares'}]
        # Separate issued common classes can share one consolidated treasury
        # count. Subtract that count once, after summing the issued classes.
        issued_classes: dict[str, dict] = {}
        for item in issued:
            old = issued_classes.get(item['class'])
            if old is None or item['rounding_unit'] < old['rounding_unit']:
                issued_classes[item['class']] = item
        consolidated_treasury = [item for item in treasury if item['class'] == 'all_common']
        if (issued_classes and 'all_common' not in issued_classes and consolidated_treasury):
            held = min(consolidated_treasury, key=lambda item: item['rounding_unit'])
            parts = list(issued_classes.values())
            value = sum((Decimal(item['value']) for item in parts), Decimal(0)) - Decimal(held['value'])
            rounding = sum(item['rounding_unit'] for item in parts) + held['rounding_unit']
            if value >= 0:
                outstanding.append({**held, 'class': 'all_common', 'value': format(value, 'f'),
                                    'rounding_unit': rounding, 'exact': rounding == 0,
                                    'derived': 'issued - treasury',
                                    'derived_from': deepcopy(parts + [held])})
        for issue in issued:
            matches = [item for item in treasury if item['class'] == issue['class']]
            if not matches:
                continue
            finest = min(item['rounding_unit'] for item in matches)
            matches = [item for item in matches if item['rounding_unit'] == finest]
            if len({item['value'] for item in matches}) != 1:
                continue
            held = matches[0]
            value = Decimal(issue['value']) - Decimal(held['value'])
            if value < 0:
                continue
            rounding = issue['rounding_unit'] + held['rounding_unit']
            outstanding.append({**issue, 'value': format(value, 'f'),
                                'rounding_unit': rounding, 'exact': rounding == 0,
                                'derived': 'issued - treasury',
                                'derived_from': [deepcopy(issue), deepcopy(held)]})
        candidates = outstanding
        # One finest-precision value per class; repeated notes are not additive.
        classes: dict[str, dict] = {}
        conflicts = set()
        for item in candidates:
            old = classes.get(item['class'])
            rank = (item['rounding_unit'], bool(item.get('derived')))
            old_rank = (old['rounding_unit'], bool(old.get('derived'))) if old is not None else None
            if old is None or rank < old_rank:
                classes[item['class']] = item
                conflicts.discard(item['class'])
            elif rank == old_rank and item['value'] != old['value']:
                conflicts.add(item['class'])
        if conflicts:
            continue
        class_rows = [item for key, item in classes.items() if key != 'all_common']
        # A precise consolidated fact is preferable to rounding each class.
        total = classes.get('all_common')
        complete_direct_classes = (class_rows and all(item['exact'] and not item.get('derived') for item in class_rows)
                                   and not known_classes - {item['class'] for item in class_rows})
        if total is not None and total.get('derived') and complete_direct_classes:
            selected = class_rows
        elif total is not None and (not class_rows or total['exact']
                                  or known_classes - {item['class'] for item in class_rows}
                                  or total['rounding_unit'] <= sum(item['rounding_unit'] for item in class_rows)):
            selected = [total]
        elif class_rows:
            selected = class_rows
        else:
            continue
        value = sum((Decimal(item['value']) for item in selected), Decimal(0))
        rounding = sum(item['rounding_unit'] for item in selected)
        record = {'value': format(value, 'f'), 'unit': 'shares',
                       'period_start': None, 'period_end': end,
                       'tag': 'CommonStockSharesOutstanding',
                       'accession': filing.get('accessionNumber') or filing.get('accession'),
                       'filed': filing.get('filingDate') or filing.get('filed'),
                       'form': filing.get('form'), 'cik': str(cik),
                       'classes': class_rows or selected, 'rounding_unit': rounding,
                       'exact': rounding == 0, 'source': 'primary_html',
                       'primary_document': filing.get('primaryDocument'),
                       'cover_page_shares': deepcopy(cover)}
        if any(item.get('derived') for item in selected):
            record['derived'] = 'issued - treasury'
            record['derived_from'] = [deepcopy(item.get('derived_from', [])) for item in selected]
        if total is None and known_classes - {item['class'] for item in selected}:
            record['class_aggregation_incomplete'] = True
            record['missing_classes'] = sorted(known_classes - {item['class'] for item in selected})
            record['exact'] = False
        result.append(record)
    return result, cover


def enrich_shares(rows: list[dict], filings: list[dict] | None, client: Any,
                  cik: str | int | None, *, before_fetch: Callable[[], Any] | None = None) -> list[dict]:
    """Enrich latest annual/interim periods using only caller-allowed filings.

    The caller supplies the PIT-filtered filing set. No submission search or
    arbitrary history download is performed. A False budget callback skips a
    fetch. Cache/network availability failures preserve conservative row data.
    """
    result = deepcopy(rows)
    for row in result:
        row.setdefault('source', 'companyfacts')
        row.setdefault('rounding_unit', int(_rounding_unit(row)))
        # No apparent rounding is not proof of exactness without inline evidence.
        row.setdefault('exact', row['rounding_unit'] == 0)
    if client is None or cik is None or not filings:
        return result
    financial = [f for f in filings if f.get('form') in {'10-K', '10-K/A', '10-Q', '10-Q/A', '20-F', '20-F/A', '40-F', '40-F/A'}]
    order = lambda f: (f.get('reportDate') or '', f.get('filingDate') or '',
                       f.get('accessionNumber') or f.get('accession') or '')
    selected = []
    if financial:
        selected.append(max(financial, key=order))
    annual = [f for f in financial if f.get('form') in {'10-K', '10-K/A', '20-F', '20-F/A', '40-F', '40-F/A'}]
    if annual and max(annual, key=order) not in selected:
        selected.append(max(annual, key=order))
    parsed = []
    originals_for_amendment = {}
    for filing in selected:
        accession = filing.get('accessionNumber') or filing.get('accession')
        document = filing.get('primaryDocument')
        if not accession or not document:
            continue
        if before_fetch is not None and before_fetch() is False:
            continue
        try:
            body = client.primary_document(cik, accession, document)
        except (OfflineDataUnavailable, SecNetworkError, OSError):
            continue
        counts, cover = _parse(body, filing, cik)
        if not counts and str(filing.get('form', '')).endswith('/A') and filing.get('reportDate'):
            originals = [f for f in financial if f.get('form') == filing['form'][:-2]
                         and f.get('reportDate') == filing['reportDate']]
            if originals:
                original = max(originals, key=order)
                original_accession = original.get('accessionNumber') or original.get('accession')
                originals_for_amendment[original_accession] = accession
                if original not in selected:
                    selected.append(original)
        parsed.extend(counts)
        for row in result:
            if row.get('accession') == accession:
                row['cover_page_shares'] = deepcopy(cover)
    for count in parsed:
        matches = [i for i, row in enumerate(result)
                   if row.get('accession') == count['accession']
                   and row.get('period_end') == count['period_end']
                   and row.get('period_start') is None]
        if matches:
            for i in matches:
                result[i] = {**result[i], **count}
        else:
            result.append(count)
    # Exact counts from another allowed filing can resolve the same period's
    # rounded balance. Keep original filing identity and expose corroboration.
    exact_groups: dict[str, list[dict]] = {}
    for count in parsed:
        if count['exact']:
            exact_groups.setdefault(count['period_end'], []).append(count)
    exact = {end: candidates[0] for end, candidates in exact_groups.items()
             if len({Decimal(item['value']) for item in candidates}) == 1}
    for row in result:
        candidates = exact_groups.get(row.get('period_end'), [])
        if candidates and row.get('period_end') not in exact:
            row['precision_conflict'] = [{'value': item['value'], 'accession': item['accession']}
                                         for item in candidates]
        confirmed = exact.get(row.get('period_end'))
        if confirmed and not row.get('exact') and not row.get('class_aggregation_incomplete'):
            old_accession = row.get('accession')
            row.update(value=confirmed['value'], classes=deepcopy(confirmed['classes']),
                       exact=True, rounding_unit=0, source='primary_html',
                       precision_source_accession=confirmed['accession'],
                       precision_source=deepcopy(confirmed))
            row['accession'] = old_accession
    for row in result:
        amendment = originals_for_amendment.get(row.get('accession'))
        if amendment:
            row['amendment_accession'] = amendment
            row['amendment_note'] = 'same-period original; amendment has no period-end share facts'
    return result
