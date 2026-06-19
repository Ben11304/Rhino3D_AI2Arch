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

- `type` ∈ `coincident | on_top_of | symmetric_about | child_of | interpenetrate`.
- `from` / `to` are `part_id`s (for `symmetric_about`, `to` may be a symmetry/frame name).
- `child_of` models an operation result owning the inputs it consumed (e.g. the unioned `frame`
  node is the parent; the consumed `leg_*`/`seat` nodes are children — their GUIDs are now
  invalid and must be resolved through the parent).
- `interpenetrate` records the **realized** overlap depth feeding a boolean union; per correction
  C3 it must be 0.5–2 mm, never coincident. `penetration` is required on these edges.

Edges let a re-read answer "is the seat *on top of* the legs?" structurally before any vision call.

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
