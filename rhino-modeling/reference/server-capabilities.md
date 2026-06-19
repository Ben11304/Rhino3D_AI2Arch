# rhino-modeling — server capabilities & the "pick ONE" rule

Several MCP servers expose Rhino/Grasshopper to an LLM. Their tool **surfaces differ**, so the
orchestrator must detect which flavor is connected before routing, and then commit to a single
execution surface for the whole job.

Detect the flavor with `../scripts/detect_server.py` (feed it the connected MCP tool names). It
prints the recommended execution surface and any missing capabilities. The classification rules
below are the same ones that script encodes — keep them in sync.

---

## The four flavors

### 1. `rhinomcp` (jingcheng-chen/rhinomcp) — the reference surface

The richest, most typed Rhino surface and the one the whole skill suite is written against.

- **Create / edit:** `create_object`, `loft`, `extrude_curve`, `sweep1`,
  `boolean_union`, `boolean_difference`, `boolean_intersection`, `offset_curve`, `pipe`,
  `run_command`.
- **Escape hatches:** `execute_rhinoscript_python_code`, `execute_rhinocommon_csharp_code` —
  use **only** for revolve / shell / network surface (no typed tool).
- **Grasshopper:** `gh_add_component`, `gh_build_graph`, `gh_connect_components`,
  `gh_mutate_graph`, `gh_run_solution`, `gh_get_canvas_state`, `gh_get_component_type_info`.
- **Sensors:** `get_document_summary`, `get_objects`, `get_object_info`, `analyze_objects`,
  `capture_viewport`.
- **Docs:** `search_rhinoscript_functions`, `get_rhinoscript_docs`, `validate_connection`.

**Recommended surface:** typed tools for everything except revolve/shell/network surface; full
plan->build->verify->repair loop is available. This is the assumed baseline of every sibling skill.

### 2. `grasshopper-mcp` — canvas-first

Focused on authoring Grasshopper **definitions**; thin on direct Rhino-document baking.

- **Strong:** `gh_add_component`, `gh_connect_components`, `gh_build_graph`, `gh_mutate_graph`,
  `gh_run_solution`, `gh_get_canvas_state`, `gh_get_component_type_info`.
- **Weak / missing:** direct typed Rhino solid ops (`loft`/`sweep1`/`boolean_*` as document
  operations), and sometimes `analyze_objects` / `capture_viewport`.

**Recommended surface:** route **grasshopper-parametric** here. Baked-geometry verification may need
a `gh_run_solution` + bake step before `analyze_objects` is meaningful. If `capture_viewport` is
absent, verification is math-only (no vision) — flag that to the user.

### 3. `lamcp` — minimal / scripting-centric

A lean server that mostly exposes a code-execution escape hatch plus basic sensors.

- **Present:** `execute_rhinoscript_python_code` (and/or a generic run/exec tool), often
  `get_document_summary` or a `get_objects` equivalent.
- **Missing:** most typed create tools, the `gh_*` family, and frequently `analyze_objects` /
  `capture_viewport`.

**Recommended surface:** `execute_rhinoscript_python_code` for **all** geometry, honoring the
codegen guard contract (conventions §5) by hand since there are no typed shortcuts. Without
`analyze_objects` you must compute counts/volumes/bboxes inside the executed script and **print**
them; without `capture_viewport` there is no vision — verification falls back to math-only and
profile-fidelity checks are unavailable. Surface both gaps.

### 4. `SerjoschDuering` (SerjoschDuering/rhino-mcp variant) — research/hybrid

A community variant whose tool names diverge from rhinomcp (different verbs, partial coverage,
sometimes a Grasshopper bridge). Coverage varies by build.

- **Likely present:** a create/exec surface and some sensors under non-standard names.
- **Likely divergent:** tool names won't match the rhinomcp typed set 1:1; `gh_*` coverage and
  `analyze_objects` / `capture_viewport` are version-dependent.

**Recommended surface:** treat as **rhinomcp-like but unverified**. Probe with
`get_document_summary` (or its closest equivalent) first, map the available verbs onto the loop, and
fall back to a code-execution tool for anything missing. Report every capability the loop needs but
cannot find.

---

## Classification heuristics (encoded in `detect_server.py`)

The detector scores the connected tool-name set against each flavor's signature:

- `gh_*` family present **and** typed Rhino solid ops present -> **rhinomcp** (high confidence).
- `gh_*` family present but typed Rhino solid ops absent -> **grasshopper-mcp**.
- Typed Rhino solid ops present, **no** `gh_*`, but **>= 3 rhinomcp marker sensors/docs tools**
  (`get_document_summary`, `analyze_objects`, `capture_viewport`, `search_rhinoscript_functions`,
  `get_rhinoscript_docs`, `validate_connection`) -> **rhinomcp** (medium confidence): the canonical
  typed surface with the Grasshopper bridge simply not connected this session. The full loop still
  runs; only parametric/definition requests are blocked until the GH bridge attaches.
- A code-execution tool present but no `gh_*` and no typed solid ops -> **lamcp**.
- Names that don't match the rhinomcp typed set (fewer than 3 markers) yet still expose
  create+sensors -> **SerjoschDuering** (unverified).

It then reports, against the loop's needs, which of these are **missing**: a typed create surface,
the `gh_*` family, `analyze_objects` (math verification), and `capture_viewport` (vision).

---

## v1 recommendation: pick ONE server

For v1, **commit to a single server for the entire job** — ideally **`rhinomcp`**, which is the
surface every sibling skill is authored against and the only one offering the full
plan->build->verify->repair loop with both math (`analyze_objects`) and vision (`capture_viewport`).

Reasons not to straddle two servers mid-build:

- **GUID ledger integrity (C1):** the scene-graph maps `part_id -> GUID` in one document/session.
  Two servers can mean two documents or two object tables, and the ledger silently desyncs.
- **Verification parity:** a model built with typed tools on one server but verified through a
  different server's sensors invites tool-name and unit mismatches.
- **Token cost:** re-establishing state across servers means re-querying the document — exactly the
  thing the token-economy rules forbid (see [token-economy.md](token-economy.md)).

If the connected server is **not** rhinomcp, run the loop on whatever single flavor is live, accept
the reduced surface (e.g. math-only verification on lamcp, canvas-first on grasshopper-mcp), and
**surface the missing capabilities** to the user up front rather than degrading silently.
