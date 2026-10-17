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
    aoi_id VARCHAR NOT NULL,
    chunk VARCHAR NOT NULL,
    stage VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    error VARCHAR,
    artifact VARCHAR,
    PRIMARY KEY (aoi_id, chunk, stage)
)
"""


@dataclass(frozen=True)
class Chunk:
    aoi_id: str
    code: str  # HUC code at the planned chunk level
    huc12: tuple[str, ...]


@dataclass(frozen=True)
class TilePlan:
    aoi_id: str
    aoi_name: str
    chunk_digits: int
    stages: tuple[str, ...]
    chunks: tuple[Chunk, ...]


def aoi_id(aoi: AOI) -> str:
    """Stable id from the AOI's covering HUC12 set (same cover => same work)."""
    digest = hashlib.sha256(",".join(sorted(aoi.huc12_codes)).encode()).hexdigest()
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
    ident = aoi_id(aoi)
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
        """Insert missing rows as ``pending``. Returns how many rows were new."""
        now = datetime.now(UTC)
        before = self.count(plan.aoi_id)
        rows = [
            (c.aoi_id, c.code, stage, "pending", now) for c in plan.chunks for stage in plan.stages
        ]
        self._con.executemany(
            "INSERT INTO chunks (aoi_id, chunk, stage, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            rows,
        )
        return self.count(plan.aoi_id) - before

    def count(self, aoi: str) -> int:
        row = self._con.execute("SELECT count(*) FROM chunks WHERE aoi_id = ?", [aoi]).fetchone()
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
            "artifact = coalesce(?, artifact) WHERE aoi_id = ? AND chunk = ? AND stage = ? "
            "RETURNING 1",
            [status, datetime.now(UTC), error, artifact, aoi, chunk, stage],
        ).fetchall()
        if not updated:
            raise KeyError((aoi, chunk, stage))

    def status(self, aoi: str, chunk: str, stage: str) -> Status:
        row = self._con.execute(
            "SELECT status FROM chunks WHERE aoi_id = ? AND chunk = ? AND stage = ?",
            [aoi, chunk, stage],
        ).fetchone()
        if row is None:
            raise KeyError((aoi, chunk, stage))
        return row[0]  # type: ignore[no-any-return]

    def reset_interrupted(self, aoi: str) -> int:
        """Return ``running`` rows (from a crashed run) to ``pending``."""
        rows = self._con.execute(
            "UPDATE chunks SET status = 'pending', updated_at = ? "
            "WHERE aoi_id = ? AND status = 'running' RETURNING 1",
            [datetime.now(UTC), aoi],
        ).fetchall()
        return len(rows)

    def runnable(self, plan: TilePlan, stage: str, *, retry_failed: bool = False) -> list[str]:
        """Chunks ready to run ``stage``: the stage is pending (or failed, if asked) and every
        earlier stage in the plan is ``done`` for that chunk."""
        if stage not in plan.stages:
            raise ValueError(f"stage {stage!r} not in plan")
        earlier = plan.stages[: plan.stages.index(stage)]
        wanted = ("pending", "failed") if retry_failed else ("pending",)
        out: list[str] = []
        for chunk in plan.chunks:
            if self.status(plan.aoi_id, chunk.code, stage) not in wanted:
                continue
            if all(self.status(plan.aoi_id, chunk.code, s) == "done" for s in earlier):
                out.append(chunk.code)
        return out

    def summary(self, aoi: str) -> dict[str, dict[str, int]]:
        """``{stage: {status: count}}`` for one AOI, including zero counts."""
        out: dict[str, dict[str, int]] = {s: dict.fromkeys(STATUSES, 0) for s in STAGES}
        for stage, status, n in self._con.execute(
            "SELECT stage, status, count(*) FROM chunks WHERE aoi_id = ? GROUP BY ALL", [aoi]
        ).fetchall():
            out[stage][status] = int(n)
        return {s: c for s, c in out.items() if any(c.values())}
