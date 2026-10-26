"""Streamlit Community Cloud: live deterministic verifier for KIC 13F holdings (no LLM)."""
import datetime as dt
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from demo_core import HOLDINGS, HYPOTHESES, summary_markdown, verify
from watch_view import render as render_watch

st.set_page_config(page_title="가설 레드팀 · 투자 전제 감시", layout="wide")
st.markdown("## 가설 레드팀 · 투자 전제 감시 Agent")
tab_watch, tab_live = st.tabs(["① 전제 감시 타임라인 (21개월 재생)", "② 지금 검산해 보기 (SEC 실시간)"])
with tab_watch:
    render_watch()
with tab_live:
    st.markdown(
        "KIC가 공개한 13F(2026년 6월 말) 보유 미국 기업 중 하나를 고르거나 아무 미국 상장사 코드를 넣으면, "
        "SEC EDGAR 원공시를 지금 받아 와서 가설의 사실을 코드로 검산합니다. 숫자마다 원문 링크와 원문 검색어가 붙습니다. "
        "LLM 단계(Planner·레드팀·Reporter)가 포함된 전체 판정서는 "
        "[공개 갤러리](https://claude.ai/artifact/LSKmeqeTgJLGdXuNeCE7eC)에 있습니다."
    )

    choices = [f"{r['ticker']} · {r['name']} · {float(r['weight_pct']):.2f}%" for r in HOLDINGS]
    col1, col2 = st.columns(2)
    choice = col1.selectbox("KIC 13F 보유 기업 (평가액 비중 순)", choices)
    free = col2.text_input("또는 종목 코드 직접 입력", placeholder="예: GOOG, URI, KO")
    col3, col4 = st.columns(2)
    label = col3.radio("가설", [v["label"] for v in HYPOTHESES.values()])
    asof = col4.text_input("기준일 (그날까지 공개된 공시만 사용)", dt.date.today().isoformat())

    if st.button("검산 실행", type="primary"):
        ticker = free.strip() or choice.split(" · ")[0]
        template = next(k for k, v in HYPOTHESES.items() if v["label"] == label)
        with st.spinner("SEC 원공시를 받아 검산하는 중..."):
            try:
                result = verify(ticker, template, asof)
            except Exception as exc:  # show the stop reason instead of a stack trace
                st.error(f"멈춤: {exc}")
                st.stop()
        st.markdown(summary_markdown(result))
        with st.expander("판정서 전체 (원공시 입력·계산·감사로그)"):
            components.html(Path(result["html_path"]).read_text(encoding="utf-8"), height=900, scrolling=True)
