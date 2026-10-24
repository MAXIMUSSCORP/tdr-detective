"""Strip credentials from text before it is printed or written.

Covers credential values found in the environment (and in a Google service-account file),
in raw and JSON-escaped form, plus anything shaped like an Anthropic key.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

SECRET_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
_GOOGLE_CREDENTIAL_FIELDS = ("private_key", "private_key_id", "client_secret", "refresh_token")
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
_MIN_SECRET_LEN = 8
REDACTED = "[redacted]"


def _secret_values() -> list[str]:
    values = [os.environ.get(name, "") for name in SECRET_ENV]
    google = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if google:
        try:
            data = json.loads(Path(google).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            values += [v for k in _GOOGLE_CREDENTIAL_FIELDS if isinstance(v := data.get(k), str)]
    return [v for v in values if len(v) >= _MIN_SECRET_LEN]


def redact(text: str) -> str:
    """Replace credential values (raw and JSON-escaped) and Anthropic-key-shaped strings."""
    for value in _secret_values():
        for form in (value, json.dumps(value)[1:-1]):
            text = text.replace(form, REDACTED)
    return _ANTHROPIC_KEY.sub(REDACTED, text)
