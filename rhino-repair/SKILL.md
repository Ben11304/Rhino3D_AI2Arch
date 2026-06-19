---
name: rhino-repair
description: >-
  Triages and fixes Rhino and Grasshopper modeling failures by applying the smallest
  correct fix within a bounded iteration budget. Triage order is strict: first
  syntactic/runtime errors (rhinomcp auto-rolls-back, re-read state, route the message
  back through the RhinoScript docs RAG), then numeric mismatch (adjust the one offending
  param), then visual mismatch (structural fix ONLY when a topology binary_question
  fails). Use this on any failed execute_rhinoscript_python_code or
  execute_rhinocommon_csharp_code, any gh_run_solution warning or error, or any
  measure/vision mismatch surfaced by analyze_objects or render-and-look. Enforces the
  C8 repair-budget rule — a per-failure-item attempt cap plus a separate global wall —
  tracking per-part pass/fail so repair converges instead of oscillating. Knows the
  operation-specific fixes (loft seam/twist, sweep rail continuity, revolve axis
  coplanarity, boolean coplanar/partial-union, fillet radius, offset self-intersection)
  and the IronPython/.NET traps of the C# and lamcp paths.
allowed-tools: []
---

# rhino-repair

The **repair** phase of the Rhino skill suite. Owns ONE job: when something has already
failed — a thrown exception, a `gh_run_solution` warning, a numeric check off by more than
its tolerance, or a vision binary_question that came back "no" — decide the **smallest
correct fix** and apply it, without thrashing.

This skill does not build models from scratch (that is `text-to-model` / `image-to-model`)
and does not decide *whether* a model is acceptable (that is `render-and-look`, which owns
the verify block). It is invoked **after** a failure is known, receives the failing item,
and returns either a fix or an honest "could not fix" surfaced to the caller.

All shared rules — units/tolerance discipline, the GUID ledger (C1), the codegen guard
contract, interpenetration (C3), revolve/shell (C6), vision-demotion (C4), ratio-vs-absolute
(C5), and the repair budget (C8) — live in **[../shared/conventions.md](../shared/conventions.md)**.
Read it; this skill does not duplicate it.

---

## When to invoke

- Any `execute_rhinoscript_python_code` or `execute_rhinocommon_csharp_code` call that threw
  or returned an error string.
- Any `gh_run_solution` that reported a component warning/error, or a graph that produced no
  output on a wire that should carry geometry.
- Any **numeric_check** or **ratio_check** from the verify block whose `|measured - expect|`
  exceeded `tol` (routed here by `render-and-look` / `analyze_objects`).
- Any **binary_question** that vision answered against expectation.

You receive: the **IR** (`build-plan.schema.json`), the **scene-graph** (part_id → GUID
ledger), and the **specific failing item** (error text, or `{metric, measured, expect, tol}`,
or `{question, expected, got}`). You do NOT re-run the whole verify suite — you fix the one
item and re-measure only the affected parts (token economy, conventions §11).

---

## Triage order (cheapest, most certain fix first)

Always walk these tiers **in order**. Never spend a numeric/visual repair attempt on a defect
that is actually a syntax or runtime error — that wastes budget and corrupts the convergence
signal.

### Tier 1 — Syntactic / runtime error (compiler + exception)

The failing call threw (Python `SyntaxError`, `NameError`, `TypeError`, a RhinoCommon
`Exception`, or a non-zero error from the MCP bridge).

1. **rhinomcp auto-rolls-back.** A failed `execute_*` leaves the document in its pre-call
   state — no half-baked objects to clean up. Do **not** start deleting things; assume the
   transaction reverted.
2. **Re-read world state**, do not trust your memory of it. Call **`get_document_summary`**
   (not a full `get_objects` dump) to confirm object counts/layers, and read the
   scene-graph artifact for part_id → GUID. If a GUID you expected is gone, resolve via the
   `UserString "part_id"` fallback (C1).
3. **Route the error back through the docs, RAG-style.** Take the exception's symbol
   (the bad function name, the wrong overload, the missing argument) and call
   **`search_rhinoscript_functions`** then **`get_rhinoscript_docs`** to get the correct
   signature *before* re-emitting code. For C#/CommonRhino overload and threading errors,
   consult **[reference/lamcp-dotnet-traps.md](reference/lamcp-dotnet-traps.md)**. Fix the
   call to match the real signature; re-run.
4. **Never burn verify budget on syntax.** A Tier-1 fix is a code correction, not a model
   defect — it does **not** consume the per-item numeric/visual attempt budget below.
   Re-emitting corrected code and re-running is "free" against C8 (but still subject to the
   global wall, so genuinely unrecoverable syntax loops do terminate).

### Tier 2 — Numeric mismatch (measurement off, geometry topologically fine)

The op ran, the Brep is valid and solid, but a `numeric_check` (text pipeline) or
`ratio_check` (image pipeline) is outside `tol`.

