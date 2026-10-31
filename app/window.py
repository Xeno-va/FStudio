"""What both laser panels share: the window frame, the telemetry pane and log, the drawing
preview, and running laser commands off the screen's thread. Each laser's own panel
(fullspectrum.py, uvmarker.py) builds its settings and buttons on top of this."""
import datetime
import math
import time
import queue
import sys
import threading
from pathlib import Path

import cairo
import gi
import numpy as np

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import backends  # noqa: E402
import drawing  # noqa: E402
import jobtools  # noqa: E402
import settings  # noqa: E402

LOGS = Path(__file__).parent / "logs"
TELL_COLORS = {"tx": "#64b5f6", "rx": "#81c784", "status": "#d7ccc8", "info": "#bdbdbd", "error": "#ff6b6b"}


class LaserWindow(Gtk.ApplicationWindow):
    """Top: laser name, state, Reconnect. Middle: the drawing and the subclass's settings on
    the left, telemetry on the right. Bottom: the subclass's buttons."""

    def __init__(self, app, laser_name, svg_path):
        super().__init__(application=app, title=laser_name)
        self.set_default_size(1280, 720)
        self.name, self.cfg = laser_name, settings.lasers()[laser_name]
        self.layers, self.base_layers, self.page = {}, {}, (0, 0)
        # serial_value None: the serial is drawn as written, # signs and all, until Batch is open.
        self.serials, self.serial_value, self.batch_next = [], None, 1
        self.svg_path = Path(svg_path) if svg_path else None
        self.events = queue.Queue()
        LOGS.mkdir(exist_ok=True)
        self.log = open(LOGS / f"{datetime.date.today()}.log", "a")
        self.backend = backends.KINDS[self.cfg["kind"]](laser_name, self.cfg, self.tell)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10)
        self.set_child(box)

        top = Gtk.Box(spacing=8)
        title = Gtk.Label()
        title.set_markup(f"<b>{GLib.markup_escape_text(laser_name)}</b>")
        self.state = Gtk.Label(label="not connected", xalign=0, hexpand=True)
        reconnect = Gtk.Button(label="Reconnect")
        reconnect.connect("clicked", lambda *_: self.connect_laser())
        for w in (title, self.state, reconnect):
            top.append(w)
        box.append(top)

        # The settings side never shrinks below what it needs; telemetry takes what is left.
        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, position=960,
                          shrink_start_child=False, resize_end_child=False)
        box.append(split)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.append(Gtk.Label(label="Telemetry", xalign=0))
        self.telemetry = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.buffer = self.telemetry.get_buffer()
        for kind, color in TELL_COLORS.items():
            self.buffer.create_tag(kind, foreground=color)
        side.append(Gtk.ScrolledWindow(child=self.telemetry, vexpand=True, min_content_width=220))
        # Typed commands go straight to the laser; the reply lands in the telemetry above.
        self.command = Gtk.Entry(placeholder_text=self.console_hint())
        self.command.connect("activate", self.send_command)
        side.append(self.command)
        split.set_end_child(side)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.preview = Gtk.DrawingArea(content_height=240, hexpand=True, vexpand=True)
        self.preview.set_draw_func(self.draw_preview)
        main.append(Gtk.Frame(child=self.preview))
        under = Gtk.Box(spacing=8)
        self.drawing_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        simulate = Gtk.Button(label="Simulate", tooltip_text="play the job on the drawing as the laser would run it")
        simulate.connect("clicked", lambda *_: self.simulate())
        # Stop while it plays; once stopped or finished the same button clears it off the drawing.
        self.sim_stop = Gtk.Button(label="Stop", tooltip_text="stop the simulation, then clear it")
        self.sim_stop.connect("clicked", lambda *_: self.stop_simulation())
        # Simulation speed, 0.01x to 1000x real time on a log scale.
        self.sim_speed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -2, 3, 0.05)
        self.sim_speed.set_value(0)
        self.sim_speed.set_size_request(140, -1)
        self.sim_speed.set_draw_value(True)
        self.sim_speed.set_format_value_func(lambda _s, v: f"×{10 ** v:.2g}")
        self.sim_speed.set_tooltip_text("simulation speed")
        test = Gtk.Button(label="Material test…", tooltip_text="a grid of squares at stepped power and speed")
        test.connect("clicked", lambda *_: MaterialTest(self).present())
        self.batch_button = Gtk.Button(label="Batch…", tooltip_text="run the design once per serial number; "
                                       "mark where the number goes with # signs in a text, e.g. BK###")
        self.batch_button.connect("clicked", lambda *_: BatchWindow(self).present())
        for w in (self.drawing_label, simulate, self.sim_stop, self.sim_speed, test, self.batch_button):
            under.append(w)
        main.append(under)
        self.sim = None
        self.load_drawing()
        main.append(self.build_settings())
        split.set_start_child(main)

        for row in self.build_buttons():
            box.append(row)

        GLib.timeout_add(100, self.drain)
        self.connect("close-request", self.on_close)
        self.connect_laser()

    # Each laser's panel fills these in.
    def build_settings(self): raise NotImplementedError
    def build_buttons(self): raise NotImplementedError
    def bed_note(self): return ""
    def console_hint(self): return "type a command for the laser, Enter sends"
    def preview_job(self): return []  # the job as Start would send it, design where it sits on the page
    def start_point(self): return (0.0, 0.0)  # where the laser is before and after a job, page mm
    def test_settings(self): raise NotImplementedError  # the material test's power and speed ranges
    def run_test(self, job, bounds, frame_only): raise NotImplementedError

    def piece(self):
        """On the screen's thread: what one batch piece sends, as a function the batch runs on
        its own thread until the laser has finished it. None when it cannot run."""
        job = self.preview_job()
        return (lambda: self.backend.run(job)) if job else None

    # ------------------------------------------------------------ command line
    def send_command(self, entry):
        text = entry.get_text().strip()
        if text:
            entry.set_text("")
            self.work(lambda: self.backend.console(text))

    # ------------------------------------------------------------ simulation
    def simulate(self):
        """Work out the head's whole path for the job as it is set up now, then play it from the
        start: what the laser burns stays drawn, the head moves over the drawing."""
        self.clear_simulation()
        job = self.preview_job()
        if not job:
            self.tell("error", "nothing to simulate: every color is unticked or skipped")
            return
        self.drawing_label.set_text("Working out the job…")

        def work():
            try:
                t, x, y, on, eng, pitch, note = self.backend.simulate(job)
            except Exception as e:
                self.tell("error", f"simulate: {type(e).__name__}: {e}")
                return
            GLib.idle_add(self.start_simulation, t, x, y, on, eng, pitch, note)
        threading.Thread(target=work, daemon=True).start()

    ENGRAVING_FASTER = 10  # engravings and fills play this much faster than cuts, so cuts can be watched

    def start_simulation(self, t, x, y, on, eng, pitch, note):
        # The clock runs on playback time: real job time, with engraving stretches shortened.
        dt = np.diff(t, prepend=0.0)
        play = np.cumsum(np.where(eng, dt / self.ENGRAVING_FASTER, dt))
        self.sim = {"t": play, "real": t, "x": x, "y": y, "on": on, "eng": eng, "pitch": pitch, "note": note, "clock": 0.0,
                    "last": time.monotonic(), "drawn": 0, "surface": None, "key": None, "running": True}
        # Start at a speed that plays the whole job in about half a minute, slower than real time
        # for a short job.
        self.sim_speed.set_value(min(3.0, max(-2.0, math.log10(max(play[-1], 1e-9) / 30))))
        GLib.timeout_add(30, self.step_simulation)

    def stop_simulation(self):
        if self.sim and self.sim["running"]:
            self.sim["running"] = False
            self.show_sim_time("stopped")
            self.sim_stop.set_label("Clear")
        else:
            self.clear_simulation()

    def clear_simulation(self):
        """Back to the plain drawing."""
        if self.sim:
            self.sim["running"] = False
        self.sim = None
        self.sim_stop.set_label("Stop")
        self.show_drawing_label()
        self.preview.queue_draw()

    def step_simulation(self):
        sim = self.sim
        if not sim or not sim["running"]:
            return False
        now = time.monotonic()
        sim["clock"] = min(sim["t"][-1], sim["clock"] + (now - sim["last"]) * 10 ** self.sim_speed.get_value())
        sim["last"] = now
        done = sim["clock"] >= sim["t"][-1]
        self.show_sim_time("done" if done else "simulating")
        self.preview.queue_draw()
        if done:
            sim["running"] = False
            self.sim_stop.set_label("Clear")
        return not done

    def show_sim_time(self, state):
        sim = self.sim
        note = f"  ·  {sim['note']}" if sim["note"] else ""
        real = float(np.interp(sim["clock"], sim["t"], sim["real"]))
        self.drawing_label.set_text(f"{state.capitalize()}: {backends.fmt_time(real)} of "
                                    f"{backends.fmt_time(sim['real'][-1])}{note}")

    def view(self, w, h):
        """Scale and offset fitting the page, and the head's whole path while simulating (the
        Full Spectrum's home can sit off a page smaller than the bed)."""
        x0, y0, x1, y1 = 0.0, 0.0, self.page[0], self.page[1]
        if self.sim:
            x0, y0 = min(x0, self.sim["x"].min()), min(y0, self.sim["y"].min())
            x1, y1 = max(x1, self.sim["x"].max()), max(y1, self.sim["y"].max())
        s = min((w - 20) / max(x1 - x0, 1e-6), (h - 20) / max(y1 - y0, 1e-6))
        return s, (w - (x1 - x0) * s) / 2 - x0 * s, (h - (y1 - y0) * s) / 2 - y0 * s

    def draw_simulation(self, cr, w, h, s, ox, oy):
        """Over the faded drawing: everything burned so far (kept on its own image, so each frame
        only adds the new stretch), and the head, red while the laser is on."""
        sim = self.sim
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.paint()
        key = (w, h, s, ox, oy)
        if sim["key"] != key:
            sim["surface"], sim["key"], sim["drawn"] = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h), key, 0
        n = int(np.searchsorted(sim["t"], sim["clock"], side="right"))
        if n > sim["drawn"] + 1:
            burn = cairo.Context(sim["surface"])
            burn.set_source_rgb(0.45, 0.2, 0.05)
            xs, ys, on = sim["x"] * s + ox, sim["y"] * s + oy, sim["on"]
            lit = np.flatnonzero(on[max(1, sim["drawn"]):n]) + max(1, sim["drawn"])
            # Cuts and outlines about as wide as the beam (0.1 mm), never fatter than 1.2 px so small
            # lettering stays readable. Engraving lines exactly as wide as their spacing with square
            # ends, so a fill covers its shape and nothing past its edges.
            for engraving, width, cap in ((False, min(1.2, max(0.1 * s, 0.6)), cairo.LINE_CAP_ROUND),
                                          (True, max(sim["pitch"] * s, 0.3), cairo.LINE_CAP_BUTT)):
                burn.set_line_width(width)
                burn.set_line_cap(cap)
                for i in lit[sim["eng"][lit] == engraving]:
                    burn.move_to(xs[i - 1], ys[i - 1])
                    burn.line_to(xs[i], ys[i])
                burn.stroke()
            sim["drawn"] = n - 1
        cr.set_source_surface(sim["surface"], 0, 0)
        cr.paint()
        # The head, between the last point passed and the next.
        i = min(max(n, 1), len(sim["t"]) - 1)
        t0, t1 = sim["t"][i - 1], sim["t"][i]
        f = 0.0 if t1 <= t0 else min(1.0, (sim["clock"] - t0) / (t1 - t0))
        hx = (sim["x"][i - 1] + f * (sim["x"][i] - sim["x"][i - 1])) * s + ox
        hy = (sim["y"][i - 1] + f * (sim["y"][i] - sim["y"][i - 1])) * s + oy
        firing = sim["on"][i] and sim["clock"] < sim["t"][-1]
        cr.set_source_rgb(0.9, 0.1, 0.1) if firing else cr.set_source_rgb(0.3, 0.3, 0.3)
        cr.arc(hx, hy, 4, 0, 2 * math.pi)
        cr.fill() if firing else cr.stroke()

    # ------------------------------------------------------------ telemetry
    def tell(self, kind, text):
        self.events.put((kind, text))

    def drain(self):
        while not self.events.empty():
            kind, text = self.events.get()
            if kind == "_state":
                self.state.set_text(text)
                continue
            stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            line = f"{stamp} {kind:6} {text}\n"
            self.buffer.insert_with_tags_by_name(self.buffer.get_end_iter(), line, kind)
            self.telemetry.scroll_to_iter(self.buffer.get_end_iter(), 0, False, 0, 0)
            self.log.write(f"{datetime.date.today()} {line}")
            self.log.flush()
        return True

    def work(self, fn):
        """Run a laser command off the screen's thread; errors go to telemetry."""
        if not self.backend.connected:
            self.tell("error", "not connected to the laser")
            return

        def run():
            try:
                fn()
            except Exception as e:
                self.tell("error", f"{type(e).__name__}: {e}")
        threading.Thread(target=run, daemon=True).start()

    def connect_laser(self):
        self.backend.close()
        self.tell("_state", "connecting…")

        def connect():
            try:
                self.backend.connect()
                self.tell("_state", "connected")
            except Exception as e:
                self.tell("error", f"could not connect: {e}")
                self.tell("_state", "not connected")
        threading.Thread(target=connect, daemon=True).start()

    # ------------------------------------------------------------ drawing
    def load_drawing(self):
        if not self.svg_path or not self.svg_path.exists():
            self.drawing_label.set_text("No drawing: send one from Inkscape (Extensions → Laser).")
            return
        self.base_layers, self.page = drawing.read(str(self.svg_path))
        self.backend.page = self.page
        # Serial numbers: texts with # signs where the number goes. Inkscape's plugin turns text into outlines and
        # leaves the drawing as it was (with its text) beside it, so look there first.
        raw = self.svg_path.with_name(self.svg_path.stem + ".raw.svg")
        self.serials = jobtools.serial_fields(raw if raw.exists() else self.svg_path)
        saved = settings.remembered(self.batch_key())
        self.batch_next = int(saved.get("next", 1))
        self.set_serial(None)
        self.show_drawing_label()

    def show_drawing_label(self):
        if not self.svg_path or not self.svg_path.exists():
            return
        note = self.bed_note()
        serial = (f"  ·  serial {self.serial_name(self.serial_value)}" if self.serial_value is not None
                  else f"  ·  serial {self.serials[0]['text']} (numbered in Batch)") if self.serials else ""
        self.drawing_label.set_text(f"{self.svg_path.stem}: {self.page[0]:.0f} × {self.page[1]:.0f} mm, "
                                    f"{len(self.layers)} colors{serial}" + (f"  ·  {note}" if note else ""))

    def serial_name(self, value, index=0):
        return jobtools.serial_text(self.serials[index], value)

    def batch_key(self):
        return f"{self.drawing_key()} - batch"

    def set_serial(self, value):
        """Draw the serial numbers with this value (several on the page count on from it, top to
        bottom), so Frame, Simulate and Start all use it. The value and the text's own color
        decide how it runs, like anything else of that color."""
        self.serial_value = value
        self.layers = {c: {k: list(v) for k, v in kinds.items()} for c, kinds in self.base_layers.items()}
        for i, field in enumerate(self.serials):
            layer = self.layers.setdefault(field["color"], {"stroke": [], "width": [], "fill": []})
            layer["fill"] += jobtools.serial_outline(field, None if value is None else value + i)
        if self.sim:
            self.clear_simulation()
        self.preview.queue_draw()

    def drawing_key(self):
        return f"{self.name} - {self.svg_path.stem}" if self.svg_path else self.name

    def bounds(self):
        pts = [pt for k in self.layers.values() for p in k["stroke"] + k["fill"] for pt in p]
        if not pts:
            return (0, 0, 10, 10)
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def frame_paths(self, shape, shift=(0.0, 0.0)):
        """What Frame traces (page mm): the bounding box as one closed path, or every outline
        and filled shape's edge in the design."""
        dx, dy = shift
        if shape == "bounding box":
            x0, y0, x1, y1 = self.bounds()
            paths = [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]
        else:
            paths = [p for k in self.layers.values() for p in k["stroke"]]
            paths += [p if p[0] == p[-1] else p + [p[0]] for k in self.layers.values() for p in k["fill"]]
        return [[(x + dx, y + dy) for x, y in p] for p in paths]

    def draw_preview(self, area, cr, w, h):
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        if not self.layers or not self.page[0]:
            return
        # The page at its true shape, centered in whatever space the preview has.
        s, ox, oy = self.view(w, h)
        cr.save()
        cr.translate(ox, oy)
        cr.set_source_rgb(0.85, 0.85, 0.85)
        cr.rectangle(0, 0, self.page[0] * s, self.page[1] * s)
        cr.stroke()
        self.draw_marks(cr, s)
        for color, kinds in self.layers.items():
            r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
            cr.set_source_rgb(r, g, b)
            for poly in kinds["fill"]:
                cr.move_to(poly[0][0] * s, poly[0][1] * s)
                for x, y in poly[1:]:
                    cr.line_to(x * s, y * s)
                cr.close_path()
            cr.set_fill_rule(0)
            cr.fill()
            cr.set_line_width(1)
            for poly in kinds["stroke"]:
                cr.move_to(poly[0][0] * s, poly[0][1] * s)
                for x, y in poly[1:]:
                    cr.line_to(x * s, y * s)
                cr.stroke()
        cr.restore()
        if self.sim:
            self.draw_simulation(cr, w, h, s, ox, oy)

    def draw_marks(self, cr, s):
        """Anything a laser's panel draws on the page under the design (home, lens center)."""

    def on_close(self, *_):
        self.backend.close()
        self.log.close()
        return False


