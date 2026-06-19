# verification.md — binary-question generation, colored capture, numeric-vs-vision split, silhouette compare

This is the working recipe for the `render-and-look` skill. It expands the procedure in
[`../SKILL.md`](../SKILL.md). Canonical rules (vision-demotion C4, ratio-vs-absolute C5, token
economy, GUID ledger) live in [`../../shared/conventions.md`](../../shared/conventions.md) and are
**not** duplicated here. The IR shape is
[`../../shared/build-plan.schema.json`](../../shared/build-plan.schema.json).

---

## 1. Numeric-vs-vision split table (apply BEFORE writing any question)

The first job is to *throw away* every candidate question that a number could answer and send it
to **measure-verify** (`rhino-scene-state`). Vision keeps only shape/likeness/relative-color.

| Candidate judgment                                   | Route to        | Why |
|------------------------------------------------------|-----------------|-----|
| How many legs / solids / holes (a count)             | **measure-verify** (`solid_count`) | counting in a render is unreliable; bbox/analyze is exact |
| Seat height / overall height / any length in mm/cm   | **measure-verify** (`numeric_checks`) | vision cannot read absolute size (C4) |
| Distance / gap / offset between two parts            | **measure-verify** (`numeric_checks`) | measurable → math |
| Realized gap where two parts must touch (connectivity) | **measure-verify** (`rhino-scene-state` §13/C9 sweep) | the realized solid-to-solid gap is the authoritative verdict; vision is advisory only (see §6) |
| Symmetry *offset* equal on both sides (a number)     | **measure-verify** (`ratio_checks`) | measurable → math |
| Total volume / does a boolean keep volume            | **measure-verify** (`total_volume`) | partial-boolean guard is numeric (C2) |
| Angle of a taper / tilt in degrees                   | **measure-verify** (`numeric_checks`) | measurable → math |
| Does the leg taper read as conical (a *shape*)       | **vision** (this skill) | profile-shape fidelity |
| Does the back read as curved vs flat (a *shape*)     | **vision** | profile-shape fidelity |
| Does the whole thing read as a <wine glass> (likeness) | **vision** | qualitative "looks like X" |
| Is the **red** seat above the **blue** legs (relative position by color) | **vision** | color + above/below is robust |
| Does the left half mirror the right (qualitative)    | **vision** | mirror *appearance*, not measured offset |
| Does the rendered silhouette match the reference proportionally | **vision** (image pipeline, ratios only, C5) | shape overlap, scale-invariant |

Rule of thumb: **if the answer is a number, it is not a vision question.** If you can only answer
it by squinting at relative shape or color, it belongs here.

---

## 2. Binary-question generation recipe (from the IR)

Generate **2-5** questions, **Yes/No/Unclear**, sourced from the IR in this priority order. Stop at
5; prefer the most discriminating questions (ones most likely to expose a real defect).

### Source A — `verify.binary_questions` (use verbatim)
These are pre-authored for color/relative-position phrasing. Emit each as-is. Example IR:
```json
"verify": { "binary_questions": [
  "is the red seat above the blue legs?",
  "do the four legs splay outward toward the floor?"
] }
```
→ ask both verbatim.

### Source B — profile operations → one profile-fidelity question each
For every part whose `operation` is `loft`, `revolve`, `sweep1`, or whose `provenance` names a
distinctive profile, author **one** shape-fidelity question. Name the part by its render **color**.
- revolve generatrix of a stem → "does the **green** stem read as a slender turned profile?"
- loft through widening sections (a bowl) → "does the **red** bowl flare smoothly from base to rim?"
- sweep1 along a curved rail (a handle) → "does the **yellow** handle read as one smooth arc?"

### Source C — `compare_to_reference: true` → silhouette question (see §4)
"does the model silhouette read as the reference <object> in proportion?"

