"""Read an Inkscape drawing into laser paths grouped by color.

Every shape is flattened to straight segments in millimeters with all
transforms applied. A shape's outline color makes a cut or mark path; a
filled shape is also kept as a filled area for engraving. Each line's width
is kept, so a line can be engraved as the area it covers.
"""
import math

import svgelements as se

PX_MM = 25.4 / 96.0  # Inkscape works at 96 px per inch


def _hex(color):
    if color is None or color.value is None or color.alpha == 0:
        return None
    return color.hexrgb.lower()


CHORD_MM = 0.1  # captured: RK-CAD flattens curves into ~0.1 mm chords (circle r 8 -> 503 chords)


def _polylines(shape):
    """Flatten a shape into lists of (x, y) points in mm, one list per subpath.

    Straight segments stay single segments (their corners are kept exact); curves are cut into
    equal chords no longer than CHORD_MM, as RK-CAD does."""
    path = se.Path(shape) if not isinstance(shape, se.Path) else shape
    out = []
    for sub in path.as_subpaths():
        pts = []
        for seg in se.Path(sub):
            if isinstance(seg, se.Move):
                pts.append((seg.end.x * PX_MM, seg.end.y * PX_MM))
            elif isinstance(seg, (se.Line, se.Close)):
                if seg.end is not None:
                    pts.append((seg.end.x * PX_MM, seg.end.y * PX_MM))
            else:
                # Captured: fixed 0.1 mm steps along the curve, a short last chord to land on the end.
                length = seg.length(error=1e-6) * PX_MM
                n = max(1, math.ceil(length / CHORD_MM - 1e-9))
                ts = [k * CHORD_MM / length for k in range(1, n)] + [1.0]
                pts += [(seg.point(u).x * PX_MM, seg.point(u).y * PX_MM) for u in ts]
        clean = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        if len(clean) >= 2:
            out.append(clean)
    return out


def read(svg_source):
    """Return {color: {"stroke": [polylines], "width": [mm, one per stroke polyline],
    "fill": [polylines]}} and the page size in mm."""
    svg = se.SVG.parse(svg_source, ppi=96.0, reify=True)
    layers = {}
    for el in svg.elements():
        if not isinstance(el, se.Shape) or isinstance(el, se.Text):
            continue
        if el.values.get("visibility") == "hidden" or el.values.get("display") == "none":
            continue
        stroke, fill = _hex(el.stroke), _hex(el.fill)
        if not stroke and not fill:
            continue
        lines = _polylines(el)
        new = lambda: {"stroke": [], "width": [], "fill": []}
        if stroke:
            layer = layers.setdefault(stroke, new())
            layer["stroke"] += lines
            layer["width"] += [(el.implicit_stroke_width or 0) * PX_MM] * len(lines)
        if fill:
            layers.setdefault(fill, new())["fill"] += lines
    return layers, (svg.width * PX_MM, svg.height * PX_MM)


if __name__ == "__main__":
    import sys
    layers, page = read(sys.argv[1])
    print(f"page {page[0]:.1f} x {page[1]:.1f} mm")
    for color, kinds in layers.items():
        pts = sum(len(p) for p in kinds["stroke"] + kinds["fill"])
        print(f"{color}: {len(kinds['stroke'])} outlines, {len(kinds['fill'])} filled areas, {pts} points")
