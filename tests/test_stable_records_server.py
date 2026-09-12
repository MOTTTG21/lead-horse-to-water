"""Tests for the stable records MCP server: read-only, unrelated to the
live horse simulation, and simple by design -- it exists to prove the
gateway pattern generalizes past a single integration, not to add new
exploit surface."""

import pytest

from horse_gateway.stable_records_server import StableRecordsServer, UnknownToolError


def test_get_feeding_schedule_returns_data():
    server = StableRecordsServer()
    result = server.execute("get_feeding_schedule", {})
    assert "schedule" in result
    assert len(result["schedule"]) > 0


def test_get_vet_history_returns_data():
    server = StableRecordsServer()
    result = server.execute("get_vet_history", {})
    assert "history" in result
    assert len(result["history"]) > 0


def test_unknown_tool_raises():
    server = StableRecordsServer()
    with pytest.raises(UnknownToolError):
        server.execute("get_ownership_papers", {})


def test_custom_data_can_be_injected_for_testing():
    server = StableRecordsServer(
        feeding_schedule=[{"time": "06:00", "ration": "hay"}],
        vet_history=[{"date": "2026-01-01", "note": "routine checkup"}],
    )
    assert server.execute("get_feeding_schedule", {})["schedule"] == [
        {"time": "06:00", "ration": "hay"}
    ]
    assert server.execute("get_vet_history", {})["history"] == [
        {"date": "2026-01-01", "note": "routine checkup"}
    ]
