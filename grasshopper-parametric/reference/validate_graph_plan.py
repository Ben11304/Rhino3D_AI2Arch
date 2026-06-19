#! python3
"""Pre-flight validator for a Grasshopper graph-plan JSON.

Stdlib-only (json/sys/argparse). NO third-party deps. Run BEFORE gh_build_graph:

    python3 ${CLAUDE_SKILL_DIR}/reference/validate_graph_plan.py path/to/plan.json

It checks the structural invariants this skill enforces (see SKILL.md and
reference/gh-wiring.md) so the LLM does not hand gh_build_graph a plan that will
mis-wire or produce a dead slider:

  1. Every connection's `from`/`to` references a DECLARED component and a non-empty
     port name (catches dangling ports and typo'd component ids).
  2. Every Number Slider has a real min/max/value (NOT the broken default 0..1 with no
     value) and min < max and value in [min, max].
  3. Slider authoring ORDER is recorded and contiguous (0, 1, 2, then upward) so the
     "first slider -> input A" convention (gh-wiring §4.2) is honored, and every
     slider in `sliders_table` maps to a real connection target.
  4. No duplicate component ids.

Only its summary (and any FAIL lines) prints to stdout, so only that enters context.
Exit code 0 = plan is structurally sound; 1 = at least one FAIL; 2 = bad input file.
"""

import argparse
import json
import sys

SLIDER_TYPES = {"Number Slider", "MD Slider", "Graph Mapper"}


def _err(msgs, text):
    msgs.append("FAIL: " + text)


def _split_endpoint(endpoint):
    """Split 'component_id.port' into (component_id, port). Tolerates ids without a dot."""
    if not isinstance(endpoint, str) or not endpoint:
        return None, None
    if "." not in endpoint:
        return endpoint, ""
    cid, port = endpoint.rsplit(".", 1)
    return cid, port


