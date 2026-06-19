#!/usr/bin/env python3
"""Extract a revolve/loft profile from an image silhouette (stdlib only).

The image pipeline's most over-trusted step is reading a profile curve off a single
silhouette edge: perspective skew and foreshortening bias one side relative to the
other. This script implements the EXTRACT_PROFILE discipline from
``../../shared/conventions.md`` (corrections C5 and C6):

  * It splits the silhouette boundary points into a LEFT and a RIGHT silhouette about
    the detected axis and AVERAGES their half-widths to cancel perspective skew.
  * It resamples the averaged profile into ordered control points suitable for
    ``Rhino.Geometry.Curve.CreateInterpolatedCurve`` (each point is on the +axis side,
    i.e. the generatrix of a solid of revolution).
  * It FLAGS low confidence (large left/right asymmetry, too few points, or a profile
    that does not return to the axis at top/bottom -- a C6 requirement for a closed
    revolve).
  * When confidence is low it emits a set of FALLBACK ARCHETYPAL profiles (cylinder,
    bottle, ogee_vase, bowl, baluster) for the render-vs-reference loop to choose among,
    rather than trusting raw pixel sampling.

No third-party imports: pure Python 3 standard library only (no numpy, no cv2).

INPUT (JSON on stdin, or a path as argv[1]):
    {
      "boundary": [[x, y], [x, y], ...],     # silhouette boundary points (pixels or mm)
      "axis": [[x0, y0], [x1, y1]],          # two points defining the symmetry axis
      "samples": 24,                          # optional: number of output control points
      "y_up": true                            # optional: true => larger param = profile top
    }

OUTPUT (JSON on stdout):
    {
      "ok": true,
      "axis": {"origin": [...], "direction_unit": [...], "length": <float>},
      "control_points": [[x, y, z], ...],     # 3D, on the WorldXZ plane, x = radius, z = height
      "n_control_points": <int>,
      "confidence": "high" | "medium" | "low",
      "confidence_reasons": [ ... ],
      "starts_on_axis": <bool>,               # C6: first control point radius ~ 0
      "ends_on_axis": <bool>,                 # C6: last  control point radius ~ 0
      "closes_to_axis": <bool>,               # both ends on axis (closed solid of revolution)
      "metrics": {"max_width": <float>, "height": <float>, "height_over_max_width": <float>,
                   "lr_asymmetry": <float>, "n_input": <int>},
      "fallback_profiles": { ... }            # present (non-empty) only when confidence == "low"
    }

The control points are returned in the WorldXZ plane (x = radius from axis, y = 0,
z = height along axis), which is the natural construction plane for a revolve whose axis
is WorldZ; the executor relocates them to the part frame via Transform.PlaneToPlane.
"""

import json
import math
import sys

# ----------------------------------------------------------------------------------------
# Confidence thresholds (tunable, documented).
# ----------------------------------------------------------------------------------------
MIN_BOUNDARY_POINTS = 8          # fewer than this => cannot trust a profile at all
LR_ASYMMETRY_LOW = 0.18          # mean |L-R| / max_width above this => low confidence
LR_ASYMMETRY_MED = 0.07          # between MED and LOW => medium confidence
AXIS_CLOSE_FRAC = 0.04           # end half-width <= this * max_width => "on axis" (C6)
DEFAULT_SAMPLES = 24
EPS = 1e-9


# ----------------------------------------------------------------------------------------
# Small 2D vector helpers (stdlib only).
# ----------------------------------------------------------------------------------------
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _length(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1])


def _unit(v):
    n = _length(v)
    if n < EPS:
        return (0.0, 0.0)
    return (v[0] / n, v[1] / n)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _cross_z(a, b):
    """Z-component of the 2D cross product a x b; sign gives the side of a line."""
    return a[0] * b[1] - a[1] * b[0]


def _axis_frame(axis):
    """Return (origin, t_unit, axis_length) for a two-point axis.

    t_unit is the unit direction along the axis; the signed perpendicular distance of any
    point to the axis tells us which silhouette (left/right) it belongs to.
    """
    o = (float(axis[0][0]), float(axis[0][1]))
    p1 = (float(axis[1][0]), float(axis[1][1]))
    d = _sub(p1, o)
    length = _length(d)
    if length < EPS:
        raise ValueError("axis endpoints are coincident; cannot define an axis direction")
    return o, _unit(d), length


