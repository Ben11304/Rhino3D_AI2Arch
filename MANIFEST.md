# Manifest — Rhino Skill Suite

Every file in the tree with a one-line purpose. Paths are relative to the repo root
(`/Users/caedstudent/Working/rhino-skills`). `[py]` = stdlib-only Python 3 (passes `py_compile`);
`[json]` = parses with `json.load`.

## Top-level docs

| File | Purpose |
|------|---------|
| `README.md` | Overview, layered architecture diagram, 8-skill table, modeling cognition loop, IR contract pointer, the 8 corrections, and the phased roadmap. |
| `INSTALL.md` | How to install (symlink/copy each skill dir + `shared/` into a `skills/` folder), knowledge-skill note, target MCP server, and the required/recommended MCP extensions. |
| `MANIFEST.md` | This file — every file with a one-line purpose. |

## shared/ — single source of truth (sibling of all skill dirs)

| File | Purpose |
|------|---------|
| `shared/conventions.md` | The canonical rules every skill links to: units/tolerance (§1), GUID ledger C1 (§2), naming/layers/UserString (§3), frame discipline (§4), the 9-point codegen guard contract (§5), interpenetration C3 (§6), revolve/shell C6 (§7), vision-demotion C4 (§8), ratio-vs-absolute C5 (§9), repair budget C8 (§10), token economy (§11). |
| `shared/build-plan.schema.json` `[json]` | JSON Schema (draft 2020-12, `additionalProperties:false` throughout) for the build-plan IR shared by text-to-model, image-to-model, and rhino-scene-state. |

## rhino-modeling/ — router / orchestrator

| File | Purpose |
|------|---------|
| `rhino-modeling/SKILL.md` | Router manifest: universal preamble, master plan→build→verify→repair checklist, typed-tool-first policy, ROUTE table, delegation map + IR hand-off contract. Owns the build-plan IR artifact. |
| `rhino-modeling/reference/workflow.md` | The full cognition loop in detail: phases P0..P8 with decision points and the sibling that owns each phase; honors C1–C8. |
| `rhino-modeling/reference/token-economy.md` | Concrete token-budget rules and the four-bucket cost model; scene-graph as cache; interaction with the C8 repair budget. |
| `rhino-modeling/reference/server-capabilities.md` | How rhinomcp / grasshopper-mcp / lamcp / SerjoschDuering surfaces differ, the classification heuristics, and the v1 "pick ONE server" rule. |
| `rhino-modeling/scripts/detect_server.py` `[py]` | Classifies the connected MCP server flavor from its tool names (text or `--json`), reports the recommended surface, present/missing loop capabilities, exec-only ops, and the pick-one recommendation. |

## rhino-geometry-api/ — knowledge (user-invocable:false)

| File | Purpose |
|------|---------|
| `rhino-geometry-api/SKILL.md` | Knowledge entry point: the HARD RULE (docs before any `execute_*`/`gh_add_component`), the 9-point codegen guard summary, one-level-deep links. |
| `rhino-geometry-api/reference/geometry-ops.md` | Per-op cheat-sheets with real RhinoCommon names + C7 pre-flight + C2/C3 post-checks: loft, sweep1, revolve, extrude, boolean, fillet, offset, network surface; plus a failure-signal table. |
| `rhino-geometry-api/reference/rhinocommon.md` | Core RhinoCommon idioms: rs-vs-RhinoCommon-vs-CPython3 decision table, booleans/loft/revolve/offset, `ObjectAttributes`/`SetUserString`, `AddBrep` returning the GUID, and the measuring idioms for the verify loop. |
| `rhino-geometry-api/reference/types-and-conversions.md` | NURBS/Brep/SubD/Mesh reliable-vs-fragile matrix, conversion functions with gotchas, and how to pick the build type per IR primitive/operation. |
| `rhino-geometry-api/reference/tolerance-units.md` | Read-don't-hardcode tolerance rules, setting the unit system before geometry, `overall_height_mm` always-mm conversion, and the C3 tolerance-vs-penetration interaction. |
| `rhino-geometry-api/scripts/codegen_guard.py` `[py]` | Linter/wrapper for the 9-point codegen contract: `--lint` prints PASS/WARN/FAIL per rule (exit 1 on FAIL); `--wrap` brackets the snippet with a live-tolerance preamble + single-redraw postamble. |

## text-to-model/ — text producer

| File | Purpose |
|------|---------|
| `text-to-model/SKILL.md` | Text-pipeline entry point: 8-step workflow (units → clarify ≤3 dims → emit IR → validate → parameterize in local frames → typed-tool emit + interpenetrating booleans → reconcile+measure → render-and-look ≤3). Verifies absolutes. |
| `text-to-model/reference/part-recipes.md` | Four concrete parametric recipes (assembly-of-primitives, revolve, loft, extrude) as IR fragments + the typed MCP tool sequence, with real RhinoCommon names. |
| `text-to-model/examples/chair.json` `[json]` | Complete valid build-plan IR: four-legged chair (seat + 4 interpenetrating legs + back) unioned into one solid; symmetry, params, and a verify block; validates clean. |
| `text-to-model/scripts/validate_plan.py` `[py]` | Stdlib-only build-plan IR validator: required fields, units enum, per-number unit+frame, primitive XOR operation, C3 penetration on union joins, C6 profile-on-axis, scale well-formedness, C5 confidence routing, bbox sanity. Exits non-zero on failure. |

## image-to-model/ — image producer