class MaterialTest(Gtk.Window):
    """One job of squares: power steps down the rows, speed across the columns, each row and
    column labelled. Each laser's panel says what its power and speed mean and how it runs."""

    def __init__(self, panel):
        super().__init__(title=f"Material test — {panel.name}", transient_for=panel)
        self.panel = panel
        spec = panel.test_settings()  # {"speed_unit", "fields": [(key, label, lo, hi, step, digits, default)], "fill": label}
        saved = settings.remembered(f"{panel.name} - material test")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10)
        self.set_child(box)
        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        common = [("rows", "Rows (power steps)", 1, 20, 1, 0, 5), ("power_hi", "Power % from", 0, 100, 1, 0, 100),
                  ("power_lo", "Power % to", 0, 100, 1, 0, 20), ("cols", "Columns (speed steps)", 1, 20, 1, 0, 5),
                  ("size", "Square mm", 1, 50, 0.5, 1, 5), ("gap", "Gap mm", 0, 20, 0.5, 1, 2)]
        self.fields = {}
        for r, (key, label, lo, hi, step, digits, default) in enumerate(common + spec["fields"]):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            w = spin(lo, hi, step, digits, saved.get(key, default))
            w.connect("value-changed", lambda *_: self.remember())
            grid.attach(w, 1, r, 1, 1)
            self.fields[key] = w
        r = len(common) + len(spec["fields"])
        grid.attach(Gtk.Label(label="Squares", xalign=0), 0, r, 1, 1)
        self.kinds = [spec["fill"], "outline"]
        self.kind = pick(self.kinds, saved.get("kind", spec["fill"]))
        self.kind.connect("notify::selected", lambda *_: self.remember())
        grid.attach(self.kind, 1, r, 1, 1)
        self.speed_unit = spec["speed_unit"]
        box.append(grid)
        self.size = Gtk.Label(xalign=0)
        box.append(self.size)
        buttons = Gtk.Box(spacing=8, homogeneous=True)
        for b in (big_button("Frame", lambda: self.go(True)), big_button("Start", lambda: self.go(False), "suggested-action"),
                  big_button("Stop", lambda: panel.work(panel.backend.stop), "destructive-action")):
            buttons.append(b)
        box.append(buttons)
        self.remember()

    def values(self):
        v = {k: spin_value(w) for k, w in self.fields.items()}
        v["kind"] = self.kinds[self.kind.get_selected()]
        v["speed_unit"] = self.speed_unit
        return v

    def remember(self):
        import jobtools
        v = self.values()
        settings.remember(f"{self.panel.name} - material test", {k: x for k, x in v.items() if k != "speed_unit"})
        w, h = jobtools.grid_size(v)
        self.size.set_text(f"Grid with labels: {w:.0f} × {h:.0f} mm, {int(v['rows']) * int(v['cols'])} squares")

    def go(self, frame_only):
        import jobtools
        v = self.values()
        cells, labels = jobtools.test_grid(v, (0.0, 0.0))
        w, h = jobtools.grid_size(v)
        self.panel.run_test((v, cells, labels), (0.0, 0.0, w, h), frame_only)


