"""Decision-document gate tab. Uses the same server-side gate as the API (gate/service.py), one ledger file per viewer session,
and shows the recorded end-to-end run whose head hash is committed to GitHub (gate/ANCHOR.json)."""
import json
import tempfile
import uuid
from pathlib import Path

import streamlit as st

from gate.ledger import Ledger
from gate.service import ACTIONS, Gate, GateError

HERE = Path(__file__).parent


def _gate():
    sid = st.session_state.setdefault("gate_sid", uuid.uuid4().hex)
    return Gate(Path(tempfile.gettempdir()) / "thesis-gate" / f"{sid}.jsonl")


def _esc(s):
    return str(s).replace("$", "\\$")


def render_recorded():
    anchor = json.loads((HERE / "gate" / "ANCHOR.json").read_text(encoding="utf-8"))
    ledger = Ledger(HERE / anchor["ledger"])
    ok, bad = ledger.verify()
    st.markdown(f"#### 기록된 전 과정 실행 · `{anchor['run']}`")
    st.caption(f"마지막 해시 {anchor['head'][:16]}… 를 GitHub 커밋으로 고정 · 지금 다시 계산: "
               + ("✓ 일치, 체인 정상" if ok and ledger.head()["head"] == anchor["head"] else f"✗ 불일치 (seq {bad})"))
    for e in ledger.entries():
        d = e["data"]
        detail = d.get("reason") or d.get("status") or ", ".join(d.get("unresolved", [])) or ""
        st.markdown(_esc(f"`#{e['seq']}` **{e['report']}** · {e['event']} · {d.get('actor', '')} {d.get('action', '')} {detail}"))


def render():
    st.markdown("봉인 후 채점에서 **실제로 깨짐이 나온 전제**가 투자 검토 보고서에 자동으로 붙습니다. "
                "처리(조사 착수 또는 사유 있는 기각)하지 않으면 **제출 API가 거부(409)**하고, 거부도 기록됩니다.")
    st.caption("이 화면의 기록은 서버 파일에 추가만 됩니다(보는 사람마다 별도 파일, 클라우드 재시작 시 초기화). "
               "공식 기록은 아래 '기록된 전 과정 실행'이며 해시가 GitHub에 고정돼 있습니다. KIC 결재 시스템 연동은 아직 없습니다.")
    gate = _gate()
    rid = st.selectbox("보유 검토 보고서", sorted(gate.reports))
    state = gate.attach(rid)
    r = gate.reports[rid]
    if r.get("agent_headline"):
        st.markdown(_esc(f"Agent 조사: {r['agent_headline']}"))
    if r.get("source_check"):
        st.markdown(_esc(f"원문 대조: **{r['source_check'].get('verdict')}** — {r['source_check'].get('basis')}"))
    for f in state["findings"]:
        res = f["resolution"]
        st.markdown(_esc(f"**{f['premise']}** {f['old']} → **{f['new']}** · 삭제 불가 · "
                         + (f"처리됨: {res['action']} ({res['actor']}) — {res['reason']}" if res else "⛔ 미처리")))
    open_ids = state["unresolved"]
    if open_ids:
        with st.form(f"resolve-{rid}", clear_on_submit=True):
            finding = st.selectbox("처리할 전제", open_ids)
            action = st.radio("처리", ACTIONS, horizontal=True)
            reason = st.text_input("사유 (필수)")
            actor = st.text_input("담당자 (필수)", value="담당자A")
            if st.form_submit_button("기록하고 처리"):
                try:
                    gate.resolve(rid, finding, action, reason, actor)
                    st.rerun()
                except GateError as e:
                    st.error(f"처리 거부: {e}")
    if st.button("보고서 제출"):
        ok, entry = gate.submit(rid, "담당자A")
        if ok:
            st.success(f"✅ 제출됨 · 영수증 해시 {entry['hash'][:16]}…")
        else:
            st.error("⛔ 409 제출 거부 — 미처리: " + ", ".join(entry["data"]["unresolved"]))
    ok, bad = gate.ledger.verify()
    st.markdown("#### 이 세션의 기록 (추가 전용 해시 체인)")
    st.caption("✓ 체인 정상" if ok else f"✗ 체인 손상 seq {bad}")
    for e in gate.ledger.entries():
        st.markdown(_esc(f"`#{e['seq']}` {e['report']} · {e['event']} · `{e['hash'][:12]}…`"))
    st.divider()
    render_recorded()
