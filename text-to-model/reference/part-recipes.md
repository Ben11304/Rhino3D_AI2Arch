# Part Recipes — text-to-model

Concrete, copy-adaptable recipes that turn one **build-plan IR fragment** into a **typed MCP tool
sequence**. Each recipe is self-contained: the IR fragment is what the planner emits, the tool
sequence is what the executor runs. All rules referenced here (C1–C8, codegen guard contract,
interpenetration, revolve/shell, vision-demotion) live in
[`../../shared/conventions.md`](../../shared/conventions.md) — this file does not duplicate them.

Conventions used below:
- Every number is in the document `units` (here **mm**) against `world_frame` (here `WorldXY`).
- `${CLAUDE_SKILL_DIR}` is the skill root; scripts print only the result GUID to stdout.
- Prefer the **typed** MCP tools (`create_object`, `loft`, `sweep1`, `extrude_curve`,
  `boolean_union/difference/intersection`, `offset_curve`, `pipe`). Fall back to
  `execute_rhinoscript_python_code` / `execute_rhinocommon_csharp_code` **only** for operations
  with no typed tool — currently **revolve**, **shell**, and **network surface**.
- After any mutator that does not return a GUID, run the create-then-find-newest shim from
  conventions §2 and register `part_id -> GUID` in the scene-graph (correction C1).
- After every boolean, run the EXPECTED-COUNT + TOTAL-VOLUME check (correction C2).

---

## Recipe 1 — Assembly of primitives (chair, table, bracket)

Build solid primitives on explicit frames, push mating parts together by the IR `penetration`
depth, then `boolean_union` into one welded solid. This is the workhorse for furniture and
brackets. See `../examples/chair.json` for a complete plan.

### IR fragment

```json
{
  "parts": [
    { "id": "seat", "primitive": "box",
      "frame": { "plane": "WorldXY", "origin": [0, 0, 430] },
      "dims": { "x": 420, "y": 420, "z": 40 } },
    { "id": "leg_front_right", "primitive": "cylinder",
      "frame": { "plane": "WorldXY", "origin": [180, 180, 0] },
      "dims": { "radius": 18, "height": 411.5 },
      "relations": [ { "type": "interpenetrate", "to": "seat", "penetration": 1.5 } ] },
    { "id": "chair_solid", "operation": "boolean", "op": "union",
      "inputs": ["seat", "leg_front_right", "leg_front_left", "leg_back_right", "leg_back_left", "back"],
      "result": "chair_solid" }
  ],
  "boolean_plan": [
    { "op": "union",
      "inputs": ["seat", "leg_front_right", "leg_front_left", "leg_back_right", "leg_back_left", "back"],
      "result": "chair_solid" }
  ]
}
```

### Typed MCP tool sequence

1. `create_object` type=`BOX` for `seat` — center/frame `[0,0,430]`, size `420×420×40`. Capture the
   returned GUID; register `seat -> GUID`; stamp `UserString part_id="seat"`, name, layer.
2. `create_object` type=`CYLINDER` for each leg. The IR leg `height` already bakes in the `1.5 mm`
   interpenetration (411.5 = 410 floor-to-seat-bottom + 1.5 overlap), so the leg top sits **inside**
   the seat slab — never coincident with it (correction C3). Register each GUID.
3. `create_object` type=`BOX` for `back`, sunk into the seat by `1.5 mm` along the contact normal.
4. `boolean_union` with all six input GUIDs → one `chair_solid` GUID. `boolean_union` consumes its
   inputs, so capture the new GUID and re-register `chair_solid -> GUID`; the old part GUIDs are now
   dead (resolve survivors by `UserString part_id` if needed, conventions §2).
5. **Verify (C2):** `analyze_objects` on `chair_solid` → assert `solid_count == 1` and
   `total_volume ≈ Σ part volumes − Σ overlap volumes` within tolerance. A silently dropped leg
   leaves a *valid, solid* 3-legged chair that only the count+volume check catches.

When `create_object` does not expose a needed primitive transform, fall back to
`execute_rhinocommon_csharp_code` building `Rhino.Geometry.Box` / `Cylinder` on a
`Transform.PlaneToPlane(Plane.WorldXY, target)` frame (conventions §4) — never hand-translate
coordinates.

---

