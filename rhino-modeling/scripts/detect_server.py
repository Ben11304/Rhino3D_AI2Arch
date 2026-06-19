#!/usr/bin/env python3
"""Detect which MCP server flavor is connected to Rhino/Grasshopper and recommend an
execution surface.

Input: a list of the MCP tool names that are currently available, supplied EITHER as a
single JSON array argument / stdin, OR as plain whitespace/comma-separated tokens.

Examples
--------
    python3 detect_server.py '["create_object","loft","boolean_union","gh_add_component"]'
    echo '["execute_rhinoscript_python_code","get_document_summary"]' | python3 detect_server.py
    python3 detect_server.py create_object loft boolean_union get_document_summary

Output: a human-readable report (and, with --json, a machine-readable JSON object) naming the
detected flavor, the recommended execution surface, and the loop capabilities that are missing.

Stdlib only. No third-party imports. Passes `python3 -m py_compile`.
"""

import json
import sys

# --- signature tool sets per flavor (real rhinomcp / grasshopper-mcp tool names) -----------

# Typed Rhino solid/curve operations that imply a rich, typed create surface (rhinomcp).
TYPED_SOLID_OPS = frozenset({
    "create_object",
    "loft",
    "extrude_curve",
    "sweep1",
    "boolean_union",
    "boolean_difference",
    "boolean_intersection",
    "offset_curve",
    "pipe",
})

# The Grasshopper canvas family.
GH_TOOLS = frozenset({
    "gh_add_component",
    "gh_build_graph",
    "gh_connect_components",
    "gh_mutate_graph",
    "gh_run_solution",
    "gh_get_canvas_state",
    "gh_get_component_type_info",
})

# Code-execution escape hatches.
EXEC_TOOLS = frozenset({
    "execute_rhinoscript_python_code",
    "execute_rhinocommon_csharp_code",
    "run_command",
})

# Sensors the verify loop relies on.
MATH_SENSORS = frozenset({"analyze_objects", "get_object_info"})
SUMMARY_SENSORS = frozenset({"get_document_summary", "get_objects"})
VISION_SENSORS = frozenset({"capture_viewport"})

# Tool names that strongly indicate the canonical rhinomcp surface specifically.
RHINOMCP_MARKERS = frozenset({
    "get_document_summary",
    "analyze_objects",
    "capture_viewport",
    "search_rhinoscript_functions",
    "get_rhinoscript_docs",
    "validate_connection",
})

# Ops with NO typed tool anywhere -> must always go through an exec hatch (conventions §11).
EXEC_ONLY_OPS = ("revolve", "shell", "network surface")


def _parse_tools(argv):
    """Build the set of available tool names from argv or stdin.

    Accepts a JSON array (as one arg or on stdin) or loose whitespace/comma tokens.
    """
    raw_parts = []

    # 1) explicit args (excluding flags)
    args = [a for a in argv if a != "--json"]
    if args:
        raw_parts.extend(args)

    # 2) stdin, only if it is not a TTY and nothing usable came from args
    if not raw_parts and not sys.stdin.isatty():
        stdin_data = sys.stdin.read()
        if stdin_data.strip():
            raw_parts.append(stdin_data)

    tools = set()
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        # Try JSON first (array or single string).
        parsed = None
        try:
            parsed = json.loads(part)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and item.strip():
                    tools.add(item.strip())
            continue
        if isinstance(parsed, str) and parsed.strip():
            tools.add(parsed.strip())
            continue
        # Fall back to comma/space splitting of the raw token.
        for token in part.replace(",", " ").split():
            token = token.strip().strip('"').strip("'")
            if token:
                tools.add(token)
    return tools


