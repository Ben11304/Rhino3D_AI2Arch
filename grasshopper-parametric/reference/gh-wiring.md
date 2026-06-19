# Grasshopper wiring & port semantics

The hard knowledge base for **which output type may feed which input**, the slider
taxonomy, slider ordering, and the common type-mismatch errors with their concrete fixes.
This is the reference the skill consults *before* every wire. It exists because a GH wire
that "looks right" will silently coerce or drop the wrong type — the LLM graph deficit.

Always confirm the **real** port names and types at runtime with
`gh_get_component_type_info` (port letters differ per component), and validate each wire
with `validate_connection` before building. The tables below are the mental model; the MCP
calls are the ground truth.

---

## 0. The one rule

> **Introspect the ports → validate the connection → then wire.** Never type a port name
> from memory, never assume an output type silently fits an input. A wrong-named port is a
> no-op; a wrong-typed wire is an invisible coercion.

GH coerces along a fixed hierarchy (roughly: `Integer → Number → Text`,
`Point → Vector` in some contexts, `Curve → Geometry`, `Brep/Surface/Mesh → Geometry`).
Coercion that GH *cannot* do quietly produces an empty branch or an "Invalid cast"
warning. The whole point of `validate_connection` is to find the second class before you
run the solution.

---

## 1. Data-type compatibility matrix (source output → target input)

`OK` = wires directly. `ADAPTER` = needs an inserted component (see §5). `NO` = never
valid; rethink the graph.

| source output \ target input | Number | Integer | Point | Vector | Plane | Line | Curve | Surface | Brep | Mesh | Domain | Geometry |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Number**   | OK | OK (rounds) | NO | NO | NO | NO | NO | NO | NO | NO | ADAPTER `Construct Domain` | NO |
| **Integer**  | OK | OK | NO | NO | NO | NO | NO | NO | NO | NO | ADAPTER | NO |
| **Point**    | NO | NO | OK | ADAPTER `Vector 2Pt` | ADAPTER `XY Plane`/`Plane Origin` | NO | NO | NO | NO | NO | NO | OK |
| **Vector**   | NO | NO | ADAPTER `Move`/`Pt+Vec` | OK | NO | NO | NO | NO | NO | NO | NO | OK |
| **Plane**    | NO | NO | ADAPTER `Plane Origin` | ADAPTER `Deconstruct Plane`→Z | OK | NO | NO | NO | NO | NO | NO | OK |
| **Line**     | NO | NO | ADAPTER `End Points` | ADAPTER `Line` dir | NO | OK | OK | NO | NO | NO | NO | OK |
| **Curve**    | NO | NO | ADAPTER `Evaluate Curve` | NO | ADAPTER `Perp Frame`/`Horizontal Frame` | NO | OK | ADAPTER `Boundary Surfaces`/`Patch` | ADAPTER `Boundary Surfaces`→`Brep` | NO | OK | OK |
| **Surface**  | NO | NO | NO | NO | ADAPTER `Surface Frame` | NO | ADAPTER `Brep Edges`/`Isocurve` | OK | OK (auto) | ADAPTER `Mesh Surface` | NO | OK |
| **Brep**     | NO | NO | NO | NO | NO | NO | ADAPTER `Brep Edges` | ADAPTER `Deconstruct Brep`→Faces | OK | ADAPTER `Mesh Brep` | NO | OK |
| **Mesh**     | NO | NO | NO | NO | NO | NO | NO | NO | NO | OK | NO | OK |
| **Domain**   | ADAPTER `Deconstruct Domain` | NO | NO | NO | NO | NO | NO | NO | NO | NO | OK | NO |
| **Geometry** | NO | NO | NO | NO | NO | NO | OK* | OK* | OK* | OK* | NO | OK |

`OK*` = the generic `Geometry` param carries whatever was upstream; the downstream
component will reject it at solve time if the actual content is the wrong kind. Prefer a
typed param so `validate_connection` can catch it early.

Reading the table: a component's **output type** is the row; the **input you want to feed**
is the column. Anything but `OK` means insert the named adapter or restructure.

---

## 2. Point vs. Plane (the most common silent error)

`Point` and `Plane` are **not interchangeable**, and this is the single most frequent
mis-wire.

- Many "place / orient" inputs (`Rectangle`, `Circle`, `Box`, `Orient`, `Revolution` base,
  text dot, `Construct Mesh` frame) want a **Plane**, not a Point. Feeding a Point either
  fails to wire or GH auto-promotes it to a **world-XY plane at that point**, quietly
  discarding any intended rotation.
