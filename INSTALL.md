# Installing the Rhino Skill Suite

These are Claude Code **Agent Skills**. Each top-level directory that contains a `SKILL.md` is one
installable skill. Claude discovers a skill by its `SKILL.md` frontmatter (`name` + `description`)
and loads its `reference/`, `scripts/`, `schema/`, and `examples/` on demand.

---

## 1. Where skills live

Claude Code loads skills from either location:

- **Personal (all projects):** `~/.claude/skills/<skill-name>/`
- **Project-scoped (this repo only):** `<project>/.claude/skills/<skill-name>/`

A skill directory must contain a `SKILL.md` at its root; its referenced files sit one level deep
under it (`reference/`, `scripts/`, `schema/`, `examples/`).

> **Important — the `shared/` directory.** Every skill's `SKILL.md` links to the single source of
> truth at `../shared/conventions.md` and `../shared/build-plan.schema.json` (and nested reference
> files use `../../shared/...`). Those relative links resolve **one directory above the skill**, so
> `shared/` must be installed **as a sibling of the skill directories** — i.e. directly inside the
> same `skills/` folder. Install the whole tree, not individual skills in isolation.

## 2. Install all skills (recommended: symlink the tree)

The 9 install units are the 8 skills **plus** `shared/`:

```
rhino-modeling  rhino-geometry-api  text-to-model  image-to-model
grasshopper-parametric  rhino-scene-state  render-and-look  rhino-repair  shared
```

### Option A — symlink each unit (keeps a single editable source of truth)

```bash
SRC="/Users/caedstudent/Working/rhino-skills"
DEST="$HOME/.claude/skills"          # or: <project>/.claude/skills
mkdir -p "$DEST"
for d in rhino-modeling rhino-geometry-api text-to-model image-to-model \
         grasshopper-parametric rhino-scene-state render-and-look rhino-repair shared; do
  ln -sfn "$SRC/$d" "$DEST/$d"
done
```

### Option B — copy each unit (frozen snapshot)

```bash
SRC="/Users/caedstudent/Working/rhino-skills"
DEST="$HOME/.claude/skills"
mkdir -p "$DEST"
for d in rhino-modeling rhino-geometry-api text-to-model image-to-model \
         grasshopper-parametric rhino-scene-state render-and-look rhino-repair shared; do
  cp -R "$SRC/$d" "$DEST/$d"
done
```

After installing, restart / reload Claude Code so it re-scans the skills directory.

## 3. Verify the install

```bash
DEST="$HOME/.claude/skills"
# every SKILL.md present?
for d in rhino-modeling rhino-geometry-api text-to-model image-to-model \
         grasshopper-parametric rhino-scene-state render-and-look rhino-repair; do
  test -f "$DEST/$d/SKILL.md" && echo "ok  $d" || echo "MISSING $d"
done
test -f "$DEST/shared/conventions.md" && echo "ok  shared" || echo "MISSING shared"

# scripts compile, examples validate
python3 -m py_compile "$DEST"/*/scripts/*.py "$DEST"/*/reference/*.py
python3 "$DEST/text-to-model/scripts/validate_plan.py" "$DEST/text-to-model/examples/chair.json"
python3 "$DEST/text-to-model/scripts/validate_plan.py" "$DEST/image-to-model/examples/vase.json"
```

Both example IRs must validate (exit 0). All scripts are **stdlib-only Python 3** — no `pip install`
is required to run them. They are executed by skills via `${CLAUDE_SKILL_DIR}` and only their stdout
enters the model's context.

## 4. User-invocable vs. knowledge skills

Two skills are **knowledge skills** with `user-invocable: false` in their frontmatter:

- `rhino-geometry-api` — RhinoCommon / rhinoscriptsyntax reference and the codegen guard contract.
- `rhino-scene-state` — the GUID-ledger / scene-graph bookkeeping and reconcile loop.

They are **not** triggered directly by a user request; they are pulled in by sibling skills during
the loop. The user-facing entry points are `rhino-modeling` (router) and the producer/phase skills
(`text-to-model`, `image-to-model`, `grasshopper-parametric`, `render-and-look`, `rhino-repair`).

---

## 5. MCP server target

The suite is authored against **`rhinomcp` (jingcheng-chen/rhinomcp)** — the richest typed Rhino
surface and the only one offering the full plan→build→verify→repair loop with both math
(`analyze_objects`) and vision (`capture_viewport`). Install and connect that MCP server in Rhino,
then point Claude Code at it.

`rhino-modeling/scripts/detect_server.py` classifies whichever server is actually connected
(`rhinomcp` / `grasshopper-mcp` / `lamcp` / `SerjoschDuering`) from its tool names and reports the
recommended execution surface and any missing capabilities. Run it at ROUTE time:

```bash
python3 "$DEST/rhino-modeling/scripts/detect_server.py" \
  '["create_object","loft","boolean_union","execute_rhinoscript_python_code","get_document_summary","analyze_objects","capture_viewport"]'
```

**v1 rule: pick ONE server for the whole job (prefer `rhinomcp`).** Do not straddle two servers
mid-build — the `part_id → GUID` ledger (C1) desyncs across documents/object tables, verification
loses parity, and re-querying state across servers violates the token-economy rules.

### Tool surface the suite uses

- **Mutators (hands):** `create_object`, `loft`, `extrude_curve`, `sweep1`,
  `boolean_union/difference/intersection`, `offset_curve`, `pipe`, `run_command`,
  `execute_rhinoscript_python_code`, `execute_rhinocommon_csharp_code`,
  `gh_add_component`, `gh_build_graph`, `gh_connect_components`, `gh_mutate_graph`,
  `gh_run_solution`.
- **Sensors (eyes):** `get_document_summary`, `get_objects`, `get_object_info`,
  `analyze_objects`, `capture_viewport`, `gh_get_canvas_state`.
- **Docs:** `search_rhinoscript_functions`, `get_rhinoscript_docs`,
  `gh_get_component_type_info`, `validate_connection`.

### Required / recommended MCP extensions

The skill layer currently compensates for several gaps with shims (create-then-find-newest GUID
diff, manual bbox math, whole-document re-query). The following server-side extensions would let
the suite drop those shims and run the loop more cheaply and reliably:

| Extension | What it provides | Which correction / cost it serves |
|-----------|------------------|-----------------------------------|
| **Annotated / material-tagged capture** | `capture_viewport` that bakes per-part color/material/label into the image (or returns a part→color legend) | C4 — reliable "is the red part above the blue part?" without a fragile color-then-restore dance |
| **Bbox-by-GUID** | Direct bounding box (and volume) for a given GUID without a full `get_objects` dump | C2 / token economy — measure-verify count+volume cheaply, no whole-document re-query |
| **Object-to-object distance query** | Min distance / overlap between two GUIDs | C3 — confirm the 0.5–2 mm interpenetration before a union, not after it silently fails |
| **GUID round-trip on every mutator** | Every typed mutator (loft/boolean/extrude/…) returns the GUID it created | C1 — removes the create-then-find-newest shim entirely |
| **Changed-since-T delta** | List objects added/modified/deleted since a snapshot token | C1 / token economy — exact post-mutation diff instead of set-differencing the whole table |
| **Transaction grouping** | Begin/commit/rollback a group of mutations as one unit | C8 / C1 — clean atomic repair attempts and reliable rollback when a fix regresses a passing item |

Until these land, the skills run on the standard `rhinomcp` surface using the documented shims —
nothing here requires the extensions to function; they only make it cheaper and more robust.
