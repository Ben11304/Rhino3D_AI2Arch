---
name: image-to-model
description: >-
  Turn a reference image (photo, render, sketch, or product shot) of a physical
  object into an editable Rhino model. Vision analysis classifies the object and
  picks an archetype, reads available views (front/side/three-quarter) and
  symmetry planes, factorizes discrete structure before continuous shape, extracts
  silhouette/profile curves, grounds absolute scale with explicit provenance, emits
  a validated build-plan IR, generates guarded RhinoCommon geometry, then closes a
  render-vs-reference silhouette compare loop. Use when an image is attached and the
  user says model / recreate / reconstruct / make this in Rhino / build this from the
  photo. Conditional factorization: discrete structure first (how many parts, how
  they join), then continuous parameters (profiles, radii, tapers), then ground scale
  last. Image-derived sizes are guesses, so the verify loop checks scale-invariant
  RATIOS, never absolutes.
allowed-tools: Bash(python3 *)
---

# image-to-model

Reconstruct a Rhino model from one or more reference images. This skill owns the
**image pipeline** phase of the suite: it produces the build-plan IR (the `object`
through `verify` fields) and the guarded geometry codegen, then hands the realized
GUIDs to `rhino-scene-state` and the verify loop to `render-and-look`.

Read the canonical rules first: [`../shared/conventions.md`](../shared/conventions.md)
and the IR contract [`../shared/build-plan.schema.json`](../shared/build-plan.schema.json).
This skill must honor corrections **C1–C8** from those conventions; the two that bite
the image pipeline hardest are **C5** (verify ratios, not absolutes) and **C4** (demote
vision for anything measurable).

## When to use vs. when not to

- **Use** when a reference image is attached AND the user asks to model / recreate /
  reconstruct / rebuild / "make this in Rhino" from the picture.
- **Do not use** when the request is a pure text description with no image — that is
  `text-to-model` (which verifies *absolute* dimensions because scale is stated).
- **Do not use** for parametric definition authoring on the Grasshopper canvas — that is
  `grasshopper-parametric`.

## Core idea: conditional factorization

Reconstruct in the order that the most reliable information is available, conditioning
each later step on the earlier ones:

1. **Discrete structure first** — *what are the parts and how do they join?* Counts,
   topology, and symmetry are the most robust things vision can read. Lock them before
   touching any dimension.
2. **Continuous parameters next** — profiles, radii, tapers, fillets. These are read
   off silhouettes/profiles and are less certain than counts.
3. **Ground scale last** — absolute size is the least reliable thing in an image. Carry
   it as a range + confidence and never let it gate the discrete or continuous steps.

This ordering means a wrong absolute scale never corrupts the topology, and the verify
loop can repair shape (ratios) without ever trusting a guessed millimeter.

## Pipeline checklist

Run these in order. Each step writes into the IR; do not skip ahead.

1. **Identify + pick archetype.** From the image, classify the object class and select
   a parametric archetype (e.g. vase = revolved solid of revolution; chair = seat +
   legs + back; mug = revolved body + applied handle). The archetype seeds the discrete
   factorization and the fallback profiles. See
   [`reference/vision-analysis.md`](reference/vision-analysis.md).

2. **Determine views & symmetry.** Decide which views are present (front, side,
   three-quarter / 3-4) and detect the symmetry type: **rotational** (solids of
   revolution — record `symmetry.type="rotational"`, `axis`) or **bilateral / mirror**
   (record `symmetry.type="mirror"`, `plane`). Symmetry **completes occluded geometry**:
   the unseen back of a bilateral object is the mirror of the front. See
   [`reference/vision-analysis.md`](reference/vision-analysis.md).

3. **Detect symmetry-breaking features BEFORE factorizing.** Find the single spout,
   the off-center handle, the one asymmetric arm — anything that violates the declared
   symmetry — *first*. Author those as standalone parts placed by an explicit frame, and
   exclude them from the mirror/array. Mirroring a handle onto both sides is the classic
   image-pipeline failure; this step prevents it. See
   [`reference/vision-analysis.md`](reference/vision-analysis.md).

