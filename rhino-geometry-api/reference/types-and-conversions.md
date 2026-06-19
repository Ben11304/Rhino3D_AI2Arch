# Geometry type selection & conversions

Which Rhino geometry type to build in, when each is **reliable** vs **fragile**,
and how to convert between them. Conventions live in
[`../../shared/conventions.md`](../../shared/conventions.md).

The core trade-off: **Brep** (boundary-representation NURBS solid/surface) is the
suite's default because booleans, offsets, volume, and naked-edge checks are
exact on it. **SubD** and **Mesh** are easier to make organic but lose the exact
analytic checks the verify loop (C2/C3) depends on. Prefer Brep; convert to mesh
only for display, to SubD only for genuinely freeform shapes.

---

## Selection matrix

| Type      | RhinoCommon class                         | Reliable for (use it)                                                                 | Fragile for (avoid)                                                                 |
|-----------|-------------------------------------------|--------------------------------------------------------------------------------------|------------------------------------------------------------------------------------|
| **NURBS curve** | `Curve` / `NurbsCurve`              | profiles, rails, sections, generatrices; exact lengths & tangents; loft/sweep/revolve inputs | nothing geometric — curves are the safe primitive layer                            |
| **NURBS surface** | `Surface` / `NurbsSurface`        | single untrimmed patches; revolve/extrude/network *intermediate* results              | as a final solid — a bare surface is open; wrap to Brep + cap before measuring      |
| **Brep**  | `Brep`                                    | **default.** solids, trimmed surfaces, booleans, fillets, solid offset/shell, exact volume, naked-edge & solid checks (C2/C3) | extremely organic blends with many fighting constraints — may need SubD then convert |
| **SubD**  | `SubD`                                    | smooth organic forms (furniture curves, character-ish shapes) authored from a control cage | exact booleans/volume — convert to Brep first; SubD booleans are approximate        |
| **Mesh**  | `Mesh`                                    | display, vision capture, fast viewport, watertight checks via `IsClosed`              | precise CAD ops — mesh booleans are approximate; volume is only as good as the facet density |

Rule: **author measurable parts as Brep.** If a part must be SubD (freeform),
convert to Brep *before* any boolean and *before* any C2 volume check.

---

## When Brep is reliable vs fragile

**Reliable:** boxes, cylinders, cones, spheres, lofts/sweeps/revolves of clean
profiles, booleans of interpenetrating solids, edge fillets within radius limits,
solid shells of closed breps. All the verify-loop math (`VolumeMassProperties`,
`GetNakedEdges`, `IsSolid`) is exact on a valid Brep.

**Fragile:** booleans of coincident (non-interpenetrating) inputs (C3); fillets
with radius ≥ min local edge (C7); offsets ≥ min feature size (C7); lofts of
opposed/seam-misaligned sections (C7). These don't throw — they return empty or a
*valid Brep missing a part* (C2). The cheat-sheets in
[`geometry-ops.md`](geometry-ops.md) give the pre-flight that keeps Brep in its
reliable regime.

---

## Conversion functions

```python
# Surface  -> Brep
brep = surface.ToBrep()
brep = Rhino.Geometry.Brep.CreateFromSurface(surface)
brep = Rhino.Geometry.Brep.CreateFromRevSurface(rev_surface, False, False)  # revolve (C6)

# Extrusion -> Brep
brep = extrusion.ToBrep()

# Brep -> Mesh (for display / vision capture; choose a quality)
meshes = Rhino.Geometry.Mesh.CreateFromBrep(brep,
             Rhino.Geometry.MeshingParameters.FastRenderMesh)   # or QualityRenderMesh / Default
mesh = Rhino.Geometry.Mesh()
for m in meshes:
    mesh.Append(m)

# SubD <-> Brep / Mesh
brep   = Rhino.Geometry.Brep.CreateFromSubD(subd, tol)          # SubD -> Brep (do this before booleans)
subd_b = Rhino.Geometry.SubD.CreateFromMesh(mesh)               # Mesh -> SubD
mesh_b = Rhino.Geometry.Mesh.CreateFromSubD(subd, 3)            # SubD -> Mesh (subdivision level)

# Mesh -> Brep (approximate; only for simple meshes)
brep_m = Rhino.Geometry.Brep.CreateFromMesh(mesh, False)        # trimmedTriangles=False

# Close / cap helpers used across conversions
brep = brep.CapPlanarHoles(tol) or brep                         # close planar openings
joined = Rhino.Geometry.Brep.JoinBreps(list_of_breps, tol)      # -> Brep[] (merge faces into one)
```

**Conversion gotchas:**
- `Surface.ToBrep()` / `CreateFromSurface` give an **untrimmed, open** Brep — cap
  it before `IsSolid` or volume checks.
- `Brep.CreateFromSubD` requires a valid `tol`; a coarse tolerance loses the
  smooth edges. Do the SubD→Brep conversion **before** any boolean so the C2
  volume check operates on exact geometry.
- `Mesh.CreateFromBrep` returns a **list** of meshes (one per face) — append them
  into a single `Mesh` for capture.
- Mesh→Brep and Mesh→SubD are *approximations*; never feed a mesh-derived Brep
  into a C2 volume gate expecting an exact match.

---

## Picking the build type from the IR

- IR `primitive` (box/cylinder/sphere/cone/plane) → build as **Brep** directly
  (`Brep.CreateFromBox`, `Brep.CreateFromCylinder`, `Brep.CreateFromSphere`,
  `Brep.CreateCone`, etc.).
- IR `interpolated_curve` → **Curve** via `Curve.CreateInterpolatedCurve` (a
  section/rail/generatrix; not a solid).
- IR `operation` loft/sweep1/revolve/extrude → produce a **Surface/Brep**, then
  cap to a closed **Brep** if the part must be solid (C6).
- IR `operation` shell → **Brep.CreateOffset** on a closed **Brep** (cap first).
- IR `operation` boolean → **Brep** booleans with the C2 count+volume guard.

Everything the verify loop measures ends up a **Brep** before measurement.
