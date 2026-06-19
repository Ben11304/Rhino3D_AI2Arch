# IronPython / .NET traps — `execute_rhinocommon_csharp_code` and lamcp

When a repair must reach for `execute_rhinocommon_csharp_code` (or the lamcp C#/.NET path) —
or when IronPython is the runtime behind `execute_rhinoscript_python_code` — a different class
of failure appears: **.NET type/overload/thread traps** that throw before any geometry logic
runs. These are **Tier-1 syntactic/runtime** failures (see [../SKILL.md](../SKILL.md)): the
fix is a code correction routed through the docs, and it does **not** burn verify budget.

Prefer typed MCP tools over raw `execute_*` (conventions §11). Only fall back to the C#/lamcp
path for ops with no typed tool (revolve, shell, network surface). When you must, avoid these.

---

## Trap 1 — type checks: `GetType().Name`, not `isinstance`

**Symptom.** `isinstance(obj, Brep)` returns False for an object that is plainly a Brep, or a
`TypeError`/`NameError` about the type, or a CLR object that does not behave like the Python
type you expected.

**Cause.** Across the .NET/IronPython boundary the object is a **CLR instance**, and Python's
`isinstance` against an imported `Rhino.Geometry.Brep` is unreliable — the proxy type identity
does not always match. A `RhinoObject`'s `.Geometry` may also surface as `GeometryBase`, an
`Extrusion`, or a `Brep` depending on how it was baked.

**Fix.** Branch on the **CLR runtime type name**, and convert `Extrusion`→`Brep` explicitly:

```python
#! python3
import Rhino
from Rhino.Geometry import Brep, Extrusion

def as_brep(geom):
    tname = geom.GetType().Name          # reliable across the boundary; "Brep", "Extrusion", ...
    if tname == "Brep":
        return geom
    if tname == "Extrusion":
        return geom.ToBrep(True)         # splitKinkyFaces=True
    if tname == "Surface" or "Surface" in tname:
        return Brep.CreateFromSurface(geom)
    return None
```

In C# (`execute_rhinocommon_csharp_code`) the equivalent is pattern matching
`if (geom is Brep b) { ... }` / `geom.GetType().Name`, **not** a reflection guess. Confirm the
actual member names via `gh_get_component_type_info` or `get_rhinoscript_docs` before relying
on them.

---

## Trap 2 — `RemoveSource` / object-table GUID overloads

**Symptom.** `TypeError: expected Guid, got RhinoObject` (or the reverse), or
`Delete`/`Replace`/`RemoveSource` "no overload matches" errors when removing or replacing an
object, or a Grasshopper component's `RemoveSource` rejects your argument.

**Cause.** Many object-table and component methods are **overloaded on `System.Guid` vs the
object** (`RhinoObject`, or a GH `IGH_Param`). Passing the wrong one throws an overload-
resolution error. `sc.doc.Objects.Delete` accepts a `Guid`, a `RhinoObject`, or an
`ObjRef` — different overloads with different semantics; GH param `RemoveSource(Guid)` removes
by the **source param's InstanceGuid**, not by a wire or a value.

**Fix.** Pass the **Guid overload** deliberately and keep GUIDs (not live objects) in the
ledger (C1):

```python
#! python3
import scriptcontext as sc
import System

# Delete by GUID overload (the ledger stores GUIDs, not RhinoObjects):
guid = scene_graph[part_id]                    # a System.Guid
sc.doc.Objects.Delete(guid, True)              # (Guid, bool quiet) overload

# Replace geometry by GUID, then re-stamp identity so the ledger stays valid:
obj = sc.doc.Objects.FindId(guid)              # FindId takes a Guid and returns the RhinoObject
sc.doc.Objects.Replace(guid, new_brep)         # (Guid, Brep) overload
```

For Grasshopper via `gh_mutate_graph` / lamcp, `RemoveSource` expects the **source component's
InstanceGuid** (a `System.Guid`), so disconnect by `gh_connect_components` semantics or pass
the param's `InstanceGuid`, never the wire object. When an overload is ambiguous, cast
explicitly (`System.Guid` / `clr` cast) rather than letting IronPython guess.

---

## Trap 3 — `System.Convert.ToDouble`, not `float(System.Decimal)`

**Symptom.** `float(x)` raises `TypeError`/`invalid literal`, or silently truncates, when `x`
came from a .NET numeric source (`System.Decimal`, `System.Single`, a boxed numeric, or a GH
`GH_Number`). Slider values and some component outputs arrive as `System.Decimal`.

**Cause.** Python's `float()` does not reliably convert **boxed .NET numeric types**.
`System.Decimal` in particular is not a Python float and `float(decimal_value)` can throw or
lose precision.

**Fix.** Convert with `System.Convert.ToDouble` (handles Decimal/Single/boxed numerics
uniformly):

```python
#! python3
import System

def to_double(x):
    # Robust across System.Decimal, System.Single, System.Int32, boxed numerics, numeric strings.
    return System.Convert.ToDouble(x)

radius = to_double(slider_value)               # slider_value may be System.Decimal
height = to_double(component_output)           # may be System.Single / GH_Number's .Value
```

In C#, take the value as `double` via `Convert.ToDouble(...)` or read the strongly-typed
`.Value`; do not `(double)(object)` a boxed `Decimal` — that throws `InvalidCastException`.
Round-trip through `System.Convert` whenever a number crosses from GH/.NET into your geometry
math.

---

## Trap 4 — no heavy geometry mutation on the HTTP (non-UI) thread

**Symptom.** Rhino **crashes outright** (or freezes, or the document corrupts) during or just
after an `execute_*` / lamcp call that added, deleted, or rebuilt many objects — often
intermittently, with no Python traceback because the process died.

**Cause.** The MCP/lamcp bridge handles the request on a **background HTTP thread**, but
Rhino's document and display are **not thread-safe**. Mutating `sc.doc.Objects` (Add/Delete/
Replace), rebaking large Breps, or redrawing views from the non-UI thread races the UI thread
and can hard-crash Rhino. Reads are usually tolerable; **mutations are not**.

**Fix.** Marshal document mutations onto the **main/UI thread**, and keep the worker thread to
pure geometry computation (no `sc.doc` writes):

```python
#! python3
import scriptcontext as sc
import Rhino

def commit_on_ui_thread(action):
    # Build/compute geometry off-thread; do the doc mutation + redraw on the UI thread.
    def _do():
        action()                         # AddBrep / Delete / Replace happen here
        sc.doc.Views.Redraw()            # redraw only on the UI thread
    Rhino.RhinoApp.InvokeOnUiThread(System.Action(_do))

import System
# usage: compute brep first (safe off-thread), then:
commit_on_ui_thread(lambda: sc.doc.Objects.AddBrep(brep, attr))
```

`Rhino.RhinoApp.InvokeOnUiThread(System.Action)` queues the delegate onto Rhino's UI thread.
Do **one** redraw at the end, on the UI thread (conventions §5 step 9). If a repair must
mutate many objects, batch the geometry computation off-thread and commit the whole batch in a
single UI-thread invocation rather than per-object — fewer cross-thread hops, far lower crash
risk. After any crash-suspected failure, treat the document as untrusted: re-read with
`get_document_summary` and reconcile the scene-graph before continuing (C1).
