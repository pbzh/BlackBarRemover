# BlackBar Remover

A desktop application that detects and removes black bars (letterboxing and pillarboxing) from video files. The current GUI is a Wails app with a Go backend, a lightweight HTML/CSS/JS frontend, and FFmpeg/FFprobe for media analysis and encoding.

![Go](https://img.shields.io/badge/Go-1.22%2B-blue)
![Wails](https://img.shields.io/badge/Wails-v2-blue)
![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows%20%7C%20Linux-blue)
![FFmpeg](https://img.shields.io/badge/FFmpeg-required-orange)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Features

- **Automatic black bar detection** via FFmpeg's `cropdetect` filter, sampling frames at a configurable interval
- **Before/after preview** with a time scrubber, zoom (up to 1600 %, Ctrl/⌘ + wheel or trackpad pinch), drag-to-pan with both panes kept in sync, and a side-by-side / stacked layout toggle
- **Manual crop editor** — drag the crop box or its handles, type exact W/H/X/Y values, or pick a standard aspect ratio; changes apply immediately
- **Batch processing** — drop a whole folder; detection runs up to 4 files in parallel
- **Hardware-aware encoding modes**:
  | Mode | Description |
  |------|-------------|
  | AMF (AMD) – HW Encode | Software decode + AMD AMF hardware encode (RDNA/RDNA2/RDNA3/RDNA4 GPUs incl. RX 9070 XT) |
  | AMF (AMD) – Full HW Pipeline | D3D11VA decode + crop + AMF encode (Windows only) |
  | VideoToolbox – HW Encode | Software decode + Apple VideoToolbox hardware encode on macOS |
  | VideoToolbox – Full HW Pipeline | VideoToolbox decode + crop + encode on macOS |
  | CPU – Software | libx264 / libx265 fully in software (universal fallback) |
- **Quality & preset controls** — CQP (AMF), VideoToolbox quality, or CRF (CPU), plus speed preset
- **Overwrite original** option (encodes to a temp file, then replaces with backup/restore protection)
- **Per-file status** table with live encoding progress bar

---

## Requirements

### Wails app

- Go 1.22 or newer
- Wails v2 CLI
- FFmpeg and FFprobe available on `PATH` or in a known install location

Install Wails:

```bash
go install github.com/wailsapp/wails/v2/cmd/wails@latest
```

### Legacy Python app

The repository still includes `blackbar_remove.py`, the original PyQt6 implementation. Use it only if you specifically want the Python version.

```bash
pip install PyQt6
python blackbar_remove.py
```

### FFmpeg
FFmpeg must be installed and accessible. The application searches these locations automatically (in order):

1. System `PATH` (recommended)
2. `C:\ffmpeg\bin\`
3. `C:\Program Files\ffmpeg\bin\`
4. `~\ffmpeg\bin\`
5. Scoop: `~\scoop\apps\ffmpeg\current\bin\`
6. macOS Homebrew: `/opt/homebrew/bin/` or `/usr/local/bin/`
7. Linux: `/usr/bin/`, `/usr/local/bin/`, `/snap/bin/`, or `~/bin/`

#### Recommended FFmpeg build (includes AMF support)
Download a full build from **[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)** — choose a `ffmpeg-master-latest-win64-gpl` release.

#### Quick install options
```powershell
# winget
winget install Gyan.FFmpeg

# Scoop
scoop install ffmpeg

# Chocolatey
choco install ffmpeg
```

### Hardware acceleration (optional)

#### AMD AMF (Advanced Media Framework)
AMF modes require:
- An AMD GPU with VCN encode support (Polaris RX 400 series and newer; RDNA1/2/3/4 incl. RX 9070 XT all supported)
- **Windows**: AMD Adrenalin drivers installed
- **Linux**: Mesa with VA-API and the `amdgpu` kernel driver
- An FFmpeg build with `--enable-amf` (BtbN GPL builds include this on Windows)

AV1 encode (`av1_amf`) requires RDNA3 or newer (RX 7000 / RX 9000 series). HEVC and H.264 work on older RDNA generations as well.

If AMF is unavailable the app warns on startup; CPU mode still works.

#### Apple VideoToolbox
VideoToolbox modes require macOS and an FFmpeg build with VideoToolbox support. Homebrew FFmpeg is usually sufficient:

```bash
brew install ffmpeg
```

---

## Supported Video Formats

`.mp4` `.mkv` `.avi` `.mov` `.ts` `.flv` `.wmv` `.webm` `.m4v`

---

## Installation

### Run from source

```bash
git clone https://github.com/pbzh/BlackBarRemover.git
cd BlackBarRemover
pip install PyQt6
python blackbar_remove.py
```

### macOS app (Apple Silicon)

The **Build macOS app** GitHub Actions workflow (`.github/workflows/build-macos.yml`)
builds `BlackBar Remover.app` with PyInstaller on an Apple Silicon runner. It runs on
pushes that touch the app, the assets or the workflow, and can be started manually
from the Actions tab. Download the `BlackBarRemover-macos-arm64` artifact from the run.

The app is unsigned, so remove the quarantine flag after unzipping:

```bash
xattr -dr com.apple.quarantine "BlackBar Remover.app"
```

FFmpeg is not bundled; install it with `brew install ffmpeg`.

To build locally on a Mac instead:

```bash
pip install PyQt6 pyinstaller pillow
pyinstaller --noconfirm --windowed --name "BlackBar Remover" \
  --icon assets/appicon.png --add-data "assets:assets" blackbar_remove.py
```

### Windows app (x64, e.g. AMD RX 9000 series)

The **Build Windows app** workflow (`.github/workflows/build-windows.yml`) builds
`BlackBar Remover.exe` on a Windows runner. It runs on the same kind of pushes as the macOS
build and can be started manually. Download the `BlackBarRemover-windows-x64` artifact,
unzip it, and run `BlackBar Remover.exe` from inside the unzipped folder (keep the files
next to it). Windows SmartScreen may warn because the app is unsigned: **More info → Run anyway**.

FFmpeg is not bundled. For AMD AMF encoding, download `ffmpeg-master-latest-win64-gpl`
from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) and put `ffmpeg.exe` and
`ffprobe.exe` in `C:\ffmpeg\bin` (or anywhere on `PATH`), with current AMD Adrenalin drivers
installed. Check with `ffmpeg -hide_banner -encoders | findstr amf`.

To build locally on Windows instead (PowerShell):

```powershell
py -m pip install PyQt6 pyinstaller pillow
py -m PyInstaller --noconfirm --windowed --name "BlackBar Remover" `
  --icon assets/appicon.png --add-data "assets;assets" blackbar_remove.py
```

---

## Usage

### 1. Load files
- Select **Single File** or **Folder (Batch)**, then click **Browse…**
- All supported video files are listed in the table with their resolution and codec

### 2. Detect black bars
- Adjust **Sample Interval** (seconds between sampled frames; lower = more accurate, slower)
- Click **Detect Black Bars**
- Up to 4 files are analysed in parallel; results appear in the table as they finish
- Files with no black bars are marked *No black bars* and skipped during processing

### 3. Preview & adjust (optional)
- Select or double-click a row (after detection the first file with bars is previewed automatically)
- Scrub the timeline to check different timestamps
- Adjust the crop by dragging the red box or its handles, with the **Aspect** dropdown, or the **W / H / X / Y** fields — edits apply immediately (values snap to even numbers); **Reset** reverts to the detected crop
- Zoom with **− / + / Fit / 1:1**, Ctrl/⌘ + mouse wheel, trackpad pinch, or Ctrl/⌘ `+` `−` `0` (fit) `1` (100 %); drag the image to pan. From 200 % pixels are shown unsmoothed so bar edges can be checked exactly
- **⇅ / ⇆** switches between side-by-side and stacked panes

### 4. Configure encoding
| Setting | Description |
|---------|-------------|
| HW Mode | AMF / VideoToolbox / CPU modes, filtered by platform |
| Quality | 1 (best) – 51 (smallest); maps to constant QP (AMF), VideoToolbox quality, or `CRF` (CPU) |
| Preset | Encoding speed: `veryfast` → `veryslow` |
| Output suffix | String appended to output filename (default `_cropped`) |
| Overwrite original | Encode to a temporary file, then replace the source with backup/restore protection |

### 5. Process
- Click **Process** — only files with detected black bars are encoded
- A progress bar shows per-file encode progress (%)
- Output files land next to the originals (or replace them if *Overwrite original* is checked)

---

## Codec Support Matrix

| Source Codec | AMF Encoder | VideoToolbox Encoder | CPU Encoder |
|---|---|---|---|
| H.264 (8-bit) | `h264_amf` | `h264_videotoolbox` | `libx264` |
| H.264 (10-bit) | `h264_amf` (8-bit only — use CPU) | `h264_videotoolbox` (8-bit only — use CPU) | `libx264` |
| HEVC / H.265 | `hevc_amf` | `hevc_videotoolbox` | `libx265` |
| AV1 | `av1_amf` (RDNA3+) | `h264_videotoolbox` | `libx264` |
| VP9 | `h264_amf` | `h264_videotoolbox` | `libvpx-vp9` |
| Other | `h264_amf` | `h264_videotoolbox` | `libx264` |

Audio and subtitle streams are always copied without re-encoding.

---

## Architecture

### Wails app

```
wails-app/
├── main.go                  # Wails application setup and window options
├── app.go                   # Wails-bound app methods, dialogs, state, events
├── ffmpeg.go                # FFmpeg/FFprobe discovery, cropdetect, encode args, crop math
├── frontend/
│   ├── index.html           # Application shell
│   └── src/
│       ├── app.js           # UI state, Wails calls, event handlers, preview rendering
│       └── style.css        # Desktop UI styling
└── wails.json               # Wails build configuration
```

Detection and encoding run in Go goroutines and publish progress through Wails runtime events. Detection updates shared crop state under a mutex; processing uses immutable file snapshots for safer concurrent behavior. Preview scrubbing guards against stale asynchronous frame results so older FFmpeg frame extractions cannot overwrite newer scrub positions.

Encoding captures the tail of FFmpeg stderr and logs it on failure, which makes codec, filter, permission, and hardware acceleration problems easier to diagnose.

### Legacy Python app

```
blackbar_remove.py
├── Constants & codec maps
├── find_ffmpeg_tool()       — PATH + known install locations (no subprocess)
├── check_hw_available()     — probes FFmpeg hwaccels + encoders lists
├── get_video_info()         — ffprobe JSON → stream metadata
├── extract_frame()          — single-frame PNG extraction via ffmpeg
├── calc_crop_for_aspect()   — geometry helper for standard aspect ratios
├── CropDetectWorker         — async QProcess wrapper for cropdetect
├── EncodeWorker             — async QProcess wrapper for encoding
├── ZoomImageView / CropCanvas / ZoomScrollArea — zoomable frame views, crop overlay, pan & zoom input
├── PreviewPanel             — before/after panes with scrubber, zoom & crop editor
└── BlackBarRemoveApp        — QMainWindow, table, batch orchestration
```

The Python app uses `QProcess` for detection and encoding, a `ThreadPoolExecutor` for parallel ffprobe on load, and a background thread for preview frame extraction.

---

## Troubleshooting

**`ffmpeg not found`**
Add ffmpeg to your system PATH or place the binary at `C:\ffmpeg\bin\ffmpeg.exe`.

**Encoding error with AMF**
`av1_amf` only runs on RDNA3 (RX 7000) or newer GPUs. For older AMD cards, pick HEVC/H.264 source codecs or switch to **CPU – Software**. Update Adrenalin drivers if AMF reports `NotSupported`.

**Encoding error with VideoToolbox**
Some codecs or pixel formats are not supported by VideoToolbox. Switch to **CPU – Software** mode or use a source format supported by the VideoToolbox encoder.

**Preview shows "Failed to extract frame"**
The timestamp may be beyond the video's duration, or the file is corrupted. Try scrubbing to a different position.

**Overwrite original fails**
The Wails app writes a temporary encoded file first, then replaces the source. If replacement fails because of file permissions, locks, or cross-device filesystem behavior, the original file is restored from a temporary backup when possible.

---

## License

MIT — see [LICENSE](LICENSE) for details.
