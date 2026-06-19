---
name: rhino-scene-state
user-invocable: false
description: Maintains the externalized scene-graph world model for the Rhino skill suite — the part_id->GUID ledger plus the bbox/dimension ledger — and reconciles expected-vs-actual after every mutation. Captures each baked object's GUID, name, layer, local frame, bounding box, key dims and volume at bake time; resolves objects GUID-first with a UserString part_id fallback when a boolean consumes inputs; diffs the declared scene-graph against a cheap live document summary to flag MISSING, EXTRA, PHANTOM (untagged), DUPLICATE, MIS-SIZED parts and post-boolean count/volume mismatches, counting authoritatively by part_id rather than the unreliable document object_count. Use automatically around every geometry-producing or mutating step (create_object, loft, extrude, sweep1, revolve, shell, boolean_union/difference/intersection, bake). Enforces token economy via get_document_summary aggregates before re-querying full objects.
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

1. **Capture-before (cheap).** Call `get_document_summary` for the live tolerance/units and to
   snapshot the **set of part_ids** (and the GUID set, if you need a create-then-find-newest shim).
   Do **not** dump full objects here, and do **not** treat the document `object_count` as a count of
   *your* parts.
2. **Mutate.** Run the typed MCP geometry tool (preferred) or fall back to `execute_*` only for ops
   with no typed tool (revolve, shell, network surface — conventions §11).
3. **Capture-after (cheap first).** Call `get_document_summary` again and read `objects_by_type` /
   the per-object `part_id` (UserString) — **not** the aggregate `object_count`. The authoritative
   "did the right thing happen?" signal is the change in the set of **tagged part_ids**, never the
   raw object total: `object_count` has been observed at 54 when only 18 real BREPs existed (E2), so
   a count delta proves nothing. If `part_id` tags are not in the summary, escalate to a **paginated**
   `get_objects` at this **decision point** with `include_geometry=false` and read the UserStrings.
4. **Reconcile.** Run [`scripts/reconcile.py`](scripts/reconcile.py) with the expected scene-graph
   (derived from the IR) and the actual document summary. It **enumerates by `part_id`** (GUID-first,
   UserString fallback) and asserts on identity, never on `object_count` (which it reports for
   transparency only). It flags:
   - **MISSING** — a declared node with no matching live object (and no `child_of` edge to explain
     consumption).
   - **EXTRA** — a **tagged** live object whose `part_id` no declared node claims (or a surplus copy).
   - **PHANTOM** — an **untagged** live object (no `part_id` UserString): a leftover that inflated
     `object_count`. Repair hint: delete-untagged (E2). *This is the category that explains 54-vs-18.*
   - **DUPLICATE** — one declared `part_id` resolved to **more than one** live object: the
     double/triple-execution signature (E1).
   - **MIS-SIZED** — a matched node whose live bbox span differs from the declared dims beyond
     tolerance.
   - **COUNT / VOLUME mismatch (C2)** — a boolean/operation result whose realized solid count or
     total volume diverges from `expected_solid_count` / the summed IR volume. The realized count is
     read from the object's own `solid_count`/`piece_count`, else from the **part_id-enumeration
     count** — never from `object_count`. A *valid* Brep missing a part (e.g. a 3-legged chair that
     should have 4) passes `IsValid`/`IsSolid`; only this count+volume guard catches the
     **partial/silent boolean failure**.
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

For a **staged** build (conventions §12), reconcile **per stage** so a re-emitted stage is checked
against only its own nodes and the rest of the model never registers as EXTRA:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/reconcile.py" \
  --expected expected_scene_graph.json \
  --actual   actual_document_summary.json \
  --stage bell_chamber
```

Only the script's structured stdout report enters context; its exit code gates the loop.

---

## Scoped idempotent stages + checkpoints (conventions §12)

EMIT is **staged**, and each stage is **scoped-idempotent**: a stage script deletes only the live
objects tagged with its `stage` (via [`scripts/stage_emit.py`](scripts/stage_emit.py)), then
re-creates them once. This skill owns the ledger bookkeeping that keeps the scene-graph consistent
across stages:

1. **Stamp `stage`.** Every baked object carries `UserString "stage"` (alongside `part_id`), and
   every node records its `stage`. This is the scoped delete key for a re-emit and the `--stage`
   reconcile filter.
2. **Replace, don't append, on re-emit.** A stage re-emit purges that stage's live objects and
   re-bakes them; in the ledger, **drop that stage's old nodes and append the new GUIDs**, then bump
   `revision` and set `last_op` (e.g. `re-emit:bell_chamber`). Nodes of other stages are untouched.
3. **Checkpoint at each stage boundary.** When a stage bakes and `reconcile.py --stage <id>` passes,
   append a `checkpoints[]` entry `{stage, status:"passed", revision, part_ids}` and **Save the
   .3dm** (MCP save tool / `RhinoDoc.Save`). The checkpoint + save is the **persisted ledger** — a
   crash, rollback, or fragile-op failure (a missing `Brep.CreateOffset` overload) loses at most the
   in-flight stage, and the build resumes from the last passing checkpoint rather than from zero.

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
