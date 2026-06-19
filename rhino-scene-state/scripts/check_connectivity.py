#!/usr/bin/env python3
"""Detect + enforce inter-part CONNECTIVITY (correction C9 / conventions §13).

This is the #1 fix from the v2 hardening session. §1-§12 prove each part is
individually well-formed (valid, solid, right count, right volume) but say
NOTHING about whether parts actually TOUCH where they must. The dominant
observed failure was FALSE CONFIDENCE: the framework declared success while
balusters never reached a rising helical handrail, columns floated above the
floor, and arches did not seat on column tops -- a human caught every one by eye.
check_connectivity turns every declared contact relation into a MEASURED NUMERIC
OBLIGATION a stage cannot pass without satisfying.

The triad (defense in depth, strict order)
-------------------------------------------
  PREVENT  (Phase 3) value_ref/support resolves the attach literal at codegen.
  DETECT   (Phase 5/6, THIS script) measures the realized gap and classifies it.
  ENFORCE  (the completeness clause) UNCOVERED = FAIL.
Detection does NOT depend on the resolver: it consumes a gap JSON produced
IN-RHINO by measuring two LIVE solids by GUID, so it stands alone even if PREVENT
was wrong.

What this script is (and is NOT)
--------------------------------
It does NOT itself call Rhino. The realized gap between two live solids can only
be measured server-side via execute_rhinoscript_python_code (Brep.ClosestPoint /
Brep MinDistanceBetween read live by GUID -- A1). stage_emit.py emits that sweep;
its compact JSON output is the --actual input here. This script is the OFFLINE
classifier + enforcer: given the DECLARED relations (--expected, a build-plan IR
or a scene-graph) and the MEASURED gaps (--actual), it
  * enumerates every declared CONTACT relation (B3 stage scope aware),
  * applies the PER-RELATION-TYPE tolerance band (A3, a directed band, never one
    symmetric +/-tol),
  * classifies each edge pass / out_of_band / uncovered,
  * enforces the COMPLETENESS CLAUSE: every NON-floating assembly part must own
    >=1 declared+measured contact; floating:true parts are exempt (F),
  * samples symmetric array families on a re-check (B2),
  * exits NON-ZERO on any out_of_band or uncovered so the caller gates the stage.

NON-NEGOTIABLE rules honored here (conventions §13)
---------------------------------------------------
A1 NO CIRCULAR PROBE: the gap is measured BETWEEN TWO LIVE SOLIDS by GUID upstream
   (in --actual.measured_between), NEVER against an IR coordinate. This classifier
   only trusts an --actual entry that carries measured_between=[guidA,guidB]; a
   declared edge with no such measurement is UNCOVERED, not silently passed.
A2 ORIENTED HANDLE: world-AABB is UNSOUND for helical/rotated parts. This script
   NEVER derives a gap from a node bbox; the gap is read only from --actual (a
   realized solid-to-solid measurement). obb/centroid are oriented handles for the
   in-Rhino sweep; here they only confirm a node exists / is non-axis-aligned.
A3 PER-RELATION-TYPE BAND: see band_for().
A4 CURVED SUPPORTS: at_surface='realized' => the upstream gap is the nearest point
   on B's realized solid; nothing here re-derives it from a face label.
B2 SAMPLE families: --recheck measures first/middle/last + flagged of an array
   family; full N only when the array rule changed (--array-rule-changed).
B3 STAGE SCOPE: --stage evaluates only relations whose endpoints are in the
   current/closed stages (mirror reconcile.py --stage).
F  FLOATING OPT-OUT: floating:true parts are exempt from completeness.

stdlib only; Python 3.9-compatible; passes `python3 -m py_compile`.
"""

import argparse
import json
import sys


# --------------------------------------------------------------------------- #
# IO helpers
# --------------------------------------------------------------------------- #

