# Vision analysis — reading structure, symmetry, and profile from a reference image

This reference governs the **vision pass** of the image pipeline: how to extract object
class/archetype, available views, symmetry planes, profile/silhouette curves, and — most
importantly — how to **detect symmetry-breaking features before factorizing** so a single
spout or off-center handle is never mirrored onto both sides.

Canonical rules live in [`../../shared/conventions.md`](../../shared/conventions.md). The
vision-demotion rule (**C4**) and the ratio-vs-absolute rule (**C5**) are binding here:
vision reads *structure, symmetry, and shape fidelity*; it never *measures*. Anything
countable, dimensionable, or positional is confirmed later by `analyze_objects` / bbox math.

---

## 1. Identify the object class and pick an archetype

The first vision question is **"what is this?"**, answered as an object class plus a
**parametric archetype** — a reusable construction template that seeds the discrete
factorization and the fallback profiles.

| Object class            | Archetype                              | Build operation(s)                  |
|-------------------------|----------------------------------------|-------------------------------------|
| vase / bottle / glass   | solid of revolution                    | `revolve` (+ `shell` for hollow)    |
| mug / teapot / pitcher  | revolved body + applied handle/spout   | `revolve` body, `sweep1`/`pipe` handle, `boolean union` |
| chair / stool / table   | seat/top + legs + (back)               | `box`/`cylinder` parts, instanced legs, `boolean union` |
| bottle-cap / lid / knob | short revolution or `extrude` profile  | `revolve` / `extrude`               |
| lamp / spindle / baluster | stacked solid of revolution          | `revolve` of a multi-lobe profile   |
| picture frame / tray    | extruded closed section                | `extrude` of a profile curve        |

The archetype answers, up front: *is this rotational or bilateral? how many parts? which
parts are instanced (legs)? which parts are applied (handle/spout)?* Record the chosen
archetype in each part's `provenance` field so the IR is auditable.

If the image is ambiguous between archetypes, prefer the one with **rotational symmetry**
when the silhouette is left-right mirror-equal about a vertical axis (a strong revolution
cue), otherwise the bilateral archetype.

---

## 2. Determine which views are present

Classify the camera(s):

- **front** / **side** — near-orthographic elevations; the silhouette is the cleanest
  profile source. Best case for `extract_profile.py`.
- **three-quarter (3-4)** — the most common product shot. Foreshortening skews the
  silhouette; treat extracted widths as **low confidence** and rely on averaging
  left+right about the axis (see §5) plus the render-vs-reference loop.
- **top / plan** — gives the cross-section footprint and instance positions (e.g. where
  four legs sit), not the profile.

When only a single three-quarter view exists, you cannot read true depth. Use symmetry
(§3) to *complete* the unseen geometry rather than inventing it, and flag the whole model
as scale-uncertain (`scale.confidence` ≤ medium).

---

## 3. Detect symmetry — and use it to complete occluded geometry

Two symmetry types drive reconstruction. Detect which one the object has and record it in
the IR `symmetry` array:

- **Rotational** (solid of revolution): the silhouette is mirror-equal about a single
  vertical axis and the object reads as "spun". Encode as
  `{ "type": "rotational", "axis": "WorldZ", "origin": [...] }`. One profile curve plus a
  `revolve` reconstructs the entire surface; no occluded geometry remains.
- **Bilateral / mirror**: one plane (usually `WorldYZ` or `WorldXZ`) splits the object
  into mirror halves (chairs, most furniture, car bodies). Encode as
  `{ "type": "mirror", "plane": "WorldYZ", "origin": [...] }`.

**Symmetry completes occluded geometry.** The unseen back of a bilateral object is the
mirror image of the visible front; author the visible half and produce the other half by
transforming the base part's geometry across the symmetry plane (never by re-typing
coordinates — see conventions §4). For rotational objects, the entire 360° surface is
implied by one profile, so there is no "back" to guess.

For rotational symmetry with a finite repeated feature count (e.g. a 5-lobed base), add
`"count": 5` to the symmetry entry.

---

## 4. Detect symmetry-breaking features BEFORE factorizing

**This is the step that most often goes wrong, so it runs before the part list is built.**

A symmetry-breaking feature is geometry that violates the object's declared symmetry:

