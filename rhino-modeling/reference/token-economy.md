# rhino-modeling — token economy & the cost model

Concrete, enforceable rules for keeping the orchestration loop cheap. The canonical statement lives
in [../../shared/conventions.md](../../shared/conventions.md) §11; this file is the operational
playbook with the **cost model** that explains *why* each rule pays off.

---

## The cost model

Every loop iteration spends tokens in four buckets. The orchestrator's job is to keep each bucket as
small as the task allows without losing the externalized world state.

| Bucket | What costs tokens | The lever |
|---|---|---|
| **Query** | reading document/scene state back into context | summary-not-dump, paginate, cache in scene-graph |
| **Vision** | image bytes from `capture_viewport` | low-res, render only at decision points |
| **Codegen** | the geometry Python you emit + its stdout | typed tools first, print only the GUID |
| **Repair churn** | re-querying/re-rendering the same geometry | re-measure only affected parts |

The dominant cost is almost always **Query** (whole-document dumps) and **Vision** (high-res frames
captured every step). Both are avoidable with no loss of correctness, because the **real** world
state is externalized in the IR and the scene-graph — which are far cheaper to re-read than the live
document.

Rule of thumb: a `get_document_summary` is O(number of objects) of cheap metadata; a full
`get_objects` with `include_geometry=true` is O(number of objects × control points) of geometry
payload. For a 30-part model the difference is one to three orders of magnitude in tokens.

---

## Rule 1 — `get_document_summary` over `get_objects` dumps

- Use **`get_document_summary`** to learn counts, layers, bounding info, and the object-id set.
- Reach for `get_objects` / `get_object_info` **only** when you need a specific object's detail, and
  then **target it by GUID or `part_id`**, never "give me everything".
- The Phase-0 pre-mutation snapshot is a summary, not a dump. The create-then-find-newest GUID diff
  only needs the **id set**, which the summary provides.

## Rule 2 — paginate, and `include_geometry=false`

- For any list query, pass `offset` / `limit` and page through; do not pull the whole table at once.
- Default to **`include_geometry=false`**. Control-point payloads are the single largest token sink.
  Set it true only for the one object whose geometry you actually need to inspect.
- Combine with a `part_id` / layer filter so you fetch the smallest relevant slice.

## Rule 3 — low-res capture, render only at decision points

- `capture_viewport` at **low resolution** for vision checks. Vision answers qualitative questions
  (profile fidelity, relative position) that do not need pixels.
- **Render only at decision points** (after a build phase completes, after a repair) — never after
  every primitive. A 12-part model needs a handful of captures, not 12.
- **Color parts before capture** (conventions §3/§8, C4) so a single low-res frame answers "is the
  red seat above the blue legs?" reliably — no high-res zoom needed.
- Everything **measurable** (count, dimension, position, symmetry) goes to `analyze_objects` / bbox
  math, which is text-cheap, **not** to vision. This is the vision-demotion rule (C4) and it is also
  the biggest vision-token saver.

## Rule 4 — never re-query unchanged geometry

- The scene-graph artifact (`part_id -> GUID`, owned by rhino-scene-state) **is** your cache. Read
  state from it instead of re-hitting the document.
- After a repair, re-measure / re-render **only the affected parts** (look up just their GUIDs).
  Untouched parts keep their last-known verdict.
- Do not re-validate a Brep you have not mutated since its last check.

## Rule 5 — prefer typed tools; print only the result

- Typed MCP tools (`create_object`, `loft`, `extrude_curve`, `sweep1`,
  `boolean_union/difference/intersection`, `offset_curve`, `pipe`, `gh_*`) cost far fewer tokens
  than hand-written `execute_rhinoscript_python_code`, and they fail more legibly. Fall back to
  `execute_*` **only** for revolve / shell / network surface.
- Scripts are executed via `${CLAUDE_SKILL_DIR}` and **only their stdout enters context**. Print the
  **one** value you need (typically a GUID), not debug noise. A chatty script is a token leak.

---

## Anti-patterns (each one burns tokens for no correctness gain)

- Dumping `get_objects` with `include_geometry=true` "to be safe" — instead snapshot a summary.
- Capturing a high-res viewport after every single primitive — instead render at decision points.
- Asking vision "is this 450 mm?" — instead measure with `analyze_objects` (vision can't, and the
  bytes are wasted).
- Re-reading the whole document after a one-part repair — instead read that part's GUID from the
  scene-graph.
- Re-emitting an unchanged part's geometry to "refresh" it — instead trust the ledger.
- Writing Python to do a `boolean_union` a typed tool already exposes — instead call the typed tool.

---

## Budget interaction with repair (C8)

Token economy and the repair budget reinforce each other: bounding repair to **N=3 per item** and a
**global wall of 12** caps the worst-case re-query/re-render churn. Combined with "re-measure only
affected parts" (Rule 4), a stuck defect cannot run the context dry — it is surfaced as "could not
fix" and the loop moves on (conventions §10).
