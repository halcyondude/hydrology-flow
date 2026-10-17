from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hydrology_flow import cli
from hydrology_flow.aoi import AOIResolver
from hydrology_flow.config import Settings
from tests.conftest import REPO_ROOT, FakeBoundaries, FakeHucs

ALL_COMMANDS = ["aoi", *cli.STUBS]


def test_top_level_help_lists_every_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ALL_COMMANDS:
        assert name in out
    assert "examples:" in out


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_each_subcommand_self_documents(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--profile", "--zip", "--place", "--county", "--state", "--bbox", "--huc"):
        assert flag in out


@pytest.mark.parametrize("command", list(cli.STUBS))
def test_stubs_exit_2_with_issue_number(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([command, "--zip", "12345"]) == 2
    err = capsys.readouterr().err
    assert f"not implemented (see issue #{cli.STUBS[command][0]})" in err


def test_aoi_flags_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["aoi", "--zip", "12345", "--state", "ST"])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_county_is_repeatable_but_exclusive_with_others() -> None:
    args = cli.build_parser().parse_args(["aoi", "--county", "A County, ST", "--county", "B, ST"])
    assert cli.spec_from_args(args).county == ("A County, ST", "B, ST")
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["aoi", "--county", "A, ST", "--zip", "12345"])


def test_aoi_requires_a_selector() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["aoi"])
    assert exc.value.code == 2


def test_bbox_and_huc_parse() -> None:
    args = cli.build_parser().parse_args(["aoi", "--bbox", "-2.5", "1.5", "-1.5", "2.5"])
    assert cli.spec_from_args(args).bbox == (-2.5, 1.5, -1.5, 2.5)
    args = cli.build_parser().parse_args(["aoi", "--huc", "0101,0102"])
    assert cli.spec_from_args(args).huc == ("0101", "0102")


def test_profile_selector_uses_profile_aoi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    args = cli.build_parser().parse_args(["aoi", "--profile", "worcester-ma"])
    assert cli.spec_from_args(args).county == ("Worcester County, MA",)


def test_aoi_command_writes_geojson(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "default_resolver",
        lambda client, s: AOIResolver(FakeBoundaries(), FakeHucs(), cache_dir=s.aoi_cache_dir),
    )
    out = tmp_path / "aoi.geojson"
    assert cli.main(["aoi", "--state", "ST", "--out", str(out)]) == 0
    feature = json.loads(out.read_text())
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["huc12_count"] == 2
    stdout = capsys.readouterr().out
    assert "HUC12 count:    2" in stdout
    assert "010100000101" in stdout


def test_aoi_error_exit_code(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert cli.main(["aoi", "--profile", "does-not-exist"]) == 1
    assert "error:" in capsys.readouterr().err


# ---- the hard-coded-geography guard (also run in CI) -------------------------------

GUARD = REPO_ROOT / "scripts" / "check_no_geography.sh"


def run_guard(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(GUARD), str(target)], capture_output=True, text=True, check=False)


def test_guard_passes_on_real_src() -> None:
    result = run_guard(REPO_ROOT / "src")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "planted",
    [
        "LAT = 42.28425",
        "LON = -71.85749",
        'DAM = "MA00123"',
        "COMID = 6116810",
        'GAUGE = "01109570"',
    ],
)
def test_guard_fails_on_planted_geography(tmp_path: Path, planted: str) -> None:
    (tmp_path / "mod.py").write_text(planted + "\n")
    result = run_guard(tmp_path)
    assert result.returncode == 1
    assert "ERROR" in result.stderr


def test_guard_ignores_short_decimals(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("RATE = 5.0\nVERSION = '0.1.0'\nTOL = 5e-4\n")
    assert run_guard(tmp_path).returncode == 0
