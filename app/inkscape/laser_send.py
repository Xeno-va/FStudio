"""Inkscape plugin: save the drawing and open the laser panel, then hand Inkscape straight back.

Runs inside Inkscape's sandbox. Text is turned into outlines with Inkscape's
own converter so the laser sees exactly what is on screen. The panel is
started on the laptop itself, outside the sandbox.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import inkex

PROJECT = Path(os.path.realpath(__file__)).parents[2]
JOBS = PROJECT / "app" / "jobs"
PYTHON = PROJECT / ".venv" / "bin" / "python"
# Each kind of laser has its own panel.
PANELS = {"fullspectrum": PROJECT / "app" / "fullspectrum.py", "rk-uv": PROJECT / "app" / "uvmarker.py"}
sys.path.insert(0, str(PROJECT / "app"))
import settings  # noqa: E402


class SendToLaser(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument("--laser")
        pars.add_argument("--control", type=inkex.Boolean, default=False)  # Direct control only

    def effect(self):
        JOBS.mkdir(parents=True, exist_ok=True)
        name = Path(self.document_path() or "untitled.svg").stem
        name = re.sub(r"[^\w.-]+", "-", name) or "untitled"
        raw = JOBS / f"{name}.raw.svg"
        out = JOBS / f"{name}.svg"
        self.document.write(str(raw))
        # Serial numbers (texts with # signs where the number goes, e.g. "BK###") stay as text in
        # the raw copy, where the panel reads them, and are left out of the outlines it engraves.
        serial = re.compile(r"^[^#]*#+[^#]*$")
        for el in list(self.svg.descendants().filter(inkex.TextElement)):
            if serial.match((el.get_text() or "").strip()):
                el.delete()
        plain = JOBS / f"{name}.plain.svg"
        self.document.write(str(plain))
        # Text to outlines, using Inkscape itself, so fonts match the screen.
        subprocess.run(["inkscape", "--batch-process", str(plain), "--actions",
                        f"select-all:all;object-to-path;export-plain-svg;export-filename:{out};export-do"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if not out.exists():
            out = plain
        panel = PANELS[settings.lasers()[self.options.laser]["kind"]]
        cmd = [str(PYTHON), str(panel), "--laser", self.options.laser, str(out)] + \
            (["--control"] if self.options.control else [])
        if Path("/.flatpak-info").exists():
            cmd = ["flatpak-spawn", "--host"] + cmd
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            inkex.errormsg(f"Could not open the laser panel: {e}")

    def save(self, stream):
        # Leave the drawing untouched.
        pass


if __name__ == "__main__":
    SendToLaser().run()
