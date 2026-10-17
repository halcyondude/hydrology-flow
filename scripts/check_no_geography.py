#!/usr/bin/env python3
"""Fail if a source tree contains hard-coded geography.

Geography (coordinates, site ids, place names) belongs in ``profiles/<slug>/profile.yaml``.
This checker scans every file under the target directory (default ``src``):

* ``.py`` files are parsed with ``ast``/``tokenize`` so findings depend on structure, not on
  how a literal happens to be spelled (digit separators, unary minus, tuple vs list, ...).
* ``.yaml``/``.yml``/``.json``/``.toml``/``.cfg``/``.ini``/``.txt``/``.csv`` files are scanned
  as text with equivalent rules.

Rules (each finding names its rule):

``coord``
    a float with >= 2 decimals that is bound to a coordinate-like name (lat, lon, lng, x, y,
    coord, ...), or two or more such floats that sit together in a tuple/list/call and all lie
    in the latitude/longitude range (so ``(42.28, -71.86)`` is caught but a lone ``9.80665`` or
    ``0.2083`` is not).
``separator``
    a float whose fractional digits use ``_`` separators (a way to dodge digit regexes).
``id``
    an int/str bound to a name like comid/nid/huc/site/station/fips/reachcode/geoid/gauge when
    the value looks like an identifier (>= 5 digits, or letters followed by >= 4 digits).
``nid``
    any string containing a NID-style id (two letters + five digits), any case.
``place``
    a string naming a US place or county with its state (``"Springfield County, MA"``), or a
    full US state name.

Escape hatch: put ``# geo-ok: <reason>`` (a non-empty reason is required) on any line of the
offending statement. Use it for neutral placeholders, never for real site data.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

PRAGMA = re.compile(r"geo-ok:\s*(\S.*)")
LAT_MAX = 90.0
LON_MAX = 180.0
MIN_DECIMALS = 2
MIN_ID_DIGITS = 5

GEO_NAME = re.compile(
    r"(^|_)(lat|lats|lon|lons|lng|long|latitude|longitude|x|y|coord|coords|coordinate|"
    r"coordinates|centroid|latlon|lonlat|xy)(_|\d|$)",
    re.IGNORECASE,
)
ID_NAME = re.compile(
    r"(comid|(^|_)nid(_|$)|huc\d*|(^|_)site(s)?(_|$)|station|fips|reachcode|geoid|gauge|"
    r"usgs|ghcn|(^|_)au(_|$)|zcta)",
    re.IGNORECASE,
)
ID_VALUE = re.compile(r"^(\d{5,}|[A-Za-z]{1,4}\d{4,}|\d+[A-Za-z]+\d+)$")
NID_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{5}(?!\d)", re.IGNORECASE)

US_STATE_ABBREVS = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
    "PR",
    "GU",
    "VI",
    "AS",
    "MP",
]
US_STATE_NAMES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
    "Wisconsin", "Wyoming", "District of Columbia", "Puerto Rico",
]  # fmt: skip
PLACE_WITH_STATE = re.compile(
    r"[A-Z][A-Za-z.'-]+(?: [A-Za-z.'-]+)*,\s*(?:" + "|".join(US_STATE_ABBREVS) + r")\b"
)
STATE_NAME = re.compile(r"\b(?:" + "|".join(US_STATE_NAMES) + r")\b", re.IGNORECASE)

TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".txt", ".csv"}
TEXT_FLOAT = re.compile(r"(?<![\w.])-?\d{1,3}\.\d{2,}(?![\w.])")
TEXT_COORD_KEY = re.compile(
    r"""["']?\b(?:\w*_)?(lat|lon|lng|latitude|longitude|x|y|coord\w*)\b["']?\s*[:=]\s*(-?\d+\.\d{2,})""",
    re.IGNORECASE,
)
TEXT_ID_KEY = re.compile(
    r"""["']?([\w-]*(?:comid|nid|huc\d*|site|station|fips|reachcode|geoid|gauge|usgs|ghcn|zcta)[\w-]*)["']?\s*[:=]\s*["']?([A-Za-z0-9]{5,})""",
    re.IGNORECASE,
)
TEXT_DIGIT_SEP = re.compile(r"\d+\.\d*_\d")


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    message: str
    end_line: int = 0

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


# ---------------------------------------------------------------- python ----


