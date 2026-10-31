"""The Full Spectrum H-Series panel (CO2, moving head), laid out like Epilog's job settings.

usage: fullspectrum.py [--laser NAME] [--control] [drawing.svg]
"""
import argparse

import jobtools

from window import (GLib, Gtk, LaserWindow, big_button, pick, preset_picker, presets_tab, replace_page,
                    run_app, save_preset_button, settings, spin, spin_value)

KIND = "fullspectrum"
CUT_COLOR = "#ff0000"
ORIGINS = ["center", "top left", "top", "top right", "left", "right", "bottom left", "bottom", "bottom right"]

# General tab: job-wide settings, in framed boxes side by side. ("key", label, kind, args)
GENERAL = [
    ("-", "Job", None, None),
    ("job_type", "Job type", "choice", ["combined", "raster", "vector"]),
    ("dpi", "Resolution (dpi)", "choice", ["250", "500", "1000"]),
    ("start_from", "Start from", "choice", ["Absolute", "Relative"]),
    ("origin", "Job origin", "choice", ORIGINS),
    ("frame_shape", "Frame", "choice", ["bounding box", "outline"]),
    ("-", "Raster setting", None, None),
    ("raster_speed", "Speed %", "spin", (1, 100, 1, 0, 80)),
    ("raster_power", "Power %", "spin", (0, 100, 1, 0, 30)),
    ("direction", "Engrave direction", "choice", ["left and right", "up and down", "diagonal"]),
    ("dither", "Dithering", "choice", ["none", "simple", "halftone"]),
    ("raster_passes", "Passes", "spin", (1, 20, 1, 0, 1)),
    ("-", "Vector setting", None, None),
    ("vector_speed", "Speed %", "spin", (1, 100, 1, 0, 15)),
    ("vector_power", "Power %", "spin", (0, 100, 1, 0, 60)),
    ("vector_passes", "Passes", "spin", (1, 20, 1, 0, 1)),
    ("vector_kerf", "Kerf mm", "spin", (0, 2, 0.01, 2, 0)),
]

# Color Mapping: a color on "general" uses the General tab; otherwise its own settings.
MODES = ["general", "skip", "vector", "raster"]
FIELDS = [("power", "Power %", 0, 100, 1, 0, 10), ("speed", "Speed %", 1, 100, 1, 0, 50),
          ("passes", "Passes", 1, 20, 1, 0, 1), ("kerf", "Kerf mm", 0, 2, 0.01, 2, 0)]
# Vector only: the beam's width, half of it added outside each part and inside each hole.
VECTOR_ONLY = ("kerf",)
# Raster only: RetinaEngrave's resolution box, dither buttons and direction.
CHOICES = [("dpi", "Resolution (dpi)", ["250", "500", "1000"], "500"),
           ("dither", "Dithering", ["none", "simple", "halftone"], "none"),
           ("direction", "Engrave direction", ["left and right", "up and down", "diagonal"], "left and right")]
RASTER_ONLY = ("dpi", "dither", "direction")
# What the sender calls each mode.
SENDS = {"vector": "cut", "raster": "engrave"}