def validate(plan):
    """Return a list of FAIL strings (empty list == valid)."""
    fails = []

    if not isinstance(plan, dict):
        return ["FAIL: top-level plan is not a JSON object"]

    components = plan.get("components")
    if not isinstance(components, list) or not components:
        return ["FAIL: plan.components must be a non-empty array"]

    connections = plan.get("connections", [])
    if not isinstance(connections, list):
        _err(fails, "plan.connections must be an array")
        connections = []

    sliders_table = plan.get("sliders_table", [])
    if not isinstance(sliders_table, list):
        _err(fails, "plan.sliders_table must be an array")
        sliders_table = []

    # --- index components, detect duplicate ids -------------------------------
    by_id = {}
    for i, comp in enumerate(components):
        if not isinstance(comp, dict):
            _err(fails, "components[%d] is not an object" % i)
            continue
        cid = comp.get("id")
        ctype = comp.get("type")
        if not isinstance(cid, str) or not cid:
            _err(fails, "components[%d] missing non-empty 'id'" % i)
            continue
        if not isinstance(ctype, str) or not ctype:
            _err(fails, "component '%s' missing non-empty 'type'" % cid)
        if cid in by_id:
            _err(fails, "duplicate component id '%s'" % cid)
        by_id[cid] = comp

    # --- 1. connections reference declared components + real ports ------------
    for i, conn in enumerate(connections):
        if not isinstance(conn, dict):
            _err(fails, "connections[%d] is not an object" % i)
            continue
        src, dst = conn.get("from"), conn.get("to")
        scid, sport = _split_endpoint(src)
        dcid, dport = _split_endpoint(dst)
        if scid is None:
            _err(fails, "connections[%d].from is missing or not 'id.port'" % i)
        else:
            if scid not in by_id:
                _err(fails, "connections[%d].from references unknown component '%s'" % (i, scid))
            if not sport:
                _err(fails, "connections[%d].from '%s' has an empty output port" % (i, src))
        if dcid is None:
            _err(fails, "connections[%d].to is missing or not 'id.port'" % i)
        else:
            if dcid not in by_id:
                _err(fails, "connections[%d].to references unknown component '%s'" % (i, dcid))
            if not dport:
                _err(fails, "connections[%d].to '%s' has an empty input port" % (i, dst))

    # --- 2. Number Sliders carry a real range --------------------------------
    for cid, comp in by_id.items():
        if comp.get("type") != "Number Slider":
            continue
        s = comp.get("slider")
        if not isinstance(s, dict):
            _err(fails, "Number Slider '%s' has no 'slider' {min,max,value} block" % cid)
            continue
        mn, mx, val = s.get("min"), s.get("max"), s.get("value")
        nums = {"min": mn, "max": mx, "value": val}
        bad = [k for k, v in nums.items() if not isinstance(v, (int, float))]
        if bad:
            _err(fails, "Number Slider '%s' missing numeric %s" % (cid, ", ".join(sorted(bad))))
            continue
        if mn == 0 and mx == 1:
            _err(fails, "Number Slider '%s' left at default 0..1 range (set a real range)" % cid)
        if not mn < mx:
            _err(fails, "Number Slider '%s' min (%s) must be < max (%s)" % (cid, mn, mx))
        elif not (mn <= val <= mx):
            _err(fails, "Number Slider '%s' value %s outside [%s, %s]" % (cid, val, mn, mx))

    # --- 3. slider authoring order recorded, contiguous, and resolvable -------
    declared_slider_ids = {cid for cid, c in by_id.items() if c.get("type") in SLIDER_TYPES}
    conn_targets = set()
    for conn in connections:
        if isinstance(conn, dict):
            conn_targets.add(conn.get("to"))

    if sliders_table:
        orders = []
        tabled_ids = set()
        for i, row in enumerate(sliders_table):
            if not isinstance(row, dict):
                _err(fails, "sliders_table[%d] is not an object" % i)
                continue
            sid = row.get("id")
            order = row.get("order")
            wires_to = row.get("wires_to")
            if sid not in by_id:
                _err(fails, "sliders_table[%d] id '%s' is not a declared component" % (i, sid))
            else:
                tabled_ids.add(sid)
            if not isinstance(order, int):
                _err(fails, "sliders_table row for '%s' missing integer 'order'" % sid)
            else:
                orders.append(order)
            if not isinstance(wires_to, str) or not wires_to:
                _err(fails, "sliders_table row for '%s' missing 'wires_to' target port" % sid)
            elif wires_to not in conn_targets:
                _err(fails, "slider '%s' wires_to '%s' but no connection feeds that port"
                     % (sid, wires_to))
        if orders:
            orders_sorted = sorted(orders)
            if orders_sorted != list(range(len(orders_sorted))):
                _err(fails, "slider 'order' values %s are not contiguous from 0 (first slider -> input A)"
                     % orders_sorted)
            if len(set(orders)) != len(orders):
                _err(fails, "slider 'order' values contain duplicates: %s" % orders)
        missing = declared_slider_ids - tabled_ids
        if missing:
            _err(fails, "sliders not recorded in sliders_table (order/meaning undefined): %s"
                 % ", ".join(sorted(missing)))
    elif declared_slider_ids:
        _err(fails, "plan has sliders %s but no sliders_table to record order/meaning"
             % ", ".join(sorted(declared_slider_ids)))

    return fails


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate a Grasshopper graph-plan JSON.")
    parser.add_argument("plan", help="path to the graph-plan JSON file")
    args = parser.parse_args(argv)

    try:
        with open(args.plan, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        print("FAIL: could not read/parse '%s': %s" % (args.plan, exc))
        return 2

    fails = validate(plan)
    comp_count = len(plan.get("components", [])) if isinstance(plan, dict) else 0
    conn_count = len(plan.get("connections", [])) if isinstance(plan, dict) else 0

    if fails:
        for line in fails:
            print(line)
        print("PLAN INVALID: %d failure(s) across %d components / %d connections"
              % (len(fails), comp_count, conn_count))
        return 1

    print("PLAN OK: %d components, %d connections, sliders verified" % (comp_count, conn_count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
