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
  - relational-IR constructs (the PREVENT leg of conventions §13/C9):
      * value_ref ({param} / {op,args} / {part,of,at}) shape + DAG acyclicity
      * per-part 'support' publication (plane_z/top_z/base_z/helix_z/z_at_angle)
      * per-part 'array' (radial/helical/linear) generating rule
      * the extended relation enum (lands_on/meets/spans/spans_between)
      * the COMPLETENESS WARN: a non-floating assembly part with no declared
        contact relation (floating:true suppresses it)

--resolve mode (PREVENT, conventions §13):
  Resolves the relational constructs into a PLAIN-LITERAL IR before emit:
  topo-sorts the value_ref DAG (a cycle is an ERROR), folds {op,args}
  arithmetic, substitutes {param}, reads attach coordinates published by
  another part's 'support' ({part,of,at} -> e.g. baluster.top_z :=
  support(rail).z_at_angle(this_angle)), and expands each 'array' part into
  its concrete 0-based instances ('<id>#<i>'). No eval, no general solver:
  just arithmetic + attach-point resolution. The resolved IR is emitted to
  stdout (or --out) and STILL validates with this same validator.

Usage:
    python3 validate_plan.py <plan.json>
    python3 validate_plan.py <plan.json> --schema ../../shared/build-plan.schema.json
    python3 validate_plan.py <plan.json> --resolve            # literal IR -> stdout
    python3 validate_plan.py <plan.json> --resolve --out resolved.json

