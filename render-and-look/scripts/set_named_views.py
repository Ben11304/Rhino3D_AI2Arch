#! python3
"""
set_named_views.py  --  emit deterministic named-camera setup commands for render-and-look.

Stdlib only. Python 3 (3.7+). No third-party imports. Passes `python3 -m py_compile`.

WHAT THIS PRINTS
----------------
A single JSON object on stdout with:
  - "views":  the 4 canonical named views (front / top / right / iso) with parallel
              (orthographic / isometric) projection, fixed camera direction, up vector,
              and a frame-all so the whole model fills the frame identically every run.
  - "rhinocommon_python": a ready-to-run RhinoCommon Python 3 program. The render-and-look
              agent passes this string to the MCP tool `execute_rhinoscript_python_code`
              (or runs the equivalent statements via `run_command`) to actually set the
              named views in the live Rhino document. Setting deterministic cameras makes
              every `capture_viewport` repeatable, so the render+measure+repair loop compares
              like with like.

This script DOES NOT touch Rhino itself — it has no Rhino available in plain CPython. It only
generates the command text. Only this script's stdout enters the agent context (conventions
§11 token economy), so it prints exactly one JSON blob and nothing else.

USAGE
-----
  python3 set_named_views.py [--target-from-doc] [--width 512] [--height 512]
                             [--margin 1.10] [--no-frame-all] [--pretty]

  --target-from-doc   Frame all visible objects (BoundingBox of the document) so the model
                      fills each view. This is the default behavior; the flag is accepted for
                      explicitness in the SKILL invocation.
  --width/--height    Intended capture size in pixels (recorded in the JSON for the agent's
                      capture_viewport call; default 512x512, low-res per token economy).
  --margin            Zoom-extents padding factor (default 1.10 => 10% margin around the model).
  --no-frame-all      Emit camera orientation only; skip the per-view ZoomBoundingBox/ZoomExtents
                      (use when the caller wants to keep the current zoom).
  --pretty            Pretty-print the JSON (default is compact, one line, fewer tokens).
"""

import argparse
import json
import sys


# Canonical named views. Each is a PARALLEL projection (orthographic for front/top/right,
# isometric for iso) so captures are deterministic and silhouettes are undistorted by
# perspective. Vectors are unit camera DIRECTION (from camera toward target) and world UP.
#   - front : looking along +Y  (camera south of the model), up = +Z   -> the XZ silhouette
#   - top   : looking along -Z  (camera above the model),     up = +Y   -> the XY plan
#   - right : looking along -X  (camera east of the model),   up = +Z   -> the YZ silhouette
#   - iso   : SE isometric parallel view, up = +Z             -> a single "reads as a whole" glance
CANONICAL_VIEWS = [
    {
        "name": "front",
        "projection": "parallel",
        "camera_direction": [0.0, 1.0, 0.0],
        "up_vector": [0.0, 0.0, 1.0],
        "description": "orthographic front, XZ silhouette",
    },
    {
        "name": "top",
        "projection": "parallel",
        "camera_direction": [0.0, 0.0, -1.0],
        "up_vector": [0.0, 1.0, 0.0],
        "description": "orthographic top, XY plan",
    },
    {
        "name": "right",
        "projection": "parallel",
        "camera_direction": [-1.0, 0.0, 0.0],
        "up_vector": [0.0, 0.0, 1.0],
        "description": "orthographic right, YZ silhouette",
    },
    {
        "name": "iso",
        "projection": "parallel",
        "camera_direction": [-1.0, 1.0, -1.0],
        "up_vector": [0.0, 0.0, 1.0],
        "description": "SE isometric parallel, whole-model glance",
    },
]


def build_views(width, height, margin, frame_all):
    """Return the list of canonical view specs annotated with capture intent."""
    views = []
    for v in CANONICAL_VIEWS:
        spec = dict(v)
        spec["capture"] = {
            "width": width,
            "height": height,
            "frame_all": frame_all,
            "margin": margin,
        }
        views.append(spec)
    return views


def _py_float_list(values):
    """Render a [x, y, z] list as Python source text with explicit floats."""
    return "[" + ", ".join("{:.6f}".format(float(c)) for c in values) + "]"


