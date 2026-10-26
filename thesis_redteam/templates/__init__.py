"""Deterministic thesis templates and their public registry."""

from __future__ import annotations

from typing import Any

from . import earnings_quality, shareholder_return


TEMPLATE_REGISTRY: dict[str, dict[str, Any]] = {
    "shareholder_return": {
        "module": shareholder_return,
        "label_ko": "주주환원",
        "claim_ids": ("C1", "C2", "C3", "C4"),
    },
    "earnings_quality": {
        "module": earnings_quality,
        "label_ko": "이익의 현금 뒷받침",
        "claim_ids": ("E1", "E2", "E3", "E4", "E5"),
    },
}


def supported_templates() -> tuple[str, ...]:
    return tuple(TEMPLATE_REGISTRY)


def template_info(name: str) -> dict[str, Any]:
    try:
        return TEMPLATE_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unsupported template: {name}") from exc


def evaluate(name: str, fields: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return template_info(name)["module"].evaluate(fields, **kwargs)

