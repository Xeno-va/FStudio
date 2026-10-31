"""Inkscape plugin: set the page up for a laser. The page becomes the laser's bed (or lens
field), drawn at the drawing's existing scale so nothing on it changes size, and the cut,
score and engrave colors from the laser defaults are added as swatches."""
import os
import sys
from pathlib import Path

import inkex

PROJECT = Path(os.path.realpath(__file__)).parents[2]
sys.path.insert(0, str(PROJECT / "app"))
import settings  # noqa: E402


class SetUpPage(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument("--laser")

    def effect(self):
        laser = settings.lasers().get(self.options.laser, {})
        size = str(laser.get("bed") or laser.get("field") or "")
        try:
            w_mm, h_mm = (float(v) for v in size.lower().replace("mm", "").split("x")[:2])
        except ValueError:
            raise inkex.AbortExtension(f"No bed or field size for {self.options.laser} in the laser list")
        svg = self.svg
        # Keep the drawing's scale (user units per mm) so existing artwork keeps its size.
        vb = svg.get_viewbox()
        width = svg.get("width")
        if vb and vb[2] and width:
            per_mm = vb[2] / inkex.units.convert_unit(width, "mm")
        else:
            per_mm = 96 / 25.4  # no viewBox: user units are px
        svg.set("width", f"{w_mm:g}mm")
        svg.set("height", f"{h_mm:g}mm")
        svg.set("viewBox", f"0 0 {w_mm * per_mm:g} {h_mm * per_mm:g}")
        nv = svg.namedview
        nv.set("inkscape:document-units", "mm")
        for role, spec in settings.roles().items():
            sid = f"laser-{role.lower()}"
            if svg.getElementById(sid) is None:
                grad = inkex.LinearGradient(id=sid)
                grad.set("inkscape:swatch", "solid")
                grad.set("inkscape:label", f"Laser {role}")
                grad.add(inkex.Stop(offset="0", style=f"stop-color:{spec['color']};stop-opacity:1"))
                svg.defs.add(grad)


if __name__ == "__main__":
    SetUpPage().run()
