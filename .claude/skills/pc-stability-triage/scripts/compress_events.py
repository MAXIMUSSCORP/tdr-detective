"""Compress pc-diag ``query_events`` output into a short, evidence-tagged text digest.

Usage::

    python compress_events.py [FILE ...] [--map MAP.json] [--prefix E]
                              [--burst-gap SECONDS] [--max-noise-lines N]
    <json producer> | python compress_events.py

Input (stdin when no FILE is given) is JSON in one of these shapes:

* a list of EventRecord objects (record_id, time, log, provider, event_id, level,
  message, data);
* a ``query_events`` result object with a ``records`` key;
* a saved tool-result wrapper ``[{"type": "text", "text": "<one of the above>"}]``.

Several FILEs are merged and de-duplicated by (log, record_id), so compress every
query of one investigation in a single run to keep evidence ids unique.

Output is plain text, all times UTC. Lines starting with ``#`` are headers. Every
other line starts with an evidence id (``E1``, ``E2``...) that maps to record_ids:

1. ``# timeline``: known signal signatures in chronological order. Repeats of the
   same (log, provider, event_id, message template) at most ``--burst-gap``
   seconds after the previous one collapse into one line with a count and first-last
   time. Only the high-signal data fields of each signature are kept (BugcheckCode
   converted from decimal, LiveKernelEvent P1, WHEA error source, application names).
2. ``# unclassified errors``: critical/error events with no known signature,
   grouped by template and collapsed into bursts the same way.
3. ``# noise``: counts only, per (log, provider, event_id), largest first. Groups
   beyond ``--max-noise-lines`` fold into one final "other noise" line.

Message templates mask GUIDs, hex values and digits so repeats group together.
``--map`` writes ``{"E1": [record_id, ...], ...}`` as JSON. The output is
deterministic: the same records always give the same digest.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

MESSAGE_WIDTH = 100
FIELDS_MESSAGE_WIDTH = 60
NOISE_TEMPLATE_WIDTH = 60
MAX_LISTED_RECORDS = 3

_GUID = re.compile(
    r"\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?"
)
_HEX_PREFIXED = re.compile(r"0[xX][0-9a-fA-F]+")
_HEX_BARE = re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b")
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,7}$")
_BUGCHECK_IN_TEXT = re.compile(r"0x[0-9a-fA-F]{8}")
_BUGCHECK_WAS = re.compile(r"bug ?check was:?\s*(0x[0-9a-fA-F]{1,8})", re.IGNORECASE)

_LEVEL_RANK = {"critical": 0, "error": 1, "warning": 2, "information": 3, "verbose": 4}
_LEVEL_SHORT = {
    "critical": "crit",
    "error": "err",
    "warning": "warn",
    "information": "info",
    "verbose": "verb",
}

# Verified against Microsoft Learn's bug check and kernel live dump code references.
BUGCHECK_NAMES: dict[int, str] = {
    0x0A: "IRQL_NOT_LESS_OR_EQUAL",
    0x1A: "MEMORY_MANAGEMENT",
    0x3B: "SYSTEM_SERVICE_EXCEPTION",
    0x50: "PAGE_FAULT_IN_NONPAGED_AREA",
    0x7E: "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED",
    0x9F: "DRIVER_POWER_STATE_FAILURE",
    0xA0: "INTERNAL_POWER_ERROR",
    0xD1: "DRIVER_IRQL_NOT_LESS_OR_EQUAL",
    0xEF: "CRITICAL_PROCESS_DIED",
    0x101: "CLOCK_WATCHDOG_TIMEOUT",
    0x10E: "VIDEO_MEMORY_MANAGEMENT_INTERNAL",
    0x116: "VIDEO_TDR_FAILURE",
    0x117: "VIDEO_TDR_TIMEOUT_DETECTED",
    0x119: "VIDEO_SCHEDULER_INTERNAL_ERROR",
    0x124: "WHEA_UNCORRECTABLE_ERROR",
    0x133: "DPC_WATCHDOG_VIOLATION",
    0x139: "KERNEL_SECURITY_CHECK_FAILURE",
    0x141: "VIDEO_ENGINE_TIMEOUT_DETECTED",
    0x142: "VIDEO_TDR_APPLICATION_BLOCKED",
    0x1A1: "WIN32K_CALLOUT_WATCHDOG_LIVEDUMP",
    0x1B0: "VIDEO_MINIPORT_FAILED_LIVEDUMP",
}


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Event:
    record_id: int
    time: datetime
    log: str
    provider: str
    event_id: int
    level: str
    message: str
    data: Mapping[str, str]

    @property
    def provider_key(self) -> str:
        return self.provider.strip().lower()


def _parse_time(value: object, index: int) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"record {index}: 'time' must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"record {index}: bad time {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_event(raw: object, index: int) -> Event:
    if not isinstance(raw, Mapping):
        raise ValueError(f"record {index}: expected an object, got {type(raw).__name__}")
    for key in ("record_id", "time", "provider", "event_id"):
        if key not in raw:
            raise ValueError(f"record {index}: missing {key!r}")
    record_id, event_id = raw["record_id"], raw["event_id"]
    if isinstance(record_id, bool) or not isinstance(record_id, int):
        raise ValueError(f"record {index}: 'record_id' must be an integer")
    if isinstance(event_id, bool) or not isinstance(event_id, int):
        raise ValueError(f"record {index}: 'event_id' must be an integer")
    data = raw.get("data") or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"record {index}: 'data' must be an object")
    return Event(
        record_id=record_id,
        time=_parse_time(raw["time"], index),
        log=str(raw.get("log") or ""),
        provider=str(raw["provider"]),
        event_id=event_id,
        level=str(raw.get("level") or "information").lower(),
        message=_SPACE.sub(" ", str(raw.get("message") or "")).strip(),
        data={str(k): "" if v is None else str(v) for k, v in data.items()},
    )


def extract_records(payload: object) -> tuple[list[object], bool]:
    """Return (raw records, truncated flag) from any accepted input shape."""
    if (
        isinstance(payload, list)
        and payload
        and all(
            isinstance(item, Mapping) and item.get("type") == "text" and "text" in item
            for item in payload
        )
    ):
        records: list[object] = []
        truncated = False
        for item in payload:
            inner_records, inner_truncated = extract_records(json.loads(str(item["text"])))
            records.extend(inner_records)
            truncated = truncated or inner_truncated
        return records, truncated
    if isinstance(payload, list):
        return list(payload), False
    if isinstance(payload, Mapping) and isinstance(payload.get("records"), list):
        return list(payload["records"]), bool(payload.get("truncated"))
    raise ValueError("input must be a list of events or an object with a 'records' list")


def load_events(raw_records: Iterable[object]) -> list[Event]:
    """Validate, de-duplicate by (log, record_id) and sort chronologically."""
    seen: set[tuple[str, int]] = set()
    events: list[Event] = []
    for index, raw in enumerate(raw_records):
        event = _to_event(raw, index)
        key = (event.log, event.record_id)
        if key not in seen:
            seen.add(key)
            events.append(event)
    events.sort(key=lambda e: (e.time, e.log, e.record_id, e.provider_key, e.event_id))
    return events


def template(message: str) -> str:
    """Mask GUIDs, hex values and digits so repeated messages share one template."""
    text = _GUID.sub("<guid>", _SPACE.sub(" ", message).strip())
    text = _HEX_PREFIXED.sub("<hex>", text)
    text = _HEX_BARE.sub("<hex>", text)
    return _DIGITS.sub("#", text)


# --------------------------------------------------------------------------- #
# Signatures
# --------------------------------------------------------------------------- #


def _lookup(data: Mapping[str, str], *names: str) -> str | None:
    lowered = {k.lower(): v for k, v in data.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _from_message(event: Event, label: str) -> str | None:
    match = re.search(rf"\b{re.escape(label)}:\s*([^\s,;]+)", event.message)
    return match.group(1) if match else None


def code_name(code: int) -> str:
    name = BUGCHECK_NAMES.get(code)
    return f"0x{code:X} {name}" if name else f"0x{code:X}"


def _wer_event_name(event: Event) -> str:
    return (_lookup(event.data, "EventName") or _from_message(event, "Event Name") or "").strip()


def _wer_param(event: Event, number: int) -> str | None:
    return _lookup(event.data, f"P{number}") or _from_message(event, f"P{number}")


Fields = list[tuple[str, str]]


def _keep(*names: str) -> Callable[[Event], Fields]:
    def extract(event: Event) -> Fields:
        lowered = {k.lower(): (k, v) for k, v in event.data.items()}
        found = [lowered[n.lower()] for n in names if lowered.get(n.lower(), ("", ""))[1]]
        return list(found)

    return extract


def _kernel_power_fields(event: Event) -> Fields:
    fields: Fields = []
    raw = _lookup(event.data, "BugcheckCode")
    if raw is not None:
        try:
            code = int(raw, 0) if raw.lower().startswith("0x") else int(raw)
        except ValueError:
            fields.append(("BugcheckCode", raw))
        else:
            fields.append(("BugcheckCode", raw if code == 0 else f"{raw}({code_name(code)})"))
    for name in ("BugcheckParameter1", "PowerButtonTimestamp", "SleepInProgress"):
        value = _lookup(event.data, name)
        if value is not None:
            fields.append((name, value))
    return fields


def _system_bugcheck_fields(event: Event) -> Fields:
    explicit = _BUGCHECK_WAS.search(event.message)
    if explicit:
        return [("bugcheck", code_name(int(explicit.group(1), 16)))]
    first_param = _lookup(event.data, "param1") or ""
    match = _BUGCHECK_IN_TEXT.search(first_param) or _BUGCHECK_IN_TEXT.search(event.message)
    if not match:
        return []
    return [("bugcheck", code_name(int(match.group(0), 16)))]


def _live_kernel_fields(event: Event) -> Fields:
    fields: Fields = []
    p1 = _wer_param(event, 1)
    if p1:
        try:
            fields.append(("P1", f"{p1}({code_name(int(p1, 16))})"))
        except ValueError:
            fields.append(("P1", p1))
    return fields


def _wer_fields(*params: int) -> Callable[[Event], Fields]:
    def extract(event: Event) -> Fields:
        fields: Fields = [("EventName", _wer_event_name(event))]
        for number in params:
            value = _wer_param(event, number)
            if value:
                fields.append((f"P{number}", value))
        return fields

    return extract


def _wer_named(*names: str) -> Callable[[Event], bool]:
    wanted = {n.lower() for n in names}
    return lambda event: _wer_event_name(event).lower() in wanted


def _provider_prefix(prefix: str) -> Callable[[Event], bool]:
    return lambda event: event.provider_key.startswith(prefix)


@dataclass(frozen=True)
class Signature:
    tag: str
    providers: frozenset[str]
    event_ids: frozenset[int] | None = None
    fields: Callable[[Event], Fields] = field(default=lambda _event: [])
    when: Callable[[Event], bool] | None = None

    def matches(self, event: Event) -> bool:
        if self.providers and event.provider_key not in self.providers:
            return False
        if self.event_ids is not None and event.event_id not in self.event_ids:
            return False
        return self.when is None or self.when(event)


def _sig(
    tag: str,
    providers: Iterable[str],
    event_ids: Iterable[int] | None = None,
    fields: Callable[[Event], Fields] | None = None,
    when: Callable[[Event], bool] | None = None,
) -> Signature:
    return Signature(
        tag=tag,
        providers=frozenset(p.lower() for p in providers),
        event_ids=None if event_ids is None else frozenset(event_ids),
        fields=fields or (lambda _event: []),
        when=when,
    )


WER_APP = ["Windows Error Reporting"]
GPU_DRIVERS = ["nvlddmkm", "amdkmdag", "amdkmdap", "atikmdag", "atikmpag"]

SIGNATURES: tuple[Signature, ...] = (
    _sig("tdr-recovered", ["Display"], [4101]),
    _sig("gpu-driver", GPU_DRIVERS),
    _sig("gpu-driver", [], when=_provider_prefix("igfx")),
    _sig("unexpected-reboot", ["Microsoft-Windows-Kernel-Power"], [41], _kernel_power_fields),
    _sig(
        "bugcheck",
        ["Microsoft-Windows-WER-SystemErrorReporting", "BugCheck"],
        [1001],
        _system_bugcheck_fields,
    ),
    _sig("live-dump", WER_APP, [1001], _live_kernel_fields, _wer_named("LiveKernelEvent")),
    _sig("bugcheck-report", WER_APP, [1001], _wer_fields(1, 2), _wer_named("BlueScreen")),
    _sig(
        "app-hang",
        WER_APP,
        [1001],
        _wer_fields(1),
        _wer_named("AppHangB1", "AppHangTransient", "AppHangXProcB1", "MoAppHang"),
    ),
    _sig(
        "app-crash",
        WER_APP,
        [1001],
        _wer_fields(1, 4),
        _wer_named("APPCRASH", "MoAppCrash", "BEX", "BEX64"),
    ),
    _sig("app-crash", ["Application Error"], [1000], _keep("AppName", "ModuleName", "param1")),
    _sig("app-hang", ["Application Hang"], [1002], _keep("AppName", "param1")),
    _sig("dirty-shutdown", ["EventLog"], [6008]),
    _sig("dump-failed", ["volmgr"], [46, 161]),
    _sig(
        "whea",
        ["Microsoft-Windows-WHEA-Logger"],
        None,
        _keep("ErrorSource", "ErrorType", "ApicId", "MCABank", "PhysicalAddress"),
    ),
    _sig("storage", ["disk"], [7, 11, 51, 153]),
    _sig("storage", ["stornvme", "storahci", "iaStorAC", "iaStorA", "nvme"], [11, 129]),
    _sig("storage", ["Ntfs", "Microsoft-Windows-Ntfs"], [55, 98, 140]),
    _sig("mem-low", ["Microsoft-Windows-Resource-Exhaustion-Detector"], [2004]),
    _sig("cpu-throttle", ["Microsoft-Windows-Kernel-Processor-Power"], [37]),
    _sig("driver-install", ["Microsoft-Windows-UserPnp"], [20001, 20003]),
    _sig("update-installed", ["Microsoft-Windows-WindowsUpdateClient"], [19], _keep("updateTitle")),
    _sig("shutdown-planned", ["User32"], [1074]),
    _sig("shutdown-clean", ["EventLog"], [6006]),
    _sig("shutdown-clean", ["Microsoft-Windows-Kernel-General"], [13]),
    _sig("boot", ["EventLog"], [6005]),
    _sig("boot", ["Microsoft-Windows-Kernel-General"], [12]),
)

KNOWN_NOISE: tuple[Signature, ...] = (
    _sig("known-noise", ["Microsoft-Windows-DistributedCOM", "DCOM"], [10016]),
    _sig("known-noise", [], [4123, 4127], when=lambda e: "display" in e.provider_key),
    _sig("known-noise", ["Service Control Manager"], [7036, 7040, 7042]),
)


def classify(event: Event) -> tuple[str, Signature | None]:
    """Return ("signal" | "noise" | "unclassified", matching signature)."""
    for signature in SIGNATURES:
        if signature.matches(event):
            return "signal", signature
    for signature in KNOWN_NOISE:
        if signature.matches(event):
            return "noise", signature
    if _LEVEL_RANK.get(event.level, 9) <= _LEVEL_RANK["error"]:
        return "unclassified", None
    return "noise", None


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #


@dataclass
class Digest:
    text: str
    evidence: dict[str, list[int]]
    timeline_ids: list[str]
    unclassified_ids: list[str]
    noise_ids: list[str]


@dataclass
class _Group:
    events: list[Event]
    signature: Signature | None = None

    @property
    def first(self) -> Event:
        return self.events[0]

    @property
    def last(self) -> Event:
        return self.events[-1]

    def level(self) -> str:
        return min((e.level for e in self.events), key=lambda lv: _LEVEL_RANK.get(lv, 9))


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


def _records(events: Sequence[Event]) -> str:
    ids = [str(e.record_id) for e in events]
    if len(ids) <= MAX_LISTED_RECORDS:
        return "rec=" + ",".join(ids)
    shown = ",".join(ids[: MAX_LISTED_RECORDS - 1])
    return f"rec={shown},+{len(ids) - MAX_LISTED_RECORDS + 1}"


def _merged_fields(group: _Group) -> str:
    if group.signature is None:
        return ""
    values: dict[str, list[str]] = {}
    for event in group.events:
        for name, value in group.signature.fields(event):
            bucket = values.setdefault(name, [])
            if value not in bucket:
                bucket.append(value)
    parts = []
    for name, seen in values.items():
        shown = "|".join(seen[:3]) + ("|..." if len(seen) > 3 else "")
        parts.append(f"{name}={shown}")
    return (" " + " ".join(parts)) if parts else ""


def _bursts(events: Sequence[Event], gap_seconds: float) -> list[list[Event]]:
    bursts: list[list[Event]] = []
    for event in events:
        if bursts and (event.time - bursts[-1][-1].time).total_seconds() <= gap_seconds:
            bursts[-1].append(event)
        else:
            bursts.append([event])
    return bursts


def compress(
    events: Sequence[Event],
    *,
    burst_gap: float = 60.0,
    prefix: str = "E",
    max_noise_lines: int = 25,
    truncated: bool = False,
) -> Digest:
    """Build the digest for chronologically sorted events (see ``load_events``)."""
    if burst_gap <= 0:
        raise ValueError("burst_gap must be positive")
    if max_noise_lines < 1:
        raise ValueError("max_noise_lines must be at least 1")
    if not _PREFIX.match(prefix):
        raise ValueError("prefix must be a letter followed by up to 7 letters, digits or _")

    signal: dict[tuple[str, str, int, str, str], list[Event]] = {}
    unclassified: dict[tuple[str, str, int, str], list[Event]] = {}
    noise: dict[tuple[str, str, int], list[Event]] = {}
    noise_known: set[tuple[str, str, int]] = set()
    signature_of: dict[tuple[str, str, int, str, str], Signature] = {}

    for event in events:
        section, signature = classify(event)
        tmpl = template(event.message)
        if section == "signal" and signature is not None:
            skey = (event.log, event.provider, event.event_id, tmpl, signature.tag)
            signal.setdefault(skey, []).append(event)
            signature_of.setdefault(skey, signature)
        elif section == "unclassified":
            unclassified.setdefault((event.log, event.provider, event.event_id, tmpl), []).append(
                event
            )
        else:
            nkey = (event.log, event.provider, event.event_id)
            noise.setdefault(nkey, []).append(event)
            if signature is not None:
                noise_known.add(nkey)

    timeline = [
        _Group(burst, signature_of[key])
        for key, grouped in signal.items()
        for burst in _bursts(grouped, burst_gap)
    ]
    timeline.sort(
        key=lambda g: (
            g.first.time,
            g.first.log,
            g.first.provider_key,
            g.first.event_id,
            g.first.record_id,
        )
    )
    unclassified_groups = sorted(
        (
            _Group(burst)
            for grouped in unclassified.values()
            for burst in _bursts(grouped, burst_gap)
        ),
        key=lambda g: (
            g.first.time,
            g.first.log,
            g.first.provider_key,
            g.first.event_id,
            g.first.record_id,
        ),
    )
    noise_items = sorted(
        noise.items(), key=lambda item: (-len(item[1]), item[0][0], item[0][1].lower(), item[0][2])
    )

    single_day = len({e.time.date() for e in events}) <= 1
    time_format = "%H:%M:%S" if single_day else "%m-%d %H:%M:%S"

    def when(group: _Group) -> str:
        start = group.first.time.strftime(time_format)
        if len(group.events) == 1:
            return start
        return f"{start}-{group.last.time.strftime(time_format)} x{len(group.events)}"

    evidence: dict[str, list[int]] = {}
    counter = 0

    def next_id(members: Sequence[Event]) -> str:
        nonlocal counter
        counter += 1
        eid = f"{prefix}{counter}"
        evidence[eid] = [e.record_id for e in members]
        return eid

    lines: list[str] = []
    if events:
        span = f"{events[0].time:%Y-%m-%d %H:%M:%S}..{events[-1].time:%Y-%m-%d %H:%M:%S} UTC"
    else:
        span = "no records"
    lines.append(f"# pc-diag event digest | {len(events)} records | {span}")
    if truncated:
        lines.append("# WARNING source result was truncated: narrow the window or filters")

    timeline_ids: list[str] = []
    lines.append(f"# timeline ({len(timeline)} lines, chronological)")
    for group in timeline:
        eid = next_id(group.events)
        timeline_ids.append(eid)
        tag = group.signature.tag if group.signature else "signal"
        fields = _merged_fields(group)
        message = _clip(group.first.message, FIELDS_MESSAGE_WIDTH if fields else MESSAGE_WIDTH)
        lines.append(
            f"{eid} {when(group)} {group.first.log} {group.first.provider}/{group.first.event_id} "
            f"{_LEVEL_SHORT.get(group.level(), group.level())} [{tag}]{fields} "
            f'"{message}" {_records(group.events)}'
        )

    unclassified_ids: list[str] = []
    lines.append(f"# unclassified errors ({len(unclassified_groups)} lines)")
    for group in unclassified_groups:
        eid = next_id(group.events)
        unclassified_ids.append(eid)
        message = _clip(group.first.message, MESSAGE_WIDTH)
        lines.append(
            f"{eid} {when(group)} {group.first.log} {group.first.provider}/{group.first.event_id} "
            f'{_LEVEL_SHORT.get(group.level(), group.level())} "{message}" {_records(group.events)}'
        )

    noise_ids: list[str] = []
    noise_total = sum(len(grouped) for _, grouped in noise_items)
    lines.append(f"# noise ({noise_total} events, counts only)")
    shown_items, rest = noise_items[:max_noise_lines], noise_items[max_noise_lines:]
    for (log, provider, event_id), grouped in shown_items:
        eid = next_id(grouped)
        noise_ids.append(eid)
        templates = Counter(template(e.message) for e in grouped)
        top, _count = min(templates.items(), key=lambda item: (-item[1], item[0]))
        known = " [known-noise]" if (log, provider, event_id) in noise_known else ""
        variants = f" +{len(templates) - 1} variants" if len(templates) > 1 else ""
        lines.append(
            f"{eid} x{len(grouped)} {log} {provider}/{event_id}{known} "
            f'"{_clip(top, NOISE_TEMPLATE_WIDTH)}"{variants}'
        )
    if rest:
        members = [e for _, grouped in rest for e in grouped]
        eid = next_id(members)
        noise_ids.append(eid)
        lines.append(f"{eid} x{len(members)} other noise in {len(rest)} provider/event groups")

    return Digest(
        text="\n".join(lines) + "\n",
        evidence=evidence,
        timeline_ids=timeline_ids,
        unclassified_ids=unclassified_ids,
        noise_ids=noise_ids,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _read_inputs(paths: Sequence[str]) -> tuple[list[object], bool]:
    sources = (
        [(p, Path(p).read_text(encoding="utf-8-sig")) for p in paths]
        if paths
        else [("<stdin>", sys.stdin.buffer.read().decode("utf-8-sig"))]
    )
    records: list[object] = []
    truncated = False
    for name, text in sources:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name}: invalid JSON ({exc.msg} at line {exc.lineno})") from exc
        try:
            found, was_truncated = extract_records(payload)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
        records.extend(found)
        truncated = truncated or was_truncated
    return records, truncated


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compress_events.py",
        description="Compress pc-diag query_events JSON into an evidence-tagged digest.",
    )
    parser.add_argument("files", nargs="*", help="JSON files (default: stdin)")
    parser.add_argument("--map", dest="map_path", help="write evidence id -> record_ids JSON here")
    parser.add_argument("--prefix", default="E", help="evidence id prefix (default: E)")
    parser.add_argument(
        "--burst-gap", type=float, default=60.0, help="seconds that still join a burst (60)"
    )
    parser.add_argument(
        "--max-noise-lines", type=int, default=25, help="noise groups listed before 'other' (25)"
    )
    args = parser.parse_args(argv)

    try:
        raw_records, truncated = _read_inputs(args.files)
        digest = compress(
            load_events(raw_records),
            burst_gap=args.burst_gap,
            prefix=args.prefix,
            max_noise_lines=args.max_noise_lines,
            truncated=truncated,
        )
    except (OSError, ValueError) as exc:
        print(f"compress_events: error: {exc}", file=sys.stderr)
        return 2

    if args.map_path:
        Path(args.map_path).write_text(
            json.dumps(digest.evidence, indent=None, sort_keys=False) + "\n", encoding="utf-8"
        )
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.write(digest.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
