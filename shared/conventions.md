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

def add_and_register(geom, part_id, layer_index=None, stage=None):
    """Bake geometry, return its GUID, and stamp the part_id ledger key."""
    attr = Rhino.DocObjects.ObjectAttributes()
    attr.Name = part_id
    attr.SetUserString("part_id", part_id)
    if stage is not None:
        attr.SetUserString("stage", stage)   # scoped idempotent delete key (§12)
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
- **UserString** `stage` = the build stage id (§12). This is the scoped delete key that makes a
  re-emit idempotent without wiping the whole model. Stamp it on **every** baked object.
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

### 5a. The DIRECTION-PIN idiom (every directional op)

A directional constructor — `Extrusion.Create`, `Surface.CreateExtrusion`,
`RevSurface.Create`, `Brep.CreateFromRevSurface`, `Curve.CreateExtrusion` — extrudes/revolves
**along the curve's own normal/tangent direction, which is NOT trustworthy.** A profile authored
on `WorldXZ` can extrude in `-Z` instead of `+Z`, so a seat meant to top out at 450 lands at 410
and the *count* still passes. The realized "seat extruded to 410 not 450" defect came from exactly
this: trusting the curve normal instead of pinning the result to the intended Z.

**Rule: after any directional create, read `GetBoundingBox(True)` and PIN the result to the
IR-intended Z** (translate so the intended face — top or bottom — sits exactly where the IR says),
then re-read to confirm. Pin to the face the IR anchors on (`at_surface`), not the midpoint.

```python
#! python3
import scriptcontext as sc
import Rhino
from Rhino.Geometry import Vector3d, Transform

# brep is the freshly extruded/revolved result; the IR says its TOP must be at intended_top_z
# (or its BASE at intended_base_z). Direction came out of the curve normal — do not trust it.
bb = brep.GetBoundingBox(True)
if anchor == "top":
    dz = intended_top_z - bb.Max.Z        # move so the top lands on the intended plane
elif anchor == "bottom":
    dz = intended_base_z - bb.Min.Z       # move so the base lands on the intended plane
else:
    dz = intended_center_z - 0.5 * (bb.Min.Z + bb.Max.Z)
if abs(dz) > sc.doc.ModelAbsoluteTolerance:
    brep.Transform(Transform.Translation(Vector3d(0, 0, dz)))
bb = brep.GetBoundingBox(True)            # re-read to CONFIRM the pin, never assume it took
assert abs((bb.Max.Z if anchor == "top" else bb.Min.Z)
           - (intended_top_z if anchor == "top" else intended_base_z)) <= sc.doc.ModelAbsoluteTolerance
```

The published `support` level (§13) a part exposes is the **pinned** value, so any part that attaches
to it via a `value_ref` resolves to where the geometry *actually* is, not where the curve normal put it.

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

---

## 12. Scoped idempotent staged emit

The emit protocol is **staged**, and every stage is **scoped-idempotent**. This defeats three
observed failure modes at once: the MCP `execute_*` wrapper silently running a script **twice**
(doubling the geometry); a single fragile op (e.g. a missing `Brep.CreateOffset` overload) rolling
back an entire one-shot build; and editing one feature forcing a wipe-and-rebuild of the whole model.

**Definitions.**
- A **stage** is a named, ordered slice of the IR `parts` (declared in the IR `stages[]` array, or
  inline per-part via `part.stage`; a part with neither belongs to the implicit `default` stage).
  Stages bake and reconcile as **one unit**, then **checkpoint**, before the next stage runs.
- **Scoped idempotent** means a stage script first **deletes only the live objects that belong to
  THIS stage** (matched by `UserString "stage"`, an explicit `part_id` allow-list, or a layer
  scope), then re-creates them once. Re-running the same stage script therefore converges to exactly
  one copy of that stage — and touches no other stage.

**The stage emit shape (non-negotiable).** Each stage is emitted as a SINGLE `execute_*` call whose
body is: *(1) the scoped purge preamble → (2) the snapshot for the create-then-find-newest shim →
(3) the guarded `Create*`/bake code for this stage's parts, each stamped with `UserString "stage"`.*
Generate the preamble with the helper rather than hand-writing the delete loop:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/stage_emit.py" \
  --stage bell_chamber \
  --part-ids bell_floor,bell_col_0,bell_col_1
