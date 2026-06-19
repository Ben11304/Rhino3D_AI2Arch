<div align="center">

# 🦏 Rhino3D_AI2Arch

### An Agent‑Skill suite that turns an LLM into a *reliable* Rhino / Grasshopper modeler — building editable 3D from a **text description** or a **reference image**.

![Rhino](https://img.shields.io/badge/Rhino-8-darkgreen?style=flat-square)
![Claude Code](https://img.shields.io/badge/Claude%20Code-Agent%20Skills-d97757?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/Protocol-MCP-444?style=flat-square)
![Skills](https://img.shields.io/badge/skills-8-blue?style=flat-square)
![Status](https://img.shields.io/badge/build-verified%20%E2%9C%93-success?style=flat-square)

*MCP is the **hands + eyes**. This suite is the **brain + method**.*

</div>

---

## Why this exists

Connecting an agent to a raw Rhino MCP server gets you *motor control* — `create_object`, `loft`,
`boolean_union`, `execute_*` — but the agent stays **weak at model logic and 3D spatial reasoning**.
It hallucinates API names, loses track of what it built, and "succeeds" at a 3‑legged chair because
a boolean silently dropped a leg.

The fix is **not** a bigger model — it's *method*. This suite attacks the LLM's spatial deficit
**at inference time** by:

1. **Externalizing spatial state** into two re‑readable artifacts — a validated *build‑plan IR* and a
   *scene‑graph* — instead of holding geometry in the model's head.
2. **Giving the model eyes** via a render‑and‑look loop, while routing every *measurable* question to
   math instead of vision.
3. **Encoding the eight ways LLM‑driven CAD silently fails** (the [corrections](#the-8-non-negotiable-corrections)) as hard rules every skill obeys.

> **Mental model:** *Never hold geometry state in your head; read it back from the document or the
> artifacts.* The MCP server does what your hands and eyes do; the skills supply the discipline,
> decomposition, and verification a bare tool call can't.

---

## Contents

- [Quickstart](#quickstart)
- [Layered architecture](#layered-architecture)
- [The 8 skills](#the-8-skills)
- [The modeling cognition loop](#the-modeling-cognition-loop)
- [The IR contract](#the-ir-contract-hand-off-between-phases)
- [The 8 non‑negotiable corrections](#the-8-non-negotiable-corrections)
- [Phased roadmap](#phased-roadmap)
- [Repo layout](#repo-layout)

---

## Quickstart

```bash
# 1. Get the suite where Claude Code can see it (symlink keeps it editable in place)
ln -s "$(pwd)" ~/.claude/skills/rhino-skills        # or copy the whole tree

# 2. Connect a Rhino MCP server (rhinomcp recommended) in Rhino 8, point Claude at it

# 3. Just ask:
#    "model a Windsor chair, seat height 450mm"        → text-to-model pipeline
#    "recreate this vase in Rhino"  (+ image)          → image-to-model pipeline
#    "make it parametric with a height slider"         → grasshopper-parametric
```

`rhino-modeling` auto‑routes the request, runs `detect_server.py` to confirm the live tool surface,
then drives the `plan → build → verify → repair` loop. The two *knowledge* skills load automatically;
you never invoke them by hand. See [`INSTALL.md`](INSTALL.md) for the full setup, the symlink caveat
(`../shared` links must resolve), and the list of MCP extensions that unlock v2+.

> **Note** — the suite targets a real, running Rhino + MCP server. The bundled Python helpers are
> stdlib‑only and CI‑verified (compile + run), but the geometry they orchestrate only executes inside
> Rhino.

---

## Layered architecture

```
            ┌──────────────────────────────────────────────────────────────┐
  USER  ──▶ │  rhino-modeling  (router / orchestrator, owns the loop + IR)  │
            └───────────────┬──────────────────────────────────────────────┘
                            │ routes by request type, drives plan→build→verify→repair
        ┌───────────────────┼───────────────────────────────┐
        ▼                   ▼                                ▼
  PRODUCERS           KNOWLEDGE (user-invocable:false)   PARAMETRIC
  text-to-model       rhino-geometry-api                 grasshopper-parametric
  image-to-model      rhino-scene-state (GUID ledger)
        │                   │                                │
        └─────── all hand off via two artifacts ─────────────┘
                            │
        ┌───────────────────┼───────────────────────────────┐
        ▼                                                    ▼
  render-and-look (eyes: vision + silhouette)         rhino-repair (bounded fix loop)

        ARTIFACTS (the externalized world state, re-read every step):
          • build-plan IR  (shared/build-plan.schema.json)   — declared INTENT
          • scene-graph    (rhino-scene-state/schema/...)    — realized GUID TRUTH

        BENEATH EVERYTHING:
          • Rhino MCP server  (hands + eyes: create/loft/boolean/execute_*, sensors, gh_*)
          • shared/conventions.md  (single source of truth for all 8 corrections)
```

The skill layer never does geometry the MCP server can't; it constrains *how* the model drives the
server: decompose‑to‑a‑validated‑IR before emitting, snapshot‑before‑mutate, render + measure +
repair in a bounded loop, and route every measurable question to math instead of vision.

---

## The 8 skills

| # | Skill | Phase it owns | One‑line purpose |
|---|-------|---------------|------------------|
| 1 | **rhino-modeling** | router / orchestrator | Classifies the request, owns the build‑plan IR artifact, and drives the master plan→build→verify→repair loop, delegating each phase to a sibling. |
| 2 | **rhino-geometry-api** *(knowledge)* | API knowledge | Authoritative RhinoCommon / rhinoscriptsyntax reference: type selection, the codegen‑guard contract, per‑op pre‑flight + post‑checks. Does not create objects. |
| 3 | **text-to-model** | text producer | Turns a worded description into a validated build‑plan IR and dimensioned solids; verifies **absolute** dimensions (scale is stated). |
| 4 | **image-to-model** | image producer | Reconstructs a model from a reference image via conditional factorization (discrete→continuous→scale); verifies scale‑invariant **ratios**, not absolutes. |
| 5 | **grasshopper-parametric** | Grasshopper phase | Builds a live GH definition with validated wiring and real slider ranges; also offers the cheaper v1 re‑runnable‑IR alternative. |
| 6 | **rhino-scene-state** *(knowledge)* | GUID ledger | Owns the scene‑graph artifact (`part_id → GUID` + bbox/volume ledger); reconciles expected‑vs‑actual after every mutation (MISSING / EXTRA / MIS‑SIZED / count+volume). |
| 7 | **render-and-look** | perception / verify | Sets named views, colors each part, authors 2–5 Yes/No/Unclear questions from the IR, answers them from low‑res captures, returns a differential repair list. |
| 8 | **rhino-repair** | repair | Triages failures (syntax → numeric → visual) and applies the smallest correct fix under the C8 budget, tracking a per‑(item,part) convergence ledger. |

Knowledge skills (`rhino-geometry-api`, `rhino-scene-state`) are `user-invocable: false`: they are
loaded by sibling skills, never invoked directly by the user.

---

## The modeling cognition loop

The router runs this checklist top to bottom; each phase is delegated to the owning skill.

```
[ ] 0. PREAMBLE   units + tolerance set & read live; pre-mutation summary snapshotted
[ ] 1. ROUTE      classify request → producer + server surface (detect_server.py)
[ ] 2. KNOW-API   pull the right RhinoCommon/MCP recipe        (rhino-geometry-api)
[ ] 3. PLAN       decompose intent → validated build-plan IR   (text/image producer)
[ ] 4. EMIT       guarded codegen, typed-tool-first, pre-flight inputs (C7)
[ ] 5. REMEMBER   bake → capture GUIDs → write scene-graph ledger (rhino-scene-state)
[ ] 6. SEE        render + measure: vision for shape, math for numbers (render-and-look)
[ ] 7. REPAIR     bounded fix loop on failing verify items     (rhino-repair)
[ ] 8. REPORT     surface "could not fix" items + final scene-graph + IR
```

Full detail with decision points and per‑phase ownership: [`rhino-modeling/reference/workflow.md`](rhino-modeling/reference/workflow.md).

---

## The IR contract (hand‑off between phases)

Every phase communicates through **two artifacts**, never through in‑context memory:

- **Build‑plan IR** — [`shared/build-plan.schema.json`](shared/build-plan.schema.json). The producer
  skills write `object`..`verify`; the router owns the artifact lifecycle and validates it against the
  schema. Every number carries an implicit **unit + frame** (the document `units`, measured against
  `world_frame`). Worked examples:
  [`text-to-model/examples/chair.json`](text-to-model/examples/chair.json) (absolute, text pipeline)
  and [`image-to-model/examples/vase.json`](image-to-model/examples/vase.json) (ratio + range, image
  pipeline).
- **Scene‑graph** — [`rhino-scene-state/schema/scene-graph.schema.json`](rhino-scene-state/schema/scene-graph.schema.json).
  Owned **solely** by `rhino-scene-state`; records the realized `part_id → GUID` ledger plus
  bbox/volume. GUIDs are appended here out‑of‑band, **never** written back into the IR.

**No two skills write the same artifact field.** Producers fill the IR content fields; the router owns
the IR artifact; `rhino-scene-state` is the only writer of the scene‑graph. The canonical rules every
skill defers to live in **[`shared/conventions.md`](shared/conventions.md)** — the single source of
truth.

---

## The 8 non‑negotiable corrections

These come from an adversarial review of how LLM‑driven CAD silently fails. Every relevant skill
honors them; [`shared/conventions.md`](shared/conventions.md) defines each one once.

| ID | Correction | Why it matters |
|----|------------|----------------|
| **C1** | **GUID ledger.** Objects are referenceable only by GUID; the scene‑graph stores `part_id → GUID` captured at bake. `UserString "part_id"` is the fallback resolver. Every mutator returns its GUID (wrap those that don't in a create‑then‑find‑newest shim). | A boolean consumes its inputs and mints a new object — the old GUIDs die. Without the ledger + fallback, later steps reference dead handles. |
| **C2** | **Partial/silent boolean failure.** Verify **expected solid count + total volume** against the IR, not just `IsValid`/`IsSolid`. | A boolean can drop a part and still return a *valid* solid. A 3‑legged chair (one leg silently lost) passes every naive guard; only count+volume catches it. |
| **C3** | **Interpenetration on joins.** Parts feeding a boolean union must **interpenetrate 0.5–2 mm**, never touch coincidentally. The IR join relation carries a required `penetration` field. | Coplanar/coincident contact is degenerate and makes unions fail partially or leave naked edges. |
| **C4** | **Demote vision.** Anything measurable (count, dimension, position, symmetry) goes through `analyze_objects` / bbox math, not vision. Color each part before capture so vision answers reliable relative‑position questions. | The LLM cannot read "450 mm" off a render reliably, but *can* tell "red is above blue". Keep vision to profile fidelity and "looks like X". |
| **C5** | **Image ⇒ verify ratios, not absolutes.** Image‑derived absolute sizes are guesses; carry scale as a `[min,max]` range + confidence. The image pipeline fires repairs on scale‑invariant **`ratio_checks`** only. The text pipeline verifies **absolutes**. | A wrong guessed millimeter must never corrupt topology or trigger a bad repair. |
| **C6** | **Revolve / shell.** `RevSurface.Create` returns a **surface, not a solid**; the profile must start+end on the axis (or be closed); **cap before shell**; `Brep.CreateOffset(solid=True)` needs a closed brep. | Skipping cap‑before‑shell or an off‑axis profile yields an open surface that silently fails to solidify. |
| **C7** | **Pre‑flight inputs**, not just post‑check results: loft curves same‑direction + seam‑aligned; sweep rail G1; fillet radius < min local edge; offset distance < min feature; revolve axis coplanar with profile. | Most op failures are caused by bad *inputs*; checking only the result wastes the budget chasing symptoms. |
| **C8** | **Repair budget.** A per‑failure‑item cap (each item ≤ N=3 attempts, then mark "could not fix" and surface) **plus** an independent global wall (default 12), never one global counter, with a per‑(item,part) convergence ledger. | Stops the loop from thrashing forever on one defect or ping‑ponging two conflicting items. |

> The most over‑trusted step is **`extract_profile`**: average left + right silhouettes about the
> axis to cancel perspective skew, flag low confidence, and fall back to archetype profiles chosen by
> the render‑vs‑reference loop rather than trusting raw pixel sampling.

---

## Phased roadmap

- **v1 (current).** Pick **one** MCP server (prefer `rhinomcp`) for the whole job — don't straddle two
  servers mid‑build (the GUID ledger desyncs, verification loses parity, cross‑server re‑queries break
  token economy). Full plan→build→verify→repair loop on `rhinomcp`. For "parametric", prefer the
  re‑runnable‑IR alternative (≈80% of the value, 0% of the GH wiring risk) unless Grasshopper is
  explicitly requested.
- **v2.** First‑class Grasshopper definitions: introspect‑then‑validate‑then‑wire, batched
  `gh_build_graph` / `gh_mutate_graph`, run‑then‑read‑warnings, geometry corrections (C2/C3/C6)
  enforced inside GH.
- **Beyond.** Multi‑server bridging once the required MCP extensions land (see [`INSTALL.md`](INSTALL.md)):
  annotated/material‑tagged capture, bbox‑by‑GUID, object‑to‑object distance query, GUID round‑trip on
  every mutator, changed‑since‑T delta, and transaction grouping. These close the gaps where the skill
  layer currently compensates with shims (create‑then‑find‑newest, manual bbox math, whole‑document
  re‑query).

---

## Repo layout

```
rhino-skills/
├── README.md  INSTALL.md  MANIFEST.md
├── shared/                  conventions.md (single source of truth) + build-plan.schema.json
├── rhino-modeling/          router/orchestrator + detect_server.py
├── rhino-geometry-api/      knowledge: RhinoCommon recipes + codegen_guard.py
├── text-to-model/           text producer + validate_plan.py + examples/chair.json
├── image-to-model/          image producer + extract_profile.py + examples/vase.json
├── grasshopper-parametric/  GH phase + validate_graph_plan.py
├── rhino-scene-state/       knowledge: GUID ledger + reconcile.py + scene-graph.schema.json
├── render-and-look/         perception/verify + set_named_views.py
└── rhino-repair/            bounded repair + repair_budget.py
```

See [`MANIFEST.md`](MANIFEST.md) for a one‑line purpose per file and [`INSTALL.md`](INSTALL.md) for
installation.

---

<div align="center">
<sub>Built as a reasoning layer over a Rhino MCP server · 8 skills · 40 files · all helpers compile + run‑verified</sub>
</div>
