"""Mock MCP tool server for the stable records system.

Read-only, unrelated to the live horse simulation. Exists specifically to
prove the gateway pattern generalizes past a single integration: the same
policy engine and audit log handle this system with zero special-casing
(see tests/test_integration_multi_system.py).
"""

from __future__ import annotations

from typing import Any

DEFAULT_FEEDING_SCHEDULE = [
    {"time": "06:00", "ration": "hay, 2 flakes"},
    {"time": "12:00", "ration": "grain, 1 scoop"},
    {"time": "18:00", "ration": "hay, 2 flakes"},
]

DEFAULT_VET_HISTORY = [
    {"date": "2025-11-03", "note": "Routine checkup, all normal."},
    {"date": "2026-02-14", "note": "Hoof trim, mild thrush treated."},
]


class UnknownToolError(Exception):
    pass


class StableRecordsServer:
    def __init__(
        self,
        feeding_schedule: list[dict[str, Any]] | None = None,
        vet_history: list[dict[str, Any]] | None = None,
    ):
        self._feeding_schedule = feeding_schedule or DEFAULT_FEEDING_SCHEDULE
        self._vet_history = vet_history or DEFAULT_VET_HISTORY

    def execute(self, tool: str, params: dict[str, Any]) -> Any:
        if tool == "get_feeding_schedule":
            return {"schedule": self._feeding_schedule}
        if tool == "get_vet_history":
            return {"history": self._vet_history}
        raise UnknownToolError(tool)
