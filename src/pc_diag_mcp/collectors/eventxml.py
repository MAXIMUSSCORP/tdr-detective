"""Event Log query building and event XML parsing.

The FilterXml QueryList is built here, in Python, from a validated ``EventQuery`` and handed
to PowerShell as data on stdin. Nothing the model supplies is ever interpreted as PowerShell.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from defusedxml import ElementTree as SafeET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from pydantic import ValidationError

from pc_diag_mcp.collectors.base import CollectorError
from pc_diag_mcp.models import EventLevel, EventQuery, EventRecord

MAX_TEXT = 600
MAX_LABEL = 128
_EVENT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
# Defence in depth: EventQuery already restricts provider names to this alphabet.
_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9 ._-]{1,128}$")
_LEVEL_CODES: dict[EventLevel, tuple[int, ...]] = {
    EventLevel.CRITICAL: (1,),
    EventLevel.ERROR: (2,),
    EventLevel.WARNING: (3,),
    # WER and other classic sources log informational events as LogAlways (0).
    EventLevel.INFORMATION: (0, 4),
    EventLevel.VERBOSE: (5,),
}
_LEVEL_BY_CODE = {code: level for level, codes in _LEVEL_CODES.items() for code in codes}
_WHITESPACE = re.compile(r"\s+")
# C0/C1 controls except tab, plus zero-width and bidi-override characters that can hide text.
_UNSAFE_CHARS = re.compile(
    r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _xpath_time(value: datetime) -> str:
    utc = value.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _any_of(field: str, values: list[str]) -> str:
    return "(" + " or ".join(f"{field}={v}" for v in values) + ")"


def build_filter_xml(query: EventQuery, log: str) -> str:
    """One ``<QueryList>`` for ``log``. Logs are queried separately so ``MaxEvents`` keeps the
    newest events of each log rather than whatever the merged reader yields first."""
    if log not in query.logs:
        raise ValueError(f"log {log!r} was not requested by the query")

    clauses: list[str] = []
    if query.providers:
        for provider in query.providers:
            if not _SAFE_PROVIDER.fullmatch(provider):
                raise ValueError(f"unsafe provider name rejected: {provider!r}")
        names = " or ".join(f"@Name='{p}'" for p in query.providers)
        clauses.append(f"Provider[{names}]")
    if query.event_ids:
        clauses.append(_any_of("EventID", [str(int(i)) for i in query.event_ids]))
    if query.levels:
        codes = [str(c) for level in query.levels for c in _LEVEL_CODES[EventLevel(level)]]
        clauses.append(_any_of("Level", codes))
    start, end = _xpath_time(query.window.start), _xpath_time(query.window.end)
    clauses.append(f"TimeCreated[@SystemTime>='{start}' and @SystemTime<='{end}']")

    query_list = ET.Element("QueryList")
    query_el = ET.SubElement(query_list, "Query", Id="0", Path=log)
    select = ET.SubElement(query_el, "Select", Path=log)
    select.text = f"*[System[{' and '.join(clauses)}]]"
    return ET.tostring(query_list, encoding="unicode")


def truncate(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sanitize_text(text: str, limit: int = MAX_TEXT) -> str:
    """Log text is written by arbitrary local software: drop control/bidi characters."""
    return truncate(_UNSAFE_CHARS.sub(" ", text).strip(), limit)


def sanitize_label(text: str, limit: int = MAX_LABEL) -> str:
    return truncate(_UNSAFE_CHARS.sub("", text).strip(), limit)


def clean_message(message: str | None) -> str:
    if not message:
        return ""
    return truncate(_WHITESPACE.sub(" ", _UNSAFE_CHARS.sub(" ", message)).strip())


def sanitize_record(record: EventRecord) -> EventRecord:
    """Idempotent; applied to every collector's output, not only to freshly parsed XML."""
    data: dict[str, str] = {}
    for key, value in record.data.items():
        _put(data, key, value)
    return record.model_copy(
        update={
            "log": sanitize_label(record.log),
            "provider": sanitize_label(record.provider),
            "message": clean_message(record.message),
            "data": data,
        }
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _put(data: dict[str, str], key: str, value: str) -> None:
    key = sanitize_label(key) or "param"
    name, n = key, 1
    while name in data:
        n += 1
        name = sanitize_label(f"{key}_{n}")
    data[name] = sanitize_text(value)


def _event_data(event: ET.Element) -> dict[str, str]:
    data: dict[str, str] = {}
    event_data = event.find(f"{_EVENT_NS}EventData")
    if event_data is not None:
        position = 0
        for item in event_data:
            if _local(item.tag) != "Data":
                continue  # <Binary> blobs carry no names and can be large
            position += 1
            _put(data, item.get("Name") or f"param{position}", item.text or "")
    user_data = event.find(f"{_EVENT_NS}UserData")
    if user_data is not None:
        for leaf in user_data.iter():
            if leaf is not user_data and len(leaf) == 0:
                _put(data, _local(leaf.tag), leaf.text or "")
    return data


def _parse_time(value: str) -> datetime:
    # Windows writes 7 fractional digits; normalise to microseconds and an explicit offset.
    text = _FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_event(xml: str, message: str | None) -> EventRecord:
    """Normalise one event from ``EventLogRecord.ToXml()`` plus its rendered message."""
    try:
        event = SafeET.fromstring(xml, forbid_dtd=True)
        system = event.find(f"{_EVENT_NS}System")
        if system is None:
            raise ValueError("missing <System>")
        provider = system.find(f"{_EVENT_NS}Provider")
        created = system.find(f"{_EVENT_NS}TimeCreated")
        if provider is None or created is None:
            raise ValueError("missing <Provider> or <TimeCreated>")
        data = _event_data(event)
        text = clean_message(message) or clean_message(" ".join(v for v in data.values() if v))
        return EventRecord(
            record_id=int(system.findtext(f"{_EVENT_NS}EventRecordID") or ""),
            time=_parse_time(created.get("SystemTime") or ""),
            log=sanitize_label(system.findtext(f"{_EVENT_NS}Channel") or ""),
            provider=sanitize_label(provider.get("Name") or ""),
            event_id=int(system.findtext(f"{_EVENT_NS}EventID") or ""),
            level=_LEVEL_BY_CODE.get(
                int(system.findtext(f"{_EVENT_NS}Level") or "4"), EventLevel.INFORMATION
            ),
            message=text,
            data=data,
        )
    except (ET.ParseError, DefusedXmlException, ValueError, ValidationError) as exc:
        raise CollectorError(f"could not parse event XML: {exc}") from exc
