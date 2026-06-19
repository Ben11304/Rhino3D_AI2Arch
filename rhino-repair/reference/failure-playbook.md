# Failure Playbook — operation-specific symptom → cause → fix

Concrete repairs for the geometry operations that fail most. Each entry is
**symptom** (what you observe) → **cause** (why it happened) → **fix** (the smallest
correct change). Apply the matching fix from the appropriate triage tier in
[../SKILL.md](../SKILL.md); honor the shared rules in
[../../shared/conventions.md](../../shared/conventions.md).

Conventions used below:

```python
#! python3
import scriptcontext as sc
import Rhino
tol     = sc.doc.ModelAbsoluteTolerance       # never hardcode 0.001
ang_tol = sc.doc.ModelAngleToleranceRadians
```

All measurements that prove a fix go through `analyze_objects` / bbox math
(`VolumeMassProperties.Compute`, `Brep.GetBoundingBox`), never vision (C4). Topology
("is it one connected solid?", "are all parts present?") may go to colored-part vision.

---

## 1. Loft — twisting or seam jump

**Symptom.** `Brep.CreateFromLoft` returns a Brep that is pinched, hour-glassed, or has a
visible diagonal seam crossing the surface; the rendered solid looks twisted between sections.

**Cause.** The section curves disagree on (a) **start point / seam position** and/or
(b) **parametric direction**. Loft connects section *i*'s start to section *i+1*'s start; if
one closed curve seams at 12 o'clock and the next at 6 o'clock, or one runs CW and the next
CCW, the surface twists. This is a pre-flight INPUT defect (C7), not a result defect — naive
`IsValid` passes.

**Fix.** Align seams and directions *before* lofting:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import Brep, Point3d, LoftType
tol = sc.doc.ModelAbsoluteTolerance

def align_sections(curves):
    base = curves[0]
    # 1. unify parametric direction: dot of start tangents must be positive
    base_t = base.TangentAtStart
    for c in curves[1:]:
        if c.TangentAtStart * base_t < 0:
            c.Reverse()
    # 2. align seams: move each closed curve's seam to the param closest to base's start point
    base_start = base.PointAtStart
    for c in curves:
        if c.IsClosed:
            ok, t = c.ClosestPoint(base_start)
            if ok:
                c.ChangeClosedCurveSeam(t)   # rotate seam to t
                c.SetStartPoint(c.PointAt(t)) # snap exact start (open-form safety)
    return curves

sections = align_sections(sections)
res = Brep.CreateFromLoft(sections, Point3d.Unset, Point3d.Unset, LoftType.Normal, False)
if not res or res.Count == 0:
    raise RuntimeError("loft returned nothing after seam/direction alignment")
brep = res[0]
```

`Curve.SetStartPoint` repositions an open curve's start; `Curve.ChangeClosedCurveSeam(t)`
rotates a closed curve's seam to parameter `t`. Use `ClosestPoint` to pick `t` consistently
across all sections so every seam lines up. If twist persists, try
`LoftType.Straight`/`LoftType.Loose`, or insert an intermediate section to constrain the run.

---

## 2. Sweep1 — rail continuity / kinks

**Symptom.** `Brep.CreateFromSweep` (sweep1) yields a wrinkled or self-overlapping surface
near a corner, or returns an empty array.

**Cause.** The rail is only **G0** (positional) continuous — a sharp kink where two segments
meet — so the frame the profile rides flips orientation at the corner. Sweep1 needs a rail
that is at least **G1** (tangent-continuous) for a clean surface (C7). A polyline rail or a
joined curve with hard corners is the usual culprit.

**Fix.** Detect the kink and either fillet the rail or split-and-sweep per smooth span:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import Brep, Curve
tol     = sc.doc.ModelAbsoluteTolerance
ang_tol = sc.doc.ModelAngleToleranceRadians

# Find G1 discontinuities along the rail.
kinks = []
t = rail.Domain.Min
while True:
    ok, t = rail.GetNextDiscontinuity(Rhino.Geometry.Continuity.G1_continuous,
                                      t, rail.Domain.Max)
    if not ok:
        break
    kinks.append(t)

if kinks:
    # Option A: round the corners so the whole rail is G1, then sweep once.
    r = min(small_radius_below_min_edge, 0.0)  # choose radius < min local edge (see fillet entry)
    filleted = Curve.CreateFilletCornersCurve(rail, fillet_radius, tol, ang_tol)
    rail = filleted or rail
    # Option B (if filleting changes design intent): split at kinks, sweep each span, join.
sweep = Rhino.Geometry.SweepOneRail()
sweep.AngleToleranceRadians = ang_tol
sweep.SweepTolerance = tol
breps = sweep.PerformSweep(rail, profile_curve)
if not breps or breps.Count == 0:
    raise RuntimeError("sweep1 returned nothing; rail likely still not G1")
```

