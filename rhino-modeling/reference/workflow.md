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

## Phase 0.5 — RESUME (orchestrator), only when reopening a saved build

Owner: **rhino-modeling**. Run this phase **only** when the job opens an existing `.3dm` that already
has a sidecar (`<name>.rhino-skills/`, conventions §12a) — a crash recovery or a continue-where-I-left-off
edit. A fresh build skips straight to Phase 1.

1. **Re-bind `part_id -> live GUID` from UserStrings, NOT from the saved GUID.** A `.3dm` round-trip
   does **not** preserve `System.Guid`s (conventions §12a), so the GUID stored in
   `scene-graph.json` is a **hint only**. The authoritative handle is the `UserString "part_id"`
   stamped on every baked object (§2/§3). Walk the live document once and rebuild the ledger via the
   `rebind_part_ids()` helper shape in conventions §12a — every node whose `part_id` resolves to a live
   object gets its `guid` overwritten with the current value; a node whose `part_id` resolves to no
   live object is a **lost part** (route that stage to re-emit).
2. **Connectivity edges survive the round-trip untouched.** Edges are keyed by `part_id` only, never
   GUID (conventions §13/C1), so the connectivity ledger needs no re-binding — only the per-node `guid`
   is re-bound. This is why C1 keys on `part_id`: the rail moved through a save/reopen but every
   baluster edge still points at it.
3. **Run ONE reconcile before emitting the first incomplete stage.** Before baking anything new, run
   `reconcile.py --stage <id>` for the **last stage the checkpoints claim passed** to confirm the
   re-bound ledger matches the live document (no MISSING/EXTRA/MIS-SIZED). This catches a `.3dm` that
   was edited outside the loop, or a checkpoint the `.3dm` does not actually back. On a clean reconcile,
   trust each stage's last `checkpoints[]` entry (`status` + `revision` + `connectivity_status`) to
   decide which stages are already good and which must re-emit — and re-emit starts at the **first**
   stage whose checkpoint is not `status:"passed"` **with** `connectivity_status:"green"`.

**Decision point P0.5:** did every node's `part_id` re-bind to a live GUID, and did the last-passing
stage reconcile clean? If a part is lost or reconcile flags a defect, mark that stage incomplete and
let EMIT rebuild it — never trust a saved GUID over a live UserString, and never resume past a stage
whose `connectivity_status` is not `green`.

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
- **At the stage boundary, CHECKPOINT + SIDECAR SAVE.** After this stage's parts baked and
  `reconcile.py --stage <id>` passes, append `checkpoints[]: {stage, status:"passed", revision,
  part_ids, connectivity, connectivity_status}` to the scene-graph, bump `revision`, set `last_op`,
  and **persist** in the load-bearing order below. The save is what persists the ledger across a crash
  or rollback (defeats E11).
- **There is NO dedicated MCP save tool (correction D).** Persistence runs through
  `execute_rhinoscript_python_code` calling RhinoCommon directly: `ok = sc.doc.WriteFile(path, opts)`
  (or `Rhino.RhinoDoc.ActiveDoc.SaveAs(path)`), which **returns a bool** — see the
  `save_with_sidecar()` helper shape in conventions §12a. The sidecar lives next to the `.3dm` in
  `<name>.rhino-skills/` (build-plan.json + scene-graph.json + `checkpoints/NNNN-stage.json`).
- **Write ORDER is load-bearing — ledger AFTER geometry.** (1) `ok = sc.doc.WriteFile(...)`;
  (2) **only if `ok is True`** write the JSON sidecar; (3) write each JSON file **atomically**
  (`tmp + os.replace`) so a crash mid-write never leaves a half-written ledger. If `WriteFile` returns
  `False` — **or you are uncertain it persisted (flag it, D)** — do **not** advance the checkpoint:
  stop and surface rather than recording a checkpoint the `.3dm` does not back. Geometry is the source
  of truth; the JSON is a hint.

