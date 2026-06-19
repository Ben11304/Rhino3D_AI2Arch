---
name: text-to-model
description: >-
  Turn a text description of an object — chair, vase, table, bracket, building —
  into an editable, dimensioned Rhino model via a validated build-plan IR. Use
  when asked to model, build, or design a 3D object from words alone with no
  reference image (the image pipeline handles photos). Runs clarify -> plan ->
  validate -> emit typed geometry -> measure-and-render verify -> bounded repair.
  Produces parametric solids (boxes, cylinders, revolves, lofts, extrudes)
  assembled with interpenetrating boolean unions, verifies absolute dimensions
  against the stated scale, and hands the realized GUIDs to the scene-state
  ledger. Keywords: text to 3D, prompt to model, parametric chair, revolve vase,
  loft hull, extrude bracket, build-plan IR, dimensioned model from a description.
allowed-tools: Bash(python3 *)
paths:
  - "*.3dm"
  - "*.json"
---

# text-to-model

Turn a **text description** (no reference image) into an **editable, dimensioned Rhino model**.
This skill owns the *text* producer phase of the suite: it writes a build-plan IR conforming to
[`../shared/build-plan.schema.json`](../shared/build-plan.schema.json), drives guarded typed
geometry codegen, and runs the measure-and-render verify loop. Because the size is **stated in
words**, this pipeline verifies **absolute** dimensions (`numeric_checks`) — unlike the image
pipeline, which can only trust scale-invariant ratios (correction C5).

All shared rules — units/tolerance, the GUID ledger (C1), partial-boolean count+volume (C2),
interpenetration (C3), vision-demotion (C4), revolve/shell (C6), pre-flight inputs (C7), repair
budget (C8), token economy — live in **[`../shared/conventions.md`](../shared/conventions.md)**.
Read it; do not re-derive those rules here. Concrete per-shape recipes are in
[`reference/part-recipes.md`](reference/part-recipes.md).

## When to use vs. siblings

- **Use this skill** when the request is *words only*: "model a four-legged dining chair, 450 mm
  seat height", "design a 300 mm vase", "build an L-bracket 120×80×12 with a bolt hole".
- Use **image-to-model** when a photo/silhouette is supplied (ratios, not absolutes).
- Use **grasshopper-parametric** when the deliverable is a live parametric *graph*, not baked solids.
- **rhino-modeling** routes; **rhino-scene-state** owns the GUID ledger; **render-and-look** owns
  vision; **rhino-repair** owns the bounded fix loop. Hand off via the IR + scene-graph artifacts.

## Workflow checklist

Work the steps in order. Externalize state into the IR artifact and re-read it each step; never hold
geometry in your head.

1. **Confirm units + tolerance.** Decide the document `units` (mm/cm/m/in/ft) and absolute
   `tolerance` from the prompt; default mm + 0.01 mm for product-scale objects. These become the IR
   `units`/`tolerance`. At runtime read the live `sc.doc.ModelAbsoluteTolerance` — never hardcode
   (conventions §1). Set `sc.doc.ModelUnitSystem` **before** any geometry exists.

2. **Clarify ≤ 3 load-bearing dimensions.** Ask at most three questions, and only for dimensions
   that change the topology or fail verification if guessed — e.g. seat height, overall height,
   wall thickness. For everything else pick sensible category defaults and record them in
   `params` + `provenance`. Do not interrogate the user about cosmetic details.

3. **Emit the build-plan IR.** Write a JSON file conforming to the schema:
   `object`, `units`, `tolerance`, `world_frame`, a `scale` block with `value_source:"stated"` and
   `confidence:"high"` (text pipeline), `params` for every proportion, `parts` (primitive XOR
   operation each), `boolean_plan`, and `verify`. Declare `symmetry` (mirror/rotational) to author
   one instance and reflect the rest. Every number is in `units` against `world_frame`.

4. **Validate with `scripts/validate_plan.py`.** Run it before emitting any geometry:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/validate_plan.py" path/to/plan.json
   ```

   It exits non-zero with verbose, frame-aware messages on the first structural defect (missing
   unit/frame, missing C3 `penetration` on a union, revolve profile not on axis per C6, malformed
   scale, bbox-vs-stated-height mismatch). **Fix the IR until it exits 0** — do not generate geometry
   from an invalid plan. See `examples/chair.json` for a complete plan that validates.

5. **Parameterize relations to numbers in LOCAL frames.** Resolve every `relation` and `param` to
   concrete numbers expressed in each part's **local frame**, then relocate to the world via
   `Transform.PlaneToPlane(Plane.WorldXY, target)` (conventions §4). For union joins, bake the
   `penetration` depth into the mating part's geometry so faces **overlap 0.5–2 mm**, never touch
   coincidentally (C3). Do not sprinkle offsets through raw coordinates.

6. **Emit per part via typed tools, then `boolean_*`.** Create each primitive with `create_object`
   (BOX/CYLINDER/SPHERE/CONE) and curves/operations with the typed `loft`, `sweep1`,
   `extrude_curve`, `offset_curve`, `pipe` tools. Fall back to `execute_rhinocommon_csharp_code` /
   `execute_rhinoscript_python_code` **only** for revolve / shell / network surface (no typed tool).
   Follow the codegen guard contract (conventions §5): pre-flight inputs (C7), null + `IsValid` +
   `IsSolid` + `GetNakedEdges` checks, bake with `Name` + `UserString "part_id"` + layer, return the
   GUID. Wrap any mutator that does not return a GUID in the create-then-find-newest shim (C1). Then
   run the `boolean_plan` with interpenetrating inputs.

7. **Reconcile + measure-verify.** Register every `part_id -> GUID` into the scene-graph (C1). After
   each boolean, run the **EXPECTED-COUNT + TOTAL-VOLUME** check against the IR (C2) — a silently
   dropped leg leaves a *valid, solid* 3-legged chair that only count+volume catches. Evaluate the
   IR `numeric_checks` and `ratio_checks` via `analyze_objects` / bbox math (vision-demotion, C4):
   overall height, seat height, solid count, key ratios.

8. **Render-and-look ≤ 3 iterations.** Color each part (conventions §3/§8), `capture_viewport` at
   low resolution, and answer the IR `binary_questions` with vision — *relative* position/color
   questions only ("is the red seat above the blue legs?"), never measured size. Drive repairs
   through the bounded loop (per-item budget N=3, global wall 12; C8/conventions §10): re-measure and
   re-render only the affected parts each pass. After ≤ 3 vision iterations, surface any item still
   marked "could not fix" to the caller. Save the model as a `.3dm` and emit the final IR JSON.

## Outputs

- The validated **build-plan IR** (`*.json`) — the editable spec, re-readable and re-buildable.
- The realized **`.3dm`** model with `part_id`-tagged, layered, dimensioned solids.
- The `part_id -> GUID` entries handed to **rhino-scene-state**, and any unresolved verify items
  surfaced to the caller for **rhino-repair**.
