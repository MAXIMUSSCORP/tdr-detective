"""Parse the agent's final answer into a ``Diagnosis``.

The skill's output contract ends the answer with one fenced ```` ```json ```` block. The
*last* such block wins, so a model that revises itself is scored on its final answer.
Failures are returned as a message, never raised.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from pc_diag_mcp.models import Diagnosis

_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ERROR_LIMIT = 500


def extract_diagnosis(raw: str) -> tuple[Diagnosis | None, str | None]:
    """Parse the last fenced ```json block of ``raw`` as a ``Diagnosis``.

    Returns ``(diagnosis, None)`` on success and ``(None, reason)`` otherwise.
    """
    blocks = _JSON_FENCE.findall(raw)
    if not blocks:
        return None, "no fenced ```json block found"
    try:
        payload = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON in ```json block: {exc}"
    try:
        return Diagnosis.model_validate(payload), None
    except ValidationError as exc:
        detail = str(exc).replace("\n", " ")[:_ERROR_LIMIT]
        return None, f"Diagnosis validation failed: {detail}"
