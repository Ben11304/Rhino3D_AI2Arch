# The Scene-Graph Artifact

The scene-graph is the **externalized world model** for the Rhino skill suite: the single
re-read-every-step record of what *actually* exists in the live document, as opposed to the
build-plan IR, which records only *intent*. This skill (`rhino-scene-state`) is the sole owner and
writer of this artifact. Every other skill reads it; none of them write it.

Mental model (from [`../../shared/conventions.md`](../../shared/conventions.md)): the LLM spatial
deficit is fixed by *externalizing spatial state* into artifacts that are re-read each step instead
of held in context. The scene-graph is that artifact for realized geometry. **Never hold the GUID
table or part positions in your head — read them back from this artifact every step.**

The artifact conforms to [`../schema/scene-graph.schema.json`](../schema/scene-graph.schema.json)
and is the realized counterpart to [`../../shared/build-plan.schema.json`](../../shared/build-plan.schema.json).

---

## 1. Top-level shape

```json
{
  "object": "four-legged dining chair",
  "units": "mm",
  "tolerance": 0.01,
  "world_frame": "WorldXY",
  "revision": 7,
  "last_op": "boolean_union:frame",
  "nodes": [ ... ],
  "edges": [ ... ]
}
```

- `object`, `units`, `tolerance`, `world_frame` are copied verbatim from the IR. `units` MUST equal
  the live `sc.doc.ModelUnitSystem` and `tolerance` defaults to the live
  `sc.doc.ModelAbsoluteTolerance` (read live, never hardcode — conventions §1).
- `revision` is bumped once per mutation cycle so a re-read can detect a stale copy. `last_op` is a
  short human label of the mutation that produced this revision (e.g. `bake:leg[2]`,
  `boolean_difference:seat-cutout`).

---

## 2. Nodes — the part_id -> GUID ledger + the bbox/dimension ledger

Each **node** is one realized part. It mirrors exactly one IR part (by `part_id`) and records the
truth captured **at bake time** — the moment `AddBrep` / `AddCurve` / `AddSurface` returned a GUID.

| field                   | meaning |
|-------------------------|---------|
| `part_id`               | stable label = IR `part.id` = the object's `UserString "part_id"`. The fallback resolver. |
| `guid`                  | the Rhino `System.Guid` (string) returned at bake. The **canonical** handle (C1). |
| `name`                  | Rhino object `Name`, set equal to `part_id`. |
| `layer`                 | Rhino layer name (IR `part.layer`, or a default layer named after `object`). |
| `primitive`             | how it was produced: a primitive (`box`/`cylinder`/`sphere`/`cone`/`plane`/`interpolated_curve`) or an operation result (`loft`/`sweep1`/`revolve`/`extrude`/`shell`/`boolean`). |
| `frame`                 | the local construction frame it was authored on (`origin` + optional named plane / axes), copied from the IR. |
| `bbox`                  | world-axis-aligned bounding box `{min:[x,y,z], max:[x,y,z]}` from `GetBoundingBox(True)`. |
| `dims`                  | key realized dimensions in `units` (e.g. `{x,y,z}`, `{radius,height}`, or `{width,depth,height}` derived from the bbox). |
| `volume`               | solid volume (`units^3`) from `VolumeMassProperties.Compute`, for closed solids only. |
| `is_solid`              | `Brep.IsSolid` — closed solid vs. open curve/surface. |
| `expected_solid_count`  | for operation results: how many disjoint solids the result SHOULD contain per the IR `boolean_plan` (drives the C2 partial-failure check). |
| `provenance`            | why the part exists, copied from the IR. |
| `color`                 | optional vision color for a colored-part capture (C4) — a render concern, not identity. |

The **node is the ledger.** The `part_id -> guid` pair is the GUID ledger (C1); the `bbox` + `dims`
+ `volume` are the dimension ledger that the reconcile loop diffs against the live document.

### Bounding box & dimensions are captured, not computed in-head
`bbox` comes straight from `geom.GetBoundingBox(True)` (world-aligned) at bake time. `dims` are
either the authored primitive dimensions or derived from the bbox span
(`width = max.x - min.x`, etc.). Recording both lets reconcile catch a part that baked at the wrong
size (MIS-SIZED) without re-querying full geometry.

---

## 3. Edges — realized relations

Each **edge** mirrors one IR relation between two parts:

```json
{ "type": "interpenetrate", "from": "leg_fl", "to": "seat", "penetration": 1.0 }
```