| File | Purpose |
|------|---------|
| `image-to-model/SKILL.md` | Image-pipeline entry point: 10-step pipeline (identify+archetype → views & symmetry → detect symmetry-breakers → factorize discrete → extract profile → ground scale → emit IR → guarded geometry → silhouette compare → iterate ≤3 on RATIOS per C5). |
| `image-to-model/reference/vision-analysis.md` | Vision-pass reference: object-class→archetype, view classification, symmetry detection + completion, detecting symmetry-breaking features before factorizing, and the C4 vision-vs-math routing table. |
| `image-to-model/reference/scale-grounding.md` | Scale-grounding priority (stated → reference_object → metrology_assumption), the range+confidence rule, provenance recording, and the C5 ratio-only verify consequence. |
| `image-to-model/scripts/extract_profile.py` `[py]` | Projects silhouette points to (along-axis, perpendicular), averages left+right half-widths to cancel skew (C5), resamples to ordered 3D control points; flags low confidence and emits 5 fallback archetype profiles closed to the axis (C6). |
| `image-to-model/examples/vase.json` `[json]` | Complete build-plan IR: hollow revolved vase — interpolated generatrix on-axis (C6), revolve+cap then shell (C6), `metrology_assumption` scale as a `[250,350]` range/low confidence (C5), rotational symmetry, ratio-only verify. |

## grasshopper-parametric/ — Grasshopper phase

| File | Purpose |
|------|---------|
| `grasshopper-parametric/SKILL.md` | GH entry point: the v2 workflow (introspect-then-validate-then-wire, batched build/mutate, run-then-read-warnings, C2/C3/C6 inside GH, bounded repair) AND the v1 re-runnable-IR alternative. |
| `grasshopper-parametric/reference/gh-wiring.md` | Port-semantics knowledge base: source→target compatibility matrix, point-vs-plane silent error, curve/surface/brep promotion, slider taxonomy + ordering rule, adapter table, warning→fix mappings. |
| `grasshopper-parametric/reference/gh-patterns.md` | Ready-to-build definition templates as component graphs (revolve-a-profile, array-on-curve, parametric-louver) with slider tables, wiring, verify steps, and a `validate_connection` watch-list. |
| `grasshopper-parametric/reference/validate_graph_plan.py` `[py]` | Pre-flight validator for a graph-plan JSON: every connection references a declared component+port, sliders have real `min<max<value` (rejects default 0..1), slider order recorded/contiguous, no duplicate ids. Exit 0/1/2. |

## rhino-scene-state/ — knowledge (user-invocable:false), GUID ledger

| File | Purpose |
|------|---------|
| `rhino-scene-state/SKILL.md` | Knowledge entry point: the GUID-ledger protocol (C1 capture-at-bake, three-way identity, GUID-first/UserString-fallback/Name-last resolution, boolean-consumes-inputs `child_of`), the capture-before/after reconcile loop (C2/C3), and token-economy enforcement. |
| `rhino-scene-state/reference/scene-graph.md` | Artifact-format reference: top-level shape, nodes as the combined `part_id→GUID` + bbox/dims ledger, realized-relation edges, re-read-each-step discipline, and the strict resolve() order with a runnable snippet. |
| `rhino-scene-state/schema/scene-graph.schema.json` `[json]` | JSON Schema (draft 2020-12) for the scene-graph artifact: requires object/units/tolerance/nodes; node requires bbox + anyOf guid|part_id; edge requires type/from/to with an interpenetrate→penetration conditional; `additionalProperties:false`. |
| `rhino-scene-state/scripts/reconcile.py` `[py]` | Diffs expected scene-graph vs actual document summary; matches GUID-first then part_id then Name; flags MISSING (honoring `child_of` consumption), EXTRA, MIS-SIZED, and post-boolean COUNT+VOLUME mismatch (C2). Exits non-zero on any mismatch. |

## render-and-look/ — perception / verify

| File | Purpose |
|------|---------|
| `render-and-look/SKILL.md` | Perception/verify manifest: set 4 named views, color each part before low-res capture (C4), author 2–5 Yes/No/Unclear questions from the IR, CADCodeVerify discipline (only No/Unclear repair), the demotion rule, orthographic-only silhouette compare (C5). |
| `render-and-look/reference/verification.md` | The working recipe: numeric-vs-vision split table, binary-question generation from the IR, color-per-part capture protocol, the orthographic silhouette-compare procedure (ratios per C5), and the differential-repair-list output contract. |
| `render-and-look/scripts/set_named_views.py` `[py]` | Emits a JSON blob describing 4 deterministic named cameras (front/top/right ortho + iso parallel) plus a ready-to-run RhinoCommon program string to set them so captures are repeatable. |

## rhino-repair/ — bounded repair

| File | Purpose |
|------|---------|
| `rhino-repair/SKILL.md` | Repair entry point: strict triage (Tier 1 syntactic/runtime, Tier 2 numeric, Tier 3 visual structural-only), the C8 budget (per-item N=3 + global wall 12) with a per-(item,part) convergence ledger, and the output contract. |
| `rhino-repair/reference/failure-playbook.md` | Operation-specific symptom→cause→fix with real RhinoCommon code: loft seam/twist, sweep rail G1, revolve axis/profile/cap (C6), boolean coplanar/non-solid/partial-union (C2/C3), fillet radius clamp, offset self-intersection, network-surface grid. |
| `rhino-repair/reference/lamcp-dotnet-traps.md` | The IronPython/.NET traps for `execute_rhinocommon_csharp_code`/lamcp: `GetType().Name` vs `isinstance`, `System.Guid` overload discipline (C1), `System.Convert.ToDouble`, and the non-UI-thread crash hazard — all framed as Tier-1 fixes. |
| `rhino-repair/scripts/repair_budget.py` `[py]` | RepairLedger implementing the C8 per-item cap + independent global wall + per-(item,part) state (open/in_progress/pass/could_not_fix/conflict); Tier-1 syntax re-emits spend the wall but not per-item budget; surfaces unresolved items. |