def _project(point, origin, t_unit):
    """Decompose point into (s, w): s = signed distance ALONG axis from origin,
    w = signed perpendicular distance (left positive, right negative)."""
    rel = _sub(point, origin)
    s = _dot(rel, t_unit)
    w = _cross_z(t_unit, rel)   # +ve on one side of the axis, -ve on the other
    return s, w


# ----------------------------------------------------------------------------------------
# Core: average left + right silhouettes about the axis.
# ----------------------------------------------------------------------------------------
def _binned_halfwidths(boundary, origin, t_unit, axis_len, n_bins):
    """Bin boundary points by their position s along the axis, then for each bin take the
    max |w| on the left and on the right. Returns parallel lists:
        s_centers[i], left_w[i], right_w[i], have_left[i], have_right[i]
    Half-widths use the maximum extent in each bin so a concave neck is still captured.
    """
    # Determine the along-axis extent actually covered by the silhouette.
    s_values = []
    for p in boundary:
        s, _w = _project(p, origin, t_unit)
        s_values.append(s)
    s_min = min(s_values)
    s_max = max(s_values)
    span = s_max - s_min
    if span < EPS:
        span = max(axis_len, 1.0)

    left = [0.0] * n_bins
    right = [0.0] * n_bins
    have_left = [False] * n_bins
    have_right = [False] * n_bins

    for p in boundary:
        s, w = _project(p, origin, t_unit)
        frac = (s - s_min) / span
        b = int(frac * (n_bins - 1) + 0.5)
        if b < 0:
            b = 0
        elif b > n_bins - 1:
            b = n_bins - 1
        aw = abs(w)
        if w >= 0.0:
            if not have_left[b] or aw > left[b]:
                left[b] = aw
            have_left[b] = True
        else:
            if not have_right[b] or aw > right[b]:
                right[b] = aw
            have_right[b] = True

    s_centers = [s_min + (i / float(n_bins - 1)) * span for i in range(n_bins)]
    return s_centers, left, right, have_left, have_right


def _fill_gaps(values, present):
    """Linearly interpolate bins that received no point, using nearest filled neighbours.
    Leading/trailing empty bins copy the nearest filled value."""
    n = len(values)
    out = list(values)
    filled = [i for i in range(n) if present[i]]
    if not filled:
        return [0.0] * n
    # Leading gap.
    first = filled[0]
    for i in range(0, first):
        out[i] = values[first]
    # Trailing gap.
    last = filled[-1]
    for i in range(last + 1, n):
        out[i] = values[last]
    # Interior gaps between consecutive filled indices.
    for k in range(len(filled) - 1):
        a = filled[k]
        b = filled[k + 1]
        if b - a <= 1:
            continue
        va = values[a]
        vb = values[b]
        for i in range(a + 1, b):
            t = (i - a) / float(b - a)
            out[i] = va + (vb - va) * t
    return out


def _resample_profile(boundary, axis, samples):
    """Average left/right half-widths about the axis and resample into `samples` ordered
    profile points. Returns (s_centers, avg_w, left_w, right_w, lr_asymmetry, max_width).
    """
    origin, t_unit, axis_len = _axis_frame(axis)
    n_bins = max(samples, 12)

    s_centers, left, right, have_left, have_right = _binned_halfwidths(
        boundary, origin, t_unit, axis_len, n_bins)

    left_f = _fill_gaps(left, have_left)
    right_f = _fill_gaps(right, have_right)

    # Average the two silhouettes => cancels perspective skew (the whole point, C5).
    avg = [(left_f[i] + right_f[i]) * 0.5 for i in range(n_bins)]

    max_width = max(avg) if avg else 0.0
    # Left/right disagreement, normalized by width, drives the confidence flag.
    if max_width > EPS:
        lr_asym = sum(abs(left_f[i] - right_f[i]) for i in range(n_bins)) / (n_bins * max_width)
    else:
        lr_asym = 1.0

    # Resample the n_bins arrays down/uniformly to exactly `samples` ordered points.
    if n_bins == samples:
        rs_s = list(s_centers)
        rs_w = list(avg)
    else:
        rs_s = []
        rs_w = []
        for j in range(samples):
            t = j / float(samples - 1) if samples > 1 else 0.0
            fpos = t * (n_bins - 1)
            i0 = int(math.floor(fpos))
            i1 = min(i0 + 1, n_bins - 1)
            frac = fpos - i0
            rs_s.append(s_centers[i0] + (s_centers[i1] - s_centers[i0]) * frac)
            rs_w.append(avg[i0] + (avg[i1] - avg[i0]) * frac)

    return rs_s, rs_w, left_f, right_f, lr_asym, max_width


