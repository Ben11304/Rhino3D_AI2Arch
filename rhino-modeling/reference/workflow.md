# rhino-modeling — the modeling cognition loop (full detail)

This is the detailed version of the master loop in `../SKILL.md`. It expands each phase of
**route -> know-API -> plan -> emit -> remember -> see -> repair**, names the decision points, and
states which sibling skill owns each phase. The orchestrator (rhino-modeling) owns the **build-plan
IR artifact** and the loop control; every other phase is delegated.

The whole loop exists to defeat the LLM spatial deficit by **externalizing spatial state** into two
artifacts that are re-read every step — the build-plan IR and the scene-graph — and by **giving
eyes** via render-and-look. Canonical rules: [../../shared/conventions.md](../../shared/conventions.md).
IR contract: [../../shared/build-plan.schema.json](../../shared/build-plan.schema.json).

---

## Phase 0 — PREAMBLE (orchestrator)

Owner: **rhino-modeling**.

1. **Units + tolerance first.** If the request implies or states a unit system, set
   `sc.doc.ModelUnitSystem` (e.g. `Rhino.UnitSystem.Millimeters`) **before any geometry exists**.
   Then read the live tolerances and pass them into every `Create*` / `Join` / `Offset` / boolean /
   `GetNakedEdges`:
   - `tol = sc.doc.ModelAbsoluteTolerance`
   - `ang_tol = sc.doc.ModelAngleToleranceRadians`
   Never bake a literal `0.001`. (conventions §1)
2. **Snapshot before mutating.** Call `get_document_summary` to capture the pre-mutation object set
   (ids only). This is the baseline for the create-then-find-newest GUID diff and for the
   expected-count checks. Prefer the summary over a full `get_objects` dump (conventions §11).

**Decision point P0:** is the document in the right unit system and is the snapshot recorded? If
not, stop — do not emit geometry against an unknown frame/tolerance.

---

## Phase 1 — ROUTE (orchestrator)

Owner: **rhino-modeling**. Two routing decisions happen here.

### 1a. Which PRODUCER builds the IR?

| Request signal | Route to | Why |
|---|---|---|
| Pure language + stated dimensions ("450 mm seat") | **text-to-model** | scale `value_source = stated`, high confidence -> verify absolutes |
| "from this image" / photo / sketch / silhouette | **image-to-model** | scale `reference_object` or `metrology_assumption` -> range + confidence -> verify ratios only (C5) |
| "parametric", "Grasshopper", "slider", "definition", "driven by N" | **grasshopper-parametric** | output is a canvas graph, not just baked Breps |
| "how do I", "which function", API question | **rhino-geometry-api** | knowledge only; no IR, no geometry |

A request can be **mixed** (e.g. "from this image, but make it parametric"). Resolve by primary
deliverable: if the user wants a live definition, grasshopper-parametric drives and consumes the
image-to-model IR as its proportions source.

### 1b. Which SERVER surface executes?

Run `scripts/detect_server.py` against the connected MCP tool names. It classifies the flavor
(rhinomcp / grasshopper-mcp / lamcp / SerjoschDuering), prints the **recommended execution surface**,
and lists **missing capabilities**. See [server-capabilities.md](server-capabilities.md). **v1 rule:
pick ONE server** and stay on it for the whole job; do not straddle two servers mid-build.

**Decision point P1:** producer chosen + server surface confirmed + required tools present. If a
required capability is missing (e.g. no `gh_*` tools but the request is parametric), report the gap
and stop rather than faking it with `execute_*`.

---

## Phase 2 — KNOW-API (knowledge skill)

Owner: **rhino-geometry-api** (`user-invocable: false` knowledge skill).

Pull the correct RhinoCommon / rhinoscriptsyntax recipe and the **codegen guard contract** for the
operations the IR will need (loft, sweep1, revolve, shell, boolean, offset, pipe). The contract
(conventions §5) is non-negotiable for any emitted geometry Python:

1. `#! python3` shebang on line 1.
2. `# r: <pkg>` only for genuine third-party packages — never for `Rhino`, `rhinoscriptsyntax`,
   `scriptcontext`, `System`.
3. Read `tol` / `ang_tol` from the document; never hardcode.
4. **Pre-flight the INPUTS (C7)** before any `Create*`.
5. Null/empty check after every `Create*`.
6. `IsValid` + `IsSolid` + `GetNakedEdges` on every resulting Brep.
7. **Post-boolean EXPECTED-COUNT + TOTAL-VOLUME** check against the IR (C2).
8. Name + `SetUserString("part_id", ...)` + layer at bake.
9. Return the GUID; `sc.doc.Views.Redraw()` once; print only the GUID.

**Pre-flight specifics (C7):**
- **loft:** all section curves run the **same direction** and seams are aligned (`Reverse()` any
  that oppose `TangentAtStart`).
- **sweep1:** the rail is **G1 continuous**.
- **fillet:** radius **< min local edge length**.
- **offset:** distance **< min feature size**.
- **revolve:** axis is **coplanar with the profile** and the profile touches the axis at **both
  ends** (or is closed) (C6/C7).

**Decision point P2:** is there a typed MCP tool for this op? If yes, use it. **Fall back to
`execute_rhinoscript_python_code` / `execute_rhinocommon_csharp_code` ONLY for revolve, shell, and
network surface** — they have no typed tool (conventions §11).

---

## Phase 3 — PLAN (producer skill)

Owner: **text-to-model** or **image-to-model** (or grasshopper-parametric for definitions).

Decompose the request into a **schema-conformant build-plan IR** *before* emitting any geometry.
The IR is the externalized spatial intent. It must validate against
[../../shared/build-plan.schema.json](../../shared/build-plan.schema.json):

- `object`, `units`, `tolerance`, `parts` are required.
- Each part is **either** a `primitive` **or** an `operation`, never both.
- Build each part on an explicit `Plane`, authored at `Plane.WorldXY`, then relocate via
  `Transform.PlaneToPlane` to its `frame` (conventions §4). Symmetry instances come from
  transforming the base part, never re-authored coordinates.
- `scale.value_source`: `stated` (text, high) vs `reference_object` / `metrology_assumption`
  (image, range + medium/low). This drives the verify routing (C5).
- Union joins declare an `interpenetrate` relation with `penetration` 0.5–2 mm (C3).
- `boolean_plan` gives the executor a single linear, volume-checkable sequence (C2).
- `verify` carries `binary_questions` + `compare_to_reference` (vision) and `numeric_checks` +
  `ratio_checks` (math) per the vision-demotion rule (C4).

For **image** inputs, EXTRACT_PROFILE is the most over-trusted step: **average left+right
silhouettes about the axis** to cancel perspective skew, flag low-confidence extractions, and
**fall back to archetype profiles** chosen by the render-vs-reference loop rather than trusting raw
pixel sampling (conventions §9, C5).

**Decision point P3:** does the IR validate AND is every dimension expressed against `world_frame`
in `units`? If not, repair the IR before emitting — never patch with ad-hoc coordinate offsets.

---

## Phase 4 — EMIT (orchestrator + geometry-api)

Owner: **rhino-modeling** drives; codegen shape comes from **rhino-geometry-api**.

Walk the IR `parts` / `boolean_plan` in order, emitting guarded geometry per the contract above.
Typed-tool-first; `execute_*` only for revolve/shell/network surface. For every primitive: author
on WorldXY, transform to `frame`. For every operation: pre-flight inputs, create, null-check,
validate. For every union: interpenetrate the inputs by `penetration` first, then
`Brep.CreateBooleanUnion`, then the count+volume guard.

**Decision point P4:** did each `Create*` return non-null and did each result pass
`IsValid`/`IsSolid`/`GetNakedEdges`? A failure here routes straight to REPAIR (Phase 7) for that
item, not a silent continue.