```

It prints a ready-to-run preamble that defines and runs `purge_stage()`:

```python
#! python3
import scriptcontext as sc, Rhino, json
STAGE = 'bell_chamber'
PART_IDS = {'bell_floor', 'bell_col_0', 'bell_col_1'}

def _obj_in_stage(obj):
    a = obj.Attributes
    if a.GetUserString("stage") == STAGE:           # primary scope key
        return True
    if PART_IDS is not None and a.GetUserString("part_id") in PART_IDS:
        return True
    return False

def purge_stage():
    doomed = [o.Id for o in sc.doc.Objects if _obj_in_stage(o)]
    return sum(1 for g in doomed if sc.doc.Objects.Delete(g, True))   # quiet=True

_deleted = purge_stage()
# Snapshot AFTER the purge so deleted objects never confuse find_newest_guid (§2).
_stage_before_ids = set(o.Id for o in sc.doc.Objects)
print(json.dumps({"stage": STAGE, "deleted": _deleted}))
# ---- append this stage's guarded Create*/bake below; stamp stage on every bake ----
```

**Ordering of purge vs. snapshot is load-bearing.** The §2 `find_newest_guid` shim asserts *exactly
one new object*. Take its `_stage_before_ids` snapshot **after** `purge_stage()` so the per-bake diff
sees only genuinely new objects. (Phase-0's pre-mutation snapshot is per-stage, not per-build.)

**Stage boundary = bake + reconcile + checkpoint.** A stage is "done" only when its parts baked,
`reconcile.py --stage <id>` passes for that stage's nodes, and the modeling skill writes a
**checkpoint** into the scene-graph (`checkpoints[]`: `{stage, status:"passed", revision, part_ids}`)
and **Saves the .3dm** (`Rhino.RhinoDoc.ActiveDoc.Save(...)` via the MCP save tool). The save makes
the checkpoint the persisted ledger (defeats the "30-minute session never saved" loss).

**Why this self-corrects the double-execution.** If the wrapper runs the stage script twice, the
second run's `purge_stage()` deletes the first run's objects (they carry the same `stage` tag) before
re-creating them, so the document ends with exactly one copy. No whole-model wipe is needed.

**Why this bounds a fragile-op failure.** A stage failure (a Create returns null) aborts only that
stage; earlier stages are already baked, checkpointed and saved. The repair loop re-emits **just the
failed stage** (its idempotent purge cleans any partial bake from the aborted attempt first). No
prior stage is touched, and the count never doubles.

**Scoped re-emit for an edit.** To change the bell chamber on a 931-solid tower, re-run only the
`bell_chamber` stage script: it purges + rebuilds those parts, you reconcile `--stage bell_chamber`,
re-checkpoint, and re-verify only the stages whose `depends_on` includes `bell_chamber`. The shaft
and base are never rebuilt.

**Ledger consistency across stages.** The scene-graph stays consistent because (a) each node records
its `stage`; (b) a stage re-emit replaces exactly that stage's nodes (delete by stage, then append
the new GUIDs) and bumps `revision`; (c) `checkpoints[]` records the last passing revision per stage
so a resumed/edited build knows what is already good. `reconcile.py --stage <id>` filters expected
nodes to that stage and ignores live objects tagged with other stages, so a per-stage diff never
reports another stage's parts as EXTRA.

**Sizing a stage.** One stage per natural assembly (base / shaft / colonnade / bell chamber), or per
`count`-instanced family, capped so a stage is a few-dozen solids — small enough that a re-emit is
cheap, large enough that reconcile has a meaningful unit. Booleans must stay **within one stage**
(their inputs and result share a stage) so a `child_of` consumption is never split across a stage
boundary.

### 12a. Sidecar persistence + resume

There is **no dedicated MCP save tool** (correction D). Persistence is done by `execute_*` calling
RhinoCommon directly: `sc.doc.WriteFile(path, opts)` (or `Rhino.RhinoDoc.ActiveDoc.SaveAs(path)`),
which **returns a bool** — you must check it is `True` before writing any JSON ledger.

**Sidecar layout.** State lives next to the `.3dm` in a sidecar directory:

```
<name>.3dm
<name>.rhino-skills/
  build-plan.json                 # the IR (intent)
  scene-graph.json                # the realized ledger (latest revision)
  checkpoints/
    0001-base.json                # per-stage frozen checkpoint (status, revision, connectivity)
    0002-shaft.json
    ...