- a **single spout** on an otherwise rotational teapot,
- an **off-center / one-sided handle** on a mug or pitcher,
- a **single pour lip** or **asymmetric thumb-rest**,
- one **asymmetric arm** or a side table on only one side of a bilateral object.

Procedure:

1. After choosing the archetype and symmetry (§1–§3), scan the silhouette for any feature
   that appears on **only one side** of the symmetry axis/plane, or that breaks the
   left-right mirror equality.
2. **Subtract these from the symmetric base** in your mental model. The rotational body or
   the mirrored half is built *without* them.
3. Author each symmetry-breaking feature as its **own part** with an explicit `frame`
   (origin + axes) placing it on the correct single side, and **exclude it from the
   mirror/array**. A handle is typically a `sweep1`/`pipe` along a rail, boolean-unioned to
   the body with an `interpenetrate` relation (0.5–2 mm, C3).

> Failure mode this prevents: detecting a handle, then mirroring the whole object across
> `WorldYZ`, which clones the handle onto the opposite side and produces a two-handled mug.
> By isolating the handle *before* the mirror/array, the symmetric operation only ever sees
> the symmetric body.

Record on the feature part `provenance: "symmetry-breaking: single handle, right side"`
so the asymmetry is auditable and a reviewer can confirm it was intentional.

---

## 5. Extract profile / silhouette curves

For revolved and lofted parts, the shape comes from a **profile curve** read off the
silhouette. Do **not** trust a single silhouette edge — perspective skew and foreshortening
bias it.

Use [`../scripts/extract_profile.py`](../scripts/extract_profile.py):

- Input: a JSON list of silhouette boundary points (from edge detection or hand-traced)
  plus the detected axis (two points).
- It **averages the left and right silhouettes about the axis** to cancel perspective skew
  (the `EXTRACT_PROFILE` discipline in conventions §9 / C5).
- It resamples into ordered control points suitable for
  `Rhino.Geometry.Curve.CreateInterpolatedCurve`.
- It **flags low confidence** (large left/right asymmetry, too few points, or a profile
  that does not return to the axis), and when confidence is low it emits a set of
  **fallback archetypal profiles** (e.g. `cylinder`, `ogee_vase`, `bottle`, `bowl`) for the
  render-vs-reference loop to choose among, rather than trusting raw pixel sampling.

The resulting control points become an `interpolated_curve` part. For a solid of revolution
the profile **must start and end on the axis** (C6); the script reports whether the averaged
profile closes to the axis and the executor must snap the first/last control point onto the
axis line within `tol` before calling `RevSurface.Create`.

For lofts (e.g. a tapered body read from several cross-sections), extract one profile per
section, ensure all sections run the **same direction and are seam-aligned** (C7), and list
them in IR order under `sections`.

---

## 6. What vision answers vs. what math answers (C4)

| Question                                                | Answered by      |
|---------------------------------------------------------|------------------|
| What object class / archetype is this?                  | vision           |
| Is this rotational or bilateral symmetric?              | vision           |
| Is there a one-sided handle / spout?                    | vision           |
| Does the rendered silhouette read as the reference?     | vision (compare) |
| How many legs / lobes / parts?                          | `analyze_objects` / bbox count |
| Is the red seat above the blue legs?                    | vision (colored parts) |
| What is `height / max_width`?                           | bbox math        |
| What is the overall height in mm?                       | bbox math (only trustworthy when scale is `stated`) |

Color each part before the capture (conventions §3/§8) so vision is asked the *reliable*
relative question ("is the red part above the blue part?") and never the *unreliable*
metric one ("is it 450 mm?").

---

## 7. Output of the vision pass

The vision pass feeds the IR producer with:

- `object` (plain-language name) and per-part `provenance`,
- the `symmetry` array (rotational vs mirror, with axis/plane + origin),
- the discrete part list with symmetry-breaking features already isolated,
- profile control points (via `extract_profile.py`) for revolved/lofted parts,
- a confidence flag that, combined with scale grounding
  ([`scale-grounding.md`](scale-grounding.md)), sets `scale.confidence` and therefore
  whether the verify loop may use absolute `numeric_checks` or `ratio_checks` only (C5).