## Recipe 2 — Revolve a profile (vase, bottle, turned leg, lampshade)

A `RevSurface.Create` returns a **surface, not a solid** (correction C6). The profile must start
**and** end **on the axis** (or be closed); cap before any shell. There is **no typed revolve
tool**, so this recipe uses `execute_rhinocommon_csharp_code` / `execute_rhinoscript_python_code`.

### IR fragment

```json
{
  "parts": [
    { "id": "vase_profile", "primitive": "interpolated_curve",
      "provenance": "wine-glass archetype generatrix, lies in the XZ plane",
      "control_points": [
        [0, 0, 0], [55, 0, 0], [60, 0, 20], [38, 0, 90],
        [34, 0, 180], [52, 0, 250], [46, 0, 300], [0, 0, 300]
      ] },
    { "id": "vase_body", "operation": "revolve",
      "generatrix": "vase_profile",
      "axis": { "line": [[0, 0, 0], [0, 0, 300]] },
      "angle_deg": 360, "cap": true },
    { "id": "vase_hollow", "operation": "shell",
      "inputs": ["vase_body"], "thickness": 3, "open_face": 0 }
  ]
}
```

Note the generatrix **starts at `[0,0,0]` and ends at `[0,0,300]`** — both on the Z axis — so the
revolved surface closes into a solid. `validate_plan.py` enforces this endpoint-on-axis rule.

### Tool sequence (typed where possible, `execute_*` for revolve/shell)

1. Build the profile with `create_object` type=`CURVE` through `control_points` (or
   `Curve.CreateInterpolatedCurve` inside `execute_rhinocommon_csharp_code`). Register
   `vase_profile -> GUID`.
2. **Pre-flight (C7):** confirm the axis is coplanar with the profile and both endpoints lie on the
   axis line within `sc.doc.ModelAbsoluteTolerance`.
3. Revolve via `execute_rhinocommon_csharp_code`:
   `RevSurface.Create(profile, axisLine, 0.0, RhinoMath.ToRadians(360))` →
   `Brep.CreateFromRevSurface(rev, false, false)` → `brep.CapPlanarHoles(tol)` →
   assert `brep.IsSolid`. Register `vase_body -> GUID`.
4. Shell via `execute_rhinocommon_csharp_code`:
   `Brep.CreateOffset(brep, -thickness, true, true, tol)` (solid offset). The brep **must already be
   closed** (step 3 capped it). Leave `open_face` 0 open for a vessel. Register `vase_hollow -> GUID`.
5. **Verify:** `analyze_objects` for wall thickness sanity (bbox math, C4); vision (`capture_viewport`
   + render-and-look) for *"does the silhouette read as a vase?"* only (profile-shape fidelity).

---

## Recipe 3 — Loft through varying sections (boat hull, tapered vase, organic seat shell)

`Brep.CreateFromLoft` through 2+ section curves. The recipe's whole risk is **C7 pre-flight**: all
sections must run the **same direction** and have **aligned seams**, or the loft twists.

### IR fragment

```json
{
  "parts": [
    { "id": "sec_base", "primitive": "interpolated_curve",
      "frame": { "plane": "WorldXY", "origin": [0, 0, 0] },
      "control_points": [[-100,-60,0],[100,-60,0],[120,0,0],[100,60,0],[-100,60,0],[-120,0,0],[-100,-60,0]] },
    { "id": "sec_mid", "primitive": "interpolated_curve",
      "frame": { "plane": "WorldXY", "origin": [0, 0, 150] },
      "control_points": [[-90,-50,150],[90,-50,150],[105,0,150],[90,50,150],[-90,50,150],[-105,0,150],[-90,-50,150]] },
    { "id": "sec_top", "primitive": "interpolated_curve",
      "frame": { "plane": "WorldXY", "origin": [0, 0, 300] },
      "control_points": [[-70,-40,300],[70,-40,300],[82,0,300],[70,40,300],[-70,40,300],[-82,0,300],[-70,-40,300]] },
    { "id": "hull", "operation": "loft",
      "sections": ["sec_base", "sec_mid", "sec_top"] }
  ]
}
```

### Typed MCP tool sequence

