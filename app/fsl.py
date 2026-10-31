"""Drive a Full Spectrum H-Series (5th-gen hobby) laser over the network.

A Python copy of what RetinaEngrave 3D 4.430 does, read from its decompiled
source (see ../PROTOCOL.md): the startup packet, the cut planner it actually
runs (its built-in fallback), the step stream, the engraving rows, the job
packing and the network exchange. Plain Python, so it runs on the laptop or a
Raspberry Pi. Nothing here has been tried on the laser yet.

Units inside are motor steps and seconds, as in RetinaEngrave.
"""
import math
import socket
import struct
import time
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field

TICK = 0.0005  # one step record every 0.5 ms
MAX_STEPS = 50  # most steps one record may carry, per axis
PIECE = 262144  # job compression piece size
MAX_JOB = 25165824  # RetinaEngrave refuses compressed jobs this large

PORT_CMD, PORT_DATA, PORT_STATUS = 12345, 12346, 12347

# Packet types for "pass" packets.
P_INIT, P_IDLE, P_VECTOR, P_ENGRAVE, P_BYTES, P_STATUS, P_USB, P_COMBINED = range(8)

# Idle command codes seen in RetinaEngrave.
IDLE_ENGRAVE_START = 2
IDLE_HOME = 4
IDLE_INIT = 5
IDLE_MOVE = 6
IDLE_VECTOR_POWER = 22


# ---------------------------------------------------------------- settings

@dataclass
class Model:
    """One laser's settings, read from Full Spectrum's model file."""
    name: str
    values: dict = field(repr=False)

    def __getattr__(self, key):
        try:
            return self.values[key]
        except KeyError:
            raise AttributeError(key)


def load_model(path):
    root = ET.parse(path).getroot()[0]
    values = {}
    for child in root:
        if len(child):
            values[child.tag] = [_num(c.text) for c in child]
        else:
            values[child.tag] = _num(child.text)
    values.setdefault("Pierce", 0)  # milliseconds; not in the file, a user setting
    return Model(values["LaserModel"], values)


def _num(text):
    if text is None:
        return None
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(text, text)


# ---------------------------------------------------------- startup packet

def init_struct(m, image_w_in=0.0, image_h_in=0.0, driver_version=0, power_max=None):
    """The startup packet: motion limits from the model file, little-endian, packed.
    Captured: RetinaEngrave fills the image size from the loaded drawing, at connect and in jobs."""
    t0 = lambda acc: int(100000000.0 * math.sqrt(2.0 / acc))
    ints = [driver_version]
    ints += [t0(a) for a in m.EngraveAcceleration[:3]] + [int(v) for v in m.EngraveMaxVelocity[:3]]
    ints += [t0(a) for a in m.VectorAcceleration[:3]] + [int(v) for v in m.VectorMaxVelocity[:3]]
    ints += [t0(a) for a in m.JogAcceleration[:3]] + [int(v) for v in m.JogMaxVelocity[:3]]
    mode = 0
    ints += [mode, int(image_w_in * m.StepsPerInch[0]), int(image_h_in * m.StepsPerInch[1])]
    ints += [2, 2, 1]  # laser fire delay, dead time, repeats: fixed in RetinaEngrave
    flip = sum(1 << i for i in range(3) if m.FlipAxes[i] == 1)
    tail = [flip, m.FlipLaserOutput, m.FlipLaserPWMPower, m.LaserPowerMax if power_max is None else power_max,
            m.HomeDirection,
            m.FlipHomeDirection[0], m.FlipHomeDirection[1], m.FlipHomeDirection[2],
            *m.LimitContCondition[:4]]
    out = struct.pack(f"<{len(ints)}i", *ints) + bytes(tail)
    out += struct.pack("<3i", *m.MaxSteps[:3]) + struct.pack("<3i", *m.TableSize[:3])
    return out


def idle_struct(command, p1=0, p2=0, p3=0, p4=0, p5=0, p6=0, p7=0):
    return struct.pack("<BBhhhiii", command, p1, p2, p3, p4, p5, p6, p7)


