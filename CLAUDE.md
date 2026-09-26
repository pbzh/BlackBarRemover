# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BlackBarRemover is a video processing tool that removes black bars (letterboxing/pillarboxing) from videos using FFmpeg's `cropdetect` filter.

The **Python/PyQt6 desktop app** (`blackbar_remove.py`) is the single, primary implementation. It is a personal tool run directly from source, so easy iteration matters more than packaged distribution.

> **History:** An earlier `wails-app/` cross-platform rewrite in Go (Wails v2 + vanilla JS) was removed when the project consolidated back onto the Python app. It remains recoverable from git history if ever needed. Intel QSV encoding (`qsv` / `qsv_fullhw` modes) was likewise removed and can be restored from history.

## Commands

### Python App
```bash
pip install PyQt6
python blackbar_remove.py
```

**Requirements:** Python 3.10+ (uses `X | Y` type unions), PyQt6, and FFmpeg installed on the system (with the `h264_amf` encoder for AMD hardware acceleration — e.g. a BtbN FFmpeg build on Windows).

## Architecture

### Data Flow
1. **File loading** → extract metadata via `ffprobe` (resolution, codec, duration)
2. **Crop detection** → run `ffmpeg -vf "fps=1/N,cropdetect=limit=0.0941:round=16:reset=0"` (limit is a fraction of max pixel value — `CROPDETECT_LIMIT` = 24/255 — so it works for 8- and 10-bit sources), collect all reported crop regions, pick the most frequent one
3. **Preview** → extract PNG frames at given timestamps with optional crop filter applied, show side-by-side comparison
4. **Encoding** → construct FFmpeg args based on codec + hardware mode, run with progress piped back to UI

### Hardware Acceleration

Encoding paths selected at runtime via the "HW Mode" dropdown (`_HW_MODES_ALL` filters
options by platform). Each maps to an encoder map + rate-control convention in
`EncodeWorker.__init__`:
- **AMF (AMD Radeon)** — `amf` / `amf_fullhw`: `h264_amf`/`hevc_amf`/`av1_amf` (`AMF_ENCODERS`); constant-QP rate control (`-rc cqp` with `-qp_i/-qp_p/-qp_b`) mapped from the 1–51 quality scale, rescaled to 0–255 for `av1_amf`; the libx264-style preset is translated to AMF's `-quality speed/balanced/quality` via `amf_quality_from_preset()`. AMF is encode-only in FFmpeg, so `amf_fullhw` pairs it with a Windows `d3d11va` hardware decode (guarded by `D3D11VA_DECODABLE`), crops on CPU frames, and hands them straight to the encoder (no hwupload). `av1_amf` requires RDNA3+.
- **VideoToolbox (macOS/Apple Silicon)** — `vt` / `vt_fullhw`: `h264_videotoolbox`/`hevc_videotoolbox`; quality mapped from CRF scale (1–51) to `q:v` 100–1 (FFmpeg divides by 100 internally). Constant-quality `q:v` works on Apple Silicon only, which is the only supported Mac target.
- **CPU fallback** — `cpu`: `libx264`/`libx265`/`libvpx-vp9`; standard `-crf`.

`check_hw_available()` probes `ffmpeg -hwaccels` for VideoToolbox and `ffmpeg -encoders`
for `h264_amf` (AMF encoders are not reported by `-hwaccels`).

Audio and subtitles are always stream-copied (no re-encoding).

### Python App Structure (`blackbar_remove.py`)

Single-file, ~1900 lines. Key classes:
- `BlackBarRemoveApp` (QMainWindow) — main orchestrator
- `CropDetectWorker` / `EncodeWorker` — QProcess wrappers for async FFmpeg calls
- `CropCanvas` — interactive crop region editor overlay on QLabel
- `PreviewPanel` — side-by-side before/after preview with timestamp scrubber

Uses `ThreadPoolExecutor` for parallel frame extraction; QProcess for non-blocking FFmpeg subprocesses.

## FFmpeg Integration Notes

- Tool discovery checks PATH first, then platform-specific locations (`/opt/homebrew/bin/` on macOS, `C:\ffmpeg\bin\` on Windows)
- `ffprobe` output parsed as JSON; 10-bit depth detected from pixel format string containing `10`
- Cropdetect collects all `crop=W:H:X:Y` lines from stderr, returns the most common value
- Frame extraction writes a temp PNG and returns its path; background threads hand results to the GUI thread via queued signals (never `QTimer.singleShot` from a Python thread — it is never delivered)