1. `create_object` type=`CURVE` for each closed section. Register each `part_id -> GUID`.
2. **Pre-flight (C7):** for every section after the first, compare `TangentAtStart` against the base
   section; reverse with `Curve.Reverse()` when the dot product is negative; align seams to the
   nearest parameter (use the codegen-guard `preflight_loft` helper, conventions §5).
3. `loft` (typed tool) with the ordered section GUIDs, `LoftType.Normal`, `closed=false`. Capture the
   resulting GUID. If the typed tool returns no GUID, run the find-newest shim (C1).
4. Cap the open ends with `CapPlanarHoles(tol)` (via `execute_rhinocommon_csharp_code`) if a closed
   solid is required, then assert `IsSolid` and `GetNakedEdges()` is empty.
5. **Verify:** `analyze_objects` for bbox extents (C4 numeric); render-and-look for *"does the
   surface read as a smooth hull (no twist/pinch)?"* (vision, profile fidelity).

---

## Recipe 4 — Extrude a prismatic profile (bracket plate, table apron, building footprint)

`extrude_curve` (typed) or `Surface.CreateExtrusion` for a constant cross-section pushed along a
straight distance. Ideal for prismatic brackets, slab floors, and extruded building masses.

### IR fragment

```json
{
  "parts": [
    { "id": "plate_profile", "primitive": "interpolated_curve",
      "frame": { "plane": "WorldXY", "origin": [0, 0, 0] },
      "control_points": [[0,0,0],[120,0,0],[120,80,0],[40,80,0],[40,40,0],[0,40,0],[0,0,0]] },
    { "id": "bracket_plate", "operation": "extrude",
      "inputs": ["plate_profile"], "height": 12, "cap": true }
  ]
}
```

### Typed MCP tool sequence

1. `create_object` type=`CURVE` for the **closed** profile (last point equals first). Register GUID.
2. **Pre-flight (C7):** confirm the profile is closed and planar; extrusion distance `height`
   exceeds `tol` and is below the part's min in-plane feature size for sane proportions.
3. `extrude_curve` (typed) along the frame normal by `height`. Capture GUID; find-newest shim if the
   tool returns none (C1).
4. Cap into a closed solid (`cap=true` → `CapPlanarHoles(tol)`); assert `IsSolid`.
5. Holes/slots: model the hole as a `cylinder` primitive that **interpenetrates** the plate, then
   `boolean_difference` (base = plate). Run the C2 count+volume check afterward.
6. **Verify:** `analyze_objects` for plate thickness and hole count (C4 numeric); vision only for
   *"does the outline match the intended bracket profile?"*.

---

## Recipe 5 — Relational construction (the PREVENT leg of connectivity, §13/C9)

The dominant real-session failure was **connectivity**: balusters that did not reach a rising
helical handrail, columns floating above the floor, arches not seated on column tops — every one
caught by a human *by eye*. The fix has three legs (conventions
[§13](../../shared/conventions.md)): **PREVENT** (build the attach geometry to the *correct literal*),
**DETECT** (`check_connectivity` measures the realized solid-to-solid gap), **ENFORCE**
(`UNCOVERED = FAIL`). This recipe is the **PREVENT** leg — it lets the IR *publish* a support and
*reference* it, instead of guessing a coordinate.

Three new IR constructs make this work; `validate_plan.py --resolve` folds them into a plain-literal
IR **before** any geometry is emitted (and the literal IR re-validates):

- **`support`** (per part): a level/law a part PUBLISHES for others to attach to. A floor publishes
  `{kind:"plane_z", value:0}`; a rising helical rail publishes
  `{kind:"helix_z", helix:{base_z, pitch, radius, start_angle}}`. The helix law is
  `Z(theta_deg) = base_z + pitch*((theta - start_angle)/360)`.
- **`value_ref`** (any numeric: dims/height/thickness/penetration/frame.origin/tol): a NON-literal
  number resolved at codegen — `{param:"name"}`, `{op:"+|-|*|/", args:[...]}` (folded left-to-right;
  `args[0]` is the minuend/numerator), or `{part:"id", of:"top_z|base_z|z_at_angle|z|centroid_z",
  at?:deg}` reading a coordinate **published by another part's `support`**.
