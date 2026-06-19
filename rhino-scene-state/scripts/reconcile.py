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
* EXTRA     : a TAGGED live object whose part_id no declared node claims, or a
              surplus live copy of an already-matched part_id.
* PHANTOM   : an UNTAGGED live object (no part_id UserString). These are the
              leftover / double-baked objects that inflate the document
              object_count (evidence E2: object_count=54 vs 18 real BREPs).
              They are surfaced separately with a delete-untagged repair hint.
* DUPLICATE : one declared part_id resolved to MORE THAN ONE live object -- the
              double/triple-execution signature (evidence E1).
* MIS-SIZED : a matched node whose live bbox span differs from the declared
              dims/bbox span beyond tolerance.
* COUNT     : a boolean/operation result whose realized disjoint-solid count
              diverges from expected_solid_count -- correction C2. The realized
              count is read authoritatively (per-object solid_count/piece_count,
              else the part_id-enumeration count), NEVER from object_count.
* VOLUME    : a matched solid (or a boolean result) whose realized volume
              diverges from the declared volume beyond a volume tolerance --
              correction C2 (partial/silent boolean failure).

MEASUREMENT-TRUTH RULE (E2): the document object_count is NEVER an authoritative
signal. The authoritative live count is the number of distinct, correctly-tagged
part_ids (UserString "part_id"). reconcile reports object_count for transparency
but asserts only on the part_id-keyed diff: MISSING/EXTRA/PHANTOM/DUPLICATE plus
the per-node size/count/volume checks.

Matching is GUID-first; on a GUID miss it falls back to part_id (the
authoritative UserString resolver), then to the object Name (correction C1).

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

def object_part_id(obj):
    """Read the authoritative part_id off a live object.

    Authoritative = the UserString "part_id" (correction C1), surfaced by the
    summary either as a top-level `part_id` field or nested under
    `user_strings`/`userStrings`. Returns the part_id string or None when the
    object carries no part_id tag (an UNTAGGED / phantom object).
    """
    if not isinstance(obj, dict):
        return None
    pid = obj.get("part_id")
    if pid is None:
        user = obj.get("user_strings") or obj.get("userStrings") or {}
        if isinstance(user, dict):
            pid = user.get("part_id")
    if pid is None or str(pid) == "":
        return None
    return str(pid)


def object_stage(obj):
    """Read a live object's "stage" tag (conventions §12), or None if untagged.

    Surfaced by the summary as a top-level `stage` field or nested under
    `user_strings`/`userStrings`, exactly like part_id. Used only to SCOPE a
    per-stage reconcile (--stage); never an identity signal.
    """
    if not isinstance(obj, dict):
        return None
    st = obj.get("stage")
    if st is None:
        user = obj.get("user_strings") or obj.get("userStrings") or {}
        if isinstance(user, dict):
            st = user.get("stage")
    if st is None or str(st) == "":
        return None
    return str(st)


def index_actuals(objects):
    """Build GUID / part_id / name indexes over the actual objects.

    `by_part` maps a part_id to the LIST of live objects carrying that part_id
    (NOT a single object) so the caller can detect DUPLICATE bakes -- correction
    C1 / measurement-truth: object_count is never trusted; the authoritative
    count is the number of distinct, correctly-tagged part_ids.
    """
    by_guid = {}
    by_part = {}   # part_id -> [obj, ...]  (list, to expose duplicates)
    by_name = {}
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        guid = norm_guid(obj.get("guid") or obj.get("id"))
        if guid is not None:
            by_guid[guid] = obj
        pid = object_part_id(obj)
        if pid is not None:
            by_part.setdefault(pid, []).append(obj)
        name = obj.get("name")
        if name:
            by_name.setdefault(str(name), obj)
    return by_guid, by_part, by_name