```

**Write ORDER is load-bearing — ledger AFTER geometry.** Geometry is the source of truth; the JSON
is a hint. So: (1) `ok = sc.doc.WriteFile(...)`; (2) **only if `ok is True`**, write the JSON
sidecar; (3) write each JSON file **atomically** with `tmp + os.replace` so a crash mid-write never
leaves a half-written ledger. If `WriteFile` returns `False` (or you are uncertain it persisted),
**do not** advance the checkpoint — flag it and stop, rather than recording a checkpoint the `.3dm`
does not back.

```python
#! python3
import scriptcontext as sc, os, json, tempfile

def save_with_sidecar(dotrhino_path, sidecar_dir, scene_graph, checkpoint_obj, stage_name):
    opts = sc.doc.CreateDefaultWriteOptions() if hasattr(sc.doc, "CreateDefaultWriteOptions") else None
    ok = sc.doc.WriteFile(dotrhino_path, opts) if opts is not None else sc.doc.WriteFile(dotrhino_path)
    if ok is not True:                          # D: confirm True BEFORE the sidecar
        raise RuntimeError("WriteFile returned %r; NOT writing sidecar (geometry not persisted)" % (ok,))
    os.makedirs(os.path.join(sidecar_dir, "checkpoints"), exist_ok=True)
    _atomic_json(os.path.join(sidecar_dir, "scene-graph.json"), scene_graph)
    n = checkpoint_obj["revision"]
    _atomic_json(os.path.join(sidecar_dir, "checkpoints", "%04d-%s.json" % (n, stage_name)), checkpoint_obj)
    return True

def _atomic_json(path, obj):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)                   # atomic on POSIX + Windows
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
```

**Resume (Phase 0.5).** A `.3dm` round-trip does **not** preserve `System.Guid`s, so the saved GUID
in the sidecar is only a **hint**. On reopen, re-bind `part_id -> live GUID` from the **authoritative**
`UserString "part_id"` stamped on each object (§2/§3), not from the saved GUID:

```python
#! python3
import scriptcontext as sc, json

def rebind_part_ids(saved_scene_graph_path):
    """Phase 0.5: rebuild the GUID ledger from live UserStrings (authoritative)."""
    live = {}
    for o in sc.doc.Objects:
        pid = o.Attributes.GetUserString("part_id")
        if pid:
            live[pid] = o.Id                    # part_id -> CURRENT guid (saved guid is just a hint)
    sg = json.load(open(saved_scene_graph_path))
    for node in sg.get("nodes", []):
        pid = node.get("part_id")
        if pid in live:
            node["guid"] = str(live[pid])       # overwrite stale saved guid with the live one
    return sg, live
```

Because edges are **part_id-keyed** (C1, §13), the connectivity ledger survives the round-trip
unchanged; only the per-node `guid` is re-bound. After re-binding, a resumed build trusts each
stage's last `checkpoints[]` entry (status + revision + connectivity_status) to know which stages are
already good and which must re-emit.

---

## 13. Connectivity as a NUMERIC OBLIGATION (correction C9)

The dominant observed failure was **false confidence**: the framework declared success while
inter-part *connectivity* defects went uncaught — balusters not reaching a rising helical handrail,
columns floating above the floor, arches not seated on column tops — and a human caught every one
*by eye*. §1–§12 guarantee each part is individually well-formed (valid, solid, right count, right
volume) but say **nothing about whether parts actually TOUCH where they must.** C9 closes that hole
by turning every contact relation into a **measured numeric obligation** that a stage cannot pass
without satisfying.

**The triad — defense in depth, strict order.**
1. **PREVENT** (Phase 3, relational-IR): attach geometry resolves to the *correct literal*. A
   baluster's top is a `value_ref {part:"rail", of:"z_at_angle", at:θ}` reading the rail's
   **published `support`** (§ schema `support`/`value_ref`), not a guessed number, so it is built to
   reach the rail in the first place.
2. **DETECT** (Phase 5/6, this section): `check_connectivity` measures the **realized** gap between
   the two live solids and flags out-of-band / uncovered. Detection does **not** depend on the
   resolver — it reads the document — so it stands alone even if PREVENT was wrong.
3. **ENFORCE** (the completeness clause below): UNCOVERED = FAIL. This is the clause that makes
   "declared success with gaps" *impossible*.

**A1 — NO CIRCULAR PROBE. Measure the realized gap between two LIVE SOLIDS, by GUID.** The gap input
is **two GUIDs**, resolved from the live document (via `part_id` → live GUID, §2). Measure with
`Brep.ClosestPoint` / the realized solid-to-solid `MinDistanceBetween` (face-pair minimum). **NEVER**
measure against a probe point whose coordinates come from the IR intent you are verifying — that just
re-confirms the bug. The intent is what produced the (possibly wrong) geometry; only document-truth
can catch it.

```python
#! python3
import scriptcontext as sc, Rhino

