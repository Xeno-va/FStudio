"""Inkscape plugin: restyle the selection as one laser role from the laser defaults:
Cut and Score as hairline outlines with no fill, Engrave as a fill with no outline."""
import os
import sys
from pathlib import Path

import inkex

PROJECT = Path(os.path.realpath(__file__)).parents[2]
sys.path.insert(0, str(PROJECT / "app"))
import settings  # noqa: E402

SHAPES = (inkex.PathElement, inkex.Rectangle, inkex.Circle, inkex.Ellipse, inkex.Line,
          inkex.Polyline, inkex.Polygon, inkex.TextElement)


class MarkAs(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument("--role", default="Cut")

    def effect(self):
        spec = settings.roles().get(self.options.role)
        if spec is None:
            raise inkex.AbortExtension(f"No role {self.options.role} in the laser defaults")
        if not self.svg.selection:
            raise inkex.AbortExtension("Select what to mark first")
        width = self.svg.unittouu(f"{float(spec.get('width', 0)):g}mm")
        for el in self.svg.selection.values():
            for shape in [el] + list(el.descendants()):
                if not isinstance(shape, SHAPES):
                    continue
                style = shape.style
                if str(spec.get("fill", "no")).lower() == "yes":
                    style["fill"] = spec["color"]
                    style["fill-opacity"] = "1"
                    style["stroke"] = "none"
                else:
                    style["fill"] = "none"
                    style["stroke"] = spec["color"]
                    style["stroke-opacity"] = "1"
                    style["stroke-width"] = f"{width:g}"


if __name__ == "__main__":
    MarkAs().run()
