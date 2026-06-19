# CLAUDE.md — working on this repository

Guidance for Claude Code (and any agent) **editing or extending this repo**. This is about
*maintaining the skill suite*, not about using it at runtime — for runtime/usage see `README.md`,
and for the binding rules every skill obeys see `shared/conventions.md` (the single source of truth).

## What this repo is

`Rhino3D_AI2Arch` is a suite of **Claude Code Agent Skills** that sit on top of a Rhino MCP server so
an LLM can build editable 3D from a text description or a reference image. The MCP server is the
*hands + eyes*; these skills are the *brain + method* (externalized state, API discipline,
decompose‑to‑validated‑IR, render + measure + repair loop). There is **no application to run** here —
the deliverable is the skills, their reference docs, their JSON contracts, and stdlib Python helpers.

## Repository map

```
shared/                  conventions.md (SINGLE SOURCE OF TRUTH) + build-plan.schema.json (the IR)
rhino-modeling/          router/orchestrator — owns the build-plan IR + the plan→build→verify→repair loop
rhino-geometry-api/      knowledge skill (user-invocable:false) — RhinoCommon recipes + codegen_guard.py
text-to-model/           text → IR producer + validate_plan.py + examples/chair.json
image-to-model/          image → IR producer + extract_profile.py + examples/vase.json
grasshopper-parametric/  Grasshopper definitions + validate_graph_plan.py
rhino-scene-state/       knowledge skill (user-invocable:false) — GUID ledger + reconcile.py + scene-graph.schema.json
render-and-look/         perception/verify — set_named_views.py
rhino-repair/            bounded repair loop — repair_budget.py
```

Each skill folder is `SKILL.md` + `reference/*.md` (deep knowledge, loaded on demand) +
`scripts/*.py` (deterministic helpers, run via `${CLAUDE_SKILL_DIR}`) + sometimes `examples/` or
`schema/`. `MANIFEST.md` lists every file with a one‑line purpose.

## Hard invariants — do not break these when editing

1. **The 8 corrections (C1–C8) are load‑bearing.** They are defined once in `shared/conventions.md`
   and referenced by the skills that own them. Never weaken or duplicate them; if you touch a skill,
   confirm its corrections still hold (see the table in `README.md`). Scope boundaries are
   intentional — e.g. `rhino-scene-state` has no C6/C7 because it is the ledger, not a builder; that
   is correct, not a gap.
2. **Artifact ownership is exclusive.** The *build‑plan IR* is owned by `rhino-modeling` (producers
   fill `object`..`verify`); the *scene‑graph* is written **only** by `rhino-scene-state`. GUIDs live
   in the scene‑graph and are **never** written back into the IR. No two skills write the same field.
3. **Conventions are linked, never copied.** Reference files link to `../shared/conventions.md`
   (or `../../shared/conventions.md` from inside `reference/`). Do not paste convention text into a
   skill — fix it in one place.
4. **References stay exactly one level deep.** `SKILL.md → reference/x.md` is fine; `reference/x.md →
   reference/y.md → reference/z.md` is not. Keep the progressive‑disclosure tree shallow so the model
   never does partial reads and misses content.
5. **SKILL.md frontmatter** must have `name` (matching the directory) and a third‑person,
   keyword‑rich `description` distinct from siblings. Knowledge skills keep `user-invocable: false`
   (`rhino-geometry-api`, `rhino-scene-state`). `allowed-tools` for skills that run helpers includes
   `Bash(python3 *)`.
6. **No placeholders, TODOs, or `...`.** Use real RhinoCommon / rhinoscriptsyntax names and real MCP
   tool names (`create_object`, `loft`, `boolean_union`, `execute_rhinoscript_python_code`,
   `gh_build_graph`, `get_document_summary`, `analyze_objects`, `capture_viewport`, …). The only
   anti‑example names allowed are explicitly labeled as such in `rhino-geometry-api/SKILL.md`.

## Environment & how to validate changes

- **Local Python is 3.9.6** — helper scripts must avoid 3.10+ syntax (no `match`, no `X | Y` type
  unions, no `str.removeprefix` if you target older). They must be **stdlib‑only** (no `numpy`,
  `cv2`, `jsonschema`, `pyyaml` in shipped code; those may be used only for *validating* during dev).
