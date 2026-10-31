"""Job geometry both panels share: kerf offset, inside cuts before outside cuts, the
material test grid, and labels drawn as strokes. All in page millimeters, y down."""
import re

import cairo
from shapely.geometry import LinearRing, LineString, Point, Polygon
from shapely.ops import unary_union


def _closed(path):
    return len(path) >= 4 and path[0] == path[-1]


def _polygon(path):
    try:
        poly = Polygon(path)
        return poly if poly.is_valid and poly.area > 0 else poly.buffer(0)
    except ValueError:
        return None


def depths(paths):
    """How many other closed paths surround each path: 0 is an outside edge, 1 a hole in it,
    2 a part inside that hole, and so on. Open paths count what surrounds their first point."""
    polys = [_polygon(p) if _closed(p) else None for p in paths]
    out = []
    for i, p in enumerate(paths):
        probe = polys[i] if polys[i] is not None else Point(p[0])
        out.append(sum(1 for j, q in enumerate(polys)
                       if j != i and q is not None and q.area > probe.area and q.contains(probe)))
    return out


def inside_first(paths):
    """Deepest paths first, so holes and inner parts are cut while the part around them is still
    held by the sheet. Paths at the same depth keep their order."""
    d = depths(paths)
    return [p for _, _, p in sorted(((-d[i], i, p) for i, p in enumerate(paths)))]


def line_areas(lines, widths, least_mm):
    """The area each line covers at its drawn width, never thinner than least_mm (one engraving
    dot, so a hairline still engraves), as closed polylines: outer edges and holes."""
    if not lines:
        return []
    area = unary_union([LineString(l).buffer(max(w, least_mm) / 2, cap_style="flat", join_style="mitre",
                                              mitre_limit=4) for l, w in zip(lines, widths)])
    out = []
    for poly in getattr(area, "geoms", [area]):
        if not poly.is_empty:
            out += [list(poly.exterior.coords)] + [list(r.coords) for r in poly.interiors]
    return out


def fill_groups(polys):
    """Filled areas grouped into shapes: each outer edge with the holes directly inside it, so
    a letter O is one shape. Groups come in the order of their outer edges."""
    d = depths(polys)
    groups = {i: [p] for i, p in enumerate(polys) if d[i] % 2 == 0}
    for i, p in enumerate(polys):
        if d[i] % 2:
            hole = Polygon(p) if _closed(p) else None
            owner = [j for j in groups if d[j] == d[i] - 1 and hole is not None and
                     (_polygon(polys[j]) or Polygon()).contains(hole.representative_point())]
            groups[owner[0] if owner else i] = groups.get(owner[0] if owner else i, []) + [p]
    return [groups[i] for i in sorted(groups)]


def kerf_offset(paths, kerf_mm):
    """Each closed path moved out by half the kerf on an outside edge (even depth) and in on a
    hole (odd depth), corners kept sharp. Open paths and paths a hole would swallow stay as
    drawn."""
    if not kerf_mm:
        return paths
    half = kerf_mm / 2
    out = []
    for p, depth in zip(paths, depths(paths)):
        poly = _polygon(p) if _closed(p) else None
        if poly is None:
            out.append(p)
            continue
        grown = poly.buffer(half if depth % 2 == 0 else -half, join_style="mitre", mitre_limit=4)
        if grown.is_empty or grown.geom_type != "Polygon":
            out.append(p)
            continue
        ring = list(grown.exterior.coords)
        # Keep the drawn direction of travel.
        if LinearRing(ring).is_ccw != LinearRing(p).is_ccw:
            ring.reverse()
        out.append([(x, y) for x, y in ring])
    return out


def text_paths(text, x, y, height_mm, anchor="left"):
    """A line of text as stroke outlines (page mm), its baseline at y. anchor: left, right or
    center on x."""
    surface = cairo.RecordingSurface(cairo.Content.COLOR_ALPHA, None)
    cr = cairo.Context(surface)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(height_mm / 0.72)  # cap height is about 0.72 of the font size
    ext = cr.text_extents(text)
    dx = {"left": 0, "right": -ext.x_advance, "center": -ext.x_advance / 2}[anchor]
    cr.move_to(x + dx, y)
    cr.text_path(text)
    paths, cur = [], []
    for kind, pts in cr.copy_path_flat():
        if kind == cairo.PATH_MOVE_TO:
            if len(cur) > 1:
                paths.append(cur)
            cur = [pts]
        elif kind == cairo.PATH_LINE_TO:
            cur.append(pts)
        elif kind == cairo.PATH_CLOSE_PATH and cur:
            cur.append(cur[0])
            paths.append(cur)
            cur = []
    if len(cur) > 1:
        paths.append(cur)
    return paths


def steps(lo, hi, n):
    return [lo] if n <= 1 else [lo + (hi - lo) * k / (n - 1) for k in range(n)]


