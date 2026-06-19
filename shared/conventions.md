# Rhino Skill Suite — Canonical Conventions

This is the **single source of truth** for the entire Rhino AI-agent skill suite. Every other
skill links here with a relative path (e.g. `../../shared/conventions.md`). When a rule changes,
it changes here and nowhere else.

Mental model: **MCP is hands+eyes, the skill layer is brain+method.** The LLM spatial deficit is
fixed at inference time by *externalizing spatial state* into artifacts (the build-plan IR and the
scene-graph) that are re-read every step, plus *giving eyes* via render-and-look. Never hold
geometry state in your head; read it back from the document or the artifacts.

The shared IR these conventions govern is defined in
[`build-plan.schema.json`](./build-plan.schema.json).

---

## 1. Units & tolerance — read, never hardcode

Set the document unit system **first**, before any geometry exists, then read tolerances live off
the document. Never bake a literal like `0.001` into geometry code.

```python
#! python3
import scriptcontext as sc
import Rhino

# Tolerances are properties of the active document. Read them every run.
tol      = sc.doc.ModelAbsoluteTolerance        # absolute length tolerance, in model units
ang_tol  = sc.doc.ModelAngleToleranceRadians    # angular tolerance, in radians
unit_sys = sc.doc.ModelUnitSystem               # Rhino.UnitSystem enum (Millimeters, etc.)

# If the IR requests a unit system, set it BEFORE creating geometry:
#   sc.doc.ModelUnitSystem = Rhino.UnitSystem.Millimeters
```

- Every number in the IR carries an implicit **unit + frame**: the value is in the document
  `units` (see schema) and is measured against `world_frame`.
- Pass `tol` into every `Brep.Create*`, `Brep.Join`, `Curve.Offset`, boolean, and
  `GetNakedEdges` check. Pass `ang_tol` where an angle tolerance is accepted.
- `overall_height_mm` in the IR `scale` block is always **millimetres**, independent of the
  document unit system. Convert when comparing to model-unit measurements.

---

## 2. The GUID LEDGER (correction C1)

**Objects are only referenceable by GUID.** part_id is not a Rhino handle — it is our label.

- The **scene-graph artifact** (owned by `rhino-scene-state`) stores `part_id -> GUID`, captured at
  **bake time** (the moment `AddBrep`/`AddCurve`/etc. returns a GUID).
- Tag every baked object with `UserString "part_id"` so the part is recoverable when a GUID is
  lost (e.g. after a boolean consumes inputs and produces a new object). **UserString `part_id` is
  the fallback resolver.**
- **Every mutator must return its created GUID.** Typed MCP tools that don't return a GUID must be
  wrapped in a *create-then-find-newest* shim:

```python
#! python3
import scriptcontext as sc
import Rhino

def add_and_register(geom, part_id, layer_index=None):
    """Bake geometry, return its GUID, and stamp the part_id ledger key."""
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.Name = part_id
    attr.SetUserString("part_id", part_id)
    if layer_index is not None:
        attr.LayerIndex = layer_index
    guid = sc.doc.Objects.AddBrep(geom, attr)   # AddBrep/AddCurve/AddSurface return a System.Guid
    if guid == Rhino.RhinoMath.UnsetGuidValue if hasattr(Rhino.RhinoMath, "UnsetGuidValue") else (guid is None):
        raise RuntimeError("bake failed for part_id=%s" % part_id)
    return guid

def find_newest_guid(before_ids):
    """Shim for ops that don't return a GUID: diff the object table."""
    after = {o.Id for o in sc.doc.Objects}
    new = list(after - set(before_ids))
    if len(new) != 1:
        raise RuntimeError("expected exactly 1 new object, found %d" % len(new))
    return new[0]

def resolve(part_id):
    """Fallback resolver: find an object by its UserString part_id when the GUID is lost."""
    for o in sc.doc.Objects:
        if o.Attributes.GetUserString("part_id") == part_id:
            return o.Id
    return None
```