def packet(data, kind):
    """A "pass" packet: AA, type, 2-byte length, data, then 8 bytes of BB CC."""
    return bytes([0xAA, kind]) + struct.pack("<h", len(data)) + data + b"\xbb\xcc" * 4


# ------------------------------------------------------------ cut planner

def _cubic(a, b, c, d, t):
    return a * t * t * t + b * t * t + c * t + d


class _Start:
    """Ramp up from rest to speed v (RetinaEngrave's start segment)."""
    def __init__(self, p0, v, amax):
        if v == 0 or math.isnan(v):
            self.k, self.T = (0.0, 0.0, 0.0, p0), 0.0
            return
        a = math.copysign(amax, v)
        self.T = 4.0 * v / a
        self.k = (-0.0625 * a * a / v, 0.5 * a, 0.0, p0)

    def at(self, t):
        return _cubic(*self.k, t)


class _Line:
    def __init__(self, p0, v, T):
        self.p0, self.v, self.T = p0, v, T

    def at(self, t):
        return self.v * t + self.p0


class _Blend:
    """Constant-acceleration change from speed v1 to v2, centred on the corner."""
    def __init__(self, p, v1, v2, amax):
        if v1 == v2 or math.isnan(v1) or math.isnan(v2):
            self.k, self.T = (0.0, 0.0, 0.0, p), 0.0
            return
        a = math.copysign(amax, v2 - v1)
        self.T = (v2 - v1) / a
        self.k = (0.0, 0.5 * a, v1, p - v1 * (self.T / 2.0))

    def at(self, t):
        return _cubic(*self.k, t)


class _Stop:
    """Ramp down from speed v to rest at p_end."""
    def __init__(self, p_end, v, amax):
        if v == 0 or math.isnan(v):
            self.k, self.T = (0.0, 0.0, 0.0, p_end), 0.0
            return
        a = -1 * math.copysign(amax, v)
        self.T = -4.0 * v / a
        self.k = (-0.0625 * a * a / v, -0.25 * a, v, p_end + 4.0 * v * v / a)

    def at(self, t):
        return _cubic(*self.k, min(t, self.T))


def f32(v):
    """Round to single precision. RetinaEngrave's planner works in floats (decoded), and each
    float operation equals the double result rounded once to float."""
    return struct.unpack("f", struct.pack("f", v))[0]


def _sign(v):
    return (v > 0) - (v < 0)


def _bsearch(xs, v):
    """.NET List<float>.BinarySearch, including which index it lands on among equal values."""
    lo, hi = 0, len(xs) - 1
    while lo <= hi:
        i = lo + ((hi - lo) >> 1)
        if xs[i] == v:
            return i
        if xs[i] < v:
            lo = i + 1
        else:
            hi = i - 1
    return ~lo


class Axis1D:
    """One axis following its points at given times (RetinaEngrave's DecoupledTrajectory1D):
    segments in double, segment start times and speeds in float, as decoded."""
    def __init__(self, p, t, amax):
        v = f32(f32(p[1] - p[0]) / t[1])
        self.starts, self.parts = [0.0], [_Start(p[0], v, amax)]
        for i in range(1, len(p) - 1):
            v1 = f32(f32(p[i] - p[i - 1]) / f32(t[i] - t[i - 1]))
            v2 = f32(f32(p[i + 1] - p[i]) / f32(t[i + 1] - t[i]))
            blend = _Blend(p[i], v1, v2, amax)
            here = f32(self.starts[-1] + f32(self.parts[-1].T))
            end = f32(t[i] - f32(f32(blend.T) / 2.0))
            self._add(_Line(self.parts[-1].at(self.parts[-1].T), v1, f32(end - here)))
            self._add(blend)
        v = f32(f32(p[-1] - p[-2]) / f32(t[-1] - t[-2]))
        stop = _Stop(p[-1], v, amax)
        here = f32(self.starts[-1] + f32(self.parts[-1].T))
        end = f32(t[-1] - f32(stop.T))
        self._add(_Line(self.parts[-1].at(self.parts[-1].T), v, f32(end - here)))
        self._add(stop)
        self.T = f32(sum(f32(s.T) for s in self.parts))  # LINQ Sum: double total, then float

    def _add(self, seg):
        self.starts.append(f32(self.starts[-1] + f32(self.parts[-1].T)))
        self.parts.append(seg)

    def at(self, t):
        i = _bsearch(self.starts, f32(t))
        if i < 0:
            i = ~i - 1
        return f32(self.parts[i].at(t - self.starts[i]))