def realized_gap(guid_a, guid_b):
    """Signed gap in model units between two LIVE breps (A1). + = real space, - = overlap.
    Inputs are GUIDs read from the document, never IR coordinates."""
    tol = sc.doc.ModelAbsoluteTolerance
    ba = sc.doc.Objects.FindId(guid_a).Geometry      # live geometry, by GUID
    bb = sc.doc.Objects.FindId(guid_b).Geometry
    # If the realized solids overlap, the gap is NEGATIVE (penetration depth).
    inter = Rhino.Geometry.Brep.CreateBooleanIntersection([ba], [bb], tol)
    if inter and len(inter) > 0:
        bbx = inter[0].GetBoundingBox(True)          # crude overlap depth from the intersection solid
        return -min(bbx.Max.X - bbx.Min.X, bbx.Max.Y - bbx.Min.Y, bbx.Max.Z - bbx.Min.Z)
    # Otherwise the positive gap is the nearest realized point on B from A and vice-versa.
    return min(_closest(ba, bb), _closest(bb, ba))

def _closest(src, tgt):
    """Nearest realized distance from src's surface samples onto tgt's solid (Brep.ClosestPoint)."""
    best = float("inf")
    for v in src.Vertices:                           # vertices are exact realized points on src
        ok, u, w, ci, n, dist = tgt.ClosestPoint(v.Location, 0.0)  # Brep.ClosestPoint -> nearest pt on tgt
        if ok and dist < best:
            best = dist
    return best
