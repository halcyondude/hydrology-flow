"""``hflow`` command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import httpx

from hydrology_flow import __version__
from hydrology_flow.aoi import AOI, AOISpec, default_resolver
from hydrology_flow.aoi.arcgis import ArcGISError
from hydrology_flow.aoi.tigerweb import BoundaryNotFoundError
from hydrology_flow.config import Settings, get_settings
from hydrology_flow.http import create_client
from hydrology_flow.profiles import ProfileNotFoundError, load_profile
from hydrology_flow.telemetry import setup_telemetry, shutdown_telemetry

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
HUC_LIST_PREVIEW = 20
HUC8_DIGITS = 8

# subcommand -> (tracking issue, one-line description)
STUBS: dict[str, tuple[int, str]] = {
    "acquire": (3, "Download DEM, NHD/WBD/NLDI, met, soils/NLCD, NID, gauges and WQP for the AOI."),
    "condition": (
        4,
        "Condition terrain and derive flow direction/accumulation and HAND per HUC12.",
    ),
    "delineate": (4, "Delineate focus-site watersheds and build stage-storage curves."),
    "sediment": (5, "Erosion hotspots, sediment delivery and water-quality synthesis."),
    "model": (6, "Curve numbers, design storms, unit hydrograph and reservoir routing."),
    "rainflow": (6, "Rainflow analysis of simulated stage/flow and continuous simulation."),
    "bake": (8, "Bake static tiles and assets for the 3D viewer."),
    "all": (1, "Run every stage for the AOI (see the epic for sequencing)."),
}

AOI_EXAMPLES = """\
examples:
  hflow aoi --profile my-region
  hflow aoi --zip 12345
  hflow aoi --place "Exampleville, ST"
  hflow aoi --county "Alpha County, ST" --county "Beta County, ST"
  hflow aoi --state ST --out state.geojson
  hflow aoi --bbox WEST SOUTH EAST NORTH
  hflow aoi --huc 01234567
"""


def add_aoi_flags(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_argument_group(
        "area of interest",
        "Pick exactly one selector. --county may be repeated; the others are mutually exclusive.",
    ).add_mutually_exclusive_group(required=required)
    group.add_argument("--profile", metavar="SLUG", help="region profile slug (profiles/<slug>/)")
    group.add_argument("--zip", metavar="ZIP", help="5-digit ZIP (Census ZCTA)")
    group.add_argument(
        "--place", metavar="'CITY, ST'", help="Census place, e.g. 'Exampleville, ST'"
    )
    group.add_argument(
        "--county",
        metavar="'NAME COUNTY, ST'",
        action="append",
        help="county; repeat for several (their union is the AOI)",
    )
    group.add_argument("--state", metavar="ST", help="state postal code or name")
    group.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="bounding box in EPSG:4326 degrees",
    )
    group.add_argument(
        "--huc", metavar="CODE[,CODE]", help="hydrologic unit code(s), 2 to 12 digits"
    )


def spec_from_args(args: argparse.Namespace) -> AOISpec:
    if args.profile:
        return load_profile(args.profile).aoi
    return AOISpec(
        zip=args.zip,
        place=args.place,
        county=tuple(args.county or ()),
        state=args.state,
        bbox=tuple(args.bbox) if args.bbox else None,
        huc=tuple(c.strip() for c in args.huc.split(",")) if args.huc else (),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hflow",
        description=(
            "hydrology-flow: reproducible watershed hydrology from public data. "
            "Pick an area (ZIP, place, counties, state, bbox or HUC); work runs per HUC12."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=AOI_EXAMPLES,
    )
    parser.add_argument("--version", action="version", version=f"hflow {__version__}")
    parser.add_argument(
        "--telemetry",
        choices=("console", "otlp", "none"),
        help="opt in to telemetry export (default: none; otlp if HFLOW_OTLP_ENDPOINT is set)",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    aoi = sub.add_parser(
        "aoi",
        help="resolve an area of interest to geometry and its covering HUC12 set",
        description=(
            "Resolve an AOI via Census TIGERweb (ZIP, place, county, state) and the USGS WBD "
            "(HUC12 cover). Results are cached by content hash under the cache directory."
        ),
        epilog=AOI_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_aoi_flags(aoi, required=True)
    aoi.add_argument("--out", type=Path, metavar="FILE", help="write the AOI as a GeoJSON Feature")
    aoi.add_argument("--no-cache", action="store_true", help="ignore cached AOI results")

    for name, (issue, desc) in STUBS.items():
        stub = sub.add_parser(
            name,
            help=f"{desc} (not implemented; see issue #{issue})",
            description=f"{desc}\n\nNot implemented yet: see issue #{issue}.",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        add_aoi_flags(stub, required=False)
    return parser


def format_summary(aoi: AOI) -> str:
    west, south, east, north = aoi.bounds
    lines = [
        f"AOI: {aoi.name}",
        f"  selector:       {aoi.spec.label()}",
        f"  area:           {aoi.area_sqkm:,.1f} km2",
        f"  bounds (4326):  W {west:.4f}  S {south:.4f}  E {east:.4f}  N {north:.4f}",
        f"  projected CRS:  {aoi.projected_crs}",
        "  sources:",
        *(f"    - {s.kind} {s.source_id} {s.name} ({s.source})" for s in aoi.sources),
        f"  HUC12 count:    {len(aoi.huc12)}"
        + (
            f"  (dropped {aoi.huc12_dropped} with < {aoi.huc_min_overlap_fraction:.0%} overlap)"
            if aoi.huc_min_overlap_fraction
            else ""
        ),
    ]
    per_huc8 = Counter(h.huc12[:HUC8_DIGITS] for h in aoi.huc12)
    lines.append(
        f"  HUC8 basins:    {len(per_huc8)}  ("
        + ", ".join(f"{k}: {v}" for k, v in sorted(per_huc8.items())[:HUC_LIST_PREVIEW])
        + ")"
    )
    for h in aoi.huc12[:HUC_LIST_PREVIEW]:
        area = f"{h.area_sqkm:8.1f} km2" if h.area_sqkm is not None else ""
        lines.append(f"    {h.huc12}  {area}  {h.name}")
    if len(aoi.huc12) > HUC_LIST_PREVIEW:
        lines.append(
            f"    ... {len(aoi.huc12) - HUC_LIST_PREVIEW} more (use --out for the full list)"
        )
    return "\n".join(lines)


async def run_aoi(args: argparse.Namespace, settings: Settings) -> int:
    spec = spec_from_args(args)
    async with create_client(settings) as client:
        aoi = await default_resolver(client, settings).resolve(spec, use_cache=not args.no_cache)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(aoi.to_feature()))
    print(format_summary(aoi))
    if args.out:
        print(f"wrote {args.out}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in STUBS:
        print(
            f"{args.command}: not implemented (see issue #{STUBS[args.command][0]})",
            file=sys.stderr,
        )
        return EXIT_USAGE

    settings = get_settings()
    if args.telemetry:
        settings = settings.model_copy(update={"telemetry_exporter": args.telemetry})
    setup_telemetry(settings)
    try:
        return asyncio.run(run_aoi(args, settings))
    except (ValueError, ProfileNotFoundError, BoundaryNotFoundError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ArcGISError as exc:
        print(f"error: the data service reported a failure: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except httpx.HTTPError as exc:
        print(f"error: network request failed: {exc!r}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    sys.exit(main())