def match_node(node, by_guid, by_part, by_name):
    """Resolve a declared node to its live object(s).

    GUID-first, then part_id (UserString fallback, the AUTHORITATIVE handle),
    then Name (low confidence). Returns (objects_list, match_method); the list
    has >1 entry only when several live objects share one part_id (a DUPLICATE
    bake, e.g. from a double execution -- evidence E1).
    """
    guid = norm_guid(node.get("guid"))
    if guid is not None and guid in by_guid:
        return [by_guid[guid]], "guid"

    pid = node.get("part_id")
    if pid is not None and str(pid) in by_part:
        return list(by_part[str(pid)]), "part_id"

    name = node.get("name") or pid
    if name is not None and str(name) in by_name:
        return [by_name[str(name)]], "name"

    return [], None


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
# connectivity checkpoint maintenance (conventions §13 / C9)
# --------------------------------------------------------------------------- #
# These two operations keep the per-stage CONNECTIVITY checkpoints honest when a
# stage is re-emitted or deleted. They mutate the scene-graph artifact's
# checkpoints/edges IN PLACE (they do NOT touch the live document) and are the
# C1/C2 half of §13: the DETECT sweep (check_connectivity.py / stage_emit.py)
# measures gaps, but a stage whose neighbour just moved must be RE-MEASURED, and a
# deleted part must turn its now-orphaned contacts into explicit FAILs -- never a
# false pass by omission.

def _stage_of_part_in_graph(part_id, nodes, stages_index):
    """Resolve a part_id's stage from the scene-graph nodes (mirror node.stage).

    For an array instance 'baluster#3' falls back to the family id 'baluster'.
    """
    if part_id is None:
        return None
    sid = stages_index.get(part_id)
    if sid is not None:
        return sid
    fam = str(part_id).split("#", 1)[0]
    return stages_index.get(fam)


def _build_stage_index(nodes):
    """Map part_id -> stage from the scene-graph nodes (the part_id-keyed truth)."""
    idx = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        pid = node.get("part_id") or node.get("name")
        if pid:
            idx[str(pid)] = node.get("stage")
    return idx


def invalidate_crossing_stages(graph, reemitted_stage):
    """C1: re-emitting `reemitted_stage` dirties the connectivity of every stage
    that has a relation CROSSING INTO it.

    Re-baking a stage gives its parts new GUIDs but the SAME part_ids, so any edge
    whose other endpoint lives in `reemitted_stage` may now be stale (the rail
    moved, every baluster is orphaned). Because edges are part_id-keyed (never
    GUID), we find those crossing edges and set connectivity_status='not_run' on
    the OWNING stage's checkpoint (so the §13 sweep MUST re-run before that stage
    is green again), even though that stage's own geometry was untouched.

    Mutates graph['checkpoints'][*] in place. Returns the list of stage ids whose
    connectivity was invalidated (excluding the re-emitted stage itself, which the
    caller re-sweeps anyway).
    """
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    edges = graph.get("edges", []) if isinstance(graph, dict) else []
    checkpoints = graph.get("checkpoints", []) if isinstance(graph, dict) else []
    stages_index = _build_stage_index(nodes)

    crossing = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        # An edge is OWNED by the stage of its 'from' part (the part that DECLARES
        # the contact obligation: the baluster lands_on the rail, so the edge is a
        # baluster_stage obligation). The supports it reaches are 'to'/'to2'.
        owning_stage = _stage_of_part_in_graph(edge.get("from"), nodes, stages_index)
        if owning_stage is None or owning_stage == reemitted_stage:
            # The re-emitted stage re-sweeps its OWN edges anyway; only OTHER
            # stages whose obligations reach INTO it need invalidating.
            continue
        support_stages = [
            _stage_of_part_in_graph(edge.get("to"), nodes, stages_index),
            _stage_of_part_in_graph(edge.get("to2"), nodes, stages_index),
        ]
        # This OTHER stage's contact reaches a part in the re-emitted stage: the
        # re-bake can move that support and orphan the obligation -> invalidate.
        if reemitted_stage in support_stages:
            crossing.add(owning_stage)

    invalidated = []
    for cp in checkpoints:
        if not isinstance(cp, dict):
            continue
        if cp.get("stage") in crossing:
            cp["connectivity_status"] = "not_run"
            invalidated.append(cp.get("stage"))
    return invalidated


