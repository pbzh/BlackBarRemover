# BlackBar Remover

A desktop app that detects and removes black bars (letterboxing and pillarboxing) from videos.
It finds the picture area with FFmpeg's `cropdetect`, lets you check and fine-tune the crop in a
zoomable before/after preview, and re-encodes only the video stream — on the GPU where possible.

Built with Python and PyQt6. Runs on Windows, macOS (Apple Silicon) and Linux.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![PyQt6](https://img.shields.io/badge/GUI-PyQt6-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-blue)
![FFmpeg](https://img.shields.io/badge/FFmpeg-required-orange)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Features

- **Automatic detection** with FFmpeg `cropdetect` — works for 8-bit and 10-bit/HDR sources; short clips are sampled more densely so detection always has enough frames
- **Before/after preview** with a time scrubber, zoom from 10 % to 1600 % (buttons, Ctrl/⌘ + wheel, trackpad pinch, keyboard), drag-to-pan with both panes kept in sync, and a side-by-side / stacked layout
- **Crop editor** — drag the crop box or its handles, type exact W/H/X/Y values, or pick a standard aspect ratio; edits apply immediately and snap to even numbers
- **Batch processing** — load a folder (or drop it on the window); detection runs up to 4 files in parallel
- **Hardware encoding** — AMD AMF (Radeon, incl. RX 9000 series) and Apple VideoToolbox, with CPU fallback
- **Keeps everything else** — audio, subtitles, cover art, chapters and attachments are copied unchanged; only the main video stream is cropped and re-encoded
- **Clear errors** — if an encode fails, FFmpeg's own error output is shown in the log

---

## Download

Ready-to-run builds are produced by GitHub Actions on every change to the app
([Actions tab](https://github.com/pbzh/BlackBarRemover/actions)). Open the latest successful
run of the relevant workflow and download its artifact (GitHub login required; artifacts are
kept for 30 days).

| Platform | Workflow | Artifact |
|---|---|---|
| Windows x64 | **Build Windows app** | `BlackBarRemover-windows-x64` |
| macOS, Apple Silicon | **Build macOS app** | `BlackBarRemover-macos-arm64` |

FFmpeg is **not bundled** — install it separately (see [FFmpeg](#ffmpeg)).

**Windows:** unzip and run `BlackBar Remover\BlackBar Remover.exe` (keep the other files next to
it). The app is unsigned, so SmartScreen may warn: **More info → Run anyway**.

**macOS:** unzip, then remove the quarantine flag once, because the app is unsigned:

```bash
xattr -dr com.apple.quarantine "BlackBar Remover.app"
```

### Run from source

```bash
git clone https://github.com/pbzh/BlackBarRemover.git
cd BlackBarRemover
pip install PyQt6
python blackbar_remove.py
```

Requires Python 3.10 or newer.

---

## FFmpeg

`ffmpeg` and `ffprobe` must be installed. The app looks for them on `PATH` first, then in:

- **Windows:** `C:\ffmpeg\bin\`, `C:\Program Files\ffmpeg\bin\`, `C:\Program Files (x86)\ffmpeg\bin\`, `~\ffmpeg\bin\`, Scoop (`~\scoop\apps\ffmpeg\current\bin\`), Chocolatey (`C:\ProgramData\chocolatey\bin\`)
- **macOS:** `/opt/homebrew/bin/`, `/usr/local/bin/`, `~/bin/`
- **Linux:** `/usr/bin/`, `/usr/local/bin/`, `/snap/bin/`, `~/bin/`

At startup the log shows which FFmpeg was found and which hardware encoders are available.

**Windows (AMD):** download `ffmpeg-master-latest-win64-gpl` from
[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) and put `ffmpeg.exe` and `ffprobe.exe`
in `C:\ffmpeg\bin`. Install current AMD Adrenalin drivers. Check AMF support with:

```powershell
ffmpeg -hide_banner -encoders | findstr amf
```

(`winget install Gyan.FFmpeg`, `scoop install ffmpeg` or `choco install ffmpeg` also work, as long
as the check above lists the `*_amf` encoders.)

**macOS:** `brew install ffmpeg` (includes VideoToolbox).

**Linux:** use your distribution's FFmpeg. AMF on Linux additionally needs AMD's proprietary AMF
runtime; without it, use CPU mode.

---

## Usage

1. **Load** — choose **File** or **Folder (batch)** and click **Browse…**, paste a path and press
   Enter, or drop a file/folder onto the window. Supported: `.mp4` `.mkv` `.avi` `.mov` `.ts`
   `.flv` `.wmv` `.webm` `.m4v`.
2. **Detect** — click **Detect Black Bars**. *Sample interval* sets the seconds between analysed
   frames (lower = more thorough, slower). Results appear in the table; files without bars are
   marked *No black bars* and skipped when processing.
3. **Preview & adjust** — the first file with bars is previewed automatically; select or
   double-click any row to preview it.
   - Scrub the timeline to check other scenes.
   - Adjust the crop by dragging the red box or its handles, with the **Aspect** dropdown, or the
     **W / H / X / Y** fields. **Reset** returns to the detected crop.
   - Zoom with **− / + / Fit / 1:1**, Ctrl/⌘ + mouse wheel, trackpad pinch, or Ctrl/⌘ `+` `−`
     `0` (fit) `1` (100 %). Drag the image to pan. From 200 % pixels are shown unsmoothed, so bar
     edges can be checked exactly.
   - **⇅ / ⇆** switches between side-by-side and stacked panes (stacked suits very wide films).
4. **Settings** — pick the HW mode, quality and preset (see below).
5. **Process** — click **Process**. Output is written next to the source as
   `<name>_cropped.<ext>` (same container), or replaces the source if *Overwrite original* is
   checked. **Cancel** stops detection or processing and deletes the partially written output file.

### Settings

| Setting | Description |
|---|---|
| HW mode | Encoding path; only the modes available on your platform are listed (see below) |
| Quality | 1 (best, largest) – 51 (smallest). Constant QP for AMF, `-q:v` 100–1 for VideoToolbox, CRF for CPU. Default 23 |
| Preset | `veryfast` … `veryslow`. AMF maps it to speed / balanced / quality; not used by VideoToolbox |
| Sample interval | Seconds between frames analysed during detection (default 15 s) |
| Output suffix | Appended to the output file name (default `_cropped`) |
| Overwrite original | Encode to `<name>_tmp.<ext>`, then replace the source with it. There is no backup — keep a copy if you need one |

---

## Hardware modes

| Mode | Platform | Decode | Crop | Encode |
|---|---|---|---|---|
| AMF – HW Encode (AMD) | Windows, Linux | CPU | CPU | GPU (AMF) |
| AMF – Full HW Pipeline (AMD) | Windows | GPU (D3D11VA) | CPU | GPU (AMF) |
| VideoToolbox – HW Encode | macOS | CPU | CPU | GPU (VideoToolbox) |
| VideoToolbox – Full HW Pipeline | macOS | GPU (VideoToolbox) | CPU | GPU (VideoToolbox) |
| CPU – Software | all | CPU | CPU | CPU (libx264 / libx265 / libvpx-vp9) |

**HW Encode vs. Full HW Pipeline:** the only difference is where the source is decoded. Output
quality is identical.

- **Full HW Pipeline** decodes on the GPU. Best for heavy sources (4K, HEVC, 10-bit/HDR, AV1) and
  leaves the CPU almost idle.
- **HW Encode** decodes on the CPU. Works with every source codec and is often just as fast for
  1080p H.264.
- On Windows, Full HW decodes H.264, HEVC, VP9 and AV1 on the GPU; other codecs (e.g. MPEG-2,
  VC-1 — no longer hardware-decodable on RDNA4) automatically fall back to CPU decode. On macOS,
  use **HW Encode** if Full HW fails for an unusual source codec.
- VideoToolbox constant-quality encoding requires Apple Silicon.

### Output codec

The output keeps the source codec where the selected encoder supports it:

| Source | AMF | VideoToolbox | CPU |
|---|---|---|---|
| H.264 | `h264_amf` | `h264_videotoolbox` | `libx264` |
| HEVC | `hevc_amf` | `hevc_videotoolbox` | `libx265` |
| AV1 | `av1_amf` (RDNA3 or newer) | `h264_videotoolbox` | `libx264` |
| VP9 | `h264_amf` | `h264_videotoolbox` | `libvpx-vp9` |
| Other | `h264_amf` | `h264_videotoolbox` | `libx264` |

Hardware H.264 encoders are 8-bit only — use **CPU** mode for 10-bit H.264 sources.

---

## Troubleshooting

**`ffmpeg not found`** — install FFmpeg (see [FFmpeg](#ffmpeg)) and restart the app.

**No AMF / VideoToolbox encoder found** — your FFmpeg build lacks hardware encoder support. Use a
BtbN build on Windows or Homebrew FFmpeg on macOS, and update GPU drivers. CPU mode always works.

**An encode fails** — the log shows FFmpeg's error output below the failed file. Common causes:
`av1_amf` on a pre-RDNA3 GPU, a 10-bit H.264 source in a hardware mode, or an unusual source codec
in *Full HW Pipeline* mode. Try **HW Encode**, then **CPU – Software**.

**Detection failed** — the file may be damaged, or FFmpeg could not decode it. Check the file in
a player and try again with a smaller sample interval.

**Preview shows "Failed to extract frame"** — scrub to a different position; the file may be
damaged at that point.

---

## Building the apps yourself

The workflows in `.github/workflows/` package the app with PyInstaller. To build locally:

**macOS**

```bash
pip install PyQt6 pyinstaller pillow
pyinstaller --noconfirm --windowed --name "BlackBar Remover" \
  --icon assets/appicon.png --add-data "assets:assets" blackbar_remove.py
```

**Windows (PowerShell)**

```powershell
py -m pip install PyQt6 pyinstaller pillow
py -m PyInstaller --noconfirm --windowed --name "BlackBar Remover" `
  --icon assets/appicon.png --add-data "assets;assets" blackbar_remove.py
```

The result is in `dist/`. Both workflows can also be started manually from the Actions tab
(**Run workflow**).

---

## How it works

Everything lives in a single file, `blackbar_remove.py`:

```
blackbar_remove.py
├── find_ffmpeg_tool() / check_hw_available()   FFmpeg discovery and HW encoder probing
├── get_video_info()                            ffprobe → codec, size, duration, main video stream
├── CropDetectWorker                            cropdetect via QProcess; most frequent crop wins
├── EncodeWorker                                builds the FFmpeg command per HW mode; progress + error capture
├── ZoomImageView / CropCanvas / ZoomScrollArea zoomable frame views, crop overlay, pan & zoom input
├── PreviewPanel                                scrubber, before/after panes, crop editing
└── BlackBarRemoveApp                           main window, file table, batch detection & encoding
```

- **Detection** runs `cropdetect` on frames sampled every *N* seconds and picks the most
  frequently reported crop.
- **Preview** extracts one frame per scrub position; the "after" pane shows the crop region of
  that same frame, so it updates live while you edit.
- **Encoding** maps all streams, stream-copies everything, and re-encodes only the main video
  stream with the crop filter — so cover art, subtitles and audio pass through untouched.
- FFmpeg runs through `QProcess` (detection, encoding) and background threads (ffprobe, frame
  extraction), so the UI never blocks.

---

## License

MIT — see [LICENSE](LICENSE) for details.