def load_json(path):
    """Load a JSON file, raising a clear error on failure."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise SystemExit("FATAL: could not read JSON from %r: %s" % (path, exc))


# Relation types that are MEASURED contact obligations (participate in §13/A3 and
# in the completeness clause). symmetric_about / child_of are LOGICAL relations,
# not measured contacts, so they neither get a band nor satisfy completeness.
CONTACT_TYPES = frozenset(
    ("on_top_of", "coincident", "lands_on", "meets",
     "interpenetrate", "spans", "spans_between")
)
LOGICAL_TYPES = frozenset(("symmetric_about", "child_of"))


# --------------------------------------------------------------------------- #
# expected-side normalization: accept a build-plan IR OR a scene-graph
# --------------------------------------------------------------------------- #

def array_count(part):
    """Return the family size N for a part with an 'array' block, else 1.

    A part with array.count = N stands for N instances baked as part_id
    '<id>#<i>' (0-based). One IR part = one family; each instance is a node.
    """
    arr = part.get("array") if isinstance(part, dict) else None
    if isinstance(arr, dict):
        try:
            n = int(arr.get("count", 1))
            return n if n >= 1 else 1
        except (TypeError, ValueError):
            return 1
    return 1


def _stage_of_part(part, stages_by_part):
    """Resolve a part's stage: explicit stages[] mapping wins, else inline field."""
    pid = part.get("id")
    if pid in stages_by_part:
        return stages_by_part[pid]
    return part.get("stage")


def normalize_expected(expected):
    """Flatten either a build-plan IR or a scene-graph into a common shape.

    Returns (parts_index, edges) where:
      * parts_index maps a concrete part_id (instance id for arrays, e.g.
        'baluster#3') -> {"floating": bool, "stage": str|None, "is_family": bool,
        "family_id": str, "instance": int|None}.
      * edges is a list of normalized edge dicts:
        {"type","from","to","to2","at_surface","tol","floating","penetration","stage"}.

    Edges are keyed by part_id ONLY (C1). For a build-plan IR, per-part relations
    are lifted to edges with from = the owning part ('this' or part.id). For a
    scene-graph, the edges[] array is used directly. Array families are expanded
    to concrete instance ids so completeness + sampling can reason per instance.
    """
    parts_index = {}
    edges = []

    # ---- build-plan IR path -------------------------------------------------- #
    if isinstance(expected, dict) and "parts" in expected:
        parts = expected.get("parts") or []

        # stages[] (if present) is authoritative over the inline part.stage.
        stages_by_part = {}
        for st in (expected.get("stages") or []):
            if not isinstance(st, dict):
                continue
            sid = st.get("id")
            for pid in (st.get("parts") or []):
                stages_by_part[pid] = sid

        for part in parts:
            if not isinstance(part, dict):
                continue
            pid = part.get("id")
            if not pid:
                continue
            stage = _stage_of_part(part, stages_by_part)
            floating = bool(part.get("floating", False))
            n = array_count(part)
            if n > 1:
                for i in range(n):
                    iid = "%s#%d" % (pid, i)
                    parts_index[iid] = {
                        "floating": floating, "stage": stage,
                        "is_family": True, "family_id": pid, "instance": i,
                    }
            else:
                parts_index[pid] = {
                    "floating": floating, "stage": stage,
                    "is_family": False, "family_id": pid, "instance": None,
                }

            # Lift this part's relations to part_id-keyed edges. For an array
            # family the relation fans out across every instance: each
            # 'baluster#i' lands_on the rail, so the sweep measures N gaps and
            # completeness pressures every instance (the from is the instance id,
            # never the family id). An explicit 'this' naming a single instance
            # overrides the fan-out.
            if n > 1:
                from_ids = ["%s#%d" % (pid, i) for i in range(n)]
            else:
                from_ids = [pid]
            for rel in (part.get("relations") or []):
                if not isinstance(rel, dict):
                    continue
                rtype = rel.get("type")
                explicit_this = rel.get("this")
                targets = [explicit_this] if explicit_this else from_ids
                for frm in targets:
                    edges.append({
                        "type": rtype,
                        "from": frm,
                        "to": rel.get("to"),
                        "to2": rel.get("to2"),
                        "at_surface": rel.get("at_surface"),
                        "tol": rel.get("tol"),
                        "floating": floating,
                        "penetration": rel.get("penetration"),
                        "stage": stage,
                    })
        return parts_index, edges

    # ---- scene-graph path ---------------------------------------------------- #
    # In the scene-graph schema the floating opt-out (F) lives on the EDGE
    # (edge.floating), NOT the node (node additionalProperties:false rejects a
    # node-level 'floating'). So a part's floating status is DERIVED: a part is
    # floating when it owns (is the 'from' of) a floating edge. The IR path above
    # reads part.floating directly (valid there).
    raw_edges = expected.get("edges", []) if isinstance(expected, dict) else []
    nodes = expected.get("nodes", []) if isinstance(expected, dict) else []
    node_stage = {}

    # First pass over edges: which parts own a floating edge?
    floating_parts = set()
    for edge in raw_edges:
        if isinstance(edge, dict) and edge.get("floating"):
            if edge.get("from"):
                floating_parts.add(edge.get("from"))

    for node in nodes:
        if not isinstance(node, dict):
            continue
        pid = node.get("part_id") or node.get("name")
        if not pid:
            continue
        node_stage[pid] = node.get("stage")
        parts_index[pid] = {
            "floating": pid in floating_parts,
            "stage": node.get("stage"),
            "is_family": "#" in str(pid),
            "family_id": str(pid).split("#", 1)[0],
            "instance": (int(str(pid).split("#", 1)[1])
                         if "#" in str(pid)
                         and str(pid).split("#", 1)[1].isdigit() else None),
        }

    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        frm = edge.get("from")
        # An edge's stage = the stage of its 'from' endpoint (the owning part).
        stage = node_stage.get(frm)
        edges.append({
            "type": edge.get("type"),
            "from": frm,
            "to": edge.get("to"),
            "to2": edge.get("to2"),
            "at_surface": edge.get("at_surface"),
            "tol": edge.get("tol"),
            "floating": bool(edge.get("floating", False)),
            "penetration": edge.get("penetration"),
            "stage": stage,
        })
    return parts_index, edges


