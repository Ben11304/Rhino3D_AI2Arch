# Grasshopper definition patterns

Concrete, ready-to-build definition **templates** expressed as component graphs
(component types + slider table + connections). Each is shaped so you can feed it almost
directly to `gh_build_graph` after you (1) confirm the real port names with
`gh_get_component_type_info` and (2) `validate_connection` on every wire. The port letters
below are the *usual* GH letters — treat them as the plan, confirm them as the truth.

Wiring/adapter rules referenced here live in [`gh-wiring.md`](gh-wiring.md). The geometry
corrections (C2 count+volume, C3 0.5–2 mm interpenetration, C6 cap-before-solid) are from
[`../../shared/conventions.md`](../../shared/conventions.md) and still apply inside GH.

The shared graph-plan shape each template follows:

```json
{
  "components": [
    {"id": "slider_radius", "type": "Number Slider",
     "slider": {"min": 10, "max": 60, "value": 25, "rounding": "Float"}},
    {"id": "cylinder", "type": "Cylinder"}
  ],
  "sliders_table": [
    {"id": "slider_radius", "param": "radius", "wires_to": "cylinder.R", "order": 0}
  ],
  "connections": [
    {"from": "slider_radius.N", "to": "cylinder.R"}
  ]
}
```

`from`/`to` are `component_id.port`. `order` records slider authoring order so
"first slider → input A" holds (gh-wiring §4.2). Run
[`validate_graph_plan.py`](validate_graph_plan.py) on this JSON before building.

---

## Pattern A — Revolve a profile (lathe a vase / bottle / table leg)

Spin a profile curve about an axis into a solid of revolution, with sliders for the profile
size and the sweep angle. Honors **C6**: `RevSrf` yields a *surface*, so cap it and check
`IsSolid` before treating it as a solid.

**Sliders (in order):**
0. `profile_height` (Number, 50..400, value 200, Float)
1. `profile_radius` (Number, 10..120, value 40, Float)
2. `neck_radius` (Number, 2..60, value 12, Float)
3. `revolve_angle` (Number, 0..360, value 360, Float — Integer if you want whole degrees)

**Components & wiring:**

```
slider_profile_height.N ─┐
slider_profile_radius.N ─┤   (used to place the interpolation points)
slider_neck_radius.N    ─┘

Construct Point (x=profile_radius, y=0, z=0)        -> pt_base
Construct Point (x=neck_radius,    y=0, z=profile_height) -> pt_top
Construct Point (x=profile_radius*0.6, y=0, z=profile_height*0.5) -> pt_mid   (via Multiplication comps)
   (build the in-between points with `Multiplication` / `Construct Point`; all share y=0 so the
    profile is PLANAR in worldXZ and COPLANAR with the Z axis — C7)

Interpolate (Curve) : V = {pt_base, pt_mid, pt_top}      -> profile_curve
   (profile must touch the axis at both ends OR be closed — for a vase, drop the first and
    last points onto x=0 with `Construct Point(x=0, y=0, z)` so it starts/ends ON the Z axis — C6)

Line (Z axis): A = Construct Point(0,0,0), B = Construct Point(0,0,profile_height) -> axis_line
   (axis is coplanar with the profile because every profile point has y=0 — C7)

Revolution (RevSrf): Profile (P)=profile_curve, Axis (A)=axis_line, Domain=revolve_angle
   -> rev_surface

Cap Holes: Brep/Surf = rev_surface -> capped_brep      (C6: now a candidate solid)
```

**Verify (per C4/C6):** bake `capped_brep` or read its props — confirm `IsSolid` /
no naked edges and that `total_volume` is non-zero and tracks `profile_radius`. Vision only
for "does the silhouette read as a vase".

**`validate_connection` watch-list:** `Construct Point` outputs **Point**, but `Revolution`
`A` wants an **axis = Line/Plane** — wire the **Line** there, not a Point (gh-wiring §2).
`revolve_angle` is a **Number** feeding a **Domain/angle** input; use `Construct Domain
(0, radians)` or the component's degree input as `gh_get_component_type_info` reveals.

---

## Pattern B — Array on a curve (fence posts / balusters / lights along a path)

Place N copies of a base object at evenly spaced, **curve-oriented** frames along a rail.
The classic point-vs-plane trap (gh-wiring §2) lives here: you must orient to **planes**,
not points, or every copy faces world-up.

**Sliders (in order):**
0. `count` (Number, **Integer** rounding, 2..60, value 12)
1. `post_radius` (Number, 5..60, value 20, Float)
2. `post_height` (Number, 100..2000, value 900, Float)

**Components & wiring:**