- **`array`** (per part): declares ONE part as a FAMILY of `count` instances
  (`radial`/`helical`/`linear`); the resolver expands it to concrete parts `<id>#<i>` (0-based).
  Inside an array part a value_ref may read the **reserved params** `__angle__` (this instance's
  array angle, degrees) and `__i__` (this instance's 0-based index), so each member can resolve its
  own attach height.

> The resolver turns these into literals: run
> `python3 scripts/validate_plan.py <plan>.json --resolve --out resolved.json`. A cycle in the
> value_ref DAG is an ERROR; an unknown param/part, a division by zero, or a non-finite result is an
> ERROR. The emitted `resolved.json` has **no** value_refs/arrays left and validates with the same
> validator — that literal IR is what the connectivity sweep (§13 DETECT) later measures against.

### 5a — Helical baluster family landing on a rising rail (the exact stair case)

The rail publishes its helix law; each baluster reads the rail's `z_at_angle` at **its own** array
angle, so its top is built to reach the rail (no guessed length). A `lands_on` relation with
`at_surface:"realized"` makes the curved support measured by realized distance (A4), not a face label.

```json
{
  "params": { "rail_base_z": 900, "rail_pitch": 600, "rail_radius": 1500,
              "post_base_z": 0, "baluster_radius": 12 },
  "parts": [
    { "id": "rail", "primitive": "cylinder",
      "frame": { "plane": "WorldXY", "origin": [1500, 0, 900] },
      "dims": { "radius": 30, "height": 50 },
      "support": { "kind": "helix_z",
        "helix": { "base_z": { "param": "rail_base_z" }, "pitch": { "param": "rail_pitch" },
                   "radius": { "param": "rail_radius" }, "start_angle": 0 } } },

    { "id": "baluster", "primitive": "cylinder",
      "frame": { "plane": "WorldXY", "origin": [0, 0, { "param": "post_base_z" }] },
      "dims": { "radius": { "param": "baluster_radius" },
        "height": { "op": "-", "args": [
            { "part": "rail", "of": "z_at_angle", "at": { "param": "__angle__" } },
            { "param": "post_base_z" } ] } },
      "array": { "kind": "helical", "axis": "WorldZ", "count": 5,
        "radius": { "param": "rail_radius" }, "angle_step": 18,
        "pitch": { "param": "rail_pitch" }, "start_angle": 0 },
      "relations": [ { "type": "lands_on", "to": "rail", "at_surface": "realized" } ] }
  ]
}
```

`--resolve` expands `baluster` into `baluster#0..#4` on the helix (plan radius 1500, rising
`z_step = pitch*angle_step/360 = 30 mm` per step) with **per-instance** heights computed from the
rail's helix law: `900, 930, 960, 990, 1020` at angles `0,18,36,54,72`. No baluster is the same
length — exactly what stops the "balusters do not reach the rising rail" defect. The
[`examples/chair.json`](../examples/chair.json) leg height is the same idea in miniature:
`height := seat.base_z - floor.top_z + join_penetration`.

### Tool sequence (5a)

1. Build `rail` (typed `create_object` / pipe along a helix curve); stamp `UserString part_id="rail"`,
   register `rail -> GUID`. Pin its published support height with the §5a DIRECTION-PIN idiom so the
   support level is *where the geometry actually is*.
2. For each resolved `baluster#i` (the IR is already literal): `create_object` type=`CYLINDER` on its
   frame; stamp `UserString part_id="baluster#i"`; register each GUID.
3. **DETECT (§13):** run the per-stage connectivity sweep — ONE `execute_rhinoscript_python_code`
   that loops this stage's edges, measures `realized_gap(rail_GUID, baluster#i_GUID)` for each (sample
   first/middle/last + flagged on re-emit, full N on initial bake, B2), and returns only the
   `out_of_band`/`uncovered` violations. A `lands_on` band is `[-penetration, +tol]`.

### 5b — Columns landing on a stylobate floor

The floor publishes a flat `plane_z`; every column's base resolves to it and `lands_on` it. A
`linear` array lays the colonnade out at a fixed bay spacing.

