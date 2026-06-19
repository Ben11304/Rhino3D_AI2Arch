# Per-operation cheat-sheets (pre-flight + post-check)

Conventions are in [`../../shared/conventions.md`](../../shared/conventions.md).
This file is the per-op detail: the **real** RhinoCommon function, the inputs you
must pre-flight (correction **C7**), the validity/count/volume post-checks
(corrections **C2/C3**), and the concrete fix for each failure mode.

Read `tol = sc.doc.ModelAbsoluteTolerance` and
`ang_tol = sc.doc.ModelAngleToleranceRadians` live; pass them into every
`Create*`/`Join`/boolean call. Never hardcode `0.001`.

**Emit each op inside a SCOPED IDEMPOTENT STAGE** (conventions §12). Do not emit
the whole model as one atomic script. Every `execute_*` body is: *(1) the scoped
purge preamble from
[`../../rhino-scene-state/scripts/stage_emit.py`](../../rhino-scene-state/scripts/stage_emit.py)
(`--stage <id>`), which deletes only this stage's tagged objects → (2) the
per-stage `_stage_before_ids` snapshot → (3) the guarded `Create*`/bake below,
each bake stamping `attr.SetUserString("stage", <id>)`.* This makes a re-run
(including the observed double-execution of the wrapper) converge to one copy of
the stage, and bounds any op failure to its own stage instead of rolling back the
whole build. A fragile op with no available overload (e.g. a `Brep.CreateOffset`
shell) then fails one stage, repaired by re-emitting just that stage — never the
other 900 solids.

A note on silent failure: most of these returns are *arrays that can be empty*
(`Brep[]`, `Curve[]`) or *single objects that can be `None`*. Empty/`None` is the
failure signal — there is rarely an exception. After every `Create*` you MUST
null/empty-check, then `IsValid`/`IsSolid`/`GetNakedEdges`.

---

## loft — `Brep.CreateFromLoft`

```python
breps = Rhino.Geometry.Brep.CreateFromLoft(
    curves,                                   # IEnumerable[Curve], >= 2, ordered
    Rhino.Geometry.Point3d.Unset,             # start point (Unset = none)
    Rhino.Geometry.Point3d.Unset,             # end point   (Unset = none)
    Rhino.Geometry.LoftType.Normal,           # Normal | Loose | Tight | Straight | Uniform
    False)                                    # closed loft? (last->first)
```

Returns `Brep[]` (often 1 element). Empty array = failure.

**Pre-flight inputs (C7):**
- **Same direction.** Adjacent sections whose tangents oppose produce a twisted
  or self-intersecting loft. Align: `if c.TangentAtStart * base < 0: c.Reverse()`
  (dot product < 0 ⇒ reverse). Use the first curve's `TangentAtStart` as `base`.
