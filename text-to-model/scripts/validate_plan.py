#!/usr/bin/env python3
"""validate_plan.py -- stdlib-only validator for the Rhino Build-Plan IR.

Validates a build-plan JSON file against the *intent* of
shared/build-plan.schema.json WITHOUT any third-party dependency
(no jsonschema, no numpy). Every check is implemented by hand so the
text-to-model skill can gate its IR before emitting geometry codegen.

What it enforces (beyond raw JSON Schema), with VERBOSE per-error messages:
  - required top-level fields (object, units, tolerance, parts)
  - units is a known enum; tolerance > 0
  - every numeric dimension carries a unit context + a resolvable frame
    (so a bare "240" is rejected as "missing unit; saw 240, frame=?")
  - scale block present and well-formed (value_source / overall_height_mm /
    confidence), range ordering min<=max
  - each part is EITHER primitive XOR operation, never both / neither
  - boolean-union joins declare an 'interpenetrate' relation with a
    'penetration' depth in (0,2] mm-ish (correction C3)
  - revolve generatrix profile starts AND ends ON the revolve axis (C6)
  - loft/sweep1/extrude/shell/boolean operation field-completeness (C7 inputs)
  - bbox sanity: declared overall height vs. realizable part extents

Usage:
    python3 validate_plan.py <plan.json>
    python3 validate_plan.py <plan.json> --schema ../../shared/build-plan.schema.json

Exit code 0 => valid. Non-zero => one or more errors (printed to stderr).
"""

import argparse
import json
import math
import os
import sys


# --------------------------------------------------------------------------- #
# unit + frame metadata (mirrors schema enums / conventions.md section 1 & 4)  #
# --------------------------------------------------------------------------- #

UNIT_ENUM = ("mm", "cm", "m", "in", "ft")

# millimetres-per-unit, used to convert IR units to the always-mm scale block.
MM_PER_UNIT = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "ft": 304.8,
}

# Named planes the executor can resolve (conventions.md section 4). A frame is
# "resolvable" if it has an explicit origin OR names one of these planes.
NAMED_PLANES = ("WorldXY", "WorldYZ", "WorldZX", "WorldXZ")

PRIMITIVE_ENUM = ("box", "cylinder", "sphere", "cone", "plane", "interpolated_curve")
OPERATION_ENUM = ("loft", "sweep1", "revolve", "extrude", "shell", "boolean")
BOOLEAN_OP_ENUM = ("union", "difference", "intersection")
RELATION_ENUM = (
    "coincident",
    "on_top_of",
    "symmetric_about",
    "child_of",
    "interpenetrate",
)
SCALE_SOURCE_ENUM = ("stated", "reference_object", "metrology_assumption")
CONFIDENCE_ENUM = ("high", "medium", "low")
SYMMETRY_ENUM = ("mirror", "rotational")

# dims keys expected per primitive (conventions.md / schema dims description).
DIMS_KEYS = {
    "box": ("x", "y", "z"),
    "cylinder": ("radius", "height"),
    "sphere": ("radius",),
    "cone": ("radius", "height"),
    "plane": ("x", "y"),
    "interpolated_curve": (),  # geometry lives in control_points, not dims
}