- A **Plane** fed where a **Point** is wanted usually auto-demotes to the plane **origin**,
  silently dropping orientation.

**Fixes:**
- Point → Plane that should stay axis-aligned: `XY Plane` (origin = point) → use its Plane
  output.
- Point → Plane that should be oriented to a curve: `Perp Frame` / `Horizontal Frame`
  (curve + parameter) → Plane.
- Plane → Point: `Plane Origin` (or `Deconstruct Plane` → Origin).
- Point → oriented Plane from three points: `Plane 3Pt`.

Rule of thumb: if the input letter is `P` it is **ambiguous** — `gh_get_component_type_info`
tells you whether that `P` is a Point param or a Plane param. Check, don't guess.

---

## 3. Curve vs. Surface vs. Brep

- A **closed planar Curve** is *not* a surface. Components that want area/solid
  (`Extrude`, `Boundary Surfaces`, `Solid Union`) need a **Surface** or **Brep**, not the
  bare curve.
- **Curve → Surface:** `Boundary Surfaces` (planar closed curves) or `Patch` (free-form) or
  `Edge Surface` (3–4 edge curves). **Curve → Brep:** same, then the surface auto-promotes
  to Brep on a Brep input.
- **Extrude** takes a **Curve OR Surface/Brep** as base `B` and a **Vector** as direction
  `D`. Extruding a *curve* yields an open surface; extruding a *closed planar surface*
  yields a **closed solid** — prefer the surface route when you need a solid.
- **Surface ↔ Brep** auto-coerce in most inputs (a single-face Brep is a Surface). Multi-face
  Breps do **not** demote to Surface; use `Deconstruct Brep` → Faces.
- **Brep → Mesh** for display/booleans-on-mesh: `Mesh Brep`. **Brep → Curve** (edges):
  `Brep Edges`. **Brep → Surface** (faces): `Deconstruct Brep`.

For solids you intend to boolean, keep them as **Brep** end-to-end and verify closure
(`IsSolid`/no naked edges) — see C2/C6 below.

---

## 4. Slider taxonomy and ordering

### 4.1 Which slider for which job

| Component | Emits | Use it for | Do NOT use it for |
|---|---|---|---|
| **Number Slider** | one `Number` (or `Integer`) | a single scalar knob: radius, height, count, angle, twist | anything that needs 2+ coupled values |
| **MD Slider** (multidimensional) | one `Point` whose XYZ are the slider position in a 0–1 box | a 2-D/3-D pick (a UV pick on a surface, an XY location, an RGB-ish triple) | a plain scalar — its output is a **Point**, not a Number (classic mis-wire) |
| **Graph Mapper** | remaps an incoming `0..1` series through an editable curve (Bezier/Sine/Gaussian/etc.) | falloff / easing along a domain: louver-blade angle vs. position, taper, gradient | generating values from nothing — it needs an input series (e.g. from `Range`/`Series`) |

Key gotchas:
- **MD Slider outputs a Point.** To pull a scalar out, `Deconstruct Point` → X/Y/Z. Wiring
  an MD Slider straight into a Number input is the "why is my radius a point?" bug.
- **Graph Mapper needs a normalized input** in `0..1` and **passes the same count back
  out**, remapped. Feed it `Range(0..1, n)` or a `Remap Numbers` to `0..1`; it does not
  create the series itself.
- **Number Slider rounding**: set `Integer` rounding for counts (e.g. blade count, array N)
  so downstream `Series`/`Divide Curve` get whole numbers — a fractional count warns or
  truncates.

### 4.2 Slider ordering — "first slider → input A"

When a downstream component has multiple same-typed inputs (e.g. `Domain` start/end,
`Construct Point` X/Y/Z, a custom cluster A/B/C), GH does **not** know which slider you
*meant* for which port. The convention this skill enforces:

> **Author sliders in the order the target consumes them, and wire them in that order:
> first authored slider → input A, second → input B, …**

Concretely:
- Decide the target's input order from `gh_get_component_type_info` (it returns ports in
  index order).
- Emit the sliders in `gh_build_graph` in **that same order**, and record the mapping in the
  graph-plan **slider table** (`slider_name → target.port`).
- If you author `height` before `radius` but the target is `Cylinder(Base, Radius, Length)`,
  you must wire `radius→Radius` and `height→Length` **explicitly by port name** — do not rely
  on positional luck. The ordering rule is a *default*; the explicit port wire is the
  guarantee. `validate_connection` confirms the type; the slider table confirms the *meaning*.