4. **Factorize discrete structure.** Emit the part list: primitives, operations
   (loft / sweep1 / revolve / extrude / shell / boolean), counts, and instance frames.
   Declare `relations` — and for any part that feeds a boolean union, declare
   `interpenetrate` with a 0.5–2 mm `penetration` (C3). Build the `boolean_plan`.

5. **Extract profile / continuous params.** For revolved or lofted parts, run
   `scripts/extract_profile.py` on the silhouette boundary points to get ordered control
   points. The script **averages the left and right silhouettes about the axis** to
   cancel perspective skew, flags low confidence, and emits fallback archetype profiles
   when confidence is low. Feed the control points into an `interpolated_curve` part used
   as the revolve `generatrix` or loft `sections`. See
   [`reference/vision-analysis.md`](reference/vision-analysis.md). For solids of
   revolution the profile **must start and end on the axis** (C6).

6. **GROUND SCALE with provenance.** Resolve absolute size by the priority
   **stated → reference_object → metrology_assumption**, record the
   `scale.value_source`, `scale.overall_height_mm` as a **range** when uncertain,
   `scale.confidence`, `scale.provenance`, and (for assumptions) `scale.assumption`.
   See [`reference/scale-grounding.md`](reference/scale-grounding.md).

7. **Emit the IR.** Write a complete build-plan IR that validates against
   `../shared/build-plan.schema.json`. Producers own `object`..`verify`; do **not**
   write GUIDs here (that is the scene-graph artifact, owned by `rhino-scene-state`).
   The `verify` block must lean on **`ratio_checks`** and `compare_to_reference:true`;
   include `numeric_checks` only if scale is `stated`/high. See `examples/vase.json`.

8. **Emit geometry.** Generate guarded RhinoCommon Python following the CODEGEN GUARD
   CONTRACT in [`../shared/conventions.md`](../shared/conventions.md) §5: pre-flight the
   inputs (C7), null-check after every `Create*`, `IsValid`/`IsSolid`/`GetNakedEdges`,
   post-boolean expected-count + total-volume check (C2), and bake with `Name` +
   `SetUserString("part_id", ...)` + layer + returned GUID (C1). Prefer typed MCP tools
   (`loft`, `extrude_curve`, `sweep1`, `boolean_union/difference/intersection`,
   `offset_curve`, `pipe`); fall back to `execute_rhinocommon_csharp_code` /
   `execute_rhinoscript_python_code` only for ops with no typed tool (revolve via
   `RevSurface.Create`, shell via `Brep.CreateOffset(solid=True)`).

9. **Render-vs-reference silhouette compare.** Color each part (C4/§8), then
   `capture_viewport` at low res from the reference's camera view and have
   `render-and-look` diff the rendered silhouette against the reference. This answers
   vision-appropriate questions ("does the silhouette read as the reference object?",
   `binary_questions`, `compare_to_reference`). All measurable checks (`solid_count`,
   `total_volume`, ratios) go through `analyze_objects` / bbox math, never vision (C4).

10. **Iterate ≤ 3, verifying RATIOS not absolutes (C5).** When a verify item fails, fire
    a repair (hand off to `rhino-repair`). Because image scale is a guess, the loop fires
    repairs on scale-invariant **`ratio_checks`** only (e.g. `height/max_width`) plus the
    qualitative silhouette compare — **never** on an absolute `numeric_check` when
    `scale.confidence` is medium/low. Per-item budget **N = 3** attempts then mark
    "could not fix" and surface it; respect the global wall (C8).

## Handoffs

- **GUID ledger** → `rhino-scene-state` records `part_id -> GUID` at bake time (C1).
- **Verify loop** → `render-and-look` runs `capture_viewport` + `analyze_objects` and
  routes `binary_questions`/`compare_to_reference` to vision, `numeric_checks`/
  `ratio_checks` to math (C4).
- **Repairs** → `rhino-repair` consumes the failing verify items under the C8 budget.

## Anti-patterns (image pipeline specifically)

- Mirroring a symmetry-breaking feature (spout/handle) onto both sides — caught by step 3.
- Trusting a single silhouette edge — `extract_profile.py` averages left+right (C5).
- Repairing toward a guessed millimeter — only ratios + silhouette drive repairs (C5).
- Using vision to "measure" — counts/volumes/distances go to `analyze_objects` (C4).
- Treating an image-derived height as a point value — always a range + confidence.
