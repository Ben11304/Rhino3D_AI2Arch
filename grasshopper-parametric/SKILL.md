---
name: grasshopper-parametric
description: >-
  Produces a live, editable Grasshopper definition with Number Slider parameters
  instead of static baked geometry, encoding correct GH port and wiring semantics
  (which output type feeds which input, point-vs-plane, curve-vs-brep, slider
  ordering). Builds the component graph with gh_build_graph / gh_mutate_graph,
  validates every wire with gh_get_component_type_info + validate_connection
  before connecting, then gh_run_solution and reads back warnings. Use when the
  request says "parametric", "with sliders", "make it adjustable", "knobs to
  tweak", "Grasshopper", "GH definition", or "live/editable geometry". Also offers
  the v1 re-runnable-IR alternative that delivers ~80% of parametric value by
  rebuilding from an edited build-plan number without touching GH wiring.
allowed-tools: >-
  gh_get_component_type_info, validate_connection, gh_add_component,
  gh_connect_components, gh_build_graph, gh_mutate_graph, gh_connect_components,
  gh_run_solution, gh_get_canvas_state, get_document_summary, capture_viewport
---

# grasshopper-parametric

Turn a static build into a **live, editable Grasshopper definition** whose shape is
driven by **Number Sliders** (and a few MD Sliders / Graph Mappers). The deliverable
is a canvas the user can keep tweaking, not a frozen Brep. This skill owns the
**Grasshopper phase only**; it consumes the build-plan IR (see
[`../shared/build-plan.schema.json`](../shared/build-plan.schema.json)) and the shared
[`../shared/conventions.md`](../shared/conventions.md), and hands the canvas state back
to the orchestrator.

> Mental model (from conventions): **MCP is hands+eyes, the skill is brain+method.** GH
> wiring is exactly where the LLM spatial/graph deficit bites — a wire that "looks right"
> silently coerces the wrong type. So **externalize the graph as an explicit
> component-and-connection plan, validate every port before wiring, and read warnings
> back after the solution**. Never hold the graph in your head.

---

## When to use this skill vs. the v1 alternative (read this first)

There are two ways to deliver "parametric", and the cheap one is usually right.

### v1 — re-runnable IR regeneration (PREFER THIS unless GH is explicitly required)
"Parametric" almost always means *"let me change a number and see it update"*. You get
**~80% of that value with 0% of the GH wiring risk** by treating the build-plan IR as the
parameter surface:

1. Expose the knobs the user cares about as IR `params` (e.g. `seat_height`, `leg_radius`)
   and make `parts` reference them symbolically.
2. To "adjust", **edit one number in the IR and rebuild** via the normal text-to-model /
   geometry-api path. The rebuild is deterministic, guarded (codegen guard contract,
   conventions §5), volume-checked (C2), and leaves a clean GUID ledger.
3. Ship the IR alongside the model as the "source you edit".

Choose v1 when the user says "adjustable / I want to tweak the height" but does **not**
name Grasshopper, when the parameters are a handful of scalars, or when correctness and a
clean scene-graph matter more than an on-canvas slider. It is faster, cheaper in tokens,
and cannot produce a mis-wired graph.

### v2 — a real Grasshopper definition (this skill's main path)
Build an actual GH graph with sliders when the user explicitly asks for **Grasshopper / a
GH definition / on-canvas sliders / a live definition to hand to a colleague**, or when the
parametric relationships are graph-shaped (arrays along curves, data-tree branching,
graph-mapper falloffs) rather than a few scalars. The rest of this skill is the v2 method.

---

## The v2 workflow (build a Grasshopper definition)

### 0. Decompose to a graph plan BEFORE touching the canvas
Write the definition down as an explicit list of **components** (by exact type name) and
**connections** (`source_component.output_port -> target_component.input_port`). Pull the
shapes from [`reference/gh-patterns.md`](reference/gh-patterns.md) (revolve-a-profile,
array-on-curve, parametric-louver). This plan is the artifact you re-read each step — the
GH analogue of the build-plan IR.

Map IR `params` to **Number Sliders** one-to-one, and record **slider order**: the GH
convention is *first slider authored → input A*, so author sliders in the order the
downstream component expects them (see [`reference/gh-wiring.md`](reference/gh-wiring.md)
§ "Slider ordering"). Getting order wrong silently swaps radius↔height.

### 1. ALWAYS introspect ports before wiring (non-negotiable)
For **every** component you intend to wire, call **`gh_get_component_type_info`** to read
its real input/output **port names and expected data types**. Do **not** guess port names
(`P` vs `Point`, `B` vs `Brep`, `G` vs `Geometry`) — they differ per component and a wrong
name is a silent no-op.

Then, for **every** connection, call **`validate_connection`** with the resolved source
output type and target input type. This catches the type-mismatch class documented in
[`reference/gh-wiring.md`](reference/gh-wiring.md) (point→plane, curve→brep, number→domain)
*before* it becomes an invisible coercion or a dead wire. Only wire connections that
validate. If a connection is invalid but recoverable, insert the **adapter component** the
wiring reference prescribes (e.g. `XY Plane` to lift a point to a plane, `Boundary
Surfaces` / `Brep` to promote a curve, `Construct Domain` to make a number a domain).

### 2. Build the graph in BATCHES, not one component at a time
Prefer the **batched** tools over a flurry of singles (token economy, conventions §11):

