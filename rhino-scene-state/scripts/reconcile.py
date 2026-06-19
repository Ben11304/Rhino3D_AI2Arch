#!/usr/bin/env python3
"""Reconcile the declared scene-graph against an actual Rhino document summary.

This is the realized-vs-expected diff that brackets every mutation in the
rhino-scene-state skill. It externalizes the world model so the pipeline never
has to trust in-context memory of what was baked.

Inputs
------
--expected : a scene-graph JSON artifact (conforms to
             schema/scene-graph.schema.json) whose nodes carry the DECLARED
             part_id/guid + bbox + dims + (optionally) volume / expected_solid_count.
             This is the intent, derived from the build-plan IR.
--actual   : an actual document summary JSON. Either:
               {"objects": [ {guid,name,bbox,volume,part_id?}, ... ]}
             or a bare list [ {guid,name,bbox,volume,part_id?}, ... ].
             bbox may be {"min":[x,y,z],"max":[x,y,z]} or [minx,miny,minz,maxx,maxy,maxz].

What it checks
--------------
* MISSING   : a declared node with no matching live object (and no child_of
              edge to explain consumption by a boolean) -- correction C1.
* EXTRA     : a live object with no declared node.
* MIS-SIZED : a matched node whose live bbox span differs from the declared
              dims/bbox span beyond tolerance.
* COUNT     : a boolean/operation result whose realized disjoint-solid count
              diverges from expected_solid_count -- correction C2.
* VOLUME    : a matched solid (or a boolean result) whose realized volume
              diverges from the declared volume beyond a volume tolerance --
              correction C2 (partial/silent boolean failure).

Matching is GUID-first; on a GUID miss it falls back to part_id, then to the
object Name (the UserString part_id fallback resolver, correction C1).

Exits non-zero on any mismatch so the caller can gate the pipeline and hand off
to rhino-repair. stdlib only; passes `python3 -m py_compile`.
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


def norm_guid(value):
    """Normalize a GUID-ish string for comparison (lowercase, stripped, no braces).

    Returns None for empty / unset GUIDs so they never match by accident.
    """
    if value is None:
        return None
    text = str(value).strip().lower().strip("{}")
    if not text:
        return None
    # Treat the all-zero (unset) GUID as no-guid.
    if text == "00000000-0000-0000-0000-000000000000":
        return None
    return text


def as_object_list(actual):
    """Accept either {'objects': [...]} or a bare [...] for the actual summary."""
    if isinstance(actual, dict):
        objs = actual.get("objects")
        if objs is None:
            objs = actual.get("nodes", [])
        return objs if isinstance(objs, list) else []
    if isinstance(actual, list):
        return actual
    return []


# --------------------------------------------------------------------------- #
# bbox / dimension helpers
# --------------------------------------------------------------------------- #

def parse_bbox(bbox):
    """Return (min_xyz, max_xyz) as two 3-tuples of floats, or None if unparseable.

    Accepts {'min':[x,y,z],'max':[x,y,z]} or a flat [minx,miny,minz,maxx,maxy,maxz].
    """
    if bbox is None:
        return None
    try:
        if isinstance(bbox, dict):
            lo = bbox.get("min")
            hi = bbox.get("max")
            if lo is None or hi is None or len(lo) < 3 or len(hi) < 3:
                return None
            return (
                (float(lo[0]), float(lo[1]), float(lo[2])),
                (float(hi[0]), float(hi[1]), float(hi[2])),
            )
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 6:
            return (
                (float(bbox[0]), float(bbox[1]), float(bbox[2])),
                (float(bbox[3]), float(bbox[4]), float(bbox[5])),
            )
    except (TypeError, ValueError):
        return None
    return None


def bbox_span(bbox):
    """Return (dx, dy, dz) extents of a bbox, or None."""
    parsed = parse_bbox(bbox)
    if parsed is None:
        return None
    lo, hi = parsed
    return (abs(hi[0] - lo[0]), abs(hi[1] - lo[1]), abs(hi[2] - lo[2]))


def declared_span(node):
    """Best declared (dx, dy, dz) for a node: prefer bbox span, else map dims.

    Returns None when neither a bbox nor enough dims are present.
    """
    span = bbox_span(node.get("bbox"))
    if span is not None:
        return span

    dims = node.get("dims") or {}
    # Box-like.
    if all(k in dims for k in ("x", "y", "z")):
        return (float(dims["x"]), float(dims["y"]), float(dims["z"]))
    if all(k in dims for k in ("width", "depth", "height")):
        return (float(dims["width"]), float(dims["depth"]), float(dims["height"]))
    # Cylinder/cone-like: radius+height -> bbox is (2r, 2r, h).
    if "radius" in dims and "height" in dims:
        r = float(dims["radius"])
        return (2.0 * r, 2.0 * r, float(dims["height"]))
    # Sphere.
    if "radius" in dims and "height" not in dims:
        r = float(dims["radius"])
        return (2.0 * r, 2.0 * r, 2.0 * r)
    return None


def span_delta(expected, actual):
    """Max absolute per-axis difference between two (dx,dy,dz) spans."""
    return max(
        abs(expected[0] - actual[0]),
        abs(expected[1] - actual[1]),
        abs(expected[2] - actual[2]),
    )


# --------------------------------------------------------------------------- #
# indexing / matching
# --------------------------------------------------------------------------- #

def index_actuals(objects):
    """Build GUID / part_id / name indexes over the actual objects."""
    by_guid = {}
    by_part = {}
    by_name = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        guid = norm_guid(obj.get("guid") or obj.get("id"))
        if guid is not None:
            by_guid[guid] = obj
        pid = obj.get("part_id")
        if pid is None:
            # UserString fallback: some summaries nest user strings.
            user = obj.get("user_strings") or obj.get("userStrings") or {}
            if isinstance(user, dict):
                pid = user.get("part_id")
        if pid:
            by_part.setdefault(str(pid), obj)
        name = obj.get("name")
        if name:
            by_name.setdefault(str(name), obj)
    return by_guid, by_part, by_name


def match_node(node, by_guid, by_part, by_name):
    """Resolve a declared node to a live object.

    GUID-first, then part_id (UserString fallback), then Name (low confidence).
    Returns (object_or_None, match_method).
    """
    guid = norm_guid(node.get("guid"))
    if guid is not None and guid in by_guid:
        return by_guid[guid], "guid"

    pid = node.get("part_id")
    if pid is not None and str(pid) in by_part:
        return by_part[str(pid)], "part_id"

    name = node.get("name") or pid
    if name is not None and str(name) in by_name:
        return by_name[str(name)], "name"

    return None, None


def consumed_by_edge(part_id, edges):
    """True if a child_of edge explains this node being consumed by a boolean (C1).

    A node that is the 'from' of a child_of edge was consumed into its 'to' parent.
    """
    if not part_id:
        return False
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if edge.get("type") == "child_of" and str(edge.get("from")) == str(part_id):
            return True
    return False


# --------------------------------------------------------------------------- #
# core reconcile
# --------------------------------------------------------------------------- #

def reconcile(expected, actual, tol, vtol_ratio):
    """Diff declared scene-graph vs. actual document summary.

    Returns a report dict with categorized findings and an ok flag.
    """
    nodes = expected.get("nodes", []) if isinstance(expected, dict) else []
    edges = expected.get("edges", []) if isinstance(expected, dict) else []
    objects = as_object_list(actual)

    by_guid, by_part, by_name = index_actuals(objects)

    report = {
        "units": expected.get("units") if isinstance(expected, dict) else None,
        "tolerance": tol,
        "declared_nodes": len(nodes),
        "actual_objects": len(objects),
        "matched": [],
        "missing": [],
        "extra": [],
        "mis_sized": [],
        "count_mismatch": [],
        "volume_mismatch": [],
        "consumed": [],
    }

    matched_ids = set()  # python id() of matched actual objects, to find EXTRA

    for node in nodes:
        pid = node.get("part_id")
        label = pid or node.get("name") or node.get("guid") or "<unnamed>"

        obj, method = match_node(node, by_guid, by_part, by_name)

        if obj is None:
            # A boolean may have legitimately consumed this input (C1).
            if consumed_by_edge(pid, edges):
                report["consumed"].append({"part_id": label})
            else:
                report["missing"].append({
                    "part_id": label,
                    "guid": node.get("guid"),
                })
            continue

        matched_ids.add(id(obj))
        match_entry = {"part_id": label, "matched_by": method}
        report["matched"].append(match_entry)

        # --- MIS-SIZED: compare bbox spans within tolerance ----------------- #
        exp_span = declared_span(node)
        act_span = bbox_span(obj.get("bbox"))
        if exp_span is not None and act_span is not None:
            delta = span_delta(exp_span, act_span)
            if delta > tol:
                report["mis_sized"].append({
                    "part_id": label,
                    "expected_span": [round(v, 6) for v in exp_span],
                    "actual_span": [round(v, 6) for v in act_span],
                    "max_axis_delta": round(delta, 6),
                    "tol": tol,
                })

        # --- VOLUME mismatch (C2): per-part volume divergence --------------- #
        exp_vol = node.get("volume")
        act_vol = obj.get("volume")
        if exp_vol is not None and act_vol is not None:
            try:
                exp_vol_f = float(exp_vol)
                act_vol_f = float(act_vol)
            except (TypeError, ValueError):
                exp_vol_f = act_vol_f = None
            if exp_vol_f is not None and act_vol_f is not None:
                vtol = max(vtol_ratio * abs(exp_vol_f), tol)
                if abs(exp_vol_f - act_vol_f) > vtol:
                    report["volume_mismatch"].append({
                        "part_id": label,
                        "expected_volume": round(exp_vol_f, 6),
                        "actual_volume": round(act_vol_f, 6),
                        "vtol": round(vtol, 6),
                    })

        # --- COUNT mismatch (C2): post-boolean disjoint-solid count --------- #
        exp_count = node.get("expected_solid_count")
        if exp_count is not None:
            act_count = obj.get("solid_count")
            if act_count is None:
                act_count = obj.get("piece_count")
            if act_count is not None:
                try:
                    if int(act_count) != int(exp_count):
                        report["count_mismatch"].append({
                            "part_id": label,
                            "expected_solid_count": int(exp_count),
                            "actual_solid_count": int(act_count),
                        })
                except (TypeError, ValueError):
                    pass

    # --- EXTRA: live objects not matched to any declared node --------------- #
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if id(obj) in matched_ids:
            continue
        report["extra"].append({
            "guid": obj.get("guid") or obj.get("id"),
            "name": obj.get("name"),
            "part_id": obj.get("part_id"),
        })

    report["ok"] = not (
        report["missing"]
        or report["extra"]
        or report["mis_sized"]
        or report["count_mismatch"]
        or report["volume_mismatch"]
    )
    return report


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def print_report(report, as_json):
    """Emit the structured reconcile report to stdout."""
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    print("=== scene-graph reconcile report ===")
    print("units=%s  tolerance=%s" % (report.get("units"), report.get("tolerance")))
    print("declared_nodes=%d  actual_objects=%d  matched=%d"
          % (report["declared_nodes"], report["actual_objects"], len(report["matched"])))

    def dump(title, items, fmt):
        if not items:
            return
        print("\n[%s] %d" % (title, len(items)))
        for item in items:
            print("  - " + fmt(item))

    dump("CONSUMED (by boolean, expected)", report["consumed"],
         lambda i: "%s" % i["part_id"])
    dump("MISSING", report["missing"],
         lambda i: "%s (guid=%s)" % (i["part_id"], i.get("guid")))
    dump("EXTRA", report["extra"],
         lambda i: "guid=%s name=%s part_id=%s"
                   % (i.get("guid"), i.get("name"), i.get("part_id")))
    dump("MIS-SIZED", report["mis_sized"],
         lambda i: "%s expected=%s actual=%s delta=%s (tol=%s)"
                   % (i["part_id"], i["expected_span"], i["actual_span"],
                      i["max_axis_delta"], i["tol"]))
    dump("COUNT mismatch (C2)", report["count_mismatch"],
         lambda i: "%s expected_solids=%d actual_solids=%d"
                   % (i["part_id"], i["expected_solid_count"], i["actual_solid_count"]))
    dump("VOLUME mismatch (C2)", report["volume_mismatch"],
         lambda i: "%s expected_vol=%s actual_vol=%s (vtol=%s)"
                   % (i["part_id"], i["expected_volume"], i["actual_volume"], i["vtol"]))

    print("\nRESULT: %s" % ("OK" if report["ok"] else "MISMATCH"))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile a declared scene-graph against an actual Rhino "
                    "document summary; exit non-zero on any mismatch.")
    parser.add_argument("--expected", required=True,
                        help="path to the expected scene-graph JSON (from the IR).")
    parser.add_argument("--actual", required=True,
                        help="path to the actual document summary JSON.")
    parser.add_argument("--tol", type=float, default=None,
                        help="bbox/dimension match tolerance in model units "
                             "(default: the expected scene-graph 'tolerance').")
    parser.add_argument("--vtol-ratio", type=float, default=0.01,
                        help="relative volume tolerance as a fraction of the "
                             "expected volume (default 0.01 = 1%%).")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON instead of text.")
    args = parser.parse_args(argv)

    expected = load_json(args.expected)
    actual = load_json(args.actual)

    tol = args.tol
    if tol is None:
        tol = expected.get("tolerance") if isinstance(expected, dict) else None
    if tol is None:
        tol = 0.01  # safe default when neither flag nor artifact supplies one
    tol = float(tol)

    report = reconcile(expected, actual, tol, float(args.vtol_ratio))
    print_report(report, args.json)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
