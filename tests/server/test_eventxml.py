from __future__ import annotations

from datetime import UTC, datetime

import pytest
from defusedxml import ElementTree as ET
from pydantic import ValidationError

from pc_diag_mcp.collectors.base import CollectorError
from pc_diag_mcp.collectors.eventxml import (
    build_filter_xml,
    clean_message,
    parse_event,
)
from pc_diag_mcp.models import EventLevel, EventQuery, TimeWindow

WINDOW = TimeWindow(
    start=datetime(2026, 10, 6, 5, 0, tzinfo=UTC), end=datetime(2026, 10, 6, 6, 0, 30, tzinfo=UTC)
)


def _select_text(xml: str) -> tuple[str, str]:
    root = ET.fromstring(xml)
    query = root.find("Query")
    assert query is not None
    select = query.find("Select")
    assert select is not None and select.text is not None
    return query.get("Path", ""), select.text


def test_filter_xml_contains_all_clauses() -> None:
    query = EventQuery(
        window=WINDOW,
        logs=["System"],
        providers=["Microsoft-Windows-Kernel-Power", "Display"],
        event_ids=[41, 4101],
        levels=[EventLevel.CRITICAL, EventLevel.INFORMATION],
    )
    path, select = _select_text(build_filter_xml(query, "System"))
    assert path == "System"
    assert "@Name='Microsoft-Windows-Kernel-Power' or @Name='Display'" in select
    assert "EventID=41 or EventID=4101" in select
    # Information covers both Level 4 and LogAlways (0), which WER uses.
    assert "Level=1 or Level=0 or Level=4" in select
    assert "@SystemTime>='2026-10-06T05:00:00.000Z'" in select
    assert "@SystemTime<='2026-10-06T06:00:30.000Z'" in select


def test_filter_xml_escapes_comparison_operators() -> None:
    xml = build_filter_xml(EventQuery(window=WINDOW, logs=["Application"]), "Application")
    assert "&gt;=" in xml and "&lt;=" in xml
    assert ">='" not in xml.replace("&gt;='", "")


def test_filter_xml_without_optional_filters_is_time_only() -> None:
    _, select = _select_text(build_filter_xml(EventQuery(window=WINDOW), "Application"))
    assert "Provider" not in select and "EventID" not in select and "Level" not in select


def test_filter_xml_rejects_log_not_in_query() -> None:
    with pytest.raises(ValueError, match="not requested"):
        build_filter_xml(EventQuery(window=WINDOW, logs=["System"]), "Application")


@pytest.mark.parametrize(
    "hostile",
    [
        "x' or 1=1 or @Name='y",
        "Display]]</Select><Select Path='Security'>*",
        'a"; Remove-Item C:\\ -Recurse; "',
        "$(Get-Process)",
        "name`n",
        "",
        "a" * 129,
    ],
)
def test_hostile_provider_strings_are_rejected_by_validation(hostile: str) -> None:
    with pytest.raises(ValidationError):
        EventQuery(window=WINDOW, providers=[hostile])


def test_builder_rejects_quotes_even_if_validation_is_bypassed() -> None:
    forged = EventQuery.model_construct(
        window=WINDOW, logs=["System"], providers=["a'b"], event_ids=None, levels=None
    )
    with pytest.raises(ValueError, match="provider"):
        build_filter_xml(forged, "System")


KP41_XML = (
    "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
    "<Provider Name='Microsoft-Windows-Kernel-Power'/>"
    "<EventID>41</EventID><Version>10</Version><Level>1</Level>"
    "<TimeCreated SystemTime='2026-10-06T05:53:47.2767968Z'/><EventRecordID>27880</EventRecordID>"
    "<Channel>System</Channel><Computer>HOST</Computer></System><EventData>"
    "<Data Name='BugcheckCode'>159</Data><Data Name='BugcheckParameter1'>0x4</Data>"
    "<Data Name='SleepInProgress'>1</Data><Data Name='PowerButtonTimestamp'>0</Data>"
    "</EventData></Event>"
)


def test_parse_event_reads_system_and_named_data() -> None:
    record = parse_event(KP41_XML, "The system has rebooted\r\n  without cleanly shutting down.")
    assert record.record_id == 27880
    assert record.time == datetime(2026, 10, 6, 5, 53, 47, 276796, tzinfo=UTC)
    assert record.log == "System"
    assert record.provider == "Microsoft-Windows-Kernel-Power"
    assert record.event_id == 41
    assert record.level is EventLevel.CRITICAL
    assert record.message == "The system has rebooted without cleanly shutting down."
    assert record.data["BugcheckCode"] == "159"
    assert record.data["SleepInProgress"] == "1"


def test_parse_event_classic_provider_unnamed_data_and_qualifiers() -> None:
    xml = (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
        "<Provider Name='Display'/><EventID Qualifiers='0'>4101</EventID><Level>3</Level>"
        "<TimeCreated SystemTime='2026-10-06T05:50:00Z'/><EventRecordID>5</EventRecordID>"
        "<Channel>System</Channel></System><EventData><Data>nvlddmkm</Data><Data></Data>"
        "<Binary>00FF</Binary></EventData></Event>"
    )
    record = parse_event(xml, None)
    assert record.event_id == 4101
    assert record.level is EventLevel.WARNING
    assert record.data == {"param1": "nvlddmkm", "param2": ""}
    # No rendered message (provider metadata missing): fall back to the data values.
    assert record.message == "nvlddmkm"


def test_parse_event_flattens_user_data() -> None:
    xml = (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System>"
        "<Provider Name='P'/><EventID>7</EventID><Level>0</Level>"
        "<TimeCreated SystemTime='2026-10-06T05:50:00.5Z'/><EventRecordID>9</EventRecordID>"
        "<Channel>Application</Channel></System><UserData><Ev xmlns='urn:x'>"
        "<Reason>timeout</Reason><Inner><Code>5</Code></Inner><Code>6</Code></Ev></UserData></Event>"
    )
    record = parse_event(xml, "")
    assert record.level is EventLevel.INFORMATION
    assert record.data == {"Reason": "timeout", "Code": "5", "Code_2": "6"}


def test_parse_event_truncates_long_messages_and_values() -> None:
    long = "word " * 500
    xml = KP41_XML.replace(
        "<Data Name='SleepInProgress'>1</Data>", f"<Data Name='Big'>{long}</Data>"
    )
    record = parse_event(xml, long)
    assert len(record.message) <= 600 and record.message.endswith("…")
    assert len(record.data["Big"]) <= 600


@pytest.mark.parametrize(
    "bad",
    [
        "<not-closed",
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System/></Event>",
        "<!DOCTYPE x [<!ENTITY a 'b'>]><Event>&a;</Event>",
    ],
)
def test_parse_event_rejects_malformed_or_entity_xml(bad: str) -> None:
    with pytest.raises(CollectorError):
        parse_event(bad, "")


def test_clean_message_collapses_whitespace() -> None:
    assert clean_message("  a\r\n\tb   c ") == "a b c"
    assert clean_message(None) == ""
