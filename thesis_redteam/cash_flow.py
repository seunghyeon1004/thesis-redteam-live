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
            self.stack.append({"parts": [], "facts": set()})
        for table in self.stack:
            if tag == "tr":
                table["parts"].append("\n")
            elif tag in {"td", "th", "div", "p", "br"}:
                table["parts"].append(" ")
            elif tag == "ix:nonfraction" and not self.hidden:
                table["facts"].add((attrs.get("name"), attrs.get("contextref")))

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
            elif tag in {"td", "th", "div", "p"}:
                table["parts"].append(" ")
        if tag == "table" and self.stack:
            self.tables.append(self.stack.pop())

    def handle_data(self, data):
        if self.context is not None and self.date_field:
            self.context[self.date_field] += data.strip()
        if not self.hidden:
            for table in self.stack:
                table["parts"].append(data)


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
    return not any(pattern.search(row) for section in sections for row in section)