# ----------------------------------------------------------------------------------------
# Build 3D control points + confidence assessment.
# ----------------------------------------------------------------------------------------
def _to_control_points(s_centers, half_widths, y_up):
    """Map (s along axis, half-width) -> 3D points on the WorldXZ plane: x = radius,
    y = 0, z = height. Ordered bottom -> top of the profile."""
    s_min = min(s_centers)
    pts = []
    for s, w in zip(s_centers, half_widths):
        z = s - s_min
        pts.append([round(max(w, 0.0), 6), 0.0, round(z, 6)])
    if not y_up:
        pts.reverse()
        # Re-zero height so the first point is at z = 0 after the flip.
        z0 = pts[0][2]
        for p in pts:
            p[2] = round(p[2] - z0, 6)
        # Heights were reversed; make them monotonically increasing again.
        zmax = pts[-1][2]
        if zmax < 0:
            for p in pts:
                p[2] = round(-p[2], 6)
    return pts


def _snap_ends_to_axis(pts, max_width):
    """C6: a closed solid of revolution needs the profile to touch the axis at both ends.
    Report whether each end is already on the axis; do not force it (the executor snaps
    within tol), but flag closure so the caller knows whether to add cap points."""
    if not pts:
        return False, False
    thresh = AXIS_CLOSE_FRAC * max_width if max_width > EPS else EPS
    starts_on_axis = pts[0][0] <= thresh
    ends_on_axis = pts[-1][0] <= thresh
    return starts_on_axis, ends_on_axis


def _assess_confidence(n_input, lr_asym, starts_on_axis, ends_on_axis, max_width):
    reasons = []
    level = "high"

    if n_input < MIN_BOUNDARY_POINTS:
        reasons.append("too few boundary points (%d < %d)" % (n_input, MIN_BOUNDARY_POINTS))
        level = "low"

    if max_width <= EPS:
        reasons.append("degenerate profile: zero max width")
        level = "low"

    if lr_asym >= LR_ASYMMETRY_LOW:
        reasons.append("large left/right asymmetry (%.3f >= %.3f): strong perspective skew"
                       % (lr_asym, LR_ASYMMETRY_LOW))
        level = "low"
    elif lr_asym >= LR_ASYMMETRY_MED:
        reasons.append("moderate left/right asymmetry (%.3f): foreshortening likely"
                       % lr_asym)
        if level == "high":
            level = "medium"

    if not (starts_on_axis or ends_on_axis):
        reasons.append("profile does not return to the axis at either end (C6: open revolve)")
        if level == "high":
            level = "medium"

    if not reasons:
        if starts_on_axis and ends_on_axis:
            reasons.append("clean symmetric silhouette; profile closes to axis at both ends")
        else:
            reasons.append("clean symmetric silhouette; profile returns to axis at one end "
                           "(executor snaps the other end onto the axis within tol, C6)")
    return level, reasons