`Curve.GetNextDiscontinuity(Continuity.G1_continuous, ...)` walks the rail reporting every
tangent break. `Curve.CreateFilletCornersCurve` rounds them. If the profile must stay
perpendicular, set the sweep's roadlike frame; if the rail self-intersects, that is a design
defect to surface, not a repair.

---

## 3. Revolve — open surface (not a solid)

**Symptom.** After `RevSurface.Create` the result has naked edges / `IsSolid` is False, or
the later `CapPlanarHoles`/shell step fails.

**Cause.** Two classic violations of C6/C7:
(a) the **profile does not start AND end on the revolve axis** (and is not closed), so the
revolved surface is an open tube, not a closed solid; and/or
(b) the **axis is not coplanar with the profile**, so the revolve sweeps a degenerate shape.
Remember `RevSurface.Create` returns a **Surface, not a solid** — it must be wrapped and
capped.

**Fix.** Pre-flight coplanarity and on-axis endpoints, then cap:

```python
#! python3
import math
import scriptcontext as sc
import Rhino
from Rhino.Geometry import RevSurface, Brep, Line, Point3d, Vector3d, Plane
tol = sc.doc.ModelAbsoluteTolerance

axis = Line(Point3d(*axis_p0), Point3d(*axis_p1))

# (a) endpoints on axis (unless the profile is closed)
def dist_point_to_line(pt, ln):
    cp = ln.ClosestPoint(pt, False)
    return cp.DistanceTo(pt)

if not profile.IsClosed:
    if dist_point_to_line(profile.PointAtStart, axis) > tol:
        profile.SetStartPoint(axis.ClosestPoint(profile.PointAtStart, False))
    if dist_point_to_line(profile.PointAtEnd, axis) > tol:
        profile.SetEndPoint(axis.ClosestPoint(profile.PointAtEnd, False))

# (b) coplanarity: profile plane must contain the axis direction
got_plane, prof_plane = profile.TryGetPlane(tol)
if got_plane:
    axis_dir = Vector3d(axis.To - axis.From)
    if abs(prof_plane.Normal * axis_dir) > sc.doc.ModelAngleToleranceRadians:
        raise RuntimeError("revolve axis not coplanar with profile; rebuild profile in axis plane")

rev  = RevSurface.Create(profile, axis, 0.0, math.radians(angle_deg))
brep = Brep.CreateFromRevSurface(rev, False, False)
brep = brep.CapPlanarHoles(tol) or brep
if not brep.IsSolid:
    raise RuntimeError("revolve still open after cap; check profile closure / on-axis ends")
```

`Curve.SetStartPoint` / `SetEndPoint` snap the generatrix ends exactly onto the axis line.
`Line.ClosestPoint(pt, False)` gives the foot of the perpendicular. Cap **before** any shell
(`Brep.CreateOffset(..., solid=True)` needs a closed Brep — C6).

---

## 4. Boolean union/difference — coplanar contact, non-solid input, or partial union

### 4a. Coplanar / coincident contact

**Symptom.** `Brep.CreateBooleanUnion` returns the inputs unchanged, returns `null`, or
produces a result with naked edges at the seam.

**Cause.** The two solids touch on an **exactly coincident / coplanar face** instead of
overlapping. A zero-thickness contact is degenerate — the union has no volume to merge
through (C3).

**Fix.** Push the mating part into its neighbour by the IR `penetration` depth (0.5–2 mm)
along the contact normal *before* the union:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import Brep, Vector3d, Transform, VolumeMassProperties
tol = sc.doc.ModelAbsoluteTolerance