def classify(tools):
    """Return a dict describing the detected flavor, surface, and missing capabilities."""
    tools = set(tools)

    has_typed = bool(tools & TYPED_SOLID_OPS)
    has_gh = bool(tools & GH_TOOLS)
    has_exec = bool(tools & EXEC_TOOLS)
    has_math = bool(tools & MATH_SENSORS)
    has_summary = bool(tools & SUMMARY_SENSORS)
    has_vision = bool(tools & VISION_SENSORS)
    rhinomcp_marker_count = len(tools & RHINOMCP_MARKERS)

    # --- flavor classification (mirrors server-capabilities.md heuristics) ---
    if has_gh and has_typed:
        flavor = "rhinomcp"
        confidence = "high"
        surface = (
            "Typed MCP tools for everything except revolve/shell/network surface; "
            "full plan->build->verify->repair loop available."
        )
    elif has_gh and not has_typed:
        flavor = "grasshopper-mcp"
        confidence = "high"
        surface = (
            "Route grasshopper-parametric here; author on the canvas (gh_* tools) and "
            "gh_run_solution + bake before analyze_objects is meaningful."
        )
    elif has_typed and not has_gh and rhinomcp_marker_count >= 3:
        # Canonical rhinomcp typed surface + its marker sensors, but the Grasshopper
        # bridge is not connected this session. Still rhinomcp; just no canvas family.
        flavor = "rhinomcp"
        confidence = "medium"
        surface = (
            "Typed MCP tools for everything except revolve/shell/network surface; full "
            "plan->build->verify->repair loop available. Grasshopper canvas (gh_*) is NOT "
            "connected this session, so parametric/definition requests cannot be served until "
            "the GH bridge is attached."
        )
    elif has_exec and not has_gh and not has_typed:
        flavor = "lamcp"
        confidence = "high"
        surface = (
            "execute_rhinoscript_python_code for ALL geometry, honoring the codegen guard "
            "contract by hand; compute and print counts/volumes/bboxes inside the script."
        )
    elif (has_typed or has_exec or has_summary) and rhinomcp_marker_count < 3:
        flavor = "SerjoschDuering"
        confidence = "low"
        surface = (
            "Treat as rhinomcp-like but UNVERIFIED: probe get_document_summary (or closest "
            "equivalent) first, map available verbs onto the loop, fall back to an exec tool "
            "for anything missing."
        )
    else:
        flavor = "unknown"
        confidence = "low"
        surface = (
            "Could not classify. Probe with get_document_summary and validate_connection, "
            "then map the available verbs onto the loop manually."
        )

    # --- missing capabilities against the loop's needs ---
    missing = []
    if not has_typed:
        missing.append(
            "typed create surface (create_object/loft/sweep1/boolean_*): "
            "must hand-write guarded geometry via execute_rhinoscript_python_code"
        )
    if not has_gh:
        missing.append(
            "Grasshopper canvas family (gh_*): parametric/definition requests cannot be served"
        )
    if not has_math:
        missing.append(
            "analyze_objects/get_object_info (MATH verification): "
            "numeric_checks/ratio_checks must be computed inside executed scripts"
        )
    if not has_summary:
        missing.append(
            "get_document_summary/get_objects (state snapshot): "
            "create-then-find-newest GUID diff and expected-count checks are degraded"
        )
    if not has_vision:
        missing.append(
            "capture_viewport (VISION): no profile-fidelity or relative-position checks; "
            "verification falls back to math-only"
        )
    if not has_exec:
        missing.append(
            "execute_* escape hatch: revolve/shell/network surface (no typed tool) cannot be built"
        )

    return {
        "flavor": flavor,
        "confidence": confidence,
        "recommended_surface": surface,
        "missing_capabilities": missing,
        "capabilities": {
            "typed_solid_ops": has_typed,
            "grasshopper_canvas": has_gh,
            "exec_hatch": has_exec,
            "math_sensors": has_math,
            "state_summary": has_summary,
            "vision_capture": has_vision,
        },
        "exec_only_ops": list(EXEC_ONLY_OPS),
        "tool_count": len(tools),
        "v1_recommendation": (
            "Pick ONE server for the whole job (prefer rhinomcp). Do not straddle two servers "
            "mid-build: the part_id->GUID ledger (C1) desyncs, verification loses parity, and "
            "re-querying state across servers violates the token-economy rules."
        ),
    }


def render_report(result):
    """Render the classification dict as a readable text report."""
    lines = []
    lines.append("=== MCP server flavor detection ===")
    lines.append("flavor               : %s (confidence: %s)"
                 % (result["flavor"], result["confidence"]))
    lines.append("tools inspected      : %d" % result["tool_count"])
    lines.append("")
    lines.append("recommended surface  :")
    lines.append("  " + result["recommended_surface"])
    lines.append("")
    caps = result["capabilities"]
    lines.append("capabilities present :")
    for key in ("typed_solid_ops", "grasshopper_canvas", "exec_hatch",
                "math_sensors", "state_summary", "vision_capture"):
        mark = "yes" if caps[key] else "NO"
        lines.append("  [%3s] %s" % (mark, key))
    lines.append("")
    if result["missing_capabilities"]:
        lines.append("missing capabilities :")
        for m in result["missing_capabilities"]:
            lines.append("  - " + m)
    else:
        lines.append("missing capabilities : none")
    lines.append("")
    lines.append("exec-only ops (no typed tool, always via execute_*): %s"
                 % ", ".join(result["exec_only_ops"]))
    lines.append("")
    lines.append("v1 recommendation    :")
    lines.append("  " + result["v1_recommendation"])
    return "\n".join(lines)


def main(argv):
    want_json = "--json" in argv
    tools = _parse_tools(argv)
    if not tools:
        sys.stderr.write(
            "error: no tool names provided. Supply a JSON array (arg or stdin) or "
            "whitespace/comma-separated tool names.\n"
        )
        return 2
    result = classify(tools)
    if want_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
