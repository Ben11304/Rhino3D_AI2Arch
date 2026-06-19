#!/usr/bin/env python3
"""codegen_guard.py — enforce the Rhino codegen contract on a generated snippet.

This is the runnable implementation of the CODEGEN GUARD CONTRACT defined in
shared/conventions.md (section 5). It is stdlib-only and CPython3.

Two modes:
  --lint PATH   Statically analyze a generated Rhino python snippet and print a
                structured PASS / WARN / FAIL report against the contract rules.
  --wrap PATH   Emit the snippet to stdout bracketed by a guard preamble (reads
                tol/ang_tol live, sets nothing it shouldn't) and a postamble that
                redraws the document once. Does not modify the input file.

The contract rules checked (mirroring conventions.md section 5):
  1. `#! python3` shebang on line 1.
  2. `# r:` requirement comments only for genuine third-party packages, never for
     Rhino / rhinoscriptsyntax / scriptcontext / System.
  3. Tolerances read from the document, never hardcoded (no literal 0.001 etc.).
  4. Pre-flight of inputs before Create* (C7) — heuristic.
  5. Null/empty check after every Create*.
  6. IsValid / IsSolid / GetNakedEdges on resulting Brep.
  7. Post-boolean expected-count + total-volume check (C2).
  8. Name + SetUserString("part_id", ...) + layer at bake (C1).
  9. AddBrep (etc.) returns a GUID and a single Views.Redraw() at the end.

Exit code: 0 if no FAIL findings, 1 if any FAIL finding, 2 on usage error.
No third-party imports.
"""

import argparse
import re
import sys


# --- packages that are ALWAYS present in the Rhino python runtime --------------
ALWAYS_PRESENT = ("Rhino", "rhinoscriptsyntax", "scriptcontext", "System")

# --- regexes describing the contract surface ----------------------------------
RE_SHEBANG = re.compile(r"^#!\s*python3\s*$")
RE_REQUIRE = re.compile(r"^#\s*r:\s*(?P<pkg>[A-Za-z0-9_\-\.]+)")
# hardcoded absolute tolerance literals that should be sc.doc.ModelAbsoluteTolerance
RE_HARDCODED_TOL = re.compile(r"(?<![\w.])0\.0+1\b|(?<![\w.])1e-0?[0-9]\b")
RE_LIVE_TOL = re.compile(r"\.ModelAbsoluteTolerance\b")
RE_LIVE_ANGTOL = re.compile(r"\.ModelAngleToleranceRadians\b")

# Create* calls that must be null/empty-checked.
RE_CREATE = re.compile(
    r"\b(?:Brep|Surface|Curve|NurbsSurface|NurbsCurve|Mesh|SubD|Extrusion|"
    r"RevSurface|VolumeMassProperties|AreaMassProperties)\."
    r"(?:Create\w*|CreateFromLoft|CreateFromSweep|CreateFromRevSurface|"
    r"CreateInterpolatedCurve|CreateExtrusion|CapPlanarHoles|JoinBreps|"
    r"CreateBooleanUnion|CreateBooleanDifference|CreateBooleanIntersection|"
    r"CreateOffset|CreateFilletEdges|CreateNetworkSurface)\b"
)

# Boolean ops that REQUIRE a post count+volume check (C2).
RE_BOOLEAN = re.compile(
    r"\bBrep\.CreateBoolean(?:Union|Difference|Intersection)\b"
)

# Validity / closure checks.
RE_ISVALID = re.compile(r"\.IsValid\b")
RE_ISSOLID = re.compile(r"\.IsSolid\b")
RE_NAKED = re.compile(r"\.GetNakedEdges\b")

# Volume + count checks (C2).
RE_VOLUME = re.compile(r"\bVolumeMassProperties\.Compute\b|\.Volume\b")
RE_COUNT = re.compile(r"\blen\s*\(|\.Count\b")

# Bake + tagging (C1).
RE_ADD = re.compile(r"\.Objects\.Add(?:Brep|Curve|Surface|Mesh|Point)\b")
RE_USERSTRING = re.compile(r"SetUserString\s*\(\s*['\"]part_id['\"]")
RE_NAME = re.compile(r"\.Name\s*=")
RE_LAYER = re.compile(r"\.LayerIndex\s*=")
RE_REDRAW = re.compile(r"\.Views\.Redraw\s*\(")

# Pre-flight heuristics (C7) — any of these signals input pre-flighting.
RE_PREFLIGHT = re.compile(
    r"\.Reverse\s*\(|\.SetStartPoint\s*\(|\.SetEndPoint\s*\(|"
    r"ChangeClosedCurveSeam|TangentAtStart|GetNextDiscontinuity|"
    r"IsClosed\b|IsPlanar\s*\(|preflight"
)


