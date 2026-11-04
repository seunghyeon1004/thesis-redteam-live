"""Decision-document gate: a review report cannot be submitted while an attached broken premise is unresolved.

Findings come from the sealed live season (premises that newly became FALSE after a filing).
Every attach, resolution and submit attempt is written to the append-only ledger, including rejections.
"""
import json
from pathlib import Path

from gate.ledger import Ledger

ACTIONS = ("조사 착수", "기각")
SEASON = Path(__file__).resolve().parent.parent / "live_season.json"


class GateError(ValueError):
    pass


def load_findings(season_path=SEASON):
    data = json.loads(Path(season_path).read_text(encoding="utf-8"))
    reports = {}
    for c in data["catches"]:
        broken = [k for k in c["changed"] if c["new"].get(k) == "FALSE"]
        if not broken:
            continue
        rid = f"{c['ticker']}-{c['memo']}"
        reports[rid] = {
            "report": rid,
            "ticker": c["ticker"],
            "form": c["form"],
            "accepted_utc": c["accepted_utc"],
            "findings": [{"id": f"{rid}:{k}", "premise": k, "old": c["old"].get(k), "new": c["new"][k]} for k in broken],
            "agent_headline": c.get("agent_headline"),
            "source_check": c.get("source_check"),
        }
    return reports


class Gate:
    def __init__(self, ledger_path, season_path=SEASON):
        self.ledger = Ledger(ledger_path)
        self.reports = load_findings(season_path)

    def _report(self, rid):
        if rid not in self.reports:
            raise GateError(f"unknown report: {rid}")
        return self.reports[rid]

    def _events(self, rid):
        return [e for e in self.ledger.entries() if e["report"] == rid]

    def state(self, rid):
        r = self._report(rid)
        events = self._events(rid)
        attached = {e["data"]["finding"] for e in events if e["event"] == "attached"}
        resolved = {}
        for e in events:
            if e["event"] == "resolved":
                resolved[e["data"]["finding"]] = e["data"]
        findings = [{**f, "attached": f["id"] in attached, "resolution": resolved.get(f["id"])} for f in r["findings"]]
        return {**r, "findings": findings, "unresolved": [f["id"] for f in findings if f["attached"] and not f["resolution"]],
                "submitted": any(e["event"] == "submitted" for e in events)}

    def attach(self, rid):
        """Insert the report's broken premises. Idempotent: a finding is attached once and never removed."""
        st = self.state(rid)
        for f in st["findings"]:
            if not f["attached"]:
                self.ledger.append(rid, "attached", {"finding": f["id"], "status": f"{f['old']} → {f['new']}"})
        return self.state(rid)

    def resolve(self, rid, finding, action, reason, actor):
        st = self.state(rid)
        ids = {f["id"]: f for f in st["findings"]}
        if finding not in ids or not ids[finding]["attached"]:
            raise GateError(f"finding not attached: {finding}")
        if ids[finding]["resolution"]:
            raise GateError(f"already resolved: {finding}")
        if action not in ACTIONS:
            raise GateError(f"action must be one of {ACTIONS}")
        if not (reason or "").strip() or not (actor or "").strip():
            raise GateError("reason and actor are required")
        return self.ledger.append(rid, "resolved", {"finding": finding, "action": action, "reason": reason.strip(), "actor": actor.strip()})

    def submit(self, rid, actor):
        """(True, receipt entry) or (False, rejection entry). Both outcomes are recorded."""
        if not (actor or "").strip():
            raise GateError("actor is required")
        st = self.state(rid)
        if st["unresolved"]:
            return False, self.ledger.append(rid, "submit_rejected", {"actor": actor.strip(), "unresolved": st["unresolved"]})
        if not any(f["attached"] for f in st["findings"]):
            return False, self.ledger.append(rid, "submit_rejected", {"actor": actor.strip(), "unresolved": ["premises not attached"]})
        return True, self.ledger.append(rid, "submitted", {"actor": actor.strip(), "resolved": [f["id"] for f in st["findings"]]})