**Decision point P5:** is every part id of THIS stage present as a node with a live GUID, and did the
scoped reconcile pass, and did `WriteFile` return `True` before the sidecar was written? A missing key
means a part was silently dropped (common after a partial boolean, C2) — route to REPAIR for this stage
only. A `WriteFile` that did not return `True` means the geometry is not persisted — stop, do not
checkpoint. On pass, checkpoint + sidecar save, then advance to the next stage. (The CONNECTIVITY GATE
in Phase 6 must also be green before this stage's checkpoint may read `status:"passed"`.)

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

### The CONNECTIVITY GATE (conventions §13/C9 — part of the Definition of Done)

SEE is not done when shapes merely *look* right; it is done when every declared contact is
**numerically proven in-band**. This gate is the DETECT leg of the triad (PREVENT at Phase 3 ->
DETECT here -> ENFORCE via the completeness clause), and it stands alone: it reads the **live
document by GUID**, not the IR intent, so it catches a defect even if PREVENT resolved a wrong literal
(A1).

- **One batched sweep per stage (B1).** Run the per-stage connectivity sweep as **ONE**
  `execute_rhinoscript_python_code` call — the `check_connectivity --stage <id>` operation (it mirrors
  `reconcile.py --stage <id>` in scope, B3, and is owned by **rhino-scene-state**). Its body loops only
  the edges whose endpoints are in the current or already-closed stages, measures each **realized
  solid-to-solid gap by GUID** (A1; `Brep.ClosestPoint` / boolean-intersection sign), judges it against
  the **per-relation-type band** (A3: `on_top_of`=>`[0,+tol]`, `lands_on`/`meets`=>`[-pen,+tol]`,
  `coincident`=>`|g|<=tol`, `interpenetrate`=>`[-2,-0.5]mm`, `spans`/`spans_between`=>both endpoint gaps
  in band), and returns **only the violations** (`out_of_band` + `uncovered`) as compact JSON. Passing
  gaps never enter context.
- **Realized handle, never AABB (A2/A4).** For helical/rotated/curved members the sweep uses the node
  `obb`/`centroid` only for orientation + coarse culling and measures against the realized solid with
  `at_surface:"realized"` (the nearest point on B's solid, i.e. Z-at-arc-length), never an AABB or a
  scalar face height.
- **Sample symmetric families (B2).** A radial/helical array of N: measure **all N** on the initial
  bake; on a re-emit checkpoint measure a **sample** (first / middle / last + any previously flagged
  member); re-measure the **full N only when the `array` rule changed**.
- **The gate (ENFORCE, completeness clause).** The stage's checkpoint may read
  `connectivity_status:"green"` (and therefore `status:"passed"`) **only** when the sweep returns
  **zero `out_of_band` and zero `uncovered`** entries. Every **non-floating** assembly part must own
  **>= 1 declared + measured** contact; a declared contact with no measurement is `uncovered` = FAIL;
  parts flagged `floating:true` are exempt (F). Silence is no longer a pass.

**Decision point P6:** collect pass/fail per verify item **and** the connectivity verdict. Any verify
fail -> REPAIR with that item's identity attached. Any `out_of_band` or `uncovered` connectivity entry
-> REPAIR routed by the connectivity playbook entry (Phase 7), and the stage **cannot** be marked done
until the re-run sweep is green. Image pipeline: ignore absolute `numeric_check` failures when
`scale.confidence` is medium/low — but the connectivity gate is **scale-invariant** (a measured gap in
model units) and therefore applies to the image pipeline too.

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

### Routing a CONNECTIVITY violation (conventions §13/C9)

An `out_of_band` or `uncovered` entry from the Phase-6 sweep is a repair item just like a verify
failure, carrying `{edge:{type,from,to}, gap, band, status}`. Route it to the connectivity entry in
[../../rhino-repair/reference/failure-playbook.md](../../rhino-repair/reference/failure-playbook.md)
("part A does not reach support B (gap > tol)"):

- **`out_of_band`** (a measured gap outside the band — a baluster short of the rail, a column floating
  above the floor, an arch sunk into a column): translate/extend the **`from` part A** by the measured
  gap to its support, or recompute `A.top`/`A.base` through the resolver `support` function (PREVENT),
  then re-emit **A's stage only**. For a helical/rising support, recompute at **A's own angle**
  (`value_ref {part:"rail", of:"z_at_angle", at:θ_A}`), not a global Z.
- **`uncovered`** (a declared contact with no measurement, or a non-floating part with no declared
  contact, or an edge into a deleted part_id, C2): this is a *completeness* defect, not a geometry
  one — add the missing declared relation to the IR (PREVENT) so the contact is actually built and
  measured, or mark the part `floating:true` if it is genuinely meant to float (F). Never satisfy an
  `uncovered` by deleting the edge.
- **CROSS-STAGE INVALIDATION (C1).** Re-emitting the support's stage **moves** it, so set
  `connectivity_status:"not_run"` on **every** stage that has an edge crossing into the re-emitted
  stage, and re-run their sweeps — even though those stages' own geometry was untouched. This works
  because edges are keyed by `part_id` (the re-bake changes GUIDs, not part_ids).

After a connectivity repair, **re-run the per-stage sweep** (and any invalidated crossing stage's
sweep); the stage is done only when the sweep is green.

**Decision point P7:** after a repair, re-run the affected SEE checks **and the connectivity sweep for
the affected + any invalidated crossing stage**. Green + verify-pass -> continue. Budget or wall hit
-> stop and surface (the connectivity defect is reported, never hidden).

---

## Definition of Done (the clause that kills false confidence)

A stage — and the whole build — may be declared **done ONLY when every declared contact is
numerically proven in-band.** Concretely, a stage is done when ALL hold:

1. every part of the stage is a node with a **live GUID** and the scoped `reconcile.py --stage <id>`
   passes (no MISSING / EXTRA / MIS-SIZED, no partial-boolean count/volume mismatch, C2);
2. `sc.doc.WriteFile(...)` returned **`True`** and the sidecar JSON was written atomically
   **after** the geometry (conventions §12a / D);
3. the Phase-6 **connectivity sweep is GREEN** — zero `out_of_band` and zero `uncovered` entries —
   so every non-floating assembly part owns **>= 1 declared + measured** contact (conventions §13/C9
   completeness clause; `floating:true` parts exempt, F);
4. every Phase-6 vision/math verify item is Pass (or a surfaced "could not fix").

The framework may **never** report success while a connectivity obligation is `out_of_band` or
`uncovered`. Silence is not a pass: an unmeasured contact is a FAIL. This is the defense-in-depth
result of the triad — PREVENT (Phase 3 value_ref/support resolves the attach literal) -> DETECT
(Phase 6 realized GUID-to-GUID sweep, independent of the resolver) -> ENFORCE (this clause).

---

## Phase 8 — REPORT (orchestrator)

Owner: **rhino-modeling**.

Return: the final scene-graph (`part_id -> GUID`), the validated IR, and an explicit list of any
**"could not fix"** items with the verify check that failed and the measured value. Never silently
drop a defect — a valid-looking but wrong model (e.g. a 3-legged chair that passed every naive
guard) must be reported, not hidden.

---

## Loop-control summary

EMIT→REMEMBER→(reconcile)→(connectivity sweep)→checkpoint loops **per stage** (conventions §12/§13)
before the whole-model SEE pass; a stage failure or a connectivity violation routes to REPAIR for that
stage only. A reopened build runs P0.5 RESUME first.

```
P0   PREAMBLE  --(units/snapshot ok?)-->        P1 ROUTE
P0.5 RESUME    --(reopen only: rebind part_id->GUID from UserString; 1 reconcile of last-passing stage)
P1   ROUTE     --(producer+server ok?)-->       P2 KNOW-API
P2   KNOW-API  --(typed tool? else execute_*)-> P3 PLAN  (PREVENT: value_ref/support resolves attach literal)
P3   PLAN      --(IR validates?)-->             P4 EMIT (per stage, scoped-idempotent)
  for each stage in depends_on order (start at first non-green stage on resume):
    P4 EMIT     --(purge-stage; Create* valid?)--+fail--> P7 REPAIR (this stage only)
                \--ok-------------->             P5 REMEMBER (stamp stage + GUID)
    P5 REMEMBER --(stage reconcile --stage ok?)--+fail--> P7 REPAIR (this stage only)
                \--ok-------------->             P6 SEE: connectivity sweep --stage (DETECT, A1)
    P6 GATE     --(sweep green? 0 out_of_band + 0 uncovered)--+violations--> P7 REPAIR (connectivity)
                \--green----------->             WriteFile==True? --> sidecar JSON (ledger-after-geometry, D)
                                                  --> CHECKPOINT (status:passed, connectivity_status:green) --> next stage
P6 SEE        --(whole-model verify pass?)--+fail--------> P7 REPAIR
              \--pass----------->             P8 REPORT
P7 REPAIR     --(budget/wall left?)--+yes----> re-EMIT affected STAGE / re-SEE / re-sweep
              |                                 (re-emit a support => set crossing stages not_run, C1)
              \--no------------->             P8 REPORT (surface "could not fix" / connectivity defect)
```

Definition of Done (above): a stage is done only when reconcile passes, `WriteFile` returned `True`,
the connectivity sweep is green (every non-floating contact numerically proven in-band, §13/C9), and
every verify item passes. The framework never reports success with an `out_of_band`/`uncovered`
contact.