- `type` ∈ `coincident | on_top_of | symmetric_about | child_of | interpenetrate | lands_on | meets
  | spans | spans_between` (the last four are the v2 measured-contact obligations, §13/C9).
- `from` / `to` are `part_id`s **only, never GUIDs** (C1): a re-bake gives a part a new GUID but the
  same `part_id`, so part_id-keyed edges survive a re-emit and let the cross-stage invalidation
  (§8/C1) find every edge crossing into a re-emitted stage. For `symmetric_about`, `to` may be a
  symmetry/frame name. For `spans_between`, `to` is the first support and `to2` the second.
- `child_of` models an operation result owning the inputs it consumed (e.g. the unioned `frame`
  node is the parent; the consumed `leg_*`/`seat` nodes are children — their GUIDs are now
  invalid and must be resolved through the parent). `child_of`/`symmetric_about` are **logical**,
  not measured.
- `interpenetrate` records the **realized** overlap depth feeding a boolean union; per correction
  C3 it must be 0.5–2 mm, never coincident. `penetration` is required on these edges.
- Optional `at_surface` (`top|bottom|nearest|centerline|realized`), `tol` (per-edge band override),
  and `floating` (the F opt-out, §8) mirror the IR relation. The **measured gap** and its
  pass/out_of_band/uncovered status do **not** live on the edge — they live in the per-checkpoint
  `connectivity` list (§8).

Edges let a re-read answer "is the seat *on top of* the legs?" structurally before any vision call.

---

## 3a. Measurement truth — `object_count` is NEVER authoritative

The document `object_count` (from `get_document_summary`) is a **diagnostic, not a
signal**. In a real session it reported **54** while only **18** real BREPs existed: a
double execution plus phantom non-solid leftovers had inflated the total. Trusting that
number would have declared the build wrong (or right) for the wrong reason.

**Rule:** the authoritative live count is the number of **distinct, correctly-tagged
part_ids** — objects enumerated by their `UserString "part_id"` (correction C1), *not* the
document object total. Resolve the realized count in this strict order and never fall back
to `object_count`:

1. **By part_id (authoritative).** Count distinct `UserString "part_id"` values across the
   live objects. This is the truth: it ignores untagged leftovers and exposes double-bakes.
2. **By layer.** When a part_id is absent, the IR `part.layer` partitions objects into the
   expected layer buckets; a layer holding more objects than its declared parts is a
   leftover signal.
3. **By `objects_by_type`.** Only as a coarse last resort (e.g. "how many BREPs vs curves"),
   never as the per-part identity.

`reconcile.py` consumes this directly. It reports `actual_objects` (the raw `object_count`)
**for transparency only** and asserts on the part_id-keyed diff, which adds two categories on
top of MISSING/EXTRA/MIS-SIZED:

| category    | meaning | evidence |
|-------------|---------|----------|
| **PHANTOM** | a live object carrying **no** `part_id` UserString — an untagged leftover that inflates `object_count`. Repair hint: delete-untagged. | E2 (54 vs 18) |
| **DUPLICATE** | one declared `part_id` resolved to **more than one** live object — the double/triple-execution signature. | E1 |

So a summary where `object_count = 54` but the part_id enumeration yields the 18 declared
ids (and 36 phantoms) is reported as **18 matched + 36 PHANTOM**, not "54 objects, off by
36 nodes". The scene-graph's identity is `part_id`, and the count follows identity.

> **Per-node count (C2) also comes from identity, not `object_count`.** The post-boolean
> disjoint-solid count is read from the object's own `solid_count`/`piece_count`; if the cheap
> summary omits it, reconcile falls back to the **part_id-enumeration count** for that node
> (how many live objects share the id) rather than silently skipping the check. The aggregate
> `object_count` is *never* used to derive a per-node count.

---

## 4. The scene-graph is re-read every step as canonical world state

The whole point of the artifact is that it is **the** source of truth between steps, replacing
in-context memory:

1. **Before** a mutation, read the scene-graph to resolve the GUIDs of the parts you are about to
   operate on (never re-derive them from the IR — the IR has no GUIDs).
2. **After** a mutation, append/update the affected nodes (new GUID, new bbox, new volume), add
   any `child_of` edges for consumed inputs, bump `revision`, set `last_op`.
3. The reconcile loop ([`../scripts/reconcile.py`](../scripts/reconcile.py)) then diffs the
   scene-graph against a cheap live document summary to confirm the realized scene matches the
   declared one.

Because the artifact is re-read rather than remembered, a part's position/size/handle can never
silently drift in context — it is always grounded in the last captured truth.

---