def referential_check_on_purge(graph, deleted_stage=None, deleted_part_ids=None):
    """C2: deleting a stage/part flags every edge pointing at a now-deleted
    part_id as UNCOVERED, instead of silently passing by omission.

    A deleted part = no relation to check = a *false pass by omission*. This
    PRE-PURGE referential check enumerates every edge whose 'from'/'to'/'to2'
    references a part_id that is about to vanish (the deleted stage's parts, plus
    any explicit deleted_part_ids), and returns an 'uncovered' connectivity entry
    for each. The caller folds these into the affected checkpoints' connectivity
    list and sets connectivity_status='violations'.

    Returns a list of {edge, status:'uncovered', reason} entries (the §13
    connectivity_entry shape). Does NOT delete anything itself.
    """
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    edges = graph.get("edges", []) if isinstance(graph, dict) else []
    stages_index = _build_stage_index(nodes)

    doomed = set(str(p) for p in (deleted_part_ids or []))
    if deleted_stage is not None:
        for pid, sid in stages_index.items():
            if sid == deleted_stage:
                doomed.add(str(pid))
        # Array instances: a family node 'baluster' in the stage dooms every
        # instance id 'baluster#i' referenced by edges.
    uncovered = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if edge.get("type") in ("child_of", "symmetric_about"):
            continue  # logical relations are not measured contacts (no C2 FAIL)
        refs = []
        for role in ("from", "to", "to2"):
            val = edge.get(role)
            if val is None:
                continue
            fam = str(val).split("#", 1)[0]
            if str(val) in doomed or fam in doomed:
                refs.append((role, val))
        if not refs:
            continue
        ekey = {"type": edge.get("type"), "from": edge.get("from"),
                "to": edge.get("to")}
        if edge.get("to2"):
            ekey["to2"] = edge.get("to2")
        uncovered.append({
            "edge": ekey,
            "status": "uncovered",
            "reason": "edge references deleted part_id(s) %s (C2 pre-purge "
                      "referential check: no relation to check is a FAIL, not a "
                      "pass)" % ", ".join("%s=%s" % (r, v) for r, v in refs),
        })
    return uncovered


# --------------------------------------------------------------------------- #
# core reconcile
# --------------------------------------------------------------------------- #

