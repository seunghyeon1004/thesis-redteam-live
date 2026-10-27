"""Live season view: filings accepted after the 2026-10-26 seal, scored by the sealed monitor and checked by hand."""
import json
from pathlib import Path

import streamlit as st

from watch_view import pill

MEMO = {"shareholder_return": "자사주로 주주에게 현금을 돌려준다", "earnings_quality": "이익이 늘고 현금이 뒷받침한다"}


def esc(text):
    return str(text).replace("$", "\\$")


def render():
    data = json.loads((Path(__file__).parent / "live_season.json").read_text(encoding="utf-8"))
    st.markdown(
        f"보유 544곳의 메모 전제와 코드 해시를 **{data['sealed']}** 공개 저장소에 봉인한 뒤, **그 이후 접수된 공시만** 같은 코드로 채점한 실제 기록입니다. "
        "전제 상태가 바뀐 건은 사람이 SEC 원문으로 다시 확인했습니다.")
    c = st.columns(4)
    c[0].metric("채점한 공시", f"{data['filings_scored']}건")
    c[1].metric("전제 상태 변화", f"{data['status_changes']}건")
    c[2].metric("원문상 실제 변화", f"{data['verified_real']}건")
    c[3].metric("확인 대기", f"{data['pending_check']}건")
    st.caption(f"마지막 감시 실행 {data['last_run_utc'][:16].replace('T', ' ')} UTC · 4시간마다 자동 실행")
    for x in sorted(data["catches"], key=lambda c: c["memo"] != "shareholder_return"):
        h = x["human"] or {}
        with st.container(border=True):
            st.markdown(f"#### {x['ticker']} · {x['form']} · 메모 “{MEMO.get(x['memo'], x['memo'])}”")
            st.markdown(f"접수 {x['accepted_utc'][:16].replace('T', ' ')} UTC → 감지 {x['detected_utc'][:16].replace('T', ' ')} UTC")
            for k in x["changed"]:
                st.markdown(f"**{esc(h.get('premise', k))}** &nbsp; {pill(x['old'].get(k, ''))} → {pill(x['new'][k])}", unsafe_allow_html=True)
            if h:
                st.markdown(f"- 숫자: {esc(h['numbers'])}\n- 원문 줄: `{h['source_line']}`\n- 맥락: {esc(h['context'])}\n"
                            f"- 사람 확인: **{h['verdict']}** ({h['checked']}) · [원문 열기]({h['url']})")
            if x["agent_questions"]:
                st.markdown("조사 Agent의 확인 질문" + (" (설명 재생성본)" if x["regenerated"] else ""))
                st.markdown("\n".join(f"- {esc(q)}" for q in x["agent_questions"]))
    with st.expander("공개한 결함"):
        st.markdown("\n".join(f"- {d}" for d in data["defects"]))
