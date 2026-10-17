# Contributing

## Setup

```sh
uv sync            # creates .venv with runtime + dev dependencies
uv run pytest      # unit tests; network tests are skipped by default
uv run pytest -m network   # live tests against Census TIGERweb and USGS WBD (slow, ~2 min)
```

Checks that CI runs, all of which must pass locally first:

```sh
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy --strict src tests
uv run python scripts/check_no_geography.py src
uv run pytest
```

## Rule 1: geography is data, not code

`src/` must not contain latitudes, longitudes, COMIDs, station ids, FIPS codes, NID ids or place
names. Site-specific values live in `profiles/<slug>/profile.yaml`.

CI runs `scripts/check_no_geography.py`, an AST-based checker over every file under `src/`
(`.py` by AST; yaml/json/toml as text). It flags:

- `coord`: floats with 2+ decimals bound to names like `lat`, `lon`, `x`, `y`, `coord`, or two or
  more coordinate-range floats together in a tuple, list or call. Lone physical constants such as
  `9.80665` or `0.2083` pass.
- `separator`: `_` digit separators inside a decimal (`42.284_25`).
- `id`: int/str ids (5+ digits, or letters plus 4+ digits) bound to names containing comid, nid,
  huc, site, station, fips, reachcode, geoid, gauge and similar.
- `nid`: any string holding a NID-style id (two letters plus five digits), any case.
- `place`: strings naming a US place with its state, or a US state name.

For a neutral placeholder that the checker cannot tell from real data, add
`# geo-ok: <reason>` to the statement. A reason is required. Never use it for real site data.
CLI help and docstrings use placeholders (`12345`, `Alpha County, ST`), not real places; real
examples go in the README.

## Rule 2: every assumption is marked and bounded

A value is an *assumption* if it encodes a modeling judgement rather than a fact from a source or
a pure engineering knob. Examples: a generalization tolerance, a curve number default, a
sediment delivery ratio, a rainfall-loss fraction. Each one needs:

1. A named constant (no magic numbers in logic).
2. An `# ASSUMPTION:` comment that says what is assumed, why, and the **sensitivity range** over
   which the result is expected to hold.
3. An entry in `docs/assumptions.md` when it is unresolved or affects results a reader would care
   about.

```python
# ASSUMPTION: AOI boundary generalization tolerance, in degrees of EPSG:4326.
# 5e-4 deg is roughly 50 m. Sensitivity range: 0 (full resolution) to 5e-3 (~500 m).
DEFAULT_AOI_SIMPLIFY_DEG = 5e-4
```

In YAML (profiles), use the same `# ASSUMPTION:` comment prefix. Do not invent hydrologic
parameters: if a value is unknown, leave it unset and record it as an open question.

## Rule 3: HTTP goes through `hydrology_flow.http`

Create clients with `hydrology_flow.http.create_client()`. It provides per-host rate limiting,
retries with backoff and jitter (honoring `Retry-After`), structured logs, an OpenTelemetry span
per request, and a content-hashed on-disk cache. Never use `requests`; never build a bare
`httpx.AsyncClient`.

## Extending the AOI resolver

Boundary and HUC sources are `Protocol` classes in `hydrology_flow/aoi/providers.py`. To add a
source (for example a state GIS service), implement `BoundaryProvider` or `HucProvider` and pass it
to `AOIResolver`. Public-domain sources only.

## Tests

- Every new module ships with tests. Mock HTTP with `respx`.
- Mark anything that touches a live service with `@pytest.mark.network`; CI skips those.
- Test the behavior of the guard by planting a literal in a temp directory (see
  `tests/test_geography_guard.py`), never in `src/`.

## Commits and PRs

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `ci:`). PRs link their issue
(`Closes #N`). A PR that adds or edits Markdown includes a "Rendered docs" section with blob links.

## Private data

Committee-private files go under `data/private/` (gitignored, except `.gitkeep`). Profiles point
at them; they are never committed or published.