def build_rhinocommon_python(views, margin, frame_all):
    """
    Emit a self-contained RhinoCommon Python 3 program (as a string) that the agent runs via
    execute_rhinoscript_python_code. It defines each canonical view on the active viewport using
    documented RhinoCommon calls, optionally frames all objects, and saves a NamedView so the
    camera is reproducible across iterations.

    Real APIs used (all exist in RhinoCommon / rhinoscriptsyntax):
      - scriptcontext.doc.Views.ActiveView.ActiveViewport
      - viewport.SetProjection(DefinedViewportProjection..., name, updateScreenPort)
      - viewport.SetCameraDirection(Vector3d, updateTargetLocation)
      - viewport.CameraUp = Vector3d
      - viewport.ZoomBoundingBox(BoundingBox)   (frame-all)
      - viewport.ZoomExtents()                  (fallback frame-all)
      - scriptcontext.doc.NamedViews.Add(name, viewportId)
      - scriptcontext.doc.Views.Redraw()
    """
    lines = []
    lines.append("#! python3")
    lines.append("import scriptcontext as sc")
    lines.append("import Rhino")
    lines.append("from Rhino.Geometry import Vector3d, BoundingBox")
    lines.append("from Rhino.Display import DefinedViewportProjection")
    lines.append("")
    lines.append("MARGIN = {:.6f}".format(float(margin)))
    lines.append("FRAME_ALL = {}".format("True" if frame_all else "False"))
    lines.append("")
    lines.append("def _doc_bbox():")
    lines.append("    # Union the bounding box of every visible object so all views frame the model identically.")
    lines.append("    bb = BoundingBox.Unset")
    lines.append("    for obj in sc.doc.Objects:")
    lines.append("        if obj is None or obj.IsDeleted:")
    lines.append("            continue")
    lines.append("        if hasattr(obj, 'Visible') and not obj.Visible:")
    lines.append("            continue")
    lines.append("        gbb = obj.Geometry.GetBoundingBox(True)")
    lines.append("        if gbb.IsValid:")
    lines.append("            bb = BoundingBox.Union(bb, gbb) if bb.IsValid else gbb")
    lines.append("    return bb")
    lines.append("")
    lines.append("def _pad(bb, margin):")
    lines.append("    # Inflate the bbox about its center by 'margin' so a frame-all leaves a clean border.")
    lines.append("    if not bb.IsValid:")
    lines.append("        return bb")
    lines.append("    c = bb.Center")
    lines.append("    dx = (bb.Max.X - bb.Min.X) * 0.5 * margin")
    lines.append("    dy = (bb.Max.Y - bb.Min.Y) * 0.5 * margin")
    lines.append("    dz = (bb.Max.Z - bb.Min.Z) * 0.5 * margin")
    lines.append("    return BoundingBox(c.X - dx, c.Y - dy, c.Z - dz, c.X + dx, c.Y + dy, c.Z + dz)")
    lines.append("")
    lines.append("def _set_view(vp, name, cam_dir, up):")
    lines.append("    vp.SetProjection(DefinedViewportProjection.Perspective, name, False)")
    lines.append("    vp.ChangeToParallelProjection(True)")
    lines.append("    vp.SetCameraDirection(Vector3d(cam_dir[0], cam_dir[1], cam_dir[2]), True)")
    lines.append("    vp.CameraUp = Vector3d(up[0], up[1], up[2])")
    lines.append("    if FRAME_ALL:")
    lines.append("        bb = _pad(_doc_bbox(), MARGIN)")
    lines.append("        if bb.IsValid:")
    lines.append("            vp.ZoomBoundingBox(bb)")
    lines.append("        else:")
    lines.append("            vp.ZoomExtents()")
    lines.append("    sc.doc.NamedViews.Add(name, vp.Id)")
    lines.append("    return name")
    lines.append("")
    lines.append("view  = sc.doc.Views.ActiveView")
    lines.append("vp    = view.ActiveViewport")
    lines.append("saved = []")
    for v in views:
        cam = _py_float_list(v["camera_direction"])
        up = _py_float_list(v["up_vector"])
        lines.append("saved.append(_set_view(vp, {!r}, {}, {}))".format(v["name"], cam, up))
    lines.append("sc.doc.Views.Redraw()")
    lines.append("print(','.join(saved))   # only the named-view list enters context")
    return "\n".join(lines)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="set_named_views.py",
        description="Emit deterministic named-camera setup commands (front/top/right/iso) "
        "for the render-and-look perception loop.",
    )
    p.add_argument(
        "--target-from-doc",
        action="store_true",
        help="Frame all visible document objects (default behavior; flag is explicit).",
    )
    p.add_argument("--width", type=int, default=512, help="Capture width in px (default 512, low-res).")
    p.add_argument("--height", type=int, default=512, help="Capture height in px (default 512, low-res).")
    p.add_argument("--margin", type=float, default=1.10, help="Frame-all padding factor (default 1.10).")
    p.add_argument(
        "--no-frame-all",
        dest="frame_all",
        action="store_false",
        help="Skip per-view zoom-extents; set camera orientation only.",
    )
    p.add_argument("--pretty", action="store_true", help="Pretty-print the JSON output.")
    p.set_defaults(frame_all=True)
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    if args.width <= 0 or args.height <= 0:
        sys.stderr.write("width and height must be positive integers\n")
        return 2
    if args.margin <= 0:
        sys.stderr.write("margin must be positive\n")
        return 2

    views = build_views(args.width, args.height, args.margin, args.frame_all)
    rc_python = build_rhinocommon_python(views, args.margin, args.frame_all)

    payload = {
        "skill": "render-and-look",
        "purpose": "deterministic named cameras for repeatable capture_viewport",
        "frame_all": args.frame_all,
        "target_from_doc": True if args.target_from_doc else args.frame_all,
        "capture_resolution": {"width": args.width, "height": args.height},
        "views": views,
        "run_via": "execute_rhinoscript_python_code",
        "rhinocommon_python": rc_python,
        "note": "Run rhinocommon_python in Rhino to create the 4 named views, then call "
        "capture_viewport per view at the given low resolution. Front/top/right are "
        "orthographic; iso is parallel isometric. No oblique camera-solve is performed "
        "(image silhouette compare uses a clean orthographic view).",
    }

    if args.pretty:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=False))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