1. Identify the **single offending parameter** that drives the metric (e.g.
   `overall_height` ← a box `dims.z` or a cylinder `dims.height`; a ratio ← the part it
   scales). The IR `params` block and the check's `metric` name tell you which.
2. **Adjust that one param** toward the expected value; re-author only that part and the
   parts that depend on it. Do **not** re-model unrelated parts.
3. **Pipeline routing (C5):** if `scale.confidence` is `medium`/`low` (image pipeline), you
   may only repair against **ratio_checks** — never nudge a part to satisfy an absolute
   `numeric_check`. If `scale.confidence` is `high` (text pipeline, `value_source: stated`),
   repair against absolute `numeric_checks`.
4. Re-measure with **`analyze_objects`** / bbox math (C4 — never vision for a number).
   This counts as **one attempt** against that item's per-item budget.

### Tier 3 — Visual mismatch (it doesn't look right)

A `binary_question` or `compare_to_reference` came back wrong. Vision is reserved for
profile-shape fidelity and "does it look like X" (C4).

1. **Distinguish appearance from topology.** Most "looks wrong" answers are a *position/scale*
   problem already covered by Tier 2 numeric/ratio checks — handle those there. Re-color the
   parts and re-`capture_viewport` after a Tier-2 fix before assuming a structural defect.
2. **Structural fix ONLY when a topology binary_question fails** — e.g. "is the seat one
   connected solid?", "is the spout part of the body?", "are the four legs all present?".
   Those indicate a real topology defect (a partial boolean dropped a part per C2, a loft
   twisted, a revolve never closed). Apply the operation-specific fix from
   **[reference/failure-playbook.md](reference/failure-playbook.md)**.
3. A pure "the curve looks a bit lumpy / the proportion feels off" with all numeric and
   ratio checks passing and all topology questions passing is **not** a repair target — mark
   it acceptable and stop. Do not chase subjective vision.

---

## The repair budget (correction C8) — converge, never oscillate

Two **independent** limits. This is the rule that stops the loop from thrashing.

- **Per-failure-item budget:** each failing verify item gets up to **N = 3** attempts. After
  N attempts that item is marked **`could not fix`** and **surfaced to the user** with its
  last measured value and last applied fix. Do not keep retrying one defect.
- **Global wall:** a hard ceiling of **12** total repair iterations across *all* items,
  independent of any single item's budget. Hitting the wall stops the loop and reports every
  still-failing item. (Tier-1 syntax re-emits don't spend per-item budget but DO count toward
  this global wall, so an unrecoverable syntax loop still terminates.)

**Track per-part pass/fail so the loop converges instead of oscillating.** Keep a small
status ledger keyed by `(item, part_id)`. A fix for item A must not silently regress item B:
after each attempt, re-measure A **and** re-check the parts a prior pass had already marked
`pass`. If an attempt makes a previously-passing item fail, **revert that attempt** (rhinomcp
rolled back nothing this time — you mutated the doc — so undo it explicitly via the inverse
op or by re-baking the prior geometry) and try a different, smaller change. Never let two
items ping-pong: if items A and B cannot both pass, surface the conflict rather than
alternating fixes until the global wall.

The runnable budget/ledger helper is in
**[scripts/repair_budget.py](scripts/repair_budget.py)** — execute it via
`${CLAUDE_SKILL_DIR}` and read only its stdout.

```
status ledger (one row per (item, part)):  item_id | part_id | state | attempts | last_measured | last_fix
states: open -> in_progress -> pass | could_not_fix | conflict
converged when: every row is pass OR could_not_fix OR conflict, AND no row flipped state this pass.
```

---

## Output contract

Return to the caller, for every failing item handled:

- `fixed` items: the item id, the part(s) re-authored, the param/op changed, and the new
  measured value proving it now passes.
- `could_not_fix` items: the item id, attempts spent, last measured value, last fix tried,
  and a one-line reason — so the human (or the router) can decide.
- `conflict` items: the two items that cannot both be satisfied and the tradeoff.

Never claim success without a **fresh** measurement (C4 for numbers, colored-part vision for
topology). A silent partial boolean (C2) passes `IsValid`/`IsSolid`; only the
expected-count + total-volume check proves the fix took.

---

## References (one level deep)

- **[reference/failure-playbook.md](reference/failure-playbook.md)** — operation-specific
  symptom → cause → concrete fix for loft, sweep1, revolve, boolean, fillet, offset, and
  network surface.
- **[reference/lamcp-dotnet-traps.md](reference/lamcp-dotnet-traps.md)** — the IronPython
  and .NET traps of `execute_rhinocommon_csharp_code` / lamcp: type checks, GUID overloads,
  `System.Decimal` conversion, and the non-UI-thread crash hazard.
- **[../shared/conventions.md](../shared/conventions.md)** — the single source of truth for
  units, the GUID ledger, the codegen guard, interpenetration, revolve/shell, vision-demotion,
  ratio-vs-absolute, and the repair budget.