## 5. Naming & layer conventions (mirror conventions §3)

Every object is identifiable three ways, set together at bake time:

- **Name** = `part_id` (human-readable in the Rhino object table).
- **Layer** = the IR `part.layer`, or a default layer named after `object`.
- **UserString `"part_id"`** = the canonical ledger key — survives renames, and is the **fallback
  resolver**. Optionally `UserString "provenance"` mirrors the IR provenance.

Coloring for a vision capture sets `attr.ColorSource = ObjectColorSource.ColorFromObject` and
`attr.ObjectColor`; color is recorded in the node's `color` field but is **never** an identity
handle.

---

## 6. GUID-or-UserString resolution order (correction C1)

When you need the live Rhino object for a `part_id`, resolve in this strict order and **stop at the
first hit**:

1. **By GUID** — `sc.doc.Objects.FindId(guid)` using the node's `guid`. This is the canonical path
   and the only O(1) lookup. Use it whenever the node has a valid, non-unset GUID.
2. **By UserString fallback** — if the GUID is missing/unset/stale (e.g. a boolean consumed the
   input and produced a new object, invalidating the old handle), scan the object table for the
   object whose `UserString "part_id"` equals the node's `part_id`, then **backfill** the node's
   `guid` with the found `Id` so the next lookup is O(1) again.
3. **By Name** — last resort only: match `o.Attributes.Name == part_id`. Names are not guaranteed
   unique and can be renamed by the user, so treat a Name hit as low-confidence and re-tag the
   object's `UserString "part_id"` before trusting it.

```python
#! python3
import scriptcontext as sc

def resolve(node):
    # 1. canonical: by GUID
    guid = node.get("guid")
    if guid:
        obj = sc.doc.Objects.FindId(guid)   # System.Guid lookup; None if gone
        if obj is not None:
            return obj
    # 2. fallback: by UserString part_id (then backfill node['guid'])
    pid = node.get("part_id")
    if pid:
        for o in sc.doc.Objects:
            if o.Attributes.GetUserString("part_id") == pid:
                node["guid"] = str(o.Id)    # backfill the ledger
                return o
    # 3. last resort: by Name (low confidence)
    if pid:
        for o in sc.doc.Objects:
            if o.Attributes.Name == pid:
                o.Attributes.SetUserString("part_id", pid)
                sc.doc.Objects.ModifyAttributes(o, o.Attributes, True)
                node["guid"] = str(o.Id)
                return o
    return None
```

A consumed input (e.g. a leg eaten by a boolean union) will resolve to **nothing** by GUID and to
**the union result's owner** structurally via its `child_of` edge — that is expected, and the
reconcile loop treats it as MISSING-by-consumption, not an error, when a `child_of` edge explains
it.

---

## 7. Token economy when reading the scene-graph (mirror conventions §11)

- The scene-graph itself is the cheap in-context world state — **read it instead of re-querying the
  document.** Never call `get_objects` to learn something the scene-graph already records.
- Refresh nodes from the live document only at **decision points**, and then prefer
  `get_document_summary` (a cheap aggregate) over a full `get_objects` dump.
- When a full per-object query is unavoidable, **paginate** (`offset`/`limit`) and set
  `include_geometry=false` — the bbox/volume aggregates are enough for reconcile.

---

## 8. Connectivity — the realized-gap obligation (correction C9 / conventions §13)

§1–§7 prove each part is *individually* well-formed; they say **nothing about whether parts actually
TOUCH where they must.** The dominant v2 failure was **false confidence**: the framework declared
success while balusters never reached a rising helical handrail, columns floated above the floor, and
arches did not seat on column tops — a human caught every one by eye. C9 turns every contact relation
into a **measured numeric obligation** a stage cannot pass without satisfying. The full rule set lives
in [`../../shared/conventions.md`](../../shared/conventions.md) §13; this section documents how the
scene-graph artifact and its scripts realize it.

**The triad (defense in depth, strict order).** PREVENT (Phase 3, `value_ref`/`support` resolves the
attach literal) → DETECT (Phase 5/6, the realized solid-to-solid sweep below) → ENFORCE (the
completeness clause: UNCOVERED = FAIL). DETECT reads document truth, so it stands alone even if
PREVENT was wrong.

### 8.1 The realized gap is measured between two LIVE solids, by GUID (A1/A4)