# --------------------------------------------------------------------------- #
# actual-side normalization: the connectivity gap report
# --------------------------------------------------------------------------- #

def edge_key(edge):
    """Canonical part_id-keyed key for an edge (C1: NEVER GUID).

    (type, from, to, to2). 'to' may be None for a completeness UNCOVERED record.
    """
    return (
        edge.get("type"),
        edge.get("from"),
        edge.get("to"),
        edge.get("to2"),
    )


def index_measurements(actual):
    """Index the --actual connectivity report by edge key.

    Accepts any of:
      * a bare list  [ {edge:{from,to,type,to2?}, gap, measured_between}, ... ]
      * {"measurements": [...]} or {"connectivity": [...]} or {"entries": [...]}
      * {"violations": [...]} (the B1 in-Rhino sweep returns ONLY violations;
        absence of an edge then means 'measured + passed', NOT 'uncovered' --
        see classify()).
    Each measurement carries the realized signed gap and measured_between=[guidA,
    guidB] (A1). Returns (by_key, violations_only) where violations_only is True
    when the report is the B1 violations-only shape (so a missing edge is a PASS).
    """
    rows = None
    violations_only = False
    if isinstance(actual, list):
        rows = actual
    elif isinstance(actual, dict):
        for field in ("measurements", "connectivity", "entries", "gaps"):
            if isinstance(actual.get(field), list):
                rows = actual[field]
                break
        if rows is None and isinstance(actual.get("violations"), list):
            rows = actual["violations"]
            violations_only = True
        if rows is None:
            rows = []
    else:
        rows = []

    by_key = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        edge = row.get("edge") if isinstance(row.get("edge"), dict) else row
        key = edge_key(edge)
        by_key[key] = {
            "gap": row.get("gap"),
            "measured_between": row.get("measured_between"),
            "status": row.get("status"),  # may be pre-set by the in-Rhino sweep
            "band": row.get("band"),
        }
    return by_key, violations_only


