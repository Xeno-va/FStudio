"""One connection class per kind of laser, all speaking the same few verbs.

Every class reports what it does through `tell(kind, text)`: "tx" for what
was sent, "rx" for what came back, "status" for decoded laser status,
"info" and "error". The panel shows these live and writes them to the log.
"""
import math
import socket
import struct
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

# The H-Series' motion limits and settings, in the layout of Full Spectrum's own model file.
FS_MODEL = HERE / "machine-h-series.xml"


class Backend:
    def __init__(self, name, cfg, tell):
        self.name, self.cfg, self.tell = name, cfg, tell
        self.connected = False

    # Each verb runs on a worker thread; the panel never waits on it.
    def connect(self): raise NotImplementedError
    def run(self, job): raise NotImplementedError
    def pause(self): self.tell("info", "pause is not available on this laser yet")
    def resume(self): self.tell("info", "resume is not available on this laser yet")
    def stop(self): self.tell("info", "stop is not available on this laser yet")
    def test_fire(self, power_pct, ms): self.tell("info", "test fire is not available on this laser yet")
    def frame(self, paths): self.tell("info", "frame is not available on this laser yet")
    def home(self): self.tell("info", "home is not available on this laser yet")
    def console(self, text): self.tell("info", "typed commands are not available on this laser yet")
    def close(self): pass

    # The simulation and the run share the same job building, so what plays is what is sent.
    def simulate(self, job):
        """The head's path through the whole job: (t, x, y, on, engraving, pitch, note), one sample
        per point, t in seconds from the start, x and y in page mm, on = the laser was on getting
        to this point, engraving = this stretch is part of an engraving or fill; pitch is the mm
        between engraving lines, so they can be drawn as wide as they really are."""
        raise NotImplementedError