# ----------------------------------------------------------------------------------------
# Fallback archetypal profiles (emitted only when confidence is low).
# ----------------------------------------------------------------------------------------
def _archetype_profiles(height, max_radius):
    """Normalized archetypal generatrix profiles scaled to the observed bounding box.

    Each profile is a list of [radius, 0, height] control points, bottom -> top, starting
    and ending ON the axis (radius 0) so each is a valid closed solid of revolution (C6).
    The render-vs-reference loop picks whichever best matches the reference silhouette.
    """
    h = height if height > EPS else 1.0
    r = max_radius if max_radius > EPS else 0.5

    def scaled(norm_pairs):
        # norm_pairs: list of (radius_frac, height_frac), both in [0, 1].
        return [[round(rf * r, 6), 0.0, round(hf * h, 6)] for (rf, hf) in norm_pairs]

    return {
        "cylinder": scaled([
            (0.0, 0.0), (1.0, 0.0), (1.0, 0.5), (1.0, 1.0), (0.0, 1.0),
        ]),
        "bowl": scaled([
            (0.0, 0.0), (0.55, 0.05), (0.9, 0.25), (1.0, 0.55), (0.98, 0.9),
            (0.95, 1.0), (0.0, 1.0),
        ]),
        "bottle": scaled([
            (0.0, 0.0), (1.0, 0.02), (1.0, 0.45), (0.92, 0.6), (0.35, 0.78),
            (0.3, 0.95), (0.3, 1.0), (0.0, 1.0),
        ]),
        "ogee_vase": scaled([
            (0.0, 0.0), (0.45, 0.0), (0.5, 0.08), (0.95, 0.28), (1.0, 0.45),
            (0.7, 0.66), (0.55, 0.82), (0.7, 0.96), (0.72, 1.0), (0.0, 1.0),
        ]),
        "baluster": scaled([
            (0.0, 0.0), (0.6, 0.0), (0.6, 0.08), (1.0, 0.2), (0.45, 0.4),
            (0.35, 0.55), (0.85, 0.72), (0.5, 0.9), (0.5, 1.0), (0.0, 1.0),
        ]),
    }


# ----------------------------------------------------------------------------------------
# Driver.
# ----------------------------------------------------------------------------------------
def extract_profile(data):
    boundary = data.get("boundary")
    axis = data.get("axis")
    samples = int(data.get("samples", DEFAULT_SAMPLES))
    y_up = bool(data.get("y_up", True))

    if not isinstance(boundary, list) or len(boundary) < 2:
        raise ValueError("'boundary' must be a list of >= 2 [x, y] points")
    if not isinstance(axis, list) or len(axis) != 2:
        raise ValueError("'axis' must be exactly two [x, y] points")
    if samples < 3:
        samples = 3

    boundary = [(float(p[0]), float(p[1])) for p in boundary]
    n_input = len(boundary)

    origin, t_unit, axis_len = _axis_frame(axis)

    s_centers, half_widths, left_f, right_f, lr_asym, max_width = _resample_profile(
        boundary, axis, samples)

    control_points = _to_control_points(s_centers, half_widths, y_up)
    height = control_points[-1][2] - control_points[0][2] if control_points else 0.0
    height = abs(height)

    starts_on_axis, ends_on_axis = _snap_ends_to_axis(control_points, max_width)
    closes_to_axis = starts_on_axis and ends_on_axis

    confidence, reasons = _assess_confidence(
        n_input, lr_asym, starts_on_axis, ends_on_axis, max_width)

    hw = (height / max_width) if max_width > EPS else 0.0

    result = {
        "ok": True,
        "axis": {
            "origin": [round(origin[0], 6), round(origin[1], 6)],
            "direction_unit": [round(t_unit[0], 6), round(t_unit[1], 6)],
            "length": round(axis_len, 6),
        },
        "control_points": control_points,
        "n_control_points": len(control_points),
        "confidence": confidence,
        "confidence_reasons": reasons,
        "starts_on_axis": starts_on_axis,
        "ends_on_axis": ends_on_axis,
        "closes_to_axis": closes_to_axis,
        "metrics": {
            "max_width": round(max_width, 6),
            "height": round(height, 6),
            "height_over_max_width": round(hw, 6),
            "lr_asymmetry": round(lr_asym, 6),
            "n_input": n_input,
        },
        "fallback_profiles": {},
    }

    # Only emit fallback archetypes when we cannot trust the sampled profile (C5).
    if confidence == "low":
        result["fallback_profiles"] = _archetype_profiles(height, max_width)

    return result


def main(argv):
    if len(argv) > 1:
        with open(argv[1], "r") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        sys.stderr.write("extract_profile: no input (provide JSON on stdin or a path arg)\n")
        return 2

    try:
        data = json.loads(raw)
        result = extract_profile(data)
    except (ValueError, KeyError, TypeError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}))
        sys.stdout.write("\n")
        return 1

    sys.stdout.write(json.dumps(result, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
