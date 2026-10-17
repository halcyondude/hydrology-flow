# Contributing

## Setup

```sh
uv sync            # creates .venv with runtime + dev dependencies
uv run pytest      # unit tests; network tests are skipped by default
uv run pytest -m network   # live tests against Census TIGERweb and USGS WBD
```

Checks that CI runs, all of which must pass locally first:

```sh
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy --strict src tests
scripts/check_no_geography.sh src
uv run pytest
```

## Rule 1: geography is data, not code

`src/` must not contain latitudes, longitudes, COMIDs, station ids, FIPS codes, NID ids or place
names. Site-specific values live in `profiles/<slug>/profile.yaml`. CI runs
`scripts/check_no_geography.sh`, which fails on decimal literals with four or more decimal places
(`-?[0-9]{1,3}\.[0-9]{4,}`) and on the known reference-site ids. If you need a tiny tolerance
written as a decimal, use scientific notation (`5e-4`) and a named constant.

CLI help and docstrings use placeholders (`12345`, `Alpha County, ST`), not real places. Real
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
  `tests/test_cli.py`), never in `src/`.

## Commits and PRs

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `ci:`). PRs link their issue
(`Closes #N`). A PR that adds or edits Markdown includes a "Rendered docs" section with blob links.

## Private data

Committee-private files go under `data/private/` (gitignored, except `.gitkeep`). Profiles point
at them; they are never committed or published.
