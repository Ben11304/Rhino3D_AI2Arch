---
name: render-and-look
description: Closes the perception loop for Rhino models by rendering canonical orthographic and iso views, coloring each part, generating 2-5 Yes/No/Unclear binary verification questions from the build-plan IR, answering them from low-res viewport captures, and emitting a differential repair list of only the failed items. Use after any text-to-model, image-to-model, grasshopper-parametric, or rhino-repair step that changed visible geometry, to judge profile-shape fidelity and "does it look like X" while delegating every measurable check (count, dimension, position, symmetry) to numeric measure-verify. Keywords render verify vision capture viewport binary question silhouette compare reference overlay CADCodeVerify perception loop differential repair.
allowed-tools: Bash(python3 *)
---

# render-and-look

**Role in the suite:** the *eyes* of the brain+method layer. This skill renders the
current document, asks a small set of binary questions derived from the build-plan IR,
answers them by looking at colored renders, and hands back a **differential repair list**
(only the items that failed). It owns the **vision** half of verification; it does **not**
own measurement.

Read [`../shared/conventions.md`](../shared/conventions.md) first — §8 (vision-demotion C4),
§9 (ratio-vs-absolute C5), §11 (token economy) govern everything below. The IR contract is
[`../shared/build-plan.schema.json`](../shared/build-plan.schema.json); the verify routing
recipe is [`reference/verification.md`](reference/verification.md).

---

## When to run

Run after **any** step that changed visible geometry: a `text-to-model` /
`image-to-model` build, a `grasshopper-parametric` bake, or a `rhino-repair` fix. Render
only at these **decision points**, never after every primitive (token economy, §11).

Inputs you receive from the orchestrator (`rhino-modeling`):
- the **build-plan IR** (`object`, `parts`, `symmetry`, `verify`, optional `scale`),
- the **scene-graph artifact** (`part_id -> GUID`, owned by `rhino-scene-state`),
- for the image pipeline, the **reference image / extracted silhouette**.

Output you return to the orchestrator:
- a **differential repair list**: `[{ part_id, question, verdict: "no"|"unclear", hint }]`.
  Items answered **Yes** are *omitted* so already-correct parts never regress.

---

## CRITICAL demotion rule (C4) — read this before doing anything

**Anything measurable is NOT judged here.** Counts, dimensions, positions, distances,
heights, angles, and symmetry offsets are verified **numerically** by
`rhino-scene-state` / **measure-verify** against the IR `numeric_checks` / `ratio_checks`
using `analyze_objects` and bounding-box math — *not* by looking at a render.

This skill judges **only**:
- **profile-shape fidelity** — "does the leg taper read as conical?", "is the back curved?",
- **qualitative likeness** — "does the overall silhouette read as a wine glass?",
- **relative position by color** — "is the **red** seat above the **blue** legs?" (reliable
  because color + above/below is robust; absolute height is not — that goes to measure-verify).

If a candidate question can be answered with a number, **route it to measure-verify** and
drop it from the vision set. The split table is in
[`reference/verification.md`](reference/verification.md).

---

## Procedure

### 1. Set the 4 canonical named views (deterministic cameras)

