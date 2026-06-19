#! python3
"""Repair budget + convergence ledger for the rhino-repair skill (correction C8).

Pure stdlib, runs under any Python 3. This is decision/bookkeeping logic only --
it touches NO Rhino geometry, so it is safe to run off the Rhino process. Execute it
via ${CLAUDE_SKILL_DIR}; ONLY its stdout enters the agent context.

Two independent limits enforced here:
  * per-failure-item budget: each failing item gets up to N attempts (default 3),
    then it is marked could_not_fix and surfaced to the user.
  * global wall: a hard ceiling on TOTAL repair iterations across all items
    (default 12), independent of any single item's budget.

The ledger also tracks per-part pass/fail so the loop CONVERGES instead of
oscillating: if an attempt makes a previously-passing item regress, the attempt is
revertible and the pair is flagged as a conflict rather than ping-ponging forever.

Usage:
    python3 repair_budget.py            # runs a self-demonstration and prints a report
    python3 repair_budget.py --json     # same demo, machine-readable JSON to stdout

Importable API (the part the skill actually uses):
    ledger = RepairLedger(per_item_budget=3, global_wall=12)
    ledger.register(item_id, part_id)
    ok = ledger.may_attempt(item_id)            # False if item exhausted or wall hit
    ledger.record_attempt(item_id, part_id, passed=<bool>, measured=..., fix="...")
    ledger.regressed(item_id, by_item_id)       # flag a conflict, revert recommended
    ledger.converged()                          # True when no row is still 'open'/'in_progress'
    print(ledger.report())                      # human report (also surfaces could_not_fix/conflict)
"""

import argparse
import json

# Default budgets (correction C8). Mirrors ../../shared/conventions.md sec.10.
PER_ITEM_BUDGET = 3
GLOBAL_WALL = 12

# Row states for the (item, part) status ledger.
OPEN = "open"
IN_PROGRESS = "in_progress"
PASS = "pass"
COULD_NOT_FIX = "could_not_fix"
CONFLICT = "conflict"

TERMINAL_STATES = (PASS, COULD_NOT_FIX, CONFLICT)


class Row:
    """One (item, part) status row in the convergence ledger."""

    __slots__ = ("item_id", "part_id", "state", "attempts",
                 "last_measured", "last_fix", "regressed_by")

    def __init__(self, item_id, part_id):
        self.item_id = item_id
        self.part_id = part_id
        self.state = OPEN
        self.attempts = 0
        self.last_measured = None
        self.last_fix = None
        self.regressed_by = None  # item_id whose fix broke this row, if any

    def as_dict(self):
        return {
            "item_id": self.item_id,
            "part_id": self.part_id,
            "state": self.state,
            "attempts": self.attempts,
            "last_measured": self.last_measured,
            "last_fix": self.last_fix,
            "regressed_by": self.regressed_by,
        }


