"""Decision-document gate on real post-seal cases. Session-only demo: no server store or external hash anchor yet."""
import hashlib
import json
from pathlib import Path

import streamlit as st

GENESIS = "GENESIS"


def entry_hash(entry):
    core = {k: entry[k] for k in ("case", "action", "reason", "prev")}
    return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def append(ledger, case, action, reason):
    prev = ledger[-1]["hash"] if ledger else GENESIS
    entry = {"case": case, "action": action, "reason": reason, "prev": prev}
    entry["hash"] = entry_hash(entry)
    return [*ledger, entry]


def chain_ok(ledger):
    prev = GENESIS
    for e in ledger:
        if e["prev"] != prev or e["hash"] != entry_hash(e):
            return False
        prev = e["hash"]
    return True


def broken_cases(data):
    """Premises that newly became FALSE. A source-checked false alarm stays visible: the reviewer dismisses it with a reason."""
    return [c for c in data["catches"] if any(c["new"].get(k) == "FALSE" for k in c["changed"])]


def render():
    data = json.loads((Path(__file__).parent / "live_season.json").read_text(encoding="utf-8"))
    st.markdown("봉인 후 채점에서 **실제로 깨짐이 나온 전제**를 보고서에 붙인 시연입니다. 처리하지 않으면 상정 버튼이 열리지 않습니다.")
    st.caption("시연 범위: 기록은 이 브라우저 세션에만 있고 서버 저장·해시 외부 고정·결재 연동은 아직 없습니다. 투자 결론과 담당자 입력은 직접 입력합니다.")
    ledger = st.session_state.setdefault("gate_ledger", [])
    cases = broken_cases(data)
    labels = {f"{c['ticker']} · {c['form']} · {c['memo']}": c for c in cases}
    case = labels[st.selectbox("보유 검토 보고서", list(labels))]
    key = f"{case['ticker']}|{case['memo']}"
    for k in case["changed"]:
        st.markdown(f"**{k}** {case['old'].get(k)} → **{case['new'][k]}** · 삭제 불가")
    check = case.get("source_check") or {}
    if check:
        st.markdown(f"원문 대조: **{check.get('verdict')}** — {check.get('basis')}".replace("$", "\\$"))
    done = [e for e in ledger if e["case"] == key]
    with st.form(f"resolve-{key}", clear_on_submit=True):
        action = st.radio("처리", ["조사 착수", "기각"], horizontal=True)
        reason = st.text_input("사유 (필수)")
        if st.form_submit_button("기록하고 처리"):
            if reason.strip():
                ledger = st.session_state["gate_ledger"] = append(ledger, key, action, reason.strip())
                done = [e for e in ledger if e["case"] == key]
            else:
                st.error("사유를 입력해야 처리됩니다.")
    st.button("보고서 상정", disabled=not done, key="submit", help="깨진 전제를 처리해야 열립니다")
    st.markdown("⛔ 제출 차단 — 미처리 깨진 전제가 있습니다" if not done else "✅ 상정 가능 — 처리 이력이 기록됐습니다")
    st.markdown("#### 처리 기록 (세션 해시 체인)")
    for i, e in enumerate(ledger, 1):
        st.markdown(f"#{i} {e['case']} · {e['action']} · {e['reason']}  \n`hash {e['hash'][:16]}… ← {e['prev'][:16]}`".replace("$", "\\$"))
    if ledger:
        c = st.columns(2)
        if c[0].button("체인 검증"):
            st.markdown("✓ 연결 정상" if chain_ok(ledger) else "✗ 체인 손상: 과거 기록이 바뀜")
        if c[1].button("첫 기록 몰래 수정 시도"):
            st.session_state["gate_ledger"][0] = {**ledger[0], "reason": "(몰래 바꾼 사유)"}
            st.rerun()