def _corner_ratio(ratio, p, dur, amax):
    """How hard one axis must accelerate at each corner, against its limit (float, as decoded)."""
    n = len(p)
    if dur[0] != 0 and p[0] != p[1]:
        v = f32(f32(p[1] - p[0]) / dur[0])
        a = f32(_sign(v) * amax)
        half = f32(dur[0] / 2.0)
        q = f32(f32(v - 0.0) / a)
        ratio[0] = max(f32(2.0 * f32(math.sqrt(f32(q / half)))), ratio[0])
    for i in range(1, n - 1):
        v1 = f32(f32(p[i] - p[i - 1]) / dur[i - 1])
        v2 = f32(f32(p[i + 1] - p[i]) / dur[i])
        if v1 != v2:
            a = f32(_sign(f32(v2 - v1)) * amax)
            h1, h2 = f32(dur[i - 1] / 2.0), f32(dur[i] / 2.0)
            h = min(h1, h2)
            r = f32(math.sqrt(f32(f32(f32(v2 - v1) / a) / f32(h + h))))
            ratio[i - 1] = max(r, ratio[i - 1])
            ratio[i] = max(r, ratio[i])
    if dur[n - 2] != 0 and p[n - 1] != p[n - 2]:
        v = f32(f32(p[n - 1] - p[n - 2]) / dur[n - 2])
        a = f32(-1.0 * _sign(v) * amax)
        half = f32(dur[n - 2] / 2.0)
        q = f32(f32(0.0 - v) / a)
        ratio[n - 2] = max(f32(2.0 * f32(math.sqrt(f32(q / half)))), ratio[n - 2])


class CutPath:
    """Plan one path exactly as RetinaEngrave's built-in planner does (it is the one that runs:
    the faster native planner it prefers, DecoupledTrajectory.dll, is not shipped). Checked
    sample for sample against RetinaEngrave's own planner run under Wine."""
    def __init__(self, xs, ys, speed, ax, ay):
        xs, ys = [f32(v) for v in xs], [f32(v) for v in ys]
        speed, ax, ay = f32(speed), f32(ax), f32(ay)
        n = len(xs)
        lengths = [f32(math.sqrt(f32(xs[i + 1] - xs[i]) ** 2 + f32(ys[i + 1] - ys[i]) ** 2)) for i in range(n - 1)]
        longest = max(lengths)
        lim_x = f32(math.sqrt(f32(f32(0.125 * ax) * longest)))
        lim_y = f32(math.sqrt(f32(f32(0.125 * ay) * longest)))
        slow = f32(f32(longest / min(speed, min(lim_x, lim_y))) * 10.0)
        dur = [slow] * (n - 1)
        last = f32(3.4028234663852886e38)
        while True:
            ratio = [0.0] * (n - 1)
            _corner_ratio(ratio, xs, dur, ax)
            _corner_ratio(ratio, ys, dur, ay)
            for i, r in enumerate(ratio):
                if r > 1.0:
                    dur[i] = f32(dur[i] * f32(1.0 + f32(f32(r - 1.0) * f32(0.05))))
                else:
                    dur[i] = max(f32(dur[i] * f32(1.0 - f32(f32(1.0 - r) * f32(0.05)))), f32(lengths[i] / speed))
            total = f32(sum(dur))  # LINQ Sum: double total, then float
            if abs(f32(last - total)) < f32(0.0001):
                break
            last = total
        t = [0.0]
        for d in dur:
            t.append(f32(t[-1] + d))
        self.x, self.y = Axis1D(xs, t, ax), Axis1D(ys, t, ay)
        self.T = self.x.T
        self.rounding = max(math.hypot(self.x.at(tk) - px, self.y.at(tk) - py) for tk, px, py in zip(t, xs, ys))
        self.samples = math.ceil(self.T / f32(TICK))

    def sample(self, i):
        """Position at sample i, as RetinaEngrave takes it: float time, float result."""
        tk = f32(f32(i) * f32(TICK))
        return self.x.at(tk), self.y.at(tk)