class FullSpectrum(Backend):
    """The H-Series, through the sender copied from RetinaEngrave. UNVERIFIED on the laser."""

    def connect(self):
        import find_fsl
        self.load_model()
        fsl = self.fsl
        ip = str(self.cfg.get("address", "auto"))
        if ip == "auto":
            self.tell("tx", "locator broadcast (UDP 23)")
            hits = [h for h in find_fsl.find() if "FSL Laser" in h[1]]
            for h in hits:
                self.tell("rx", f"{h[0]}: {h[1]}")
            if not hits:
                raise IOError("no Full Spectrum laser answered on the network")
            ip = hits[0][0]
        self.tell("tx", f"connect {ip}:{fsl.PORT_CMD}")
        self.laser = fsl.Laser(ip)
        self.tell("rx", self.laser.greeting.strip() or "(no greeting)")
        self.tell("tx", "startup settings (pass: idle 5 + init)")
        self.tell("rx", self.laser.init(self.model).strip())
        self.connected = True
        threading.Thread(target=self._status_feed, daemon=True).start()

    def _status_feed(self):
        names = ["SerialNumber", "Mode", "PanelMode", "Slow", "Status", "BufferSize", "LastAccepted",
                 "LastRejected", "RejectReason", "BufferFull", "Timestamp", "X", "Y", "Z",
                 "MarkX", "MarkY", "MarkZ", "Crc32", "Runtime", "Key", "ID"]
        fmt = "<IhhiiiBBBBI3i3iIIII"
        size = struct.calcsize(fmt)
        sock = self.laser.status
        sock.settimeout(1.0)
        last = None
        while self.connected:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                self.tell("error", f"status feed closed: {e}")
                return
            if len(data) < size:
                self.tell("rx", f"status (short, {len(data)} bytes): {data.hex(' ')}")
                continue
            vals = dict(zip(names, struct.unpack_from(fmt, data)))
            self.position = (vals["X"], vals["Y"])
            self.state = (vals["Mode"], vals["Status"])
            self.idle_state = getattr(self, "idle_state", None) or self.state
            line = (f"mode {vals['Mode']} status {vals['Status']} buffer {vals['BufferSize']} "
                    f"pos {vals['X']},{vals['Y']} accepted {vals['LastAccepted']} "
                    f"rejected {vals['LastRejected']} reason {vals['RejectReason']}")
            if line != last:
                self.tell("status", line)
                last = line

    def run(self, job, absolute=True):
        """absolute: home first and run where the design sits on the page. Relative: the panel
        has already moved the design so its job origin sits where the head is now."""
        self.start_from(absolute)
        engrave = [layer for layer in job if layer["profile"].get("mode") == "engrave" and not sweeps(layer)]
        cut_paths = self.cut_paths(job)
        spi = self.model.StepsPerInch[0] / 25.4
        for layer in engrave:
            dpi = int(layer["profile"].get("dpi", 500))  # RetinaEngrave's default job resolution
            rows, corner = rasterize(layer["fills"], dpi, with_corner=True)
            if not rows:
                continue
            p = layer["profile"]
            data = self.fsl.engrave_job(rows, self.model, p.get("speed", 50), p.get("power", 0), dpi=dpi,
                                        ceiling_pct=self.ceiling())
            # An engraving runs from wherever the head is, so put it on the image's top-left
            # corner first, as RetinaEngrave's Move To does from the position the laser reports.
            self.go_to((corner[0] * spi, corner[1] * spi))
            for n in range(int(p.get("passes", 1))):
                if n:
                    self.go_to((corner[0] * spi, corner[1] * spi))
                self.tell("tx", f"engrave job, pass {n + 1}, {len(rows)} rows, {len(data)} bytes")
                self.laser.send_job(data)
                self.tell("rx", "engrave job accepted")
                self.wait_idle()
        if cut_paths:
            self.go_to(self.home_steps())
            # Decided (2026-10-30): the page is the bed and the head parks at its top-right
            # corner, so every job starts and ends there and positions are measured from it.
            # UNVERIFIED on the laser: that the controller runs a job from where the head is
            # (as RetinaEngrave's planning assumes) and that +x is rightward on the bed.
            recs = self.fsl.cut_records(cut_paths, self.model, home=self.home_steps())
            # The header carries the drawing's size; the job runs from the laser's origin, so its
            # far corner is the size. UNVERIFIED what the laser does with it. RetinaEngrave sent no
            # vector-power command before this job (captured), so neither do we.
            w = max(max(xs) for xs, ys, *_ in cut_paths) / self.model.StepsPerInch[0]
            h = max(max(ys) for xs, ys, *_ in cut_paths) / self.model.StepsPerInch[1]
            data = self.fsl.cut_job(recs, self.model, w, h, vector_pct=self.ceiling())
            self.tell("tx", f"cut job, {len(recs)} steps ({len(recs) * self.fsl.TICK:.0f} s), {len(data)} bytes")
            self.tell("rx", (self.laser.send_job(data) or "").strip())

    def load_model(self):
        if not hasattr(self, "model"):
            import fsl
            self.fsl, self.model = fsl, fsl.load_model(FS_MODEL)
        return self.model

    def cut_order(self, job):
        """Every path the head draws with the laser on, as (page-mm path, profile). First the
        engravings swept up and down or diagonally, as lines one dot apart; then the cuts, each
        color's kerf applied, inside cuts before the outside cuts around them across all colors,
        so a part never drops through the honeycomb before its holes are cut."""
        import jobtools
        import rkjob
        swept, pairs = [], []
        for layer in job:
            p = layer["profile"]
            angle = sweeps(layer)
            if angle is not None and layer["fills"]:
                # RetinaEngrave's engrave job only sweeps left and right, so any other direction is
                # drawn as fill lines in a cut job. Its speed is a share of the cutting top speed.
                lines = rkjob.hatch(layer["fills"], 25.4 / int(p.get("dpi", 500)), angle)
                swept += [(line, p) for line in lines]
            elif p.get("mode") != "engrave":
                pairs += [(path, p) for path in jobtools.kerf_offset(layer["paths"], float(p.get("kerf", 0)))]
        order = jobtools.inside_first([path for path, _p in pairs])
        index = {id(path): prof for path, prof in pairs}
        return swept + [(path, index[id(path)]) for path in order]

    def cut_paths(self, job):
        """The cut order in the sender's terms: steps, speed, power byte, passes."""
        m = self.load_model()
        spi = m.StepsPerInch[0] / 25.4
        return [([x * spi for x, y in path], [y * spi for x, y in path],
                 p.get("speed", 50) / 100.0 * m.VectorMaxVelocity[0], round(255 * p.get("power", 0) / 100.0),
                 p.get("passes", 1)) for path, p in self.cut_order(job)]

    def simulate(self, job):
        """Left-and-right engravings first, row by row, then everything in the cut job from the
        motion planner's own step records, every half millisecond, from home and back.
        UNVERIFIED: engraving rows are timed as if RetinaEngrave's engraving speed counted motor
        steps per second, as cutting speed does."""
        import numpy as np
        m = self.load_model()
        spmm = m.StepsPerInch[0] / 25.4
        home = self.home_steps()
        t, x, y, on, eng = [0.0], [home[0] / spmm], [home[1] / spmm], [False], [False]

        def go(px, py, burn, speed_mm_s, engraving=True):
            t.append(t[-1] + math.dist((x[-1], y[-1]), (px, py)) / speed_mm_s)
            x.append(px), y.append(py), on.append(burn), eng.append(engraving)
        rapid = m.RapidVectorMoveTopSpeed[0] / spmm
        note = ""
        for layer in job:
            p = layer["profile"]
            if p.get("mode") != "engrave" or sweeps(layer) is not None or not layer["fills"]:
                continue
            dpi = int(p.get("dpi", 500))
            rows, corner = rasterize(layer["fills"], dpi, with_corner=True)
            v = p.get("speed", 50) / 100.0 * m.EngraveMaxVelocity[0] / spmm
            px = 25.4 / dpi
            note = "engraving speed assumed"
            for _ in range(int(p.get("passes", 1))):
                sweep = 1
                for r, row in enumerate(rows):
                    bits = np.unpackbits(np.frombuffer(row, dtype=np.uint8))
                    if not bits.any():
                        continue
                    edges = np.flatnonzero(np.diff(np.concatenate(([0], bits, [0]))))
                    runs = list(zip(edges[::2], edges[1::2]))[::sweep]
                    ry = corner[1] + (r + 0.5) * px
                    for a, b in runs:
                        a, b = (a, b) if sweep == 1 else (b, a)
                        go(corner[0] + a * px, ry, False, v if x[-1] != corner[0] + a * px else rapid)
                        go(corner[0] + b * px, ry, True, v)
                    sweep = -sweep
        paths = self.cut_paths(job)
        if paths:
            go(home[0] / spmm, home[1] / spmm, False, rapid, False)
            recs = np.array(self.fsl.cut_records(paths, m, home=self.home_steps()), dtype=np.int64)
            # The up-and-down and diagonal engravings lead the cut job, and plan the same as a job of
            # those lines alone: they end at its last record with the laser on.
            n_swept = sum(1 for _path, p in self.cut_order(job) if p.get("mode") == "engrave")
            swept = 0
            if n_swept:
                alone = np.array(self.fsl.cut_records(paths[:n_swept], m, home=self.home_steps()))
                swept = int(np.flatnonzero(alone[:, 3] > 0)[-1]) + 1
            sx = np.where(recs[:, 0] & 1, recs[:, 1], -recs[:, 1])
            sy = np.where(recs[:, 0] & 2, recs[:, 2], -recs[:, 2])
            t0 = t[-1]
            t += list(t0 + self.fsl.TICK * np.arange(1, len(recs) + 1))
            x += list((home[0] + np.cumsum(sx)) / spmm)
            y += list((home[1] + np.cumsum(sy)) / spmm)
            on += list(recs[:, 3] > 0)
            eng += [True] * swept + [False] * (len(recs) - swept)
        dpis = [int(l["profile"].get("dpi", 500)) for l in job if l["profile"].get("mode") == "engrave"]
        pitch = 25.4 / dpis[0] if dpis else 0.0
        return np.array(t), np.array(x), np.array(y), np.array(on), np.array(eng), pitch, note

    def console(self, text):
        """A raw line on the command connection, as RetinaEngrave's own commands are sent."""
        self.tell("tx", text)
        self.tell("rx", self.laser.say(text + "\n").strip() or "(no reply)")

    def home(self):
        """Captured from RetinaEngrave's code: one command; the laser reports where it ends up."""
        self.tell("tx", "home")
        self.tell("rx", self.laser.home().strip())
        time.sleep(1.0)
        self.homed_at = getattr(self, "position", (0, 0))

    def wait_idle(self, timeout=3600):
        """Hold the next move or job until the laser reports what it reported when idle.
        UNVERIFIED: which status values mean "running" on the real laser."""
        time.sleep(1.0)
        end = time.monotonic() + timeout
        while getattr(self, "state", None) != getattr(self, "idle_state", None) and time.monotonic() < end:
            time.sleep(0.2)

    def go_to(self, page_xy):
        """Move the head to a page position (motor steps, x right, y down from the page's top-left).
        UNVERIFIED on the laser: that the reported position counts in the same directions as
        the page; to be settled by the first test at the bench (a small square top-right)."""
        home = self.home_steps()
        here = getattr(self, "position", None)
        base = getattr(self, "homed_at", None)
        if here is None or base is None:
            self.tell("error", "press Home first: the head's place is not known")
            raise RuntimeError("not homed")
        want = (page_xy[0] - home[0], page_xy[1] - home[1])
        now = (here[0] - base[0], here[1] - base[1])
        dx, dy = int(round(want[0] - now[0])), int(round(want[1] - now[1]))
        if dx or dy:
            self.tell("tx", f"move {dx}, {dy} steps")
            self.tell("rx", self.laser.move(dx, dy).strip())
            time.sleep(0.5)

    def _spmm(self, axis):
        spi = self.model.StepsPerInch
        return spi[min(axis, len(spi) - 1)] / 25.4

    def jog(self, dx_mm, dy_mm):
        """A relative move of the head, page directions (x right, y down).
        UNVERIFIED: that the laser's step directions match the page's."""
        dx, dy = round(dx_mm * self._spmm(0)), round(dy_mm * self._spmm(1))
        self.tell("tx", f"jog {dx_mm:+.2f}, {dy_mm:+.2f} mm ({dx}, {dy} steps)")
        self.tell("rx", self.laser.move(dx, dy).strip())

    def go_to_mm(self, x_mm, y_mm):
        self.go_to((x_mm * self._spmm(0), y_mm * self._spmm(1)))

    def where(self):
        """The head's page position in mm, from the reported position since Home."""
        here, base = getattr(self, "position", None), getattr(self, "homed_at", None)
        if here is None or base is None:
            return None
        h = self.home_steps()
        return ((h[0] + here[0] - base[0]) / self._spmm(0), (h[1] + here[1] - base[1]) / self._spmm(1))

    def ceiling(self):
        """Overall power ceiling, % (RetinaEngrave's power sliders), set once per laser in the
        laser list as power_ceiling; every color's power is a share of it."""
        return float(str(self.cfg.get("power_ceiling", 100)).split()[0])

    def home_steps(self):
        """Home, the page's top-right corner, in motor steps (page x right, y down). The page is
        the bed; the laser list's bed size is used only when no drawing is open."""
        page = getattr(self, "page", None)
        w_mm = page[0] if page and page[0] else float(str(self.cfg.get("bed", "508 x 305")).split("x")[0])
        return (w_mm * self.model.StepsPerInch[0] / 25.4, 0.0)

    def pause(self):
        self.tell("tx", "pause")
        self.tell("rx", self.laser.pause().strip())

    def resume(self):
        self.tell("tx", "resume")
        self.tell("rx", self.laser.resume().strip())

    def stop(self):
        self.tell("tx", "abort")
        self.tell("rx", self.laser.abort().strip())

    def test_fire(self, power_pct, ms):
        # A cut job with no motion: the laser held at power for the time asked.
        recs = [(0, 0, 0, round(255 * power_pct / 100.0))] * max(1, round(ms / 1000 / self.fsl.TICK))
        self.tell("tx", f"test fire {power_pct}% for {ms} ms")
        self.tell("rx", (self.laser.send_job(self.fsl.cut_job(recs, self.model, 0.0, 0.0)) or "").strip())

    def start_from(self, absolute):
        if absolute:
            self.home()
            self.wait_idle()
        elif getattr(self, "homed_at", None) is None:
            self.tell("error", "Relative needs to know where the head is: press Home once, then jog")
            raise RuntimeError("not homed")

    def frame(self, paths, absolute=True):
        """The paths given (page mm: one box, or every outline) traced by the head with the
        laser off at full vector speed, where the job will run."""
        self.start_from(absolute)
        self.go_to(self.home_steps())
        spi = self.model.StepsPerInch[0] / 25.4
        fast = self.model.VectorMaxVelocity[0]
        recs = self.fsl.cut_records([([x * spi for x, y in p], [y * spi for x, y in p], fast, 0, 1) for p in paths],
                                    self.model, home=self.home_steps())
        self.tell("tx", f"frame: {len(paths)} outline{'s' * (len(paths) != 1)} traced with the laser off")
        self.tell("rx", (self.laser.send_job(self.fsl.cut_job(recs, self.model, 0.0, 0.0)) or "").strip())

    def close(self):
        self.connected = False
        try:
            self.laser.close()
        except Exception:
            pass


