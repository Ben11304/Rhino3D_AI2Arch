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
