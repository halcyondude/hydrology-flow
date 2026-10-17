"""Tiling planner: AOI -> HUC12 work chunks with a resumable DuckDB manifest.

Work is chunked per HUC12 (or coarser, e.g. HUC10) so AOIs far larger than memory are
processed one chunk at a time. The manifest records ``(aoi, chunk, stage)`` status; re-running
a plan skips chunks already ``done`` and can pick up ``failed`` or interrupted ones.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

import duckdb

from hydrology_flow.aoi.models import AOI, HUC12_DIGITS

STAGES: tuple[str, ...] = (
    "acquire",
    "condition",
    "delineate",
    "sediment",
    "model",
    "rainflow",
    "bake",
)
Status = Literal["pending", "running", "done", "failed"]
STATUSES: tuple[Status, ...] = ("pending", "running", "done", "failed")
CHUNK_DIGITS_ALLOWED = (8, 10, HUC12_DIGITS)
AOI_ID_LEN = 16

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    plan_id VARCHAR NOT NULL,
    chunk VARCHAR NOT NULL,
    stage VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    error VARCHAR,
    artifact VARCHAR,
    PRIMARY KEY (plan_id, chunk, stage)
)
"""


@dataclass(frozen=True)
class Chunk:
    plan_id: str
    code: str  # HUC code at the planned chunk level
    huc12: tuple[str, ...]


@dataclass(frozen=True)
class TilePlan:
    plan_id: str
    aoi_name: str
    chunk_digits: int
    stages: tuple[str, ...]
    chunks: tuple[Chunk, ...]


def plan_id(aoi: AOI, chunk_digits: int = HUC12_DIGITS) -> str:
    """Stable id from the covering HUC12 set and the chunk level (same cover + level => same work)."""
    key = f"{chunk_digits}:" + ",".join(sorted(aoi.huc12_codes))
    digest = hashlib.sha256(key.encode()).hexdigest()
    return digest[:AOI_ID_LEN]


def plan_chunks(
    aoi: AOI,
    *,
    chunk_digits: int = HUC12_DIGITS,
    stages: Sequence[str] = STAGES,
) -> TilePlan:
    if chunk_digits not in CHUNK_DIGITS_ALLOWED:
        raise ValueError(f"chunk_digits must be one of {CHUNK_DIGITS_ALLOWED}")
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        raise ValueError(f"unknown stages: {unknown}")
    groups: dict[str, list[str]] = {}
    for code in sorted(aoi.huc12_codes):
        groups.setdefault(code[:chunk_digits], []).append(code)
    ident = plan_id(aoi, chunk_digits)
    chunks = tuple(Chunk(ident, k, tuple(v)) for k, v in sorted(groups.items()))
    return TilePlan(ident, aoi.name, chunk_digits, tuple(stages), chunks)


class Manifest:
    """DuckDB-backed status table. Safe to reopen; ``register`` never clobbers progress."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(path))
        self._con.execute(_SCHEMA)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def register(self, plan: TilePlan) -> int:
        """Insert missing rows as ``pending`` in one statement. Returns how many were new."""
        before = self.count(plan.plan_id)
        self._con.execute(
            "INSERT INTO chunks (plan_id, chunk, stage, status, updated_at) "
            "SELECT ?, c.chunk, s.stage, 'pending', ? "
            "FROM (SELECT unnest(?::VARCHAR[]) AS chunk) c "
            "CROSS JOIN (SELECT unnest(?::VARCHAR[]) AS stage) s "
            "ON CONFLICT DO NOTHING",
            [plan.plan_id, datetime.now(UTC), [c.code for c in plan.chunks], list(plan.stages)],
        )
        return self.count(plan.plan_id) - before

    def count(self, aoi: str) -> int:
        row = self._con.execute("SELECT count(*) FROM chunks WHERE plan_id = ?", [aoi]).fetchone()
        return int(row[0]) if row else 0

    def set_status(
        self,
        aoi: str,
        chunk: str,
        stage: str,
        status: Status,
        *,
        error: str | None = None,
        artifact: str | None = None,
    ) -> None:
        updated = self._con.execute(
            "UPDATE chunks SET status = ?, updated_at = ?, error = ?, "
            "artifact = coalesce(?, artifact) WHERE plan_id = ? AND chunk = ? AND stage = ? "
            "RETURNING 1",
            [status, datetime.now(UTC), error, artifact, aoi, chunk, stage],
        ).fetchall()
        if not updated:
            raise KeyError((aoi, chunk, stage))

    def status(self, aoi: str, chunk: str, stage: str) -> Status:
        row = self._con.execute(
            "SELECT status FROM chunks WHERE plan_id = ? AND chunk = ? AND stage = ?",
            [aoi, chunk, stage],
        ).fetchone()
        if row is None:
            raise KeyError((aoi, chunk, stage))
        return row[0]  # type: ignore[no-any-return]

    def reset_interrupted(self, aoi: str) -> int:
        """Return ``running`` rows (from a crashed run) to ``pending``."""
        rows = self._con.execute(
            "UPDATE chunks SET status = 'pending', updated_at = ? "
            "WHERE plan_id = ? AND status = 'running' RETURNING 1",
            [datetime.now(UTC), aoi],
        ).fetchall()
        return len(rows)

    def runnable(self, plan: TilePlan, stage: str, *, retry_failed: bool = False) -> list[str]:
        """Chunks ready to run ``stage``: the stage is pending (or failed, if asked) and every
        earlier stage in the plan is ``done`` for that chunk. One query, any plan size."""
        if stage not in plan.stages:
            raise ValueError(f"stage {stage!r} not in plan")
        earlier = list(plan.stages[: plan.stages.index(stage)])
        wanted = ["pending", "failed"] if retry_failed else ["pending"]
        rows = self._con.execute(
            "SELECT c.chunk FROM chunks c "
            "WHERE c.plan_id = ? AND c.stage = ? AND list_contains(?, c.status) "
            "AND NOT EXISTS (SELECT 1 FROM chunks e WHERE e.plan_id = c.plan_id "
            "AND e.chunk = c.chunk AND list_contains(?, e.stage) AND e.status <> 'done') "
            "ORDER BY c.chunk",
            [plan.plan_id, stage, wanted, earlier],
        ).fetchall()
        return [r[0] for r in rows]

    def summary(self, aoi: str) -> dict[str, dict[str, int]]:
        """``{stage: {status: count}}`` for one AOI, including zero counts."""
        out: dict[str, dict[str, int]] = {s: dict.fromkeys(STATUSES, 0) for s in STAGES}
        for stage, status, n in self._con.execute(
            "SELECT stage, status, count(*) FROM chunks WHERE plan_id = ? GROUP BY ALL", [aoi]
        ).fetchall():
            out[stage][status] = int(n)
        return {s: c for s, c in out.items() if any(c.values())}
