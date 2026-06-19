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
   expected-count checks. Prefer the summary over a full `get_objects` dump (conventions §11). Note:
   EMIT is **staged** (conventions §12), so the load-bearing GUID-diff snapshot is taken
   **per-stage, AFTER that stage's scoped purge**, inside the stage script — not once for the whole
   build. This Phase-0 snapshot is the build-wide baseline; the per-stage snapshot is what
   `find_newest_guid` actually diffs against.

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

## Phase 4 — EMIT (orchestrator + geometry-api), STAGED + scoped-idempotent

Owner: **rhino-modeling** drives; codegen shape comes from **rhino-geometry-api**.

Emit is **staged**, and every stage is **scoped-idempotent** (conventions §12). Do **not** emit the
whole model as one atomic script — that is what let a single `Brep.CreateOffset` failure roll back
all 931 solids, and what let the double-execution wrapper double the geometry. Instead walk the IR
`stages[]` in `depends_on` order (a part with no stage is the implicit `default` stage), and for
**each stage** emit ONE `execute_*` call shaped as:

1. **Scoped purge preamble.** Generate it with the helper, then concatenate your bake code after it:
   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../rhino-scene-state/scripts/stage_emit.py" \
     --stage <stage_id> --part-ids <id1>,<id2>,...
   ```
   It deletes only the live objects tagged with this `stage` (or in the `part_id` allow-list), then
   snapshots `_stage_before_ids` **after** the purge for the create-then-find-newest shim.
2. **Guarded bake of this stage's parts** (the §5 codegen contract): for every primitive author on
   WorldXY → transform to `frame`; for every operation pre-flight inputs, create, null-check,
   validate; for every union interpenetrate by `penetration`, `Brep.CreateBooleanUnion`, then the
   count+volume guard. Stamp `attr.SetUserString("stage", <stage_id>)` and `"part_id"` on **every**
   bake so the next re-run can find and purge it.

Booleans must live **inside one stage** (inputs + result share a stage) so a `child_of` consumption
never crosses a stage boundary.

**Decision point P4:** did each `Create*` in the stage return non-null and pass
`IsValid`/`IsSolid`/`GetNakedEdges`? A failure aborts **only this stage** and routes straight to
REPAIR (Phase 7) for the failing item — earlier stages are already baked, reconciled, checkpointed
and saved, so they are not touched. The idempotent purge means re-running the stage cleans any
partial bake from the aborted attempt before rebuilding.

---

## Phase 5 — REMEMBER (state skill)

Owner: **rhino-scene-state**.

- At bake time, capture the **GUID** returned by `AddBrep` / `AddCurve` / `AddSurface` and write
  `part_id -> GUID` (with `stage`) into the scene-graph artifact node.
- **Every mutator must return its GUID.** Typed tools that don't return one are wrapped in a
  **create-then-find-newest** shim: diff the current object table against the **per-stage**
  `_stage_before_ids` snapshot (taken after the scoped purge, conventions §12); exactly one new
  object is expected.
- Tag each object with `UserString "part_id"`, `UserString "stage"` (and optionally `provenance`) —
  `part_id` is the **fallback resolver** when a GUID is lost after a boolean consumes its inputs, and
  `stage` is the **scoped idempotent delete key** for a re-emit (conventions §2/§3/§12, C1).
- **At the stage boundary, CHECKPOINT.** After this stage's parts baked and `reconcile.py --stage
  <id>` passes, append `checkpoints[]: {stage, status:"passed", revision, part_ids}` to the
  scene-graph, bump `revision`, set `last_op`, and **Save the .3dm** (via the MCP save tool /
  `RhinoDoc.Save`). The save is what persists the ledger across a crash or rollback (defeats E11).

**Decision point P5:** is every part id of THIS stage present as a node with a live GUID, and did the
scoped reconcile pass? A missing key means a part was silently dropped (common after a partial
boolean, C2) — route to REPAIR for this stage only. On pass, checkpoint + save, then advance to the
next stage.

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
scene-graph; never re-query unchanged geometry). When a repair must re-bake geometry it **re-emits
only the affected stage** (conventions §12): re-run that stage's scoped-idempotent script, which
purges and rebuilds just that stage's objects, then reconcile `--stage <id>` and re-checkpoint. No
prior stage is rebuilt, so a fix to one feature never disturbs the rest of a 931-solid model, and the
idempotent purge guarantees the re-bake does not duplicate geometry. Repairs are driven by the IR
`verify` block and the same vision/math routing.

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

EMIT→REMEMBER→(reconcile)→checkpoint loops **per stage** (conventions §12) before the whole-model
SEE pass; a stage failure routes to REPAIR for that stage only.

```
P0 PREAMBLE   --(units/snapshot ok?)-->        P1 ROUTE
P1 ROUTE      --(producer+server ok?)-->       P2 KNOW-API
P2 KNOW-API   --(typed tool? else execute_*)-> P3 PLAN
P3 PLAN       --(IR validates?)-->             P4 EMIT (per stage, scoped-idempotent)
  for each stage in depends_on order:
    P4 EMIT     --(purge-stage; Create* valid?)--+fail--> P7 REPAIR (this stage only)
                \--ok-------------->             P5 REMEMBER (stamp stage + GUID)
    P5 REMEMBER --(stage reconcile --stage ok?)--+fail--> P7 REPAIR (this stage only)
                \--ok-------------->             CHECKPOINT + Save .3dm --> next stage
P6 SEE        --(whole-model verify pass?)--+fail--------> P7 REPAIR
              \--pass----------->             P8 REPORT
P7 REPAIR     --(budget/wall left?)--+yes----> re-EMIT affected STAGE / re-SEE affected
              \--no------------->             P8 REPORT (surface "could not fix")
```
