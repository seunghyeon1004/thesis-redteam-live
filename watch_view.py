"""Thesis-watch timeline view: precomputed point-in-time replay (2025-01-02 memo → every later 10-Q/10-K), no LLM."""
import json
from pathlib import Path

import streamlit as st

DATA = json.loads((Path(__file__).parent / "watch_replay.json").read_text(encoding="utf-8"))
PREMISES = {"F1": "자사주를 산다 (최근 12개월 매입 > 0)", "F2": "순환원이 양수다 (매입 > 보통주 발행)",
            "F3": "주식수가 줄었다 (연말 기준, 전년 대비)"}
PILL = {"TRUE": ("유지", "#1f7a4d", "#e3f4ea"), "FALSE": ("깨짐", "#b42318", "#fde8e6"),
        "판정불가": ("판정불가", "#6b7280", "#eef0f3"), "대기": ("대기", "#6b7280", "#eef0f3")}


def pill(status):
    text, fg, bg = PILL.get(status, (status, "#6b7280", "#eef0f3"))
    return f'<span style="background:{bg};color:{fg};padding:2px 9px;border-radius:10px;font-size:0.85em;font-weight:600">{text}</span>'


def filing_url(cik, acc):
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"


def render():
    n_alert = sum(1 for d in DATA for e in d["ev"] if e["flips"])
    n_file = sum(len(d["ev"]) for d in DATA)
    st.markdown(
        "2025년 1월 2일에 보유 기업마다 **“자사주로 주주에게 현금을 돌려준다”** 메모를 썼다고 가정하고, 이후 접수된 모든 10-Q·10-K를 "
        "**접수 다음 날 그 시점에 공개된 정보만으로** 다시 검산한 기록입니다. 전제의 참·거짓이 바뀐 공시에만 알림이 나갑니다 "
        "(심각도는 코드 규칙, LLM 미사용).")
    c = st.columns(3)
    c[0].metric("KIC 13F 보유 미국 기업", f"{len(DATA)}곳")
    c[1].metric("다시 검산한 정기공시", f"{n_file:,}건")
    c[2].metric("알림이 나간 공시", f"{n_alert}건 ({n_alert / n_file * 100:.1f}%)")
    only = st.toggle("전제가 깨진 적 있는 기업만 보기", value=True)
    rows = [d for d in DATA if not only or any("FALSE" in f[1] for e in d["ev"] for f in e["flips"].values())]
    labels = [f"{d['t']} · {d['n']} · {d['w']:.2f}%" for d in rows]
    default = next((i for i, d in enumerate(rows) if d["t"] == "GOOG"), 0)
    d = rows[st.selectbox("보유 기업 (평가액 비중 순)", range(len(rows)), index=default, format_func=lambda i: labels[i])]
    head = "".join(f"<th style='text-align:left;padding:6px 8px'>{PREMISES[k]}</th>" for k in PREMISES)
    body = (f"<tr style='background:#f6f7f9'><td style='padding:6px 8px'><b>메모 작성</b><br><small>2025-01-02</small></td><td></td>"
            + "".join(f"<td style='padding:6px 8px'>{pill(d['base'].get(k, ''))}</td>" for k in PREMISES) + "</tr>")
    for e in d["ev"]:
        alert = e["flips"]
        broke = any(v[1] == "FALSE" for v in alert.values())
        tag = ("<br><span style='color:#b42318;font-weight:700'>⚠ 알림 · 전제 깨짐</span>" if broke
               else "<br><span style='color:#1f7a4d;font-weight:700'>알림 · 전제 회복</span>" if alert else "")
        cells = "".join(
            f"<td style='padding:6px 8px;{'outline:2px solid #b42318;' if k in alert else ''}'>{pill(e['st'].get(k, ''))}</td>" for k in PREMISES)
        body += (f"<tr style='border-top:1px solid #e5e7eb'><td style='padding:6px 8px'><a href='{filing_url(d['cik'], e['acc'])}' target='_blank'>"
                 f"{e['form']}</a><br><small>접수 {e['filed']}</small></td><td style='padding:6px 8px'><small>검산 {e['asof']}</small>{tag}</td>{cells}</tr>")
    st.markdown(f"<div style='overflow-x:auto'><table style='border-collapse:collapse;width:100%;font-size:0.92em'>"
                f"<tr style='border-bottom:2px solid #1b2433'><th style='text-align:left;padding:6px 8px'>공시</th><th></th>{head}</tr>{body}</table></div>",
                unsafe_allow_html=True)
    alerts = [e for e in d["ev"] if e["flips"]]
    if alerts:
        st.markdown("#### 알림 내용 (원공시 계산 근거)")
    for e in alerts:
        with st.expander(f"{e['filed']} {e['form']} — " + ", ".join(
                f"{k} {PILL[a][0]}→{PILL[b][0]}" for k, (a, b) in e["flips"].items()), expanded=True):
            for k, (a, b) in e["flips"].items():
                st.markdown(f"**{PREMISES[k]}**: {pill(a)} → {pill(b)}<br><small>{e['why'][k]}</small>", unsafe_allow_html=True)
            st.markdown(f"[SEC 원공시 열기]({filing_url(d['cik'], e['acc'])})")
    st.caption("판정불가 = 원공시만으로 확정할 수 없음(표시 방식 변경 등). 판정불가는 알림을 내지 않고 직전 확정 상태를 유지합니다. "
               "이 화면은 사전 계산된 재생 결과이며, 2026-10-26 이후 접수 공시는 봉인된 코드로 별도 채점합니다.")
