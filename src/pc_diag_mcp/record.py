"""Capture live telemetry into a replay directory, and scrub captures before sharing.

python -m pc_diag_mcp.record capture NAME [--hours 24 | --start ISO --end ISO] [--dxdiag]
python -m pc_diag_mcp.record scrub SRC DST [--literal TEXT ...]

``capture`` writes to ``fixtures/raw/NAME`` (git-ignored). ``scrub`` copies a directory,
replacing host identifiers with stable placeholders, and fails closed: if any pattern still
matches afterwards, nothing is written and the exit code is non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pc_diag_mcp.collectors.base import Collector, CollectorUnavailable
from pc_diag_mcp.collectors.powershell import PowerShellCollector
from pc_diag_mcp.models import EventQuery, ReplayScenario, TimeWindow
from pc_diag_mcp.tools.params import DEVICE_CLASS_ALLOWLIST

RAW_ROOT = Path("fixtures/raw")
IDENTIFIERS_FILE = ".identifiers.json"

# --------------------------------------------------------------------------- #
# Scrubbing
# --------------------------------------------------------------------------- #

_PROFILE = re.compile(
    r"(?i)(?P<prefix>[A-Z]:[\\/]+Users[\\/]+)(?!\[USER\])"
    r"(?!(?:Public|Default|All Users)(?:[\\/]|$))"
    r"(?P<name>[^\\/:*?\"<>|\r\n]+)"
)
_SID = re.compile(r"S-1-5-21(?:-\d+){3,4}")
_MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Device instance IDs end in a per-device suffix that is often a serial number.
_INSTANCE = re.compile(
    r"(?i)\b(?P<prefix>(?:PCI|USB|HID|SWD|ACPI|SCSI|NVME|STORAGE|DISPLAY|BTHENUM|BTH|HDAUDIO|"
    r"MONITOR|USBSTOR|ROOT)\\[^\\\s\"<]+\\)(?!\[INSTANCE-)(?P<id>[^\\\s\"\[<]+)"
)
_SERIAL_KEY = re.compile(r"(?i)serial|machineid|uuid|productid|hardwareid")
_EMPTY_SERIAL = {"", "0", "0x0", "none", "null", "n/a", "unknown", "to be filled by o.e.m."}
_PLACEHOLDER = re.compile(r"^\[[A-Z]+(?:-\d+)?\]$")
# Literals that would mangle ordinary data if replaced everywhere.
_GENERIC_NAMES = {"root", "user", "users", "admin", "administrator", "system", "default", "public"}


@dataclass
class Scrubber:
    """Replaces identifiers with placeholders that are stable within one scrub run."""

    literals: dict[str, str] = field(default_factory=dict)
    _seen: dict[tuple[str, str], str] = field(default_factory=dict)

    def add_literal(self, value: str | None, placeholder: str) -> None:
        if value and len(value.strip()) >= 3 and value.strip().lower() not in _GENERIC_NAMES:
            self.literals.setdefault(value.strip(), placeholder)

    def _stable(self, kind: str, value: str) -> str:
        key = (kind, value.lower())
        if key not in self._seen:
            count = sum(1 for k, _ in self._seen if k == kind) + 1
            self._seen[key] = f"[{kind}-{count}]"
        return self._seen[key]

    def scrub_text(self, text: str) -> str:
        text = _PROFILE.sub(lambda m: m.group("prefix") + "[USER]", text)
        text = _SID.sub(lambda m: self._stable("SID", m.group(0)), text)
        text = _MAC.sub(lambda m: self._stable("MAC", m.group(0)), text)
        text = _EMAIL.sub(lambda m: self._stable("EMAIL", m.group(0)), text)
        text = _INSTANCE.sub(
            lambda m: m.group("prefix") + self._stable("INSTANCE", m.group("id")), text
        )
        # Longest first so "TESTHOST-PC" is replaced before "TESTHOST".
        for literal in sorted(self.literals, key=len, reverse=True):
            text = re.sub(re.escape(literal), self.literals[literal], text, flags=re.IGNORECASE)
        return text

    def scrub_value(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {self.scrub_text(str(k)): self.scrub_value(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub_value(v, key) for v in value]
        if isinstance(value, str):
            if key and _SERIAL_KEY.search(key) and value.strip().lower() not in _EMPTY_SERIAL:
                return self._stable("SERIAL", value)
            return self.scrub_text(value)
        return value

    def findings(self, value: Any, key: str | None = None, where: str = "$") -> list[str]:
        """Every place a pattern still matches; empty means the data is clean."""
        found: list[str] = []
        if isinstance(value, dict):
            for k, v in value.items():
                found += self._text_findings(str(k), f"{where}.<key>")
                found += self.findings(v, str(k), f"{where}.{k}")
            return found
        if isinstance(value, list):
            for i, v in enumerate(value):
                found += self.findings(v, key, f"{where}[{i}]")
            return found
        if isinstance(value, str):
            if (
                key
                and _SERIAL_KEY.search(key)
                and value.strip().lower() not in _EMPTY_SERIAL
                and not _PLACEHOLDER.match(value)
            ):
                found.append(f"{where}: serial-like field {key}")
            found += self._text_findings(value, where)
        return found

    def _text_findings(self, text: str, where: str) -> list[str]:
        found: list[str] = []
        checks: list[tuple[str, re.Pattern[str]]] = [
            ("user-profile path", _PROFILE),
            ("SID", _SID),
            ("MAC address", _MAC),
            ("email address", _EMAIL),
            ("device instance id", _INSTANCE),
        ]
        for label, pattern in checks:
            if pattern.search(text):
                found.append(f"{where}: {label}")
        lowered = text.lower()
        for literal in self.literals:
            if literal.lower() in lowered:
                found.append(f"{where}: host literal")
        return found


class ScrubError(RuntimeError):
    pass


def default_literals() -> dict[str, str]:
    """Identifiers of the machine running the scrubber."""
    found: dict[str, str] = {}
    for var, placeholder in (
        ("COMPUTERNAME", "[COMPUTERNAME]"),
        ("USERNAME", "[USERNAME]"),
        ("USER", "[USERNAME]"),
    ):
        value = os.environ.get(var)
        if value:
            found[value] = placeholder
    found.setdefault(socket.gethostname(), "[COMPUTERNAME]")
    return found


def scrub_directory(src: Path, dst: Path, extra_literals: Iterable[str] = ()) -> list[Path]:
    """Copy ``src`` to ``dst`` scrubbed. Raises ``ScrubError`` (writing nothing) on leftovers."""
    if not src.is_dir():
        raise ScrubError(f"source directory not found: {src}")
    if dst.exists() and any(dst.iterdir()):
        raise ScrubError(f"destination is not empty: {dst}")

    scrubber = Scrubber()
    identifiers_file = src / IDENTIFIERS_FILE
    if identifiers_file.is_file():
        identity = json.loads(identifiers_file.read_text(encoding="utf-8"))
        scrubber.add_literal(identity.get("computer_name"), "[COMPUTERNAME]")
        scrubber.add_literal(identity.get("user_name"), "[USERNAME]")
        scrubber.add_literal(identity.get("user_domain"), "[COMPUTERNAME]")
    for literal, placeholder in default_literals().items():
        scrubber.add_literal(literal, placeholder)
    for literal in extra_literals:
        scrubber.add_literal(literal, "[REDACTED]")

    outputs: dict[Path, str] = {}
    problems: list[str] = []
    for file in sorted(p for p in src.rglob("*") if p.is_file()):
        relative = file.relative_to(src)
        if relative.name == IDENTIFIERS_FILE:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ScrubError(f"{relative}: not UTF-8 text; refusing to copy binary data") from exc
        if file.suffix.lower() == ".json":
            document = scrubber.scrub_value(json.loads(text))
            problems += [f"{relative}{p[1:]}" for p in scrubber.findings(document)]
            outputs[relative] = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        else:
            cleaned = scrubber.scrub_text(text)
            problems += [f"{relative}{p[1:]}" for p in scrubber._text_findings(cleaned, "$")]
            outputs[relative] = cleaned
    if problems:
        raise ScrubError("patterns still match after scrubbing:\n  " + "\n  ".join(problems))

    # Stage next to the destination, then move into place, so a failure leaves nothing behind.
    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".scrub-", dir=dst.parent))
    try:
        for relative, content in outputs.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if dst.exists():
            dst.rmdir()
        staging.rename(dst)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return [dst / relative for relative in outputs]


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def capture(
    collector: Collector,
    name: str,
    window: TimeWindow,
    *,
    description: str = "",
    root: Path = RAW_ROOT,
    include_dxdiag: bool = False,
    identity: Callable[[], dict[str, str]] | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Record ``window`` from ``collector`` into ``root/name`` as a replay directory."""
    system = collector.system_info()
    scenario = ReplayScenario(
        id=name,
        description=description or f"Captured {window.start:%Y-%m-%d %H:%M}Z",
        now=window.end,
        system=system,
    )
    target = root / name
    target.mkdir(parents=True, exist_ok=False)
    _write_json(target / "scenario.json", scenario.model_dump(mode="json"))

    events = collector.events(EventQuery(window=window, max_events=5000))
    _write_json(target / "events.json", [e.model_dump(mode="json") for e in events])
    log(f"events.json: {len(events)} events")

    optional: list[tuple[str, Callable[[], Any]]] = [
        ("process_snapshot.json", lambda: collector.process_snapshot(25).model_dump(mode="json")),
        ("sensors.json", lambda: [s.model_dump(mode="json") for s in collector.sensors(None)]),
        (
            "drivers.json",
            lambda: [
                d.model_dump(mode="json") for d in collector.drivers(list(DEVICE_CLASS_ALLOWLIST))
            ],
        ),
    ]
    if include_dxdiag:
        optional.append(("dxdiag.json", lambda: collector.dxdiag().model_dump(mode="json")))
    for filename, produce in optional:
        try:
            _write_json(target / filename, produce())
            log(f"{filename}: ok")
        except CollectorUnavailable as exc:
            log(f"{filename}: skipped ({exc})")

    if identity is not None:
        _write_json(target / IDENTIFIERS_FILE, identity())
    return target