# --------------------------------------------------------------------------- #
# A3 per-relation-type tolerance band
# --------------------------------------------------------------------------- #

def _num(value, default=None):
    """Coerce a value to float; value_ref objects are unresolved -> default."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        # A value_ref dict that was never resolved to a literal: not usable here.
        return default


def band_for(edge, doc_tol, default_penetration):
    """Return (lo, hi) acceptance band on the SIGNED gap for this edge (A3).

    The band is DIRECTED, never one symmetric +/-tol:
      on_top_of      => [0, +tol]                rests on surface; penetration FAILS
      coincident     => [-tol, +tol]             faces flush
      lands_on       => [-penetration, +tol]     base reaches support, may seat in
      meets          => [-penetration, +tol]     two ends abut
      interpenetrate => [-2, -0.5] (mm; C3)      union overlap must be NEGATIVE
      spans          => [-penetration, +tol]     bridges at both ends (per endpoint)
      spans_between  => [-penetration, +tol]     supported at to AND to2 (per endpt)

    tol is the per-edge override when present, else doc_tol. penetration is the
    per-edge value when present, else default_penetration.
    """
    rtype = edge.get("type")
    tol = _num(edge.get("tol"), doc_tol)
    if tol is None:
        tol = doc_tol
    pen = _num(edge.get("penetration"), default_penetration)
    if pen is None or pen <= 0:
        pen = default_penetration

    if rtype == "on_top_of":
        return (0.0, tol)
    if rtype == "coincident":
        return (-tol, tol)
    if rtype in ("lands_on", "meets", "spans", "spans_between"):
        return (-pen, tol)
    if rtype == "interpenetrate":
        # C3: the realized overlap MUST be negative 0.5-2 mm. A coincident or
        # positive gap here means the union join will be degenerate/empty.
        return (-2.0, -0.5)
    # Logical relations have no measured band.
    return (None, None)


# --------------------------------------------------------------------------- #
# B3 stage scope
# --------------------------------------------------------------------------- #

def stage_in_scope(stage, scope_stage, closed_stages):
    """True when an edge tagged `stage` is in the --stage scope (B3).

    An edge is evaluated only if its (from-endpoint) stage is the current stage
    or one of the already-closed stages. An edge into a not-yet-built stage is
    DEFERRED (not failed). When no --stage is given, everything is in scope.
    """
    if scope_stage is None:
        return True
    if stage is None:
        # Untagged edges (implicit 'default' stage) are always in scope so a
        # scoped sweep still checks default-stage relations touching this stage.
        return True
    if stage == scope_stage:
        return True
    if closed_stages and stage in closed_stages:
        return True
    return False


# --------------------------------------------------------------------------- #
# B2 array-family sampling
# --------------------------------------------------------------------------- #

def family_sample_indices(count):
    """Indices to MEASURE on a re-check for an N-member family (B2).

    first / middle / last. (Plus any previously-flagged member, added by the
    caller via --flagged.) Full N is only re-measured when the array rule changed
    (handled by --array-rule-changed, which disables sampling entirely).
    """
    if count <= 0:
        return set()
    if count <= 3:
        return set(range(count))
    return {0, count // 2, count - 1}


def family_instance_in_sample(part_id, parts_index, flagged):
    """For a re-check (B2): is this family-instance part_id in the sample set?

    Non-family parts are always 'in sample'. A flagged instance id is always kept.
    """
    info = parts_index.get(part_id, {})
    if not info.get("is_family"):
        return True
    if part_id in flagged:
        return True
    fam = info.get("family_id")
    inst = info.get("instance")
    if inst is None:
        return True
    # Count the family size from parts_index.
    count = sum(
        1 for k, v in parts_index.items()
        if v.get("is_family") and v.get("family_id") == fam
    )
    return inst in family_sample_indices(count)


# --------------------------------------------------------------------------- #
# core classification + completeness
# --------------------------------------------------------------------------- #

def classify(expected, actual, doc_tol, default_penetration, stage=None,
             closed_stages=None, recheck=False, array_rule_changed=False,
             flagged=None):
    """Classify every declared contact edge pass/out_of_band/uncovered (§13).

    Returns a report dict. `ok` is True ONLY when there are zero out_of_band and
    zero uncovered entries -- the COMPLETENESS CLAUSE that makes "declare success
    while gaps remain" impossible.
    """
    closed_stages = set(closed_stages or [])
    flagged = set(flagged or [])
    parts_index, edges = normalize_expected(expected)
    by_key, violations_only = index_measurements(actual)

    report = {
        "stage": stage,
        "doc_tol": doc_tol,
        "default_penetration": default_penetration,
        "recheck": recheck,
        "array_rule_changed": array_rule_changed,
        "evaluated": 0,
        "deferred": 0,
        "skipped_sampled_out": 0,
        "passes": [],          # informational; not returned by the in-Rhino sweep
        "out_of_band": [],
        "uncovered": [],
    }

    # Track which NON-floating in-scope parts gained >=1 measured contact, for the
    # completeness clause. A part PARTICIPATES if it is the 'from' or 'to' of any
    # declared contact edge in scope; only PARTICIPATING non-floating parts are
    # held to the >=1-measured-contact rule (a part with no relations at all is
    # not "in an assembly" by this signal and is not pressured).
    participating = set()
    measured_contact = set()

    for edge in edges:
        rtype = edge.get("type")
        # Logical relations are not measured contacts; skip band + completeness.
        if rtype in LOGICAL_TYPES:
            continue
        if rtype not in CONTACT_TYPES:
            # Unknown / non-contact type: ignore (forward-compatible).
            continue

        frm = edge.get("from")
        to = edge.get("to")
        e_stage = edge.get("stage")

        # F FLOATING OPT-OUT: a floating edge is informational. It generates no
        # UNCOVERED pressure (an unmeasured floating contact is NOT a fail) and no
        # out_of_band FAIL (a floating part is INTENDED to have a gap). It also
        # does NOT credit completeness (a floating part is not required to own a
        # measured contact; see the completeness loop's floating skip). Drop it.
        if edge.get("floating") or parts_index.get(frm, {}).get("floating"):
            continue

        # B3 stage scope: only evaluate edges whose owning-endpoint stage is the
        # current or a closed stage. Edges into not-yet-built stages are deferred.
        if not stage_in_scope(e_stage, stage, closed_stages):
            report["deferred"] += 1
            continue

        # B2 family sampling: on a re-check (and unless the array rule changed),
        # only measure a sample of a family's instances. A sampled-out instance
        # is NOT evaluated AND NOT pressured for completeness this round -- so its
        # 'from' is excluded from `participating` (the skip happens BEFORE the
        # participation tracking below). The support side ('to') still
        # participates via the instances that ARE in the sample.
        if recheck and not array_rule_changed:
            if not family_instance_in_sample(frm, parts_index, flagged):
                report["skipped_sampled_out"] += 1
                continue

        # Both endpoints participate (the part is "in an assembly").
        if frm:
            participating.add(frm)
        if to:
            participating.add(to)
        if edge.get("to2"):
            participating.add(edge.get("to2"))

        lo, hi = band_for(edge, doc_tol, default_penetration)
        ekey = {"type": rtype, "from": frm, "to": to}
        if edge.get("to2"):
            ekey["to2"] = edge.get("to2")

        meas = by_key.get(edge_key(edge))

        # --- UNCOVERED: a declared contact with NO realized measurement -------- #
        # A1: a measurement is only trusted if it carries measured_between=[guidA,
        # guidB] (proof it was solid-to-solid against the document) AND a numeric
        # gap. In the B1 violations-only report a MISSING edge means 'measured +
        # passed' (the sweep already proved it in-Rhino), so do NOT flag it
        # uncovered -- but still credit the contact toward completeness.
        if meas is None:
            if violations_only:
                # Passed in-Rhino; not echoed. Credit the contact, no violation.
                if frm:
                    measured_contact.add(frm)
                if to:
                    measured_contact.add(to)
                continue
            report["uncovered"].append({
                "edge": ekey,
                "status": "uncovered",
                "reason": "declared contact has no realized measurement "
                          "(no measured_between gap in the --actual report)",
            })
            continue

        # An explicit pre-classified 'uncovered' from the in-Rhino sweep (e.g. C2
        # endpoint deleted/never built): honor it directly.
        if meas.get("status") == "uncovered":
            report["uncovered"].append({
                "edge": ekey,
                "status": "uncovered",
                "reason": "endpoint not live (deleted/never built) per the sweep",
            })
            continue

        gap = _num(meas.get("gap"))
        mb = meas.get("measured_between")
        # A1 enforcement: a contact 'measurement' with no two-GUID proof is not a
        # realized solid-to-solid measurement -> it is UNCOVERED, never a pass.
        if gap is None or not (isinstance(mb, (list, tuple)) and len(mb) == 2):
            report["uncovered"].append({
                "edge": ekey,
                "status": "uncovered",
                "reason": "measurement lacks a numeric gap and two-GUID "
                          "measured_between proof (A1: not solid-to-solid)",
            })
            continue

        report["evaluated"] += 1

        # This edge has a real realized measurement -> the parts have a measured
        # contact (credit BOTH endpoints toward completeness even if out_of_band:
        # the obligation was measured, the value is just wrong = out_of_band FAIL,
        # not uncovered).
        if frm:
            measured_contact.add(frm)
        if to:
            measured_contact.add(to)

        # --- band check (A3) --------------------------------------------------- #
        if lo is not None and hi is not None and not (lo - 1e-9 <= gap <= hi + 1e-9):
            report["out_of_band"].append({
                "edge": ekey,
                "gap": round(gap, 6),
                "band": [round(lo, 6), round(hi, 6)],
                "measured_between": list(mb),
                "status": "out_of_band",
            })
        else:
            report["passes"].append({
                "edge": ekey,
                "gap": round(gap, 6),
                "band": [round(lo, 6), round(hi, 6)] if lo is not None else None,
                "measured_between": list(mb),
                "status": "pass",
            })

    # --- COMPLETENESS CLAUSE (ENFORCE) --------------------------------------- #
    # Every NON-floating participating part must own >=1 declared+measured
    # contact. A participating part with NO measured contact is UNCOVERED=FAIL.
    # floating:true parts are EXEMPT (F). On a re-check, parts whose only edges
    # were sampled out are not pressured (they are not in `participating`).
    for pid in sorted(participating):
        info = parts_index.get(pid, {})
        if info.get("floating"):
            continue
        # Only pressure parts that are themselves in the stage scope.
        if not stage_in_scope(info.get("stage"), stage, closed_stages):
            continue
        if pid not in measured_contact:
            report["uncovered"].append({
                "edge": {"from": pid, "to": None},
                "status": "uncovered",
                "reason": "non-floating assembly part has no declared+measured "
                          "contact (completeness clause)",
            })

    report["ok"] = not (report["out_of_band"] or report["uncovered"])
    report["connectivity_status"] = "green" if report["ok"] else "violations"
    return report


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def print_report(report, as_json):
    """Emit the connectivity classification to stdout."""
    if as_json:
        # Mirror the B1 in-Rhino sweep shape on stdout: violations only, plus a
        # compact roll-up. passes[] are dropped from the JSON (never enter context
        # per B1) but kept in the dict for programmatic callers that asked for it.
        out = {
            "stage": report.get("stage"),
            "connectivity_status": report.get("connectivity_status"),
            "ok": report.get("ok"),
            "evaluated": report.get("evaluated"),
            "deferred": report.get("deferred"),
            "skipped_sampled_out": report.get("skipped_sampled_out"),
            "out_of_band": report.get("out_of_band"),
            "uncovered": report.get("uncovered"),
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    print("=== connectivity check (C9 / §13) ===")
    print("stage=%s  doc_tol=%s  default_penetration=%s"
          % (report.get("stage"), report.get("doc_tol"),
             report.get("default_penetration")))
    print("evaluated=%d  deferred(out-of-scope)=%d  sampled_out=%d  passed=%d"
          % (report.get("evaluated", 0), report.get("deferred", 0),
             report.get("skipped_sampled_out", 0), len(report.get("passes", []))))

    def dump(title, items, fmt):
        if not items:
            return
        print("\n[%s] %d" % (title, len(items)))
        for item in items:
            print("  - " + fmt(item))

    dump("OUT_OF_BAND (measured, outside the A3 band = FAIL)",
         report["out_of_band"],
         lambda i: "%s %s->%s gap=%s band=%s"
                   % (i["edge"].get("type"), i["edge"].get("from"),
                      i["edge"].get("to"), i.get("gap"), i.get("band")))
    dump("UNCOVERED (no measured contact / deleted endpoint = FAIL)",
         report["uncovered"],
         lambda i: "%s->%s : %s"
                   % (i["edge"].get("from"), i["edge"].get("to"),
                      i.get("reason", "uncovered")))

    print("\nRESULT: %s  (connectivity_status=%s)"
          % ("GREEN" if report["ok"] else "VIOLATIONS",
             report.get("connectivity_status")))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Detect + enforce inter-part connectivity (C9 / conventions "
                    "§13): classify declared contact relations against realized "
                    "gaps measured in-Rhino; exit non-zero on any out_of_band or "
                    "uncovered.")
    parser.add_argument("--expected", required=True,
                        help="path to the DECLARED relations: a build-plan IR "
                             "(parts[].relations) OR a scene-graph (edges[]).")
    parser.add_argument("--actual", required=True,
                        help="path to the connectivity gap report produced "
                             "IN-RHINO by the per-stage sweep (stage_emit.py): "
                             "a list of {edge:{from,to,type}, gap, "
                             "measured_between:[guidA,guidB]} (A1), or the "
                             "{'violations':[...]} B1 shape.")
    parser.add_argument("--tol", type=float, default=None,
                        help="document acceptance tolerance in model units "
                             "(default: the expected artifact 'tolerance').")
    parser.add_argument("--penetration", type=float, default=2.0,
                        help="default allowed negative-gap floor (seating depth) "
                             "for lands_on/meets/spans when an edge omits its own "
                             "penetration; in model units (default 2.0).")
    parser.add_argument("--stage", default=None,
                        help="scope the check to one build stage (B3): only "
                             "relations whose endpoints are in the current or "
                             "closed stages are evaluated; edges into not-yet-"
                             "built stages are deferred, not failed.")
    parser.add_argument("--closed-stages", default=None,
                        help="comma-separated stage ids already baked+closed "
                             "(their endpoints count as in-scope for --stage).")
    parser.add_argument("--recheck", action="store_true",
                        help="re-emit checkpoint mode (B2): sample array families "
                             "(first/middle/last + --flagged) instead of all N.")
    parser.add_argument("--array-rule-changed", action="store_true",
                        help="the generating array rule changed: re-measure the "
                             "FULL N of every family (disables --recheck sampling).")
    parser.add_argument("--flagged", default=None,
                        help="comma-separated family-instance part_ids "
                             "(e.g. 'baluster#7') previously flagged; always "
                             "measured on a --recheck.")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON (violations-only roll-up, "
                             "the B1 shape) instead of text.")
    args = parser.parse_args(argv)

    expected = load_json(args.expected)
    actual = load_json(args.actual)

    tol = args.tol
    if tol is None and isinstance(expected, dict):
        tol = expected.get("tolerance")
    if tol is None:
        tol = 0.01
    tol = float(tol)

    closed = None
    if args.closed_stages:
        closed = [s.strip() for s in args.closed_stages.split(",") if s.strip()]
    flagged = None
    if args.flagged:
        flagged = [s.strip() for s in args.flagged.split(",") if s.strip()]

    report = classify(
        expected, actual, tol, float(args.penetration),
        stage=args.stage, closed_stages=closed,
        recheck=args.recheck, array_rule_changed=args.array_rule_changed,
        flagged=flagged,
    )
    print_report(report, args.json)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
