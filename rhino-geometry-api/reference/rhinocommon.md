# Core RhinoCommon idioms

The minimum RhinoCommon vocabulary an LLM needs to emit correct Rhino 8 geometry
code. Conventions (GUID ledger, frame discipline, codegen guard) live in
[`../../shared/conventions.md`](../../shared/conventions.md) — this file is the
API surface, not the policy.

Always read tolerances live:

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import (Brep, Curve, Surface, Point3d, Vector3d, Plane,
                            Transform, VolumeMassProperties)

tol     = sc.doc.ModelAbsoluteTolerance      # length tolerance, model units
ang_tol = sc.doc.ModelAngleToleranceRadians  # angle tolerance, radians
```

---

## rhinoscriptsyntax vs RhinoCommon vs CPython3 — when to use each

| Layer                 | Import                         | Use it for                                                                 | Avoid for |
|-----------------------|--------------------------------|----------------------------------------------------------------------------|-----------|
| **rhinoscriptsyntax** | `import rhinoscriptsyntax as rs` | quick scripted ops that return **GUIDs already baked into the doc** (`rs.AddBox`, `rs.AddLoftSrf`, `rs.BooleanUnion`); fast prototyping | precise tolerance control, inspecting geometry before bake, returning intermediate `Brep` objects |
| **RhinoCommon**       | `import Rhino` + `from Rhino.Geometry import ...` | everything that needs **geometry objects in memory** (Brep/Curve/Surface), explicit `tol`, validity inspection, the codegen guard contract | nothing — this is the default for the suite |
| **CPython3 (`#! python3`)** | shebang on line 1            | the runtime the suite targets in Rhino 8 (`execute_rhinoscript_python_code`); full CPython stdlib + RhinoCommon via the dotnet bridge | IronPython-only idioms (`clr.AddReference` patterns differ) |

Rule of thumb: **build with RhinoCommon, inspect, then bake once** through
`sc.doc.Objects.AddBrep` so you control the GUID and tagging. Use
rhinoscriptsyntax only when you don't need the intermediate object. The `rs`
wrappers internally call RhinoCommon and bake immediately, so you lose the
pre-bake validity/volume checks the contract requires — prefer RhinoCommon for
anything the verify loop will measure.

`Rhino.RhinoDoc.ActiveDoc` and `scriptcontext.doc` refer to the same active
document in the MCP execution context; use `sc.doc` for consistency with the
conventions snippets.

---

## Booleans — `Brep.CreateBooleanUnion / Difference / Intersection`

```python
union = Brep.CreateBooleanUnion(list_of_breps, tol)               # -> Brep[]
diff  = Brep.CreateBooleanDifference(base_breps, tool_breps, tol) # -> Brep[]
inter = Brep.CreateBooleanIntersection(a_breps, b_breps, tol)     # -> Brep[]
```

- All take an explicit `tol` and return a **`Brep[]`** (possibly empty).
- Inputs must **interpenetrate 0.5–2 mm** (correction C3); coincident faces are
  degenerate.
- After the call, run the expected-count + total-volume check (C2 —
  `verify_boolean` in `geometry-ops.md`). `IsValid`/`IsSolid` alone do not catch
  a silently dropped part.

---

## Loft — `Brep.CreateFromLoft`

```python
breps = Brep.CreateFromLoft(curves, Point3d.Unset, Point3d.Unset,
                            Rhino.Geometry.LoftType.Normal, False)   # -> Brep[]
```

Pre-flight: same direction (`c.TangentAtStart * base < 0 → c.Reverse()`) and
seam-aligned (`c.SetStartPoint(target)`). See `geometry-ops.md` for the full
checklist.

---

## Revolve — `RevSurface.Create` + Brep conversion (C6)

```python
import math
rev  = Rhino.Geometry.RevSurface.Create(profile, axis_line, 0.0,
                                        math.radians(angle_deg))  # -> RevSurface (Surface!)
brep = Brep.CreateFromRevSurface(rev, False, False)              # -> Brep (open)
brep = brep.CapPlanarHoles(tol) or brep                         # close before any shell
if not brep.IsSolid:
    raise RuntimeError("revolve not closed")
```

`RevSurface.Create` yields a **Surface**, never a solid. `axis_line` is a
`Rhino.Geometry.Line`; it must be coplanar with `profile`, and `profile` must
touch the axis at both ends or be closed.

---

## Shell / solid offset — `Brep.CreateOffset`

```python
# Solid wall of thickness `t` (inward). Brep MUST be closed first (cap per C6).
shell = Brep.CreateOffset(brep, -t, True, True, tol)   # (brep, distance, solid, extend, tol)
```

