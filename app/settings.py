"""The laser list and the profile files: plain text, edited by hand."""
import re
from pathlib import Path

HERE = Path(__file__).parent
LASERS = HERE / "lasers.txt"
DEFAULTS = HERE / "defaults.txt"
PROFILES = HERE / "profiles"
CHOICES = HERE / "choices"  # remembered color -> profile picks, one file per drawing

KIND_FOLDER = {"fullspectrum": "fullspectrum-h", "rk-uv": "rk-uv"}


def _value(text):
    text = text.strip().split(" #", 1)[0].strip()  # a comment needs a space before it, so colors like #ff0000 survive
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


def read_blocks(path):
    """[Name] headings with key = value lines under each."""
    blocks, current = {}, None
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            current = blocks.setdefault(m.group(1).strip(), {})
        elif "=" in line and current is not None:
            k, v = line.split("=", 1)
            current[k.strip()] = _value(v)
    return blocks


def read_profile(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = _value(v)
    return out


def lasers():
    return read_blocks(LASERS)


def profiles(kind):
    folder = PROFILES / KIND_FOLDER.get(kind, kind)
    folder.mkdir(parents=True, exist_ok=True)
    return {p.stem: p for p in sorted(folder.glob("*.txt"))}


def remembered(drawing_name):
    f = CHOICES / f"{drawing_name}.txt"
    return read_profile(f) if f.exists() else {}


def remember(drawing_name, picks):
    CHOICES.mkdir(exist_ok=True)
    (CHOICES / f"{drawing_name}.txt").write_text("".join(f"{k} = {v}\n" for k, v in picks.items()))


def roles():
    """{role: {color, width, fill}} from the laser defaults: the [Cut], [Score], [Engrave] blocks."""
    out = {}
    for name, block in read_blocks(DEFAULTS).items():
        if " " not in name and "color" in block:
            block["color"] = str(block["color"]).lower()
            out[name] = block
    return out


def color_defaults(kind):
    """{color: starting settings} for one laser kind, from the [kind Role] blocks."""
    blocks, out = read_blocks(DEFAULTS), {}
    for role, spec in roles().items():
        start = blocks.get(f"{kind} {role}")
        if start:
            out[spec["color"]] = start
    return out


def set_laser_value(name, key, value):
    """Change or add one key = value line in a laser's block of the laser list."""
    lines = LASERS.read_text().splitlines()
    out, inside, done = [], False, False
    for line in lines:
        m = re.fullmatch(r"\[(.+)\]", line.strip())
        if m:
            if inside and not done:
                out.append(f"{key} = {value}")
                done = True
            inside = m.group(1).strip() == name
        elif inside and re.match(rf"\s*{re.escape(key)}\s*=", line):
            comment = line.split(" #", 1)[1] if " #" in line else ""
            line = f"{key} = {value}" + (f"     #{comment}" if comment else "")
            done = True
        out.append(line)
    if inside and not done:
        out.append(f"{key} = {value}")
    LASERS.write_text("\n".join(out) + "\n")
