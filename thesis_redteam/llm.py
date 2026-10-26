"""Small, auditable LLM provider boundary used by the M2 agent stages.

The provider deliberately knows nothing about investment calculations.  It
only turns a prompt plus JSON schema into a JSON object and records enough
metadata for the run trace.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import time
from typing import Any, Protocol


class LLMProvider(Protocol):
    def complete_json(self, *, system: str, user: str, schema: dict, model: str) -> dict:
        """Return the provider's structured JSON response."""


def _usage(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        metadata = payload.get("metadata")
        raw = metadata.get("usage") if isinstance(metadata, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    input_tokens = raw.get("input_tokens", raw.get("inputTokens", raw.get("input")))
    output_tokens = raw.get("output_tokens", raw.get("outputTokens", raw.get("output")))
    result: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if key in raw:
            result[key] = raw[key]
    return result


class ClaudeCLIProvider:
    """Invoke Claude CLI with the exact no-tool, strict JSON contract."""

    def __init__(self, *, binary: str | None = None, timeout: float = 120.0) -> None:
        self.binary = binary or os.environ.get("CLAUDE_BIN", "claude")
        self.timeout = timeout
        self.records: list[dict[str, Any]] = []
        self.usage_records = self.records
        self.last_usage: dict[str, Any] = {}
        self.last_duration_ms = 0.0

    def complete_json(self, *, system: str, user: str, schema: dict, model: str) -> dict:
        schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        command = [
            self.binary,
            "-p",
            user,
            "--setting-sources",
            "",
            "--system-prompt",
            system,
            "--json-schema",
            schema_json,
            "--output-format",
            "json",
            "--model",
            model,
            "--tools",
            "",
            "--strict-mcp-config",
        ]
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            payload = json.loads(completed.stdout)
            if not isinstance(payload, dict) or "structured_output" not in payload:
                raise ValueError("Claude output did not contain structured_output")
            structured = payload["structured_output"]
            if isinstance(structured, str):
                structured = json.loads(structured)
            if not isinstance(structured, dict):
                raise ValueError("structured_output is not a JSON object")
            usage = _usage(payload)
            return structured
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            usage = locals().get("usage", {"input_tokens": None, "output_tokens": None})
            self.last_duration_ms = duration_ms
            self.last_usage = {**usage, "duration_ms": duration_ms}
            self.records.append({"usage": dict(self.last_usage), "duration_ms": duration_ms, "model": model})

    @property
    def usage(self) -> list[dict[str, Any]]:
        return self.records


class FakeProvider:
    """Deterministic provider for tests and the offline demo.

    Responses are keyed by stage name (``planner``, ``redteam``, or
    ``reporter``).  The orchestrator calls :meth:`set_stage`; direct callers
    can rely on the schema title or a ``stage:...`` marker in the system text.
    """

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.responses = responses or {}
        self.stage: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.usage_records = self.records
        self.last_usage: dict[str, Any] = {}
        self.last_duration_ms = 0.0

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    @staticmethod
    def _stage_from(schema: dict[str, Any], system: str) -> str:
        match = re.search(r"stage[:=]([a-z_]+)", system, re.IGNORECASE)
        if match:
            return match.group(1).lower()
        title = str(schema.get("title", "")).lower()
        for name in ("planner", "redteam", "reporter"):
            if name in title or name in system.lower():
                return name
        return "default"

    def complete_json(self, *, system: str, user: str, schema: dict, model: str) -> dict:
        stage = self.stage or self._stage_from(schema, system)
        started = time.perf_counter()
        response = self.responses.get(stage, self.responses.get("default", {}))
        if isinstance(response, list):
            response = response.pop(0) if response else {}
        if callable(response):
            response = response(system=system, user=user, schema=schema, model=model)
        if not isinstance(response, dict):
            raise ValueError(f"fake response for {stage} is not an object")
        if "structured_output" in response and isinstance(response["structured_output"], dict):
            response = response["structured_output"]
        result = copy.deepcopy(response)
        duration_ms = (time.perf_counter() - started) * 1000
        usage = {"input_tokens": None, "output_tokens": None, "duration_ms": duration_ms}
        self.last_usage = usage
        self.last_duration_ms = duration_ms
        self.calls.append({"stage": stage, "model": model, "system": system, "user": user})
        self.records.append({"stage": stage, "usage": dict(usage), "duration_ms": duration_ms})
        return result

    @property
    def usage(self) -> list[dict[str, Any]]:
        return self.records


def _codex_schema(schema: dict) -> dict:
    """Codex structured output requires closed objects and required properties."""
    result = copy.deepcopy(schema)

    def close(node: Any) -> None:
        if isinstance(node, dict):
            if node.get('type') == 'object':
                node['additionalProperties'] = False
                node['required'] = list(node.get('properties', {}))
            for child in node.values():
                close(child)
        elif isinstance(node, list):
            for child in node:
                close(child)

    close(result)
    return result


class CodexCLIProvider:
    """Structured Codex exec boundary; stdin is closed even in background runs."""

    def __init__(self, *, binary: str | None = None, timeout: float = 120.0) -> None:
        self.binary = binary or os.environ.get('CODEX_BIN', 'codex')
        self.timeout = timeout
        self.records: list[dict[str, Any]] = []
        self.usage_records = self.records
        self.last_usage: dict[str, Any] = {}
        self.last_duration_ms = 0.0

    def complete_json(self, *, system: str, user: str, schema: dict, model: str) -> dict:
        import tempfile
        from pathlib import Path

        selected_model = 'gpt-6.1-sol' if model in {'sonnet', 'haiku', 'opus', ''} else model
        started = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix='thesis-codex-') as directory:
                schema_path = Path(directory) / 'schema.json'
                output_path = Path(directory) / 'output.json'
                schema_path.write_text(json.dumps(_codex_schema(schema), ensure_ascii=False), encoding='utf-8')
                command = [self.binary, 'exec', '-m', selected_model,
                           '-c', 'model_reasoning_effort=low', '--skip-git-repo-check',
                           '-s', 'read-only', '--output-schema', str(schema_path),
                           '-o', str(output_path), system + '\n\n' + user]
                subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=self.timeout, check=True)
                result = json.loads(output_path.read_text(encoding='utf-8'))
                if not isinstance(result, dict):
                    raise ValueError('Codex output is not a JSON object')
                return result
        finally:
            self.last_duration_ms = (time.perf_counter() - started) * 1000
            self.last_usage = {'input_tokens': None, 'output_tokens': None,
                               'duration_ms': self.last_duration_ms}
            self.records.append({'model': selected_model, 'usage': dict(self.last_usage),
                                 'duration_ms': self.last_duration_ms})

    @property
    def usage(self) -> list[dict[str, Any]]:
        return self.records


def provider_from_env() -> LLMProvider:
    name = os.environ.get('THESIS_LLM_PROVIDER', 'claude').lower()
    if name == 'codex':
        return CodexCLIProvider()
    if name == 'claude':
        return ClaudeCLIProvider()
    raise ValueError(f'unsupported THESIS_LLM_PROVIDER: {name}')