KINDS = {"fullspectrum": FullSpectrum}


def sweeps(layer):
    """The fill-line angle for an engraving swept up and down (90) or diagonally (45); None for
    left and right, which RetinaEngrave's own engrave job does."""
    if layer["profile"].get("mode") != "engrave":
        return None
    return {"up and down": 90.0, "diagonal": 45.0}.get(layer["profile"].get("direction"))


def fmt_time(seconds):
    seconds = round(seconds)
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def rasterize(fills, dpi, with_corner=False):
    """Filled areas (mm polylines) to 1-bit rows, 8 pixels per byte, even-odd fill; with_corner
    also returns the image's top-left corner in mm."""
    from PIL import Image, ImageChops, ImageDraw
    pts = [p for poly in fills for p in poly]
    if not pts:
        return ([], (0.0, 0.0)) if with_corner else []
    px = dpi / 25.4
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    w = int((max(p[0] for p in pts) - x0) * px) + 2
    h = int((max(p[1] for p in pts) - y0) * px) + 2
    img = Image.new("1", (w, h), 0)
    for poly in fills:
        layer = Image.new("1", (w, h), 0)
        ImageDraw.Draw(layer).polygon([((x - x0) * px, (y - y0) * px) for x, y in poly], fill=1)
        img = ImageChops.logical_xor(img, layer)
    rows = [img.crop((0, r, w, r + 1)).tobytes() for r in range(h)]
    return (rows, (x0, y0)) if with_corner else rows