def _parse_time(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} needs a UTC offset, e.g. 2026-10-06T05:00Z")
    return value.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pc_diag_mcp.record",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="record a live window into fixtures/raw/NAME")
    cap.add_argument("name")
    cap.add_argument("--start", type=_parse_time)
    cap.add_argument("--end", type=_parse_time)
    cap.add_argument(
        "--hours", type=float, default=24.0, help="window length when --start is omitted"
    )
    cap.add_argument("--description", default="")
    cap.add_argument("--root", type=Path, default=RAW_ROOT)
    cap.add_argument("--dxdiag", action="store_true", help="also run dxdiag (up to 90 s)")

    scr = sub.add_parser("scrub", help="copy SRC to DST with identifiers replaced")
    scr.add_argument("src", type=Path)
    scr.add_argument("dst", type=Path)
    scr.add_argument("--literal", action="append", default=[], help="extra string to redact")

    args = parser.parse_args(argv)
    if args.command == "scrub":
        try:
            written = scrub_directory(args.src, args.dst, args.literal)
        except ScrubError as exc:
            print(f"scrub failed: {exc}", file=sys.stderr)
            return 2
        print(f"wrote {len(written)} scrubbed files to {args.dst}")
        return 0

    end = args.end or datetime.now(UTC)
    start = args.start or end - timedelta(hours=args.hours)
    collector = PowerShellCollector()
    target = capture(
        collector,
        args.name,
        TimeWindow(start=start, end=end),
        description=args.description,
        root=args.root,
        include_dxdiag=args.dxdiag,
        identity=collector.identity,
    )
    print(f"captured into {target}; run 'scrub' before sharing it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