def _decimals(source: str) -> int:
    """Decimal places as written (0 for ints and exponent forms)."""
    text = source.replace("_", "")
    if "e" in text.lower() or "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _const_number(node: ast.AST) -> float | None:
    """Value of a (possibly negated) numeric literal node."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        inner = _const_number(node.operand)
        return None if inner is None else (-inner if isinstance(node.op, ast.USub) else inner)
    if isinstance(node, ast.Constant) and isinstance(node.value, float):
        return node.value
    return None


def _target_names(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.Tuple | ast.List):
        for elt in node.elts:
            yield from _target_names(elt)


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.findings: list[Finding] = []

    # -- helpers
    def _add(self, node: ast.AST, rule: str, message: str) -> None:
        line = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", line) or line
        self.findings.append(Finding(self.path, line, rule, message, end))

    def _decimals_of(self, node: ast.AST) -> int:
        inner = node.operand if isinstance(node, ast.UnaryOp) else node
        seg = ast.get_source_segment(self.source, inner) or ""
        return _decimals(seg)

    def _coordinate_like(self, node: ast.AST) -> bool:
        value = _const_number(node)
        if value is None or self._decimals_of(node) < MIN_DECIMALS:
            return False
        return abs(value) <= LON_MAX

    def _check_bound(self, names: list[str], value: ast.AST, anchor: ast.AST) -> None:
        for name in names:
            if GEO_NAME.search(name) and self._coordinate_like(value):
                self._add(anchor, "coord", f"coordinate-like literal bound to {name!r}")
            if ID_NAME.search(name):
                self._check_id_value(name, value, anchor)

    def _check_id_value(self, name: str, value: ast.AST, anchor: ast.AST) -> None:
        if isinstance(value, ast.Constant):
            v = value.value
            if isinstance(v, bool):
                return
            if isinstance(v, int) and abs(v) >= 10 ** (MIN_ID_DIGITS - 1):
                self._add(anchor, "id", f"integer id bound to {name!r}")
            elif isinstance(v, str) and ID_VALUE.match(v.strip()):
                self._add(anchor, "id", f"string id bound to {name!r}")
        elif isinstance(value, ast.Tuple | ast.List | ast.Set):
            for elt in value.elts:
                self._check_id_value(name, elt, anchor)

    # -- bindings
    def visit_Assign(self, node: ast.Assign) -> None:
        names = [n for t in node.targets for n in _target_names(t)]
        self._check_bound(names, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._check_bound(list(_target_names(node.target)), node.value, node)
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg:
            self._check_bound([node.arg], node.value, node.value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                self._check_bound([key.value], value, value)
        self.generic_visit(node)

    def _check_defaults(self, args: ast.arguments) -> None:
        positional = [*args.posonlyargs, *args.args]
        pairs = list(zip(reversed(positional), reversed(args.defaults), strict=False))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d]
        for arg, default in pairs:
            self._check_bound([arg.arg], default, default)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_defaults(node.args)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    # -- groupings of numbers
    def _check_group(self, node: ast.AST, elts: list[ast.expr]) -> None:
        nums = [e for e in elts if self._coordinate_like(e)]
        if len(nums) < 2:  # noqa: PLR2004
            return
        values = [abs(_const_number(e) or 0.0) for e in nums]
        if all(v < 1.0 for v in values):
            return  # fractions/coefficients, not coordinates
        self._add(node, "coord", "two or more coordinate-range floats together")

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._check_group(node, node.elts)
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        self._check_group(node, node.elts)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_group(node, node.args)
        self.generic_visit(node)

    # -- strings
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            text = node.value
            if NID_PATTERN.search(text):
                self._add(node, "nid", f"NID-style id in string {text[:40]!r}")
            elif PLACE_WITH_STATE.search(text) or STATE_NAME.search(text):
                self._add(node, "place", f"US place/state name in string {text[:40]!r}")
        self.generic_visit(node)


def _separator_findings(path: Path, source: str) -> list[Finding]:
    out: list[Finding] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.NUMBER and "_" in tok.string and "." in tok.string:
                frac = tok.string.split(".", 1)[1]
                if "_" in frac:
                    out.append(
                        Finding(path, tok.start[0], "separator", f"digit separator in {tok.string}")
                    )
    except tokenize.TokenError:
        pass
    return out


def _pragma_lines(source: str) -> set[int]:
    lines: set[int] = set()
    for i, line in enumerate(source.splitlines(), start=1):
        if "#" in line and (m := PRAGMA.search(line.split("#", 1)[1])) and m.group(1).strip():
            lines.add(i)
    return lines


def check_python(path: Path) -> list[Finding]:
    source = path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "syntax", f"cannot parse: {exc.msg}")]
    visitor = _Visitor(path, source)
    visitor.visit(tree)
    ok_lines = _pragma_lines(source)
    kept = [
        f
        for f in visitor.findings
        if not any(line in ok_lines for line in range(f.line, max(f.end_line, f.line) + 1))
    ]
    for f in _separator_findings(path, source):
        if f.line not in ok_lines:
            kept.append(f)
    return kept


# ------------------------------------------------------------------ text ----


def check_text(path: Path) -> list[Finding]:
    out: list[Finding] = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if (m := PRAGMA.search(line)) and m.group(1).strip():
            continue
        hits: list[tuple[str, str]] = []
        if TEXT_DIGIT_SEP.search(line):
            hits.append(("separator", "digit separator in a decimal"))
        if m := TEXT_COORD_KEY.search(line):
            hits.append(("coord", f"coordinate-like value for {m.group(1)!r}"))
        floats = [
            float(f)
            for f in TEXT_FLOAT.findall(line)
            if abs(float(f)) <= LON_MAX and abs(float(f)) >= 1.0
        ]
        if len(floats) >= 2 and not any(h[0] == "coord" for h in hits):  # noqa: PLR2004
            hits.append(("coord", "two or more coordinate-range floats on one line"))
        if (m := TEXT_ID_KEY.search(line)) and ID_VALUE.match(m.group(2)):
            hits.append(("id", f"id value for {m.group(1)!r}"))
        if NID_PATTERN.search(line):
            hits.append(("nid", "NID-style id"))
        if PLACE_WITH_STATE.search(line) or STATE_NAME.search(line):
            hits.append(("place", "US place/state name"))
        out.extend(Finding(path, i, rule, msg) for rule, msg in hits)
    return out


# ------------------------------------------------------------------ main ----


def scan(target: Path) -> list[Finding]:
    findings: list[Finding] = []
    files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    for path in files:
        if "__pycache__" in path.parts or ".egg-info" in str(path):
            continue
        if path.suffix == ".py":
            findings.extend(check_python(path))
        elif path.suffix in TEXT_SUFFIXES:
            findings.extend(check_text(path))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0]) if args else Path("src")
    findings = scan(target)
    for f in findings:
        print(f, file=sys.stderr)
    if findings:
        print(
            f"ERROR: {len(findings)} hard-coded geography finding(s) in {target}. Move them to a "
            "region profile, or add '# geo-ok: <reason>' for neutral placeholders.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: no hard-coded geography in {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