```json
{
  "params": { "floor_top": 0, "col_radius": 150, "col_height": 4200, "bay": 1800 },
  "parts": [
    { "id": "stylobate", "primitive": "box",
      "frame": { "plane": "WorldXY", "origin": [0, 0, -150] },
      "dims": { "x": 12000, "y": 2000, "z": 300 },
      "support": { "kind": "plane_z", "value": { "param": "floor_top" } } },

    { "id": "column", "primitive": "cylinder",
      "frame": { "plane": "WorldXY", "origin": [-3600, 0, { "part": "stylobate", "of": "top_z" }] },
      "dims": { "radius": { "param": "col_radius" }, "height": { "param": "col_height" } },
      "array": { "kind": "linear", "count": 5, "step": [{ "param": "bay" }, 0, 0] },
      "support": { "kind": "top_z",
        "value": { "op": "+", "args": [ { "param": "floor_top" }, { "param": "col_height" } ] } },
      "relations": [ { "type": "lands_on", "to": "stylobate", "at_surface": "top" } ] }
  ]
}
```

The column base z is `stylobate.top_z` (0), so it is built **on** the floor, not floating above it.
Each column also PUBLISHES its own `top_z` (`floor_top + col_height = 4200`) so the arch course above
can attach to it (5c). `--resolve` produces `column#0..#4` at x `-3600, -1800, 0, 1800, 3600`, each
with base on the floor.

### 5c — Arches spanning_between adjacent column tops

An arch is supported at BOTH ends by two columns; `spans_between` carries the two supports `to`
(first) and `to2` (second), and the connectivity sweep measures the gap at **both** endpoints.

```json
{
  "parts": [
    { "id": "arch_0", "operation": "sweep1", "rail": "arch_rail_0", "sections": ["arch_sec_a", "arch_sec_b"],
      "relations": [ { "type": "spans_between", "to": "column#0", "to2": "column#1", "at_surface": "realized" } ] },
    { "id": "arch_1", "operation": "sweep1", "rail": "arch_rail_1", "sections": ["arch_sec_a", "arch_sec_b"],
      "relations": [ { "type": "spans_between", "to": "column#1", "to2": "column#2", "at_surface": "realized" } ] }
  ]
}
```

The arch springing height resolves from the columns' published `top_z` (e.g. the arch rail's start/end
control points sit at `{ "part": "column#0", "of": "top_z" }`), so the arch lands **on** the column
tops. The `spans_between` edge is keyed by part_id (`column#0`/`column#1`), so re-baking a column
(changing its GUID, not its id) invalidates the arch's connectivity checkpoint (C1) and the sweep
re-measures both endpoints. A finial or pendant boss that is *meant* to hang free carries
`floating: true` so it is exempt from the completeness clause (F) and never raises a false
`UNCOVERED`.

> **Why this is not circular (A1):** the `value_ref`/`support` machinery only *builds* the geometry
> to the right place (PREVENT). The connectivity sweep (§13 DETECT) never trusts the IR coordinate — it
> measures the realized gap between the two **live solids by GUID**, so a wrong support still gets
> caught. PREVENT reduces failures; DETECT proves it; ENFORCE (`UNCOVERED = FAIL`) makes silence
> impossible.

---

## Cross-cutting checklist (applies to every recipe)

- Author on `Plane.WorldXY`, relocate via `Transform.PlaneToPlane` (conventions §4) — never bake
  offsets into raw coordinates.
- Stamp `Name`, `layer`, and `UserString "part_id"` at bake; capture the GUID into the scene-graph
  (C1).
- Union mating parts must **interpenetrate 0.5–2 mm** (C3); `validate_plan.py` rejects a union whose
  inputs declare no `interpenetrate` relation.
- Every boolean is followed by the **EXPECTED-COUNT + TOTAL-VOLUME** check (C2).
- Measurable acceptance (counts, dims, heights, symmetry) → `analyze_objects`/bbox math; vision is
  reserved for *"does it look like X"* (C4). Color each part before `capture_viewport`.
- Run `scripts/validate_plan.py <plan>.json` **before** emitting any geometry; it exits non-zero on
  the first structural defect with a verbose, frame-aware message.
- When a plan uses relational constructs (`value_ref`/`support`/`array`), run
  `scripts/validate_plan.py <plan>.json --resolve --out resolved.json` to fold them into a
  plain-literal IR (PREVENT, §13). Emit geometry from the **resolved** IR; a cycle, unknown
  param/part, or division by zero is an ERROR there. Then DETECT with the per-stage connectivity
  sweep and ENFORCE the completeness clause (`UNCOVERED = FAIL`).
