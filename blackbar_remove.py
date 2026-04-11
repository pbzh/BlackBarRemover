#!/usr/bin/env python3
"""BlackBar Remove - Detect and remove black bars from videos.
Supports Intel QSV and Apple VideoToolbox hardware acceleration.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PyQt6.QtCore import QPoint, QProcess, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".ts",
    ".flv",
    ".wmv",
    ".webm",
    ".m4v",
}
SUPPORTED_FORMATS = (
    "Video Files (*.mp4 *.mkv *.avi *.mov *.ts *.flv *.wmv *.webm *.m4v);;All Files (*)"
)

# QSV hardware decoder map: codec name -> qsv decoder
QSV_DECODERS: dict[str, str] = {
    "h264": "h264_qsv",
    "hevc": "hevc_qsv",
    "h265": "hevc_qsv",
    "vp9": "vp9_qsv",
    "av1": "av1_qsv",
    "mpeg2video": "mpeg2_qsv",
    "vc1": "vc1_qsv",
}

# QSV hardware encoder map: codec name -> qsv encoder
QSV_ENCODERS: dict[str, str] = {
    "h264": "h264_qsv",
    "hevc": "hevc_qsv",
    "h265": "hevc_qsv",
    "av1": "av1_qsv",
}

# Software fallback encoder map
SW_ENCODERS: dict[str, str] = {
    "h264": "libx264",
    "hevc": "libx265",
    "h265": "libx265",
    "vp9": "libvpx-vp9",
}

# VideoToolbox hardware encoder map (Apple Silicon / macOS)
VT_ENCODERS: dict[str, str] = {
    "h264": "h264_videotoolbox",
    "hevc": "hevc_videotoolbox",
    "h265": "hevc_videotoolbox",
    "prores": "prores_videotoolbox",
}

# Maximum concurrent cropdetect workers when processing a batch
MAX_DETECT_WORKERS = 4

# QSV quality range: 1 (best quality) – 51 (smallest file).  23 is a balanced default.
QSV_QUALITY_DEFAULT = 23
QSV_PRESETS = ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
QSV_PRESET_DEFAULT = "medium"

# Hardware-acceleration mode labels shown in the UI (filtered by platform)
_HW_MODES_ALL = [
    ("QSV – HW Encode",                "qsv",        ("win32", "linux")),
    ("QSV – Full HW Pipeline",         "qsv_fullhw", ("win32", "linux")),
    ("VideoToolbox – HW Encode",        "vt",         ("darwin",)),
    ("VideoToolbox – Full HW Pipeline", "vt_fullhw",  ("darwin",)),
    ("CPU – Software",                  "cpu",        None),
]
HW_MODES = [
    (label, key)
    for label, key, platforms in _HW_MODES_ALL
    if platforms is None or sys.platform in platforms
]

ASPECT_RATIOS = [
    ("Custom", None),
    ("From detection", None),
    ("16:9", (16, 9)),
    ("4:3", (4, 3)),
    ("21:9", (21, 9)),
    ("2.35:1 (Cinema)", (2.35, 1)),
    ("2.39:1 (Anamorphic)", (2.39, 1)),
    ("1.85:1", (1.85, 1)),
    ("1:1 (Square)", (1, 1)),
    ("9:16 (Portrait)", (9, 16)),
    ("4:5 (Portrait)", (4, 5)),
    ("3:2", (3, 2)),
    ("5:4", (5, 4)),
]


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe auto-detection
# ---------------------------------------------------------------------------


def find_ffmpeg_tool(name: str) -> str:
    """Locate an ffmpeg tool on the current platform.

    Checks the system PATH first via shutil.which (no subprocess overhead),
    then falls back to common installation directories for the current OS.
    Returns the first match found, or *name* as a last resort so that a
    clear 'not found' error surfaces at runtime.
    """
    found = shutil.which(name)
    if found:
        return found

    home = Path.home()
    candidates: list[Path] = []

    if sys.platform == "win32":
        exe = f"{name}.exe"
        candidates = [
            Path(rf"C:\ffmpeg\bin\{exe}"),
            Path(rf"C:\Program Files\ffmpeg\bin\{exe}"),
            Path(rf"C:\Program Files (x86)\ffmpeg\bin\{exe}"),
            home / "ffmpeg" / "bin" / exe,
            home / "scoop" / "apps" / "ffmpeg" / "current" / "bin" / exe,
            Path(rf"C:\ProgramData\chocolatey\bin\{exe}"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            Path(f"/opt/homebrew/bin/{name}"),
            Path(f"/usr/local/bin/{name}"),
            home / "bin" / name,
        ]
    else:  # Linux and others
        candidates = [
            Path(f"/usr/bin/{name}"),
            Path(f"/usr/local/bin/{name}"),
            Path(f"/snap/bin/{name}"),
            home / "bin" / name,
        ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return name


def check_qsv_available() -> bool:
    """Return True if FFmpeg was built with QSV support and the Intel GPU is accessible."""
    try:
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-hwaccels"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "qsv" in result.stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def check_vt_available() -> bool:
    """Return True if FFmpeg was built with VideoToolbox support."""
    try:
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-hwaccels"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "videotoolbox" in result.stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


FFMPEG = find_ffmpeg_tool("ffmpeg")
FFPROBE = find_ffmpeg_tool("ffprobe")


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def parse_crop(crop_str: str) -> tuple[int, int, int, int]:
    """Parse 'crop=W:H:X:Y' → (W, H, X, Y)."""
    parts = crop_str.replace("crop=", "").split(":")
    return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])


def crop_to_vf_qsv_fullhw(crop_filter: str) -> str:
    """Convert 'crop=W:H:X:Y' to a filter chain suitable for the full QSV HW pipeline.

    Strategy: after QSV hardware decode (-hwaccel_output_format qsv) the
    frames live on GPU surfaces.  We download to system memory, apply the
    CPU crop filter, then upload the smaller frame back to QSV surfaces
    for hardware encoding.  This avoids the fragile vpp_qsv crop parameter
    differences across FFmpeg versions while still offloading decode/encode.
    """
    return f"hwdownload,format=nv12,{crop_filter},hwupload=extra_hw_frames=64"


def crop_to_vf_vt_fullhw(crop_filter: str) -> str:
    """Convert 'crop=W:H:X:Y' to a filter chain for the VideoToolbox full HW pipeline.

    With -hwaccel videotoolbox -hwaccel_output_format videotoolbox, frames
    live on GPU surfaces.  We download to system memory for the CPU crop
    filter; the VideoToolbox encoder accepts CPU frames directly.
    """
    return f"hwdownload,format=nv12,{crop_filter}"


def get_video_info(filepath: str) -> dict | None:
    """Return video/audio stream metadata via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            [
                FFPROBE,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                filepath,
            ],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        info: dict = {"path": filepath}

        for stream in data.get("streams", []):
            if stream["codec_type"] == "video" and "video_codec" not in info:
                codec = stream["codec_name"].lower()
                info["video_codec"] = codec
                info["width"] = int(stream["width"])
                info["height"] = int(stream["height"])
                info["pix_fmt"] = stream.get("pix_fmt", "yuv420p")
                # Detect 10-bit content – QSV h264 only supports 8-bit
                info["is_10bit"] = "10" in stream.get("pix_fmt", "")
            elif stream["codec_type"] == "audio" and "audio_codec" not in info:
                info["audio_codec"] = stream["codec_name"]

        fmt = data.get("format", {})
        info["duration"] = float(fmt.get("duration", 0))
        return info if "video_codec" in info else None
    except (json.JSONDecodeError, FileNotFoundError, KeyError, ValueError):
        return None


