# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BlackBarRemover is a video processing tool that removes black bars (letterboxing/pillarboxing) from videos using FFmpeg's `cropdetect` filter.

The **Python/PyQt6 desktop app** (`blackbar_remove.py`) is the single, primary implementation. It is a personal tool run directly from source, so easy iteration matters more than packaged distribution.

> **History:** An earlier `wails-app/` cross-platform rewrite in Go (Wails v2 + vanilla JS) was removed when the project consolidated back onto the Python app. It remains recoverable from git history if ever needed.

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
2. **Crop detection** → run `ffmpeg -vf "fps=1/N,cropdetect=24:16:0"`, collect all reported crop regions, pick the most frequent one
3. **Preview** → extract PNG frames at given timestamps with optional crop filter applied, show side-by-side comparison
4. **Encoding** → construct FFmpeg args based on codec + hardware mode, run with progress piped back to UI

### Hardware Acceleration

Encoding paths selected at runtime via the "HW Mode" dropdown (`_HW_MODES_ALL` filters
options by platform). Each maps to an encoder map + rate-control convention in
`EncodeWorker.__init__`:
- **QSV (Intel Quick Sync)** — `qsv` / `qsv_fullhw`: `h264_qsv`/`hevc_qsv`/`av1_qsv`; `-global_quality` rate control; the full-HW pipeline decodes on QSV surfaces, downloads for the CPU crop filter, then re-uploads for encode.
- **AMF (AMD Radeon)** — `amf` / `amf_fullhw`: `h264_amf`/`hevc_amf`/`av1_amf` (`AMF_ENCODERS`); constant-QP rate control (`-rc cqp` with `-qp_i/-qp_p/-qp_b`) mapped from the 1–51 quality scale, rescaled to 0–255 for `av1_amf`; the libx264-style preset is translated to AMF's `-quality speed/balanced/quality` via `amf_quality_from_preset()`. AMF is encode-only in FFmpeg, so `amf_fullhw` pairs it with a Windows `d3d11va` hardware decode (guarded by `D3D11VA_DECODABLE`), crops on CPU frames, and hands them straight to the encoder (no hwupload). `av1_amf` requires RDNA3+.
- **VideoToolbox (macOS/Apple Silicon)** — `vt` / `vt_fullhw`: `h264_videotoolbox`/`hevc_videotoolbox`; quality mapped from CRF scale (1–51) to `q:v` (0.0–1.0).
- **CPU fallback** — `cpu`: `libx264`/`libx265`/`libvpx-vp9`; standard `-crf`.

`check_hw_available()` probes `ffmpeg -hwaccels` for QSV/VideoToolbox and `ffmpeg -encoders`
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
- Frame extraction returns base64-encoded PNG for embedding directly in `<img>` or canvas
