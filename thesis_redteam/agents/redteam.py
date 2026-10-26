"""Red-team stage and deterministic counterexample retrieval."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ..retrieval import retrieve_counterexamples
from ..verifier import REGISTERED_EXTRA_CHECKS
from ._schema import validate


REDTEAM_SCHEMA: dict[str, Any] = {
    "title": "redteam_output",
    "type": "object",
    "properties": {
        "challenges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "counterexample_id": {"type": "string"},
                    "risk_ko": {"type": "string"},
                    "extra_check": {"type": "string", "enum": sorted(REGISTERED_EXTRA_CHECKS)},
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical", "LOW", "MEDIUM", "HIGH"]},
                },
                "required": ["counterexample_id", "risk_ko", "extra_check", "severity"],
            },
        },
        "plan_flaws": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "flaw_ko": {"type": "string"},
                    "counterexample_id": {"type": "string"},
                },
                "required": ["claim_id", "flaw_ko", "counterexample_id"],
            },
        },
    },
    "required": ["challenges", "plan_flaws"],
}
REDTEAM_OUTPUT_SCHEMA = REDTEAM_SCHEMA


AUDIT_CHECKLISTS = {
    "shareholder_return": [
        "C1 gross vs net buyback: 총매입액에서 보통주 발행액을 차감하고 순환원 수익률 분모를 확인했는가",
        "C2 period-end vs weighted shares: 기말 실제 주식수와 가중평균/희석 주식수를 혼동했는가",
        "C3 FCF coverage: CFO-capex가 매입을 충당하는지 확인했는가",
        "missing values as zero: 누락값을 0으로 간주했는가",
        "C4 debt-funded returns: 부채/차입으로 환원했는지, 순부채/자산 증가 검사가 빠졌는가",
        "preferred/non-cash issuance: 우선주 또는 비현금 발행을 보통주 현금 발행과 혼동했는가",
        "fiscal-period mixing: 회계연도, 분기, TTM 또는 비교기간을 혼합했는가",
    ],
    "earnings_quality": [
        "E1 net income growth: 순이익이 전년 동기보다 개선되었는지 확인했는가",
        "E2 CFO support: 동일 기간 CFO/순이익으로 현금 뒷받침을 확인했는가",
        "E4 working-capital release: 운전자본 회수로 인한 CFO 개선인지 분리했는가",
        "E5 one-off gains: 일회성 이익이 이익 개선을 설명하는지 확인했는가",
        "E3 accrual spike: 발생액 급증과 이익 대비 현금흐름을 확인했는가",
        "period mixing: 회계연도, 분기, TTM 또는 비교기간을 혼합했는가",
        "NCI vs attributable: 비지배지분 포함 순이익과 지배주주 귀속이익을 혼동했는가",
    ],
}


def _prompt(*, thesis: str, plan_result: dict[str, Any], retrieved: list[dict[str, Any]], memory: str = "on", template: str = "shareholder_return") -> tuple[str, str]:
    system = (
        "stage:redteam\n"
        "당신은 투자 가설의 반례를 찾는 red-team이다. 제공된 반례만 인용하고, "
        "extra_check는 verifier 등록 enum 중 하나만 선택하라. 계산과 PASS/FAIL은 하지 말라."
    )
    if memory == "off":
        system = (
            "stage:redteam\n"
            "독립 감사: 당신은 투자 가설과 planner의 오류를 독립적으로 검토하는 red-team이다. "
            "planner의 모든 청구항(claims 및 fact_claims)을 하나씩 감사하고, "
            "아래 required_checks를 빠짐없이 점검하라. planner가 생략한 필수 검사도 감사하라. "
            "각 결함은 claim_id와 함께 plan_flaws에, 추가 검사는 challenges에 기록하라. "
            "반례 라이브러리는 제공되지 않는다. 스스로 위험과 계획 결함을 검토하되 "
            "counterexample_id는 빈 문자열로 쓰라. 반례 ID를 만들어내지 말라. "
            "extra_check는 verifier 등록 enum 중 하나만 선택하라. 계산과 PASS/FAIL은 하지 말라."
        )
    payload = {"thesis": thesis, "planner": plan_result, "counterexamples": retrieved}
    if memory == "off":
        payload["required_checks"] = AUDIT_CHECKLISTS[template]
    user = json.dumps(payload, ensure_ascii=False)
    return system, user


def red_team(
    *,
    thesis: str,
    plan_result: dict[str, Any],
    template: str,
    provider: Any,
    model: str = "sonnet",
    top_k: int = 5,
    subject_ticker: str | None = None,
    subject_cik: str | int | None = None,
    memory: str = "two-pass",
) -> dict[str, Any]:
    if memory not in {"two-pass", "on", "off"}:
        raise ValueError(f"unsupported memory mode: {memory}")
    if memory == "two-pass":
        audit = red_team(thesis=thesis, plan_result=plan_result, template=template,
                         provider=provider, model=model, top_k=top_k,
                         subject_ticker=subject_ticker, subject_cik=subject_cik, memory="off")
        return reinforce(thesis=thesis, plan_result=plan_result, audit_result=audit,
                         template=template, provider=provider, model=model, top_k=top_k,
                         subject_ticker=subject_ticker, subject_cik=subject_cik)
    retrieval_metadata: dict[str, Any] = {}
    retrieved = retrieve_counterexamples(
        json.dumps({"thesis": thesis, "plan": plan_result}, ensure_ascii=False),
        template=template,
        k=top_k,
        subject_ticker=subject_ticker,
        subject_cik=subject_cik,
        metadata=retrieval_metadata,
    ) if memory == "on" else []
    system, user = _prompt(thesis=thesis, plan_result=plan_result, retrieved=retrieved, memory=memory, template=template)
    result = provider.complete_json(system=system, user=user, schema=REDTEAM_SCHEMA, model=model)
    errors = validate(result, REDTEAM_SCHEMA)
    if errors:
        raise ValueError("redteam schema error: " + "; ".join(errors))
    normalized = dict(result)
    normalized["retrieved_counterexamples"] = retrieved
    normalized["retrieval"] = {
        "memory": memory,
        "excluded_same_subject": list(retrieval_metadata.get("excluded_same_subject", [])),
    }

    # A model must not resurrect a same-subject or otherwise unavailable
    # counterexample by returning its id from memory.  The prompt contains
    # only ``retrieved`` records, so this filter is an auditable final gate.
    allowed_ids = {str(item.get("id")) for item in retrieved}
    if memory == "off":
        allowed_ids.add("")
    normalized["challenges"] = [
        item for item in normalized.get("challenges", [])
        if str(item.get("counterexample_id")) in allowed_ids
    ]
    normalized["plan_flaws"] = [
        item for item in normalized.get("plan_flaws", [])
        if str(item.get("counterexample_id")) in allowed_ids
    ]

    # This is a guardrail for a common M2 demo plan flaw.  The LLM may explain
    # it, but it cannot silently lose it when the planner explicitly selected
    # weighted-average/diluted shares for a period-end claim.
    weighted = [
        claim for claim in plan_result.get("claims", [])
        if any(term in str(claim).lower() for term in ("weighted", "가중평균", "희석 주식"))
    ]
    period_trap = next(
        (item for item in retrieved if item.get("id") == "trap_AAPL_period_end_shares"),
        None,
    )
    if weighted and period_trap and not any(
        flaw.get("counterexample_id") == period_trap["id"] for flaw in normalized["plan_flaws"]
    ):
        normalized["plan_flaws"].append(
            {
                "claim_id": weighted[0].get("id", "C2"),
                "flaw_ko": "희석/가중평균 주식수가 아니라 기말 실제 outstanding를 사용해야 합니다.",
                "counterexample_id": period_trap["id"],
            }
        )
    return normalized


def merge_findings(audit: dict[str, Any], enrichment: dict[str, Any], allowed_ids: set[str]) -> dict[str, Any]:
    """Keep audit order/content; fill valid citations, then append unique items.

    Exact non-citation fields identify an existing item. Rewording is a new
    item, never a replacement. The first valid citation wins deterministically.
    """
    merged = deepcopy(audit)
    keys = {"plan_flaws": ("claim_id", "flaw_ko"),
            "challenges": ("risk_ko", "extra_check", "severity")}
    for kind, fields in keys.items():
        items = merged.setdefault(kind, [])
        positions = {}
        for item in items:
            positions.setdefault(tuple(item[field] for field in fields), []).append(item)
        for candidate in enrichment.get(kind, []):
            item = {field: candidate[field] for field in fields}
            citation = candidate.get("counterexample_id", "")
            item["counterexample_id"] = citation if citation in allowed_ids else ""
            key = tuple(item[field] for field in fields)
            if key in positions:
                for existing in positions[key]:
                    if not existing.get("counterexample_id") and item["counterexample_id"]:
                        existing["counterexample_id"] = item["counterexample_id"]
            else:
                items.append(item)
                positions[key] = [item]
    return merged


def reinforce(
    *, thesis: str, plan_result: dict[str, Any], audit_result: dict[str, Any],
    template: str, provider: Any, model: str = "sonnet", top_k: int = 5,
    subject_ticker: str | None = None, subject_cik: str | int | None = None,
) -> dict[str, Any]:
    """One enrichment call; retrieval happens only after the independent audit."""
    metadata: dict[str, Any] = {}
    retrieved = retrieve_counterexamples(
        json.dumps({"thesis": thesis, "plan": plan_result}, ensure_ascii=False),
        template=template, k=top_k, subject_ticker=subject_ticker,
        subject_cik=subject_cik, metadata=metadata,
    )
    audit = {kind: deepcopy(audit_result[kind]) for kind in ("plan_flaws", "challenges")}
    system = (
        "stage:redteam\n근거 보강: 독립 감사 결과를 보존하며 반례 근거만 보강하라. "
        "기존 항목에는 counterexample_id 인용만 붙일 수 있다. 기존 항목의 다른 필드는 "
        "그대로 복사하고 절대로 삭제/수정/축소하지 말라. 새로운 plan_flaws/challenges는 추가할 수 있다. "
        "제공된 반례 ID만 인용하고 근거가 없으면 빈 문자열을 쓰라. "
        "extra_check는 verifier 등록 enum만 쓰고 계산과 PASS/FAIL은 하지 말라."
    )
    user = json.dumps({"thesis": thesis, "planner": plan_result, "independent_audit": audit,
                       "counterexamples": retrieved}, ensure_ascii=False)
    enrichment = provider.complete_json(system=system, user=user, schema=REDTEAM_SCHEMA, model=model)
    errors = validate(enrichment, REDTEAM_SCHEMA)
    if errors:
        raise ValueError("redteam schema error: " + "; ".join(errors))
    merged = merge_findings(audit_result, enrichment, {str(item["id"]) for item in retrieved})
    merged["independent_audit"] = audit
    merged["retrieved_counterexamples"] = retrieved
    merged["retrieval"] = {"memory": "two-pass", "excluded_same_subject": list(metadata.get("excluded_same_subject", []))}
    return merged


run = red_team