Exit code 0 => valid. Non-zero => one or more errors (printed to stderr).
"""

import argparse
import json
import math
import os
import re
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
    # v2 connectivity hardening (conventions §13/C9):
    "lands_on",
    "meets",
    "spans",
    "spans_between",
)

# Relation kinds that constitute a MEASURED CONTACT for the completeness clause
# (conventions §13 ENFORCE). symmetric_about / child_of are logical, not measured.
CONTACT_RELATION_ENUM = (
    "coincident",
    "on_top_of",
    "interpenetrate",
    "lands_on",
    "meets",
    "spans",
    "spans_between",
)

# value_ref.of coordinate selectors (mirror schema $defs/value_ref.of).
VALUE_REF_OF_ENUM = ("z", "top_z", "base_z", "z_at_angle", "centroid_z")

# part.support.kind (mirror schema $defs/support.kind).
SUPPORT_KIND_ENUM = ("plane_z", "top_z", "base_z", "helix_z", "z_at_angle")

# part.array.kind (mirror schema $defs/array.kind).
ARRAY_KIND_ENUM = ("radial", "helical", "linear")

# at_surface enum (mirror schema relation.at_surface).
AT_SURFACE_ENUM = ("top", "bottom", "nearest", "centerline", "realized")

# Reserved params injected by the resolver INSIDE an 'array' part during
# instance expansion: the per-instance index and array angle (degrees). A
# value_ref {param:'__angle__'} inside a helical array reads THIS member's angle
# so e.g. a baluster height resolves to rail.z_at_angle(__angle__) - floor. They
# are scoped to array expansion and never appear in the emitted literal IR.
RESERVED_PARAMS = ("__i__", "__angle__")
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

    # -- value_ref / numeric (relational-IR, conventions §13 PREVENT) ------- #

    def _is_value_ref(self, v):
        """A value_ref is an object carrying exactly one of {param},{op,args},
        {part,of}. We only test the discriminator here; full shape validation
        (and cycle detection) is done in _validate_value_ref."""
        if not isinstance(v, dict):
            return False
        return ("param" in v) or ("op" in v) or ("part" in v)

    def _is_numeric(self, v):
        """schema $defs/numeric: a plain number OR a value_ref object. Use this
        wherever an IR scalar may be derived/published (dims, height, thickness,
        penetration, tol, support.value, array steps)."""
        return self._is_number(v) or self._is_value_ref(v)

    def _validate_value_ref(self, path, v):
        """Structurally validate ONE value_ref object (recursively into nested
        args). Does NOT resolve; --resolve does that with a topo-sort. Returns
        nothing; appends verbose errors."""
        if not isinstance(v, dict):
            self.err(path, "value_ref must be an object; saw %r" % (v,))
            return
        keys = set(v.keys())
        discrims = [k for k in ("param", "op", "part") if k in v]
        if len(discrims) != 1:
            self.err(
                path,
                "value_ref must carry EXACTLY ONE of {param},{op,args},{part,of}; "
                "saw discriminators %s (keys=%s)" % (discrims, sorted(keys)),
            )
            return
        if "param" in v:
            name = v.get("param")
            if not isinstance(name, str) or not name.strip():
                self.err(path + ".param", "must be a non-empty string; saw %r" % (name,))
            elif name in RESERVED_PARAMS:
                # __i__ / __angle__ are injected only inside an 'array' part at
                # resolve time; flag them if used outside one.
                if not self._in_array_part(path):
                    self.err(
                        path + ".param",
                        "reserved param '%s' is only available inside an 'array' part "
                        "during expansion; used outside an array" % name,
                    )
            else:
                params = self.plan.get("params") or {}
                if not isinstance(params, dict) or name not in params:
                    self.err(
                        path + ".param",
                        "references params['%s'] but it is not defined in top-level "
                        "params" % name,
                    )
            for stray in ("op", "args", "part", "of", "at"):
                if stray in v:
                    self.err(path, "value_ref {param} must not also carry '%s'" % stray)
        elif "op" in v:
            op = v.get("op")
            if op not in ("+", "-", "*", "/"):
                self.err(path + ".op", "must be one of ['+','-','*','/']; saw %r" % (op,))
            args = v.get("args")
            if not isinstance(args, list) or len(args) < 1:
                self.err(path + ".args", "op requires a non-empty 'args' array; saw %r" % (args,))
            else:
                for i, a in enumerate(args):
                    apath = path + ".args[%d]" % i
                    if self._is_value_ref(a):
                        self._validate_value_ref(apath, a)
                    elif not self._is_number(a):
                        self.err(apath, "arg must be a number or value_ref; saw %r" % (a,))
            for stray in ("param", "part", "of", "at"):
                if stray in v:
                    self.err(path, "value_ref {op} must not also carry '%s'" % stray)
        elif "part" in v:
            pid = v.get("part")
            if not isinstance(pid, str) or not pid.strip():
                self.err(path + ".part", "must be a non-empty part id; saw %r" % (pid,))
            elif self._find_part(pid) is None:
                self.err(path + ".part", "references unknown part id '%s'" % pid)
            of = v.get("of")
            if of not in VALUE_REF_OF_ENUM:
                self.err(path + ".of", "must be one of %s; saw %r" % (list(VALUE_REF_OF_ENUM), of))
            if of == "z_at_angle":
                at = v.get("at")
                if at is None:
                    self.err(
                        path + ".at",
                        "of='z_at_angle' requires 'at' (angle in degrees, number or value_ref)",
                    )
                elif self._is_value_ref(at):
                    self._validate_value_ref(path + ".at", at)
                elif not self._is_number(at):
                    self.err(path + ".at", "must be a number or value_ref; saw %r" % (at,))
            for stray in ("param", "op", "args"):
                if stray in v:
                    self.err(path, "value_ref {part} must not also carry '%s'" % stray)

    def _in_array_part(self, path):
        """True if the value_ref at 'path' lives inside a part that declares an
        'array' (so reserved params __i__/__angle__ are legitimately in scope).
        Parses the leading '$.parts[N]' segment of the diagnostic path."""
        m = re.match(r"^\$\.parts\[(\d+)\]", path or "")
        if not m:
            return False
        idx = int(m.group(1))
        parts = self.plan.get("parts")
        if isinstance(parts, list) and 0 <= idx < len(parts):
            p = parts[idx]
            return isinstance(p, dict) and isinstance(p.get("array"), dict)
        return False

    def _check_numeric_field(self, path, v, must_be_positive=False, label=None):
        """Validate a field declared as schema 'numeric' (number OR value_ref).
        Only LITERAL numbers can be sign-checked here; a value_ref is checked
        structurally (its sign is only known after --resolve)."""
        if self._is_value_ref(v):
            self._validate_value_ref(path, v)
            return
        if not self._is_number(v):
            self.err(path, "%smust be a number or value_ref; saw %r" % (
                (label + " ") if label else "", v))
            return
        if must_be_positive and v <= 0:
            self.err(path, "%smust be > 0 %s; saw %s" % (
                (label + " ") if label else "", self.units, v))

    def _frame_is_resolvable(self, frame, world_frame):
        """A frame resolves if it has an explicit origin (always) and, when a
        named plane is given, that plane is one the executor knows."""
        if not isinstance(frame, dict):
            return False, "frame is not an object"
        if "origin" not in frame:
            return False, "frame has no 'origin' (point3d required)"
        origin = frame.get("origin")
        # frame.origin is a point3d_expr: each coordinate is a number OR a
        # value_ref (an attach origin published by another part's support, §13).
        if not self._is_point3d_expr(origin):
            return False, "frame.origin is not a [x,y,z] point3d_expr (saw %r)" % (origin,)
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

    def _is_point3d_expr(self, v):
        """schema $defs/point3d_expr: a [x,y,z] triple where each coordinate is
        a numeric (a literal number OR a value_ref). Plain literal triples (the
        existing examples) remain valid."""
        return (
            isinstance(v, list)
            and len(v) == 3
            and all(self._is_numeric(x) for x in v)
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
            dpath = path + ".dims." + key
            if self._is_value_ref(val):
                # A derived/published dimension (§13 PREVENT). Its sign is only
                # known after --resolve; validate the value_ref shape here.
                self._validate_value_ref(dpath, val)
                if not resolvable:
                    self.err(
                        dpath,
                        "missing unit context / frame; saw value_ref, unit=%s, frame=%s"
                        % (self.units, frame_tag if frame is not None else "?"),
                    )
                continue
            if not self._is_number(val):
                self.err(
                    dpath,
                    "dimension is not numeric; saw %r (unit=%s, frame=%s)"
                    % (val, self.units, frame_tag),
                )
                continue
            if val <= 0:
                self.err(
                    dpath,
                    "dimension must be > 0; saw %s %s (frame=%s)"
                    % (val, self.units, frame_tag),
                )
            if not resolvable:
                # This is the headline verbose message the task asks for.
                self.err(
                    dpath,
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
        self._completeness_warn(parts)

    # -- completeness clause (ENFORCE, conventions §13/C9) ------------------ #

    def _completeness_warn(self, parts):
        """The completeness clause, surfaced at IR time as a WARN (the FAIL form
        is the in-Rhino connectivity sweep at Phase 5/6, conventions §13). Every
        NON-floating part that PARTICIPATES in an assembly must declare at least
        one CONTACT relation; a floating:true part is exempt (F). 'participates
        in an assembly' = the part is consumed as an input/section/generatrix/
        rail by some operation OR boolean_plan step (i.e. it is a physical
        building block, not a final welded result)."""
        if not isinstance(parts, list):
            return
        consumed = self._assembly_inputs(parts)
        # A part is COVERED if it declares a contact OR another part declares a
        # contact TO it (the base/floor that everything attaches to is covered by
        # the attachers' relations, not by owning one itself).
        contacted_to = set()
        for part in parts:
            if not isinstance(part, dict):
                continue
            for r in (part.get("relations") or []):
                if isinstance(r, dict) and r.get("type") in CONTACT_RELATION_ENUM:
                    tgt = r.get("to")
                    if isinstance(tgt, str):
                        contacted_to.add(tgt)
        for part in parts:
            if not isinstance(part, dict):
                continue
            pid = part.get("id")
            if not isinstance(pid, str) or pid not in consumed:
                continue
            if part.get("floating") is True:
                continue  # F opt-out
            if "operation" in part:
                continue  # operation results are not contact leaves themselves
            rels = part.get("relations") or []
            has_contact = any(
                isinstance(r, dict) and r.get("type") in CONTACT_RELATION_ENUM
                for r in rels
            )
            if pid in contacted_to:
                has_contact = True  # covered by an attacher's contact TO this part
            if not has_contact:
                self.warn(
                    "$.parts[id=%s]" % pid,
                    "non-floating assembly part '%s' participates in an assembly but "
                    "declares NO contact relation (%s); §13/C9 completeness: it will be "
                    "UNCOVERED=FAIL in the connectivity sweep. Add a contact relation "
                    "(on_top_of/lands_on/meets/coincident/spans/spans_between/interpenetrate) "
                    "or mark floating:true." % (pid, list(CONTACT_RELATION_ENUM)),
                )

    def _assembly_inputs(self, parts):
        """Set of part ids consumed as a physical MATING SOLID in an assembly —
        i.e. an input to a boolean op (operation='boolean') or a boolean_plan
        step. These are the parts where a missing contact ORPHANS a member (the
        connectivity failure §13 was written for). Geometry-only inputs
        (generatrix/rail/sections of loft/sweep/revolve, and shell/extrude
        inputs) build a SINGLE solid and are NOT assembly contacts, so they are
        deliberately excluded from the completeness clause."""
        consumed = set()

        def add(v):
            if isinstance(v, str):
                consumed.add(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, str):
                        consumed.add(x)

        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("operation") == "boolean":
                add(part.get("inputs"))
        for step in (self.plan.get("boolean_plan") or []):
            if isinstance(step, dict):
                add(step.get("inputs"))
        return consumed

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
        self._validate_support(path, label, part)
        self._validate_array(path, label, part)

        fl = part.get("floating")
        if fl is not None and not isinstance(fl, bool):
            self.err(path + ".floating", "must be a boolean; saw %r" % (fl,))
        return pid

    # -- support (PREVENT publication, conventions §13) --------------------- #

    def _validate_support(self, path, label, part):
        sup = part.get("support")
        if sup is None:
            return
        spath = path + ".support"
        if not isinstance(sup, dict):
            self.err(spath, "support must be an object")
            return
        kind = sup.get("kind")
        if kind not in SUPPORT_KIND_ENUM:
            self.err(spath + ".kind", "must be one of %s; saw %r" % (list(SUPPORT_KIND_ENUM), kind))
        if kind in ("plane_z", "top_z", "base_z"):
            val = sup.get("value")
            if val is None:
                self.err(
                    spath + ".value",
                    "support kind '%s' (id=%s) publishes a single level and requires "
                    "'value' (numeric)" % (kind, label),
                )
            else:
                self._check_numeric_field(spath + ".value", val, label="support value")
            if "helix" in sup:
                self.warn(spath, "support kind '%s' ignores 'helix'" % kind)
        elif kind in ("helix_z", "z_at_angle"):
            helix = sup.get("helix")
            if not isinstance(helix, dict):
                self.err(
                    spath + ".helix",
                    "support kind '%s' (id=%s) publishes a Z-law and requires "
                    "'helix' {base_z, pitch, radius?, start_angle?}" % (kind, label),
                )
            else:
                for req in ("base_z", "pitch"):
                    if req not in helix:
                        self.err(spath + ".helix", "missing required '%s'" % req)
                    else:
                        self._check_numeric_field(spath + ".helix." + req, helix[req], label=req)
                for opt in ("radius", "start_angle"):
                    if opt in helix:
                        self._check_numeric_field(spath + ".helix." + opt, helix[opt], label=opt)

    # -- array (family generating rule, conventions §13 B2) ---------------- #

    def _validate_array(self, path, label, part):
        arr = part.get("array")
        if arr is None:
            return
        apath = path + ".array"
        if not isinstance(arr, dict):
            self.err(apath, "array must be an object")
            return
        kind = arr.get("kind")
        if kind not in ARRAY_KIND_ENUM:
            self.err(apath + ".kind", "must be one of %s; saw %r" % (list(ARRAY_KIND_ENUM), kind))
        count = arr.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            self.err(apath + ".count", "array requires integer count >= 1; saw %r" % (count,))
        for fld in ("radius", "angle_step", "z_step", "pitch", "start_angle"):
            if fld in arr:
                self._check_numeric_field(apath + "." + fld, arr[fld], label=fld)
        if "step" in arr:
            step = arr["step"]
            if not self._is_point3d_expr(step):
                self.err(apath + ".step", "linear 'step' must be a [x,y,z] point3d_expr; saw %r" % (step,))
            else:
                for i, c in enumerate(step):
                    if self._is_value_ref(c):
                        self._validate_value_ref(apath + ".step[%d]" % i, c)
        # rule-completeness per kind (what the resolver needs to expand it).
        if kind == "radial":
            if "angle_step" not in arr:
                self.err(apath, "radial array requires 'angle_step' (degrees per instance)")
        elif kind == "helical":
            if "angle_step" not in arr:
                self.err(apath, "helical array requires 'angle_step' (degrees per instance)")
            if "z_step" not in arr and "pitch" not in arr:
                self.err(apath, "helical array requires 'z_step' OR 'pitch' for the Z rise")
        elif kind == "linear":
            if "step" not in arr:
                self.err(apath, "linear array requires 'step' (translation vector point3d_expr)")

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
        else:
            self._validate_frame_exprs(path + ".frame", frame)
        self._check_dims_units(path, dims, frame, world_frame)

    def _validate_frame_exprs(self, path, frame):
        """Validate any value_refs embedded in frame.origin (point3d_expr).
        Literal-only origins are a no-op (existing examples)."""
        if not isinstance(frame, dict):
            return
        origin = frame.get("origin")
        if not isinstance(origin, list):
            return
        for i, coord in enumerate(origin):
            if self._is_value_ref(coord):
                self._validate_value_ref(path + ".origin[%d]" % i, coord)

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
            if h is None:
                self.err(path + ".height", "extrude requires 'height' (numeric: number or value_ref) in %s" % self.units)
            else:
                self._check_numeric_field(path + ".height", h, must_be_positive=True, label="extrude height")
        elif op == "shell":
            inp = part.get("inputs")
            if not isinstance(inp, list) or len(inp) < 1:
                self.err(path + ".inputs", "shell requires >= 1 input part id; saw %r" % (inp,))
            t = part.get("thickness")
            if t is None:
                self.err(path + ".thickness", "shell requires 'thickness' (numeric: number or value_ref) in %s" % self.units)
            else:
                self._check_numeric_field(path + ".thickness", t, must_be_positive=True, label="shell thickness")
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

            # 'to'/'to2' referential integrity. symmetric_about may name a plane
            # (e.g. 'WorldYZ'), so unknown ids there are not an error.
            to = r.get("to")
            if to is not None and isinstance(to, str) and rt != "symmetric_about":
                if to not in NAMED_PLANES and self._find_part(to) is None:
                    self.err(rpath + ".to", "references unknown part id '%s'" % to)
            to2 = r.get("to2")
            if to2 is not None and isinstance(to2, str):
                if to2 not in NAMED_PLANES and self._find_part(to2) is None:
                    self.err(rpath + ".to2", "references unknown part id '%s'" % to2)

            # at_surface (A3/A4): default 'nearest'/realized solid measurement.
            at_surface = r.get("at_surface")
            if at_surface is not None and at_surface not in AT_SURFACE_ENUM:
                self.err(
                    rpath + ".at_surface",
                    "must be one of %s; saw %r" % (list(AT_SURFACE_ENUM), at_surface),
                )

            # per-edge tol override (A3) — numeric (number or value_ref).
            if "tol" in r:
                self._check_numeric_field(rpath + ".tol", r["tol"], must_be_positive=True, label="relation tol")

            if rt == "spans_between":
                # contract allOf: spans_between requires [to, to2].
                if "to" not in r:
                    self.err(rpath, "spans_between requires 'to' (first support part id)")
                if "to2" not in r:
                    self.err(rpath, "spans_between requires 'to2' (second support part id)")

            # A4 reminder: curved/helical supports must measure to the realized
            # solid, not a face label. Nudge the author toward at_surface=realized.
            if rt in ("lands_on", "meets", "spans", "spans_between"):
                sup_part = self._find_part(to) if isinstance(to, str) else None
                sup_kind = None
                if isinstance(sup_part, dict):
                    s = sup_part.get("support")
                    if isinstance(s, dict):
                        sup_kind = s.get("kind")
                if sup_kind in ("helix_z", "z_at_angle") and at_surface not in ("realized", "nearest", None):
                    self.warn(
                        rpath + ".at_surface",
                        "relation '%s' onto a curved/helical support ('%s') should use "
                        "at_surface='realized' (A4) so the gap is the realized solid "
                        "distance, not a face label; saw %r" % (rt, to, at_surface),
                    )

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
                elif self._is_value_ref(pen):
                    # derived penetration (e.g. a fraction of a radius); sign
                    # only known after --resolve, validate the value_ref shape.
                    self._validate_value_ref(rpath + ".penetration", pen)
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
            elif "penetration" in r:
                # lands_on/meets may carry a penetration floor (numeric).
                self._check_numeric_field(rpath + ".penetration", r["penetration"], label="penetration")

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
        # If any contributing extent depends on an UNRESOLVED value_ref (or an
        # 'array' that has not been expanded), the z-extent we can compute here is
        # PARTIAL and untrustworthy. In that case a mismatch is demoted to a WARN
        # (run --resolve first for a literal extent). Resolved IRs have no
        # value_refs left, so this gate only relaxes the pre-resolve check.
        saw_unresolved = False
        for part in parts:
            if not isinstance(part, dict) or "primitive" not in part:
                continue
            # interpolated_curve control points define the z-range of any solid
            # revolved/lofted/swept/extruded from them — fold them into the
            # extent so a model built from operations (e.g. a revolve vase) is
            # still bbox-checkable, not silently un-measured.
            if part.get("primitive") == "interpolated_curve":
                cps = part.get("control_points")
                if isinstance(cps, list):
                    for cp in cps:
                        if self._is_point3d(cp):
                            zmax = cp[2] if zmax is None else max(zmax, cp[2])
                            zmin = cp[2] if zmin is None else min(zmin, cp[2])
                continue
            if isinstance(part.get("array"), dict):
                saw_unresolved = True  # family extent unknown until expanded
            frame = part.get("frame") or {}
            origin = frame.get("origin")
            if not self._is_point3d(origin):
                # origin carries a value_ref (point3d_expr) or is malformed.
                if self._is_point3d_expr(origin):
                    saw_unresolved = True
                continue
            oz = origin[2]
            prim = part.get("primitive")
            dims = part.get("dims") or {}

            def lit(v):
                # literal number or None when the dim is a value_ref.
                return v if self._is_number(v) else None

            # crude half/full extents along z about the frame origin.
            if prim == "box":
                dz = lit(dims.get("z"))
                if dz is None:
                    saw_unresolved = True
                    continue
                top, bot = oz + dz / 2.0, oz - dz / 2.0
            elif prim in ("cylinder", "cone"):
                dz = lit(dims.get("height"))
                if dz is None:
                    saw_unresolved = True
                    continue
                top, bot = oz + dz, oz  # base at origin, grows +z
            elif prim == "sphere":
                r = lit(dims.get("radius"))
                if r is None:
                    saw_unresolved = True
                    continue
                top, bot = oz + r, oz - r
            else:
                continue
            zmax = top if zmax is None else max(zmax, top)
            zmin = bot if zmin is None else min(zmin, bot)

        if zmax is None or zmin is None:
            return
        model_h_mm = (zmax - zmin) * mm
        oh_disp = oh if isinstance(oh, (int, float)) else "[%s,%s]" % (oh_lo_mm, oh_hi_mm)
        # generous +/-50% band; this is a gross-mistake guard, not a precise check.
        if model_h_mm < 0.5 * oh_lo_mm or model_h_mm > 1.5 * oh_hi_mm:
            if saw_unresolved:
                self.warn(
                    "$.parts",
                    "bbox sanity: PARTIAL primitive z-extent %.1f mm vs declared %s mm "
                    "is out of band, but some part extents depend on unresolved "
                    "value_refs/arrays (run --resolve for the literal extent)"
                    % (model_h_mm, oh_disp),
                )
                return
            self.err(
                "$.parts",
                "bbox sanity: primitive z-extent %.1f mm is far from declared "
                "overall_height_mm %s mm (check units/frames)" % (model_h_mm, oh_disp),
            )
        else:
            self.warn(
                "$.parts",
                "bbox sanity: primitive z-extent %.1f mm vs declared %s mm (within band)"
                % (model_h_mm, oh_disp),
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


# --------------------------------------------------------------------------- #
# --resolve : relational constructs -> plain-literal IR (PREVENT, §13)          #
# --------------------------------------------------------------------------- #


class ResolveError(Exception):
    """Raised when a value_ref / array cannot be resolved to a finite literal
    (cycle in the value_ref DAG, unknown param/part, non-finite arithmetic,
    division by zero, unresolvable support read). The message is verbose."""


class Resolver(object):
    """Single forward pass with a topo-sort over the value_ref DAG. Resolves
    every value_ref to a FINITE literal and expands every 'array' part into its
    concrete 0-based instances, producing a plain-literal IR that re-validates.

    There is NO eval and NO general equation solver: only
      * arithmetic folding of {op, args} (left-to-right; args[0] is the
        minuend/numerator for '-' and '/'),
      * params[name] substitution for {param},
      * attach-point reads for {part, of, at} against the referenced part's
        PUBLISHED 'support' (or its frame-origin z), e.g.
        baluster.top := support(rail).z_at_angle(this_angle)  (A4 helix law),
        column.base := support(floor).plane_z.

    Cycle detection: a value_ref that (transitively) depends on itself via a
    {part,of} chain or a self-referential {op} is an ERROR. We detect it with a
    DFS 'resolving' stack keyed by the identity of each value_ref dict."""

    def __init__(self, plan):
        self.plan = plan
        self.units = plan.get("units") if plan.get("units") in UNIT_ENUM else "mm"
        self.params = plan.get("params") or {}
        self.parts_by_id = {}
        for p in plan.get("parts", []):
            if isinstance(p, dict) and isinstance(p.get("id"), str):
                self.parts_by_id[p["id"]] = p
        # cycle guard: ids() of value_ref dicts currently on the resolve stack.
        self._stack = []
        self._stack_set = set()

    # -- public ------------------------------------------------------------- #

    def resolve(self):
        """Return a NEW plain-literal plan dict (the input is not mutated)."""
        out = json.loads(json.dumps(self.plan))  # deep copy via round-trip
        new_parts = []
        for part in out.get("parts", []):
            if isinstance(part, dict) and isinstance(part.get("array"), dict):
                new_parts.extend(self._expand_array(part))
            else:
                new_parts.append(self._resolve_part(part))
        out["parts"] = new_parts
        return out

    # -- arrays ------------------------------------------------------------- #

    def _expand_array(self, part):
        """Expand ONE array part into 'count' concrete instances '<id>#<i>'.
        Each instance is a literal part with the array offset baked into its
        frame.origin; the 'array' key is dropped from the instances."""
        arr = part["array"]
        kind = arr.get("kind")
        count = arr.get("count")
        if not isinstance(count, int) or count < 1:
            raise ResolveError(
                "part '%s' array.count must be an integer >= 1; saw %r"
                % (part.get("id"), count)
            )
        base_id = part.get("id", "part")
        radius = self._num(arr.get("radius", 0), "%s.array.radius" % base_id)
        angle_step = self._num(arr.get("angle_step", 0), "%s.array.angle_step" % base_id)
        start_angle = self._num(arr.get("start_angle", 0), "%s.array.start_angle" % base_id)
        if "z_step" in arr:
            z_step = self._num(arr.get("z_step"), "%s.array.z_step" % base_id)
        elif "pitch" in arr:
            pitch = self._num(arr.get("pitch"), "%s.array.pitch" % base_id)
            z_step = pitch * angle_step / 360.0
        else:
            z_step = 0.0
        step = arr.get("step")
        step_vec = None
        if step is not None:
            step_vec = [self._num(c, "%s.array.step[%d]" % (base_id, i)) for i, c in enumerate(step)]

        base_origin = self._instance_base_origin(part)

        instances = []
        for i in range(count):
            inst = json.loads(json.dumps(part))
            inst.pop("array", None)
            inst["id"] = "%s#%d" % (base_id, i)
            # Expose the per-instance index/angle as RESERVED params so a value_ref
            # can resolve the attach Z at THIS member's angle (e.g. a baluster
            # height = rail.z_at_angle(__angle__) - floor). They are scoped to this
            # instance only and never written into the emitted IR.
            inst_angle = start_angle + angle_step * i
            saved = (self.params.get("__i__", None), self.params.get("__angle__", None))
            had_i, had_a = ("__i__" in self.params), ("__angle__" in self.params)
            self.params["__i__"] = i
            self.params["__angle__"] = inst_angle
            try:
                inst = self._resolve_part(inst)  # resolve value_refs with __i__/__angle__ in scope
            finally:
                if had_i:
                    self.params["__i__"] = saved[0]
                else:
                    self.params.pop("__i__", None)
                if had_a:
                    self.params["__angle__"] = saved[1]
                else:
                    self.params.pop("__angle__", None)
            ox, oy, oz = base_origin
            if kind == "linear":
                if step_vec is None:
                    raise ResolveError("linear array '%s' missing 'step'" % base_id)
                ox += step_vec[0] * i
                oy += step_vec[1] * i
                oz += step_vec[2] * i
            else:  # radial / helical
                theta = math.radians(start_angle + angle_step * i)
                ox = base_origin[0] + radius * math.cos(theta)
                oy = base_origin[1] + radius * math.sin(theta)
                if kind == "helical":
                    oz = base_origin[2] + z_step * i
            frame = inst.setdefault("frame", {"origin": [0, 0, 0]})
            frame["origin"] = [ox, oy, oz]
            instances.append(inst)
        return instances

    def _instance_base_origin(self, part):
        frame = part.get("frame")
        if isinstance(frame, dict) and isinstance(frame.get("origin"), list):
            o = frame["origin"]
            return [
                self._num(o[0], "%s.frame.origin[0]" % part.get("id")),
                self._num(o[1], "%s.frame.origin[1]" % part.get("id")),
                self._num(o[2], "%s.frame.origin[2]" % part.get("id")),
            ]
        return [0.0, 0.0, 0.0]

    # -- per-part ----------------------------------------------------------- #

    def _resolve_part(self, part):
        """Resolve every value_ref reachable inside a single part to a literal,
        IN PLACE on the (already-deep-copied) part dict, and return it."""
        if not isinstance(part, dict):
            return part
        self._resolve_in_obj(part, "%s" % part.get("id", "?"))
        return part

    def _resolve_in_obj(self, obj, path):
        """Recursively replace any nested value_ref inside a CONTAINER (a part
        dict or one of its sub-objects/lists) with its resolved literal. The
        container itself is never a value_ref (a part dict may legitimately carry
        an 'op' key for a boolean operator) — only its VALUES are tested, so a
        boolean part's "op":"union" is left untouched."""
        if isinstance(obj, dict):
            for k in list(obj.keys()):
                val = obj[k]
                if isinstance(val, dict) and self._looks_like_value_ref(val):
                    obj[k] = self._eval_ref(val, path + "." + k)
                elif isinstance(val, (dict, list)):
                    self._resolve_in_obj(val, path + "." + k)
            return obj
        if isinstance(obj, list):
            for i, val in enumerate(obj):
                if isinstance(val, dict) and self._looks_like_value_ref(val):
                    obj[i] = self._eval_ref(val, "%s[%d]" % (path, i))
                elif isinstance(val, (dict, list)):
                    self._resolve_in_obj(val, "%s[%d]" % (path, i))
            return obj
        return obj

    # -- value_ref evaluation ----------------------------------------------- #

    def _looks_like_value_ref(self, v):
        return isinstance(v, dict) and (("param" in v) or ("op" in v) or ("part" in v))

    def _eval_ref(self, ref, path):
        """Resolve ONE value_ref dict to a finite float (with cycle guard)."""
        key = id(ref)
        if key in self._stack_set:
            chain = " -> ".join(self._stack + [self._ref_label(ref)])
            raise ResolveError(
                "CYCLE in value_ref DAG at %s: %s (a value_ref cannot depend on "
                "itself; break the chain)" % (path, chain)
            )
        self._stack.append(self._ref_label(ref))
        self._stack_set.add(key)
        try:
            if "param" in ref:
                name = ref["param"]
                if name not in self.params:
                    raise ResolveError("%s: unknown param '%s'" % (path, name))
                return self._num(self.params[name], "%s(param %s)" % (path, name))
            if "op" in ref:
                return self._fold_op(ref, path)
            if "part" in ref:
                return self._read_support(ref, path)
            raise ResolveError("%s: value_ref carries none of param/op/part" % path)
        finally:
            self._stack.pop()
            self._stack_set.discard(key)

    def _ref_label(self, ref):
        if "param" in ref:
            return "param:%s" % ref.get("param")
        if "op" in ref:
            return "op:%s" % ref.get("op")
        if "part" in ref:
            return "part:%s.%s" % (ref.get("part"), ref.get("of"))
        return "value_ref"

    def _fold_op(self, ref, path):
        op = ref.get("op")
        args = ref.get("args")
        if op not in ("+", "-", "*", "/") or not isinstance(args, list) or not args:
            raise ResolveError("%s: malformed {op,args}: op=%r args=%r" % (path, op, args))
        vals = [self._num(a, "%s.args[%d]" % (path, i)) for i, a in enumerate(args)]
        acc = vals[0]
        for x in vals[1:]:
            if op == "+":
                acc = acc + x
            elif op == "-":
                acc = acc - x
            elif op == "*":
                acc = acc * x
            elif op == "/":
                if x == 0:
                    raise ResolveError("%s: division by zero in {op:'/'}" % path)
                acc = acc / x
        return acc

    def _read_support(self, ref, path):
        """Resolve {part, of, at?} by reading the referenced part's PUBLISHED
        support (or frame-origin z). This is the attach-point resolution that
        makes a baluster top land on the rail (A4) without a guessed number."""
        pid = ref.get("part")
        of = ref.get("of")
        part = self.parts_by_id.get(pid)
        if part is None:
            raise ResolveError("%s: value_ref reads unknown part '%s'" % (path, pid))
        if of == "z":
            return self._frame_origin_z(part, path)
        if of == "centroid_z":
            # The realized centroid is only known in-Rhino; at IR-resolve time we
            # approximate it by the frame-origin z (DETECT still measures truth).
            return self._frame_origin_z(part, path)
        sup = part.get("support")
        if not isinstance(sup, dict):
            raise ResolveError(
                "%s: value_ref reads support of part '%s' but it publishes no "
                "'support' block" % (path, pid)
            )
        kind = sup.get("kind")
        if of in ("top_z", "base_z"):
            if kind not in ("plane_z", "top_z", "base_z"):
                raise ResolveError(
                    "%s: of='%s' needs part '%s' to publish a plane support "
                    "(plane_z/top_z/base_z); it publishes '%s'" % (path, of, pid, kind)
                )
            return self._num(sup.get("value"), "%s(support %s.value)" % (path, pid))
        if of == "z_at_angle":
            if kind not in ("helix_z", "z_at_angle"):
                raise ResolveError(
                    "%s: of='z_at_angle' needs part '%s' to publish a helix support "
                    "(helix_z/z_at_angle); it publishes '%s'" % (path, pid, kind)
                )
            helix = sup.get("helix") or {}
            base_z = self._num(helix.get("base_z"), "%s(helix.base_z)" % path)
            pitch = self._num(helix.get("pitch"), "%s(helix.pitch)" % path)
            start_angle = self._num(helix.get("start_angle", 0), "%s(helix.start_angle)" % path)
            at = ref.get("at")
            if at is None:
                raise ResolveError("%s: of='z_at_angle' requires 'at' (angle deg)" % path)
            theta = self._num(at, "%s(at)" % path)
            # Helix law (conventions §13): Z(theta) = base_z + pitch*((theta-start)/360)
            return base_z + pitch * ((theta - start_angle) / 360.0)
        raise ResolveError("%s: unsupported value_ref.of '%s'" % (path, of))

    def _frame_origin_z(self, part, path):
        frame = part.get("frame")
        if isinstance(frame, dict) and isinstance(frame.get("origin"), list):
            return self._num(frame["origin"][2], "%s(%s.frame.origin.z)" % (path, part.get("id")))
        raise ResolveError(
            "%s: of='z'/'centroid_z' needs part '%s' to have a frame.origin" % (path, part.get("id"))
        )

    def _num(self, v, path):
        """Coerce a numeric (number OR value_ref) to a finite float."""
        if isinstance(v, bool):
            raise ResolveError("%s: expected a number, got bool %r" % (path, v))
        if isinstance(v, (int, float)):
            f = float(v)
        elif self._looks_like_value_ref(v):
            f = float(self._eval_ref(v, path))
        else:
            raise ResolveError("%s: expected a number or value_ref, got %r" % (path, v))
        if not math.isfinite(f):
            raise ResolveError("%s: resolved to a non-finite value (%r)" % (path, f))
        return f


