---
name: rhino-geometry-api
user-invocable: false
description: >-
  Authoritative RhinoCommon and rhinoscriptsyntax reference for Rhino 8 — covers
  geometry type selection (NURBS vs Brep vs SubD vs Mesh), tolerance and unit
  handling, and per-operation failure cheat-sheets for loft, sweep1, revolve,
  extrude, boolean union/difference/intersection, fillet, offset, and network
  surface. Use whenever generating or reviewing Rhino python or C# geometry code,
  or when a geometry op returns null/empty/partial. Prevents hallucinated APIs and
  silent geometry failures by supplying real function signatures, mandatory
  pre-flight input checks, and post-operation count/volume guards.
allowed-tools: Bash(python3 *)
---

# rhino-geometry-api

Knowledge skill (no user invocation). The **brain** half of "MCP is hands+eyes,
the skill layer is brain+method" for raw geometry code. It does **not** create
objects; it tells the calling skill *which* RhinoCommon / rhinoscriptsyntax call
to emit, *how to pre-flight its inputs*, and *how to prove the result is correct*.

The single source of truth for conventions is
[`../shared/conventions.md`](../shared/conventions.md). This skill never restates
those rules verbatim — it points at them and adds the per-API detail.

---

## HARD RULE — docs before code

**ALWAYS call `get_rhinoscript_docs` / `search_rhinoscript_functions` (for
rhinoscriptsyntax) or `gh_get_component_type_info` (for Grasshopper components)
BEFORE emitting any `execute_rhinoscript_python_code`,
`execute_rhinocommon_csharp_code`, or `gh_add_component`.**

The model hallucinates plausible-but-wrong API names (`Brep.Loft`,
`rs.Revolve`, `Surface.Offset(solid=True)`). Verify the exact name and signature
against the live docs first. For RhinoCommon types not in the rhinoscript docs,
confirm the signature against the cheat-sheets in `reference/` below before
emitting. Never guess an overload — read it.

Prefer **typed MCP tools** (`loft`, `extrude_curve`, `sweep1`, `boolean_union`,
`offset_curve`, `pipe`) over raw `execute_*`. Fall back to `execute_*` **only**
for ops with no typed tool: **revolve, shell, network surface**. The model
over-loves writing Python — resist it (token-economy rule §11).

---

## Codegen guard contract (summary)

Every geometry-producing snippet a skill emits MUST honor the contract defined
in [`../shared/conventions.md`](../shared/conventions.md) §5. In short:

1. `#! python3` on line 1.
2. `# r: <pkg>` comments ONLY for genuine third-party packages — never for
   `Rhino`, `rhinoscriptsyntax`, `scriptcontext`, `System`.
3. Read `tol = sc.doc.ModelAbsoluteTolerance` and
   `ang_tol = sc.doc.ModelAngleToleranceRadians` live — never hardcode `0.001`.
4. **Pre-flight the INPUTS** before any `Create*` (correction C7) — see
   `reference/geometry-ops.md`.
5. Null/empty check **after every** `Create*`.
6. `IsValid` + `IsSolid` + `GetNakedEdges` on every resulting Brep.
7. **Post-boolean: EXPECTED-COUNT + TOTAL-VOLUME** vs the IR (correction C2) — a
   partial boolean returns a *valid* Brep missing a part; only count+volume
   catches it.
8. `Name` + `SetUserString("part_id", ...)` + layer assignment at bake
   (correction C1 — the GUID ledger).
9. `AddBrep` (etc.) returns the GUID; register it; one `sc.doc.Views.Redraw()`
   at the very end; `print` only the GUID you need into context.

Lint or wrap any generated snippet with the runnable enforcer:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/codegen_guard.py" --lint /path/to/snippet.py
python3 "${CLAUDE_SKILL_DIR}/scripts/codegen_guard.py" --wrap /path/to/snippet.py
```

`--lint` prints a structured PASS/WARN/FAIL report (each contract rule it can
check statically). `--wrap` emits the snippet bracketed by a guard preamble that
reads tolerances live and a postamble that redraws once — both to stdout.

---

## Reference files (one level deep)

- [`reference/geometry-ops.md`](reference/geometry-ops.md) — per-operation
  cheat-sheets: pre-flight inputs (C7) + post-checks (C2/C3) + the real function
  name and the fix for each failure mode. loft, sweep1, revolve, extrude,
  boolean, fillet, offset, network surface.
- [`reference/rhinocommon.md`](reference/rhinocommon.md) — the core RhinoCommon
  idioms (`Brep.CreateBooleanUnion`, `Brep.CreateFromLoft`, `RevSurface.Create`
  + Brep conversion, `Brep.CreateOffset`, `Curve.CreateInterpolatedCurve`,
  `Transform.PlaneToPlane`, `ObjectAttributes`/`SetUserString`,
  `sc.doc.Objects.AddBrep`), plus when to use rhinoscriptsyntax vs RhinoCommon
  vs CPython3.
- [`reference/types-and-conversions.md`](reference/types-and-conversions.md) —
  NURBS / Brep / SubD / Mesh selection matrix (when reliable vs fragile) and the
  conversion functions between them.
- [`reference/tolerance-units.md`](reference/tolerance-units.md) —
  `ModelAbsoluteTolerance` / `ModelAngleToleranceRadians` rules, setting the unit
  system before geometry, and sensible tolerances per unit.

## Script

- [`scripts/codegen_guard.py`](scripts/codegen_guard.py) — stdlib-only Python 3
  linter/wrapper that enforces the codegen contract. Run via
  `${CLAUDE_SKILL_DIR}`; only its stdout enters context.