---

## 3. Naming, layers & UserString tagging

Every object must be identifiable three ways, set together at bake time:

- **Name** = the part_id (human-readable in the Rhino object table).
- **Layer** = the part's `layer` field, or a default layer named after `object`.
- **UserString** `part_id` = the canonical ledger key (survives renames; the fallback resolver).
- Optionally `UserString "provenance"` = the IR `provenance` string for debugging.

```python
attr = Rhino.DocObjects.ObjectAttributes()
attr.Name = part_id
attr.SetUserString("part_id", part_id)
attr.SetUserString("provenance", provenance or "")
attr.LayerIndex = ensure_layer(sc.doc, layer_name)   # create the layer if missing, return its index
guid = sc.doc.Objects.AddBrep(brep, attr)
```

When coloring parts for a vision capture (see §8), set `attr.ColorSource = ObjectColorSource.ColorFromObject`
and `attr.ObjectColor` — color is a render concern, not an identity concern; identity stays in
`part_id`.

---

## 4. Frame discipline

Build each part **on an explicit `Rhino.Geometry.Plane`**, authored at `Plane.WorldXY`, then
relocate it to its target frame. Do not author parts pre-translated by sprinkling offsets through
coordinates — that is where spatial reasoning silently drifts.

```python
#! python3
import Rhino
from Rhino.Geometry import Plane, Point3d, Vector3d, Transform

# Target frame from the IR part.frame (origin + optional axes / named plane).
origin = Point3d(ox, oy, oz)
target = Plane(origin, Vector3d(*x_axis), Vector3d(*y_axis))   # or a named plane (WorldYZ, ...)

xform = Transform.PlaneToPlane(Plane.WorldXY, target)          # move WorldXY-authored geom -> frame
geom.Transform(xform)
```

