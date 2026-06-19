---
name: rhino-modeling
description: >-
  Router and orchestrator for modeling, building, creating, and generating 3D geometry
  in Rhino and Grasshopper. Use when the user wants to model or build any 3D object — a
  chair, vase, table, lamp, mug, building, or mechanical part — from a text prompt, "from
  this image", or as a parametric / Grasshopper definition, and to author or edit a .3dm
  document. Owns the master plan -> build -> verify -> repair loop and the shared build-plan
  IR artifact, then delegates each phase to a sibling skill (text-to-model, image-to-model,
  grasshopper-parametric, rhino-geometry-api, rhino-scene-state, render-and-look,
  rhino-repair). Sets units and tolerance first, decomposes intent into a validated IR before
  emitting any geometry, snapshots scene state before mutating, prefers typed MCP tools over
  raw RhinoScript/RhinoCommon, and drives render-and-measure verification with bounded repair.
allowed-tools:
  - Bash(python3 *)
  - Read
  - Write
  - Edit
---

# rhino-modeling — the router / orchestrator

This is the **brain+method** entry point for every "model / build / create / generate a 3D thing
in Rhino or Grasshopper" request. It does not author geometry itself; it **routes** the request to
the right sibling skill, **owns the build-plan IR artifact**, and drives the universal
**plan -> build -> verify -> repair** loop end to end.

Mental model (do not violate): **MCP is hands+eyes; this skill layer is brain+method.** The LLM
spatial deficit is fixed at inference time by *externalizing spatial state* into two re-read
artifacts — the **build-plan IR** (this skill owns it) and the **scene-graph** (owned by
`rhino-scene-state`) — plus *giving eyes* via `render-and-look`. Never hold geometry state in your
head; read it back from the document or the artifacts every step.

All shared rules live in **[../shared/conventions.md](../shared/conventions.md)** — the single
source of truth. The IR contract is **[../shared/build-plan.schema.json](../shared/build-plan.schema.json)**.
Do not duplicate either here; link to them.

---

## Universal preamble (do this before anything else, every time)

1. **Set / confirm units + tolerance FIRST**, before any geometry exists. Read them live off the
   document; never hardcode a literal tolerance. If the IR requests a unit system, set
   `sc.doc.ModelUnitSystem` *before* creating geometry, then read
   `sc.doc.ModelAbsoluteTolerance` and `sc.doc.ModelAngleToleranceRadians`. See conventions §1.
2. **Decompose before you emit.** Produce and validate the **build-plan IR** (schema-conformant)
   *before* emitting a single geometry call. The IR externalizes the full spatial intent so you
   re-read world state each step instead of holding it in context. No geometry without an IR.
3. **Snapshot state before mutating.** Call `get_document_summary` (not a full `get_objects` dump)
   to record the pre-mutation object set, so every new GUID can be diffed out and registered in
   the scene-graph (conventions §2, correction C1).
4. **Typed-tool-first.** Prefer typed MCP tools (`create_object`, `loft`, `extrude_curve`,
   `sweep1`, `boolean_union/difference/intersection`, `offset_curve`, `pipe`,
   `gh_add_component`/`gh_connect_components`/`gh_build_graph`) over raw
   `execute_rhinoscript_python_code` / `execute_rhinocommon_csharp_code`. **Fall back to `execute_*`
   ONLY** for operations with no typed tool: **revolve, shell, and network surface**. The model
   over-loves writing Python; resist it (conventions §11).

---

## The master loop (checklist — run top to bottom)

```
[ ] 0. PREAMBLE   units+tolerance set & read live; pre-mutation summary snapshotted
[ ] 1. ROUTE      classify the request -> pick the producer + server surface
[ ] 2. KNOW-API   pull the right RhinoCommon/MCP recipe (rhino-geometry-api)
[ ] 3. PLAN       decompose intent -> validated build-plan IR (producer skill)
[ ] 4. EMIT       guarded codegen, typed-tool-first, pre-flight inputs (C7)
[ ] 5. REMEMBER   bake -> capture GUIDs -> write scene-graph ledger (rhino-scene-state)
[ ] 6. SEE        render + measure: vision for shape, math for numbers (render-and-look)
[ ] 7. REPAIR     bounded fix loop on failing verify items (rhino-repair)
[ ] 8. REPORT     surface "could not fix" items + final scene-graph + IR
```

Full detail, decision points, and per-phase ownership: **[reference/workflow.md](reference/workflow.md)**.

### 1. ROUTE — classify, then delegate

| Signal in the request | Producer skill | Verify pipeline |
|---|---|---|
| Stated dims / pure language ("a 450 mm dining chair") | **text-to-model** | absolute `numeric_checks` (scale = stated / high) |
| "from this image", a photo / sketch / silhouette | **image-to-model** | scale-invariant `ratio_checks` only (C5) |
| "parametric", "Grasshopper", "a slider/definition", "driven by" | **grasshopper-parametric** | gh canvas state + baked-geometry checks |
| API / "how do I", which RhinoCommon call | **rhino-geometry-api** (knowledge) | n/a (knowledge only) |

Before routing, run **`scripts/detect_server.py`** against the connected MCP tool names to confirm
which server flavor (rhinomcp / grasshopper-mcp / lamcp / SerjoschDuering) is live and which
execution surface to use. See **[reference/server-capabilities.md](reference/server-capabilities.md)**.