class Finding(object):
    """One contract finding: level in {PASS, WARN, FAIL}, a rule id, a message."""

    def __init__(self, level, rule, message, line=None):
        self.level = level
        self.rule = rule
        self.message = message
        self.line = line

    def render(self):
        loc = (" (line %d)" % self.line) if self.line else ""
        return "[%-4s] %-22s %s%s" % (self.level, self.rule, self.message, loc)


def _strip_strings_and_comments(line):
    """Remove string literals and trailing comments so literal scans don't false-fire."""
    out = []
    i = 0
    n = len(line)
    quote = None
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "#":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def lint(text):
    """Return a list of Finding objects for the snippet text."""
    findings = []
    raw_lines = text.splitlines()
    # code-only view (strings + comments removed) for literal/keyword scans
    code_lines = [_strip_strings_and_comments(ln) for ln in raw_lines]
    code = "\n".join(code_lines)

    # Rule 1: shebang ----------------------------------------------------------
    if raw_lines and RE_SHEBANG.match(raw_lines[0].rstrip()):
        findings.append(Finding("PASS", "1-shebang", "'#! python3' present on line 1"))
    else:
        findings.append(Finding("FAIL", "1-shebang",
                                 "line 1 must be exactly '#! python3'", 1))

    # Rule 2: requirement comments ---------------------------------------------
    bad_req = []
    for idx, ln in enumerate(raw_lines, start=1):
        m = RE_REQUIRE.match(ln.strip())
        if m and m.group("pkg") in ALWAYS_PRESENT:
            bad_req.append((idx, m.group("pkg")))
    if bad_req:
        for idx, pkg in bad_req:
            findings.append(Finding("FAIL", "2-requirements",
                                    "'# r: %s' is wrong; %s is always present" % (pkg, pkg),
                                    idx))
    else:
        findings.append(Finding("PASS", "2-requirements",
                                 "no '# r:' for always-present packages"))

    # Rule 3: tolerances live, not hardcoded -----------------------------------
    live = bool(RE_LIVE_TOL.search(code))
    hard_lines = [i + 1 for i, ln in enumerate(code_lines) if RE_HARDCODED_TOL.search(ln)]
    if hard_lines:
        findings.append(Finding("FAIL", "3-tolerance",
                                 "hardcoded tolerance literal; use "
                                 "sc.doc.ModelAbsoluteTolerance",
                                 hard_lines[0]))
    elif live:
        findings.append(Finding("PASS", "3-tolerance",
                                 "reads sc.doc.ModelAbsoluteTolerance"))
    else:
        findings.append(Finding("WARN", "3-tolerance",
                                 "no tolerance read found; confirm tol is read live "
                                 "if any Create*/Join/boolean is used"))
    if RE_LIVE_ANGTOL.search(code):
        findings.append(Finding("PASS", "3-angle-tol",
                                 "reads sc.doc.ModelAngleToleranceRadians"))

    has_create = bool(RE_CREATE.search(code))
    has_boolean = bool(RE_BOOLEAN.search(code))

    # Rule 4: pre-flight inputs (C7) -------------------------------------------
    if has_create:
        if RE_PREFLIGHT.search(code):
            findings.append(Finding("PASS", "4-preflight",
                                    "input pre-flight signals present (C7)"))
        else:
            findings.append(Finding("WARN", "4-preflight",
                                    "Create* used but no input pre-flight detected "
                                    "(C7: direction/seam/G1/coplanar/closed/planar)"))

    # Rule 5: null/empty check after Create* -----------------------------------
    if has_create:
        guards_none = (" is None" in code) or (" is not None" in code) or ("not results" in code)
        guards_empty = (".Count" in code) or ("len(" in code) or ("if not " in code)
        if guards_none or guards_empty:
            findings.append(Finding("PASS", "5-nullcheck",
                                    "Create* result null/empty-checked"))
        else:
            findings.append(Finding("FAIL", "5-nullcheck",
                                    "Create* result is not null/empty-checked"))

    # Rule 6: IsValid / IsSolid / GetNakedEdges --------------------------------
    if has_create:
        v = bool(RE_ISVALID.search(code))
        s = bool(RE_ISSOLID.search(code))
        nk = bool(RE_NAKED.search(code))
        if v and (s or nk):
            findings.append(Finding("PASS", "6-validity",
                                    "IsValid + IsSolid/GetNakedEdges present"))
        else:
            missing = []
            if not v:
                missing.append("IsValid")
            if not s:
                missing.append("IsSolid")
            if not nk:
                missing.append("GetNakedEdges")
            findings.append(Finding("FAIL", "6-validity",
                                    "missing validity checks: " + ", ".join(missing)))

    # Rule 7: post-boolean count + volume (C2) ---------------------------------
    if has_boolean:
        has_vol = bool(RE_VOLUME.search(code))
        has_count = bool(RE_COUNT.search(code))
        if has_vol and has_count:
            findings.append(Finding("PASS", "7-boolean-c2",
                                    "boolean has expected-count + total-volume check (C2)"))
        else:
            miss = []
            if not has_count:
                miss.append("solid-count")
            if not has_vol:
                miss.append("total-volume (VolumeMassProperties.Compute)")
            findings.append(Finding("FAIL", "7-boolean-c2",
                                    "boolean missing C2 post-check: " + ", ".join(miss)))

    # Rule 8: bake tagging (C1) ------------------------------------------------
    # SetUserString("part_id", ...) is detected against the RAW text on purpose:
    # the "part_id" literal is the ledger key and must survive string-stripping.
    if RE_ADD.search(code):
        us = bool(RE_USERSTRING.search(text))
        nm = bool(RE_NAME.search(code))
        ly = bool(RE_LAYER.search(code))
        if us and nm and ly:
            findings.append(Finding("PASS", "8-bake-tagging",
                                    "Name + SetUserString('part_id') + layer set at bake (C1)"))
        else:
            miss = []
            if not nm:
                miss.append("attr.Name")
            if not us:
                miss.append("SetUserString('part_id', ...)")
            if not ly:
                miss.append("attr.LayerIndex")
            findings.append(Finding("FAIL", "8-bake-tagging",
                                    "bake missing ledger tagging: " + ", ".join(miss)))

    # Rule 9: GUID return + single redraw --------------------------------------
    if RE_ADD.search(code):
        redraws = len(RE_REDRAW.findall(code))
        if redraws == 1:
            findings.append(Finding("PASS", "9-redraw",
                                    "exactly one Views.Redraw() at the end"))
        elif redraws == 0:
            findings.append(Finding("WARN", "9-redraw",
                                    "no Views.Redraw(); add one at the very end"))
        else:
            findings.append(Finding("WARN", "9-redraw",
                                    "%d Views.Redraw() calls; redraw once at the end" % redraws))
        # the Add* return should be captured into a variable (the GUID)
        if re.search(r"=\s*[A-Za-z_][\w.]*\.Objects\.Add", code):
            findings.append(Finding("PASS", "9-guid",
                                    "Add* return captured as the GUID (C1 ledger handle)"))
        else:
            findings.append(Finding("WARN", "9-guid",
                                    "Add* return value not captured; capture the GUID"))

    return findings