class RepairLedger:
    """Tracks per-item attempt budgets, a global wall, and per-part convergence."""

    def __init__(self, per_item_budget=PER_ITEM_BUDGET, global_wall=GLOBAL_WALL):
        if per_item_budget < 1:
            raise ValueError("per_item_budget must be >= 1")
        if global_wall < 1:
            raise ValueError("global_wall must be >= 1")
        self.per_item_budget = per_item_budget
        self.global_wall = global_wall
        self.global_iterations = 0
        self._rows = {}  # (item_id, part_id) -> Row

    # -- registration -------------------------------------------------------
    def register(self, item_id, part_id):
        """Register a failing (item, part) row if not already present."""
        key = (item_id, part_id)
        if key not in self._rows:
            self._rows[key] = Row(item_id, part_id)
        return self._rows[key]

    def rows_for_item(self, item_id):
        return [r for (i, _p), r in self._rows.items() if i == item_id]

    def item_attempts(self, item_id):
        """Max attempts spent on any part of this item (an item-level attempt
        re-authors the offending part, so attempts are counted per item)."""
        rows = self.rows_for_item(item_id)
        return max((r.attempts for r in rows), default=0)

    # -- budget gating ------------------------------------------------------
    def wall_hit(self):
        return self.global_iterations >= self.global_wall

    def item_exhausted(self, item_id):
        return self.item_attempts(item_id) >= self.per_item_budget

    def may_attempt(self, item_id):
        """True only if BOTH the per-item budget and the global wall allow it.

        Tier-1 syntax re-emits should pass count_global=True to record_attempt so
        they spend the global wall but not the per-item budget (see SKILL.md).
        """
        if self.wall_hit():
            return False
        if self.item_exhausted(item_id):
            return False
        rows = self.rows_for_item(item_id)
        if rows and all(r.state in TERMINAL_STATES for r in rows):
            return False
        return True

    # -- recording attempts -------------------------------------------------
    def record_attempt(self, item_id, part_id, passed, measured=None, fix=None,
                        spend_item_budget=True):
        """Record one repair attempt's outcome and advance the ledger.

        passed=True  -> row -> PASS.
        passed=False -> row stays IN_PROGRESS until per-item budget is spent,
                        then -> COULD_NOT_FIX (surfaced to the user).
        spend_item_budget=False is for Tier-1 syntax fixes: they tick the global
        wall but do not consume the per-item numeric/visual budget.
        """
        row = self.register(item_id, part_id)
        self.global_iterations += 1
        if spend_item_budget:
            row.attempts += 1
        row.last_measured = measured
        row.last_fix = fix

        if passed:
            row.state = PASS
        else:
            if self.item_exhausted(item_id):
                row.state = COULD_NOT_FIX
            else:
                row.state = IN_PROGRESS
        return row

    def regressed(self, item_id, part_id, by_item_id):
        """Flag that a fix for `by_item_id` made (item_id, part_id) regress.

        Per SKILL.md: revert that attempt and flag a conflict so two items do not
        oscillate until the global wall.
        """
        row = self.register(item_id, part_id)
        row.state = CONFLICT
        row.regressed_by = by_item_id
        return row

    # -- convergence --------------------------------------------------------
    def converged(self):
        """Converged when every row is terminal (no row still open/in_progress)."""
        if not self._rows:
            return True
        return all(r.state in TERMINAL_STATES for r in self._rows.values())

    def unresolved(self):
        """Rows the caller must surface: could_not_fix or conflict."""
        return [r for r in self._rows.values()
                if r.state in (COULD_NOT_FIX, CONFLICT)]

    def fixed(self):
        return [r for r in self._rows.values() if r.state == PASS]

    # -- reporting ----------------------------------------------------------
    def as_dict(self):
        return {
            "per_item_budget": self.per_item_budget,
            "global_wall": self.global_wall,
            "global_iterations": self.global_iterations,
            "wall_hit": self.wall_hit(),
            "converged": self.converged(),
            "rows": [r.as_dict() for r in self._rows.values()],
            "fixed": [r.item_id for r in self.fixed()],
            "unresolved": [r.as_dict() for r in self.unresolved()],
        }

    def report(self):
        lines = []
        lines.append("repair ledger: %d/%d global iterations (wall %s), per-item budget=%d"
                     % (self.global_iterations, self.global_wall,
                        "HIT" if self.wall_hit() else "ok", self.per_item_budget))
        lines.append("converged=%s" % self.converged())
        lines.append("%-14s %-10s %-14s %-4s %-14s %s"
                     % ("item", "part", "state", "att", "last_measured", "last_fix"))
        for r in self._rows.values():
            lines.append("%-14s %-10s %-14s %-4d %-14s %s"
                         % (r.item_id, r.part_id, r.state, r.attempts,
                            str(r.last_measured), str(r.last_fix or "")))
        unresolved = self.unresolved()
        if unresolved:
            lines.append("SURFACE TO USER (%d):" % len(unresolved))
            for r in unresolved:
                if r.state == CONFLICT:
                    lines.append("  conflict: %s vs %s (cannot both pass)"
                                 % (r.item_id, r.regressed_by))
                else:
                    lines.append("  could_not_fix: %s part=%s after %d attempts; "
                                 "last_measured=%s last_fix=%s"
                                 % (r.item_id, r.part_id, r.attempts,
                                    r.last_measured, r.last_fix))
        return "\n".join(lines)


def _demo():
    """Self-demonstration: drives the ledger through pass, could_not_fix, and conflict."""
    ledger = RepairLedger(per_item_budget=PER_ITEM_BUDGET, global_wall=GLOBAL_WALL)

    # Item A: overall_height numeric mismatch on part 'seat' -> fixed on 2nd try.
    ledger.register("overall_height", "seat")
    if ledger.may_attempt("overall_height"):
        ledger.record_attempt("overall_height", "seat", passed=False,
                               measured=412.0, fix="seat.dims.z 18->40")
    if ledger.may_attempt("overall_height"):
        ledger.record_attempt("overall_height", "seat", passed=True,
                               measured=450.0, fix="seat.dims.z 40->56")

    # Item B: solid_count topology question on 'legs' -> never fixable, exhausts budget.
    ledger.register("solid_count", "legs")
    while ledger.may_attempt("solid_count"):
        ledger.record_attempt("solid_count", "legs", passed=False,
                               measured=3, fix="re-union with 1mm penetration")

    # Item C: a fix for D regressed C -> conflict surfaced, not oscillated.
    ledger.register("seat_depth_ratio", "seat")
    ledger.regressed("seat_depth_ratio", "seat", by_item_id="back_height_ratio")

    return ledger


def main():
    ap = argparse.ArgumentParser(description="rhino-repair budget/convergence ledger (C8)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()
    ledger = _demo()
    if args.json:
        print(json.dumps(ledger.as_dict(), indent=2, default=str))
    else:
        print(ledger.report())


if __name__ == "__main__":
    main()