- **Seam alignment.** Closed section curves loft with a twist unless their seams
  line up. Snap each closed curve's start to the same parametric/world location:
  `crv.SetStartPoint(target_pt)` (or `crv.ChangeClosedCurveSeam(t)` to move the
  seam parameter). Pick `target_pt` as the closest point on each curve to a shared
  reference (e.g. the first section's start).
- **Consistent curve count/degree** is *not* required, but wildly different
  control structures give lumpy lofts — prefer `LoftType.Normal` and rebuild
  sections to matching point counts if the surface is dimpled.
- **Planarity / overlap.** Sections must be distinct (no two coincident) and
  ordered along the loft direction, not shuffled.

**Post-checks:** array non-empty → `brep[0] is not None` → `IsValid` →
`GetNakedEdges()`. A loft of open sections is an open surface (naked edges along
the ends are expected); cap with `CapPlanarHoles(tol)` if a solid is required,
then re-check `IsSolid`.

**Failure → fix:**
- Empty array → sections opposed or shuffled → reverse/reorder, retry.
- Twisted surface → seams unaligned → `SetStartPoint`/`ChangeClosedCurveSeam`.
- Not solid after cap → end profiles non-planar → use a planar profile or close
  with a loft cap curve, not `CapPlanarHoles`.

---

## sweep1 — `Brep.CreateFromSweep`

```python
breps = Rhino.Geometry.Brep.CreateFromSweep(
    rail,                 # Curve
    shapes,              # IEnumerable[Curve] cross-sections (>= 1)
    closed,              # bool: closed sweep
    tol)                 # sweep tolerance
```

Returns `Brep[]`. For finer control use `Rhino.Geometry.SweepOneRail()` (set
`.AngleToleranceRadians = ang_tol`, `.SweepTolerance = tol`, then
`.PerformSweep(rail, shapes)`).

**Pre-flight inputs (C7):**
- **Rail G1 continuity.** A rail with a tangent break (kink) makes the sweep
  pinch or flip the frame at the kink. Test continuity at interior joins with
  `rail.GetNextDiscontinuity(Continuity.G1_continuous, t0, t1, ...)`; if a G1
  break is found, either split the rail and sweep each smooth span, or rebuild
  the rail (`rail.Rebuild`) / fit a fair curve through its points.
- **Shape orientation.** Each cross-section should sit roughly perpendicular to
  the rail at its anchor; a section parallel to the rail tangent collapses.
- **Section ordering** along the rail must be monotonic (start → end), same as
  loft.

**Post-checks:** non-empty → `IsValid` → `GetNakedEdges` → cap if a solid is
required.

**Failure → fix:**
- Empty/pinched → rail has a G1 kink → split rail at discontinuities or rebuild.
- Twist along sweep → inconsistent section frames → align section start points;
  or use `SweepOneRail` with `.SetToRoadlikeTop()`/roadlike frame to lock the
  up-vector.

---

## revolve — `RevSurface.Create` (returns a SURFACE, then convert + cap) — C6

```python
import math
rev  = Rhino.Geometry.RevSurface.Create(
           profile_curve, axis_line, 0.0, math.radians(angle_deg))   # -> RevSurface (a Surface)
brep = Rhino.Geometry.Brep.CreateFromRevSurface(rev, False, False)    # -> Brep (open)
brep = brep.CapPlanarHoles(tol) or brep                              # close it
if not brep.IsSolid:
    raise RuntimeError("revolve not closed; cannot shell")
```

**`RevSurface.Create` returns a `Surface`, NOT a solid.** You must wrap it with
`Brep.CreateFromRevSurface` and then cap.

**Pre-flight inputs (C6/C7):**
- **Axis coplanar with profile.** The revolve axis (`Rhino.Geometry.Line`) must
  lie in the same plane as the profile curve. If not, the surface is degenerate.
  Check: build a plane through the profile and confirm both axis endpoints are on
  it within `tol`.
- **Profile touches the axis at start AND end**, *or* the profile is closed.
  Otherwise the revolved surface is open along the axis and can never be a solid.
  Snap the endpoints to the axis: `profile.SetStartPoint(p_on_axis)` /
  `profile.SetEndPoint(p_on_axis)`.
- **No self-crossing profile** across the axis (it would create a non-manifold
  result).

**Cap before shell (C6).** Sequence is: profile → `RevSurface.Create` →
`Brep.CreateFromRevSurface` → `CapPlanarHoles(tol)` → verify `IsSolid` →
`Brep.CreateOffset(brep, -thickness, True, True, tol)` for the shell.

**Post-checks:** `IsSolid` true after cap; for a 360° revolve there should be no
naked edges.

**Failure → fix:**
- Open after cap → profile endpoints not on axis → `SetStartPoint`/`SetEndPoint`
  onto the axis, or close the profile.
- `CreateFromRevSurface` null → axis not coplanar with profile → rebuild the
  profile in the axis plane.
- Shell (`CreateOffset`) returns empty → brep not closed → cap first; or
  `thickness` ≥ min wall → reduce thickness below the smallest local feature.

---

## extrude — `Surface.CreateExtrusion` / `Extrusion` / `Brep.CreateFromSurface`

```python
# Straight extrusion of a curve along a vector:
ext = Rhino.Geometry.Surface.CreateExtrusion(
          profile_curve, Rhino.Geometry.Vector3d(0, 0, height))   # -> Surface
brep = ext.ToBrep() if ext else None
# Capped solid from a CLOSED planar profile:
extr = Rhino.Geometry.Extrusion.Create(closed_planar_curve, height, True)  # cap=True
brep = extr.ToBrep() if extr else None
```

`Surface.CreateExtrusion` returns an open surface; for a capped solid use
`Extrusion.Create(curve, height, cap=True)` with a **closed, planar** profile.

**DIRECTION-PIN (E3 — silent wrong-Z; conventions [§5a](../../shared/conventions.md#5a-the-direction-pin-idiom-every-directional-op)).**
`Extrusion.Create` / `Surface.CreateExtrusion` (and `RevSurface.Create` /
`Brep.CreateFromRevSurface`) extrude/revolve **along the curve's own
normal/tangent, which is NOT trustworthy** — a profile authored on `WorldXZ` can
extrude in `−Z`, so a seat meant to top out at 450 lands at 410 and the *count*
still passes. After **any** directional create you MUST read
`brep.GetBoundingBox(True)` and PIN the intended face (top/bottom) onto the
IR-intended Z by `brep.Transform(Transform.Translation(Vector3d(0,0,dz)))`, then
re-read the bbox to **confirm** the pin took. The published `support` level a part
exposes is the *pinned* value. `codegen_guard.py` **RULE 10** fails any snippet
that calls a directional op without a `GetBoundingBox(` read plus a `.Max.Z`/
`.Min.Z` anchor paired with `Transform.Translation`/`.Translate()`.

**Pre-flight inputs (C7):**
- For a capped solid the profile MUST be **closed** (`curve.IsClosed`) and
  **planar** (`curve.IsPlanar(tol)`). Open or non-planar ⇒ no cap ⇒ not solid.
- `height != 0`; the extrusion vector should be off the profile plane (a vector
  in the profile plane gives a zero-thickness/degenerate surface).

**Post-checks:** `ToBrep()` non-null → `IsValid` → `IsSolid` (when capped).

**Failure → fix:**
- Not solid → profile open/non-planar → close it (`Curve.MakeClosed`/join) and
  re-plane it before extruding.
- Null surface → height vector parallel to profile plane → use an off-plane
  vector (e.g. the profile-plane normal × height).

---

## boolean — `Brep.CreateBooleanUnion / Difference / Intersection` — C2/C3

```python
union = Rhino.Geometry.Brep.CreateBooleanUnion(breps, tol)              # -> Brep[]
diff  = Rhino.Geometry.Brep.CreateBooleanDifference(base, tools, tol)   # -> Brep[]
inter = Rhino.Geometry.Brep.CreateBooleanIntersection(a, b, tol)       # -> Brep[]
```

All return `Brep[]`. **Empty array OR a valid Brep missing a part are both
possible failures** — this is the most dangerous op in the suite.

**Pre-flight inputs (C3 — INTERPENETRATE, never coincident):**
- Mating parts for a **union** must overlap by **0.5–2 mm**, never touch exactly.
  Coincident/coplanar contact faces are degenerate and make the union drop a part
  or leave naked edges. Push each mating part into its neighbour by the IR
  `penetration` depth along the contact normal **before** the boolean.
- Every input must itself be a **closed solid** (`IsSolid`) for a reliable
  result. A union of open breps may merge surfaces unpredictably.
- For **difference**, the tool must actually pass through the base (also
  overlapping, not flush).

**Post-checks — the count + volume guard (C2). This is non-negotiable:**

```python
def verify_boolean(breps, expected_solid_count, expected_volume, vtol):
    # A PARTIAL boolean returns a VALID Brep missing a part. IsValid/IsSolid pass.
    # Only an expected-count + total-volume check catches the silent drop.
    if breps is None or len(breps) != expected_solid_count:
        raise RuntimeError("boolean solid count %d != expected %d"
                           % (0 if breps is None else len(breps), expected_solid_count))
    vol = sum(Rhino.Geometry.VolumeMassProperties.Compute(b).Volume for b in breps)
    if abs(vol - expected_volume) > vtol:
        raise RuntimeError("boolean volume %.3f != expected %.3f (tol %.3f)"
                           % (vol, expected_volume, vtol))
```

A "valid 3-legged chair" (one leg silently dropped by the union) passes every
naive `IsValid`/`IsSolid` guard — only count+volume catches it.

**Failure → fix:**
- Empty array → inputs only touch (coplanar) → interpenetrate 0.5–2 mm, retry.
- Wrong solid count → a part was dropped → check that part's penetration and that
  it is itself solid; bump penetration toward 2 mm; re-run; if still wrong, union
  pairwise instead of all-at-once.
- Volume off → a face was lost or a tool over-cut → inspect with
  `analyze_objects`; for difference, confirm the tool didn't exceed the base.
- Non-manifold result → coincident faces remained → increase penetration; avoid
  exactly-aligned planar contacts.

---

## fillet — `Brep.CreateFilletEdges` (and `Curve.CreateFilletCurves`)

```python
filleted = Rhino.Geometry.Brep.CreateFilletEdges(
               brep, edge_indices, radii, radii,           # start radius, end radius per edge
               Rhino.Geometry.BlendType.Fillet,
               Rhino.Geometry.RailType.RollingBall,
               tol)                                         # -> Brep[]
```

Returns `Brep[]`. Empty = failure.

**Pre-flight inputs (C7):**
- **Radius < min local edge length / min adjacent face size.** A fillet radius
  larger than the smallest edge it touches (or larger than the gap to a
  neighbouring edge) self-intersects and the op returns empty. Measure the target
  edges (`edge.GetLength()`) and the distance to neighbouring features; keep
  `radius < 0.5 * min_local_edge` as a safe rule.
- Edges must be real manifold edges of the brep (valid `edge_indices`).
- Adjacent fillets whose radii would overlap at a shared vertex fail — fillet in
  passes or reduce radii.

**Post-checks:** non-empty → `IsValid` → `IsSolid` (a fillet on a closed solid
should stay solid).

**Failure → fix:**
- Empty → radius too big → shrink radius below `0.5 * min_local_edge`.
- Self-intersection → overlapping fillets at a vertex → fillet fewer edges per
  pass or use variable radius tapering to a smaller end radius.

---

## offset — `Curve.Offset` / `Surface.Offset` / `Brep.CreateOffset` (shell)

```python
# Planar curve offset (needs a plane + distance):
offs = curve.Offset(plane, distance, tol, Rhino.Geometry.CurveOffsetCornerStyle.Sharp)
# Solid shell offset (the brep MUST be closed):
shell = Rhino.Geometry.Brep.CreateOffset(brep, -thickness, True, True, tol)  # solid=True, both=True
```

`Curve.Offset` returns `Curve[]`; `Brep.CreateOffset` returns a `Brep[]`-like
result (an offset brep) — check for null/empty.

**Pre-flight inputs (C7):**
- **Distance < min feature size.** An offset distance larger than the smallest
  concave feature radius collapses or self-intersects the offset. For a curve,
  keep `abs(distance) <` the tightest concave fillet radius in the curve.
- **Self-intersection check.** After offsetting a curve, test the result with
  `Rhino.Geometry.Intersect.Intersection.CurveSelf(offset_curve, tol)`; if there
  are crossings, the offset is invalid — reduce distance or trim the loops.
- For **`Brep.CreateOffset` (shell)** the brep must be **closed** (cap first per
  C6). An open brep offset produces gaps, not a wall.
- Pick the correct **offset side** (sign of distance + corner style); the wrong
  side offsets outward when you wanted inward.

**Post-checks:** curve offset → non-empty + no self-intersections; shell →
`IsValid` + `IsSolid` and the wall thickness ≈ requested.

**Failure → fix:**
- Empty/collapsed → distance ≥ min feature → reduce distance.
- Self-intersecting loops → distance too large for a concave corner → reduce, or
  split the curve and offset spans separately.
- Shell empty → brep not closed → cap planar holes first; verify `IsSolid` before
  `CreateOffset`.

---

## network surface — `NurbsSurface.CreateNetworkSurface`

```python
srf, err = Rhino.Geometry.NurbsSurface.CreateNetworkSurface(
               curves,        # IEnumerable[Curve]: a grid of U and V curves
               continuity,    # 0=loose, 1=position, 2=tangency, 3=curvature
               edge_tol, interior_tol, ang_tol)          # -> (NurbsSurface, int errorcode)
```

Returns a tuple: the surface (may be `None`) and an integer error code (0 = OK).
There is **no typed MCP tool** for network surface — emit it via
`execute_rhinocommon_csharp_code` / `execute_rhinoscript_python_code`.

**Pre-flight inputs (C7 — grid requirements):**
- Curves must form an actual **grid**: at least 2 curves in each direction that
  the others cross. A pile of curves that don't intersect in two families fails.
- **U and V families must intersect** each other within `edge_tol`/`interior_tol`.
  Gaps at the crossings give error codes (1 = no curves, 2 = network not valid).
- Direction consistency: curves of one family should run the same way (reverse
  outliers, same rule as loft).
- Tolerances should be `tol` for edges/interior and `ang_tol` for angle — not
  zero.

**Post-checks:** `err == 0` and `srf is not None` → wrap with `srf.ToBrep()` →
`IsValid`; cap if a solid is needed.

**Failure → fix:**
- `err != 0` or null surface → curves don't form a crossing grid → ensure two
  intersecting families; trim/extend curves so they actually cross within tol.
- Lumpy surface → mismatched curve counts/degrees → rebuild curves to consistent
  degree before the network call.

---

## Method availability + fallback (E4 — missing method, DEGRADE VISIBLY)

Some RhinoCommon members are **not present in every Rhino build** (the version on
the connected machine decides), and a few have an *overload* that exists in the
docs but not in the running assembly. Calling a missing member raises an
`AttributeError`/`MissingMethodException` inside `execute_rhinoscript_python_code`
that reads like an unrelated geometry failure. **Probe first**, then fall back —
and tag the degraded result `shell_degraded:true` so the loss of fidelity is
**visible**, never silently rolled back.

**Probe the build before relying on a fragile member** with
[`../../rhino-modeling/scripts/detect_server.py --rhinocommon-probe`](../../rhino-modeling/scripts/detect_server.py),
which emits a `hasattr`-based RhinoCommon snippet to send through
`execute_rhinoscript_python_code`; it prints a capability map
(`{label: {present, owner, member, degraded_fallback}}`). A `present:false` entry
means take the fallback below.

| Fragile member | Probe label | If MISSING → fallback (tag `shell_degraded:true`) |
|----------------|-------------|---------------------------------------------------|
| `Brep.CreateOffset` (shell) | `brep_create_offset` | **Loft between the inner and outer profiles** (offset the section curve inward by `thickness`, then `Brep.CreateFromLoft` outer→inner and cap), or build the wall as a manual two-shell solid. Wall thickness is approximate ⇒ `shell_degraded:true`. |
| `Brep.CreateOffset` solid overload `(brep, dist, solid, extend, tol)` | `brep_create_offset_solid_overload` | `hasattr` can be `True` while this overload is absent. Wrap the call in `try/except (TypeError, Exception)`; on failure fall back to the manual two-shell loft above and tag `shell_degraded:true`. |
| `RevSurface.Create` | `rev_surface_create` | **Sweep the profile on a circular rail** (`Brep.CreateFromSweep` with a full-circle rail) or `Brep.CreatePipe` for a tube approximation, then cap. Section fidelity is approximate ⇒ `shell_degraded:true`. |
| `NurbsSurface.CreateNetworkSurface` | `nurbs_network_surface` | **`Brep.CreateFromLoft` across the U-family then trim**, or `Brep.CreatePatch` through the grid points. Surface is an approximation of the network ⇒ `shell_degraded:true`. |

`shell_degraded:true` belongs on the part's node in the scene-graph (alongside its
GUID) so verification and the user both see that the op produced a *degraded*
solid rather than the intended one — the op DEGRADES VISIBLY instead of the whole
stage rolling back. See
[`../../rhino-modeling/reference/server-capabilities.md`](../../rhino-modeling/reference/server-capabilities.md)
for how the probe + capability map are surfaced.

---

## Quick failure-signal table

| Op             | Real call                              | Returns        | Empty/null means                          |
|----------------|----------------------------------------|----------------|-------------------------------------------|
| loft           | `Brep.CreateFromLoft`                  | `Brep[]`       | sections opposed/shuffled/seam-misaligned |
| sweep1         | `Brep.CreateFromSweep`                 | `Brep[]`       | rail G1 kink / bad section frames         |
| revolve        | `RevSurface.Create` → `CreateFromRevSurface` | `Surface`→`Brep` | profile off-axis / not coplanar       |
| extrude        | `Extrusion.Create` / `Surface.CreateExtrusion` | `Extrusion`/`Surface` | profile open/non-planar       |
| boolean        | `Brep.CreateBooleanUnion`/`Difference`/`Intersection` | `Brep[]` | coincident contact (interpenetrate!)  |
| fillet         | `Brep.CreateFilletEdges`               | `Brep[]`       | radius ≥ min local edge                    |
| offset (curve) | `Curve.Offset`                         | `Curve[]`      | distance ≥ min feature / self-intersect   |
| offset (shell) | `Brep.CreateOffset`                    | `Brep`         | brep not closed                            |
| network        | `NurbsSurface.CreateNetworkSurface`    | `(srf, err)`   | curves don't form a crossing grid          |