class FullSpectrumPanel(LaserWindow):
    def bed_note(self):
        return "home is the top-right corner"

    def draw_marks(self, cr, s):
        # Home: the head parks at the bed's top-right corner.
        cr.set_source_rgb(0.2, 0.6, 0.2)
        cr.arc(self.page[0] * s, 0, 5, 0, 6.3)
        cr.fill()

    # ------------------------------------------------------------ settings
    def build_settings(self):
        self.saved = settings.remembered(self.drawing_key())
        self.tabs = Gtk.Notebook()
        self.tabs.append_page(self.general_tab(), Gtk.Label(label="General"))
        self.tabs.append_page(self.colors_tab(), Gtk.Label(label="Color Mapping"))
        self.tabs.append_page(self.presets(), Gtk.Label(label="Presets"))
        return self.tabs

    def refresh(self):
        replace_page(self.tabs, 1, self.colors_tab(), "Color Mapping")
        replace_page(self.tabs, 2, self.presets(), "Presets")

    def general_tab(self):
        self.general = {}
        columns = Gtk.Box(spacing=8, margin_top=6, margin_start=6, margin_end=6, margin_bottom=6)
        grid, r = None, 0
        for key, label, kind, args in GENERAL:
            if key == "-":
                grid, r = Gtk.Grid(column_spacing=10, row_spacing=4, margin_top=4, margin_bottom=6,
                                   margin_start=8, margin_end=8), 0
                head = Gtk.Label()
                head.set_markup(f"<b>{label}</b>")
                columns.append(Gtk.Frame(label_widget=head, child=grid, valign=Gtk.Align.START))
                continue
            grid.attach(Gtk.Label(label=label, xalign=0, hexpand=True), 0, r, 1, 1)
            if kind == "spin":
                w = spin(*args[:4], self.saved.get(f"general.{key}", args[4]))
                w.connect("value-changed", lambda *_: self.remember())
            else:
                w = pick(args, settings_for({key: self.saved.get(f"general.{key}", "500" if key == "dpi" else args[0])})[key])
                w.connect("notify::selected", lambda *_: self.remember())
            grid.attach(w, 1, r, 1, 1)
            self.general[key] = (w, kind, args)
            r += 1
        # The job origin only matters when starting from the head.
        start, origin = self.general["start_from"][0], self.general["origin"][0]
        follow = lambda *_: origin.set_sensitive(start.get_selected() == 1)
        start.connect("notify::selected", follow)
        follow()
        return columns

    def general_values(self):
        return {k: spin_value(w) if kind == "spin" else args[w.get_selected()]
                for k, (w, kind, args) in self.general.items()}

    def general_profile(self, mode):
        """What a color left on General runs with, in the same terms as a Color Mapping row."""
        g = self.general_values()
        if mode == "raster":
            return {"mode": "raster", "speed": g["raster_speed"], "power": g["raster_power"],
                    "passes": g["raster_passes"], "dpi": g["dpi"], "dither": g["dither"],
                    "direction": g["direction"]}
        return {"mode": "vector", "speed": g["vector_speed"], "power": g["vector_power"], "passes": g["vector_passes"],
                "kerf": g["vector_kerf"]}

    def colors_tab(self):
        self.rows = {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6, margin_start=6)
        if not self.layers:
            return box
        starts = settings.color_defaults(KIND)
        grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        heads = ["", "Color", "Mode"] + [f[1].replace(" %", "") for f in FIELDS] + ["dpi", "Dithering", "Direction"]
        for c, text in enumerate(heads + ["", ""]):
            grid.attach(Gtk.Label(label=text, xalign=0), c, 0, 1, 1)
        for r, (color, kinds) in enumerate(self.layers.items(), start=1):
            start = settings_for(starts.get(color.lower(), {}))  # the laser defaults for this color's role
            saved = lambda key, default: self.saved.get(f"{color}.{key}", start.get(key, default))
            swatch = Gtk.Label()
            swatch.set_markup(f'<span background="{color}">     </span>')
            grid.attach(swatch, 0, r, 1, 1)
            grid.attach(Gtk.Label(label=color, xalign=0, tooltip_text=contents(kinds)), 1, r, 1, 1)
            mode = pick(MODES, settings_for({"mode": saved("mode", "general")})["mode"])
            mode.connect("notify::selected", lambda *_: self.remember())
            grid.attach(mode, 2, r, 1, 1)
            col, spins, picks = 3, {}, {}
            for key, _label, lo, hi, step, digits, default in FIELDS:
                w = spin(lo, hi, step, digits, saved(key, default))
                w.set_width_chars(5)
                w.connect("value-changed", lambda *_: self.remember())
                grid.attach(w, col, r, 1, 1)
                spins[key], col = w, col + 1
            for key, _label, options, default in CHOICES:
                w = pick(options, saved(key, default))
                w.connect("notify::selected", lambda *_: self.remember())
                grid.attach(w, col, r, 1, 1)
                picks[key], col = (w, options), col + 1
            grid.attach(preset_picker(KIND, lambda v, n, cl=color: self.load_row(cl, v, n)), col, r, 1, 1)
            grid.attach(save_preset_button(self, KIND, lambda cl=color: self.row_values(cl), self.refresh),
                        col + 1, r, 1, 1)
            self.rows[color] = {"mode": mode, "spins": spins, "picks": picks}

            # A color on General, or skipped, uses none of its own settings; resolution,
            # dithering and direction only mean something on a raster color.
            def gray(*_, m=mode, pk=picks, sp=spins):
                chosen = MODES[m.get_selected()]
                own = chosen not in ("general", "skip")
                for key, w in sp.items():
                    w.set_sensitive(own and (chosen == "vector" or key not in VECTOR_ONLY))
                for key, (w, _o) in pk.items():
                    w.set_sensitive(own and (chosen == "raster" or key not in RASTER_ONLY))
            mode.connect("notify::selected", gray)
            gray()
        box.append(grid)
        return Gtk.ScrolledWindow(child=box, propagate_natural_height=True, propagate_natural_width=True,
                                  max_content_height=320, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def row_values(self, color):
        row = self.rows[color]
        out = {"mode": MODES[row["mode"].get_selected()]}
        out.update({k: spin_value(w) for k, w in row["spins"].items()})
        out.update({k: options[w.get_selected()] for k, (w, options) in row["picks"].items()})
        return out

    def remember(self):
        flat = {f"{c}.{k}": v for c in self.rows for k, v in self.row_values(c).items()}
        flat.update({f"general.{k}": v for k, v in self.general_values().items()})
        settings.remember(self.drawing_key(), flat)

    def load_row(self, color, values, name):
        values = settings_for(values)
        row = self.rows[color]
        if values.get("mode") in MODES:
            row["mode"].set_selected(MODES.index(values["mode"]))
        for key, w in row["spins"].items():
            if key in values:
                w.set_value(float(values[key]))
        for key, (w, options) in row["picks"].items():
            if str(values.get(key)) in options:
                w.set_selected(options.index(str(values[key])))
        self.tell("info", f"{color}: loaded preset {name}")

    def presets(self):
        mode = lambda v: settings_for(v).get("mode") or v.get("job_type", "—")
        return presets_tab(self, KIND, [("Job type", mode), ("Resolution", lambda v: f"{v['dpi']} dpi" if "dpi" in v else "—"),
                                        ("Power %", lambda v: v.get("power", v.get("vector_power", "—"))),
                                        ("Speed %", lambda v: v.get("speed", v.get("vector_speed", "—")))],
                           "Load into General", self.load_general, "Save General as preset…",
                           self.general_values, self.refresh)

    def load_general(self, values, name):
        """A General preset loads as is; a color's preset fills the raster or vector half."""
        values = settings_for(values)
        half = values.get("mode") if values.get("mode") in ("raster", "vector") else None
        if half:
            values = dict(values, **{f"{half}_{k}": values.get(k) for k in ("speed", "power", "passes")})
        for key, (w, kind, args) in self.general.items():
            v = values.get(key)
            if v is None:
                continue
            if kind == "spin":
                w.set_value(float(v))
            elif str(v) in args:
                w.set_selected(args.index(str(v)))
        self.tell("info", f"General: loaded preset {name}")

    # ------------------------------------------------------------ buttons
    def build_buttons(self):
        main = Gtk.Box(spacing=8, homogeneous=True)
        self.pause_button = big_button("Pause", self.pause)
        for b in (big_button("Frame", self.frame), big_button("Start", self.start, "suggested-action"),
                  self.pause_button, big_button("Stop", lambda: self.work(self.backend.stop), "destructive-action")):
            main.append(b)
        tools = Gtk.Box(spacing=8)
        home = Gtk.Button(label="Home")
        home.connect("clicked", lambda *_: self.work(self.backend.home))
        control = Gtk.Button(label="Direct control…")
        control.connect("clicked", lambda *_: DirectControl(self.get_application(), self).present())
        tools.append(home)
        tools.append(control)
        return [main, tools]

    def console_hint(self):
        return "command for the laser (e.g. pause), Enter sends"

    def placement(self, bounds=None, absolute_shift=(0.0, 0.0)):
        """Absolute: where the design sits on the page. Relative: the job origin goes where the
        head is now. Returns (absolute, shift in mm) or None when the head's place is unknown."""
        g = self.general_values()
        if g["start_from"] == "Absolute":
            return True, absolute_shift
        at = self.backend.where()
        if at is None:
            self.tell("error", "Relative needs to know where the head is: press Home once, then jog")
            return None
        b = bounds or self.bounds()
        col = {"left": 0, "right": 2}.get(g["origin"].split()[-1], 1)
        row = {"top": 0, "bottom": 2}.get(g["origin"].split()[0], 1)
        return False, (at[0] - (b[0] + (b[2] - b[0]) * col / 2), at[1] - (b[1] + (b[3] - b[1]) * row / 2))

    def job(self, shift):
        """Each color as one or two layers: outlines and fills, with the settings they run with.
        A color on General runs outlines as vector and fills as raster with the General tab's
        settings; the General job type then decides which kinds run at all."""
        allow = {"combined": {"vector", "raster"}, "raster": {"raster"},
                 "vector": {"vector"}}[self.general_values()["job_type"]]
        move = lambda polys: [[(x + shift[0], y + shift[1]) for x, y in p] for p in polys]
        out = []
        for color, kinds in self.layers.items():
            prof = self.row_values(color)
            if prof["mode"] == "skip":
                continue
            # The user's rule (2026-10-31): red (#ff0000) cuts, every other color engraves, its lines
            # as the area they cover. That is what General does; a color set to vector or raster
            # in Color Mapping does that instead.
            mode = prof["mode"]
            if mode == "general":
                mode = "vector" if color.lower() == CUT_COLOR else "raster"
                prof = self.general_profile(mode)
            if mode == "raster":
                dot = 25.4 / int(prof.get("dpi", 500))
                parts = [(prof, [], kinds["fill"] + jobtools.line_areas(kinds["stroke"], kinds["width"], dot))]
            else:
                edges = [p if p[0] == p[-1] else p + [p[0]] for p in kinds["fill"]]
                parts = [(prof, kinds["stroke"] + edges, [])]
            out += [{"color": color, "profile": dict(p, mode=SENDS[p["mode"]]), "paths": move(s), "fills": move(f),
                     "page": self.page} for p, s, f in parts if p["mode"] in allow and (s or f)]
        return out

    def frame(self):
        where = self.placement()
        if where:
            paths = self.frame_paths(self.general_values()["frame_shape"], where[1])
            self.work(lambda: self.backend.frame(paths, where[0]))

    def start(self):
        where = self.placement()
        if not where:
            return
        job = self.job(where[1])
        if not job:
            self.tell("error", "nothing to run: every color is skipped or ruled out by the job type")
            return
        self.tell("info", ("absolute" if where[0] else "relative") + ": "
                  + ", ".join(f"{l['color']} → {l['profile']['mode']}" for l in job))
        self.work(lambda: self.backend.run(job, absolute=where[0]))

    def preview_job(self):
        return self.job((0.0, 0.0))

    def piece(self):
        """One batch piece: placed as Start would place it, sent, and held until the laser is idle."""
        where = self.placement()
        job = self.job(where[1]) if where else None
        if not job:
            return None

        def send():
            self.backend.run(job, absolute=where[0])
            self.backend.wait_idle()
        return send

    def start_point(self):
        return (self.page[0] or self.bed()[0], 0.0)  # home, the page's top-right corner

    def bed(self):
        w, h = (float(v.split()[0]) for v in str(self.cfg.get("bed", "508 x 305")).split("x"))
        return w, h

    # ------------------------------------------------------------ material test
    def test_settings(self):
        return {"speed_unit": "%", "fill": "raster",
                "fields": [("speed_lo", "Speed % from", 1, 100, 1, 0, 10), ("speed_hi", "Speed % to", 1, 100, 1, 0, 100),
                           ("dpi", "Raster dpi", 250, 1000, 250, 0, 500),
                           ("label_power", "Label power %", 0, 100, 1, 0, 15),
                           ("label_speed", "Label speed %", 1, 100, 1, 0, 60)]}

    def run_test(self, test, bounds, frame_only):
        """The grid near home (the bed's top-right corner, 10 mm in) when starting from Absolute,
        or with its job origin at the head when Relative."""
        t, cells, labels = test
        w = bounds[2] - bounds[0]
        where = self.placement(bounds, (self.bed()[0] - w - 10, 10.0))
        if not where:
            return
        dx, dy = where[1]
        move = lambda p: [(x + dx, y + dy) for x, y in p]
        if frame_only:
            box = move([(bounds[0], bounds[1]), (bounds[2], bounds[1]), (bounds[2], bounds[3]),
                        (bounds[0], bounds[3]), (bounds[0], bounds[1])])
            self.work(lambda: self.backend.frame([box], where[0]))
            return
        raster = t["kind"] == "raster"
        job = [{"color": f"{power:g}% {speed:g}%", "page": self.page,
                "profile": {"mode": "engrave" if raster else "cut", "power": power, "speed": speed, "passes": 1,
                            "dpi": t["dpi"]},
                "paths": [] if raster else [move(sq)], "fills": [move(sq)] if raster else []}
               for power, speed, sq in cells]
        job.append({"color": "labels", "page": self.page, "fills": [], "paths": [move(p) for p in labels],
                    "profile": {"mode": "cut", "power": t["label_power"], "speed": t["label_speed"], "passes": 1}})
        self.tell("info", f"material test: {len(cells)} squares ({t['kind']}), "
                  + ("absolute" if where[0] else "relative"))
        self.work(lambda: self.backend.run(job, absolute=where[0]))

    def pause(self):
        paused = self.pause_button.get_label() == "Pause"
        self.pause_button.set_label("Resume" if paused else "Pause")
        self.work(self.backend.pause if paused else self.backend.resume)


def settings_for(values):
    """Older saved settings used the sender's words; read them in the panel's words."""
    v = dict(values)
    v["mode"] = {"cut": "vector", "engrave": "raster"}.get(v.get("mode"), v.get("mode"))
    if v.get("direction") in ("top to bottom", "bottom to top"):  # the old choice: which end the rows start from
        v["direction"] = "left and right"
    return v


def contents(kinds):
    n, f = len(kinds["stroke"]), len(kinds["fill"])
    return ", ".join(x for x in (f"{n} outline{'s' * (n != 1)}" if n else "", f"{f} filled" if f else "") if x)


class DirectControl(Gtk.ApplicationWindow):
    """Move the head by hand: jog (buttons or arrow keys), Home, go to a spot, frame the design,
    test fire, and see where the head is. The H-Series has no motorised table, so there is no up and down."""

    STEPS = ["0.1", "1", "5", "10", "50"]

    def __init__(self, app, panel):
        super().__init__(application=app, title=f"Direct control — {panel.name}")
        self.panel = panel
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10)
        self.set_child(box)
        do, b = panel.work, panel.backend

        step_row = Gtk.Box(spacing=6)
        step_row.append(Gtk.Label(label="Step mm"))
        self.step = pick(self.STEPS, "5")
        step_row.append(self.step)
        box.append(step_row)
        step = lambda: float(self.STEPS[self.step.get_selected()])

        pad = Gtk.Grid(column_spacing=4, row_spacing=4, halign=Gtk.Align.CENTER)
        for label, c, r, dx, dy in (("↑", 1, 0, 0, -1), ("←", 0, 1, -1, 0), ("→", 2, 1, 1, 0), ("↓", 1, 2, 0, 1)):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _b, x=dx, y=dy: do(lambda: b.jog(x * step(), y * step())))
            pad.attach(btn, c, r, 1, 1)
        home = Gtk.Button(label="Home")
        home.connect("clicked", lambda *_: do(b.home))
        pad.attach(home, 1, 1, 1, 1)
        box.append(pad)

        goto = Gtk.Box(spacing=6)
        gx, gy = spin(0, 2000, 0.5, 1, 0), spin(0, 2000, 0.5, 1, 0)
        go = Gtk.Button(label="Go to")
        go.connect("clicked", lambda *_: do(lambda: b.go_to_mm(gx.get_value(), gy.get_value())))
        for w in (Gtk.Label(label="X mm"), gx, Gtk.Label(label="Y mm"), gy, go):
            goto.append(w)
        box.append(goto)

        tools = Gtk.Box(spacing=6)
        frame = Gtk.Button(label="Frame")
        frame.connect("clicked", lambda *_: panel.frame())
        stop = Gtk.Button(label="Stop")
        stop.connect("clicked", lambda *_: do(b.stop))
        tools.append(frame)
        tools.append(stop)
        box.append(tools)
        fire_row = Gtk.Box(spacing=6)
        power, ms = spin(1, 100, 1, 0, 5), spin(10, 2000, 10, 0, 100)
        fire = Gtk.Button(label="Test fire")
        fire.connect("clicked", lambda *_: do(lambda: b.test_fire(int(power.get_value()), int(ms.get_value()))))
        for w in (fire, power, Gtk.Label(label="% for"), ms, Gtk.Label(label="ms")):
            fire_row.append(w)
        box.append(fire_row)

        self.position = Gtk.Label(xalign=0)
        box.append(self.position)
        GLib.timeout_add(250, self.show_position)

        # The arrow keys jog one step while this window is open, except while typing in a box.
        arrows = {65361: (-1, 0), 65362: (0, -1), 65363: (1, 0), 65364: (0, 1)}
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)

        def pressed(_c, key, *_):
            if key not in arrows or isinstance(self.get_focus(), Gtk.Text):
                return False
            dx, dy = arrows[key]
            do(lambda: b.jog(dx * step(), dy * step()))
            return True
        keys.connect("key-pressed", pressed)
        self.add_controller(keys)

    def show_position(self):
        at = self.panel.backend.where() if self.panel.backend.connected else None
        self.position.set_text("Head: unknown (press Home)" if at is None else f"Head: X {at[0]:.2f}  Y {at[1]:.2f} mm")
        return self.get_visible() or not self.get_realized()


def first_laser(kind):
    return next(n for n, c in settings.lasers().items() if c.get("kind") == kind)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--laser")
    ap.add_argument("--control", action="store_true", help="open Direct control only")
    ap.add_argument("svg", nargs="?")
    args = ap.parse_args()
    name = args.laser or first_laser(KIND)

    def start(app):
        panel = FullSpectrumPanel(app, name, args.svg)
        if args.control:
            # Direct control on its own: the panel holds the connection and its log, unseen.
            win = DirectControl(app, panel)
            win.connect("close-request", lambda *_: panel.close() or False)
            win.present()
        else:
            panel.present()
    run_app("fstudio.fullspectrum", start)


if __name__ == "__main__":
    main()
