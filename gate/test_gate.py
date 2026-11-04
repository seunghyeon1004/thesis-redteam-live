import json
import threading
import urllib.error
import urllib.request

import pytest

from gate.ledger import Ledger
from gate.server import serve
from gate.service import Gate, GateError

CLS = "CLS-shareholder_return"


@pytest.fixture
def gate(tmp_path):
    return Gate(tmp_path / "ledger.jsonl")


def test_reports_come_from_sealed_season(gate):
    assert CLS in gate.reports
    assert [f["premise"] for f in gate.reports[CLS]["findings"]] == ["F2"]
    assert "AZO-earnings_quality" not in gate.reports  # FALSE -> TRUE is a recovery, not a broken premise


def test_submit_rejected_until_resolved(gate):
    ok, entry = gate.submit(CLS, "담당자A")
    assert not ok and entry["data"]["unresolved"] == ["premises not attached"]
    gate.attach(CLS)
    ok, entry = gate.submit(CLS, "담당자A")
    assert not ok and entry["event"] == "submit_rejected" and entry["data"]["unresolved"] == [f"{CLS}:F2"]
    gate.resolve(CLS, f"{CLS}:F2", "조사 착수", "공모 현금 유입 확인 후 비중 재검토", "담당자A")
    ok, entry = gate.submit(CLS, "담당자A")
    assert ok and entry["event"] == "submitted"


def test_resolution_requires_reason_actor_and_valid_action(gate):
    gate.attach(CLS)
    for args in [("조사 착수", " ", "담당자A"), ("조사 착수", "사유", ""), ("무시", "사유", "담당자A")]:
        with pytest.raises(GateError):
            gate.resolve(CLS, f"{CLS}:F2", *args)
    with pytest.raises(GateError):
        gate.resolve(CLS, f"{CLS}:F9", "기각", "사유", "담당자A")


def test_resolution_cannot_be_overwritten(gate):
    gate.attach(CLS)
    gate.resolve(CLS, f"{CLS}:F2", "기각", "일회성 거래", "담당자A")
    with pytest.raises(GateError):
        gate.resolve(CLS, f"{CLS}:F2", "조사 착수", "다시 씀", "담당자B")


def test_attach_is_idempotent(gate):
    gate.attach(CLS)
    gate.attach(CLS)
    assert sum(e["event"] == "attached" for e in gate.ledger.entries()) == 1


def test_tampering_breaks_the_chain(tmp_path):
    g = Gate(tmp_path / "l.jsonl")
    g.attach(CLS)
    g.resolve(CLS, f"{CLS}:F2", "기각", "원래 사유", "담당자A")
    g.submit(CLS, "담당자A")
    assert g.ledger.verify() == (True, None)
    lines = (tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("원래 사유", "몰래 바꾼 사유")
    (tmp_path / "l.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert Ledger(tmp_path / "l.jsonl").verify() == (False, 2)


def test_deleting_an_entry_breaks_the_chain(tmp_path):
    g = Gate(tmp_path / "l.jsonl")
    g.attach(CLS)
    g.resolve(CLS, f"{CLS}:F2", "기각", "사유", "담당자A")
    g.submit(CLS, "담당자A")
    lines = (tmp_path / "l.jsonl").read_text(encoding="utf-8").splitlines()
    (tmp_path / "l.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    assert Ledger(tmp_path / "l.jsonl").verify() == (False, 2)


def _call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_end_to_end(tmp_path):
    httpd = serve(tmp_path / "api.jsonl")
    assert httpd.server_address[0] == "127.0.0.1"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        assert _call(base, "POST", f"/reports/{CLS}/attach", {})[0] == 200
        code, body = _call(base, "POST", f"/reports/{CLS}/submit", {"actor": "담당자A"})
        assert code == 409 and body["data"]["unresolved"] == [f"{CLS}:F2"]
        assert _call(base, "POST", f"/reports/{CLS}/resolve", {"finding": f"{CLS}:F2", "action": "기각", "reason": "", "actor": "A"})[0] == 400
        assert _call(base, "POST", f"/reports/{CLS}/resolve", {"finding": f"{CLS}:F2", "action": "조사 착수", "reason": "현금 유입 확인", "actor": "담당자A"})[0] == 200
        assert _call(base, "POST", f"/reports/{CLS}/submit", {"actor": "담당자A"})[0] == 200
        assert _call(base, "DELETE", "/ledger/verify")[0] == 405
        code, v = _call(base, "GET", "/ledger/verify")
        assert v["ok"] and v["count"] == 4  # attached, submit_rejected, resolved, submitted (invalid input is refused, not logged)
    finally:
        httpd.shutdown()