def test_grid(t, corner):
    """The material test: squares with power stepping down the rows and speed across the
    columns, labelled, with the top-left of the labels at corner (page mm).

    t: rows, cols, power_lo, power_hi, speed_lo, speed_hi, size, gap.
    Returns (cells, labels): cells are (power, speed, square path); labels are stroke paths."""
    label_h, margin = 2.5, 3.0
    left = corner[0] + 4 * label_h * 0.8 + margin  # room for "100" down the left side
    top = corner[1] + label_h * 2 + margin * 2  # room for the speed numbers and the heading
    powers = steps(t["power_hi"], t["power_lo"], int(t["rows"]))  # from, to
    speeds = steps(t["speed_lo"], t["speed_hi"], int(t["cols"]))
    size, pitch = t["size"], t["size"] + t["gap"]
    cells, labels = [], []
    labels += text_paths(f"Speed {t.get('speed_unit', '%')} →", left, corner[1] + label_h, label_h)
    for c, speed in enumerate(speeds):
        labels += text_paths(f"{speed:g}", left + c * pitch + size / 2, top - margin, label_h, "center")
    for r, power in enumerate(powers):
        y = top + r * pitch
        labels += text_paths(f"{power:g}", left - margin, y + size / 2 + label_h / 2, label_h, "right")
        for c, speed in enumerate(speeds):
            x = left + c * pitch
            cells.append((round(power, 1), round(speed, 1),
                          [(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]))
    labels += text_paths("Power %", corner[0], top + len(powers) * pitch + label_h + margin / 2, label_h)
    return cells, labels


def grid_size(t):
    """Width and height of the whole test grid with its labels, mm."""
    label_h, margin = 2.5, 3.0
    w = 4 * label_h * 0.8 + margin + int(t["cols"]) * (t["size"] + t["gap"])
    h = label_h * 2 + margin * 2 + int(t["rows"]) * (t["size"] + t["gap"]) + label_h + margin
    return w, h


# ------------------------------------------------------------ serial numbers
# A text with # signs in it (e.g. "####" or "BK###") is a serial number: the # signs are the
# counter, one digit each, and anything around them stays as it is.
SERIAL = re.compile(r"^([^#]*)(#+)([^#]*)$")


def serial_fields(svg_path):
    """Every serial text in the drawing, in page order (top to bottom, then left to right), with
    what is needed to draw any value of it in the same place, font and size."""
    import svgelements as se
    svg = se.SVG.parse(str(svg_path), ppi=96.0, reify=False)
    out = []
    for el in svg.elements():
        if not isinstance(el, se.Text) or not el.text:
            continue
        m = SERIAL.match(el.text.strip())
        if not m:
            continue
        weight = str(el.font_weight or "normal")
        out.append({"prefix": m.group(1), "width": len(m.group(2)), "suffix": m.group(3),
                    "family": (el.font_family or "sans-serif").strip("'\""),
                    "bold": weight == "bold" or (weight.isdigit() and int(weight) >= 600),
                    "size": float(el.font_size or 12), "x": float(el.x or 0), "y": float(el.y or 0),
                    "anchor": {"middle": "center", "end": "right"}.get(str(el.anchor), "left"),
                    "matrix": el.transform, "color": el.fill.hexrgb.lower() if el.fill and el.fill.value is not None else "#000000",
                    "text": el.text.strip()})
    out.sort(key=lambda f: (f["matrix"].point_in_matrix_space((f["x"], f["y"]))[1],
                            f["matrix"].point_in_matrix_space((f["x"], f["y"]))[0]))
    return out


def serial_text(field, value):
    """The serial as engraved: the # signs replaced by the number, zero-padded to their count."""
    return f"{field['prefix']}{int(value):0{field['width']}d}{field['suffix']}"


def font_file(family, bold):
    """The font file the system (and so Inkscape) uses for this family and weight."""
    import subprocess
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", f"{family}:{'bold' if bold else 'regular'}"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def serial_outline(field, value):
    """One value of a serial text as closed outlines in page mm, ready to engrave as a fill.
    Drawn from the very font file the system picks for the text's font, glyph by glyph at the
    font's own spacing, as Inkscape draws it."""
    import svgelements as se
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont
    import drawing
    text = field["text"] if value is None else serial_text(field, value)  # None: as drawn, # signs
    path = font_file(field["family"], field["bold"])
    font = TTFont(path)
    glyphs, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
    scale = field["size"] / font["head"].unitsPerEm
    names = [cmap.get(ord(c), ".notdef") for c in text]
    width = sum(hmtx[g][0] for g in names) * scale
    x = field["x"] - {"left": 0, "right": width, "center": width / 2}[field["anchor"]]
    pen = SVGPathPen(glyphs)
    for g in names:
        glyphs[g].draw(TransformPen(pen, (scale, 0, 0, -scale, x, field["y"])))
        x += hmtx[g][0] * scale
    d = pen.getCommands()
    if not d:
        return []
    shape = (se.Path(d) * field["matrix"]).reify()  # the text's own placement, in the page's pixels
    return [p if p[0] == p[-1] else p + [p[0]] for p in drawing._polylines(shape)]