class Rapid:
    """Laser-off move between two points (RetinaEngrave's trapezoid move)."""
    def __init__(self, x0, x1, y0, y1, ax, vx, ay, vy):
        self.ends = (x0, x1, y0, y1)
        self.xp = self._axis(x1 - x0, ax, vx)
        self.yp = self._axis(y1 - y0, ay, vy)
        self.T = max(self.xp[3], self.yp[3])

    @staticmethod
    def _axis(d, acc, top):
        a = math.copysign(abs(acc), d) if d else abs(acc)
        if d == 0:
            return (a, 0.0, 0.0, 0.0, 0.0)
        v = math.copysign(math.sqrt(a * d), d)
        v = math.copysign(min(abs(v), top), v)
        ta = abs(v / a)
        tc = (d - 0.5 * a * ta * ta * 2.0) / v
        return (a, v, ta, 2.0 * ta + tc, tc)

    @staticmethod
    def _at(t, p0, p1, prof):
        a, v, ta, total, tc = prof
        if t <= 0:
            return p0
        if t <= ta:
            return p0 + 0.5 * a * t * t
        if t <= ta + tc:
            return p0 + 0.5 * a * ta * ta + v * (t - ta)
        if t < total:
            return p1 - 0.5 * a * (total - t) * (total - t)  # same order as RetinaEngrave
        return p1

    def x_at(self, t):
        return self._at(t, self.ends[0], self.ends[1], self.xp)

    def y_at(self, t):
        return self._at(t, self.ends[2], self.ends[3], self.yp)


# ------------------------------------------------------------ step stream

def _quantise(target, pos):
    """Whole steps toward target this tick, as RetinaEngrave counts them (moves only past 1 step)."""
    d = target - pos
    if abs(d) <= 1.0:
        return 0, None, pos
    n = min(math.floor(abs(d)), MAX_STEPS)
    return n, d > 1.0, pos + (n if d > 0 else -n)


def _records(x_at, y_at, count, power, T, step, pos):
    """Step records for one planned move, as RetinaEngrave makes them (decoded): a clock that
    starts at 0 and is advanced by adding `step` each record, while it is below T; records after
    that hold still with the laser off and the last direction. x_at/y_at take (record index, time).
    pos is [x, y] and is updated."""
    out, last_dir, t = [], 0, 0.0
    for i in range(count):
        if not t < T:
            out.append((last_dir, 0, 0, 0))
            continue
        nx, xpos, pos[0] = _quantise(x_at(i, t), pos[0])
        ny, ypos, pos[1] = _quantise(y_at(i, t), pos[1])
        d = (1 if xpos else 0) | (2 if ypos else 0)
        out.append((d, nx, ny, power))
        last_dir = d
        t += step
    return out


def _rapid_records(x0, x1, y0, y1, m, pos):
    """Laser-off move from where the motors are; none at all when there is nowhere to go."""
    r = Rapid(x0, x1, y0, y1, m.RapidVectorMoveAcc[0], m.RapidVectorMoveTopSpeed[0],
              m.RapidVectorMoveAcc[1], m.RapidVectorMoveTopSpeed[1])
    if not r.T > 0.0:
        return []
    return _records(lambda i, t: r.x_at(t), lambda i, t: r.y_at(t), math.ceil(r.T / TICK), 0, r.T, TICK, pos)


