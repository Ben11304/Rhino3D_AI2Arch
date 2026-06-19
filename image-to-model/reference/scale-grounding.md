# Scale grounding — turning a dimensionless image into a sized model

An image has **no intrinsic scale**. A photo of a vase is identical whether the vase is
80 mm or 800 mm tall. Scale grounding is the explicit, auditable process of assigning the
model an absolute size — and recording *how sure we are*, because that confidence decides
whether the verify loop may chase absolute dimensions or only scale-invariant ratios (C5).

Canonical rule: [`../../shared/conventions.md`](../../shared/conventions.md) §9. This
reference fills in the priority table and the IR `scale` block it populates.

---

## 1. Priority table — pick the highest available source

Resolve scale by trying sources **top to bottom** and stopping at the first that applies.
Higher rows are more trustworthy and earn higher `confidence`.

| Priority | `value_source`         | Where the size comes from                                   | Typical `confidence` | `overall_height_mm` form |
|---------:|------------------------|-------------------------------------------------------------|----------------------|--------------------------|
| 1        | `stated`               | A real dimension given in the prompt ("this vase is 300 mm tall") | **high**         | single number            |
| 2        | `reference_object`     | A known-size object visible in the image (A4 sheet = 297 mm, a coin, a standard brick, a credit card = 85.6 mm) | **medium** | number or tight range |
| 3        | `metrology_assumption` | A category default assumed from the object class (no scale cue at all) | **low**     | **range** `[min, max]`   |

- **`stated`** is the only source that licenses verifying **absolute** `numeric_checks`.
  This is effectively the text pipeline's case; it rarely happens in a pure image task.
- **`reference_object`** scales off a measurable in-frame object. Carry the residual
  uncertainty (camera tilt, partial occlusion) as a **range** and `confidence: medium`.
  Record exactly which reference and its assumed real size in `provenance`.
- **`metrology_assumption`** is the fallback when the image gives no scale cue: assume the
  category default (e.g. "table vase ~250–350 mm"). This is the **lowest trust** — always a
  **range**, `confidence: low`, and an explicit `assumption` string for audit/override.

---

## 2. Carry scale as a range + confidence, never a bare point

For anything below `stated`, `scale.overall_height_mm` is a **`[min, max]` inclusive range**,
not a single guessed number. The schema allows either a number or a `[min, max]` pair
exactly so the image pipeline can express "somewhere between 250 and 350 mm".

```json
"scale": {
  "value_source": "metrology_assumption",
  "overall_height_mm": [250, 350],
  "confidence": "low",
  "provenance": "no scale cue in image; assumed category default",
  "assumption": "decorative table vase, typical overall height 250-350mm"
}
```

When you must emit concrete geometry, build at the **midpoint** of the range (here 300 mm),
but keep the range and confidence in the IR so a human can override the absolute size later
without touching the topology or the ratios.

---

## 3. Record the assumption for audit and override

Every non-`stated` grounding must leave a trail:

- `scale.provenance` — the cue used ("reference: credit card 85.6 mm wide, lower-left") or
  its absence ("no scale cue in image").
- `scale.assumption` — the category default applied, **required** when
  `value_source = "metrology_assumption"`.

This makes the single most uncertain decision in the whole pipeline a one-line edit: a
reviewer who knows the real vase is 180 mm changes `overall_height_mm` and re-bakes; because
the model was authored from ratios, every proportion stays correct.

---

## 4. The verify consequence — RATIOS only for the image pipeline (C5)

Scale confidence directly selects what the verify loop is allowed to repair against:

- **`confidence: high`** (`stated`) → the verify loop may fire repairs on **absolute**
  `numeric_checks` (e.g. `overall_height == 300 mm ± tol`). This is the text-pipeline case.
- **`confidence: medium` or `low`** (`reference_object` / `metrology_assumption`) → the
  verify loop fires repairs on **scale-invariant `ratio_checks` only** (e.g.
  `height / max_width`, `neck_width / max_width`, `seat_height / overall_height`) plus the
  qualitative `compare_to_reference` silhouette diff. It must **never** repair toward a
  guessed millimeter.

Practically, the image-pipeline IR's `verify` block should be dominated by `ratio_checks`
and `compare_to_reference: true`, and should include `numeric_checks` **only** when
`scale.value_source == "stated"`. See `../examples/vase.json` for a complete low-confidence
example whose `verify` block carries `ratio_checks` (`height/max_width`) and
`compare_to_reference: true`, and no absolute `numeric_checks`.

---

## 5. Quick decision flow

1. Did the prompt state a real dimension? → `stated`, high, single number, verify absolutes.
2. Else, is a known-size object in frame? → `reference_object`, medium, range, verify ratios.
3. Else → `metrology_assumption`, low, range, explicit `assumption`, verify ratios only.

In all cases the discrete structure and continuous proportions are fixed *before* scale is
grounded (conditional factorization), so the scale decision can never corrupt them.