The gap input is **two GUIDs** resolved from the live document (via the §6 `part_id → live GUID`
resolver), **never** an IR coordinate — a probe point taken from the intent you are verifying just
re-confirms the bug. Measure with the realized solid-to-solid `Brep.ClosestPoint` /
boolean-intersection sign: positive = a real space, negative = penetration depth. For a
curved/helical support (`at_surface: "realized"`, A4) sample **edge points along the member**, not
only vertices, so the nearest **Z-at-arc-length** point on the rail is found — "top of a helical
rail" is not a face you can name.

```python
#! python3
import scriptcontext as sc, Rhino

def realized_gap(guid_a, guid_b):
    """Signed gap in model units between two LIVE breps (A1). + = space, - = overlap.
    Inputs are GUIDs read from the document, NEVER IR coordinates."""
    tol = sc.doc.ModelAbsoluteTolerance
    ba = sc.doc.Objects.FindId(guid_a).Geometry          # live geometry, by GUID
    bb = sc.doc.Objects.FindId(guid_b).Geometry
    inter = Rhino.Geometry.Brep.CreateBooleanIntersection([ba], [bb], tol)
    if inter and len(inter) > 0:                         # overlap -> NEGATIVE gap (penetration)
        bx = inter[0].GetBoundingBox(True)
        return -min(bx.Max.X - bx.Min.X, bx.Max.Y - bx.Min.Y, bx.Max.Z - bx.Min.Z)
    return min(_closest(ba, bb), _closest(bb, ba))       # positive realized solid-to-solid distance

def _closest(src, tgt):
    """Nearest realized distance from src's edge/vertex samples onto tgt (A4)."""
    best = float("inf")
    pts = [v.Location for v in src.Vertices]
    for e in src.Edges:                                  # sample along edges for curved members (A4)
        c = e.EdgeCurve
        if c is not None:
            d = c.Domain
            pts += [c.PointAt(d.ParameterAt(k / 10.0)) for k in range(11)]
    for p in pts:
        rc = tgt.ClosestPoint(p, 0.0)                    # Brep.ClosestPoint -> nearest pt on realized solid
        if rc and rc[0] and rc[5] < best:
            best = rc[5]
    return best
```

The classifier ([`../scripts/check_connectivity.py`](../scripts/check_connectivity.py)) **only trusts
an `--actual` measurement that carries `measured_between: [guidA, guidB]`** — the proof it was
solid-to-solid against the document. A gap with no two-GUID proof (or no numeric value) is
**UNCOVERED**, never a pass. This is what makes A1 enforceable offline.

### 8.2 The oriented handle — never AABB arithmetic for non-axis-aligned parts (A2)