pen = penetration_mm          # from IR relation {"type":"interpenetrate","penetration":...}
move = Vector3d(contact_normal)
move.Unitize()
moving_brep.Transform(Transform.Translation(move * pen))   # interpenetrate, do not just touch

res = Brep.CreateBooleanUnion([brep_a, moving_brep], tol)
if res is None or len(res) == 0:
    raise RuntimeError("union failed even after interpenetration; check for non-manifold contact")
```

### 4b. Non-solid input

**Symptom.** Boolean returns nothing; inputs report `IsSolid == False`.

**Cause.** One operand is an open Brep (un-capped extrude/loft/revolve). Booleans need closed,
solid, correctly-oriented Breps.

**Fix.** Close each input first, then verify before the boolean:

```python
for b in (brep_a, brep_b):
    if not b.IsSolid:
        capped = b.CapPlanarHoles(tol)
        if capped:
            b = capped
    if not b.IsSolid:
        raise RuntimeError("boolean input still not solid after cap")
```

### 4c. Partial union (the silent killer — C2)

**Symptom.** The boolean returns a **valid, solid** Brep — but a part is missing (the classic
"valid 3-legged chair": one leg silently dropped). `IsValid` and `IsSolid` both pass.

**Cause.** One pair failed to merge (often a near-coincident or non-manifold contact, 4a) and
the operation silently skipped it, returning a valid Brep minus that piece.

**Fix.** Never trust `IsValid`/`IsSolid` for a boolean. Check **expected solid count** AND
**total volume** against the IR:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import VolumeMassProperties
tol = sc.doc.ModelAbsoluteTolerance

def verify_boolean(result_breps, expected_count, expected_volume, vtol):
    if len(result_breps) != expected_count:
        raise RuntimeError("boolean solid count %d != expected %d"
                           % (len(result_breps), expected_count))
    vol = sum(VolumeMassProperties.Compute(b).Volume for b in result_breps)
    if abs(vol - expected_volume) > vtol:
        raise RuntimeError("boolean volume %.3f != expected %.3f (tol %.3f) -- a part was dropped"
                           % (vol, expected_volume, vtol))
    return True
```

If the count/volume check fails, the repair is to re-apply 4a (add interpenetration to the
dropped pair) and union again — not to accept the valid-but-wrong result.

---

## 5. Fillet — radius too large

**Symptom.** `Brep.CreateFilletEdges` / `Curve.CreateFilletCornersCurve` returns empty or a
self-intersecting result; a Brep fillet drops the edge silently.

**Cause.** The requested **radius is >= the shortest local edge / smallest adjacent face
dimension**. There is no room to inscribe the arc, so the fillet self-overlaps or is rejected
(C7: fillet radius must be < min local edge).

**Fix.** Measure the minimum adjacent edge length and clamp the radius below it:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import Brep
tol = sc.doc.ModelAbsoluteTolerance

def min_edge_length(brep):
    return min(e.GetLength() for e in brep.Edges)

r = requested_radius
m = min_edge_length(brep)
if r >= m:
    r = 0.45 * m                 # safely inside the smallest edge
edge_indices = target_edge_indices
radii = [r] * len(edge_indices)
res = Brep.CreateFilletEdges(brep, edge_indices, radii, radii,
                             Rhino.Geometry.BlendType.Fillet,
                             Rhino.Geometry.RailType.RollingBall, tol)
if not res or res.Count == 0:
    raise RuntimeError("fillet failed; radius %.3f still too large for min edge %.3f" % (r, m))