Captures must be repeatable run-to-run so the repair loop compares like with like. Generate
the camera commands and run them once before capturing:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/set_named_views.py --target-from-doc
```

The script prints a JSON block of RhinoCommon view commands (named views **front**, **top**,
**right**, **iso**) on stdout. Pass that block to `execute_rhinoscript_python_code` (or run the
emitted statements via `run_command`) so each named view has a fixed projection, camera vector,
and a frame-all so the whole model fills the frame identically every time. `front`, `top`,
`right` are **parallel/orthographic** projections; `iso` is a parallel SE-isometric for a single
"reads as a whole" glance.

### 2. Color each part before capture (C4 reliability)

Vision answers *relative-color* questions reliably and *absolute-size* questions unreliably.
Before capturing, assign a distinct `ObjectColor` per `part_id` so questions can be phrased
"is the red part above the blue part?". Color is a render concern only — identity stays in the
`part_id` UserString (conventions §3). Use a stable palette keyed by `part_id` so the same part
is the same color across iterations. The exact color-per-part capture protocol (palette,
`ColorSource = ColorFromObject`, restore-after) is in
[`reference/verification.md`](reference/verification.md).

### 3. Capture low-res viewports (token economy, §11)

For each needed named view call `capture_viewport` at **low resolution** (e.g. 512 px wide).
Capture only the views a question actually needs — most likeness questions need just `iso` plus
one ortho; do not blindly capture all four. Never re-capture an unchanged view.

### 4. Generate 2-5 binary questions FROM the IR (the VLM authors them)

Derive a **small** set (2-5) of **Yes/No/Unclear** questions *from the IR*, not from
free imagination. Sources, in priority order:
1. the IR `verify.binary_questions` (use verbatim — already phrased for color/relative position),
2. the IR `verify.compare_to_reference` flag (→ silhouette compare, step 6),
3. IR `parts[].provenance` / profile operations (loft / revolve / sweep1) → one profile-fidelity
   question per distinctive profile ("does the <part> profile read as <shape>?"),
4. IR `symmetry` → a *qualitative* mirror question only ("does the left half mirror the right?"),
   never a measured-offset question (that is measure-verify's job).

Each question must be answerable from the captured colored renders alone and must name the
**color** or the **relative relation**, never an absolute number. The full generation recipe
(with examples per primitive/operation) is in
[`reference/verification.md`](reference/verification.md).

### 5. Answer each question from the renders → CADCodeVerify discipline

Look at the captures and answer each question **Yes / No / Unclear**. Apply the
**CADCodeVerify** rule: **only `No` and `Unclear` become repair items.** A `Yes` is dropped, so a
part that is already correct is never re-touched and cannot regress. This makes the loop
*monotone*: each iteration can only fix, never break, a passing part.

For every `No`/`Unclear`, attach: the offending `part_id` (resolve via the scene-graph), the
question, and a short **hint** (what looked wrong, e.g. "back panel reads flat, IR says curved").

### 6. Image pipeline — silhouette compare in a canonical ORTHOGRAPHIC view (C5)

When `verify.compare_to_reference` is true:
- Render the model in the **orthographic** view whose axis best matches the reference framing
  (usually `front` or `right`). **Do not** try to match an oblique reference camera — **no
  camera-solve exists** in this suite, and matching an arbitrary oblique pose is not achievable.
  Compare silhouette-to-silhouette in a clean ortho instead.
- Compare **scale-invariant silhouette RATIOS**, not absolute pixels (C5): normalize both
  silhouettes to the same bounding height, then judge proportion overlap (e.g. bowl-width /
  height, stem-length / total-height). The image pipeline may only fire repairs on these ratio
  judgments, never on absolute size.
- If the extracted profile was flagged low-confidence, prefer the **archetype fallback** judgment
  ("does it read as the archetype <wine glass>?") over pixel-exact overlap. The silhouette-compare
  procedure is detailed in [`reference/verification.md`](reference/verification.md).

### 7. Return the differential repair list

Emit the list of failed items only (empty list ⇒ all vision checks passed). The orchestrator
feeds it to `rhino-repair`, which honors the per-item + global repair budget (C8, conventions
§10). This skill does **not** mutate geometry; it only perceives and reports.

---

## Hard boundaries

- **No measurement here.** Counts / dimensions / positions / symmetry offsets → measure-verify.
- **No geometry mutation here.** Perceive and report only.
- **No oblique camera-solve.** Image compare happens in a clean orthographic view.
- **No absolute-size questions.** Phrase every question on color or relative relation.
- References go exactly one level deep; shared rules live in
  [`../shared/conventions.md`](../shared/conventions.md), never duplicated here.