def resolve_plan(plan):
    """Resolve a plan's relational constructs into a plain-literal IR. Raises
    ResolveError on cycle / unresolvable reference."""
    return Resolver(plan).resolve()


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
    parser.add_argument(
        "--resolve",
        action="store_true",
        help=(
            "resolve relational constructs (value_ref/support/array) into a "
            "plain-literal IR, re-validate it, and emit it (conventions §13 PREVENT)"
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help="with --resolve, write the resolved literal IR here instead of stdout",
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

    if not args.resolve:
        sys.stdout.write(
            "OK     %s: valid build-plan (%d part(s), %d warning(s))\n"
            % (args.plan, len(plan.get("parts", [])), len(v.warnings))
        )
        return 0

    # --resolve: source IR is valid; resolve to a plain-literal IR and re-validate.
    try:
        resolved = resolve_plan(plan)
    except ResolveError as exc:
        sys.stderr.write("ERROR  $.resolve: %s\n" % exc)
        sys.stderr.write("FAILED %s: value_ref/array resolution failed\n" % args.plan)
        return 1

    rv = Validator(resolved, units if units in UNIT_ENUM else "mm")
    rv.validate()
    for e in rv.errors:
        sys.stderr.write("ERROR  (resolved) %s\n" % e)
    if rv.errors:
        sys.stderr.write(
            "FAILED %s: resolved IR did not re-validate (%d error(s))\n"
            % (args.plan, len(rv.errors))
        )
        return 1

    payload = json.dumps(resolved, indent=2) + "\n"
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload)
        sys.stdout.write(
            "OK     %s: resolved -> %s (%d part(s) after array expansion, %d warning(s))\n"
            % (args.plan, args.out, len(resolved.get("parts", [])), len(rv.warnings))
        )
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