def on_main(fn):
    """Run fn on the screen's thread and wait for its result (from a worker thread)."""
    done, box = threading.Event(), {}

    def call():
        try:
            box["value"] = fn()
        except Exception as e:
            box["error"] = e
        done.set()
        return False
    GLib.idle_add(call)
    done.wait()
    if "error" in box:
        raise box["error"]
    return box["value"]


class BatchWindow(Gtk.Window):
    """Run the design once per serial number: from the first value, for the number of pieces
    asked, counting on by itself. As each piece finishes the drawing changes to the next number,
    and the batch waits for Next (or Enter) while the next piece is loaded. The next number is
    remembered for next time."""

    def __init__(self, panel):
        super().__init__(title=f"Batch — {panel.name}", transient_for=panel)
        self.panel, self.stopping, self.go_on = panel, False, threading.Event()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=10,
                      margin_start=10, margin_end=10)
        self.set_child(box)
        fields = panel.serials
        per = len(fields)
        if not fields:
            # How to mark one, said where it is needed.
            box.append(Gtk.Label(xalign=0, wrap=True, max_width_chars=48, label=(
                "No serial number in this drawing.\n\nIn Inkscape, write a text with # signs where the "
                "number goes, one # per digit: #### for 0001, 0002…, or BK### for BK001, BK002…. "
                "Letters before or after the # signs stay as typed. Then send the drawing again.")))
            return
        box.append(Gtk.Label(xalign=0, label=f"Serial number on the page: {fields[0]['text']}" +
                             (f" and {per - 1} more, counting on from it" if per > 1 else "")))
        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        # Only the number: the # signs are counted, everything else in the text stays as drawn.
        self.top = 10 ** fields[0]["width"] - 1
        self.first = spin(0, self.top, 1, 0, min(panel.batch_next, self.top))
        self.first.set_tooltip_text("the number for the first piece")
        self.pieces = spin(1, 100000, 1, 0, 10)
        self.last = Gtk.Label(xalign=0)
        for r, (label, w) in enumerate((("First", self.first), ("Pieces", self.pieces), ("Last", self.last))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            grid.attach(w, 1, r, 1, 1)
        box.append(grid)
        self.status = Gtk.Label(xalign=0, wrap=True)
        box.append(self.status)
        buttons = Gtk.Box(spacing=8, homogeneous=True)
        self.start_b = big_button("Start batch", self.start, "suggested-action")
        self.next_b = big_button("Next piece", self.next_piece)
        self.next_b.set_sensitive(False)
        for b in (self.start_b, self.next_b, big_button("Stop", self.stop, "destructive-action")):
            buttons.append(b)
        box.append(buttons)
        self.first.connect("value-changed", lambda *_: self.show_last())
        self.pieces.connect("value-changed", lambda *_: self.show_last())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, key, *_: key in (65293, 65421) and self.next_b.get_sensitive()
                     and (self.next_piece() or True))
        self.add_controller(keys)
        self.connect("close-request", self.closed)
        self.show_last()

    def closed(self, *_):
        """Leaving Batch stops it and puts the drawing back to the # signs."""
        self.stopping = True
        self.go_on.set()
        self.panel.set_serial(None)
        self.panel.show_drawing_label()
        return False

    def show_last(self):
        panel, first = self.panel, int(self.first.get_value())
        panel.set_serial(first)
        panel.show_drawing_label()
        last = first + int(self.pieces.get_value()) * len(panel.serials) - 1
        self.last.set_text(f"{last:0{panel.serials[0]['width']}d}")
        over = last > self.top
        self.status.set_text(f"Too many: {last} needs more digits than the {panel.serials[0]['width']} # signs "
                             "drawn." if over else "")
        self.start_b.set_sensitive(not over)

    def say(self, text):
        GLib.idle_add(self.status.set_text, text)

    def start(self):
        panel = self.panel
        if not panel.backend.connected:
            panel.tell("error", "not connected to the laser")
            return
        first, n, per = int(self.first.get_value()), int(self.pieces.get_value()), len(panel.serials)
        self.stopping = False
        self.start_b.set_sensitive(False)

        def run():
            try:
                on_main(lambda: panel.set_serial(first) or panel.show_drawing_label())
                for k in range(n):
                    value = first + k * per
                    name = panel.serial_name(value)
                    send = on_main(panel.piece)
                    if send is None:
                        break
                    self.say(f"Piece {k + 1} of {n}: {name} — running")
                    panel.tell("info", f"batch piece {k + 1} of {n}: {name}")
                    send()
                    # Counted as soon as it has run, so a stop never repeats a number, and the
                    # drawing moves on to the next number at once, so what Next will run is on screen.
                    settings.remember(panel.batch_key(), {"next": value + per})
                    on_main(lambda: panel.set_serial(value + per) or panel.show_drawing_label())
                    if self.stopping or k == n - 1:
                        break
                    self.say(f"{name} done. Next is {panel.serial_name(value + per)}, on the drawing now: "
                             "load the next piece and press Next (or Enter).")
                    GLib.idle_add(self.next_b.set_sensitive, True)
                    self.go_on.wait()
                    self.go_on.clear()
                    if self.stopping:
                        break
                done = "Stopped" if self.stopping else "Batch finished"
                # The First box takes the next number (which redraws and clears the message), then the message.
                panel.batch_next = panel.serial_value
                GLib.idle_add(self.first.set_value, min(panel.serial_value, self.top))
                self.say(f"{done}. Next: {panel.serial_name(panel.serial_value)}")
            except Exception as e:
                panel.tell("error", f"batch: {type(e).__name__}: {e}")
                self.say(f"Stopped by an error: {e}")
            finally:
                GLib.idle_add(self.start_b.set_sensitive, True)
                GLib.idle_add(self.next_b.set_sensitive, False)
        threading.Thread(target=run, daemon=True).start()

    def next_piece(self):
        self.next_b.set_sensitive(False)
        self.go_on.set()

    def stop(self):
        """Stop the laser now and end the batch; the piece it was on stays counted."""
        self.stopping = True
        self.go_on.set()
        self.panel.work(self.panel.backend.stop)