---

## Phase 5 — REMEMBER (state skill)

Owner: **rhino-scene-state**.

- At bake time, capture the **GUID** returned by `AddBrep` / `AddCurve` / `AddSurface` and write
  `part_id -> GUID` into the scene-graph artifact.
- **Every mutator must return its GUID.** Typed tools that don't return one are wrapped in a
  **create-then-find-newest** shim: diff the current object table against the Phase-0 snapshot;
  exactly one new object is expected.
- Tag each object with `UserString "part_id"` (and optionally `provenance`) — the **fallback
  resolver** when a GUID is lost after a boolean consumes its inputs (conventions §2/§3, C1).

**Decision point P5:** is every IR part id present as a key in the scene-graph with a live GUID? A
missing key means a part was silently dropped (common after a partial boolean, C2) — route to
REPAIR.

---

## Phase 6 — SEE (vision skill)

Owner: **render-and-look**.

Split the verify work by the **vision-demotion rule (C4)**:

- **Math (analyze_objects / bbox):** `solid_count`, `total_volume`, `overall_height`, positions,
  symmetry, every `numeric_check` and `ratio_check`. These are reliable.
- **Vision (low-res `capture_viewport`):** `binary_questions` (color-coded relative position) and
  `compare_to_reference` (profile-shape fidelity, "does it look like X"). **Color each part before
  capture** so vision answers "is the red part above the blue part?" not "is it 450 mm?".

Render **only at decision points**, not every step (conventions §11). For the **image pipeline**,
also diff the render against the reference silhouette when `compare_to_reference` is true, but only
fire repairs on scale-invariant `ratio_checks` (C5).

**Decision point P6:** collect pass/fail per verify item. Any fail -> REPAIR with that item's
identity attached. Image pipeline: ignore absolute `numeric_check` failures when
`scale.confidence` is medium/low.

---

## Phase 7 — REPAIR (repair skill)

Owner: **rhino-repair**.

Bounded fix loop with **two independent limits (C8)**:

- **Per-failure-item budget:** each failing verify item gets up to **N=3** attempts. After N, mark
  it **"could not fix"** and surface it. Never loop forever on one defect.
- **Global wall:** a hard ceiling of **12** total repair iterations across all items, independent of
  any single budget. Hitting the wall stops the loop and reports remaining defects.

Each repair re-renders / re-measures **only the affected parts** (read their GUIDs from the
scene-graph; never re-query unchanged geometry). Repairs are driven by the IR `verify` block and the
same vision/math routing.

**Decision point P7:** after a repair, re-run the affected SEE checks. Pass -> continue. Budget or
wall hit -> stop and surface.

---

## Phase 8 — REPORT (orchestrator)

Owner: **rhino-modeling**.

Return: the final scene-graph (`part_id -> GUID`), the validated IR, and an explicit list of any
**"could not fix"** items with the verify check that failed and the measured value. Never silently
drop a defect — a valid-looking but wrong model (e.g. a 3-legged chair that passed every naive
guard) must be reported, not hidden.

---

## Loop-control summary

```
P0 PREAMBLE   --(units/snapshot ok?)-->        P1 ROUTE
P1 ROUTE      --(producer+server ok?)-->       P2 KNOW-API
P2 KNOW-API   --(typed tool? else execute_*)-> P3 PLAN
P3 PLAN       --(IR validates?)-->             P4 EMIT
P4 EMIT       --(Create* valid?)--+fail------> P7 REPAIR
              \--ok-------------->             P5 REMEMBER
P5 REMEMBER   --(all part ids mapped?)--+fail-> P7 REPAIR
              \--ok-------------->             P6 SEE
P6 SEE        --(verify pass?)--+fail--------> P7 REPAIR
              \--pass----------->             P8 REPORT
P7 REPAIR     --(budget/wall left?)--+yes----> re-EMIT/re-SEE affected
              \--no------------->             P8 REPORT (surface "could not fix")
```