def extract_frame(
    filepath: str, timestamp: float, crop_filter: str | None = None
) -> str | None:
    """Extract a single frame as PNG to a temp file.  Returns the path or None."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()

    vf = crop_filter if crop_filter else "null"
    result = subprocess.run(
        [
            FFMPEG,
            "-ss",
            str(timestamp),
            "-i",
            filepath,
            "-vf",
            vf,
            "-frames:v",
            "1",
            "-y",
            tmp.name,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and os.path.getsize(tmp.name) > 0:
        return tmp.name
    os.unlink(tmp.name)
    return None


def calc_crop_for_aspect(
    orig_w: int, orig_h: int, ar_w: float, ar_h: float
) -> tuple[int, int, int, int]:
    """Return (crop_w, crop_h, offset_x, offset_y) for the target aspect ratio, centred."""
    target_ratio = ar_w / ar_h
    current_ratio = orig_w / orig_h

    if abs(current_ratio - target_ratio) < 0.001:
        return orig_w, orig_h, 0, 0

    if current_ratio > target_ratio:  # too wide → pillarbox
        new_h = orig_h
        new_w = int(orig_h * target_ratio)
    else:  # too tall → letterbox
        new_w = orig_w
        new_h = int(orig_w / target_ratio)

    new_w = new_w - (new_w % 2)  # encoders require even dimensions
    new_h = new_h - (new_h % 2)
    new_w = min(new_w, orig_w)
    new_h = min(new_h, orig_h)

    off_x = (orig_w - new_w) // 2
    off_y = (orig_h - new_h) // 2
    off_x = off_x - (off_x % 2)
    off_y = off_y - (off_y % 2)

    return new_w, new_h, off_x, off_y


def format_timestamp(seconds: float) -> str:
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    return f"{h}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Async workers (QProcess-based so the UI never blocks)
# ---------------------------------------------------------------------------


class CropDetectWorker:
    """Runs ffmpeg cropdetect asynchronously via QProcess."""

    def __init__(self, filepath: str, sample_interval: int, on_done):
        self.filepath = filepath
        self.sample_interval = sample_interval
        self.on_done = on_done
        self._output = ""
        self.process = QProcess()
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finished)

    def start(self):
        self.process.start(
            FFMPEG,
            [
                "-i",
                self.filepath,
                "-vf",
                f"fps=1/{self.sample_interval},cropdetect=24:16:0",
                "-f",
                "null",
                "-",
            ],
        )

    def _read(self):
        data = (
            self.process.readAllStandardOutput()
            .data()
            .decode("utf-8", errors="replace")
        )
        self._output += data

    def _finished(self):
        crops = re.findall(r"crop=(\d+:\d+:\d+:\d+)", self._output)
        if crops:
            best = Counter(crops).most_common(1)[0][0]
            self.on_done(self.filepath, f"crop={best}")
        else:
            self.on_done(self.filepath, None)


class EncodeWorker:
    """Runs ffmpeg encoding asynchronously via QProcess.

    Supports five hardware modes:
      qsv         – software decode, QSV hardware encode  (Windows/Linux)
      qsv_fullhw  – QSV hardware decode + crop + QSV encode  (fastest on Intel)
      vt          – software decode, VideoToolbox hardware encode  (macOS)
      vt_fullhw   – VideoToolbox decode + crop + VT encode  (fastest on macOS)
      cpu         – fully software encode via libx264 / libx265
    """

    def __init__(
        self,
        filepath: str,
        output_path: str,
        crop_filter: str,
        hw_mode: str,
        video_info: dict,
        quality: int,
        look_ahead: bool,
        preset: str,
        on_progress,
        on_done,
    ):
        self.filepath = filepath
        self.output_path = output_path
        self.duration = video_info.get("duration", 0)
        self.on_progress = on_progress
        self.on_done = on_done
        self.process = QProcess()
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(self._finished)

        video_codec = video_info.get("video_codec", "h264").lower()
        is_10bit = video_info.get("is_10bit", False)

        pre_input_args: list[str] = []
        vf_filter = crop_filter
        encoder: str
        enc_args: list[str] = []

        if hw_mode in ("qsv", "qsv_fullhw"):
            if video_codec == "h264" and is_10bit:
                encoder = "libx264"
                enc_args = ["-crf", str(quality), "-preset", preset]
            else:
                encoder = QSV_ENCODERS.get(video_codec, "h264_qsv")
                enc_args = [
                    "-global_quality",
                    str(quality),
                    "-preset",
                    preset,
                ]
                if look_ahead and hw_mode != "qsv_fullhw":
                    # look_ahead performs better when frames are in system memory
                    enc_args += ["-look_ahead", "1", "-look_ahead_depth", "40"]

            if hw_mode == "qsv_fullhw":
                decoder = QSV_DECODERS.get(video_codec)
                if decoder:
                    pre_input_args = [
                        "-hwaccel",
                        "qsv",
                        "-hwaccel_output_format",
                        "qsv",
                        "-c:v",
                        decoder,
                    ]
                    vf_filter = crop_to_vf_qsv_fullhw(crop_filter)
                else:
                    # No QSV decoder for this codec – still use QSV encode
                    vf_filter = crop_filter

        elif hw_mode in ("vt", "vt_fullhw"):
            encoder = VT_ENCODERS.get(video_codec, "h264_videotoolbox")
            # Map quality 1–51 (lower=better) to VT q:v 1.0–0.0 (higher=better)
            vt_q = max(0.01, 1.0 - (quality - 1) / 50.0)
            enc_args = ["-q:v", f"{vt_q:.2f}", "-allow_sw", "1"]

            if hw_mode == "vt_fullhw":
                pre_input_args = [
                    "-hwaccel",
                    "videotoolbox",
                    "-hwaccel_output_format",
                    "videotoolbox",
                ]
                vf_filter = crop_to_vf_vt_fullhw(crop_filter)

        else:  # cpu
            encoder = SW_ENCODERS.get(video_codec, "libx264")
            enc_args = ["-crf", str(quality), "-preset", preset]

        self.args = [
            *pre_input_args,
            "-i",
            filepath,
            "-vf",
            vf_filter,
            "-c:v",
            encoder,
            *enc_args,
            "-c:a",
            "copy",
            "-c:s",
            "copy",
            "-map",
            "0",
            "-progress",
            "pipe:1",
            "-y",
            output_path,
        ]

    def start(self):
        self.process.start(FFMPEG, self.args)

    def _read(self):
        data = (
            self.process.readAllStandardOutput()
            .data()
            .decode("utf-8", errors="replace")
        )
        for line in data.splitlines():
            if line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=")[1])
                    if self.duration > 0:
                        pct = min(100.0, (us / 1_000_000) / self.duration * 100)
                        self.on_progress(self.filepath, pct)
                except ValueError:
                    pass

    def _finished(self):
        self.on_done(self.filepath, self.process.exitCode() == 0)


# ---------------------------------------------------------------------------
# Interactive crop canvas
# ---------------------------------------------------------------------------


class CropCanvas(QLabel):
    """QLabel subclass that displays a frame with a draggable crop overlay.

    The user can drag the crop rectangle or its eight edge/corner handles to
    adjust the crop interactively without typing numbers.
    """

    # emitted continuously while dragging (cw, ch, cx, cy in original pixels)
    crop_changed = pyqtSignal(int, int, int, int)
    # emitted once on mouse-release so callers can trigger an expensive update
    crop_committed = pyqtSignal(int, int, int, int)

    _HANDLE = 8   # half-size of each handle square in display pixels
    _CURSOR = {
        "nw": Qt.CursorShape.SizeFDiagCursor,
        "se": Qt.CursorShape.SizeFDiagCursor,
        "ne": Qt.CursorShape.SizeBDiagCursor,
        "sw": Qt.CursorShape.SizeBDiagCursor,
        "n":  Qt.CursorShape.SizeVerCursor,
        "s":  Qt.CursorShape.SizeVerCursor,
        "w":  Qt.CursorShape.SizeHorCursor,
        "e":  Qt.CursorShape.SizeHorCursor,
        "move": Qt.CursorShape.SizeAllCursor,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setMinimumSize(200, 150)
        self.setStyleSheet("background: #000;")
        self.setMouseTracking(True)

        self._src: QPixmap | None = None   # original unscaled frame
        self._scale = 1.0                  # display / original pixel ratio
        self._orig_w = 0
        self._orig_h = 0
        self._crop: tuple[int, int, int, int] | None = None  # (cw, ch, cx, cy)

        # drag state
        self._drag_mode: str | None = None
        self._drag_anchor: QPoint | None = None
        self._drag_crop0: tuple | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_frame(self, pixmap: QPixmap, crop: tuple[int, int, int, int], avail_w: int):
        """Set source frame, initial crop, and available display width."""
        self._src = pixmap
        self._orig_w = pixmap.width()
        self._orig_h = pixmap.height()
        self._scale = min(1.0, avail_w / self._orig_w) if avail_w > 0 and self._orig_w > 0 else 1.0
        self._crop = crop
        self._redraw()

    def set_crop(self, crop: tuple[int, int, int, int]):
        """Update crop without reloading the frame (called from spinboxes)."""
        self._crop = crop
        if self._src:
            self._redraw()

    def clear_frame(self):
        self._src = None
        self._crop = None
        self.clear()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _redraw(self):
        if not self._src or self._src.isNull():
            return
        s = self._scale
        dw = round(self._orig_w * s)
        dh = round(self._orig_h * s)
        scaled = self._src.scaled(dw, dh,
                                   Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        overlay = QPixmap(scaled)
        p = QPainter(overlay)

        if self._crop:
            cw, ch, cx, cy = self._crop
            dx, dy = round(cx * s), round(cy * s)
            dw2, dh2 = round(cw * s), round(ch * s)

            # Darken everything outside the crop area
            p.setOpacity(0.55)
            p.fillRect(0, 0, dw, dh, QColor(0, 0, 0))

            # Restore the crop region at full brightness
            p.setOpacity(1.0)
            p.drawPixmap(dx, dy, scaled, dx, dy, dw2, dh2)

            # Red border
            p.setPen(QPen(Qt.GlobalColor.red, 2))
            p.drawRect(dx + 1, dy + 1, dw2 - 2, dh2 - 2)

            # White handles at corners and edge midpoints
            h = self._HANDLE
            p.setPen(QPen(QColor(255, 255, 255, 220), 1))
            p.setBrush(QColor(255, 255, 255, 210))
            for hx, hy in (
                (dx, dy), (dx + dw2, dy), (dx, dy + dh2), (dx + dw2, dy + dh2),
                (dx + dw2 // 2, dy), (dx + dw2 // 2, dy + dh2),
                (dx, dy + dh2 // 2), (dx + dw2, dy + dh2 // 2),
            ):
                p.drawRect(hx - h // 2, hy - h // 2, h, h)

        p.end()
        self.setPixmap(overlay)
        self.adjustSize()

    # ------------------------------------------------------------------
    # Hit-testing
    # ------------------------------------------------------------------

    def _hit_zones(self) -> dict[str, QRect]:
        if not self._crop:
            return {}
        cw, ch, cx, cy = self._crop
        s = self._scale
        dx, dy = round(cx * s), round(cy * s)
        dw, dh = round(cw * s), round(ch * s)
        h = self._HANDLE + 2
        mx, my = dx + dw // 2, dy + dh // 2
        return {
            "nw": QRect(dx - h, dy - h, 2 * h, 2 * h),
            "ne": QRect(dx + dw - h, dy - h, 2 * h, 2 * h),
            "sw": QRect(dx - h, dy + dh - h, 2 * h, 2 * h),
            "se": QRect(dx + dw - h, dy + dh - h, 2 * h, 2 * h),
            "n":  QRect(mx - h, dy - h, 2 * h, 2 * h),
            "s":  QRect(mx - h, dy + dh - h, 2 * h, 2 * h),
            "w":  QRect(dx - h, my - h, 2 * h, 2 * h),
            "e":  QRect(dx + dw - h, my - h, 2 * h, 2 * h),
        }

    def _hit(self, pos: QPoint) -> str | None:
        for name, rect in self._hit_zones().items():
            if rect.contains(pos):
                return name
        if not self._crop:
            return None
        cw, ch, cx, cy = self._crop
        s = self._scale
        if QRect(round(cx * s), round(cy * s), round(cw * s), round(ch * s)).contains(pos):
            return "move"
        return None

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._crop:
            return
        mode = self._hit(event.pos())
        if mode:
            self._drag_mode = mode
            self._drag_anchor = event.pos()
            self._drag_crop0 = self._crop

    def mouseMoveEvent(self, event):
        if not self._crop:
            return
        if not self._drag_mode:
            hit = self._hit(event.pos())
            self.setCursor(self._CURSOR.get(hit, Qt.CursorShape.ArrowCursor)
                           if hit else Qt.CursorShape.ArrowCursor)
            return

        s = self._scale
        if s <= 0:
            return
        delta = event.pos() - self._drag_anchor
        dx = round(delta.x() / s)
        dy = round(delta.y() / s)
        cw, ch, cx, cy = self._drag_crop0
        W, H = self._orig_w, self._orig_h

        def snap(v):   return v - (v % 2)
        def clamp(v, lo, hi): return max(lo, min(hi, snap(v)))

        m = self._drag_mode
        if m == "move":
            cx = clamp(cx + dx, 0, W - cw)
            cy = clamp(cy + dy, 0, H - ch)
        elif m == "e":
            cw = clamp(cw + dx, 2, W - cx)
        elif m == "s":
            ch = clamp(ch + dy, 2, H - cy)
        elif m == "w":
            ncx = clamp(cx + dx, 0, cx + cw - 2)
            cw = max(2, snap(cw + cx - ncx)); cx = ncx
        elif m == "n":
            ncy = clamp(cy + dy, 0, cy + ch - 2)
            ch = max(2, snap(ch + cy - ncy)); cy = ncy
        elif m == "se":
            cw = clamp(cw + dx, 2, W - cx); ch = clamp(ch + dy, 2, H - cy)
        elif m == "sw":
            ncx = clamp(cx + dx, 0, cx + cw - 2); cw = max(2, snap(cw + cx - ncx)); cx = ncx
            ch = clamp(ch + dy, 2, H - cy)
        elif m == "ne":
            cw = clamp(cw + dx, 2, W - cx)
            ncy = clamp(cy + dy, 0, cy + ch - 2); ch = max(2, snap(ch + cy - ncy)); cy = ncy
        elif m == "nw":
            ncx = clamp(cx + dx, 0, cx + cw - 2); cw = max(2, snap(cw + cx - ncx)); cx = ncx
            ncy = clamp(cy + dy, 0, cy + ch - 2); ch = max(2, snap(ch + cy - ncy)); cy = ncy

        self._crop = (cw, ch, cx, cy)
        self._redraw()
        self.crop_changed.emit(cw, ch, cx, cy)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_mode:
            if self._crop:
                self.crop_committed.emit(*self._crop)
            self._drag_mode = self._drag_anchor = self._drag_crop0 = None


# ---------------------------------------------------------------------------
# Preview panel
# ---------------------------------------------------------------------------


class PreviewPanel(QWidget):
    """Side-by-side preview with time scrubber, crop overlay and manual crop editing."""

    crop_changed = pyqtSignal(str, str)  # (filepath, new_crop_string)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tmp_files: list[str] = []
        self._current_info: dict | None = None
        self._orig_pixmap: QPixmap | None = None
        self._suppress_spinbox_signals = False
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Info bar
        self.lbl_info = QLabel("Select a file and run detection, then click Preview")
        self.lbl_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_info.setStyleSheet(
            "font-weight: bold; padding: 4px; background: #2a2a2a; color: #ccc;"
        )
        layout.addWidget(self.lbl_info)

        # Time scrubber
        scrubber_row = QHBoxLayout()
        self.lbl_time = QLabel("0:00:00")
        self.lbl_time.setFixedWidth(60)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(0)
        self.slider.setEnabled(False)
        self.lbl_duration = QLabel("0:00:00")
        self.lbl_duration.setFixedWidth(60)
        self.lbl_duration.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        scrubber_row.addWidget(self.lbl_time)
        scrubber_row.addWidget(self.slider, 1)
        scrubber_row.addWidget(self.lbl_duration)
        layout.addLayout(scrubber_row)

        self._scrub_timer = QTimer(singleShot=True, interval=300)
        self._scrub_timer.timeout.connect(self._update_frames)
        self.slider.valueChanged.connect(self._on_slider_moved)

        # Manual crop controls
        crop_group = QGroupBox("Crop Area")
        crop_outer = QVBoxLayout(crop_group)

        ar_row = QHBoxLayout()
        ar_row.addWidget(QLabel("Aspect Ratio:"))
        self.cb_aspect = QComboBox()
        for name, _ in ASPECT_RATIOS:
            self.cb_aspect.addItem(name)
        self.cb_aspect.setFixedWidth(180)
        self.cb_aspect.setEnabled(False)
        self.cb_aspect.currentIndexChanged.connect(self._on_aspect_ratio_changed)
        ar_row.addWidget(self.cb_aspect)
        ar_row.addSpacing(10)
        self.lbl_ar_info = QLabel("")
        self.lbl_ar_info.setStyleSheet("color: #888;")
        ar_row.addWidget(self.lbl_ar_info)
        ar_row.addStretch()
        crop_outer.addLayout(ar_row)

        spinbox_row = QHBoxLayout()
        for label, attr, max_val in (("W:", "sp_w", 9999), ("H:", "sp_h", 9999)):
            spinbox_row.addWidget(QLabel(label))
            sb = QSpinBox()
            sb.setRange(2, max_val)
            sb.setSingleStep(2)
            sb.setFixedWidth(80)
            setattr(self, attr, sb)
            spinbox_row.addWidget(sb)
        spinbox_row.addSpacing(10)
        for label, attr in (("X:", "sp_x"), ("Y:", "sp_y")):
            spinbox_row.addWidget(QLabel(label))
            sb = QSpinBox()
            sb.setRange(0, 9999)
            sb.setSingleStep(2)
            sb.setFixedWidth(80)
            setattr(self, attr, sb)
            spinbox_row.addWidget(sb)
        spinbox_row.addSpacing(15)

        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setToolTip("Apply crop values and refresh cropped preview")
        self.btn_apply.clicked.connect(self._apply_crop)
        self.btn_apply.setEnabled(False)
        spinbox_row.addWidget(self.btn_apply)

        self.btn_reset = QPushButton("Reset")
        self.btn_reset.setToolTip("Reset to auto-detected crop values")
        self.btn_reset.clicked.connect(self._reset_crop)
        self.btn_reset.setEnabled(False)
        spinbox_row.addWidget(self.btn_reset)

        spinbox_row.addStretch()
        crop_outer.addLayout(spinbox_row)

        self._crop_edit_timer = QTimer(singleShot=True, interval=400)
        self._crop_edit_timer.timeout.connect(self._live_update_overlay)

        for sp in (self.sp_w, self.sp_h, self.sp_x, self.sp_y):
            sp.valueChanged.connect(self._on_spinbox_changed)
            sp.setEnabled(False)

        layout.addWidget(crop_group)

        # Side-by-side image area
        images_layout = QHBoxLayout()
        for header_text, img_attr, scroll_attr, size_attr in (
            (
                "Original (crop area highlighted)",
                "lbl_orig_img",
                "scroll_orig",
                "lbl_orig_size",
            ),
            ("After Crop", "lbl_crop_img", "scroll_crop", "lbl_crop_size"),
        ):
            col = QVBoxLayout()
            hdr = QLabel(header_text)
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hdr.setStyleSheet("font-weight: bold;")
            col.addWidget(hdr)

            if img_attr == "lbl_orig_img":
                img_lbl = CropCanvas()
                img_lbl.crop_changed.connect(self._on_canvas_crop_changed)
                img_lbl.crop_committed.connect(self._on_canvas_crop_committed)
            else:
                img_lbl = QLabel()
                img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                img_lbl.setStyleSheet("background: #000;")
                img_lbl.setMinimumSize(200, 150)
            setattr(self, img_attr, img_lbl)

            scroll = QScrollArea()
            scroll.setWidget(img_lbl)
            scroll.setWidgetResizable(True)
            scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
            setattr(self, scroll_attr, scroll)
            col.addWidget(scroll, 1)

            sz_lbl = QLabel("")
            sz_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sz_lbl.setStyleSheet("color: #888;")
            setattr(self, size_attr, sz_lbl)
            col.addWidget(sz_lbl)

            images_layout.addLayout(col)

        layout.addLayout(images_layout, 1)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup_tmp(self):
        for f in self._tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        self._tmp_files.clear()

    def _crop_from_spinboxes(self) -> str:
        return (
            f"crop={self.sp_w.value()}:{self.sp_h.value()}"
            f":{self.sp_x.value()}:{self.sp_y.value()}"
        )

    def _set_spinboxes_from_crop(self, crop: str):
        cw, ch, cx, cy = parse_crop(crop)
        self._suppress_spinbox_signals = True
        self.sp_w.setValue(cw)
        self.sp_h.setValue(ch)
        self.sp_x.setValue(cx)
        self.sp_y.setValue(cy)
        self._suppress_spinbox_signals = False

    def _set_spinbox_limits(self, info: dict):
        w, h = info["width"], info["height"]
        self._suppress_spinbox_signals = True
        self.sp_w.setRange(2, w)
        self.sp_h.setRange(2, h)
        self.sp_x.setRange(0, w - 2)
        self.sp_y.setRange(0, h - 2)
        self._suppress_spinbox_signals = False

    def _on_aspect_ratio_changed(self, index: int):
        if self._suppress_spinbox_signals or not self._current_info:
            return
        info = self._current_info
        orig_w, orig_h = info["width"], info["height"]
        name, ratio = ASPECT_RATIOS[index]

        if name == "Custom":
            self.lbl_ar_info.setText("Free edit — set any crop values")
            return
        if name == "From detection":
            auto_crop = info.get("crop_auto")
            if auto_crop:
                self._set_spinboxes_from_crop(auto_crop)
                cw, ch, *_ = parse_crop(auto_crop)
                self.lbl_ar_info.setText(f"Auto-detected: {cw}×{ch} ({cw / ch:.3f}:1)")
                self._live_update_overlay()
            return
        if ratio is None:
            return

        cw, ch, cx, cy = calc_crop_for_aspect(orig_w, orig_h, *ratio)
        self._set_spinboxes_from_crop(f"crop={cw}:{ch}:{cx}:{cy}")
        self.lbl_ar_info.setText(f"{cw}×{ch} ({cw / ch:.3f}:1)")
        self._live_update_overlay()

    def _on_spinbox_changed(self, _value: int):
        if self._suppress_spinbox_signals or not self._current_info:
            return
        info = self._current_info
        w, h = info["width"], info["height"]
        self._suppress_spinbox_signals = True
        if self.sp_x.value() + self.sp_w.value() > w:
            self.sp_w.setValue(w - self.sp_x.value())
        if self.sp_y.value() + self.sp_h.value() > h:
            self.sp_h.setValue(h - self.sp_y.value())
        self._suppress_spinbox_signals = False

        if self.cb_aspect.currentIndex() != 0:
            self._suppress_spinbox_signals = True
            self.cb_aspect.setCurrentIndex(0)
            self.lbl_ar_info.setText("Free edit — set any crop values")
            self._suppress_spinbox_signals = False

        self._crop_edit_timer.start()

    def _live_update_overlay(self):
        if not self._orig_pixmap or self._orig_pixmap.isNull():
            return
        crop = self._crop_from_spinboxes()
        cw, ch, cx, cy = parse_crop(crop)
        self.lbl_orig_img.set_crop((cw, ch, cx, cy))
        self._update_info_label(crop)

    def _update_info_label(self, crop: str):
        info = self._current_info
        if not info:
            return
        cw, ch, *_ = parse_crop(crop)
        orig_w, orig_h = info["width"], info["height"]
        removed_h = orig_h - ch
        removed_w = orig_w - cw
        bar_desc = []
        if removed_h > 0:
            bar_desc.append(f"{removed_h}px horizontal")
        if removed_w > 0:
            bar_desc.append(f"{removed_w}px vertical")
        modified = (
            " (manual)" if crop != info.get("crop_auto", info.get("crop")) else ""
        )
        self.lbl_info.setText(
            f"{Path(info['path']).name}  |  "
            f"{orig_w}×{orig_h} → {cw}×{ch}  |  "
            f"Removing {', '.join(bar_desc) if bar_desc else 'nothing'}  |  "
            f"{crop}{modified}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_file(self, info: dict):
        self._cleanup_tmp()
        self._orig_pixmap = None
        self._current_info = info
        crop = info.get("crop")

        if not crop:
            self.lbl_info.setText(
                f"{Path(info['path']).name} — no crop data (run detection first)"
            )
            self._clear_images()
            self._set_crop_controls_enabled(False)
            self.slider.setEnabled(False)
            return

        if "crop_auto" not in info:
            info["crop_auto"] = crop

        cw, ch, *_ = parse_crop(crop)
        orig_w, orig_h = info["width"], info["height"]

        if cw == orig_w and ch == orig_h:
            self.lbl_info.setText(f"{Path(info['path']).name} — no black bars detected")
            self._clear_images()
            self._set_crop_controls_enabled(False)
            self.slider.setEnabled(False)
            return

        self._set_spinbox_limits(info)
        self._set_spinboxes_from_crop(crop)
        self._set_crop_controls_enabled(True)

        self._suppress_spinbox_signals = True
        self.cb_aspect.setCurrentIndex(1)  # "From detection"
        self._suppress_spinbox_signals = False
        self.lbl_ar_info.setText(f"Auto-detected: {cw}×{ch} ({cw / ch:.3f}:1)")
        self._update_info_label(crop)

        duration = info.get("duration", 0)
        self.slider.setEnabled(True)
        self.slider.setRange(0, max(1, int(duration)))
        self.slider.setValue(min(30, int(duration / 2)))
        self.lbl_duration.setText(format_timestamp(duration))
        self._update_frames()

    def clear(self):
        self._cleanup_tmp()
        self._current_info = None
        self._orig_pixmap = None
        self._clear_images()
        self.lbl_info.setText("Select a file and run detection, then click Preview")
        self.slider.setEnabled(False)
        self._set_crop_controls_enabled(False)
        self._suppress_spinbox_signals = True
        self.cb_aspect.setCurrentIndex(0)
        self._suppress_spinbox_signals = False
        self.lbl_ar_info.setText("")

    # ------------------------------------------------------------------
    # Private rendering helpers
    # ------------------------------------------------------------------

    def _set_crop_controls_enabled(self, enabled: bool):
        for sp in (self.sp_w, self.sp_h, self.sp_x, self.sp_y):
            sp.setEnabled(enabled)
        self.btn_apply.setEnabled(enabled)
        self.btn_reset.setEnabled(enabled)
        self.cb_aspect.setEnabled(enabled)

    def _clear_images(self):
        self.lbl_orig_img.clear_frame()
        self.lbl_crop_img.clear()
        self.lbl_orig_size.setText("")
        self.lbl_crop_size.setText("")
        self.lbl_time.setText("0:00:00")
        self.lbl_duration.setText("0:00:00")
        self._orig_pixmap = None

    def _on_slider_moved(self, value: int):
        self.lbl_time.setText(format_timestamp(value))
        self._scrub_timer.start()

    def _update_frames(self):
        info = self._current_info
        if not info or not info.get("crop"):
            return
        self._cleanup_tmp()
        timestamp = self.slider.value()
        crop = info["crop"]
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_orig = pool.submit(extract_frame, info["path"], timestamp)
                f_crop = pool.submit(extract_frame, info["path"], timestamp, crop)
                orig_path = f_orig.result()
                crop_path = f_crop.result()
        finally:
            QApplication.restoreOverrideCursor()

        if orig_path:
            self._tmp_files.append(orig_path)
            self._orig_pixmap = QPixmap(orig_path)
            self._draw_overlay(self._orig_pixmap, crop)
        else:
            self.lbl_orig_img.setText("Failed to extract frame")
            self.lbl_orig_size.setText("")
            self._orig_pixmap = None

        if crop_path:
            self._tmp_files.append(crop_path)
            self._show_cropped(crop_path)
        else:
            self.lbl_crop_img.setText("Failed to extract frame")
            self.lbl_crop_size.setText("")

    def _draw_overlay(self, pixmap: QPixmap, crop: str):
        """Load source frame into the CropCanvas with the current crop overlay."""
        if pixmap.isNull():
            return
        cw, ch, cx, cy = parse_crop(crop)
        avail_w = max(50, self.scroll_orig.viewport().width() - 4)
        self.lbl_orig_img.load_frame(pixmap, (cw, ch, cx, cy), avail_w)
        self.lbl_orig_size.setText(f"{pixmap.width()}×{pixmap.height()}")

    def _show_cropped(self, img_path: str):
        pixmap = QPixmap(img_path)
        if pixmap.isNull():
            return
        full_w, full_h = pixmap.width(), pixmap.height()
        available_w = self.scroll_crop.viewport().width() - 4
        if available_w > 50 and pixmap.width() > available_w:
            pixmap = pixmap.scaledToWidth(
                available_w, Qt.TransformationMode.SmoothTransformation
            )
        self.lbl_crop_img.setPixmap(pixmap)
        self.lbl_crop_img.adjustSize()
        self.lbl_crop_size.setText(f"{full_w}×{full_h}")

    def _on_canvas_crop_changed(self, cw: int, ch: int, cx: int, cy: int):
        """Called continuously while the user drags the crop overlay."""
        self._suppress_spinbox_signals = True
        self.sp_w.setValue(cw)
        self.sp_h.setValue(ch)
        self.sp_x.setValue(cx)
        self.sp_y.setValue(cy)
        if self.cb_aspect.currentIndex() != 0:
            self.cb_aspect.setCurrentIndex(0)
            self.lbl_ar_info.setText("Free edit — drag to adjust")
        self._suppress_spinbox_signals = False
        crop = f"crop={cw}:{ch}:{cx}:{cy}"
        if self._current_info:
            self._current_info["crop"] = crop
        self._update_info_label(crop)

    def _on_canvas_crop_committed(self, cw: int, ch: int, cx: int, cy: int):
        """Called once when the user releases the mouse after dragging."""
        info = self._current_info
        if not info:
            return
        crop = f"crop={cw}:{ch}:{cx}:{cy}"
        info["crop"] = crop
        timestamp = self.slider.value()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            crop_path = extract_frame(info["path"], timestamp, crop)
        finally:
            QApplication.restoreOverrideCursor()
        if crop_path:
            self._tmp_files.append(crop_path)
            self._show_cropped(crop_path)
        self.crop_changed.emit(info["path"], crop)

    def _apply_crop(self):
        info = self._current_info
        if not info:
            return
        new_crop = self._crop_from_spinboxes()
        info["crop"] = new_crop
        self._update_info_label(new_crop)
        timestamp = self.slider.value()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            crop_path = extract_frame(info["path"], timestamp, new_crop)
        finally:
            QApplication.restoreOverrideCursor()
        if crop_path:
            self._tmp_files.append(crop_path)
            self._show_cropped(crop_path)
        if self._orig_pixmap and not self._orig_pixmap.isNull():
            self._draw_overlay(self._orig_pixmap, new_crop)
        self.crop_changed.emit(info["path"], new_crop)

    def _reset_crop(self):
        info = self._current_info
        if not info:
            return
        auto_crop = info.get("crop_auto")
        if not auto_crop:
            return
        info["crop"] = auto_crop
        self._set_spinboxes_from_crop(auto_crop)
        self._suppress_spinbox_signals = True
        self.cb_aspect.setCurrentIndex(1)
        self._suppress_spinbox_signals = False
        cw, ch, *_ = parse_crop(auto_crop)
        self.lbl_ar_info.setText(f"Auto-detected: {cw}×{ch} ({cw / ch:.3f}:1)")
        self._update_info_label(auto_crop)
        timestamp = self.slider.value()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            crop_path = extract_frame(info["path"], timestamp, auto_crop)
        finally:
            QApplication.restoreOverrideCursor()
        if crop_path:
            self._tmp_files.append(crop_path)
            self._show_cropped(crop_path)
        if self._orig_pixmap and not self._orig_pixmap.isNull():
            self._draw_overlay(self._orig_pixmap, auto_crop)
        self.crop_changed.emit(info["path"], auto_crop)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------


class BlackBarRemoveApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BlackBar Remove")
        self.setMinimumSize(1060, 780)

        self.files: list[dict] = []
        self.crop_workers: list[CropDetectWorker] = []
        self.encode_worker: EncodeWorker | None = None
        self.encode_queue: list[dict] = []
        self._detect_index = 0
        self._active_detect_count = 0
        self._encode_index = 0

        self._build_ui()
        self._check_ffmpeg_on_startup()

    # ------------------------------------------------------------------
    # Startup checks
    # ------------------------------------------------------------------

    def _check_ffmpeg_on_startup(self):
        """Warn the user if ffmpeg is not found or HW accel is unavailable."""
        try:
            result = subprocess.run(
                [FFMPEG, "-version"], capture_output=True, timeout=8
            )
            if result.returncode != 0:
                raise FileNotFoundError
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            self._log(
                f"⚠  ffmpeg not found at '{FFMPEG}'.  "
                "Install ffmpeg and make sure it is in your PATH."
            )
            return

        hw_available = []
        if sys.platform in ("win32", "linux") and check_qsv_available():
            hw_available.append("QSV")
        if sys.platform == "darwin" and check_vt_available():
            hw_available.append("VideoToolbox")

        if hw_available:
            self._log(f"✔  ffmpeg found: {FFMPEG}  |  {', '.join(hw_available)} available")
        else:
            if sys.platform in ("win32", "linux"):
                self._log(
                    "⚠  QSV does not appear to be available in this ffmpeg build.  "
                    "Install an ffmpeg build with --enable-libmfx / --enable-qsv "
                    "(e.g. from https://github.com/BtbN/FFmpeg-Builds).  "
                    "CPU mode will still work."
                )
            elif sys.platform == "darwin":
                self._log(
                    "⚠  VideoToolbox does not appear to be available in this ffmpeg build.  "
                    "Install ffmpeg via Homebrew: brew install ffmpeg.  "
                    "CPU mode will still work."
                )
            else:
                self._log(f"✔  ffmpeg found: {FFMPEG}  |  CPU mode only")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Input section
        input_group = QGroupBox("Input")
        input_layout = QVBoxLayout(input_group)

        row1 = QHBoxLayout()
        self.rb_file = QRadioButton("Single File")
        self.rb_folder = QRadioButton("Folder (Batch)")
        self.rb_file.setChecked(True)
        row1.addWidget(self.rb_file)
        row1.addWidget(self.rb_folder)
        row1.addStretch()
        input_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.le_input = QLineEdit()
        self.le_input.setPlaceholderText("Select a video file or folder…")
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse)
        row2.addWidget(self.le_input, 1)
        row2.addWidget(btn_browse)
        input_layout.addLayout(row2)
        layout.addWidget(input_group)

        # Settings section
        settings_group = QGroupBox("Encoding Settings")
        settings_layout = QHBoxLayout(settings_group)

        # HW mode
        settings_layout.addWidget(QLabel("HW Mode:"))
        self.cb_hw = QComboBox()
        for label, _key in HW_MODES:
            self.cb_hw.addItem(label)
        self.cb_hw.setCurrentIndex(0)  # QSV – HW Encode is the default
        self.cb_hw.setToolTip(
            "QSV – HW Encode:         Software decode, Intel QSV hardware encode\n"
            "QSV – Full HW Pipeline:  Intel QSV decode + crop + encode  (fastest on Intel)\n"
            "VideoToolbox – HW Encode:         Software decode, Apple VT hardware encode\n"
            "VideoToolbox – Full HW Pipeline:  Apple VT decode + crop + encode  (fastest on macOS)\n"
            "CPU – Software:          Fully software encode via libx264 / libx265"
        )
        self.cb_hw.currentIndexChanged.connect(self._on_hw_mode_changed)
        settings_layout.addWidget(self.cb_hw)

        settings_layout.addSpacing(16)

        # Quality
        settings_layout.addWidget(QLabel("Quality:"))
        self.sp_quality = QSpinBox()
        self.sp_quality.setRange(1, 51)
        self.sp_quality.setValue(QSV_QUALITY_DEFAULT)
        self.sp_quality.setToolTip(
            "QSV: global_quality  (1 = best quality / largest file,  51 = worst / smallest)\n"
            "VideoToolbox: quality mapped to 1.0–0.0 range\n"
            "CPU: CRF value  (same scale applies for libx264/libx265)"
        )
        self.sp_quality.setFixedWidth(60)
        settings_layout.addWidget(self.sp_quality)

        settings_layout.addSpacing(8)

        # Preset
        settings_layout.addWidget(QLabel("Preset:"))
        self.cb_preset = QComboBox()
        self.cb_preset.addItems(QSV_PRESETS)
        self.cb_preset.setCurrentText(QSV_PRESET_DEFAULT)
        self.cb_preset.setToolTip(
            "Encoding speed preset.  Slower = better compression."
        )
        settings_layout.addWidget(self.cb_preset)

        settings_layout.addSpacing(8)

        # Look-ahead (QSV only)
        self.chk_lookahead = QCheckBox("Look-ahead")
        self.chk_lookahead.setChecked(True)
        self.chk_lookahead.setToolTip(
            "Enable QSV look-ahead for better rate control quality.\n"
            "Disabled automatically in Full HW Pipeline mode."
        )
        settings_layout.addWidget(self.chk_lookahead)

        settings_layout.addSpacing(16)

        # Sample interval for cropdetect
        settings_layout.addWidget(QLabel("Sample Interval (s):"))
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(1, 120)
        self.sp_interval.setValue(15)
        self.sp_interval.setToolTip(
            "Seconds between frames sampled for black-bar detection."
        )
        settings_layout.addWidget(self.sp_interval)

        settings_layout.addSpacing(16)

        # Output suffix
        settings_layout.addWidget(QLabel("Suffix:"))
        self.le_suffix = QLineEdit("_nocrop")
        self.le_suffix.setMaximumWidth(100)
        settings_layout.addWidget(self.le_suffix)

        settings_layout.addSpacing(8)
        self.chk_overwrite = QCheckBox("Overwrite original")
        settings_layout.addWidget(self.chk_overwrite)

        settings_layout.addStretch()
        layout.addWidget(settings_group)

        # Splitter: table (top) + preview (bottom)
        self.splitter = QSplitter(Qt.Orientation.Vertical)

        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["File", "Resolution", "Codec", "Detected Crop", "New Resolution", "Status"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 250)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(4, 120)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.currentCellChanged.connect(self._on_table_selection_changed)
        table_layout.addWidget(self.table)
        self.splitter.addWidget(table_container)

        self.preview = PreviewPanel()
        self.preview.crop_changed.connect(self._on_crop_changed)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        layout.addWidget(self.splitter, 1)

        # Action buttons
        action_row = QHBoxLayout()
        self.btn_detect = QPushButton("Detect Black Bars")
        self.btn_detect.clicked.connect(self._start_detection)
        self.btn_preview = QPushButton("Preview")
        self.btn_preview.clicked.connect(self._preview_selected)
        self.btn_preview.setEnabled(False)
        self.btn_preview.setToolTip("Load preview for the selected file")
        self.btn_process = QPushButton("Process")
        self.btn_process.clicked.connect(self._start_processing)
        self.btn_process.setEnabled(False)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel_operation)
        self.btn_cancel.setEnabled(False)
        action_row.addWidget(self.btn_detect)
        action_row.addWidget(self.btn_preview)
        action_row.addWidget(self.btn_process)
        action_row.addWidget(self.btn_cancel)
        action_row.addStretch()
        layout.addLayout(action_row)

        # Progress bar
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # Log
        self.log_widget = QTextEdit()
        self.log_widget.setReadOnly(True)
        self.log_widget.setMaximumHeight(100)
        layout.addWidget(self.log_widget)

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    def _hw_mode_key(self) -> str:
        """Return the internal key for the currently selected HW mode."""
        return HW_MODES[self.cb_hw.currentIndex()][1]

    def _on_hw_mode_changed(self, _index: int):
        mode = self._hw_mode_key()
        # Look-ahead is only meaningful for QSV HW Encode mode
        self.chk_lookahead.setEnabled(mode == "qsv")
        if mode != "qsv":
            self.chk_lookahead.setChecked(False)
        # Presets don't apply to VideoToolbox
        self.cb_preset.setEnabled(mode not in ("vt", "vt_fullhw"))

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def _cancel_operation(self):
        """Cancel any running detection or encoding operation."""
        cancelled = False

        for worker in self.crop_workers:
            if worker.process.state() != QProcess.ProcessState.NotRunning:
                worker.process.kill()
                cancelled = True
        if cancelled:
            self.crop_workers.clear()
            self._active_detect_count = 0

        if self.encode_worker and self.encode_worker.process.state() != QProcess.ProcessState.NotRunning:
            self.encode_worker.process.kill()
            if self._encode_index < len(self.encode_queue):
                output = self.encode_queue[self._encode_index].get("_output", "")
                if output and os.path.exists(output):
                    try:
                        os.remove(output)
                    except OSError:
                        pass
            self.encode_worker = None
            self.encode_queue.clear()
            cancelled = True

        if cancelled:
            self._log("Operation cancelled.")
            self.btn_detect.setEnabled(True)
            self.btn_process.setEnabled(bool(self.files))
            self.btn_preview.setEnabled(bool(self.files))
            self.btn_cancel.setEnabled(False)
            self.progress.setVisible(False)

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self.log_widget.append(msg)

    def _browse(self):
        if self.rb_folder.isChecked():
            path = QFileDialog.getExistingDirectory(self, "Select Folder")
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Video", "", SUPPORTED_FORMATS
            )
        if path:
            self.le_input.setText(path)
            self._load_files(path)

    def _load_files(self, path: str):
        self.files.clear()
        self.table.setRowCount(0)
        self.preview.clear()

        p = Path(path)
        if p.is_file():
            paths = [p]
        elif p.is_dir():
            paths = sorted(
                f for f in p.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS
            )
        else:
            self._log(f"Invalid path: {path}")
            return

        for fp in paths:
            info = get_video_info(str(fp))
            if info:
                self.files.append(info)

        self.table.setRowCount(len(self.files))
        for i, info in enumerate(self.files):
            codec_label = info["video_codec"]
            if info.get("is_10bit"):
                codec_label += " (10-bit)"
            self.table.setItem(i, 0, QTableWidgetItem(Path(info["path"]).name))
            self.table.setItem(
                i, 1, QTableWidgetItem(f"{info['width']}×{info['height']}")
            )
            self.table.setItem(i, 2, QTableWidgetItem(codec_label))
            self.table.setItem(i, 3, QTableWidgetItem(""))
            self.table.setItem(i, 4, QTableWidgetItem(""))
            self.table.setItem(i, 5, QTableWidgetItem("Loaded"))

        self._log(f"Loaded {len(self.files)} video(s)")
        self.btn_process.setEnabled(False)
        self.btn_preview.setEnabled(False)

    # ------------------------------------------------------------------
    # Table / preview interaction
    # ------------------------------------------------------------------

    def _on_table_selection_changed(
        self, row: int, _col: int, _prev_row: int, _prev_col: int
    ):
        if 0 <= row < len(self.files):
            info = self.files[row]
            if info.get("crop"):
                self.preview.load_file(info)

    def _preview_selected(self):
        row = self.table.currentRow()
        if not (0 <= row < len(self.files)):
            self._log("Select a file in the table first.")
            return
        info = self.files[row]
        if not info.get("crop"):
            self._log(
                f"No crop data for {Path(info['path']).name} — run detection first."
            )
            return
        self.preview.load_file(info)

    def _on_crop_changed(self, filepath: str, new_crop: str):
        for i, info in enumerate(self.files):
            if info["path"] == filepath:
                cw, ch, *_ = parse_crop(new_crop)
                self.table.setItem(i, 3, QTableWidgetItem(new_crop))
                self.table.setItem(i, 4, QTableWidgetItem(f"{cw}×{ch}"))
                if cw == info["width"] and ch == info["height"]:
                    self.table.setItem(i, 5, QTableWidgetItem("No black bars"))
                else:
                    removed_h = info["height"] - ch
                    removed_w = info["width"] - cw
                    details = []
                    if removed_h > 0:
                        details.append(f"{removed_h}px horizontal bars")
                    if removed_w > 0:
                        details.append(f"{removed_w}px vertical bars")
                    suffix = " (manual)" if new_crop != info.get("crop_auto") else ""
                    self.table.setItem(
                        i, 5, QTableWidgetItem(f"Crop: {', '.join(details)}{suffix}")
                    )
                self._log(f"Crop updated: {Path(filepath).name} → {new_crop}")
                break

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _start_detection(self):
        if not self.files:
            self._log("No files loaded.")
            return
        self.btn_detect.setEnabled(False)
        self.btn_process.setEnabled(False)
        self.btn_preview.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.crop_workers.clear()
        self._detect_index = 0
        self._active_detect_count = 0
        self._log("Starting black-bar detection…")
        self.progress.setVisible(True)
        self.progress.setMaximum(len(self.files))
        self.progress.setValue(0)
        # Launch initial batch of workers (up to MAX_DETECT_WORKERS in parallel)
        for _ in range(min(MAX_DETECT_WORKERS, len(self.files))):
            self._launch_one_detect()

    def _launch_one_detect(self):
        """Start detection for the next queued file, if any."""
        if self._detect_index >= len(self.files):
            return
        idx = self._detect_index
        self._detect_index += 1
        self._active_detect_count += 1
        info = self.files[idx]
        self.table.setItem(idx, 5, QTableWidgetItem("Detecting…"))
        self._log(f"Detecting: {Path(info['path']).name}")
        worker = CropDetectWorker(
            info["path"],
            self.sp_interval.value(),
            lambda fp, crop, row=idx: self._on_crop_detected(fp, crop, row),
        )
        self.crop_workers.append(worker)
        worker.start()

    def _on_crop_detected(self, filepath: str, crop: str | None, row: int):
        self._active_detect_count -= 1
        info = self.files[row]

        if crop:
            info["crop"] = crop
            self.table.setItem(row, 3, QTableWidgetItem(crop))
            cw, ch, *_ = parse_crop(crop)
            self.table.setItem(row, 4, QTableWidgetItem(f"{cw}×{ch}"))

            if cw == info["width"] and ch == info["height"]:
                self.table.setItem(row, 5, QTableWidgetItem("No black bars"))
                self._log(f"  No black bars: {Path(filepath).name}")
            else:
                removed_h = info["height"] - ch
                removed_w = info["width"] - cw
                details = []
                if removed_h > 0:
                    details.append(f"{removed_h}px horizontal bars")
                if removed_w > 0:
                    details.append(f"{removed_w}px vertical bars")
                status = f"Crop: {', '.join(details)}"
                self.table.setItem(row, 5, QTableWidgetItem(status))
                self._log(f"  {Path(filepath).name}: {crop}  ({', '.join(details)})")
        else:
            info["crop"] = None
            self.table.setItem(row, 3, QTableWidgetItem("N/A"))
            self.table.setItem(row, 5, QTableWidgetItem("Detection failed"))
            self._log(f"  Detection failed: {Path(filepath).name}")

        self.progress.setValue(self.progress.value() + 1)
        # Start next queued file, then check if the whole batch is done
        self._launch_one_detect()
        if self._active_detect_count == 0:
            self._detection_complete()

    def _detection_complete(self):
        self._log("Detection complete.")
        self.btn_detect.setEnabled(True)
        self.btn_process.setEnabled(True)
        self.btn_preview.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress.setVisible(False)
        # Auto-preview the first file that has bars
        for i, info in enumerate(self.files):
            crop = info.get("crop")
            if crop:
                cw, ch, *_ = parse_crop(crop)
                if cw != info["width"] or ch != info["height"]:
                    self.table.selectRow(i)
                    self.preview.load_file(info)
                    break

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------

    def _start_processing(self):
        self.encode_queue.clear()
        for row, info in enumerate(self.files):
            crop = info.get("crop")
            if not crop:
                continue
            cw, ch, *_ = parse_crop(crop)
            if cw == info["width"] and ch == info["height"]:
                continue
            info["_row"] = row
            self.encode_queue.append(info)

        if not self.encode_queue:
            self._log("Nothing to process — no black bars detected.")
            return

        hw_mode = self._hw_mode_key()
        self._log(
            f"Processing {len(self.encode_queue)} file(s) "
            f"[mode={hw_mode}, quality={self.sp_quality.value()}, "
            f"preset={self.cb_preset.currentText()}, "
            f"look_ahead={self.chk_lookahead.isChecked() and hw_mode == 'qsv'}]"
        )

        self.btn_detect.setEnabled(False)
        self.btn_process.setEnabled(False)
        self.btn_preview.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setMaximum(100)
        self.progress.setValue(0)
        self._encode_index = 0
        self._encode_next()

    def _encode_next(self):
        if self._encode_index >= len(self.encode_queue):
            self._log("All files processed.")
            self.btn_detect.setEnabled(True)
            self.btn_process.setEnabled(True)
            self.btn_preview.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.progress.setVisible(False)
            return

        info = self.encode_queue[self._encode_index]
        filepath = info["path"]
        p = Path(filepath)

        if self.chk_overwrite.isChecked():
            output_path = str(p.with_stem(p.stem + "_tmp"))
            info["_overwrite"] = True
            info["_tmp_path"] = output_path
        else:
            suffix = self.le_suffix.text() or "_nocrop"
            output_path = str(p.with_stem(p.stem + suffix))
            info["_overwrite"] = False

        info["_output"] = output_path
        row = info["_row"]
        self.table.setItem(row, 5, QTableWidgetItem("Encoding…"))
        self._log(f"Encoding: {p.name}  →  {Path(output_path).name}")

        hw_mode = self._hw_mode_key()
        self.encode_worker = EncodeWorker(
            filepath=filepath,
            output_path=output_path,
            crop_filter=info["crop"],
            hw_mode=hw_mode,
            video_info=info,
            quality=self.sp_quality.value(),
            look_ahead=self.chk_lookahead.isChecked() and hw_mode == "qsv",
            preset=self.cb_preset.currentText(),
            on_progress=self._on_encode_progress,
            on_done=self._on_encode_done,
        )
        self.encode_worker.start()

    def _on_encode_progress(self, _filepath: str, pct: float):
        self.progress.setValue(int(pct))

    def _on_encode_done(self, filepath: str, success: bool):
        info = self.encode_queue[self._encode_index]
        row = self.files.index(info)
        p = Path(filepath)

        if success:
            if info.get("_overwrite"):
                try:
                    os.replace(info["_tmp_path"], filepath)
                    self._log(f"  Replaced original: {p.name}")
                except OSError as exc:
                    self._log(f"  Warning: could not replace original: {exc}")
            self.table.setItem(row, 5, QTableWidgetItem("Done ✔"))
            self._log(f"  Finished: {p.name}")
        else:
            self.table.setItem(row, 5, QTableWidgetItem("Error ✖"))
            self._log(
                f"  Error encoding: {p.name}  "
                "(check the log – HW encoder may not support this codec/format; "
                "try switching to CPU mode)"
            )
            output = info.get("_output", "")
            if output and os.path.exists(output):
                try:
                    os.remove(output)
                except OSError:
                    pass

        self._encode_index += 1
        self._encode_next()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        """Cancel running operations and clean up temp files on window close."""
        for worker in self.crop_workers:
            if worker.process.state() != QProcess.ProcessState.NotRunning:
                worker.process.kill()
                worker.process.waitForFinished(2000)
        if self.encode_worker and self.encode_worker.process.state() != QProcess.ProcessState.NotRunning:
            self.encode_worker.process.kill()
            self.encode_worker.process.waitForFinished(2000)
        self.preview._cleanup_tmp()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = BlackBarRemoveApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