def big_button(label, handler, css=None):
    b = Gtk.Button(label=label)
    b.set_size_request(-1, 48)
    if css:
        b.add_css_class(css)
    b.connect("clicked", lambda *_: handler())
    return b


def spin(lo, hi, step, digits, value):
    w = Gtk.SpinButton.new_with_range(lo, hi, step)
    w.set_digits(digits)
    w.set_value(float(value))
    return w


def pick(options, value):
    w = Gtk.DropDown.new_from_strings(options)
    if str(value) in options:
        w.set_selected(options.index(str(value)))
    return w


def spin_value(w):
    return int(w.get_value()) if w.get_digits() == 0 else w.get_value()


def run_app(app_id, make):
    """Start a panel. Every send opens its own window; never hand off to one already open."""
    app = Gtk.Application(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", make)
    app.run([sys.argv[0]])


# ------------------------------------------------------------ presets
# A preset is a named set of settings saved as a small text file under the laser's
# presets folder; both panels list them on a Presets tab and offer them on every color.

def preset_names(kind):
    return list(settings.profiles(kind))


def read_preset(kind, name):
    return settings.read_profile(settings.profiles(kind)[name])


def save_preset_dialog(parent, kind, values, after):
    """Ask for a name, then save the given settings as a preset under it."""
    dialog = Gtk.Window(title="Save preset", transient_for=parent, modal=True)
    box = Gtk.Box(spacing=8, margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
    entry = Gtk.Entry(placeholder_text="preset name, e.g. 3mm birch cut", width_chars=32)
    ok = Gtk.Button(label="Save")

    def done(*_):
        name = "".join(ch if ch.isalnum() or ch in " -_" else "-" for ch in entry.get_text()).strip()
        if name:
            folder = settings.PROFILES / settings.KIND_FOLDER.get(kind, kind)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{name}.txt").write_text("".join(f"{k} = {v}\n" for k, v in values().items()))
            parent.tell("info", f"saved preset {name}")
            after()
        dialog.close()
    ok.connect("clicked", done)
    entry.connect("activate", done)
    box.append(entry)
    box.append(ok)
    dialog.set_child(box)
    dialog.present()


def presets_tab(parent, kind, columns, load_label, on_load, save_label, save_values, after):
    """The Presets tab: every saved preset with the columns asked for, Load and Delete on each,
    and a Save button underneath. columns: [(heading, function of the preset's values)]."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_start=6,
                  margin_end=6, margin_bottom=6)
    grid = Gtk.Grid(column_spacing=14, row_spacing=2)
    for c, text in enumerate(["Preset"] + [h for h, _ in columns]):
        lab = Gtk.Label(xalign=0)
        lab.set_markup(f"<b>{text}</b>")
        grid.attach(lab, c, 0, 1, 1)
    names = preset_names(kind)
    for r, name in enumerate(names, start=1):
        values = read_preset(kind, name)
        grid.attach(Gtk.Label(label=name, xalign=0), 0, r, 1, 1)
        for c, (_h, fn) in enumerate(columns, start=1):
            grid.attach(Gtk.Label(label=str(fn(values)), xalign=0), c, r, 1, 1)
        load = Gtk.Button(label=load_label)
        load.connect("clicked", lambda _b, v=values, n=name: on_load(v, n))
        grid.attach(load, len(columns) + 1, r, 1, 1)
        delete = Gtk.Button(label="Delete")
        delete.connect("clicked", lambda _b, n=name: (Gio.File.new_for_path(str(settings.profiles(kind)[n])).trash(None),
                                                      parent.tell("info", f"deleted preset {n}"), after()))
        grid.attach(delete, len(columns) + 2, r, 1, 1)
    if not names:
        grid.attach(Gtk.Label(label="No presets saved yet.", xalign=0), 0, 1, 3, 1)
    # The list scrolls inside the tab, so a long list never squeezes the drawing.
    box.append(Gtk.ScrolledWindow(child=grid, vexpand=True, min_content_height=150,
                                  hscrollbar_policy=Gtk.PolicyType.NEVER))
    save = Gtk.Button(label=save_label, halign=Gtk.Align.START)
    save.connect("clicked", lambda *_: save_preset_dialog(parent, kind, save_values, after))
    box.append(save)
    return box


def save_preset_button(parent, kind, values, after):
    """A small save button beside a color's preset list."""
    b = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Save as preset…")
    b.connect("clicked", lambda *_: save_preset_dialog(parent, kind, values, after))
    return b


def preset_picker(kind, on_pick):
    """A "Load preset…" dropdown for one color."""
    w = Gtk.DropDown.new_from_strings(["Load preset…"] + preset_names(kind))

    def picked(*_):
        i = w.get_selected()
        if i > 0:
            name = preset_names(kind)[i - 1]
            on_pick(read_preset(kind, name), name)
            w.set_selected(0)
    w.connect("notify::selected", picked)
    return w


def replace_page(notebook, index, child, label):
    """Rebuild one tab in place, keeping the tab the user is on."""
    current = notebook.get_current_page()
    notebook.remove_page(index)
    notebook.insert_page(child, Gtk.Label(label=label), index)
    notebook.set_current_page(current)
