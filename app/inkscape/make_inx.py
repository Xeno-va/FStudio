"""Write Inkscape's menu files for the laser plugin, one set per laser in the laser list.

Run again whenever lasers are added or renamed; the new files then need linking into
Inkscape's extensions folder.
"""
import sys
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import settings  # noqa: E402

# One menu item per laser and job, each naming its laser: no laser to pick in a dialog.
ITEM = """<?xml version="1.0" encoding="UTF-8"?>
<inkscape-extension xmlns="http://www.inkscape.org/namespace/inkscape/extension">
  <name>{title}</name>
  <id>fstudio.{id}</id>
  <param name="laser" type="string" gui-hidden="true">{laser}</param>{extra}
  <effect needs-live-preview="false">
    <object-type>all</object-type>
    <effects-menu>
      <submenu name="FStudio"/>
    </effects-menu>
  </effect>
  <script>
    <command location="inx" interpreter="python">{script}</command>
  </script>
</inkscape-extension>
"""
CONTROL = '\n  <param name="control" type="bool" gui-hidden="true">true</param>'

MARK = """<?xml version="1.0" encoding="UTF-8"?>
<inkscape-extension xmlns="http://www.inkscape.org/namespace/inkscape/extension">
  <name>Mark as {role}</name>
  <id>fstudio.mark.{low}</id>
  <param name="role" type="string" gui-hidden="true">{role}</param>
  <effect needs-live-preview="false">
    <object-type>all</object-type>
    <effects-menu>
      <submenu name="FStudio"/>
    </effects-menu>
  </effect>
  <script>
    <command location="inx" interpreter="python">laser_mark.py</command>
  </script>
</inkscape-extension>
"""


def _put(path, text):
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path


def items():
    """(file name, inx text) for every menu item: send, page setup and, on a laser with a
    moving head, direct control; then the Mark-as items."""
    out = []
    for name, cfg in settings.lasers().items():
        slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
        common = dict(laser=escape(name))
        out.append((f"send_{slug}.inx", ITEM.format(title=f"Send to {escape(name)}", id=f"send.{slug}",
                                                     extra="", script="laser_send.py", **common)))
        out.append((f"setup_{slug}.inx", ITEM.format(title=f"Set up page for {escape(name)}", id=f"setup.{slug}",
                                                      extra="", script="laser_setup.py", **common)))
        if cfg.get("kind") == "fullspectrum":
            out.append((f"control_{slug}.inx", ITEM.format(title=f"{escape(name)} direct control",
                                                            id=f"control.{slug}", extra=CONTROL,
                                                            script="laser_send.py", **common)))
    for role in settings.roles():
        out.append((f"laser_mark_{role.lower()}.inx", MARK.format(role=escape(role), low=role.lower())))
    return out


def write():
    return [_put(HERE / f, text) for f, text in items()]


if __name__ == "__main__":
    print(write())