A world-AABB (`bbox`) is **unsound** for helical/rotated parts: two far-apart points on a helix can
have touching bounding boxes. So every node may carry an **`obb`** (oriented bbox: plane
origin + x/y axes + extents) and a **`centroid`**/**`contact_point`**, captured at bake time. The
sweep uses these only for orientation + coarse culling; the gap itself **always** comes from the
realized solid-to-solid measurement (§8.1). `check_connectivity.py` **never** derives a gap from a
`bbox`. The bake-time obb capture (from `stage_emit.py`'s emitted postamble):

```python
plane = Rhino.Geometry.Plane.WorldXY
rc, fitted = Rhino.Geometry.Plane.FitPlaneToPoints([v.Location for v in geom.Vertices])
if fitted is not None and fitted.IsValid:               # a plane that travels with a rotated member
    plane = fitted
box = geom.GetBoundingBox(plane)                        # ORIENTED box, not the world-AABB
vm  = Rhino.Geometry.VolumeMassProperties.Compute(geom) # centroid for culling
```

### 8.3 The batched, violations-only sweep — ONE execute per stage (B1)

Do **not** round-trip per relation. [`../scripts/stage_emit.py`](../scripts/stage_emit.py)
`--connectivity-edges <ir|edges>` emits a **postamble** the caller appends AFTER its bake code, in the
**same** `execute_rhinoscript_python_code` call. The postamble loops this stage's declared contact
edges, measures each realized gap by GUID (§8.1), applies the per-relation-type band (§8.4) **in
Rhino**, captures each baked part's `obb`/`centroid` into the ledger, and prints **only the
violations** as compact JSON — passing gaps stay server-side:

```json
{ "stage": "baluster_stage",
  "uncovered":   [ { "edge": {"type":"lands_on","from":"baluster#7","to":"rail"}, "status":"uncovered" } ],
  "out_of_band": [ { "edge": {"type":"lands_on","from":"baluster#3","to":"rail"},
                     "gap": 12.0, "band": [-2.0, 0.5],
                     "measured_between": ["aaaa…","bbbb…"], "status": "out_of_band" } ],
  "obb": [ { "part_id":"baluster#3", "obb": {…}, "centroid":[…] }, … ] }
```

That violations JSON is the `--actual` input to `check_connectivity.py`, which classifies offline and
enforces completeness. Same shape as `reconcile.py`: a summary goes in, only violations come out. In
the B1 violations-only shape a **missing** edge means *measured + passed in-Rhino* (not uncovered), so
the classifier credits it toward completeness without re-measuring.

### 8.4 Per-relation-type band (A3) and array sampling (B2)

The band is **directed**, never one symmetric ±tol (table in conventions §13/A3):
`on_top_of` ⇒ `[0, +tol]` (penetration FAILS); `coincident` ⇒ `[-tol, +tol]`;
`lands_on`/`meets`/`spans`/`spans_between` ⇒ `[-penetration, +tol]`; `interpenetrate` ⇒ `[-2, -0.5]`
mm (C3, overlap must be **negative**). A `+12 mm` `lands_on` (column floating) and a `-3 mm`
`on_top_of` (arch sunk in) are **both** `out_of_band` = FAIL.

An array part (`array.count = N`) is ONE family; its relation fans out to instances `<id>#0…#N-1`,
each measured against the support. On the **initial bake** measure all N; on a **re-emit checkpoint**
(`check_connectivity.py --recheck`) measure only **first/middle/last + any `--flagged`** member;
re-measure the **full N** only when the generating `array` rule changed (`--array-rule-changed`). A
sampled-out instance is neither evaluated nor pressured for completeness that round.

### 8.5 Stage scope (B3)

`check_connectivity.py --stage <id>` (and the emitted sweep) evaluate **only** relations whose
endpoints are in the **current or already-closed** stages (`--closed-stages a,b`) — mirroring
`reconcile.py --stage`. An edge into a not-yet-built stage is **deferred**, not failed.

### 8.6 Connectivity checkpoints, cross-stage invalidation (C1), pre-purge referential check (C2)

Each `checkpoints[]` entry carries `connectivity` (the `connectivity_entry` list, violations
persisted) and `connectivity_status` ∈ `green | violations | not_run`. A checkpoint may be
`status: "passed"` **only** when `connectivity_status: "green"` (zero `out_of_band`, zero
`uncovered` for non-floating edges).

- **C1 — cross-stage invalidation.** Re-emitting stage X can move its parts (new GUIDs, same
  `part_id`s) and **orphan** every part that lands on them. `reconcile.py --expected <graph>
  --invalidate-stage X [--apply]` finds every **other** stage that owns a contact edge reaching INTO
  X (the edge's `from` is in that stage; its `to`/`to2` support is in X) and sets that stage's
  `connectivity_status: "not_run"`, so its sweep MUST re-run before it is green again. This works
  **because edges are part_id-keyed** — the re-bake changes GUIDs, not part_ids. Re-emitting a leaf
  stage (nothing reaches into it) invalidates nothing.

- **C2 — pre-purge referential check.** Deleting a stage/part must **not** silently pass the now-
  orphaned contacts (a deleted part = no relation to check = a *false pass by omission*).
  `reconcile.py --expected <graph> --purge-stage X` (or `--purge-parts a,b`) enumerates every edge
  whose `from`/`to`/`to2` references a to-be-deleted `part_id` and emits an `uncovered`
  connectivity entry for each (exit non-zero). With `--apply` it folds those entries into the owning
  stages' checkpoints and flips their `connectivity_status` to `violations`.

### 8.7 The completeness clause (ENFORCE) and the floating opt-out (F)

Every **non-floating** part that participates in an assembly (is the `from` or `to`/`to2` of any
declared contact edge in scope) **must own ≥1 declared + measured contact.** A declared contact with
**no measurement** is **UNCOVERED = FAIL**; a participating non-floating part with no measured contact
is **UNCOVERED = FAIL**. A stage is GREEN only when its in-scope sweep returns **zero** `out_of_band`
and **zero** `uncovered`. This single clause makes "declare success while gaps remain" impossible:
silence is no longer a pass.

Parts **intended** to float (pendant tip, cantilever end, free finial) opt out: IR `part.floating:
true`, mirrored on the scene-graph **edge** as `edge.floating: true` (the node schema has no
`floating` field — a part's floating status is **derived** from owning a floating edge). A floating
edge is informational: it generates no `uncovered` pressure, no `out_of_band` FAIL, and does not
credit completeness. It may still declare a measured relation; it is simply not *required* to own one.