def cut_records(paths, m, pierce_ms=None, home=(0.0, 0.0)):
    """paths: list of (xs, ys, speed_steps_per_s, power_0_255, passes), in motor steps.
    Matches RetinaEngrave record for record on captured jobs (see ../PROTOCOL.md).
    home: where the head is when the job starts and where it returns, in the same steps.
    RetinaEngrave uses (0, 0) at the drawing's corner; the panel passes the bed's top-right."""
    pierce_ms = m.Pierce if pierce_ms is None else pierce_ms
    ax, ay = m.VectorAcceleration[0], m.VectorAcceleration[1]
    pos, recs = [float(home[0]), float(home[1])], []
    for xs, ys, speed, power, passes in paths:
        if len(xs) < 2:
            continue
        plan = CutPath(xs, ys, speed, ax, ay)
        samples = [plan.sample(i) for i in range(plan.samples)]
        for _ in range(int(passes)):
            recs += _rapid_records(pos[0], f32(xs[0]), pos[1], f32(ys[0]), m, pos)
            cut = _records(lambda i, t: samples[i][0], lambda i, t: samples[i][1], plan.samples, int(power),
                           plan.T, f32(TICK), pos)
            recs += [(0, 0, 0, 255)] * math.ceil(pierce_ms / 1000.0 / TICK) + cut
    recs += _rapid_records(pos[0], float(home[0]), pos[1], float(home[1]), m, pos)
    return recs


# ------------------------------------------------------------- job packing

def compress(raw, cuts=None):
    """RetinaEngrave's job compression: piece count, then (length, deflate) per piece."""
    sizes = cuts if cuts else [min(PIECE, len(raw) - i) for i in range(0, len(raw), PIECE)]
    out, at = [struct.pack("<i", len(sizes))], 0
    for size in sizes:
        z = zlib.compress(raw[at:at + size], 6)[2:]  # drop the 2-byte header, keep the checksum
        out += [struct.pack("<i", len(z)), z]
        at += size
    job = b"".join(out)
    if len(job) >= MAX_JOB:
        raise ValueError("job too large for the laser")
    return job


def _power_byte(m, pct):
    """RetinaEngrave's (byte)(LaserPowerMax x slider / 100), never 1."""
    b = int(m.LaserPowerMax * (pct / 100.0)) & 0xFF
    return 2 if b == 1 else b


def job_header(m, kind, image_w_in, image_h_in, vector_pct=100.0, engrave_pct=100.0):
    """RetinaEngrave's 1024-byte job header (PrepareJobHeader, decoded; captured byte for byte):
    packet type, then at 512 the init idle command, the startup settings with this drawing's size
    and the job's power ceiling, and idle command 23."""
    out = bytearray(1024)
    out[0:4] = struct.pack("<i", kind)
    power = _power_byte(m, vector_pct) if kind == P_VECTOR else _power_byte(m, engrave_pct)
    block = idle_struct(IDLE_INIT) + init_struct(m, image_w_in, image_h_in, power_max=power) + idle_struct(23)
    out[512:512 + len(block)] = block
    if kind == P_COMBINED:
        out[256] = _power_byte(m, vector_pct)
    return bytes(out)


def cut_job(records, m, image_w_in, image_h_in, vector_pct=100.0):
    raw = struct.pack("<ii", 2, len(records)) + b"".join(bytes(r) for r in records)
    return job_header(m, P_VECTOR, image_w_in, image_h_in, vector_pct) + compress(raw)


def _bitrev(b):
    # UNVERIFIED: the helper RetinaEngrave applies to right-to-left rows; assumed bit reversal.
    return int(f"{b:08b}"[::-1], 2)


