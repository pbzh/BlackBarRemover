# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BlackBarRemover is a video processing tool that removes black bars (letterboxing/pillarboxing) from videos using FFmpeg's `cropdetect` filter. There are **two parallel implementations**:
- `blackbar_remove.py` — original Python/PyQt6 desktop app
- `wails-app/` — cross-platform rewrite in Go with a browser-based frontend (Wails v2 + vanilla JS)

Active development is on the Wails app; the Python app is feature-complete but not the primary focus.

## Commands

### Python App
```bash
pip install PyQt6
python blackbar_remove.py
```

### Wails App
```bash
cd wails-app

# Install Wails CLI (first time only)
go install github.com/wailsapp/wails/v2/cmd/wails@latest

# Development with hot reload
wails dev

# Production build → build/bin/BlackBarRemove.app (macOS) or .exe (Windows)
wails build
```

**Requirements:** Go 1.22+, Node.js/npm (auto-managed by Wails), FFmpeg installed on system.

## Architecture

### Data Flow (both apps)
1. **File loading** → extract metadata via `ffprobe` (resolution, codec, duration)
2. **Crop detection** → run `ffmpeg -vf "fps=1/N,cropdetect=24:16:0"`, collect all reported crop regions, pick the most frequent one
3. **Preview** → extract PNG frames at given timestamps with optional crop filter applied, show side-by-side comparison
4. **Encoding** → construct FFmpeg args based on codec + hardware mode, run with progress piped back to UI

### Wails App Structure

**Backend (Go):**
- `app.go` — `App` struct with exported methods callable from JS: `LoadFiles()`, `StartDetection()`, `StartProcessing()`, `GetFrame()`, `CalcCropForAspect()`, `CancelOperation()`, `CheckFFmpeg()`
- `ffmpeg.go` — all FFmpeg logic: tool discovery, video info extraction, frame extraction, cropdetect, encode arg construction, encoding runner
- `main.go` — Wails entry point only

**Frontend (vanilla JS):**
- `frontend/src/app.js` — all UI logic (state, event handling, table management, preview rendering, crop overlay canvas)
- `frontend/wailsjs/` — auto-generated JS/TS bindings; **do not edit manually**

**Frontend ↔ Backend communication:**
- Method calls: `await window.go.main.App.MethodName(args)` → returns `Promise<T>`
- Events from Go to JS: `wailsrt.EventsEmit(ctx, "event:name", data)` → `window.runtime.EventsOn("event:name", callback)`
- Key events: `detect:start`, `detect:result`, `detect:complete`, `encode:progress`, `encode:done`, `log`

### Hardware Acceleration

Three encoding paths selected at runtime:
- **QSV (Intel Quick Sync):** `h264_qsv`/`hevc_qsv`/`av1_qsv` encoders; uses `global_quality` for rate control
- **VideoToolbox (macOS/Apple Silicon):** `h264_videotoolbox`/`hevc_videotoolbox`; quality mapped from CRF scale (1–51) to `q:v` (0.0–1.0)
- **CPU fallback:** `libx264`/`libx265`/`libvpx-vp9`; uses standard `crf`

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