- The Rhino/RhinoCommon code the scripts *emit* cannot run here (no live Rhino/MCP in this
  environment). Verify scripts as **valid Python that runs and exits with the right code**; their
  in‑Rhino behavior is unverified by definition — say so honestly.

Run these before committing any change:

```bash
# 1. Every Python helper must compile
find . -name '*.py' -not -path '*/__pycache__/*' -print0 | xargs -0 -n1 python3 -m py_compile

# 2. Every JSON must parse
for f in $(find . -name '*.json'); do python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f"; done

# 3. The worked examples must validate against the IR (exit 0)
python3 text-to-model/scripts/validate_plan.py text-to-model/examples/chair.json
python3 text-to-model/scripts/validate_plan.py image-to-model/examples/vase.json   # cross-check ratio pipeline

# 4. Cross-references: confirm every relative .md/.py/.json link in a SKILL.md/reference exists on disk
```

If you change `shared/build-plan.schema.json`, re‑validate **both** example IRs and any validator
that hard‑codes field names (`validate_plan.py`, `reconcile.py`, `validate_graph_plan.py`). If you
change `detect_server.py`'s classification, keep `rhino-modeling/reference/server-capabilities.md` in
sync (the doc states they must match).

## Conventions for adding a new skill

- Mirror the existing folder shape: `SKILL.md` + `reference/` + `scripts/`. Give it **one** phase of
  the cognition loop; do not overlap a sibling's phase or artifact.
- Add it to the architecture diagram, the 8‑skills table, and the repo layout in `README.md`, and add
  its files to `MANIFEST.md`.
- If it introduces a new invariant, define it in `shared/conventions.md` first, then reference it.

## v2 invariants — connectivity, direction-pin, staging (honor these)

v2 added correction **C9** and supporting machinery on top of v1. When editing, preserve:

- **C9 connectivity (conventions §13).** Every declared contact relation (`lands_on`, `on_top_of`, `meets`, `spans_between`, `coincident`, `interpenetrate`) is a *measured numeric obligation*. The realized gap is measured **GUID-to-GUID between two live solids** in-Rhino (`Brep.ClosestPoint`, carrying `measured_between=[guidA,guidB]`) — **never** against an IR-derived coordinate (that just re-confirms the bug). A declared contact with no measurement is `UNCOVERED` = FAIL. `floating:true` parts are exempt. Owner: `rhino-scene-state` (`check_connectivity.py` classifies; `stage_emit.py --connectivity-edges` measures; `reconcile.py` maintains cross-stage invalidation). The connectivity *verdict* is written by `rhino-scene-state` only; render-and-look's connectivity question is advisory and can never fail the build (preserves C4 vision-demotion).
- **Per-relation-type tolerance band** (directed, never one symmetric ±tol): `on_top_of` [0,+tol]; `coincident` [-tol,+tol]; `lands_on`/`meets`/`spans*` [-penetration,+tol]; `interpenetrate` [-2,-0.5] mm.
- **Relational-IR (PREVENT).** `validate_plan.py --resolve` expands `value_ref`/`support`/`array` into a literal IR so attach points are computed, not hand-typed constants. The resolver is a single topo-sorted forward pass — no eval, no general solver; a cycle is an error.
- **Direction-pin (codegen rule 10, §5a).** Any directional op (`Extrusion.Create`/`RevSurface.Create`/…) must read `GetBoundingBox(True)` and pin the result to the IR-intended Z (`Transform.Translation`). Curve normal direction is not trustworthy (the real seat-extruded-to-410-not-450 bug).
- **Capability probe + visible degradation.** `detect_server.py --rhinocommon-probe` checks fragile methods (`Brep.CreateOffset` etc.); a missing method falls back with `shell_degraded:true`, never a silent rollback.
- **Persistence (§12).** Checkpoint the `.3dm` (via `execute_*` `sc.doc.WriteFile`, confirm `True`) *before* writing the sidecar JSON ledger; resume re-binds `part_id→GUID` from the reopened `UserString` (saved GUID is a hint only).

Definition of Done: the framework may only say ✅ when every non-floating declared contact has been numerically proven in-band — proven, not assumed.

## Commit / push etiquette

- Keep helpers stdlib‑only and 3.9‑compatible; run the four checks above before committing.
- Default branch is `main`. Remote: `https://github.com/Ben11304/Rhino3D_AI2Arch.git`.
