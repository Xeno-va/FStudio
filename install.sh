#!/usr/bin/env bash
# Set up FStudio: a Python environment beside the program, and its Inkscape menu.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# The panel's window uses the system's GTK 4 and its Python bindings (PyGObject, pycairo),
# so the environment sees the system's packages and adds only what is listed.
python3 -m venv --system-site-packages "$HERE/.venv"
"$HERE/.venv/bin/pip" install --quiet -r "$HERE/requirements.txt"
"$HERE/.venv/bin/python" -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk" \
  || { echo "GTK 4 for Python is missing: install your system's PyGObject and GTK 4 packages, then run this again."; exit 1; }

# Inkscape's menu: one folder linked into Inkscape's extensions folder.
for ext in "$HOME/.var/app/org.inkscape.Inkscape/config/inkscape/extensions" "$HOME/.config/inkscape/extensions"; do
  if [ -d "$(dirname "$ext")" ]; then
    mkdir -p "$ext"
    ln -sfn "$HERE/app/inkscape" "$ext/fstudio"
    echo "Inkscape menu installed in $ext (Extensions > FStudio). Restart Inkscape."
  fi
done
if [ -d "$HOME/.var/app/org.inkscape.Inkscape" ]; then
  echo "Inkscape from Flathub opens the panel outside its sandbox; if nothing opens, allow it once:"
  echo "  flatpak override --user --talk-name=org.freedesktop.Flatpak org.inkscape.Inkscape"
fi
