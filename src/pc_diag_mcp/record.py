"""Capture live telemetry into a replay directory, and scrub captures before sharing.

python -m pc_diag_mcp.record capture NAME [--hours 24 | --start ISO --end ISO] [--dxdiag]
python -m pc_diag_mcp.record scrub SRC DST [--literal TEXT ...]

``capture`` writes to ``fixtures/raw/NAME`` (git-ignored). ``scrub`` copies a directory,
replacing host identifiers with stable placeholders, and fails closed: if any pattern still
matches afterwards, nothing is written and the exit code is non-zero.

Replaced: host/user names and the profile path from the capture, user-profile paths, SIDs,
MACs, serial-like fields, emails, IPv4/IPv6 addresses, UNC paths, URLs with query strings and
device instance IDs. Known limitation: Wi-Fi SSIDs, other hostnames and personal device names
(e.g. Bluetooth headsets) in free text are not detected. Read the output before sharing it.
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
from pathlib import Path, PureWindowsPath
from typing import Any

from pc_diag_mcp.collectors.base import Collector, CollectorError
from pc_diag_mcp.collectors.powershell import PowerShellCollector
from pc_diag_mcp.models import EventQuery, ReplayScenario, TimeWindow
from pc_diag_mcp.tools.params import DEVICE_CLASS_ALLOWLIST

RAW_ROOT = Path("fixtures/raw")
IDENTIFIERS_FILE = ".identifiers.json"

# --------------------------------------------------------------------------- #
# Scrubbing
# --------------------------------------------------------------------------- #

_PROFILE = re.compile(
    r"(?i)(?P<prefix>(?:[A-Z]:|/mnt/[a-z])[\\/]+Users[\\/]+|/home/)(?!\[USER\])"
    r"(?!(?:Public|Default|All Users)(?:[\\/]|$))"
    r"(?P<name>[^\\/:*?\"<>|\r\n]+)"
)
# Domain/local account SIDs (S-1-5-21-...) and Entra ID account SIDs (S-1-12-1-...).
_SID = re.compile(r"S-1-(?:5-21|12-1)(?:-\d+){3,4}")
_MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Device instance IDs end in a per-device suffix that is often a serial number.
_INSTANCE = re.compile(
    r"(?i)\b(?P<prefix>(?:PCI|USB|HID|SWD|ACPI|SCSI|NVME|STORAGE|DISPLAY|BTHENUM|BTH|HDAUDIO|"
    r"MONITOR|USBSTOR|ROOT)\\[^\\\s\"<]+\\)(?!\[INSTANCE-)(?P<id>[^\\\s\"\[<]+)"
)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
# Octets are 0-255, so driver versions such as 32.0.101.8509 never match.
_IPV4 = re.compile(rf"(?<![\w.])(?:{_OCTET}\.){{3}}{_OCTET}(?!\w|\.\d)")
_IPV4_KEEP = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}  # noqa: S104 - not a bind address
_H16 = r"[0-9A-Fa-f]{1,4}"
# Full 8-group form, or a compressed form that starts with a group (fe80::..., 2001:db8::...);
# times (16:47:42) and C++ scopes (std::vector) do not match.
_IPV6 = re.compile(
    rf"(?<![\w:.])(?:(?:{_H16}:){{7}}{_H16}"
    rf"|{_H16}(?::{_H16}){{0,6}}::(?:{_H16}(?::{_H16}){{0,6}})?)"
    r"(?:%[0-9A-Za-z]+)?(?![\w:])"
)
# \\server\share, but not the \\?\ and \\.\ device-path prefixes.
_UNC = re.compile(r"(?<![\w\\])\\\\(?![?.]\\)[A-Za-z0-9._$-]+\\[^\\\s\"<>|\[]+")
_URL_WITH_QUERY = re.compile(r"\bhttps?://[^\s\"'<>]+\?[^\s\"'<>]*")
# Driver/firmware versions such as 4.92.0.0 are valid IPv4 shapes; fields named *version* are
# never addresses, so the IPv4 rule skips them.
_VERSION_KEY = re.compile(r"(?i)version")
_BARE_MAC = re.compile(r"^[0-9A-Fa-f]{12}$")
_MAC_KEY = re.compile(r"(?i)mac|address")
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

    def add_literal(self, value: str | None, placeholder: str) -> bool:
        """Register a literal; False when it is too short or generic to replace safely."""
        if value and len(value.strip()) >= 3 and value.strip().lower() not in _GENERIC_NAMES:
            self.literals.setdefault(value.strip(), placeholder)
            return True
        return False

    @staticmethod
    def _serial_like(key: str | None, value: Any) -> bool:
        if not key or not _SERIAL_KEY.search(key) or isinstance(value, bool):
            return False
        if isinstance(value, int | float):
            return value != 0
        return isinstance(value, str) and value.strip().lower() not in _EMPTY_SERIAL

    @staticmethod
    def _mac_field(key: str | None, value: Any) -> bool:
        return bool(key and _MAC_KEY.search(key) and isinstance(value, str)) and bool(
            _BARE_MAC.match(value.strip())
        )

    def _stable(self, kind: str, value: str) -> str:
        key = (kind, value.lower())
        if key not in self._seen:
            count = sum(1 for k, _ in self._seen if k == kind) + 1
            self._seen[key] = f"[{kind}-{count}]"
        return self._seen[key]

    def scrub_text(self, text: str, *, version_field: bool = False) -> str:
        # URLs first: their query strings may hold emails or names the later rules would split.
        text = _URL_WITH_QUERY.sub(lambda m: self._stable("URL", m.group(0)), text)
        text = _EMAIL.sub(lambda m: self._stable("EMAIL", m.group(0)), text)
        text = _PROFILE.sub(lambda m: m.group("prefix") + "[USER]", text)
        text = _UNC.sub(lambda m: "\\\\" + self._stable("UNC", m.group(0)), text)
        text = _SID.sub(lambda m: self._stable("SID", m.group(0)), text)
        text = _MAC.sub(lambda m: self._stable("MAC", m.group(0)), text)
        text = _IPV6.sub(lambda m: self._stable("IPV6", m.group(0)), text)
        if not version_field:
            text = _IPV4.sub(
                lambda m: (
                    m.group(0) if m.group(0) in _IPV4_KEEP else self._stable("IPV4", m.group(0))
                ),
                text,
            )
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
        if self._serial_like(key, value):
            return self._stable("SERIAL", str(value))
        if self._mac_field(key, value):
            return self._stable("MAC", str(value).strip())
        if isinstance(value, str):
            return self.scrub_text(value, version_field=bool(key and _VERSION_KEY.search(key)))
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
        if self._serial_like(key, value) and not (
            isinstance(value, str) and _PLACEHOLDER.match(value)
        ):
            found.append(f"{where}: serial-like field {key}")
        if self._mac_field(key, value):
            found.append(f"{where}: MAC address field {key}")
        if isinstance(value, str):
            versions = bool(key and _VERSION_KEY.search(key))
            found += self._text_findings(value, where, version_field=versions)
        return found

    def _text_findings(self, text: str, where: str, *, version_field: bool = False) -> list[str]:
        found: list[str] = []
        # Mirrors scrub_text, so fail-closed covers every rule it applies.
        checks: list[tuple[str, re.Pattern[str]]] = [
            ("URL with query string", _URL_WITH_QUERY),
            ("email address", _EMAIL),
            ("user-profile path", _PROFILE),
            ("UNC path", _UNC),
            ("SID", _SID),
            ("MAC address", _MAC),
            ("IPv6 address", _IPV6),
            ("device instance id", _INSTANCE),
        ]
        for label, pattern in checks:
            if pattern.search(text):
                found.append(f"{where}: {label}")
        if not version_field and any(m.group(0) not in _IPV4_KEEP for m in _IPV4.finditer(text)):
            found.append(f"{where}: IPv4 address")
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


def _require_literal(
    scrubber: Scrubber, field_name: str, value: str | None, placeholder: str
) -> None:
    # A captured identifier we cannot replace safely is a reason to stop, not skip.
    if value and not scrubber.add_literal(value, placeholder):
        raise ScrubError(
            f"captured {field_name} {value!r} is too short or generic to scrub "
            "automatically; review the capture by hand"
        )


def scrub_directory(src: Path, dst: Path, extra_literals: Iterable[str] = ()) -> list[Path]:
    """Copy ``src`` to ``dst`` scrubbed. Raises ``ScrubError`` (writing nothing) on leftovers."""
    if not src.is_dir():
        raise ScrubError(f"source directory not found: {src}")
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        raise ScrubError(f"destination is not empty: {dst}")

    scrubber = Scrubber()
    identifiers_file = src / IDENTIFIERS_FILE
    if identifiers_file.is_file():
        identity = json.loads(identifiers_file.read_text(encoding="utf-8"))
        for field_name, placeholder in (
            ("computer_name", "[COMPUTERNAME]"),
            ("user_name", "[USERNAME]"),
            ("user_domain", "[COMPUTERNAME]"),
        ):
            _require_literal(scrubber, field_name, identity.get(field_name), placeholder)
        profile = identity.get("user_profile")
        if profile:
            _require_literal(scrubber, "user_profile", profile, "[USERPROFILE]")
            leaf = PureWindowsPath(profile).name
            _require_literal(scrubber, "user_profile", leaf, "[USERNAME]")
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


_CAPTURE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REVIEW_REMINDER = (
    "Review the output before committing: the scrubber cannot recognise every identifier "
    "(e.g. Wi-Fi names or personal device names)."
)


def validate_capture_name(name: str) -> str:
    """Names become directories under fixtures/raw: no separators, no '..', no surprises."""
    if "/" in name or "\\" in name or ".." in name or not _CAPTURE_NAME.fullmatch(name):
        raise ValueError(
            f"invalid capture name {name!r}: use lowercase letters, digits, '-' or '_' "
            "(at most 64 characters, no path separators)"
        )
    return name


def _capture_name_arg(text: str) -> str:
    try:
        return validate_capture_name(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
    """Record ``window`` from ``collector`` into ``root/name`` as a replay directory.

    Everything is collected first and written last (via a staging directory), so a failed
    capture leaves nothing behind and can simply be rerun.
    """
    validate_capture_name(name)
    target = root / name
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    system = collector.system_info()
    events = collector.events(EventQuery(window=window, max_events=5000))
    files: dict[str, Any] = {"events.json": [e.model_dump(mode="json") for e in events]}
    log(f"events.json: {len(events)} events")

    latest = window.end
    optional: list[tuple[str, Callable[[], Any]]] = [
        ("process_snapshot.json", lambda: collector.process_snapshot(25)),
        ("sensors.json", lambda: collector.sensors(None)),
        ("drivers.json", lambda: collector.drivers(list(DEVICE_CLASS_ALLOWLIST))),
    ]
    if include_dxdiag:
        optional.append(("dxdiag.json", collector.dxdiag))
    for filename, produce in optional:
        try:
            result = produce()
        except CollectorError as exc:  # includes CollectorUnavailable
            log(f"{filename}: skipped ({exc})")
            continue
        items = result if isinstance(result, list) else [result]
        for item in items:
            taken_at = getattr(item, "taken_at", None)
            if taken_at is not None and taken_at > latest:
                latest = taken_at
        files[filename] = (
            [i.model_dump(mode="json") for i in result]
            if isinstance(result, list)
            else result.model_dump(mode="json")
        )
        log(f"{filename}: ok")

    # Snapshots are taken after the window closes; "now" must not precede them, or replay
    # would treat the current sensor reading as being in the future.
    scenario = ReplayScenario(
        id=name,
        description=description or f"Captured {window.start:%Y-%m-%d %H:%M}Z",
        now=latest,
        system=system,
    )
    files["scenario.json"] = scenario.model_dump(mode="json")
    if identity is not None:
        files[IDENTIFIERS_FILE] = identity()

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".capture-{name}-", dir=root))
    try:
        for filename, payload in files.items():
            _write_json(staging / filename, payload)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
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
    cap.add_argument("name", type=_capture_name_arg)
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
        print(REVIEW_REMINDER)
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
    print(REVIEW_REMINDER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
