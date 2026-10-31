# FStudio

Linux control for the **Full Spectrum Laser H-Series** (40 W CO2, 20 x 12 in bed), sent
straight from Inkscape. A replacement for RetinaEngrave on Linux.

> ## ⚠️ Not yet tested on a laser
>
> FStudio has **not been run on a real H-Series**. It has not been tested or debugged on
> hardware. Everything was checked only against recordings of what RetinaEngrave sends and
> against a simulated laser. Expect bugs.
>
> Known unknowns, in the order a first bench session should settle them:
> - **Axis directions.** If an axis counts the other way, the first job comes out mirrored.
>   Start with a small square near the top-right corner, at low power.
> - **Busy status.** Engravings, cuts and batch pieces wait for the laser to report it is idle.
>   If the status values are read wrong, the next job could be sent too early or never.
> - **Right-to-left engraving rows** may come out reversed within each row.
> - **Engraving speed units** are not known, so the simulated engraving time is a guess.
> - **Up-and-down and diagonal engraving** are drawn as lines in a cut job. RetinaEngrave does
>   not do this, so it has no reference at all.
>
> A laser can start fires and injure eyes. Stay with the machine, keep the lid closed, and
> keep a hand near the stop button.

FStudio is an independent project. It is not affiliated with or endorsed by Full Spectrum
Laser. Product names belong to their owners.

## What it does

- **Inkscape menu** (Extensions → FStudio): send the drawing to the laser, set up a page the
  size of the bed, open Direct control, and mark the selection as Cut, Score or Engrave.
- **The page is the bed.** Where a design sits on the page is where it runs. Home is the
  page's top-right corner, where the head parks.
- **Red cuts, everything else engraves.** On the General setting, red (`#ff0000`) lines and
  the edges of red shapes are cut. Every other color is engraved: filled shapes, and lines at
  their drawn width. Color Mapping can set any color to vector, raster or skip instead.
- **Settings** laid out like Epilog's: General, Color Mapping and Presets tabs. Start from
  Absolute (home first) or Relative (the job origin goes where the head is).
- **Frame** traces the bounding box or every outline with the laser off.
- **Simulate** plays the job on the drawing from the same motion the laser is sent. It has
  Stop, Clear and a speed slider; engraving plays ten times faster than cutting.
- **Kerf** per cutting color: outside edges move out and holes move in by half of it.
  Inside cuts always run before the outside cut around them.
- **Engrave direction**: left and right, up and down, or diagonal.
- **Material test**: a labelled grid of squares, power down the rows and speed across.
- **Batch serial numbers**: put `#` signs in a text where the number goes (`####`, or `BK###`
  for BK001, BK002…). Batch numbers it from a first value for a number of pieces. After each
  piece the drawing shows the next number and waits for Next (or Enter). The count is
  remembered between sessions.
- **Direct control**: jog with buttons or arrow keys, go to a position, Home, test fire.
- **Telemetry** of everything sent and received, with a command line to send raw commands.

## Install

Needs Linux, Python 3.11 or newer, GTK 4 with its Python bindings (PyGObject and pycairo,
from your system's packages), and Inkscape 1.2 or newer.

```
git clone https://github.com/Xeno-va/FStudio.git
cd FStudio
./install.sh
```

Then restart Inkscape. The laser is found on the network by itself. To use a fixed address,
put its IP in `app/lasers.txt`.

## Settings files

- `app/lasers.txt`: the laser list (address, bed size, power ceiling).
- `app/machine-h-series.xml`: the H-Series' motion limits, in the layout of Full Spectrum's
  own model file.
- `app/defaults.txt`: what Cut, Score and Engrave look like in Inkscape, and their starting
  settings.
- `app/profiles/fullspectrum-h/`: presets. The ones included are starting points for a 40 W
  tube, gathered from forum reports. Test on scrap first.
