"""The AST geography guard (scripts/check_no_geography.py): every bypass found in review."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "check_no_geography.py"
spec = importlib.util.spec_from_file_location("check_no_geography", SCRIPT)
assert spec and spec.loader
guard: Any = importlib.util.module_from_spec(spec)
sys.modules["check_no_geography"] = guard
spec.loader.exec_module(guard)


def rules(tmp_path: Path, filename: str, text: str) -> set[str]:
    (tmp_path / filename).write_text(text)
    return {f.rule for f in guard.scan(tmp_path)}


def test_real_src_is_clean() -> None:
    assert guard.scan(REPO_ROOT / "src") == []


def test_cli_exit_codes(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "a.py").write_text("X = 1\n")
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "a.py").write_text("lat = 42.28425\n")

    def run(d: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True, check=False
        )

    assert run(clean).returncode == 0
    bad = run(dirty)
    assert bad.returncode == 1
    assert "[coord]" in bad.stderr


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        # the original regex cases
        ("LAT = 42.28425\n", "coord"),
        ("LON = -71.85749\n", "coord"),
        ('DAM = "MA00123"\n', "nid"),
        ("COMID = 6116810\n", "id"),
        ('GAUGE = "01109570"\n', "id"),
        # bypasses: 3-decimal coordinates, alone-by-name and as a pair
        ("lat = 42.284\n", "coord"),
        ("POINT = (42.284, -71.857)\n", "coord"),
        ("POINT = [42.28, -71.86]\n", "coord"),
        ("p = Point(-71.857, 42.284)\n", "coord"),
        ("BBOX = (-72.31, 42.01, -71.48, 42.72)\n", "coord"),
        ("f(lon=-71.857)\n", "coord"),
        ('d = {"lat": 42.28, "lon": -71.86}\n', "coord"),
        ("def f(x=-71.86): ...\n", "coord"),
        ("x: float = -71.86\n", "coord"),
        # bypass: digit separators
        ("v = 42.284_25\n", "separator"),
        ("v = -71.857_49\n", "separator"),
        # bypasses: other COMIDs, other id names, ints and strings
        ("comid = 6116811\n", "id"),
        ('nid = "TX01234"\n', "nid"),
        ("station_id = 94746\n", "id"),
        ('usgs_site = "01109571"\n', "id"),
        ('fips = "25027"\n', "id"),
        ("fips = 25027\n", "id"),
        ('reachcode = "01090003000173"\n', "id"),
        ("huc = 109000301\n", "id"),
        ("f(comid=1234567)\n", "id"),
        ('d = {"comid": 6116810}\n', "id"),
        # bypass: lowercase / embedded NID strings
        ('dam = "ma00123"\n', "nid"),
        ('url = "https://nid.example/dam/CA10234/info"\n', "nid"),
        # bypass: place names
        ('p = "Worcester County, MA"\n', "place"),
        ('p = "Boulder, CO"\n', "place"),
        ('p = "Massachusetts"\n', "place"),
        ('p = "new york"\n', "place"),
    ],
)
def test_python_bypasses_are_caught(tmp_path: Path, text: str, rule: str) -> None:
    assert rule in rules(tmp_path, "m.py", text)


@pytest.mark.parametrize(
    ("filename", "text", "rule"),
    [
        ("p.yaml", "lat: 42.284\n", "coord"),
        ("p.yaml", "coords: [42.284, -71.857]\n", "coord"),
        ("p.yaml", "comid: 6116811\n", "id"),
        ("p.yaml", "nid: ma00123\n", "nid"),
        ("p.yaml", "city: Worcester County, MA\n", "place"),
        ("d.json", '{"lat": 42.284, "lon": -71.857}\n', "coord"),
        ("d.json", '{"site": "01109571"}\n', "id"),
        ("d.json", '{"where": "Boulder, CO"}\n', "place"),
        ("c.toml", 'station = "USW00094746"\n', "id"),
        ("c.toml", "latitude = 42.28\n", "coord"),
        ("c.toml", "v = 42.284_25\n", "separator"),
        ("nested/d.json", '{"dam": "TX01234"}\n', "nid"),
    ],
)
def test_text_files_are_scanned(tmp_path: Path, filename: str, text: str, rule: str) -> None:
    (tmp_path / filename).parent.mkdir(parents=True, exist_ok=True)
    assert rule in rules(tmp_path, filename, text)


@pytest.mark.parametrize(
    "text",
    [
        "STANDARD_GRAVITY = 9.80665\n",
        "COEFF = 0.2083\n",
        "g = 9.80665\n",
        "RATIOS = (0.2083, 0.1250)\n",  # fractions, all below 1
        "TOL = 5e-4\n",
        "RATE = 5.0\nBURST = 10\nPAGE = 1000\n",
        "HUC_MIN_DIGITS = 2\nHUC12_DIGITS = 12\n",
        "EPSG_UTM_NORTH = 32600\n",
        "LON_MIN, LON_MAX = -180.0, 180.0\n",
        "N = 1_000_000\nT = 30_000\nS = 1_000_000.0\n",
        'URL = "https://example.org/arcgis/rest/services/x/MapServer"\n',
        'HUC_FIELDS = "huc12,name,areasqkm,states"\n',
        'EXAMPLE = "Alpha County, ST"\n',
        'TIME = "Washington" if False else None  # geo-ok: test of pragma placement\n',
    ],
)
def test_physical_constants_and_neutral_code_pass(tmp_path: Path, text: str) -> None:
    assert rules(tmp_path, "m.py", text) == set()


def test_pragma_with_reason_allows(tmp_path: Path) -> None:
    ok = 'EXAMPLE = "Boulder, CO"  # geo-ok: neutral CLI help placeholder\n'
    assert rules(tmp_path, "m.py", ok) == set()


def test_pragma_covers_multiline_statement(tmp_path: Path) -> None:
    text = 'HELP = """\nexample: Boulder, CO\n"""  # geo-ok: neutral help text\n'
    assert rules(tmp_path, "m.py", text) == set()


def test_pragma_without_reason_does_not_allow(tmp_path: Path) -> None:
    assert "place" in rules(tmp_path, "m.py", 'E = "Boulder, CO"  # geo-ok:\n')
    assert "place" in rules(tmp_path, "m.py", 'E = "Boulder, CO"  # geo-ok\n')


def test_pragma_is_per_statement(tmp_path: Path) -> None:
    text = 'A = "Boulder, CO"  # geo-ok: placeholder\nlat = 42.284\n'
    assert rules(tmp_path, "m.py", text) == {"coord"}


def test_text_pragma(tmp_path: Path) -> None:
    assert rules(tmp_path, "p.yaml", "lat: 42.284  # geo-ok: documented example\n") == set()