```

`Brep.ClosestPoint(testPoint)` returns the nearest point on the *realized* solid, so the gap is read
from document truth (A1). For curved/helical members (A4) sample edge points along the rail rather
than only vertices so the nearest-arc-length point is found.

**A2 — ORIENTED HANDLE, never AABB arithmetic for non-axis-aligned relations.** A world-AABB is
*unsound* for helical/rotated parts: two far-apart points on a helix can have touching bounding boxes.
The scene-graph node therefore carries an **`obb`** (oriented bbox: plane origin + x/y axes + extents)
and/or **`centroid`/`contact_point`**, captured at bake time (scene-graph schema). `check_connectivity`
uses these only for orientation and coarse culling and **must not** fall back to AABB arithmetic to
decide a non-axis-aligned relation — the gap itself always comes from the realized solid-to-solid
measurement (A1).

**A3 — PER-RELATION-TYPE TOLERANCE (a directed band, not one symmetric ±tol).** The measured signed
gap `g` is judged against a band chosen by the relation `type` (override the document tolerance with
the relation's optional `tol`):

| relation        | band on signed gap `g`         | meaning                                            |
|-----------------|--------------------------------|----------------------------------------------------|
| `on_top_of`     | `0 <= g <= +tol`               | rests on the surface; **any** penetration is a FAIL |
| `coincident`    | `|g| <= tol`                   | faces flush                                        |
| `lands_on`      | `-penetration <= g <= +tol`    | base reaches the support, may seat slightly in     |
| `meets`         | `-penetration <= g <= +tol`    | two ends abut                                      |
| `interpenetrate`| `-2 <= g <= -0.5` (mm; C3)     | union overlap **must be NEGATIVE** 0.5–2 mm        |
| `spans`         | both endpoint gaps in band     | bridges to its support at both ends                |
| `spans_between` | both endpoint gaps in band     | supported at `to` **and** `to2`                    |

A `+12 mm` gap on a `lands_on` (column floating above the floor) and a `-3 mm` gap on an `on_top_of`
(arch sunk into the column) are **both** `out_of_band` = FAIL.

**A4 — CURVED SUPPORTS by realized nearest-point, not a face label.** "Top of a helical rail" is a
**Z-at-arc-length**, not a face you can name. For any curved/helical/rotated support set the relation
`at_surface: "realized"`; the gap is then the distance to the **nearest point on B's realized solid**
(the A1 measurement), so curved and helical supports are handled by realized distance, never a scalar
face height. The rail's published `support.helix_z` only seeds PREVENT; DETECT still measures the live
solid.

**B1 — BATCHED, violations-only, one execute per stage.** Do **not** round-trip per relation. The
connectivity sweep is **ONE `execute_rhinoscript_python_code` call per stage** whose body loops every
declared relation in that stage's scope, measures each realized gap in-Rhino, and returns **only the
violations** (`out_of_band` + `uncovered`) as compact JSON — never the passing gaps. Same shape as
`reconcile.py`: a summary goes in, only violations come out.

```python
#! python3
import scriptcontext as sc, Rhino, json
# EDGES: [{type, from, to, to2?, at_surface, tol, penetration?, floating}] for THIS stage's scope.
def sweep(edges):
    violations = []
    for e in edges:
        ga, gb = _live(e["from"]), _live(e["to"])
        if ga is None or gb is None:                       # C2: endpoint deleted/never built
            violations.append({"edge": _key(e), "status": "uncovered"})
            continue
        g = realized_gap(ga, gb)                            # A1 realized solid-to-solid
        lo, hi = band_for(e)                                # A3 per-type band
        if not (lo <= g <= hi):
            violations.append({"edge": _key(e), "gap": round(g, 4), "status": "out_of_band",
                               "measured_between": [str(ga), str(gb)], "band": [lo, hi]})
    # completeness (F): every NON-floating in-scope part needs >=1 measured contact
    for pid in _nonfloating_parts_in_scope():
        if not _has_measured_contact(pid, edges):
            violations.append({"edge": {"from": pid, "to": None}, "status": "uncovered"})
    print(json.dumps({"violations": violations}))           # ONLY violations enter context
```

**B2 — SAMPLE symmetric families.** A radial/helical array of N members: measure **all N on the
initial bake**; on re-emit checkpoints measure a **sample** (first / middle / last + any previously
flagged member); re-measure the **full N only when the generating `array` rule changed**.

**B3 — STAGE SCOPE.** `check_connectivity --stage <id>` evaluates **only** relations whose endpoints
are in the current or already-closed stages (mirror `reconcile.py --stage`). An edge into a
not-yet-built stage is deferred, not failed.

**C1 — CROSS-STAGE INVALIDATION; edges keyed by part_id ONLY (never GUID).** Re-emitting stage X must
mark `connectivity_status: "not_run"` on the checkpoint of **every** stage that has a relation
*crossing into* X — even though that stage's own geometry was untouched — because re-baking the rail
can move it and orphan every baluster. The invalidation works **because edges are keyed by part_id**:
the re-bake changes GUIDs but not part_ids, so the crossing edges are still found.

**C2 — PRE-PURGE REFERENTIAL CHECK.** Deleting a stage (or part) must flag every edge pointing at a
now-deleted part_id as **`uncovered`**, never silently pass. A deleted part = no relation to check =
a *false pass by omission*; C2 turns that omission into an explicit FAIL.

**F — FLOATING OPT-OUT.** Parts intended to float (pendant, cantilever tip, free finial) carry
`floating: true` and are **EXEMPT** from the completeness rule, so they generate no false UNCOVERED
pressure. They may still declare measured relations; they are simply not *required* to own one.

**THE COMPLETENESS CLAUSE (ENFORCE).** Every **non-floating** part that participates in an assembly
**must have at least one declared + measured contact.** A declared contact with **no measurement**
is **UNCOVERED = FAIL**. A stage is GREEN (`connectivity_status: "green"`, eligible for
`checkpoints[].status: "passed"`) **only** when its in-scope sweep returns **zero `out_of_band` and
zero `uncovered`** entries. This is the single clause that makes "declare success while gaps remain"
impossible: silence is no longer a pass — an unmeasured obligation is a failure.