def reconcile(expected, actual, tol, vtol_ratio, stage=None):
    """Diff declared scene-graph vs. actual document summary.

    When `stage` is given, the diff is SCOPED to one build stage (conventions
    §12): only expected nodes whose `stage` equals it are checked, and live
    objects explicitly tagged with a DIFFERENT stage are dropped so they are not
    reported as EXTRA/PHANTOM. This is what lets a staged re-emit verify just the
    re-built stage of a 931-solid model without the rest registering as defects.
    Untagged live objects are kept (so a scoped reconcile still surfaces a stray
    untagged object that may belong to this stage).

    Returns a report dict with categorized findings and an ok flag.
    """
    nodes = expected.get("nodes", []) if isinstance(expected, dict) else []
    edges = expected.get("edges", []) if isinstance(expected, dict) else []
    objects = as_object_list(actual)

    if stage is not None:
        nodes = [n for n in nodes
                 if isinstance(n, dict) and n.get("stage") == stage]
        objects = [o for o in objects
                   if isinstance(o, dict) and object_stage(o) in (stage, None)]

    by_guid, by_part, by_name = index_actuals(objects)

    # --- AUTHORITATIVE COUNT (measurement-truth / E2) ----------------------- #
    # NEVER trust the raw object_count: get_document_summary has been observed to
    # report 54 objects when only 18 real BREPs existed (phantom/leftover bakes,
    # double execution). The authoritative live count is the number of DISTINCT,
    # correctly-tagged part_ids -- enumerate by UserString "part_id", not by the
    # document object total. `untagged` objects carry no part_id and are exactly
    # the phantoms that inflate object_count.
    tagged_objects = sum(len(v) for v in by_part.values())
    untagged_objects = sum(
        1 for obj in objects
        if isinstance(obj, dict) and object_part_id(obj) is None
    )

    report = {
        "units": expected.get("units") if isinstance(expected, dict) else None,
        "tolerance": tol,
        "declared_nodes": len(nodes),
        # Reported for transparency, but NOT an authoritative signal (E2):
        "actual_objects": len(objects),
        # Authoritative live counts, by part_id enumeration:
        "actual_part_ids": len(by_part),
        "actual_tagged_objects": tagged_objects,
        "actual_untagged_objects": untagged_objects,
        "matched": [],
        "missing": [],
        "extra": [],
        "mis_sized": [],
        "count_mismatch": [],
        "volume_mismatch": [],
        "duplicate": [],   # one declared part_id baked more than once (E1)
        "phantom": [],     # live untagged object: no part_id at all (E2)
        "consumed": [],
    }

    matched_ids = set()  # python id() of matched actual objects, to find EXTRA

    for node in nodes:
        pid = node.get("part_id")
        label = pid or node.get("name") or node.get("guid") or "<unnamed>"

        objs, method = match_node(node, by_guid, by_part, by_name)

        if not objs:
            # A boolean may have legitimately consumed this input (C1).
            if consumed_by_edge(pid, edges):
                report["consumed"].append({"part_id": label})
            else:
                report["missing"].append({
                    "part_id": label,
                    "guid": node.get("guid"),
                })
            continue

        # DUPLICATE (E1): one declared part_id resolved to several live objects.
        # This is the double/triple-execution signature. We check the
        # authoritative part_id index DIRECTLY (not just the matched list) so a
        # GUID-first match that returned a single object still detects a second
        # live object carrying the same part_id UserString.
        dup_objs = objs
        if pid is not None and str(pid) in by_part:
            dup_objs = by_part[str(pid)]
        if len(dup_objs) > 1:
            report["duplicate"].append({
                "part_id": label,
                "count": len(dup_objs),
                "guids": [o.get("guid") or o.get("id") for o in dup_objs],
            })

        # Claim ALL live objects sharing this part_id as matched, so the surplus
        # copies are reported under DUPLICATE rather than double-counted as EXTRA.
        for dobj in dup_objs:
            matched_ids.add(id(dobj))

        # Diff against the FIRST live object for this part_id.
        obj = objs[0]
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
        # The realized count is read AUTHORITATIVELY in this priority:
        #   1. the object's own solid_count/piece_count (disjoint solids INSIDE
        #      one boolean result -- the true C2 partial-union signal), else
        #   2. the number of live objects sharing this part_id (len(objs)).
        # We NEVER derive it from the document object_count (E2). If neither a
        # per-object solid_count nor a duplicate signal is present, the realized
        # count defaults to 1 (one tagged object == one solid) so the check can
        # no longer be silently skipped.
        exp_count = node.get("expected_solid_count")
        if exp_count is not None:
            act_count = obj.get("solid_count")
            if act_count is None:
                act_count = obj.get("piece_count")
            if act_count is None:
                # No per-object solid_count in the summary: fall back to the
                # authoritative part_id enumeration instead of skipping (E2).
                act_count = len(objs)
            try:
                if int(act_count) != int(exp_count):
                    report["count_mismatch"].append({
                        "part_id": label,
                        "expected_solid_count": int(exp_count),
                        "actual_solid_count": int(act_count),
                        "count_source": (
                            "solid_count" if obj.get("solid_count") is not None
                            else "piece_count" if obj.get("piece_count") is not None
                            else "part_id_enumeration"
                        ),
                    })
            except (TypeError, ValueError):
                pass

    # --- EXTRA / PHANTOM: live objects not matched to any declared node ----- #
    # A live object that did not match a node falls into one of two buckets:
    #   * PHANTOM  -- carries NO part_id (untagged). This is the E2 signature:
    #                 the leftover/double-baked objects that inflate object_count
    #                 from 18 to 54. They are never identity-resolvable, so they
    #                 get their own category and a delete-untagged repair hint.
    #   * EXTRA    -- carries a part_id that no declared node claims, OR is a
    #                 surplus live copy of a part_id already matched once (the
    #                 second+ object behind a DUPLICATE).
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if id(obj) in matched_ids:
            continue
        pid = object_part_id(obj)
        entry = {
            "guid": obj.get("guid") or obj.get("id"),
            "name": obj.get("name"),
            "part_id": pid,
        }
        if pid is None:
            report["phantom"].append(entry)
        else:
            report["extra"].append(entry)

    report["ok"] = not (
        report["missing"]
        or report["extra"]
        or report["mis_sized"]
        or report["count_mismatch"]
        or report["volume_mismatch"]
        or report["duplicate"]
        or report["phantom"]
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
    # Authoritative line: count by part_id, NOT by object_count (E2).
    print("declared_nodes=%d  actual_part_ids=%d  matched=%d"
          % (report["declared_nodes"], report.get("actual_part_ids", 0),
             len(report["matched"])))
    print("  (raw object_count=%d  tagged=%d  untagged/phantom=%d  -- object_count NOT trusted)"
          % (report["actual_objects"], report.get("actual_tagged_objects", 0),
             report.get("actual_untagged_objects", 0)))

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
    dump("EXTRA (tagged, undeclared or surplus copy)", report["extra"],
         lambda i: "guid=%s name=%s part_id=%s"
                   % (i.get("guid"), i.get("name"), i.get("part_id")))
    dump("PHANTOM (untagged -- inflates object_count, E2)", report["phantom"],
         lambda i: "guid=%s name=%s  (no part_id; delete-untagged candidate)"
                   % (i.get("guid"), i.get("name")))
    dump("DUPLICATE (one part_id baked >1x, E1)", report["duplicate"],
         lambda i: "%s baked %d times guids=%s"
                   % (i["part_id"], i["count"], i.get("guids")))
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
                    "document summary; exit non-zero on any mismatch. Also runs "
                    "the §13/C9 connectivity-checkpoint maintenance: cross-stage "
                    "invalidation (C1) and the pre-purge referential check (C2).")
    parser.add_argument("--expected", required=True,
                        help="path to the expected scene-graph JSON (from the IR).")
    parser.add_argument("--actual", default=None,
                        help="path to the actual document summary JSON (required "
                             "for the default reconcile mode; not used by the "
                             "--invalidate-stage / --purge-stage modes).")
    parser.add_argument("--tol", type=float, default=None,
                        help="bbox/dimension match tolerance in model units "
                             "(default: the expected scene-graph 'tolerance').")
    parser.add_argument("--vtol-ratio", type=float, default=0.01,
                        help="relative volume tolerance as a fraction of the "
                             "expected volume (default 0.01 = 1%%).")
    parser.add_argument("--stage", default=None,
                        help="scope the diff to one build stage id (conventions "
                             "§12): only nodes/objects tagged with this stage are "
                             "checked, so a re-emitted stage is verified without "
                             "the rest of the model registering as defects.")
    parser.add_argument("--invalidate-stage", default=None,
                        help="C1 cross-stage invalidation MODE: name the stage "
                             "being RE-EMITTED. Sets connectivity_status='not_run' "
                             "on every OTHER stage whose checkpoint has a relation "
                             "crossing into it (edges are part_id-keyed, §13/C1), "
                             "then prints the invalidated stage ids. With --apply "
                             "the mutation is written back to --expected.")
    parser.add_argument("--purge-stage", default=None,
                        help="C2 pre-purge referential check MODE: name the stage "
                             "about to be DELETED. Flags every edge pointing at one "
                             "of its now-deleted part_ids as UNCOVERED (§13/C2) so "
                             "a deleted part becomes an explicit FAIL, never a "
                             "false pass by omission.")
    parser.add_argument("--purge-parts", default=None,
                        help="comma-separated part_ids about to be deleted "
                             "(C2 referential check), in addition to --purge-stage.")
    parser.add_argument("--apply", action="store_true",
                        help="write the mutated scene-graph back to --expected "
                             "(for --invalidate-stage / --purge-stage). Without it "
                             "the modes are read-only and only report.")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON instead of text.")
    args = parser.parse_args(argv)

    expected = load_json(args.expected)

    # --- C1 cross-stage invalidation mode ----------------------------------- #
    if args.invalidate_stage is not None:
        return _run_invalidate(expected, args)

    # --- C2 pre-purge referential check mode -------------------------------- #
    if args.purge_stage is not None or args.purge_parts is not None:
        return _run_purge(expected, args)

    # --- default reconcile mode --------------------------------------------- #
    if args.actual is None:
        parser.error("--actual is required for the default reconcile mode")
    actual = load_json(args.actual)

    tol = args.tol
    if tol is None:
        tol = expected.get("tolerance") if isinstance(expected, dict) else None
    if tol is None:
        tol = 0.01  # safe default when neither flag nor artifact supplies one
    tol = float(tol)

    report = reconcile(expected, actual, tol, float(args.vtol_ratio),
                       stage=args.stage)
    print_report(report, args.json)

    return 0 if report["ok"] else 1