def engrave_job(rows, m, speed_pct, power_pct, reverse=False, image_w_in=0.0, image_h_in=0.0, dpi=None,
                ceiling_pct=100.0):
    """rows: list of bytes, 8 pixels per byte, top row first, at `dpi` dots per inch: the
    resolution chosen for the job (RetinaEngrave's DPI box, 500 in the capture), not the model
    file's figure. Checked byte for byte against a captured RetinaEngrave engraving."""
    dpi = dpi or m.DPIvalue[0]
    speed = speed_pct / 100.0 * m.EngraveMaxVelocity[0]
    y_step = int(-(m.StepsPerInch[1] / dpi))
    if reverse:
        rows = list(reversed(rows))
        y_step = -y_step
    per_px = int(m.StepsPerInch[0] / dpi)
    headers, bodies = [], []
    y_move, x_carry, sweep, row_no, first = 0, 0, 1, 0, True
    for row in rows:
        if not any(row):
            y_move += y_step
            continue
        row = bytes(row)
        if sweep == -1:
            row = bytes(_bitrev(b) for b in reversed(row))
        lead = len(row) - len(row.lstrip(b"\0"))
        trail = len(row) - len(row.rstrip(b"\0"))
        body = row[lead:len(row) - trail]
        x = (-int(speed) if first else 0) + x_carry + lead * per_px * 8 * sweep
        first = False
        x_carry = trail * 8 * per_px * sweep
        move_dir = (1 if x > 0 else 0) | (2 if y_move <= 0 else 0)
        engrave_dir = 1 if sweep == 1 else 0
        headers.append(struct.pack("<iHBBHBBII", row_no, len(body), move_dir,
                                   int(power_pct / 100.0 * m.LaserPowerMax), int(speed),
                                   engrave_dir, per_px, abs(x), abs(y_move)))
        bodies.append(body)
        row_no += 1
        sweep *= -1
        y_move = y_step
    raw, cuts, piece = [struct.pack("<ii", 3, len(headers))], [], 8
    for h, b in zip(headers, bodies):
        size = len(h) + len(b)
        if piece + size < PIECE:
            piece += size
        else:
            cuts.append(piece)
            piece = size
        raw += [h, b]
    cuts.append(piece)
    # The header's power ceiling is RetinaEngrave's engrave power slider (captured: 100 % gives
    # the model's LaserPowerMax); each row's own power byte is the colour's share.
    return job_header(m, P_ENGRAVE, image_w_in, image_h_in, engrave_pct=ceiling_pct) + compress(b"".join(raw), cuts)


# ---------------------------------------------------------------- network

class Laser:
    """Command, data and status connections to the laser, as RetinaEngrave uses them."""

    def __init__(self, ip, timeout=5.0):
        self.ip = ip
        self.cmd = socket.create_connection((ip, PORT_CMD), timeout=timeout)
        self.greeting = self._read()
        self.status = socket.create_connection((ip, PORT_STATUS), timeout=timeout)

    def _read(self):
        try:
            return self.cmd.recv(4096).decode("utf-8", "replace")
        except socket.timeout:
            return ""

    def say(self, line):
        self.cmd.sendall(line.encode("ascii"))
        return self._read()

    def pass_packet(self, data):
        """Captured: RetinaEngrave sends "pass N", waits for the reply, then the packet, and waits again."""
        self.say(f"pass {len(data)}\n")
        self.cmd.sendall(data)
        return self._read()

    def init(self, m, image_w_in=0.0, image_h_in=0.0):
        return self.pass_packet(packet(idle_struct(IDLE_INIT), P_IDLE) +
                                packet(init_struct(m, image_w_in, image_h_in), P_INIT))

    def idle(self, command, **kw):
        return self.pass_packet(packet(idle_struct(command, **kw), P_IDLE))

    def home(self):
        """RetinaEngrave's Home (decoded): idle command 4."""
        return self.idle(IDLE_HOME)

    def move(self, dx, dy, dz=0):
        """RetinaEngrave's MoveXYZ (decoded): idle command 6, a relative move in motor steps.
        Its Move To works out the distance from the position the laser reports."""
        return self.idle(IDLE_MOVE, p2=255, p3=1, p5=int(dx), p6=int(dy), p7=int(dz))

    def send_job(self, job, run=True):
        for _ in range(10):
            self.say("xjob\n")
            self.say(f"immediate {len(job)}\n")
            self.say("data\n")
            with socket.create_connection((self.ip, PORT_DATA), timeout=10) as d:
                self.say("sending\n")  # RetinaEngrave waits for this reply, then pauses 0.1 s
                time.sleep(0.1)
                d.sendall(job)
            reply = self._read().split(" ")
            time.sleep(0.5)  # and after
            if len(reply) > 2 and reply[2].strip().isdigit() and int(reply[2]) == len(job):
                break
        else:
            raise IOError("laser never confirmed the job size")
        if run:
            return self.say("run\n")

    def pause(self):
        return self.say("pause\n")

    def resume(self):
        return self.say("resume\n")

    def abort(self):
        return self.say("abort\n")

    def close(self):
        try:
            self.say("bye\n")
        finally:
            self.cmd.close()
            self.status.close()