```

If even a clamped radius fails on one edge, fillet the edges in descending-radius batches and
surface the edge that cannot be filleted rather than silently dropping it.

---

## 6. Offset — self-intersection

**Symptom.** `Curve.Offset` returns multiple disjoint fragments or a kinked, looped curve;
`Brep.CreateOffset` produces naked edges or fails.

**Cause.** The **offset distance exceeds the smallest local feature / radius of curvature**,
so the offset collapses through itself in concave regions and forms loops (C7: offset distance
< min feature size).

**Fix.** Clamp distance below the minimum feature size; for curves, offset then prune loops:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import Curve, Point3d, Vector3d, Plane, CurveOffsetCornerStyle
tol = sc.doc.ModelAbsoluteTolerance

d = requested_offset
if d >= min_feature_size:
    d = 0.9 * min_feature_size
pieces = curve.Offset(Plane.WorldXY, d, tol, CurveOffsetCornerStyle.Sharp)
if not pieces or len(pieces) == 0:
    raise RuntimeError("offset returned nothing; distance %.3f exceeds local curvature" % d)
# self-intersection check: a clean offset is a single curve
if len(pieces) > 1:
    # keep the longest fragment, or reduce d and retry within the per-item budget
    pieces = sorted(pieces, key=lambda c: c.GetLength(), reverse=True)
result = pieces[0]
xs = Rhino.Geometry.Intersect.Intersection.CurveSelf(result, tol)
if xs and xs.Count > 0:
    raise RuntimeError("offset self-intersects (%d crossings); reduce distance" % xs.Count)
```

`Intersection.CurveSelf` reports self-crossings. For a solid shell, use the revolve/shell
sequence in [../../shared/conventions.md](../../shared/conventions.md) §7 with a thickness
smaller than the wall's minimum feature size.

---

## 7. Network surface — grid mismatch

**Symptom.** `NurbsSurface.CreateNetworkSurface` returns `null` or a wildly rippled surface,
or its error enum is non-zero.

**Cause.** The input curves do not form a clean **U×V grid**: a U-curve does not actually
cross every V-curve (or crosses twice), curves are out of order, or U and V sets are mixed
into one list. Network surfacing needs each curve in one direction to intersect each curve in
the other within tolerance.

**Fix.** Separate U from V, order them, verify intersections, then build:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import NurbsSurface
from Rhino.Geometry.Intersect import Intersection
tol     = sc.doc.ModelAbsoluteTolerance
ang_tol = sc.doc.ModelAngleToleranceRadians

# Pre-flight: every U curve must cross every V curve exactly once within tol.
for u in u_curves:
    for v in v_curves:
        ev = Intersection.CurveCurve(u, v, tol, tol)
        if ev is None or ev.Count == 0:
            raise RuntimeError("network grid gap: a U curve misses a V curve; fix grid before surfacing")

# CreateNetworkSurface(curves, continuity, edgeTol, interiorTol, angleTol) -> (surface, error_int)
all_curves = list(u_curves) + list(v_curves)
srf, err = NurbsSurface.CreateNetworkSurface(all_curves, 1, tol, tol, ang_tol)
if srf is None or err != 0:
    raise RuntimeError("network surface failed, error code %s; check U/V ordering and crossings" % err)
