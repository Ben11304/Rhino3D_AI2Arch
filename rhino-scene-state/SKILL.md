---
name: rhino-scene-state
user-invocable: false
description: Maintains the externalized scene-graph world model for the Rhino skill suite — the part_id->GUID ledger plus the bbox/dimension ledger — and reconciles expected-vs-actual after every mutation. Captures each baked object's GUID, name, layer, local frame, bounding box, key dims and volume at bake time; resolves objects GUID-first with a UserString part_id fallback when a boolean consumes inputs; diffs the declared scene-graph against a cheap live document summary to flag MISSING, EXTRA, MIS-SIZED parts and post-boolean count/volume mismatches. Use automatically around every geometry-producing or mutating step (create_object, loft, extrude, sweep1, revolve, shell, boolean_union/difference/intersection, bake). Enforces token economy via get_document_summary aggregates before re-querying full objects.
allowed-tools: Bash(python3 *)
---

# rhino-scene-state

Knowledge + bookkeeping skill that owns the **scene-graph artifact**: the suite's externalized
world model of what actually exists in the live Rhino document. It is the realized counterpart to
the build-plan IR — the IR declares *intent*, the scene-graph records the *baked truth*. This skill
is invoked automatically by the modeling pipeline around every mutating step; it is not directly
user-invocable.

All rules here defer to the single source of truth:
[`../shared/conventions.md`](../shared/conventions.md). This skill operationalizes conventions §2
(GUID ledger / C1), §3 (naming/layers/UserString), §5 step 7 + §6 (post-boolean count+volume / C2,
C3), and §11 (token economy). Do **not** duplicate those rules — link to them.

- Artifact format: [`reference/scene-graph.md`](reference/scene-graph.md)
- Artifact schema: [`schema/scene-graph.schema.json`](schema/scene-graph.schema.json)
- Reconcile script: [`scripts/reconcile.py`](scripts/reconcile.py)

---

## When this skill runs

Automatically, as a wrapper around **every geometry-producing or mutating step**: `create_object`,
`loft`, `extrude_curve`, `sweep1`, `revolve`, `shell`, `offset_curve`, `pipe`,
`boolean_union` / `boolean_difference` / `boolean_intersection`, and any raw
`execute_rhinoscript_python_code` / `execute_rhinocommon_csharp_code` that bakes or edits objects.
Each such step is bracketed by a **capture-before** and a **capture-after + reconcile**.

---

## The GUID LEDGER protocol (correction C1)

Objects are referenceable **only by GUID**; `part_id` is our label, not a Rhino handle. See
conventions §2 for the canonical `add_and_register` / `find_newest_guid` / `resolve` helpers.

1. **Capture the GUID at bake.** Every mutator must return the `System.Guid` that `AddBrep`
   (or `AddCurve` / `AddSurface`) produced. Typed MCP tools that do **not** return a GUID must be
   wrapped in a *create-then-find-newest* shim: snapshot `{o.Id for o in sc.doc.Objects}` before the
   call, diff after, and assert exactly one new object (conventions §2 `find_newest_guid`).
2. **Stamp identity three ways at bake** (conventions §3): `attr.Name = part_id`,
   `attr.SetUserString("part_id", part_id)`, and the target layer index. The `UserString "part_id"`
   is the **fallback resolver** that survives a lost GUID.
3. **Write the node.** Record `part_id`, `guid`, `name`, `layer`, `primitive`, `frame`, `bbox`
   (`geom.GetBoundingBox(True)`), `dims`, `volume` (`VolumeMassProperties.Compute`, solids only),
   `is_solid`, and `expected_solid_count` for operation results. Bump `revision`, set `last_op`.
4. **Resolve GUID-first, UserString-fallback, Name-last.** When a later step needs an object for a
   `part_id`, resolve in the strict order documented in
   [`reference/scene-graph.md`](reference/scene-graph.md) §6: `sc.doc.Objects.FindId(guid)` →
   scan `UserString "part_id"` and **backfill** the node's GUID → match `Name` (low confidence,
   re-tag before trusting).
5. **Booleans consume inputs.** A boolean invalidates the GUIDs of its inputs and creates a new
   object. Record a `child_of` edge from each consumed input to the result, give the result its own
   GUID/node, and treat the consumed inputs as MISSING-by-consumption (explained by the `child_of`
   edge) rather than as errors.

---

## The reconcile loop (corrections C2/C3)

Every mutation is bracketed by a cheap aggregate capture before and after, then a structured diff.

1. **Capture-before (cheap).** Call `get_document_summary` for the aggregate object count and the
   live tolerance/units. Do **not** dump full objects here. Snapshot the GUID set if you need a
   create-then-find-newest shim.
2. **Mutate.** Run the typed MCP geometry tool (preferred) or fall back to `execute_*` only for ops
   with no typed tool (revolve, shell, network surface — conventions §11).
3. **Capture-after (cheap first).** Call `get_document_summary` again. Compare the aggregate count
   delta to what the operation should have produced (e.g. a union of 5 solids into 1 should drop the
   count by 4). Only if the cheap delta is ambiguous, escalate to a **paginated**
   `get_objects` at this **decision point** with `include_geometry=false` (bbox + volume aggregates
   are enough), using `offset`/`limit`.
4. **Reconcile.** Run [`scripts/reconcile.py`](scripts/reconcile.py) with the expected scene-graph
   (derived from the IR) and the actual document summary. It diffs by GUID (falling back to
   `part_id`/UserString match) and flags:
   - **MISSING** — a declared node with no matching live object (and no `child_of` edge to explain
     consumption).
   - **EXTRA** — a live object with no declared node.
   - **MIS-SIZED** — a matched node whose live bbox span differs from the declared dims beyond
     tolerance.
   - **COUNT / VOLUME mismatch (C2)** — a boolean/operation result whose realized solid count or
     total volume diverges from `expected_solid_count` / the summed IR volume. A *valid* Brep
     missing a part (e.g. a 3-legged chair that should have 4) passes `IsValid`/`IsSolid`; only this
     count+volume guard catches the **partial/silent boolean failure**.
   The script exits **non-zero on any mismatch**, which gates the pipeline and hands off to
   `rhino-repair`.
5. **Interpenetration audit (C3).** For every `interpenetrate` edge feeding a union, confirm the
   realized overlap is 0.5–2 mm before the union ran; a coincident/coplanar contact is degenerate
   and is surfaced as a pre-flight failure.

Run the reconcile script as:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/reconcile.py" \
  --expected expected_scene_graph.json \
  --actual   actual_document_summary.json \
  --tol 0.01
```

Only the script's structured stdout report enters context; its exit code gates the loop.

---

## Token-economy enforcement (conventions §11)

This skill is the suite's gatekeeper against wasteful document queries:

- **The scene-graph is the cheap in-context world state — read it instead of re-querying.** Never
  call `get_objects` to learn something the artifact already records; never re-query unchanged
  geometry.
- **Prefer `get_document_summary`** (aggregate count/units/tolerance) over a full `get_objects`
  dump. Reach for `get_objects` only at **decision points**, and then **paginate**
  (`offset`/`limit`) with `include_geometry=false`.
- **Render only at decision points**, never every step (defer vision to `render-and-look`).
- Scripts run via `${CLAUDE_SKILL_DIR}` and **only their stdout enters context** — print the
  structured report and the affected GUIDs, not debug noise.