GUARD_PREAMBLE = """#! python3
# codegen_guard --wrap preamble: live tolerances, no hardcoded values.
import scriptcontext as sc
import Rhino
tol = sc.doc.ModelAbsoluteTolerance
ang_tol = sc.doc.ModelAngleToleranceRadians
# ---- begin wrapped snippet ----
"""

GUARD_POSTAMBLE = """
# ---- end wrapped snippet ----
# codegen_guard --wrap postamble: single redraw at the end.
sc.doc.Views.Redraw()
"""


def _strip_shebang_and_imports(text):
    """For --wrap: drop a duplicate shebang and the tol/ang_tol reads we re-add."""
    lines = text.splitlines()
    out = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if i == 0 and RE_SHEBANG.match(s):
            continue
        if s.startswith("tol ") and "ModelAbsoluteTolerance" in s:
            continue
        if s.startswith("ang_tol ") and "ModelAngleToleranceRadians" in s:
            continue
        out.append(ln)
    return "\n".join(out).strip("\n")


def wrap(text):
    """Return the snippet bracketed by the guard preamble/postamble."""
    body = _strip_shebang_and_imports(text)
    return GUARD_PREAMBLE + body + "\n" + GUARD_POSTAMBLE


def _read(path):
    f = open(path, "r")
    try:
        return f.read()
    finally:
        f.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Enforce the Rhino codegen contract (shared/conventions.md section 5).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--lint", metavar="PATH",
                       help="lint a generated Rhino python snippet and print a report")
    group.add_argument("--wrap", metavar="PATH",
                       help="emit the snippet bracketed by the guard preamble/postamble")
    args = parser.parse_args(argv)

    if args.wrap:
        try:
            text = _read(args.wrap)
        except (IOError, OSError) as exc:
            sys.stderr.write("error: cannot read %s: %s\n" % (args.wrap, exc))
            return 2
        sys.stdout.write(wrap(text))
        if not wrap(text).endswith("\n"):
            sys.stdout.write("\n")
        return 0

    # --lint
    try:
        text = _read(args.lint)
    except (IOError, OSError) as exc:
        sys.stderr.write("error: cannot read %s: %s\n" % (args.lint, exc))
        return 2

    findings = lint(text)
    n_fail = sum(1 for f in findings if f.level == "FAIL")
    n_warn = sum(1 for f in findings if f.level == "WARN")
    n_pass = sum(1 for f in findings if f.level == "PASS")

    print("codegen_guard report: %s" % args.lint)
    print("-" * 72)
    for f in findings:
        print(f.render())
    print("-" * 72)
    verdict = "FAIL" if n_fail else ("WARN" if n_warn else "PASS")
    print("summary: %d PASS, %d WARN, %d FAIL -> %s" % (n_pass, n_warn, n_fail, verdict))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