- **`gh_build_graph`** — emit the whole component-and-connection plan in **one call**: all
  components plus all (already-validated) connections. This is the default authoring path.
- **`gh_mutate_graph`** — apply a **batch of edits** (add/remove/rewire components, change
  slider ranges/values) in one call when revising an existing canvas.

Use the single-shot **`gh_add_component`** / **`gh_connect_components`** only for a
one-off patch on an already-built graph; never to build a whole definition node-by-node.

When you author a Number Slider via these tools, set its **min / max / value / rounding**
explicitly from the IR param (e.g. `leg_radius` → slider `10..40`, value `20`, integer or
1-decimal rounding). Unset ranges default to `0..1` and will look broken.

### 3. Run the solution, then READ the warnings
Call **`gh_run_solution`** to compute the canvas, then **read back component warnings and
errors** (orange = warning, red = error) — via the solution result and/or
**`gh_get_canvas_state`**. A GH definition can be fully wired and still be wrong:
"1 of N branches empty", "Solution exception", "Invalid curve", null Breps. Treat any
warning/error as a defect to fix, exactly like a failed verify check.

Common warning → fix mappings live in [`reference/gh-wiring.md`](reference/gh-wiring.md)
("Common type-mismatch wiring errors and fixes").

### 4. Verify the GEOMETRY, not just the graph
Wiring being valid does not mean the geometry is right. Route verification per conventions
§8 (vision-demotion, C4):

- **Measurable** outcomes (does the revolve close into a solid? expected solid count? total
  volume? slider at its midpoint gives height H?) → bake the GH output and check with
  `analyze_objects` / bbox math, or read the GH geometry's properties. Never eyeball a
  dimension.
- **Profile-shape fidelity / "does it look like X"** → `capture_viewport` (low-res) at
  decision points only, then vision.

Honor the geometry corrections even though geometry is produced inside GH:
- **C2** partial/silent boolean failure — after a `Solid Union` / `Solid Difference`
  component, check **expected solid count + total volume**, not just "is there a Brep".
- **C3** interpenetration — parts feeding a `Solid Union` must overlap **0.5–2 mm**; bake
  that offset into the upstream slider defaults / `Move` vectors, never coincident contact.
- **C6** revolve/shell — `Revolution` (`RevSrf`) yields a **surface**; add `Cap Holes` (and
  for shells a closed Brep) before treating it as a solid; the profile must touch the axis at
  both ends or be closed. See the revolve template in
  [`reference/gh-patterns.md`](reference/gh-patterns.md).

### 5. Repair loop (bounded)
Apply the repair-budget rule (conventions §10, C8): each failing warning/check gets up to
**N = 3** mutate-and-rerun attempts, with a **global wall of 12** iterations across all
defects. After N attempts on one item, mark it **"could not fix"** and surface it. Each
repair is a focused `gh_mutate_graph` (rewire / swap adapter / fix slider range) followed by
`gh_run_solution` + warning read — never a full rebuild.

### 6. Hand off
The deliverable is the **live canvas** plus the **graph plan artifact** (components +
validated connections + slider table). If the orchestrator needs baked geometry too, bake
the GH output through the normal bake-and-register path so the **GUID ledger** (C1) and
`part_id` UserStrings stay correct — the scene-graph artifact remains owned by
`rhino-scene-state`, not by this skill.

---

## Hard rules (this skill will be reviewed against these)

1. **Introspect then validate then wire**: `gh_get_component_type_info` for ports →
   `validate_connection` for every wire → only then build. No guessed port names.
2. **Batch over singles**: `gh_build_graph` / `gh_mutate_graph` are the default;
   `gh_add_component` / `gh_connect_components` are patch-only.
3. **Run then read**: always `gh_run_solution` and read warnings/errors back; an unread
   solution is an unverified one.
4. **Sliders carry real ranges**: every Number Slider gets explicit min/max/value/rounding
   from an IR param; record slider order (first slider → input A).
5. **Geometry corrections still apply inside GH**: C2 count+volume after booleans, C3
   0.5–2 mm interpenetration before unions, C6 cap-before-solid for revolves/shells.
6. **Vision is demoted** (C4): measurables go to `analyze_objects`/bbox; vision only for
   shape fidelity, on low-res captures at decision points.
7. **Offer v1 first** when the user did not explicitly ask for Grasshopper — re-runnable IR
   regeneration is cheaper, safer, and delivers most of the value.

## References (one level deep)
- [`reference/gh-wiring.md`](reference/gh-wiring.md) — port semantics: which output type
  feeds which input, point-vs-plane, curve-vs-brep, slider taxonomy, slider ordering,
  type-mismatch errors and their adapter fixes.
- [`reference/gh-patterns.md`](reference/gh-patterns.md) — ready-to-build definition
  templates (revolve-a-profile, array-on-curve, parametric-louver) as component graphs you
  can feed straight to `gh_build_graph`.
- [`reference/validate_graph_plan.py`](reference/validate_graph_plan.py) — stdlib-only
  Python 3 pre-flight: checks a graph-plan JSON (every connection references a declared
  component+port, sliders have real ranges, slider order recorded) before you call
  `gh_build_graph`. Run via `${CLAUDE_SKILL_DIR}`; only its stdout enters context.
- Shared truth: [`../shared/conventions.md`](../shared/conventions.md),
  [`../shared/build-plan.schema.json`](../shared/build-plan.schema.json).
