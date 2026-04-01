# BlueROV2 Camera + WaterLinked Sonar 3D-15 — Synchronized Recording & Visualization

Records and plays back time-synchronized video from a BlueROV2 camera (MJPEG/RTP on UDP 5604)
and point-cloud data from a WaterLinked Sonar 3D-15 (RIP2/UDP multicast on 224.0.0.96:4747).

---

## Repository layout

```
ERA/
├── sync_recorder.py        ← record both sensors simultaneously
├── sync_visualizer.py      ← play back synchronized recordings
├── camera_sonar-wrapper/   ← camera GStreamer utilities (existing)
└── wlsonar/                ← WaterLinked sonar Python package (existing)
```

---

## Prerequisites

### 1 — Conda (Miniconda or Anaconda)

Download and install from https://docs.conda.io/en/latest/miniconda.html if you do not have it.

### 2 — GStreamer runtime

GStreamer must be installed **on the system** before creating the conda environment because
the Python bindings (`PyGObject` / `gi`) wrap the native libraries.

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get update
sudo apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    python3-gi \
    python3-gst-1.0
```

**macOS (Homebrew):**
```bash
brew install gstreamer gst-plugins-base gst-plugins-good \
             gst-plugins-bad gst-plugins-ugly gst-libav pygobject3
```

**Windows:**
Install the GStreamer MSVC runtime + development packages from https://gstreamer.freedesktop.org/download/
Install both the **Runtime** and **Development** installers (same version).
Add `C:\gstreamer\1.0\msvc_x86_64\bin` to your `PATH`.

---

## Conda environment setup

### Step 1 — Create a new environment (Python ≥ 3.10 required)

```bash
conda create -n era-rov python=3.11 -y
conda activate era-rov
```

### Step 2 — Install OpenCV

```bash
conda install -c conda-forge opencv -y
```

> This installs `cv2` along with its native image codec dependencies.

### Step 3 — Install NumPy (usually pulled in by OpenCV, but be explicit)

```bash
conda install -c conda-forge numpy -y
```

### Step 4 — Install PyGObject (GStreamer Python bindings)

**Linux / macOS** — install from conda-forge:
```bash
conda install -c conda-forge pygobject gst-plugins-base gstreamer -y
```

**Windows** — conda-forge does not ship GStreamer for Windows.
Use the system GStreamer you installed above and install PyGObject via pip instead:
```bash
pip install PyGObject
```
If pip fails on Windows, download a pre-built wheel from
https://github.com/nicowillis/pygobject-windows-wheel or use the official GNOME
PyGObject installer.

### Step 5 — Install wlsonar dependencies

`python-snappy` requires the native Snappy compression library.

**Linux:**
```bash
sudo apt-get install -y libsnappy-dev
conda install -c conda-forge python-snappy -y
```

**macOS:**
```bash
brew install snappy
conda install -c conda-forge python-snappy -y
```

**Windows:**
```bash
conda install -c conda-forge python-snappy -y
```
> conda-forge ships pre-built binaries for Windows, so no separate native library is needed.

Install the remaining wlsonar dependencies:
```bash
pip install "protobuf>=6.33.2" "requests>=2.32.5"
```

### Step 6 — Install the wlsonar package (editable / local)

```bash
pip install -e ERA/wlsonar
```

> The `-e` flag installs in editable mode so any local changes to `wlsonar/` are reflected
> immediately without reinstalling.

### Step 7 — (Optional) Install matplotlib for 3D point cloud visualization

```bash
conda install -c conda-forge matplotlib -y
```

---

## Verify the installation

```bash
python - <<'EOF'
import cv2
import numpy as np
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import wlsonar
import wlsonar.range_image_protocol as rip
print("OpenCV    :", cv2.__version__)
print("NumPy     :", np.__version__)
Gst.init(None)
print("GStreamer :", Gst.version_string())
print("wlsonar   : OK")
print("All dependencies found.")
EOF
```

Expected output (versions may differ):
```
OpenCV    : 4.x.x
NumPy     : 1.x.x / 2.x.x
GStreamer : GStreamer 1.x.x
wlsonar   : OK
All dependencies found.
```

---

## Quick-start

### Record

```bash
conda activate era-rov
cd ERA
python sync_recorder.py
```

Default ports: camera on UDP **5604**, sonar multicast on **224.0.0.96:4747**.
Press **`q`** in the preview window (or **Ctrl+C**) to stop.

Outputs written to `recordings/`:
```
recordings/
├── camera_20260401_120000.mkv
├── sonar_20260401_120000.sonar
├── camera_20260401_120000_timestamps.csv
└── sonar_20260401_120000_timestamps.csv
```

Available options:
```
--camera-port     5604        UDP port for camera MJPEG stream
--camera-payload  26          RTP payload type
--fps             30          Frame rate written to video file
--sonar-port      4747        UDP port for sonar multicast
--sonar-multicast 224.0.0.96  Sonar multicast group address
--output-dir      recordings  Output directory
```

### Visualize

```bash
python sync_visualizer.py \
    --camera   recordings/camera_20260401_120000.mkv \
    --sonar    recordings/sonar_20260401_120000.sonar \
    --cam-ts   recordings/camera_20260401_120000_timestamps.csv \
    --sonar-ts recordings/sonar_20260401_120000_timestamps.csv
```

Add `--show-3d` for a live matplotlib 3D point cloud window (requires `matplotlib`).

Keyboard controls in the playback window:

| Key | Action |
|-----|--------|
| `Q` / Esc | Quit |
| `Space` | Pause / Resume |
| `→` / `D` | Seek forward 5 s |
| `←` / `A` | Seek backward 5 s |
| `+` / `=` | Double speed (max 8×) |
| `-` | Halve speed (min 0.25×) |

---

## Troubleshooting

**`ImportError: No module named 'gi'`**
→ PyGObject is not installed or not visible to conda.
On Linux run `conda install -c conda-forge pygobject` or `pip install PyGObject`.
On Windows make sure the system GStreamer `bin/` directory is on `PATH`.

**`GStreamer not found` or pipeline parse error**
→ The native GStreamer runtime is missing. Re-run the system-level installation steps above.

**`No camera frame received`**
→ Confirm the BlueROV2 companion computer is streaming MJPEG on UDP port 5604.
Test with: `gst-launch-1.0 udpsrc port=5604 ! fakesink`

**`python-snappy` build failure on Linux**
→ Install the native dev library first: `sudo apt-get install libsnappy-dev`

**Sonar packets not received**
→ Confirm the sonar is configured for UDP multicast output via its HTTP API.
Check that your network interface supports multicast: `ip maddr` (Linux) or `netsh interface ip show joins` (Windows).