### Source D — `symmetry` → one qualitative mirror question (never a measured offset)
For a `mirror` symmetry: "does the left half visually mirror the right half?" For `rotational`
with `count`: "are the <count> arms evenly spaced around the center?" (qualitative even-ness only —
the *measured* spacing is measure-verify's `ratio_checks`).

### Source E — contact `relations` → one SOFT, advisory-only connectivity question (NEVER fails the build)
For a contact relation in the IR (`on_top_of`, `lands_on`, `meets`, `spans`, `spans_between`,
`coincident`, `interpenetrate` — conventions §13/C9), author **one** colored "do they look joined?"
question, e.g. "do the **red** balusters look joined to the **blue** rail where they meet?",
"does the **green** column look seated on the **yellow** floor (no floating gap)?", "does the
**orange** arch look to rest on the **violet** column top?". This is the **lowest-priority** source
and is strictly **ADVISORY** — see §6. It exists only to give a human-legible heads-up alongside the
numeric verdict; it is **never** the source of a repair item by itself.

### Phrasing constraints (enforce on every emitted question)
- Names a **color** or a **relative relation** (above/below, inside/outside, left/right), never an
  absolute number or unit.
- Answerable from the colored renders alone, from one or two of the four named views.
- Single-clause; one defect per question so a `No` maps to exactly one `part_id`.

### Answering + CADCodeVerify discipline
Answer each **Yes / No / Unclear**. **Only `No` and `Unclear` become repair items**; `Yes` is
dropped. Correct parts are never re-touched ⇒ the loop is monotone (fixes only, never regresses).
Each repair item carries `{ part_id, question, verdict, hint }` where `part_id` is resolved through
the scene-graph (GUID ledger, conventions §2) and `hint` says what looked wrong.

---

## 3. Color-per-part capture protocol (C4 reliability)

Color makes relative-position and per-part shape questions reliable. Protocol:

1. **Stable palette keyed by `part_id`.** Use a fixed, high-contrast, color-blind-safe palette and
   pick the color by hashing the `part_id`, so the same part is the same color every iteration:

   | slot | name   | RGB           |
   |------|--------|---------------|
   | 0    | red    | (220, 50, 47) |
   | 1    | blue   | (38, 139, 210)|
   | 2    | green  | (133, 153, 0) |
   | 3    | yellow | (181, 137, 0) |
   | 4    | magenta| (211, 54, 130)|
   | 5    | cyan   | (42, 161, 152)|
   | 6    | orange | (203, 75, 22) |
   | 7    | violet | (108, 113, 196)|

2. For each baked object, resolve its GUID from the scene-graph, then set color **from object**:
   ```python
   #! python3
   import scriptcontext as sc
   import Rhino
   from System.Drawing import Color
   from Rhino.DocObjects import ObjectColorSource

   def color_part(guid, rgb):
       obj  = sc.doc.Objects.FindId(guid)
       attr = obj.Attributes
       attr.ColorSource = ObjectColorSource.ColorFromObject
       attr.ObjectColor = Color.FromArgb(*rgb)
       sc.doc.Objects.ModifyAttributes(obj, attr, True)
   ```
3. **Record** each part's color in the repair list so question phrasing and answers agree on which
   color is which part.
4. **Restore after capture** is optional — color is non-identifying (identity is the `part_id`
   UserString, conventions §3). If the orchestrator wants the original look back, snapshot
   `ColorSource`/`ObjectColor` before step 2 and re-apply after the captures.

Run captures with `capture_viewport` at low resolution (≈512 px) per token economy (§11).

---

## 4. Silhouette-compare procedure for image-derived models (C5)

Used only when the IR has `scale.value_source` ∈ {`reference_object`, `metrology_assumption`} (the
image pipeline) and `verify.compare_to_reference` is true.

### Why orthographic only
There is **no camera-solve** in this suite. An oblique reference photo cannot be reproduced by the
Rhino camera without solving its pose, which we do not do. So compare in a **clean canonical
orthographic** view (`front` or `right`, whichever axis best matches the reference framing) and
judge **proportion**, not a pixel-exact overlay of an oblique pose.

### Compare RATIOS, never absolute pixels (C5)
1. Extract the model silhouette from the chosen ortho capture (the colored solid against a plain
   background) and the reference silhouette (already produced by `image-to-model`'s
   `EXTRACT_PROFILE`).
2. **Normalize both to the same bounding height** (scale-invariant). Absolute size is untrusted for
   image-derived models, so only normalized proportions may drive a verdict.
3. Compare a few **scale-invariant ratios** that define the archetype, e.g. for a wine glass:
   - bowl_max_width / total_height,
   - stem_length / total_height,
   - foot_width / bowl_max_width.
   For a chair: seat_depth / total_height, back_height / seat_height (as *ratios*).
4. Verdict is **Yes/No/Unclear** on "do the proportions match the reference within a loose band?".
   These ratio judgments are the **only** repairs the image pipeline may fire (C5); never repair an
   image-derived model against an absolute numeric check.

### Low-confidence extraction → archetype fallback
If `EXTRACT_PROFILE` flagged the silhouette low-confidence (perspective skew not cancelled, partial
occlusion), do **not** trust pixel overlap. Instead ask the archetype-likeness question — "does it
read as the archetype <wine glass>?" — and let the render-vs-reference loop pick the archetype
profile, per conventions §9. Average left+right silhouettes about the axis before any ratio is read,
so perspective skew cancels.

---

## 5. Output contract (what render-and-look returns)

A JSON differential repair list of failed vision items only:
```json
[
  { "part_id": "back_panel", "question": "does the yellow back read as curved?",
    "verdict": "no", "hint": "back reads flat in front+iso; IR back_panel is a lofted curve" },
  { "part_id": "bowl", "question": "do the model proportions match the reference?",
    "verdict": "unclear", "hint": "bowl/height ratio looks shallow vs reference; low-confidence extract" }
]
```
An **empty list** means all vision checks passed. The orchestrator routes this to `rhino-repair`,
which enforces the per-item + global repair budget (C8, conventions §10). This skill never mutates
geometry and never measures — measurement is measure-verify's (`rhino-scene-state`) job.

---

## 6. The connectivity vision question is SOFT / ADVISORY ONLY (C4 vision-demotion preserved)

Connectivity — "do the two parts actually touch where they must?" — is a **measurable gap**, so by the
vision-demotion rule (C4) the **authoritative** verdict is the numeric realized solid-to-solid sweep
owned by `rhino-scene-state` (conventions §13/C9): the per-stage `check_connectivity` sweep measures
the gap by GUID and judges it against the per-relation-type band. Vision **cannot** read a 12 mm gap
reliably, so the Source-E "do the red and blue parts look joined?" question is kept deliberately weak:

- **It can NEVER fail the build.** A `no`/`unclear` on a Source-E connectivity question does **NOT**
  emit a repair item and does **NOT** gate the stage. Only the numeric C9 verdict (`out_of_band` /
  `uncovered`) can fail connectivity. This preserves C4: a number-answerable judgment is never decided
  by squinting at a render.
- **Its only job is corroboration.** Report the Source-E answer **separately**, as an advisory note
  attached to the stage, so a human reading the report sees the eye-check next to the authoritative
  number. If vision says "looks joined" but the numeric sweep says `out_of_band`, **the numeric verdict
  wins** and the stage fails; if vision says "looks gappy" but the sweep says green, the build still
  passes (the advisory note is surfaced for a human sanity-check, but it cannot override the measurement).
- **Output channel is separate from the repair list.** Source-E answers go in an `advisory` block,
  never in the §5 differential repair list, so they can never become repair items:

```json
{
  "repairs": [ /* §5 vision repair items — Source A–D, these DO drive repair */ ],
  "advisory": [
    { "relation": "lands_on", "from": "baluster_7", "to": "rail",
      "question": "do the red balusters look joined to the blue rail where they meet?",
      "verdict": "unclear",
      "note": "ADVISORY ONLY — authoritative verdict is rhino-scene-state §13/C9 numeric sweep; this never fails the build" }
  ]
}
```

The orchestrator treats `advisory[]` as a human-readable corroboration of the §13 sweep, not as a gate.
This is the vision side of the triad: PREVENT and ENFORCE are numeric; DETECT is numeric (the §13
sweep); vision only *advises*.