```
Curve (input rail)            -> rail            (the path; a param the user sets/picks)

slider_count.N                -> divide.N
Divide Curve: C=rail, N=count -> { Points (P), Tangents (T), Params (t) }

Perp Frames: C=rail, t=Params -> frames         (Planes oriented to the curve — NOT points!)
   (Perp Frame / Horizontal Frame turns each param into an oriented Plane: gh-wiring §5)

slider_post_radius.N -> cyl.R
slider_post_height.N -> cyl.L
Cylinder: Base (B)=World XY plane, Radius (R)=post_radius, Length (L)=post_height -> base_post
   (author ONE base post on a plane, then orient it onto each frame)

Orient: Geometry (G)=base_post, Source (A)=World XY plane, Target (B)=frames -> posts
   (Orient maps the base from its plane to every frame plane; data-tree fans out over N frames)
```

**Verify (C4):** `Divide Curve` with `count=12` must yield **12** points → **12** posts;
check the count with `analyze_objects` after bake, not by eye. If a post is missing, it is a
data-tree / branch-empty issue (gh-wiring §6), not a vision problem.

**`validate_connection` watch-list:** feed **Perp Frame Planes** (not `Divide Curve` Points)
into `Orient` Target `B` — Points there silently force world-XY orientation (gh-wiring §2).
`count` must be **Integer**-rounded or `Divide Curve` warns/truncates (gh-wiring §4.1).

---

## Pattern C — Parametric louver (sun-shade blades with a falloff)

A row of N flat blades along a frame, each blade rotated by an angle that **varies along the
row** via a **Graph Mapper** falloff — the textbook Graph Mapper use (gh-wiring §4.1). Shows
slider + MD/Graph-Mapper interplay and the curve→surface→solid promotion (gh-wiring §3).

**Sliders (in order):**
0. `blade_count` (Number, **Integer**, 3..40, value 16)
1. `span` (Number, 200..3000, value 1500, Float — overall width the blades span)
2. `blade_width` (Number, 20..200, value 80, Float)
3. `blade_depth` (Number, 1..20, value 4, Float)
4. `max_angle` (Number, 0..90, value 45, Float — peak blade tilt in degrees)

**Components & wiring:**

```
slider_blade_count.N -> series.C  (count) and -> range.N

Range: Domain=Construct Domain(0,1), Steps=blade_count-1 -> t01   (a 0..1 series, one per blade)
Graph Mapper: input=t01 -> falloff01                      (editable curve: Bezier/Gaussian/Sine)
   (Graph Mapper REMAPS the 0..1 series; it needs the Range as input — gh-wiring §4.1)

slider_max_angle.N -> mult.B
Multiplication: A=falloff01, B=max_angle -> blade_angles_deg   (0..max_angle, shaped by falloff)
Radians: Degrees=blade_angles_deg -> blade_angles_rad

slider_span.N -> series.   (spacing)
Series: Start=0, Step=span/(blade_count-1), Count=blade_count -> x_positions
Construct Point: x=x_positions, y=0, z=0 -> blade_origins   (one per blade; data-tree of N)
XY Plane: O=blade_origins -> blade_planes                  (Point -> Plane adapter, gh-wiring §5)
Rotate Plane / Rotate: Plane=blade_planes, Angle=blade_angles_rad, Axis=world Y -> tilted_planes

slider_blade_width.N -> rect.X (domain)
slider_blade_depth.N -> rect.Y (domain)
Rectangle: Plane=tilted_planes, X=Construct Domain(-w/2,w/2), Y=Construct Domain(-d/2,d/2) -> blade_rects
Boundary Surfaces: Edges=blade_rects -> blade_faces        (Curve -> Surface, gh-wiring §3)
Extrude: Base=blade_faces, Direction=Amplitude(Unit Z, blade_thickness) -> blades   (-> solid Breps)
```

**Verify (C4):** `blade_count=16` → **16** blades; the tilt angles must be **monotonic along
the Graph Mapper falloff** (read the angle list, don't eyeball). Vision only for "do the
blades read as a louver / does the falloff look smooth".

**`validate_connection` watch-list:**
- `Construct Point` Points → `XY Plane` to get **Planes** for `Rectangle` (Rectangle wants a
  **Plane**, not a Point — gh-wiring §2).
- Graph Mapper input must be a normalized `0..1` `Range`; it does not synthesize values.
- `Rectangle` curves → `Boundary Surfaces` before `Extrude`, or the extrude yields open
  surfaces with no volume (gh-wiring §3 / C6).
- `blade_count` Integer-rounded so `Series`/`Range` step counts are whole (gh-wiring §4.1).

---

## Using a pattern

1. Pick the closest template; map the IR `params` onto its **slider table** (names + ranges
   from the IR, order recorded).
2. `gh_get_component_type_info` on every component → fill in the **real** port names.
3. `validate_connection` on every connection (insert adapters from gh-wiring §5 where the
   matrix says `ADAPTER`).
4. Run [`validate_graph_plan.py`](validate_graph_plan.py) on the assembled plan JSON.
5. `gh_build_graph` (one batched call) → `gh_run_solution` → read warnings → verify geometry
   (C2/C4/C6) → bounded repair via `gh_mutate_graph` (conventions §10, N=3 / wall=12).