```

`Intersection.CurveCurve` confirms each crossing exists. The `CreateNetworkSurface` overload
returns a `(surface, error)` tuple — a non-zero error code names the grid problem; treat it as
a Tier-1 input fix, not a numeric one. This op has **no typed MCP tool**, so it runs through
`execute_rhinocommon_csharp_code` / `execute_rhinoscript_python_code` — mind the
[lamcp-dotnet-traps.md](lamcp-dotnet-traps.md).

---

## 8. Connectivity — part A does not reach support B (gap > tol)

**Symptom.** The Phase-6 connectivity sweep (conventions §13/C9, the `check_connectivity --stage`
operation owned by `rhino-scene-state`) returns an `out_of_band` entry for an edge: a baluster top
**12 mm short** of the rail (`lands_on`, `+12 mm`), a column **floating above the floor**
(`lands_on`/`on_top_of`, positive gap), or an arch **sunk into** a column top (`on_top_of`, negative
gap). Every individual part passed `IsValid`/`IsSolid`/count/volume — §1–§7 say nothing about whether
parts actually **touch**. This is the dominant false-confidence failure C9 exists to catch, and it is
detected **numerically against the live solids by GUID** (A1), never by eye.

**Cause.** The attach geometry was built to the wrong literal: a guessed Z instead of the support's
**published** level, or — for a rising/helical support — a single global Z instead of the support's
**Z-at-arc-length at A's own angle**. The realized gap between the two live solids is therefore outside
the relation's per-type band (A3). A `+gap` means A stops short of B; a `-gap` on an `on_top_of` means A
penetrates B (also a FAIL — `on_top_of` band is `[0,+tol]`).

**Fix.** Read the **measured** gap from the sweep's violation record (it already carries the signed
`gap` and the `measured_between:[guidA,guidB]`), then close it by moving/recomputing **A** and re-baking
**A's stage only**. Two equivalent routes, prefer the resolver route (PREVENT) so the fix is principled,
not a one-off nudge:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import Vector3d, Transform

tol = sc.doc.ModelAbsoluteTolerance

def support_z(support, angle_deg=None):
    """Resolve B's published support level (conventions §13 PREVENT / build-plan support law).
    helix_z: Z(theta) = base_z + pitch*((theta - start_angle)/360)  -- A4 curved support."""
    kind = support["kind"]
    if kind in ("plane_z", "top_z", "base_z", "z_at_angle"):
        if kind == "z_at_angle" and "helix" in support:   # rising support sampled AT A's own angle
            h = support["helix"]
            return h["base_z"] + h["pitch"] * ((angle_deg - h.get("start_angle", 0.0)) / 360.0)
        return support["value"]
    if kind == "helix_z":
        h = support["helix"]
        return h["base_z"] + h["pitch"] * ((angle_deg - h.get("start_angle", 0.0)) / 360.0)
    raise ValueError("unknown support kind %r" % kind)

def close_gap_translate(guid_a, measured_gap, axis=Vector3d(0, 0, 1)):
    """Route 1 (direct): translate A by the measured signed gap toward its support along 'axis'.
    measured_gap > 0 => A is short (move toward B); < 0 => A penetrates (move away)."""
    obj = sc.doc.Objects.FindId(guid_a)
    geo = obj.Geometry.Duplicate()
    v = Vector3d(axis); v.Unitize()
    geo.Transform(Transform.Translation(v * measured_gap))   # +gap closes a short, -gap backs out a sink
    sc.doc.Objects.Replace(guid_a, geo)
    return guid_a
```

**Route 2 (preferred — recompute A.top via the resolver):** instead of nudging by the residual, rebuild
A so its attach end lands exactly on `support_z(B.support, angle_deg=θ_A)`. This re-derives the literal
from B's **pinned, published** support (§5a DIRECTION-PIN: B's published level is the pinned value, not
the curve-normal value), so A is built to reach B rather than patched after the fact. Then re-emit **A's
stage only** (conventions §12 scoped-idempotent purge+rebuild), so no other stage is disturbed and the
count never doubles.

**Helical / rising-support case (A4).** When B is a helical rail (or any rotated/curved support), there
is no single "top Z" — the support height is `Z(θ) = base_z + pitch*((θ - start_angle)/360)`. Recompute
A's attach Z at **A's own angle θ_A** (the angle of this array member, e.g. `value_ref {part:"rail",
of:"z_at_angle", at:θ_A}`), never a global Z. For a radial/helical family fix the flagged member at its
angle, then on the re-emit checkpoint re-measure a **sample** (first/middle/last + every previously
flagged member, B2); re-measure the full N only if the `array` rule itself changed.

**After the fix — re-run the sweep, and invalidate crossing stages (C1).** Re-baking A's stage changes
A's GUID (not its part_id), so re-run `check_connectivity --stage <A's stage>`; it re-resolves the
part_id-keyed edge to the new GUID and must come back **green** (gap in band). If the fix moved a
**support** that other stages attach to, set `connectivity_status:"not_run"` on **every** stage with an
edge crossing into the re-emitted stage and re-run their sweeps too (C1) — moving the rail can re-open
the gap on every baluster even though those stages were untouched.

**`uncovered`, not `out_of_band`.** If the sweep returns `uncovered` (a declared contact with no
measurement, a non-floating part with no declared contact, or an edge into a now-deleted part_id, C2),
the defect is **completeness**, not geometry: add the missing declared relation to the IR so the contact
is actually built and measured (PREVENT), or mark the part `floating:true` if it genuinely floats (F).
**Never** "fix" an `uncovered` by deleting the edge — that re-creates the false-pass-by-omission C2
exists to forbid. Repairs here obey the same per-item (N=3) + global (12) budget as every other entry
(C8); a connectivity defect that cannot be closed is **surfaced**, never hidden.
