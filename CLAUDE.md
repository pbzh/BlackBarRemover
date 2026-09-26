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

**Builds:** GitHub Actions workflows `build-macos.yml` (Apple Silicon .app) and `build-windows.yml` (x64 .exe) package the app with PyInstaller and upload zipped artifacts.

**Requirements:** Python 3.10+ (uses `X | Y` type unions), PyQt6, and FFmpeg installed on the system (with the `h264_amf` encoder for AMD hardware acceleration — e.g. a BtbN FFmpeg build on Windows).

## Architecture

### Data Flow
1. **File loading** → extract metadata via `ffprobe` (resolution, codec, duration)
2. **Crop detection** → run `ffmpeg -vf "fps=1/N,cropdetect=limit=0.0941:round=16:reset=0"` (limit is a fraction of max pixel value — `CROPDETECT_LIMIT` = 24/255 — so it works for 8- and 10-bit sources), collect all reported crop regions, pick the most frequent one
3. **Preview** → extract one PNG frame at the scrubber timestamp; the "after" pane shows the crop region of that same frame (crop is pixel-exact, so no second ffmpeg call and it updates live while editing)
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

All streams are kept (`-map 0 -c copy`); only the main video stream is cropped and re-encoded via `-c:v:N` / `-filter:v:N`, where N is `video_info["video_index"]` — the first video stream that is not cover art (`disposition.attached_pic`). Never use a plain `-vf`: it would apply the crop to cover-art streams too and fail.

On encode failure, `EncodeWorker` passes the last ~20 non-progress ffmpeg output lines (run with `-loglevel warning`) to `on_done`, and the app writes them to the log.

### Python App Structure (`blackbar_remove.py`)

Single-file, ~2250 lines. Key classes:
- `BlackBarRemoveApp` (QMainWindow) — main orchestrator
- `CropDetectWorker` / `EncodeWorker` — QProcess wrappers for async FFmpeg calls
- `PreviewPanel` — before/after panes with scrubber, zoom (shared scale; fit or fixed `ZOOM_LEVELS`), synced scrolling and crop editing. Crop edits apply to `info["crop"]` immediately; the `crop_changed` signal to the main window is debounced for spinbox edits
- `ZoomImageView` — paints only the exposed region at any scale (downscales from a DPR-aware smooth cache; ≥200 % unsmoothed); `CropCanvas` subclasses it with the crop overlay
- `ZoomScrollArea` — Ctrl/⌘+wheel and pinch emit zoom requests; left/middle drag pans (canvas ignores presses outside the crop box so they reach it)

Uses a `ThreadPoolExecutor` for parallel ffprobe on file load, a background thread per preview frame extraction, and QProcess for non-blocking detection/encoding.

## FFmpeg Integration Notes

- Blocking ffmpeg/ffprobe calls go through `run_hidden()` (adds `CREATE_NO_WINDOW` on Windows) — never call `subprocess.run` directly, or the windowed .exe flashes console windows
- Tool discovery checks PATH first, then platform-specific locations (`/opt/homebrew/bin/` on macOS, `C:\ffmpeg\bin\` on Windows)
- `ffprobe` output parsed as JSON; 10-bit depth detected from pixel format string containing `10`
- Cropdetect collects all `crop=W:H:X:Y` lines from stderr, returns the most common value
- Frame extraction writes a temp PNG and returns its path; background threads hand results to the GUI thread via queued signals (never `QTimer.singleShot` from a Python thread — it is never delivered)
