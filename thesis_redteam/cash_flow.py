"""Conservative primary-HTML evidence for an absent cash-flow issuance line."""

from html.parser import HTMLParser
import re


ABSENT_ISSUANCE_NOTE = "현금흐름표 재무활동에 보통주 발행 줄이 없어 0으로 간주(원문 확인)"


class _CashFlowTables(HTMLParser):
    """Preserve statement rows and link inline facts to exact duration contexts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.stack = []
        self.contexts = {}
        self.context = None
        self.date_field = None
        self.hidden = None
        self.visible_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "xbrli:context":
            self.context = {"segmented": False}
            self.contexts[attrs.get("id")] = self.context
        elif self.context is not None:
            if tag in {"xbrli:startdate", "xbrli:enddate"}:
                self.date_field = tag
                self.context[tag] = ""
            elif tag in {"xbrli:segment", "xbrli:scenario"}:
                self.context["segmented"] = True
        if tag in {"script", "style", "ix:hidden"}:
            self.hidden = tag
        if tag == "table":
            self.stack.append({"parts": [], "facts": set(), "rows": [], "row": None})
        for table in self.stack:
            if tag == "tr":
                table["parts"].append("\n")
                table["row"] = {"parts": [], "facts": set()}
            elif tag in {"td", "th", "div", "p", "br"}:
                table["parts"].append(" ")
            elif tag == "ix:nonfraction" and not self.hidden:
                table["facts"].add((attrs.get("name"), attrs.get("contextref")))
                if table["row"] is not None:
                    table["row"]["facts"].add((attrs.get("name"), attrs.get("contextref")))

    def handle_endtag(self, tag):
        if tag == self.date_field:
            self.date_field = None
        if tag == "xbrli:context":
            self.context = None
        if tag == self.hidden:
            self.hidden = None
        for table in self.stack:
            if tag == "tr":
                table["parts"].append("\n")
                if table["row"] is not None:
                    table["rows"].append(table["row"])
                    table["row"] = None
            elif tag in {"td", "th", "div", "p"}:
                table["parts"].append(" ")
        if tag == "table" and self.stack:
            self.tables.append(self.stack.pop())

    def handle_data(self, data):
        if self.context is not None and self.date_field:
            self.context[self.date_field] += data.strip()
        if not self.hidden:
            self.visible_parts.append(data)
            for table in self.stack:
                table["parts"].append(data)
                if table["row"] is not None:
                    table["row"]["parts"].append(data + " ")


def _aggregated_financing_line(label: str) -> bool:
    # Residual "Other financing, net" differs from a specific loan/debt line
    # that happens to mention other financing liabilities.
    return bool(re.match(r'^(?:all\s+)?other(?:\s+financing(?:\s+activities)?)?\s*'
                         r'(?:[:,(]|net\b|[-−—–\d$]|$)', label.strip(), re.I))


def common_issuance_absent(body: bytes, repurchase: dict) -> bool:
    """Missing is zero only in a complete financing section for this fact's period.

    Narrative headings, truncated tables, other periods and segmented subsidiary
    contexts cannot establish absence. Non-inline documents remain unconfirmed.
    """
    parser = _CashFlowTables()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    contexts = {key for key, ctx in parser.contexts.items()
                if not ctx["segmented"]
                and ctx.get("xbrli:startdate") == repurchase.get("period_start")
                and ctx.get("xbrli:enddate") == repurchase.get("period_end")}
    sections = []
    for table in parser.tables:
        if not any(name == f"us-gaap:{repurchase['tag']}" and context in contexts
                   for name, context in table["facts"]):
            continue
        rows = [" ".join(row.split()) for row in "".join(table["parts"]).splitlines() if row.strip()]
        text = " ".join(rows)
        if not (re.search(r"\boperating activities\b", text, re.I)
                and re.search(r"\binvesting activities\b", text, re.I)):
            continue
        for index, row in enumerate(rows):
            if not re.fullmatch(r"(?:cash flows? from )?financing activities\s*:?", row, re.I):
                continue
            end = next((i for i in range(index + 1, len(rows))
                        if re.match(r"net cash\b.*\bfinancing activities\b", rows[i], re.I)), None)
            if end is None:
                return False
            sections.append(rows[index + 1:end])
    if not sections:
        return False
    action = r"\b(?:proceeds|issuance|shares issued)\b"
    common = (r"\b(?:common (?:stock|shares)|stock options?|stock (?:purchase )?plans?|"
              r"employee (?:stock|benefit plans?)|shares issued)\b")
    pattern = re.compile(rf"{action}.{{0,160}}{common}|{common}.{{0,160}}{action}|\bshares issued\b", re.I)
    # An aggregated financing line or a net repurchase line can hide issuance.
    # Absence of a tag alone never establishes absence of cash proceeds.
    ambiguous = re.compile(r"repurchas.{0,100}\bnet\b", re.I)
    return not any(pattern.search(row) or ambiguous.search(row) or _aggregated_financing_line(row)
                   for section in sections for row in section)



def cash_flow_presentation(body: bytes, source: dict) -> dict:
    """Read row labels in a duration-matched financing statement, not footnotes.

    This is evidence of presentation, never an estimate of a missing amount.
    A net amount cannot establish gross program purchases or gross issuance.
    """
    parser = _CashFlowTables()
    parser.feed(body.decode('utf-8', errors='replace'))
    contexts = {key for key, ctx in parser.contexts.items()
                if not ctx['segmented']
                and ctx.get('xbrli:startdate') == source.get('period_start')
                and ctx.get('xbrli:enddate') == source.get('period_end')}
    result = {}
    for table in parser.tables:
        text = ' '.join(table['parts'])
        if not all(re.search(rf'\b{kind} activities\b', text, re.I)
                   for kind in ('operating', 'investing', 'financing')):
            continue
        if not any(context in contexts for _, context in table['facts']):
            continue
        financing = False
        for row in table['rows']:
            label = ' '.join(''.join(row['parts']).split())
            if re.match(r'(?:cash flows? (?:from|for|used in|provided by) )?financing activities', label, re.I):
                financing = True
                continue
            if re.match(r'(?:net )?cash.*investing activities', label, re.I):
                # Some statements omit section headings; a complete investing
                # subtotal still marks the start of the financing block.
                financing = True
                continue
            if re.match(r'(?:net )?cash.*financing activities', label, re.I):
                financing = False
            if not financing:
                continue
            if row['facts'] and not any(ctx in contexts for _, ctx in row['facts']):
                continue
            row_tags = [name.split(':')[-1] for name, ctx in row['facts'] if ctx in contexts and name]
            repurchase = bool(re.search(r'repurchas|purchases? of (?:common |treasury )?(?:stock|shares)', label, re.I))
            issuance = bool(re.search(r'proceeds|issuance|options? exercised|stock plans?', label, re.I))
            equity = bool(re.search(r'common|stock|shares|options?', label, re.I))
            net = bool(re.search(r'\bnet\b', label, re.I))
            if repurchase and (net or issuance):
                for field in ('repurchase', 'common_issuance'):
                    result[field] = {'presentation':'net', 'presentation_change':
                                     'net repurchases/issuance line; gross cash components unavailable',
                                     'presentation_line':label, 'presentation_tags':row_tags}
            else:
                if repurchase:
                    result.setdefault('repurchase', {'presentation':'gross', 'presentation_line':label, 'presentation_tags':row_tags})
                if issuance and equity:
                    result.setdefault('common_issuance', {'presentation':'gross', 'presentation_line':label, 'presentation_tags':row_tags})
            if _aggregated_financing_line(label):
                result.setdefault('aggregated', {'presentation':'aggregated',
                                  'presentation_change':'cash-flow lines aggregated into other financing',
                                  'presentation_line':label, 'presentation_tags':row_tags})
    # A generic common-stock cash tag may describe employee tax settlement,
    # which the memo excludes. Only explicit exclusion language is evidence;
    # merely mentioning withholding elsewhere does not invalidate a program row.
    visible = ' '.join(' '.join(parser.visible_parts).split())
    excluded = re.search(
        r'shares of common stock purchased relate to shares withheld.{0,180}withholding taxes'
        r'|share withholding is considered a share repurchase for accounting purposes\..{0,180}'
        r'repurchases do not fall under.{0,100}repurchase authorization', visible, re.I)
    purchase = result.get('repurchase')
    if (excluded and (not purchase or (purchase.get('presentation') == 'gross'
            and 'PaymentsForRepurchaseOfCommonStock' in purchase.get('presentation_tags', [])
            and not re.search(r'program|authorization', purchase.get('presentation_line', ''), re.I)))):
        # Narrative exclusion still matters when a statement layout or heading
        # is not recognized. This establishes uncertainty, never a cash amount.
        purchase = result.setdefault('repurchase', {'presentation':'scope_unconfirmed'})
        purchase.update(presentation_change='employee-tax withholding accounted as repurchases; program scope unconfirmed',
                        scope_evidence=excluded.group(0))
    return result