def _write_back(path, graph):
    """Persist a mutated scene-graph to disk atomically (tmp + os.replace).

    Mirrors the conventions §12a ledger-after-geometry discipline: write the JSON
    to a sibling temp file then os.replace it into place so a crash never leaves a
    half-written ledger.
    """
    import os
    import tempfile
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(graph, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _run_invalidate(graph, args):
    """C1: invalidate connectivity of every stage crossing into the re-emitted one."""
    invalidated = invalidate_crossing_stages(graph, args.invalidate_stage)
    if args.apply:
        _write_back(args.expected, graph)
    payload = {
        "mode": "invalidate-stage",
        "reemitted_stage": args.invalidate_stage,
        "invalidated_stages": invalidated,
        "applied": bool(args.apply),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("=== C1 cross-stage connectivity invalidation ===")
        print("re-emitted stage: %s" % args.invalidate_stage)
        if invalidated:
            print("invalidated (connectivity_status -> not_run):")
            for sid in invalidated:
                print("  - %s" % sid)
        else:
            print("no other stage has a relation crossing into this stage.")
        print("applied=%s" % bool(args.apply))
    # Invalidation is an advisory mutation, not a failure: exit 0.
    return 0


def _run_purge(graph, args):
    """C2: flag edges pointing at to-be-deleted part_ids as UNCOVERED."""
    parts = None
    if args.purge_parts:
        parts = [p.strip() for p in args.purge_parts.split(",") if p.strip()]
    uncovered = referential_check_on_purge(
        graph, deleted_stage=args.purge_stage, deleted_part_ids=parts)
    if args.apply:
        # Fold the uncovered entries into every checkpoint whose stage owns one of
        # the orphaned edges, and mark those checkpoints' connectivity violations.
        _apply_purge_uncovered(graph, uncovered)
        _write_back(args.expected, graph)
    payload = {
        "mode": "purge-stage",
        "deleted_stage": args.purge_stage,
        "deleted_parts": parts,
        "uncovered": uncovered,
        "applied": bool(args.apply),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("=== C2 pre-purge referential check ===")
        print("deleting stage=%s parts=%s" % (args.purge_stage, parts))
        if uncovered:
            print("UNCOVERED edges (now-orphaned contacts = FAIL, not a silent pass):")
            for u in uncovered:
                e = u["edge"]
                print("  - %s->%s : %s" % (e.get("from"), e.get("to"),
                                           u.get("reason")))
        else:
            print("no edge references a deleted part_id.")
        print("applied=%s" % bool(args.apply))
    # An orphaned contact is a FAIL: exit non-zero when any uncovered edge exists.
    return 1 if uncovered else 0


def _apply_purge_uncovered(graph, uncovered):
    """Fold C2 uncovered entries into the owning stages' checkpoints in place."""
    if not uncovered:
        return
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    stages_index = _build_stage_index(nodes)
    checkpoints = graph.setdefault("checkpoints", []) if isinstance(graph, dict) else []
    cp_by_stage = {cp.get("stage"): cp for cp in checkpoints
                   if isinstance(cp, dict)}
    for u in uncovered:
        owner = u["edge"].get("from")
        sid = _stage_of_part_in_graph(owner, nodes, stages_index)
        cp = cp_by_stage.get(sid)
        if cp is None:
            continue
        # Persist only the schema-clean connectivity_entry fields (edge, status):
        # the scene-graph 'connectivity_entry' has additionalProperties:false and
        # no 'reason' field, so the human-readable 'reason' stays in the report
        # payload but is stripped here so the written artifact still validates.
        entry = {"edge": u["edge"], "status": u["status"]}
        cp.setdefault("connectivity", []).append(entry)
        cp["connectivity_status"] = "violations"


if __name__ == "__main__":
    sys.exit(main())