### 2–4. KNOW-API -> PLAN -> EMIT

- `rhino-geometry-api` supplies the **guarded codegen contract** and real RhinoCommon recipes
  (loft / sweep1 / revolve / shell / boolean). Honor the **codegen guard contract** verbatim in
  spirit (conventions §5): `#! python3` shebang, read `tol`/`ang_tol` live, **pre-flight the
  inputs (C7)**, null-check after every `Create*`, `IsValid`+`IsSolid`+`GetNakedEdges`,
  **post-boolean EXPECTED-COUNT + TOTAL-VOLUME** check (C2), bake with `part_id` UserString + layer,
  return the GUID, `Redraw()` once.
- The producer skill (text or image) writes the **build-plan IR**. **This router owns the IR
  artifact**: it is the single hand-off contract between phases. No two skills write the same IR
  field — producers write `object`..`verify`; `rhino-scene-state` appends realized GUIDs
  out-of-band in the scene-graph, never back into the IR.
- **Interpenetration (C3):** any parts feeding a `boolean union` must carry an `interpenetrate`
  relation with `penetration` 0.5–2 mm. Push mating geometry into its neighbour by that depth
  before union. Coincident/coplanar contact is forbidden.
- **Revolve/shell (C6):** `RevSurface.Create` returns a **surface**, not a solid — wrap with
  `Brep.CreateFromRevSurface`, `CapPlanarHoles(tol)`, verify closed, *then*
  `Brep.CreateOffset(-thickness, solid=True, ...)`. Profile must start+end on the axis or be closed.

### 5. REMEMBER — the GUID ledger

Delegate to **rhino-scene-state**. Every mutator must return its created GUID; wrap typed tools
that don't in a **create-then-find-newest** shim (diff the object table against the pre-mutation
snapshot). Tag every baked object with `UserString "part_id"` as the **fallback resolver** when a
GUID is lost (e.g. after a boolean consumes its inputs). The scene-graph stores `part_id -> GUID`
captured at bake time (conventions §2, C1).

### 6. SEE — render + measure (vision-demotion, C4)

Delegate to **render-and-look**. **Demote vision:** anything measurable — count, dimension,
position, symmetry — goes through `analyze_objects` / bbox math, never vision. Vision is reserved
for **profile-shape fidelity** and *"does it look like X"*. **Color each part before capture** so
vision answers the reliable "is the red seat above the blue legs?" instead of the unreliable "is it
450 mm?". Route IR `binary_questions` + `compare_to_reference` to vision; `numeric_checks` +
`ratio_checks` to math.

### 7. REPAIR — bounded fix loop (C8)

Delegate to **rhino-repair**. Two independent limits, never one global counter: a **per-failure-item
budget** (default N=3 attempts, then mark the item "could not fix" and surface it) **plus** a
**global wall** (default 12 iterations). Image pipeline fires repairs on **ratio_checks only**;
text pipeline may repair against absolute `numeric_checks` (C5).

### 8. REPORT

Surface every "could not fix" item explicitly, hand back the final scene-graph (`part_id -> GUID`)
and the IR. Do not silently drop a defect.

---

## Delegation map & hand-off contract

| Phase | Owner skill | Reads | Writes |
|---|---|---|---|
| route + orchestrate | **rhino-modeling** (this) | request, server caps | **build-plan IR (owner)** |
| API knowledge | rhino-geometry-api | — | codegen recipes (no artifact) |
| plan from text | text-to-model | request | IR `object`..`verify` |
| plan from image | image-to-model | request + image | IR `object`..`verify`, scale range |
| parametric | grasshopper-parametric | request / IR | gh canvas graph |
| GUID ledger | rhino-scene-state | bake GUIDs | scene-graph `part_id -> GUID` |
| render + measure | render-and-look | scene-graph + IR.verify | verify verdicts |
| repair | rhino-repair | verify verdicts + IR | re-emitted geometry, updated ledger |

**The contract:** producers fill the IR; this router validates it against the schema and drives the
loop; `rhino-scene-state` is the only writer of the scene-graph; nobody writes another skill's IR
field. The IR + scene-graph are the externalized world state every phase re-reads.

---

## References (one level deep)

- **[reference/workflow.md](reference/workflow.md)** — the full modeling cognition loop, decision
  points, per-phase ownership.
- **[reference/token-economy.md](reference/token-economy.md)** — concrete token-budget rules and the
  cost model.
- **[reference/server-capabilities.md](reference/server-capabilities.md)** — how rhinomcp /
  grasshopper-mcp / lamcp / SerjoschDuering tool surfaces differ; the v1 "pick ONE server" rule.
- **[../shared/conventions.md](../shared/conventions.md)** — single source of truth (units, GUID
  ledger, frames, codegen guard, interpenetration, revolve/shell, vision-demotion, ratio-vs-absolute,
  repair budget, token economy).
- **[../shared/build-plan.schema.json](../shared/build-plan.schema.json)** — the IR contract.

## Scripts

- **`scripts/detect_server.py`** — classify the connected MCP server flavor from its tool names and
  print the recommended execution surface + any missing capabilities. Run it at ROUTE time:

  ```bash
  python3 "${CLAUDE_SKILL_DIR}/scripts/detect_server.py" '["create_object","loft","boolean_union","execute_rhinoscript_python_code","get_document_summary"]'
  ```