`solid=True` requires a **closed** Brep — cap planar holes and confirm `IsSolid`
before calling. Returns an offset Brep (check for null). A negative distance
offsets inward (a hollow wall); positive offsets outward.

---

## Interpolated curve — `Curve.CreateInterpolatedCurve`

```python
pts = [Point3d(*p) for p in control_points]           # >= 2 points
crv = Curve.CreateInterpolatedCurve(pts, 3)           # degree 3 through the points
# overload with knot style + tangents also exists:
# Curve.CreateInterpolatedCurve(pts, degree, knotstyle, startTangent, endTangent)
```

Builds a NURBS curve **through** the points (interpolation), unlike
`NurbsCurve.Create`/`Curve.CreateControlPointCurve` which treat points as control
points. Use this for IR `interpolated_curve` parts: loft sections, sweep rails,
and revolve generatrices.

---

## Frame relocation — `Transform.PlaneToPlane`

```python
target = Plane(Point3d(ox, oy, oz), Vector3d(*x_axis), Vector3d(*y_axis))  # or a named plane
xform  = Transform.PlaneToPlane(Plane.WorldXY, target)   # author at WorldXY, move to frame
geom.Transform(xform)
```

Author every part on `Plane.WorldXY`, then move it with `PlaneToPlane`. Never
bake offsets into raw coordinates (frame discipline, conventions §4). Named
planes: `Plane.WorldXY`, `Plane.WorldYZ`, `Plane.WorldZX` (Rhino's WorldXZ).
Mirror/array instances come from transforming the base geometry, not
re-authoring coordinates.

Other useful transforms: `Transform.Translation(vector)`,
`Transform.Rotation(angle_rad, axis_vector, center_pt)`,
`Transform.Mirror(plane)`, `Transform.Scale(center, factor)`.

---

## Attributes + UserString tagging — the GUID ledger (C1)

```python
import Rhino
attr = Rhino.DocObjects.ObjectAttributes()
attr.Name = part_id                                   # human-readable in object table
attr.SetUserString("part_id", part_id)                # canonical ledger key / fallback resolver
attr.SetUserString("provenance", provenance or "")    # optional, for debugging
attr.LayerIndex = layer_index                          # see ensure_layer below
# coloring for a vision capture (render concern only):
attr.ColorSource = Rhino.DocObjects.ObjectColorSource.ColorFromObject
attr.ObjectColor = System.Drawing.Color.Red
```

Identity lives in `part_id` (survives renames); color is a render concern, set
separately. Read a tag back with
`obj.Attributes.GetUserString("part_id")` — the fallback resolver when a GUID is
lost after a boolean consumes its inputs.

```python
def ensure_layer(doc, name):
    idx = doc.Layers.FindByFullPath(name, -1)
    if idx >= 0:
        return idx
    layer = Rhino.DocObjects.Layer()
    layer.Name = name
    return doc.Layers.Add(layer)
```

---

## Bake + register — `sc.doc.Objects.AddBrep` (returns the GUID)

```python
guid = sc.doc.Objects.AddBrep(brep, attr)   # -> System.Guid (the ledger handle)
if guid is None or guid == System.Guid.Empty:
    raise RuntimeError("bake failed for part_id=%s" % part_id)
sc.doc.Views.Redraw()                        # once, at the very end
print(guid)                                  # only stdout enters context
```

Sibling bake calls: `AddCurve(curve, attr)`, `AddSurface(surface, attr)`,
`AddMesh(mesh, attr)`, `AddPoint(point, attr)`. **Every mutator must return its
GUID** (C1). For typed MCP tools / `rs.*` calls that don't return one, wrap with
the *create-then-find-newest* shim (conventions §2): snapshot
`{o.Id for o in sc.doc.Objects}` before, diff after, expect exactly one new id.

---

## Measuring — for the verify loop (vision-demotion, C4)

```python
vmp = VolumeMassProperties.Compute(brep)
volume = vmp.Volume                                   # total volume for the C2 check
amp = Rhino.Geometry.AreaMassProperties.Compute(brep)
centroid = amp.Centroid                               # for position checks
bbox = brep.GetBoundingBox(True)                      # world-aligned bbox
height = bbox.Max.Z - bbox.Min.Z                      # absolute dimension
naked = brep.GetNakedEdges()                          # closed solid -> none
```

Counts / volumes / distances / heights are **math**, not vision (C4). Route
`numeric_checks`/`ratio_checks` through these; reserve vision for "does it look
like X".
