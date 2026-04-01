# BlackBar Remover

A Windows desktop GUI application that detects and removes black bars (letterboxing and pillarboxing) from video files. Built with PyQt6 and powered by FFmpeg, with first-class support for Intel Quick Sync Video (QSV) hardware acceleration.

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-blue)
![FFmpeg](https://img.shields.io/badge/FFmpeg-required-orange)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Features

- **Automatic black bar detection** via FFmpeg's `cropdetect` filter, sampling frames at a configurable interval
- **Side-by-side preview** with a time scrubber — see the original and cropped frame before committing
- **Manual crop editor** — override detected values with exact W/H/X/Y spinboxes or pick a standard aspect ratio
- **Batch processing** — drop a whole folder; detection runs up to 4 files in parallel
- **Three encoding modes**:
  | Mode | Description |
  |------|-------------|
  | QSV – HW Encode | Software decode + Intel QSV hardware encode (most compatible) |
  | QSV – Full HW Pipeline | QSV decode + crop + QSV encode (fastest; requires a QSV-capable decoder) |
  | CPU – Software | libx264 / libx265 fully in software (universal fallback) |
- **Quality & preset controls** — global_quality (QSV) or CRF (CPU), plus speed preset
- **Look-ahead** toggle for better QSV rate control
- **Overwrite original** option (encodes to a temp file, then atomically replaces)
- **Per-file status** table with live encoding progress bar

---

## Requirements

### Python
- Python 3.11 or newer
- PyQt6

```bash
pip install PyQt6
```

### FFmpeg
FFmpeg must be installed and accessible. The application searches these locations automatically (in order):

1. System `PATH` (recommended)
2. `C:\ffmpeg\bin\`
3. `C:\Program Files\ffmpeg\bin\`
4. `C:\Program Files (x86)\ffmpeg\bin\`
5. `~\ffmpeg\bin\`
6. Scoop: `~\scoop\apps\ffmpeg\current\bin\`
7. Chocolatey: `C:\ProgramData\chocolatey\bin\`
8. winget default install path

#### Recommended FFmpeg build (includes QSV support)
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

### Intel Quick Sync Video (optional)
QSV modes require:
- An Intel CPU or GPU with Quick Sync support (6th gen "Skylake" or newer recommended)
- An FFmpeg build compiled with `--enable-libmfx` or `--enable-qsv`
- Up-to-date Intel graphics drivers

If QSV is unavailable the app warns on startup and **CPU mode still works normally**.

---

## Supported Video Formats

`.mp4` `.mkv` `.avi` `.mov` `.ts` `.flv` `.wmv` `.webm` `.m4v`

---

## Installation

```bash
git clone https://github.com/pbzh/BlackBarRemover.git
cd BlackBarRemover
pip install PyQt6
python blackbar_remove.py
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
- Select a row and click **Preview** (or click a row after detection completes — auto-preview kicks in)
- Scrub the timeline to check different timestamps
- Use the **Aspect Ratio** dropdown or the **W / H / X / Y** spinboxes to fine-tune the crop
- Click **Apply** to commit manual changes, **Reset** to revert to auto-detected values

### 4. Configure encoding
| Setting | Description |
|---------|-------------|
| HW Mode | QSV HW Encode / QSV Full HW Pipeline / CPU Software |
| Quality | 1 (best) – 51 (smallest); maps to `global_quality` (QSV) or `CRF` (CPU) |
| Preset | Encoding speed: `veryfast` → `veryslow` |
| Look-ahead | Enable QSV look-ahead for better rate control (QSV HW Encode only) |
| Suffix | String appended to output filename (default `_nocrop`) |
| Overwrite original | Replace source file atomically after successful encode |

### 5. Process
- Click **Process** — only files with detected black bars are encoded
- A progress bar shows per-file encode progress (%)
- Output files land next to the originals (or replace them if *Overwrite original* is checked)

---

## Codec Support Matrix

| Source Codec | QSV Decoder | QSV Encoder | CPU Fallback |
|---|---|---|---|
| H.264 (8-bit) | `h264_qsv` | `h264_qsv` | `libx264` |
| H.264 (10-bit) | `h264_qsv` | *(falls back to CPU)* | `libx264` |
| HEVC / H.265 | `hevc_qsv` | `hevc_qsv` | `libx265` |
| AV1 | `av1_qsv` | `av1_qsv` | — |
| VP9 | `vp9_qsv` | — | `libvpx-vp9` |
| MPEG-2 | `mpeg2_qsv` | — | — |
| VC-1 | `vc1_qsv` | — | — |

Audio and subtitle streams are always copied without re-encoding.

---

## Architecture

```
blackbar_remove.py
├── Constants & codec maps
├── find_ffmpeg_tool()       — PATH + known install locations (no subprocess)
├── check_qsv_available()    — probes FFmpeg hwaccels list
├── get_video_info()         — ffprobe JSON → stream metadata
├── extract_frame()          — single-frame PNG extraction via ffmpeg
├── calc_crop_for_aspect()   — geometry helper for standard aspect ratios
├── CropDetectWorker         — async QProcess wrapper for cropdetect
├── EncodeWorker             — async QProcess wrapper for encoding
├── PreviewPanel             — side-by-side QWidget with scrubber & crop editor
└── BlackBarRemoveApp        — QMainWindow, table, batch orchestration
```

Detection and encoding both use `QProcess` (non-blocking) so the UI never freezes. Frame extraction for preview uses `ThreadPoolExecutor` to run the original and cropped extractions in parallel.

---

## Troubleshooting

**`ffmpeg not found`**
Add ffmpeg to your system PATH or place the binary at `C:\ffmpeg\bin\ffmpeg.exe`.

**`QSV does not appear to be available`**
Install a QSV-enabled FFmpeg build (see [Requirements](#requirements)) and update Intel graphics drivers. Switch to **CPU – Software** mode in the meantime.

**Encoding error with QSV**
Some codec/format combinations lack a QSV encoder. Switch to **CPU – Software** mode; the error message in the log will confirm.

**Preview shows "Failed to extract frame"**
The timestamp may be beyond the video's duration, or the file is corrupted. Try scrubbing to a different position.

---

## License

MIT — see [LICENSE](LICENSE) for details.