- Named planes resolve to `Plane.WorldXY` / `Plane.WorldYZ` / `Plane.WorldZX` (Rhino's `WorldXZ`).
- Symmetry instances (mirror/rotational) are produced by transforming the base part's geometry with
  the symmetry plane/axis from the IR — never by re-authoring coordinates by hand.
- **Every IR number carries unit + frame.** A bare `[0,0,450]` means "450 model-units up the Z of
  `world_frame`", nothing else.

---

## 5. The CODEGEN GUARD CONTRACT

Any geometry-producing Python emitted by a skill **must** follow this contract. The canonical,
runnable implementation lives in
[`../rhino-geometry-api/scripts/codegen_guard.py`](../rhino-geometry-api/scripts/codegen_guard.py);
the snippet below is its shape and must be honored verbatim in spirit.

Rules baked into the contract:
1. `#! python3` shebang on line 1.
2. `# r: <pkg>` requirement comments **only** for genuine third-party packages — never for
   `Rhino`, `rhinoscriptsyntax`, `scriptcontext`, `System` (those are always present).
3. Read `tol`/`ang_tol` from the document; never hardcode.
4. **Pre-flight the INPUTS** before calling any `Create*` (correction C7) — not just the result.
5. Null/empty check **after every** `Create*`.
6. `IsValid` + `IsSolid` + `GetNakedEdges` on every resulting Brep.
7. **Post-boolean: EXPECTED-COUNT + TOTAL-VOLUME** check against the IR (correction C2).
8. Name + `SetUserString("part_id", ...)` + assign layer at bake.
9. `AddBrep` (etc.) returns the GUID; register it; `sc.doc.Views.Redraw()` once at the end.

```python
#! python3
# r: <only-genuine-third-party-here>
import scriptcontext as sc
import Rhino
from Rhino.Geometry import Brep, VolumeMassProperties

tol     = sc.doc.ModelAbsoluteTolerance
ang_tol = sc.doc.ModelAngleToleranceRadians

# --- 4. PRE-FLIGHT INPUTS (C7) -------------------------------------------------
# loft: every section curve runs the SAME direction and seams are aligned
# sweep1: rail is G1 continuous
# fillet: radius < min local edge length
# offset: distance < min feature size
# revolve: axis is coplanar with the profile AND profile touches axis at both ends (C6)
def preflight_loft(sections):
    if len(sections) < 2:
        raise ValueError("loft needs >= 2 sections")
    base = sections[0].TangentAtStart
    for c in sections[1:]:
        if c.TangentAtStart * base < 0:        # dot < 0 => opposing direction
            c.Reverse()                        # align direction BEFORE lofting

# --- 5. CREATE + NULL CHECK ----------------------------------------------------
results = Brep.CreateFromLoft(sections, Rhino.Geometry.Point3d.Unset,
                              Rhino.Geometry.Point3d.Unset,
                              Rhino.Geometry.LoftType.Normal, False)
if not results or results.Count == 0:
    raise RuntimeError("CreateFromLoft returned nothing")
brep = results[0]
if brep is None:
    raise RuntimeError("loft produced a null Brep")

# --- 6. VALIDITY ---------------------------------------------------------------
if not brep.IsValid:
    raise RuntimeError("Brep is not valid: %s" % brep.IsValidWithLog()[1])
naked = brep.GetNakedEdges()           # expect NONE for a closed solid
if brep.IsSolid is False and naked and len(naked) > 0:
    raise RuntimeError("open Brep: %d naked edges" % len(naked))

# --- 7. POST-BOOLEAN COUNT + VOLUME (C2) --------------------------------------
def verify_boolean(breps, expected_solid_count, expected_volume, vtol):
    # A partial boolean returns a VALID Brep missing a part. IsValid/IsSolid pass.
    # Only an expected-count + total-volume check catches it.
    if len(breps) != expected_solid_count:
        raise RuntimeError("boolean solid count %d != expected %d"
                           % (len(breps), expected_solid_count))
    vol = sum(VolumeMassProperties.Compute(b).Volume for b in breps)
    if abs(vol - expected_volume) > vtol:
        raise RuntimeError("boolean volume %.3f != expected %.3f (tol %.3f)"
                           % (vol, expected_volume, vtol))

# --- 8/9. BAKE + REGISTER + REDRAW --------------------------------------------
attr = Rhino.DocObjects.ObjectAttributes()
attr.Name = part_id
attr.SetUserString("part_id", part_id)
attr.LayerIndex = layer_index
guid = sc.doc.Objects.AddBrep(brep, attr)     # returns System.Guid
if guid is None:
    raise RuntimeError("bake failed")
sc.doc.Views.Redraw()
print(guid)                                   # only stdout enters context
```

---

## 6. Interpenetration rule for booleans (correction C3)

Parts that will be **boolean-unioned must interpenetrate by 0.5–2 mm**. Exactly coincident or
coplanar contact faces are *degenerate* and make the union fail partially or produce naked edges.

- The IR encodes this as a `relations` entry `{ "type": "interpenetrate", "to": "<other>",
  "penetration": <0.5..2> }`. The `penetration` field is **required** for any union join.
- When laying parts out, push mating geometry into its neighbour by the `penetration` depth along
  the contact normal **before** calling `Brep.CreateBooleanUnion`.
- After the union, always run the EXPECTED-COUNT + TOTAL-VOLUME check (§5 step 7 / C2). A valid
  3-legged chair (one leg silently dropped) passes `IsValid`/`IsSolid`; only the count+volume guard
  catches it.

---

## 7. Revolve & shell rule (correction C6)

- `RevSurface.Create` returns a **`Surface`, not a solid.** You must wrap it and cap it.
- The revolve **profile must start AND end ON the axis**, or be a closed curve. Otherwise the
  revolved surface is open and cannot be made solid.
- **Cap before shell.** `Brep.CreateOffset(..., solid=True)` (the shell operation) requires a
  **closed** Brep. Sequence: profile → `RevSurface.Create` → `Brep.CreateFromRevSurface` →
  `CapPlanarHoles(tol)` → verify closed → `Brep.CreateOffset(thickness, solid=True, ...)`.
- Pre-flight: confirm the axis is **coplanar with the profile** (C7) and the profile endpoints lie
  on the axis line within `tol`.

```python
rev = Rhino.Geometry.RevSurface.Create(profile_curve, axis_line, 0.0, math.radians(angle_deg))
brep = Rhino.Geometry.Brep.CreateFromRevSurface(rev, False, False)
brep = brep.CapPlanarHoles(tol) or brep            # close it
if not brep.IsSolid:
    raise RuntimeError("revolve not closed; cannot shell")
shell = Rhino.Geometry.Brep.CreateOffset(brep, -thickness, True, True, tol)  # solid shell
```

---

## 8. Vision-demotion rule (correction C4)

Anything **measurable** — count, dimension, position, symmetry — goes through `analyze_objects` /
bounding-box math, **not** vision. Vision is reserved for **profile-shape fidelity** and
qualitative *"does it look like X"*.

- Counts / volumes / distances / heights → `analyze_objects`, `get_object_info`, bbox arithmetic.
- *"Is the seat above the legs?"*, *"does the silhouette read as a wine glass?"* → vision.
- **Color each part before capture** (set `ObjectColor`, see §3) so vision answers the *reliable*
  question "is the red part above the blue part?" instead of the *unreliable* "is it 450 mm?".
- Verify `binary_questions` and `compare_to_reference` route to vision; `numeric_checks` and
  `ratio_checks` route to math.

---

## 9. Ratio-vs-absolute rule for the image pipeline (correction C5)

Image-derived **absolute** dimensions are guesses. Carry scale as a **range + confidence**
(`scale.overall_height_mm` may be `[min,max]`, `scale.confidence` ∈ {high,medium,low}).

- **Image pipeline:** the verify loop fires repairs on **scale-invariant `ratio_checks` only**.
  Never repair against an absolute `numeric_check` when `scale.confidence` is medium/low.
- **Text pipeline:** scale is `stated` / high confidence → verify and repair against **absolute**
  `numeric_checks`.
- `EXTRACT_PROFILE` is the most over-trusted step: **average the left and right silhouettes about
  the axis** to cancel perspective skew, flag low-confidence extractions, and **fall back to
  archetype profiles** chosen by the render-vs-reference loop rather than trusting raw pixel
  sampling.

---

## 10. Repair budget rule (correction C8)

Two independent limits, never a single global counter:

- **Per-failure-item budget:** each failing verify item gets up to **N attempts** (default `N = 3`).
  After N, mark that item **"could not fix"** and **surface it** to the caller — do not loop forever
  on one defect.
- **Global wall:** a hard ceiling on total repair iterations across all items (default `12`),
  independent of any single item's budget. Hitting the wall stops the loop and reports remaining
  defects.
- Repairs are driven by the verify block of the IR (§8/§9 routing). Each repair re-renders /
  re-measures only the affected parts.

---

## 11. Token-economy rules

- Prefer **`get_document_summary`** over full `get_objects` dumps.
- **Paginate** large queries: `offset`/`limit`, and `include_geometry=false` unless geometry is
  actually needed.
- **Low-res `capture_viewport`** for vision; render only at **decision points**, not every step.
- **Never re-query unchanged geometry** — read it from the scene-graph artifact instead.
- Prefer **typed MCP tools** over raw `execute_rhinoscript_python_code` /
  `execute_rhinocommon_csharp_code`. Fall back to `execute_*` **only** for ops with no typed tool
  (revolve, shell, network surface). The model over-loves writing Python; resist it.
- Scripts are executed via `${CLAUDE_SKILL_DIR}` and **only their stdout enters context** — print
  the one result you need (e.g. a GUID), not debug noise.
