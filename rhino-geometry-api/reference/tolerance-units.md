# Tolerance & units

Read tolerances **live** from the document; set the unit system **before** any
geometry exists. Conventions §1 in
[`../../shared/conventions.md`](../../shared/conventions.md) is the source of
truth — this file is the per-unit detail.

---

## Read, never hardcode

```python
#! python3
import scriptcontext as sc
import Rhino

tol      = sc.doc.ModelAbsoluteTolerance        # absolute length tolerance, in model units
ang_tol  = sc.doc.ModelAngleToleranceRadians    # angular tolerance, in radians
rel_tol  = sc.doc.ModelRelativeTolerance        # relative tolerance (fraction), occasionally needed
unit_sys = sc.doc.ModelUnitSystem               # Rhino.UnitSystem enum (Millimeters, ...)
```

- **Never** bake a literal like `0.001` into geometry code. The active document
  owns the tolerance; a script that hardcodes one will be wrong in any other unit
  system or document setting.
- Pass `tol` into every `Brep.Create*`, `Brep.JoinBreps`, `Curve.Offset`,
  `Brep.CreateOffset`, every boolean, `CapPlanarHoles`, and every
  `GetNakedEdges`-style closure check.
- Pass `ang_tol` where an angle tolerance is accepted (`SweepOneRail`,
  `NurbsSurface.CreateNetworkSurface`, continuity tests).

---

## Setting the unit system — before any geometry

The IR `units` field must be applied to the document **first**, before creating
geometry, or existing objects get rescaled/misinterpreted.

```python
#! python3
import scriptcontext as sc
import Rhino

# Set BEFORE creating any geometry. scale=False keeps numbers, changes the label;
# scale=True converts existing geometry. On an empty doc either is fine.
sc.doc.ModelUnitSystem = Rhino.UnitSystem.Millimeters   # or Centimeters / Meters / Inches / Feet
# After changing units, re-read tolerances — they are in the NEW units:
tol     = sc.doc.ModelAbsoluteTolerance
ang_tol = sc.doc.ModelAngleToleranceRadians
```

`Rhino.UnitSystem` members map to the IR `units` enum:

| IR `units` | `Rhino.UnitSystem` |
|------------|--------------------|
| `mm`       | `Millimeters`      |
| `cm`       | `Centimeters`      |
| `m`        | `Meters`           |
| `in`       | `Inches`           |
| `ft`       | `Feet`             |

Note: `scale.overall_height_mm` in the IR is **always millimetres**, independent
of the document unit system. Convert it to model units before comparing to a
measured `height`:

```python
mm_per_unit = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
height_in_model_units = overall_height_mm / mm_per_unit[ir_units]
```

---

## Sensible tolerances per unit

A tolerance must be **tight enough that distinct features stay distinct, loose
enough that joins/booleans close.** Roughly 1/1000 of the smallest meaningful
feature. Defaults Rhino ships, and safe values for this suite:

| Unit | `ModelAbsoluteTolerance` (typical) | Rationale |
|------|------------------------------------|-----------|
| `mm` | `0.001` – `0.01`                   | furniture/product scale; 0.01 mm closes booleans without merging real 0.5 mm penetrations (C3) |
| `cm` | `0.0001` – `0.001`                 | same physical precision, 1/10 the mm number |
| `m`  | `0.000001` – `0.0001`              | architectural scale; keep ~0.1 mm physical |
| `in` | `0.001` (inch)                     | ~0.025 mm; standard imperial CAD tolerance |
| `ft` | `0.0001` (foot)                    | ~0.03 mm; keep physical precision constant |

`ModelAngleToleranceRadians` default is ~`0.0174533` rad (1°); leave it at the
document value unless a continuity test needs it tighter.

**Interaction with the C3 interpenetration rule:** penetration depths are
**0.5–2 mm**. The tolerance must be at least an order of magnitude **smaller**
than the penetration (e.g. `tol = 0.01 mm` vs `penetration = 0.5 mm`), or the
boolean treats the overlap as noise and drops the part. If a union silently loses
a part (C2), check that `tol << penetration` before anything else.

---

## Checklist

- [ ] Unit system set from IR `units` **before** any geometry.
- [ ] `tol`/`ang_tol` read live after the unit set, never hardcoded.
- [ ] `tol` passed to every `Create*`/`Join`/`Offset`/boolean/cap call.
- [ ] `overall_height_mm` converted to model units before numeric comparison.
- [ ] `tol` at least 10× smaller than the smallest C3 penetration depth.