Getting this wrong is the silent radius↔height swap: the solution runs green, the model is
just wrong. Always cross-check the slider table against the IR `params` names.

---

## 5. Adapter components (the fixes)

Insert these between an incompatible source/target. Each is a real GH component.

| Need | Adapter component | Wiring |
|---|---|---|
| Point → axis-aligned Plane | **XY Plane** | Point → `O` (origin); use `Plane` output |
| Point → oriented Plane on a curve | **Perp Frame** / **Horizontal Frame** | Curve `C` + param `t` → `Plane` |
| Plane → Point | **Plane Origin** (or **Deconstruct Plane**) | Plane → Origin point |
| Closed planar Curve → Surface | **Boundary Surfaces** | Curves → `Surfaces` |
| Free curves → Surface | **Patch** / **Edge Surface** | Curves → `Surface` |
| Curve → solid by extrusion | **Extrude** | base Surface `B` + Vector `D` (use `Unit Z` × length) |
| Curve → plane along it | **Perp Frame** | Curve + `t` → Plane (for louver blades) |
| Number → Domain | **Construct Domain** | A (min) + B (max) → `Domain` |
| Domain → Numbers | **Deconstruct Domain** | Domain → Start/End |
| Number ↔ Vector length | **Amplitude** | Vector + Number → scaled Vector |
| Two Points → Vector | **Vector 2Pt** | A,B → Vector (set `Unitize` if you only want direction) |
| Brep → Mesh (for mesh booleans / display) | **Mesh Brep** | Brep → Mesh |
| Surface (revolve) → solid | **Cap Holes** | Brep/Surf → capped Brep (then check `IsSolid`) |
| Replicate one item across N | **Graft / Flatten / Longest List** | data-tree match before a per-branch op |

---

## 6. Common type-mismatch wiring errors → fixes

These map a GH warning/symptom to the cause and the concrete repair. After
`gh_run_solution`, read warnings (orange) and errors (red) via the solution result /
`gh_get_canvas_state` and apply the matching fix with a focused `gh_mutate_graph`.

| Symptom / warning text | Cause | Fix |
|---|---|---|
| "Invalid cast: Point → Plane" / rotation ignored | Point wired into a Plane input | insert `XY Plane` or `Perp Frame` (§2) |
| Output is a Point where you expected a number | MD Slider used as a scalar | `Deconstruct Point` → X/Y/Z, wire the right axis |
| "1 of N branches empty" | data-tree mismatch (different branch counts) | `Graft`/`Flatten`/`Longest List`/`Cross Reference` to match trees |
| "Solid Union failed" / fewer solids than inputs (C2) | coincident faces, no overlap | enforce **0.5–2 mm interpenetration** (C3): nudge inputs with `Move`+`Unit` vector before union; then re-check **expected solid count + total volume** |
| Revolve gives an open surface, no volume (C6) | `Revolution`/`RevSrf` returns a **surface**, or profile not on axis | profile must **touch the axis at both ends or be closed**; add `Cap Holes`; verify `IsSolid` before any shell/boolean |
| "Extrude produced no solid" | extruded a curve, not a surface | `Boundary Surfaces` first, then `Extrude` (§3) |
| Slider does nothing / range 0..1 | slider range left at default | set explicit min/max/value/rounding from the IR param |
| Count is fractional / `Series` warns | Number Slider feeding a count without integer rounding | set slider rounding to **Integer** (§4.1) |
| "Curve → Brep" rejected on a boolean | bare curve into a solid op | promote to Brep via `Boundary Surfaces`/`Extrude` (§3) |
| Number → Domain rejected (e.g. into `Range`/`Divide`) | scalar where a domain is expected | `Construct Domain (A,B)` then wire the Domain |
| Radius and height swapped, solution green | slider order/meaning wrong | fix per slider table (§4.2): wire **by explicit port name**, cross-check against IR `params` |

---

## 7. Pre-flight checklist before `gh_build_graph`

1. Every component in the plan has its **real port names/types** from
   `gh_get_component_type_info`.
2. Every connection passed `validate_connection` (or has an explicit **adapter** from §5).
3. Every Number Slider has **min/max/value/rounding** and appears in the **slider table**
   mapped to a target port (slider order recorded, §4.2).
4. No `Point→Plane`, `Curve→Brep`, `MD→Number`, or `Number→Domain` wires remain unadapted.
5. Booleans have upstream **interpenetration** (C3); revolves have **Cap Holes** (C6).
6. Run [`validate_graph_plan.py`](validate_graph_plan.py) on the plan JSON to catch dangling
   ports, sliders without ranges, and unrecorded slider order before you build.