class Validator(object):
    """Accumulates verbose errors instead of failing on the first problem."""

    def __init__(self, plan, units):
        self.plan = plan
        self.units = units
        self.errors = []
        self.warnings = []

    # -- diagnostics -------------------------------------------------------- #

    def err(self, path, msg):
        self.errors.append("ERROR  %s: %s" % (path, msg))

    def warn(self, path, msg):
        self.warnings.append("WARN   %s: %s" % (path, msg))

    # -- helpers ------------------------------------------------------------ #

    def _frame_desc(self, frame):
        """Human-readable frame tag for verbose unit messages."""
        if not isinstance(frame, dict):
            return "?"
        origin = frame.get("origin")
        plane = frame.get("plane")
        if plane and origin:
            return "%s@%s" % (plane, origin)
        if plane:
            return str(plane)
        if origin:
            return "origin=%s" % (origin,)
        return "?"

    def _is_number(self, v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def _frame_is_resolvable(self, frame, world_frame):
        """A frame resolves if it has an explicit origin (always) and, when a
        named plane is given, that plane is one the executor knows."""
        if not isinstance(frame, dict):
            return False, "frame is not an object"
        if "origin" not in frame:
            return False, "frame has no 'origin' (point3d required)"
        origin = frame.get("origin")
        if not self._is_point3d(origin):
            return False, "frame.origin is not a [x,y,z] point3d (saw %r)" % (origin,)
        plane = frame.get("plane")
        if plane is not None and plane not in NAMED_PLANES:
            # custom frame names are allowed by the schema but must be non-empty
            if not isinstance(plane, str) or not plane.strip():
                return False, "frame.plane is empty/non-string"
        return True, ""

    def _is_point3d(self, v):
        return (
            isinstance(v, list)
            and len(v) == 3
            and all(self._is_number(x) for x in v)
        )

    def _check_dims_units(self, path, dims, frame, world_frame):
        """Every dims value must be a real number (its unit context is the
        document `units`) AND the owning part must carry a resolvable frame so
        the number is anchored. A number with no frame is the classic
        'missing unit/frame' bug we surface verbosely."""
        frame_tag = self._frame_desc(frame)
        resolvable = frame is not None
        if frame is not None:
            ok, why = self._frame_is_resolvable(frame, world_frame)
            resolvable = ok
            if not ok:
                self.err(path + ".frame", why)
        for key, val in dims.items():
            if not self._is_number(val):
                self.err(
                    path + ".dims." + key,
                    "dimension is not numeric; saw %r (unit=%s, frame=%s)"
                    % (val, self.units, frame_tag),
                )
                continue
            if val <= 0:
                self.err(
                    path + ".dims." + key,
                    "dimension must be > 0; saw %s %s (frame=%s)"
                    % (val, self.units, frame_tag),
                )
            if not resolvable:
                # This is the headline verbose message the task asks for.
                self.err(
                    path + ".dims." + key,
                    "missing unit context / frame; saw %s, unit=%s, frame=%s"
                    % (val, self.units, frame_tag if frame is not None else "?"),
                )

    # -- top-level structure ------------------------------------------------ #

    def validate(self):
        plan = self.plan
        if not isinstance(plan, dict):
            self.err("$", "top-level plan must be a JSON object")
            return

        for req in ("object", "units", "tolerance", "parts"):
            if req not in plan:
                self.err("$", "missing required field '%s'" % req)

        obj = plan.get("object")
        if obj is not None and (not isinstance(obj, str) or not obj.strip()):
            self.err("$.object", "must be a non-empty string; saw %r" % (obj,))

        units = plan.get("units")
        if units is not None and units not in UNIT_ENUM:
            self.err(
                "$.units",
                "must be one of %s; saw %r" % (list(UNIT_ENUM), units),
            )

        tol = plan.get("tolerance")
        if tol is not None:
            if not self._is_number(tol):
                self.err("$.tolerance", "must be a number; saw %r" % (tol,))
            elif tol <= 0:
                self.err(
                    "$.tolerance",
                    "must be > 0 (absolute model tolerance in %s); saw %s"
                    % (self.units, tol),
                )

        world_frame = plan.get("world_frame", "WorldXY")
        if not isinstance(world_frame, str) or not world_frame.strip():
            self.err("$.world_frame", "must be a non-empty string; saw %r" % (world_frame,))
            world_frame = "WorldXY"

        self._validate_scale(plan.get("scale"), world_frame)
        self._validate_symmetry(plan.get("symmetry"), world_frame)

        parts = plan.get("parts")
        part_ids = set()
        if not isinstance(parts, list) or len(parts) < 1:
            self.err("$.parts", "must be a non-empty array of parts")
            parts = []
        else:
            for i, part in enumerate(parts):
                pid = self._validate_part(i, part, world_frame)
                if pid is not None:
                    if pid in part_ids:
                        self.err("$.parts[%d]" % i, "duplicate part id '%s'" % pid)
                    part_ids.add(pid)

        self._validate_boolean_plan(plan.get("boolean_plan"), part_ids)
        self._validate_verify(plan.get("verify"), plan)
        self._bbox_sanity(parts, plan, world_frame)

    # -- scale -------------------------------------------------------------- #

    def _validate_scale(self, scale, world_frame):
        if scale is None:
            self.err("$.scale", "scale block is required (value_source/overall_height_mm/confidence)")
            return
        if not isinstance(scale, dict):
            self.err("$.scale", "scale must be an object")
            return
        vs = scale.get("value_source")
        if vs is None:
            self.err("$.scale.value_source", "missing; must be one of %s" % list(SCALE_SOURCE_ENUM))
        elif vs not in SCALE_SOURCE_ENUM:
            self.err("$.scale.value_source", "must be one of %s; saw %r" % (list(SCALE_SOURCE_ENUM), vs))

        oh = scale.get("overall_height_mm")
        if oh is None:
            self.err("$.scale.overall_height_mm", "missing; required (always in mm)")
        elif self._is_number(oh):
            if oh <= 0:
                self.err("$.scale.overall_height_mm", "must be > 0 mm; saw %s" % oh)
        elif isinstance(oh, list):
            if len(oh) != 2 or not all(self._is_number(x) for x in oh):
                self.err(
                    "$.scale.overall_height_mm",
                    "range must be [min,max] of two numbers; saw %r" % (oh,),
                )
            else:
                lo, hi = oh
                if lo <= 0 or hi <= 0:
                    self.err("$.scale.overall_height_mm", "range values must be > 0 mm; saw %r" % (oh,))
                if hi < lo:
                    self.err("$.scale.overall_height_mm", "range max < min; saw [%s,%s]" % (lo, hi))
        else:
            self.err("$.scale.overall_height_mm", "must be a number or [min,max] array; saw %r" % (oh,))

        conf = scale.get("confidence")
        if conf is None:
            self.err("$.scale.confidence", "missing; must be one of %s" % list(CONFIDENCE_ENUM))
        elif conf not in CONFIDENCE_ENUM:
            self.err("$.scale.confidence", "must be one of %s; saw %r" % (list(CONFIDENCE_ENUM), conf))

        # text pipeline expectation: stated scale should be high confidence.
        if vs == "stated" and conf in ("low", "medium"):
            self.warn(
                "$.scale",
                "value_source='stated' but confidence='%s'; text pipeline normally"
                " verifies absolutes at high confidence" % conf,
            )
        if vs == "metrology_assumption" and "assumption" not in scale:
            self.warn("$.scale", "value_source='metrology_assumption' but no 'assumption' string given")

    # -- symmetry ----------------------------------------------------------- #

    def _validate_symmetry(self, symmetry, world_frame):
        if symmetry is None:
            return
        if not isinstance(symmetry, list):
            self.err("$.symmetry", "must be an array")
            return
        for i, s in enumerate(symmetry):
            path = "$.symmetry[%d]" % i
            if not isinstance(s, dict):
                self.err(path, "symmetry entry must be an object")
                continue
            st = s.get("type")
            if st not in SYMMETRY_ENUM:
                self.err(path + ".type", "must be one of %s; saw %r" % (list(SYMMETRY_ENUM), st))
            if "origin" not in s:
                self.err(path + ".origin", "missing; symmetry origin point3d required")
            elif not self._is_point3d(s.get("origin")):
                self.err(path + ".origin", "must be a [x,y,z] point3d; saw %r" % (s.get("origin"),))
            if st == "mirror" and not s.get("plane"):
                self.err(path + ".plane", "mirror symmetry requires a 'plane'")
            if st == "rotational" and not s.get("axis"):
                self.err(path + ".axis", "rotational symmetry requires an 'axis'")
            cnt = s.get("count")
            if cnt is not None and (not isinstance(cnt, int) or isinstance(cnt, bool) or cnt < 2):
                self.err(path + ".count", "must be an integer >= 2; saw %r" % (cnt,))

    # -- parts -------------------------------------------------------------- #

    def _validate_part(self, idx, part, world_frame):
        path = "$.parts[%d]" % idx
        if not isinstance(part, dict):
            self.err(path, "part must be an object")
            return None

        pid = part.get("id")
        if not isinstance(pid, str) or not pid.strip():
            self.err(path + ".id", "missing/empty part id (becomes UserString part_id)")
            pid = None
        label = pid if pid else ("parts[%d]" % idx)

        has_prim = "primitive" in part
        has_op = "operation" in part
        if has_prim and has_op:
            self.err(path, "part '%s' has BOTH 'primitive' and 'operation'; exactly one allowed" % label)
        if not has_prim and not has_op:
            self.err(path, "part '%s' has NEITHER 'primitive' nor 'operation'; exactly one required" % label)

        if has_prim:
            self._validate_primitive(path, label, part, world_frame)
        if has_op:
            self._validate_operation(path, label, part, world_frame)

        self._validate_relations(path, label, part)
        return pid

    def _validate_primitive(self, path, label, part, world_frame):
        prim = part.get("primitive")
        if prim not in PRIMITIVE_ENUM:
            self.err(path + ".primitive", "must be one of %s; saw %r" % (list(PRIMITIVE_ENUM), prim))
            return

        frame = part.get("frame")
        if prim == "interpolated_curve":
            cps = part.get("control_points")
            if not isinstance(cps, list) or len(cps) < 2:
                self.err(
                    path + ".control_points",
                    "interpolated_curve needs >= 2 control points; saw %r" % (cps,),
                )
            else:
                for j, cp in enumerate(cps):
                    if not self._is_point3d(cp):
                        self.err(
                            path + ".control_points[%d]" % j,
                            "must be a [x,y,z] point3d in %s; saw %r" % (self.units, cp),
                        )
            return

        # solid primitives must carry dims with the right keys + units/frame.
        dims = part.get("dims")
        if not isinstance(dims, dict) or not dims:
            self.err(
                path + ".dims",
                "primitive '%s' (id=%s) requires dims %s in %s"
                % (prim, label, list(DIMS_KEYS.get(prim, ())), self.units),
            )
            return
        for key in DIMS_KEYS.get(prim, ()):
            if key not in dims:
                self.err(
                    path + ".dims",
                    "primitive '%s' (id=%s) missing dims['%s'] (unit=%s, frame=%s)"
                    % (prim, label, key, self.units, self._frame_desc(frame)),
                )
        if frame is None:
            self.err(
                path,
                "primitive '%s' (id=%s) has no 'frame'; every IR number must carry a"
                " frame (saw dims=%s, frame=?)" % (prim, label, dims),
            )
        self._check_dims_units(path, dims, frame, world_frame)

    def _validate_operation(self, path, label, part, world_frame):
        op = part.get("operation")
        if op not in OPERATION_ENUM:
            self.err(path + ".operation", "must be one of %s; saw %r" % (list(OPERATION_ENUM), op))
            return

        if op == "revolve":
            self._validate_revolve(path, label, part)
        elif op == "loft":
            secs = part.get("sections")
            if not isinstance(secs, list) or len(secs) < 2:
                self.err(path + ".sections", "loft requires >= 2 section part ids; saw %r" % (secs,))
        elif op == "sweep1":
            if not part.get("rail"):
                self.err(path + ".rail", "sweep1 requires a 'rail' part id")
            secs = part.get("sections")
            if not isinstance(secs, list) or len(secs) < 2:
                self.err(path + ".sections", "sweep1 requires >= 2 section part ids; saw %r" % (secs,))
        elif op == "extrude":
            inp = part.get("inputs")
            if not isinstance(inp, list) or len(inp) < 1:
                self.err(path + ".inputs", "extrude requires >= 1 input part id; saw %r" % (inp,))
            h = part.get("height")
            if not self._is_number(h):
                self.err(path + ".height", "extrude requires numeric 'height' in %s; saw %r" % (self.units, h))
            elif h <= 0:
                self.err(path + ".height", "extrude height must be > 0 %s; saw %s" % (self.units, h))
        elif op == "shell":
            inp = part.get("inputs")
            if not isinstance(inp, list) or len(inp) < 1:
                self.err(path + ".inputs", "shell requires >= 1 input part id; saw %r" % (inp,))
            t = part.get("thickness")
            if not self._is_number(t):
                self.err(path + ".thickness", "shell requires numeric 'thickness' in %s; saw %r" % (self.units, t))
            elif t <= 0:
                self.err(path + ".thickness", "shell thickness must be > 0 %s; saw %s" % (self.units, t))
        elif op == "boolean":
            bop = part.get("op")
            if bop not in BOOLEAN_OP_ENUM:
                self.err(path + ".op", "boolean requires op in %s; saw %r" % (list(BOOLEAN_OP_ENUM), bop))
            inp = part.get("inputs")
            if not isinstance(inp, list) or len(inp) < 1:
                self.err(path + ".inputs", "boolean requires >= 1 input part id; saw %r" % (inp,))

    def _validate_revolve(self, path, label, part):
        gen = part.get("generatrix")
        if not isinstance(gen, str) or not gen.strip():
            self.err(path + ".generatrix", "revolve requires a 'generatrix' part id")
        axis = part.get("axis")
        if not isinstance(axis, dict) or "line" not in axis:
            self.err(path + ".axis", "revolve requires axis {line:[[x,y,z],[x,y,z]]}")
            return
        line = axis.get("line")
        if (
            not isinstance(line, list)
            or len(line) != 2
            or not all(self._is_point3d(p) for p in line)
        ):
            self.err(path + ".axis.line", "axis line must be two point3d; saw %r" % (line,))
            return

        # C6: profile must start AND end ON the axis (or be closed). We resolve
        # the generatrix part's control_points and test endpoint-on-axis.
        gen_part = self._find_part(gen)
        if gen_part is None:
            self.err(path + ".generatrix", "generatrix id '%s' not found among parts" % gen)
            return
        cps = gen_part.get("control_points")
        if not isinstance(cps, list) or len(cps) < 2:
            self.warn(
                path + ".generatrix",
                "generatrix '%s' has no control_points to test axis-touch (C6); "
                "ensure it is closed or endpoints lie on the axis" % gen,
            )
            return
        start, end = cps[0], cps[-1]
        closed = self._points_equal(start, end)
        p0, p1 = line[0], line[1]
        tol = self._axis_tol()
        d_start = self._point_to_line_dist(start, p0, p1)
        d_end = self._point_to_line_dist(end, p0, p1)
        if not closed:
            if d_start > tol:
                self.err(
                    path + ".generatrix",
                    "C6 revolve profile START not on axis: distance %.4f %s > tol %.4f "
                    "(start=%s, axis=%s)" % (d_start, self.units, tol, start, line),
                )
            if d_end > tol:
                self.err(
                    path + ".generatrix",
                    "C6 revolve profile END not on axis: distance %.4f %s > tol %.4f "
                    "(end=%s, axis=%s)" % (d_end, self.units, tol, end, line),
                )

    def _validate_relations(self, path, label, part):
        rels = part.get("relations")
        if rels is None:
            return
        if not isinstance(rels, list):
            self.err(path + ".relations", "relations must be an array")
            return
        for j, r in enumerate(rels):
            rpath = path + ".relations[%d]" % j
            if not isinstance(r, dict):
                self.err(rpath, "relation must be an object")
                continue
            rt = r.get("type")
            if rt not in RELATION_ENUM:
                self.err(rpath + ".type", "must be one of %s; saw %r" % (list(RELATION_ENUM), rt))
            if rt == "interpenetrate":
                # C3: union joins MUST declare a penetration depth (0,2] mm-ish.
                if "to" not in r:
                    self.err(rpath, "interpenetrate relation requires 'to' (the mating part id)")
                pen = r.get("penetration")
                if pen is None:
                    self.err(
                        rpath + ".penetration",
                        "C3 interpenetrate join on '%s' missing 'penetration' depth; "
                        "union mating parts must overlap 0.5-2 %s" % (label, self.units),
                    )
                elif not self._is_number(pen):
                    self.err(rpath + ".penetration", "penetration must be numeric; saw %r" % (pen,))
                elif pen <= 0:
                    self.err(
                        rpath + ".penetration",
                        "C3 penetration must be > 0 %s (coincident/coplanar contact is "
                        "degenerate); saw %s" % (self.units, pen),
                    )
                else:
                    pen_mm = pen * MM_PER_UNIT.get(self.units, 1.0)
                    if pen_mm < 0.5 or pen_mm > 2.0:
                        self.warn(
                            rpath + ".penetration",
                            "C3 penetration %.3f %s = %.3f mm is outside the recommended "
                            "0.5-2 mm union overlap" % (pen, self.units, pen_mm),
                        )

    # -- boolean_plan ------------------------------------------------------- #

    def _validate_boolean_plan(self, bplan, part_ids):
        if bplan is None:
            return
        if not isinstance(bplan, list):
            self.err("$.boolean_plan", "must be an array of steps")
            return
        for i, step in enumerate(bplan):
            path = "$.boolean_plan[%d]" % i
            if not isinstance(step, dict):
                self.err(path, "boolean step must be an object")
                continue
            op = step.get("op")
            if op not in BOOLEAN_OP_ENUM:
                self.err(path + ".op", "must be one of %s; saw %r" % (list(BOOLEAN_OP_ENUM), op))
            inputs = step.get("inputs")
            if not isinstance(inputs, list) or len(inputs) < 2:
                self.err(path + ".inputs", "boolean step needs >= 2 input ids; saw %r" % (inputs,))
            else:
                for iid in inputs:
                    if iid not in part_ids:
                        self.err(path + ".inputs", "references unknown part id '%s'" % iid)
            if "result" not in step:
                self.err(path + ".result", "boolean step requires a 'result' part id")

            # C3 cross-check: union inputs should declare interpenetration.
            if op == "union" and isinstance(inputs, list):
                self._check_union_penetration(path, inputs)

    def _check_union_penetration(self, path, inputs):
        """For a union step, at least one of the mating inputs should carry an
        interpenetrate relation referencing another input (correction C3)."""
        declared = False
        input_set = set(inputs)
        for iid in inputs:
            p = self._find_part(iid)
            if not p:
                continue
            for r in p.get("relations", []) or []:
                if isinstance(r, dict) and r.get("type") == "interpenetrate":
                    to = r.get("to")
                    if to in input_set or to is None:
                        declared = True
        if not declared:
            self.err(
                path,
                "C3 union of %s declares no 'interpenetrate' relation among its inputs; "
                "mating parts must overlap 0.5-2mm or the union fails partially" % (inputs,),
            )

    # -- verify ------------------------------------------------------------- #

    def _validate_verify(self, verify, plan):
        if verify is None:
            return
        if not isinstance(verify, dict):
            self.err("$.verify", "must be an object")
            return
        for key in ("binary_questions",):
            arr = verify.get(key)
            if arr is not None and not (isinstance(arr, list) and all(isinstance(s, str) for s in arr)):
                self.err("$.verify." + key, "must be an array of strings; saw %r" % (arr,))
        for key in ("numeric_checks", "ratio_checks"):
            arr = verify.get(key)
            if arr is None:
                continue
            if not isinstance(arr, list):
                self.err("$.verify." + key, "must be an array of checks")
                continue
            for i, chk in enumerate(arr):
                cpath = "$.verify.%s[%d]" % (key, i)
                if not isinstance(chk, dict):
                    self.err(cpath, "check must be an object")
                    continue
                for req in ("metric", "expect", "tol"):
                    if req not in chk:
                        self.err(cpath, "missing '%s' (metric/expect/tol all required)" % req)
                if "metric" in chk and not isinstance(chk["metric"], str):
                    self.err(cpath + ".metric", "must be a string; saw %r" % (chk["metric"],))
                if "expect" in chk and not self._is_number(chk["expect"]):
                    self.err(cpath + ".expect", "must be a number; saw %r" % (chk["expect"],))
                if "tol" in chk:
                    if not self._is_number(chk["tol"]):
                        self.err(cpath + ".tol", "must be a number; saw %r" % (chk["tol"],))
                    elif chk["tol"] < 0:
                        self.err(cpath + ".tol", "must be >= 0; saw %s" % chk["tol"])

        # C5 routing sanity: low/medium confidence must NOT fire numeric repairs.
        scale = plan.get("scale") or {}
        conf = scale.get("confidence")
        ncs = verify.get("numeric_checks")
        if conf in ("low", "medium") and isinstance(ncs, list) and ncs:
            self.warn(
                "$.verify.numeric_checks",
                "scale.confidence='%s' (image-style): C5 says fire repairs on ratio_checks "
                "ONLY; numeric_checks present will be measured but must not drive repair" % conf,
            )

    # -- bbox sanity -------------------------------------------------------- #

    def _bbox_sanity(self, parts, plan, world_frame):
        """Cross-check the declared overall_height_mm against the realizable
        z-extent implied by primitive frames + dims. Coarse but catches gross
        unit mistakes (e.g. a 900mm chair built from 9mm parts)."""
        scale = plan.get("scale") or {}
        oh = scale.get("overall_height_mm")
        if oh is None:
            return
        if isinstance(oh, list):
            if len(oh) != 2:
                return
            oh_lo_mm, oh_hi_mm = float(oh[0]), float(oh[1])
        elif self._is_number(oh):
            oh_lo_mm = oh_hi_mm = float(oh)
        else:
            return

        mm = MM_PER_UNIT.get(self.units, 1.0)
        zmax = None
        zmin = None
        for part in parts:
            if not isinstance(part, dict) or "primitive" not in part:
                continue
            frame = part.get("frame") or {}
            origin = frame.get("origin")
            if not self._is_point3d(origin):
                continue
            oz = origin[2]
            prim = part.get("primitive")
            dims = part.get("dims") or {}
            # crude half/full extents along z about the frame origin.
            if prim == "box":
                dz = dims.get("z", 0) or 0
                top, bot = oz + dz / 2.0, oz - dz / 2.0
            elif prim in ("cylinder", "cone"):
                dz = dims.get("height", 0) or 0
                top, bot = oz + dz, oz  # base at origin, grows +z
            elif prim == "sphere":
                r = dims.get("radius", 0) or 0
                top, bot = oz + r, oz - r
            else:
                continue
            zmax = top if zmax is None else max(zmax, top)
            zmin = bot if zmin is None else min(zmin, bot)

        if zmax is None or zmin is None:
            return
        model_h_mm = (zmax - zmin) * mm
        # generous +/-50% band; this is a gross-mistake guard, not a precise check.
        if model_h_mm < 0.5 * oh_lo_mm or model_h_mm > 1.5 * oh_hi_mm:
            self.err(
                "$.parts",
                "bbox sanity: primitive z-extent %.1f mm is far from declared "
                "overall_height_mm %s mm (check units/frames)"
                % (model_h_mm, oh if isinstance(oh, (int, float)) else "[%s,%s]" % (oh_lo_mm, oh_hi_mm)),
            )
        else:
            self.warn(
                "$.parts",
                "bbox sanity: primitive z-extent %.1f mm vs declared %s mm (within band)"
                % (model_h_mm, oh if isinstance(oh, (int, float)) else "[%s,%s]" % (oh_lo_mm, oh_hi_mm)),
            )

    # -- geometry helpers --------------------------------------------------- #

    def _axis_tol(self):
        tol = self.plan.get("tolerance")
        if self._is_number(tol) and tol > 0:
            # be a little generous for endpoint-on-axis testing in the IR.
            return max(tol * 10.0, 1e-6)
        return 1e-3

    def _points_equal(self, a, b):
        if not (self._is_point3d(a) and self._is_point3d(b)):
            return False
        return all(abs(a[i] - b[i]) <= self._axis_tol() for i in range(3))

    def _point_to_line_dist(self, p, a, b):
        """Perpendicular distance from point p to the infinite line a->b."""
        ax, ay, az = a
        bx, by, bz = b
        px, py, pz = p
        dx, dy, dz = bx - ax, by - ay, bz - az
        wx, wy, wz = px - ax, py - ay, pz - az
        seg2 = dx * dx + dy * dy + dz * dz
        if seg2 <= 0.0:
            # degenerate axis; fall back to point-to-point distance.
            return math.sqrt(wx * wx + wy * wy + wz * wz)
        # cross(w, d) magnitude / |d|
        cx = wy * dz - wz * dy
        cy = wz * dx - wx * dz
        cz = wx * dy - wy * dx
        cross_mag = math.sqrt(cx * cx + cy * cy + cz * cz)
        return cross_mag / math.sqrt(seg2)

    def _find_part(self, pid):
        for p in self.plan.get("parts", []):
            if isinstance(p, dict) and p.get("id") == pid:
                return p
        return None


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def main(argv):
    parser = argparse.ArgumentParser(
        description="Validate a Rhino build-plan IR (stdlib-only)."
    )
    parser.add_argument("plan", help="path to the build-plan JSON file")
    parser.add_argument(
        "--schema",
        default=None,
        help="optional path to build-plan.schema.json (only used to confirm units enum)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress WARN lines; print only ERROR lines",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.plan):
        sys.stderr.write("ERROR  $: plan file not found: %s\n" % args.plan)
        return 2
    try:
        plan = load_json(args.plan)
    except ValueError as exc:
        sys.stderr.write("ERROR  $: invalid JSON in %s: %s\n" % (args.plan, exc))
        return 2

    units = plan.get("units") if isinstance(plan, dict) else None
    v = Validator(plan, units if units in UNIT_ENUM else "mm")
    v.validate()

    if not args.quiet:
        for w in v.warnings:
            sys.stdout.write(w + "\n")
    for e in v.errors:
        sys.stderr.write(e + "\n")

    if v.errors:
        sys.stderr.write(
            "FAILED %s: %d error(s), %d warning(s)\n"
            % (args.plan, len(v.errors), len(v.warnings))
        )
        return 1
    sys.stdout.write(
        "OK     %s: valid build-plan (%d part(s), %d warning(s))\n"
        % (args.plan, len(plan.get("parts", [])), len(v.warnings))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
