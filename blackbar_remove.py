#!/usr/bin/env python3
"""BlackBar Remove - Detect and remove black bars from videos.
Supports AMD AMF and Apple VideoToolbox hardware acceleration.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QPointF, QProcess, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Application icon, shipped alongside the script in assets/.
APP_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "appicon.png")

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

# AMD AMF hardware encoder map (Radeon on Windows; also Linux w/ amdgpu-pro AMF).
# av1_amf requires RDNA3 or newer (e.g. RX 7000 / RX 9000 series).
AMF_ENCODERS: dict[str, str] = {
    "h264": "h264_amf",
    "hevc": "hevc_amf",
    "h265": "hevc_amf",
    "av1": "av1_amf",
}

# Codecs the Windows d3d11va decoder can reliably offload for the AMF full-HW
# pipeline on modern AMD GPUs.  Kept conservative on purpose: RDNA4 (e.g. the
# RX 9070 series) dropped fixed-function decode for legacy codecs like MPEG-2,
# VC-1 and VP8, and forcing -hwaccel d3d11va on an unsupported codec makes
# FFmpeg error out rather than fall back.  Anything not listed here uses
# software decode + AMF hardware encode instead.
D3D11VA_DECODABLE: set[str] = {
    "h264",
    "hevc",
    "h265",
    "vp9",
    "av1",
}

# cropdetect black threshold as a fraction of the maximum pixel value (24/255).
# A fraction scales with bit depth; an absolute value like 24 would sit below
# limited-range black in 10-bit video (64) and detect no bars at all.
CROPDETECT_LIMIT = 24 / 255

# Maximum concurrent cropdetect workers when processing a batch
MAX_DETECT_WORKERS = 4

# Crop detection samples at least this many frames per clip; shorter clips get
# a tighter sample interval than the one set in the UI.
MIN_DETECT_SAMPLES = 10

# Quality range: 1 (best quality) – 51 (smallest file).  23 is a balanced default.
QUALITY_DEFAULT = 23
PRESETS = ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
PRESET_DEFAULT = "medium"

# Appended to output file names (movie.mkv -> movie_cropped.mkv)
DEFAULT_SUFFIX = "_cropped"

# Hardware-acceleration mode labels shown in the UI (filtered by platform)
_HW_MODES_ALL = [
    ("AMF – HW Encode (AMD)",          "amf",        ("win32", "linux")),
    ("AMF – Full HW Pipeline (AMD)",   "amf_fullhw", ("win32",)),
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


def run_hidden(*args, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run that never opens a console window.

    In a windowed Windows build (PyInstaller --windowed / pythonw) every
    console child process would otherwise flash a cmd window — noticeable
    for ffprobe on each file and ffmpeg on each preview frame.  QProcess
    already suppresses this on its own.
    """
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(*args, **kwargs)



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


def check_hw_available() -> set[str]:
    """Return the set of hardware accelerators available in this FFmpeg build."""
    found: set[str] = set()
    try:
        result = run_hidden(
            [FFMPEG, "-hide_banner", "-hwaccels"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = result.stdout.lower()
        if "videotoolbox" in out:
            found.add("videotoolbox")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return found

    # AMD AMF encoders are not listed by -hwaccels (that lists decode
    # accelerators), so probe the encoder list for h264_amf instead.
    try:
        result = run_hidden(
            [FFMPEG, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "h264_amf" in result.stdout.lower():
            found.add("amf")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return found


FFMPEG = find_ffmpeg_tool("ffmpeg")
FFPROBE = find_ffmpeg_tool("ffprobe")


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def parse_crop(crop_str: str) -> tuple[int, int, int, int]:
    """Parse 'crop=W:H:X:Y' → (W, H, X, Y). Raises ValueError on malformed input."""
    try:
        parts = crop_str.replace("crop=", "").split(":")
        return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"Invalid crop string: {crop_str!r}") from exc


def crop_to_vf_vt_fullhw(crop_filter: str, is_10bit: bool = False) -> str:
    """Convert 'crop=W:H:X:Y' to a filter chain for the VideoToolbox full HW pipeline.

    With -hwaccel videotoolbox -hwaccel_output_format videotoolbox, frames
    live on GPU surfaces.  We download to system memory for the CPU crop
    filter; the VideoToolbox encoder accepts CPU frames directly.
    """
    fmt = "p010le" if is_10bit else "nv12"
    return f"hwdownload,format={fmt},{crop_filter}"


def crop_to_vf_amf_fullhw(crop_filter: str, is_10bit: bool = False) -> str:
    """Convert 'crop=W:H:X:Y' to a filter chain for the AMD AMF full HW pipeline.

    FFmpeg's AMF encoders operate on system-memory frames, so after a d3d11va
    hardware decode (-hwaccel_output_format d3d11) we download the GPU surfaces
    to system memory, run the CPU crop filter, and hand the cropped frames
    straight to the AMF encoder (no hwupload needed).
    """
    fmt = "p010le" if is_10bit else "nv12"
    return f"hwdownload,format={fmt},{crop_filter}"


def amf_quality_from_preset(preset: str) -> str:
    """Map a libx264-style preset name to an AMF -quality value.

    AMF exposes speed/balanced/quality rather than the veryfast…veryslow scale,
    so the shared preset dropdown is translated onto that three-point axis.
    """
    if preset in ("veryfast", "faster", "fast"):
        return "speed"
    if preset in ("slow", "slower", "veryslow"):
        return "quality"
    return "balanced"


def get_video_info(filepath: str) -> dict | None:
    """Return video/audio stream metadata via ffprobe, or None on failure."""
    try:
        result = run_hidden(
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
            timeout=30,
        )
        data = json.loads(result.stdout)
        info: dict = {"path": filepath}

        video_n = 0  # index among video streams, for -c:v:N / -filter:v:N
        for stream in data.get("streams", []):
            if stream["codec_type"] == "video":
                # Cover art is exposed as a video stream with attached_pic set;
                # it is not the movie, so skip it when choosing the stream to crop.
                is_cover = stream.get("disposition", {}).get("attached_pic") == 1
                if "video_codec" in info or is_cover:
                    video_n += 1
                    continue
                info["video_index"] = video_n
                video_n += 1
                codec = stream["codec_name"].lower()
                info["video_codec"] = codec
                info["width"] = int(stream["width"])
                info["height"] = int(stream["height"])
                info["pix_fmt"] = stream.get("pix_fmt", "yuv420p")
                # Detect 10-bit content (hardware H.264 encoders are 8-bit only)
                info["is_10bit"] = "10" in stream.get("pix_fmt", "")
            elif stream["codec_type"] == "audio" and "audio_codec" not in info:
                info["audio_codec"] = stream["codec_name"]

        fmt = data.get("format", {})
        info["duration"] = float(fmt.get("duration", 0))
        return info if "video_codec" in info else None
    except (json.JSONDecodeError, FileNotFoundError, KeyError, ValueError, subprocess.TimeoutExpired):
        return None


def extract_frame(
    filepath: str, timestamp: float, crop_filter: str | None = None
) -> str | None:
    """Extract a single frame as PNG to a temp file.  Returns the path or None."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()

    vf = crop_filter if crop_filter else "null"
    try:
        result = run_hidden(
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
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        os.unlink(tmp.name)
        return None
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


def effective_sample_interval(interval: float, duration: float) -> float:
    """Sample interval for cropdetect, tightened so that a clip yields at least
    MIN_DETECT_SAMPLES frames (a clip shorter than the interval would otherwise
    yield none and detection would fail)."""
    if duration > 0 and duration < interval * MIN_DETECT_SAMPLES:
        return max(0.1, round(duration / MIN_DETECT_SAMPLES, 2))
    return interval


def format_timestamp(seconds: float) -> str:
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    return f"{h}:{m:02d}:{s:02d}"


def _snap(v: int) -> int:
    return v - (v % 2)


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, _snap(v)))


# ---------------------------------------------------------------------------
# Async workers (QProcess-based so the UI never blocks)
# ---------------------------------------------------------------------------


class CropDetectWorker:
    """Runs ffmpeg cropdetect asynchronously via QProcess."""

    def __init__(self, filepath: str, sample_interval: float, on_done):
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
                f"fps=1/{self.sample_interval},"
                f"cropdetect=limit={CROPDETECT_LIMIT:.4f}:round=16:reset=0",
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

    Supports these hardware modes:
      amf         – software decode, AMD AMF hardware encode  (Windows/Linux)
      amf_fullhw  – d3d11va hardware decode + crop + AMF encode  (Windows, AMD)
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
        preset: str,
        on_progress,
        on_done,
    ):
        self.filepath = filepath
        self.output_path = output_path
        self.duration = video_info.get("duration", 0)
        self.on_progress = on_progress
        self.on_done = on_done
        self._partial = ""                     # incomplete trailing line from last read
        self._log_tail: deque[str] = deque(maxlen=20)  # recent non-progress output
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

        if hw_mode in ("amf", "amf_fullhw"):
            encoder = AMF_ENCODERS.get(video_codec, "h264_amf")
            # AMF uses constant-QP rate control (-rc cqp) as the closest
            # analogue to CRF.  The 1–51 quality scale maps directly onto the
            # H.264/HEVC QP range; AV1 AMF uses a wider 0–255 QP range, so
            # rescale for that encoder.
            if encoder == "av1_amf":
                qp = max(0, min(255, round(quality / 51 * 255)))
            else:
                qp = max(0, min(51, quality))
            enc_args = [
                "-rc",
                "cqp",
                "-qp_i",
                str(qp),
                "-qp_p",
                str(qp),
                "-quality",
                amf_quality_from_preset(preset),
            ]
            # -qp_b applies to H.264/HEVC (B-frames); av1_amf rejects it.
            if encoder != "av1_amf":
                enc_args += ["-qp_b", str(qp)]

            if hw_mode == "amf_fullhw" and video_codec in D3D11VA_DECODABLE:
                # AMF has no decoder of its own; pair it with the Windows
                # d3d11va decoder, crop on CPU frames, then AMF encodes them.
                pre_input_args = [
                    "-hwaccel",
                    "d3d11va",
                    "-hwaccel_output_format",
                    "d3d11",
                ]
                vf_filter = crop_to_vf_amf_fullhw(crop_filter, is_10bit)
            else:
                # Software decode + AMF hardware encode.
                vf_filter = crop_filter

        elif hw_mode in ("vt", "vt_fullhw"):
            encoder = VT_ENCODERS.get(video_codec, "h264_videotoolbox")
            # Map quality 1–51 (lower=better) to VT q:v 100–1 (higher=better).
            # FFmpeg divides q:v by 100 before handing it to VideoToolbox, so
            # the scale is 1–100, not 0.0–1.0.  Constant-quality mode is only
            # available on Apple Silicon.
            vt_q = round(100 - (quality - 1) * 99 / 50)
            enc_args = ["-q:v", str(vt_q), "-allow_sw", "1"]

            if hw_mode == "vt_fullhw":
                pre_input_args = [
                    "-hwaccel",
                    "videotoolbox",
                    "-hwaccel_output_format",
                    "videotoolbox",
                ]
                vf_filter = crop_to_vf_vt_fullhw(crop_filter, is_10bit)

        else:  # cpu
            encoder = SW_ENCODERS.get(video_codec, "libx264")
            enc_args = ["-crf", str(quality), "-preset", preset]

        # Keep every stream (-map 0) but only crop/re-encode the main video
        # stream.  Everything else — audio, subtitles, cover art, data and
        # attachments — is stream-copied.  A plain -vf would apply the crop
        # to every video stream and fail on e.g. a 600×600 cover image.
        v = video_info.get("video_index", 0)
        self.args = [
            "-hide_banner",
            "-nostats",
            # Only warnings/errors reach stderr, so the captured tail shown on
            # failure is the actual cause rather than stream metadata.
            "-loglevel",
            "warning",
            *pre_input_args,
            "-i",
            filepath,
            "-map",
            "0",
            "-c",
            "copy",
            f"-c:v:{v}",
            encoder,
            f"-filter:v:{v}",
            vf_filter,
            *enc_args,
            "-progress",
            "pipe:1",
            "-y",
            output_path,
        ]

    def start(self):
        self.process.start(FFMPEG, self.args)

    # "-progress pipe:1" emits key=value lines; anything else is ffmpeg's
    # stderr (merged channel), which is kept for error reporting.
    _PROGRESS_LINE = re.compile(r"^[a-z0-9_]+=\S*$")

    def _read(self):
        data = (
            self.process.readAllStandardOutput()
            .data()
            .decode("utf-8", errors="replace")
        )
        lines = (self._partial + data).splitlines(keepends=True)
        self._partial = lines.pop() if lines and not lines[-1].endswith(("\n", "\r")) else ""
        for line in lines:
            self._handle_line(line.strip())

    def _handle_line(self, line: str):
        if not line:
            return
        # out_time_ms is also in microseconds (a long-standing ffmpeg misnomer);
        # older builds only emit that one.
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                us = int(line.split("=")[1])
                if self.duration > 0:
                    pct = min(100.0, (us / 1_000_000) / self.duration * 100)
                    self.on_progress(self.filepath, pct)
            except ValueError:
                pass
        elif not self._PROGRESS_LINE.match(line) and not line.startswith("Multiple -c"):
            # (the "Multiple -c" warning is expected: -c copy is overridden by
            # -c:v:N for the stream we re-encode)
            self._log_tail.append(line)

    def _finished(self):
        self._handle_line(self._partial.strip())
        self._partial = ""
        self.on_done(self.filepath, self.process.exitCode() == 0, list(self._log_tail))


# ---------------------------------------------------------------------------
# Zoomable image views
# ---------------------------------------------------------------------------

# Discrete zoom steps used by the +/- buttons, Ctrl/⌘+wheel and shortcuts.
ZOOM_LEVELS = [
    0.1, 0.125, 0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0
]
ZOOM_MIN, ZOOM_MAX = ZOOM_LEVELS[0], ZOOM_LEVELS[-1]


class ZoomImageView(QWidget):
    """Paints a region of a frame at an arbitrary zoom factor.

    The widget is sized to the scaled region but only the exposed part is
    painted, so high zoom levels cost no extra memory.  Downscaled views are
    drawn from a cached, smoothly pre-scaled copy (plain bilinear shrinking
    aliases badly); from 200 % up pixels are drawn unsmoothed so the edges of
    the black bars can be inspected pixel by pixel.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src: QPixmap | None = None
        self._region = QRect()  # source-pixel rectangle that is shown
        self._scale = 1.0
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None
        self.setFixedSize(0, 0)

    def has_image(self) -> bool:
        return self._src is not None and not self._src.isNull()

    def region(self) -> QRect:
        return QRect(self._region)

    def scale(self) -> float:
        return self._scale

    def set_image(self, pixmap: QPixmap | None, region: QRect | None = None):
        self._src = pixmap
        if region is not None:
            self._region = QRect(region)
        else:
            self._region = pixmap.rect() if pixmap is not None else QRect()
        self._update_size()

    def set_region(self, region: QRect):
        self._region = QRect(region)
        self._update_size()

    def set_scale(self, scale: float):
        self._scale = scale
        self._update_size()

    def _update_size(self):
        if self.has_image() and not self._region.isEmpty():
            self.setFixedSize(
                max(1, round(self._region.width() * self._scale)),
                max(1, round(self._region.height() * self._scale)),
            )
        else:
            self.setFixedSize(0, 0)
        self.update()

    def paintEvent(self, event):
        if not self.has_image() or self._region.isEmpty():
            return
        p = QPainter(self)
        s, r = self._scale, self._region
        dpr = self.devicePixelRatioF()  # 2.0 on Retina / HiDPI screens
        if s * dpr < 1.0:
            # Shrinking: draw from a cache scaled to *device* pixels so the
            # view stays sharp on HiDPI screens.
            key = (self._src.cacheKey(), s, dpr)
            if self._cache_key != key:
                self._cache = self._src.scaled(
                    max(1, round(self._src.width() * s * dpr)),
                    max(1, round(self._src.height() * s * dpr)),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._cache.setDevicePixelRatio(dpr)
                self._cache_key = key
            k = s * dpr
            p.drawPixmap(
                QRectF(0, 0, self.width(), self.height()),
                self._cache,
                QRectF(r.x() * k, r.y() * k, self.width() * dpr, self.height() * dpr),
            )
        else:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, s < 2.0)
            ex = QRectF(event.rect())
            src = QRectF(
                r.x() + ex.x() / s, r.y() + ex.y() / s, ex.width() / s, ex.height() / s
            )
            p.drawPixmap(ex, self._src, src)
        self.paint_overlay(p)
        p.end()

    def paint_overlay(self, p: QPainter):
        """Hook for subclasses to draw on top of the frame."""


class CropCanvas(ZoomImageView):
    """Frame view with a draggable crop rectangle.

    The user can drag the crop rectangle or its eight edge/corner handles to
    adjust the crop.  Presses outside the rectangle are ignored so the
    surrounding ZoomScrollArea can pan instead.
    """

    # emitted continuously while dragging (cw, ch, cx, cy in original pixels)
    crop_changed = pyqtSignal(int, int, int, int)
    # emitted once on mouse-release so callers can commit the change
    crop_committed = pyqtSignal(int, int, int, int)

    _HANDLE = 8   # size of each handle square in display pixels
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
        self.setMouseTracking(True)
        self._crop: tuple[int, int, int, int] | None = None  # (cw, ch, cx, cy)
        self._drag_mode: str | None = None
        self._drag_anchor: QPoint | None = None
        self._drag_crop0: tuple | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_frame(self, pixmap: QPixmap, crop: tuple[int, int, int, int]):
        self._crop = crop
        self.set_image(pixmap)

    def set_crop(self, crop: tuple[int, int, int, int]):
        self._crop = crop
        self.update()

    def clear_frame(self):
        self._crop = None
        self.set_image(None)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _crop_display_rect(self) -> QRect:
        cw, ch, cx, cy = self._crop
        s = self._scale
        return QRect(round(cx * s), round(cy * s), round(cw * s), round(ch * s))

    def paint_overlay(self, p: QPainter):
        if not self._crop:
            return
        r = self._crop_display_rect()
        w, h = self.width(), self.height()

        # Darken the four bands outside the crop area
        shade = QColor(0, 0, 0, 150)
        p.fillRect(0, 0, w, r.top(), shade)
        p.fillRect(0, r.top() + r.height(), w, h - r.top() - r.height(), shade)
        p.fillRect(0, r.top(), r.left(), r.height(), shade)
        p.fillRect(r.left() + r.width(), r.top(), w - r.left() - r.width(), r.height(), shade)

        # Red border
        p.setPen(QPen(Qt.GlobalColor.red, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r.adjusted(1, 1, -1, -1))

        # White handles at corners and edge midpoints
        hs = self._HANDLE
        p.setPen(QPen(QColor(255, 255, 255, 220), 1))
        p.setBrush(QColor(255, 255, 255, 210))
        dx, dy, dw, dh = r.x(), r.y(), r.width(), r.height()
        for hx, hy in (
            (dx, dy), (dx + dw, dy), (dx, dy + dh), (dx + dw, dy + dh),
            (dx + dw // 2, dy), (dx + dw // 2, dy + dh),
            (dx, dy + dh // 2), (dx + dw, dy + dh // 2),
        ):
            p.drawRect(hx - hs // 2, hy - hs // 2, hs, hs)

    # ------------------------------------------------------------------
    # Hit-testing
    # ------------------------------------------------------------------

    def _hit(self, pos: QPoint) -> str | None:
        if not self._crop:
            return None
        r = self._crop_display_rect()
        dx, dy, dw, dh = r.x(), r.y(), r.width(), r.height()
        h = self._HANDLE + 2
        mx, my = dx + dw // 2, dy + dh // 2
        zones = {
            "nw": (dx, dy), "ne": (dx + dw, dy), "sw": (dx, dy + dh), "se": (dx + dw, dy + dh),
            "n": (mx, dy), "s": (mx, dy + dh), "w": (dx, my), "e": (dx + dw, my),
        }
        for name, (zx, zy) in zones.items():
            if QRect(zx - h, zy - h, 2 * h, 2 * h).contains(pos):
                return name
        return "move" if r.contains(pos) else None

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        mode = self._hit(event.pos()) if event.button() == Qt.MouseButton.LeftButton else None
        if not mode:
            event.ignore()  # let the scroll area pan
            return
        self._drag_mode = mode
        self._drag_anchor = event.pos()
        self._drag_crop0 = self._crop

    def mouseMoveEvent(self, event):
        if not self._drag_mode:
            if not event.buttons():
                hit = self._hit(event.pos())
                if hit:
                    self.setCursor(self._CURSOR[hit])
                else:
                    self.unsetCursor()  # fall back to the scroll area's pan cursor
            event.ignore()
            return

        s = self._scale
        if s <= 0 or not self.has_image():
            return
        delta = event.pos() - self._drag_anchor
        dx = round(delta.x() / s)
        dy = round(delta.y() / s)
        cw, ch, cx, cy = self._drag_crop0
        W, H = self._src.width(), self._src.height()

        m = self._drag_mode
        if m == "move":
            cx = _clamp(cx + dx, 0, W - cw)
            cy = _clamp(cy + dy, 0, H - ch)
        elif m == "e":
            cw = _clamp(cw + dx, 2, W - cx)
        elif m == "s":
            ch = _clamp(ch + dy, 2, H - cy)
        elif m == "w":
            ncx = _clamp(cx + dx, 0, cx + cw - 2)
            cw = max(2, _snap(cw + cx - ncx)); cx = ncx
        elif m == "n":
            ncy = _clamp(cy + dy, 0, cy + ch - 2)
            ch = max(2, _snap(ch + cy - ncy)); cy = ncy
        elif m == "se":
            cw = _clamp(cw + dx, 2, W - cx); ch = _clamp(ch + dy, 2, H - cy)
        elif m == "sw":
            ncx = _clamp(cx + dx, 0, cx + cw - 2); cw = max(2, _snap(cw + cx - ncx)); cx = ncx
            ch = _clamp(ch + dy, 2, H - cy)
        elif m == "ne":
            cw = _clamp(cw + dx, 2, W - cx)
            ncy = _clamp(cy + dy, 0, cy + ch - 2); ch = max(2, _snap(ch + cy - ncy)); cy = ncy
        elif m == "nw":
            ncx = _clamp(cx + dx, 0, cx + cw - 2); cw = max(2, _snap(cw + cx - ncx)); cx = ncx
            ncy = _clamp(cy + dy, 0, cy + ch - 2); ch = max(2, _snap(ch + cy - ncy)); cy = ncy

        self._crop = (cw, ch, cx, cy)
        self.update()
        self.crop_changed.emit(cw, ch, cx, cy)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_mode:
            if self._crop:
                self.crop_committed.emit(*self._crop)
            self._drag_mode = self._drag_anchor = self._drag_crop0 = None
        else:
            event.ignore()


class ZoomScrollArea(QScrollArea):
    """Scroll area around a ZoomImageView.

    Ctrl/⌘ + wheel and trackpad pinch request zoom changes (the PreviewPanel
    owns the zoom level so both panes stay in step); left- or middle-drag on
    the image pans when it is larger than the viewport.
    """

    zoom_step = pyqtSignal(int, QPoint)       # (+1 / -1, anchor in viewport coords)
    zoom_factor = pyqtSignal(float, QPoint)   # multiplicative change (pinch)
    viewport_resized = pyqtSignal()

    def __init__(self, view: ZoomImageView, parent=None):
        super().__init__(parent)
        self.setWidget(view)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        vp = self.viewport()
        pal = vp.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(17, 17, 17))
        vp.setPalette(pal)
        vp.setAutoFillBackground(True)
        self._pan_origin: tuple[QPoint, int, int] | None = None
        vp.installEventFilter(self)
        view.installEventFilter(self)
        self.horizontalScrollBar().rangeChanged.connect(self.update_cursor)
        self.verticalScrollBar().rangeChanged.connect(self.update_cursor)

    def is_pannable(self) -> bool:
        return self.horizontalScrollBar().maximum() > 0 or self.verticalScrollBar().maximum() > 0

    def update_cursor(self, *_):
        if self._pan_origin is None:
            self.viewport().setCursor(
                Qt.CursorShape.OpenHandCursor if self.is_pannable() else Qt.CursorShape.ArrowCursor
            )

    def _viewport_pos(self, obj, pos: QPointF) -> QPoint:
        pt = pos.toPoint()
        return pt if obj is self.viewport() else obj.mapTo(self.viewport(), pt)

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.Wheel and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            dy = event.angleDelta().y()
            if dy:
                self.zoom_step.emit(1 if dy > 0 else -1, self._viewport_pos(obj, event.position()))
            return True
        if (
            t == QEvent.Type.NativeGesture
            and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture
        ):
            self.zoom_factor.emit(1.0 + event.value(), self._viewport_pos(obj, event.position()))
            return True
        if obj is self.viewport():
            if (
                t == QEvent.Type.MouseButtonPress
                and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton)
                and self.is_pannable()
            ):
                self._pan_origin = (
                    event.globalPosition().toPoint(),
                    self.horizontalScrollBar().value(),
                    self.verticalScrollBar().value(),
                )
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
            if t == QEvent.Type.MouseMove and self._pan_origin is not None:
                origin, h0, v0 = self._pan_origin
                d = event.globalPosition().toPoint() - origin
                self.horizontalScrollBar().setValue(h0 - d.x())
                self.verticalScrollBar().setValue(v0 - d.y())
                return True
            if t == QEvent.Type.MouseButtonRelease and self._pan_origin is not None:
                self._pan_origin = None
                self.update_cursor()
                return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.viewport_resized.emit()


# ---------------------------------------------------------------------------
# Preview panel
# ---------------------------------------------------------------------------


class PreviewPanel(QWidget):
    """Before/after preview with time scrubber, zoom, and crop editing.

    Only the original frame is extracted with ffmpeg; the "after" pane shows
    the crop region of that same frame (the crop filter is pixel-exact), so it
    updates instantly while the crop is edited.
    """

    crop_changed = pyqtSignal(str, str)  # (filepath, new_crop_string)
    # Internal: emitted from the frame-extraction thread; Qt queues delivery
    # onto the GUI thread.  (QTimer.singleShot from a plain Python thread is
    # never delivered, because that thread has no Qt event loop.)
    _frame_ready = pyqtSignal(int, object)  # (gen, frame_path or None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_info: dict | None = None
        self._orig_pixmap: QPixmap | None = None
        self._suppress_spinbox_signals = False
        self._frame_load_gen = 0
        self._fit = True     # zoom mode: fit to pane, or fixed self._zoom
        self._zoom = 1.0
        self._syncing_scroll = False
        self._frame_ready.connect(self._on_frame_loaded)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

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
        self.slider.setToolTip("Scrub to preview a different frame")
        self.lbl_duration = QLabel("0:00:00")
        self.lbl_duration.setFixedWidth(60)
        self.lbl_duration.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.lbl_loading = QLabel("")
        self.lbl_loading.setFixedWidth(110)
        self.lbl_loading.setStyleSheet("color: #888;")
        scrubber_row.addWidget(self.lbl_time)
        scrubber_row.addWidget(self.slider, 1)
        scrubber_row.addWidget(self.lbl_duration)
        scrubber_row.addWidget(self.lbl_loading)
        layout.addLayout(scrubber_row)

        self._scrub_timer = QTimer(singleShot=True, interval=250)
        self._scrub_timer.timeout.connect(self._update_frames)
        self.slider.valueChanged.connect(self._on_slider_moved)

        # Crop controls + zoom controls in one toolbar row
        tools = QHBoxLayout()
        tools.addWidget(QLabel("Aspect:"))
        self.cb_aspect = QComboBox()
        for name, _ in ASPECT_RATIOS:
            self.cb_aspect.addItem(name)
        self.cb_aspect.setEnabled(False)
        self.cb_aspect.currentIndexChanged.connect(self._on_aspect_ratio_changed)
        tools.addWidget(self.cb_aspect)
        tools.addSpacing(8)

        for label, attr in (("W", "sp_w"), ("H", "sp_h"), ("X", "sp_x"), ("Y", "sp_y")):
            tools.addWidget(QLabel(label))
            sb = QSpinBox()
            sb.setRange(0 if attr in ("sp_x", "sp_y") else 2, 9999)
            sb.setSingleStep(2)
            sb.setFixedWidth(72)
            sb.setKeyboardTracking(False)
            sb.setEnabled(False)
            sb.valueChanged.connect(self._on_spinbox_changed)
            setattr(self, attr, sb)
            tools.addWidget(sb)

        self.btn_reset = QPushButton("Reset")
        self.btn_reset.setToolTip("Reset to the auto-detected crop")
        self.btn_reset.clicked.connect(self._reset_crop)
        self.btn_reset.setEnabled(False)
        tools.addWidget(self.btn_reset)

        self.lbl_ar_info = QLabel("")
        self.lbl_ar_info.setStyleSheet("color: #888;")
        tools.addWidget(self.lbl_ar_info)
        tools.addStretch()

        # Zoom controls
        zoom_tip = (
            "Zoom: Ctrl/⌘ + mouse wheel, trackpad pinch, Ctrl/⌘ +/−\n"
            "Ctrl/⌘ 0 = fit, Ctrl/⌘ 1 = 100 %.  Drag the image to pan."
        )
        self.btn_zoom_out = QToolButton(text="−", toolTip="Zoom out (Ctrl/⌘ −)")
        self.btn_zoom_out.clicked.connect(lambda: self._zoom_by_step(-1))
        self.lbl_zoom = QLabel("Fit")
        self.lbl_zoom.setFixedWidth(46)
        self.lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_zoom.setToolTip(zoom_tip)
        self.btn_zoom_in = QToolButton(text="+", toolTip="Zoom in (Ctrl/⌘ +)")
        self.btn_zoom_in.clicked.connect(lambda: self._zoom_by_step(1))
        self.btn_fit = QToolButton(text="Fit", toolTip="Fit frame to pane (Ctrl/⌘ 0)")
        self.btn_fit.setCheckable(True)
        self.btn_fit.setChecked(True)
        self.btn_fit.clicked.connect(self._zoom_fit)
        self.btn_actual = QToolButton(text="1:1", toolTip="Actual pixels, 100 % (Ctrl/⌘ 1)")
        self.btn_actual.clicked.connect(lambda: self._set_zoom(1.0))
        self.btn_layout = QToolButton(text="⇅", toolTip="Toggle side-by-side / stacked panes")
        self.btn_layout.clicked.connect(self._toggle_layout)
        tools.addWidget(QLabel("Zoom:"))
        for w in (self.btn_zoom_out, self.lbl_zoom, self.btn_zoom_in,
                  self.btn_fit, self.btn_actual, self.btn_layout):
            tools.addWidget(w)
        layout.addLayout(tools)

        for seq, slot in (
            (QKeySequence.StandardKey.ZoomIn, lambda: self._zoom_by_step(1)),
            (QKeySequence("Ctrl+="), lambda: self._zoom_by_step(1)),
            (QKeySequence.StandardKey.ZoomOut, lambda: self._zoom_by_step(-1)),
            (QKeySequence("Ctrl+0"), self._zoom_fit),
            (QKeySequence("Ctrl+1"), lambda: self._set_zoom(1.0)),
        ):
            QShortcut(seq, self, activated=slot)

        # Image panes in a splitter (side by side or stacked)
        self.view_orig = CropCanvas()
        self.view_orig.crop_changed.connect(self._on_canvas_crop_changed)
        self.view_orig.crop_committed.connect(self._on_canvas_crop_committed)
        self.view_crop = ZoomImageView()

        self.images = QSplitter(Qt.Orientation.Horizontal)
        self.images.setChildrenCollapsible(False)
        for header_text, view, area_attr, size_attr in (
            ("Original — drag the red box or its handles to adjust the crop",
             self.view_orig, "area_orig", "lbl_orig_size"),
            ("After crop", self.view_crop, "area_crop", "lbl_crop_size"),
        ):
            pane = QWidget()
            col = QVBoxLayout(pane)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(2)
            hdr = QLabel(header_text)
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hdr.setStyleSheet("font-weight: bold;")
            col.addWidget(hdr)

            area = ZoomScrollArea(view)
            area.setToolTip(zoom_tip)
            area.zoom_step.connect(lambda steps, pos, a=area: self._zoom_by_step(steps, a, pos))
            area.zoom_factor.connect(lambda f, pos, a=area: self._zoom_by_factor(f, a, pos))
            area.viewport_resized.connect(self._on_viewport_resized)
            area.horizontalScrollBar().valueChanged.connect(
                lambda _v, a=area: self._sync_scroll(a))
            area.verticalScrollBar().valueChanged.connect(
                lambda _v, a=area: self._sync_scroll(a))
            setattr(self, area_attr, area)
            col.addWidget(area, 1)

            sz_lbl = QLabel("")
            sz_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sz_lbl.setStyleSheet("color: #888;")
            setattr(self, size_attr, sz_lbl)
            col.addWidget(sz_lbl)
            self.images.addWidget(pane)

        self.images.setStretchFactor(0, 1)
        self.images.setStretchFactor(1, 1)
        layout.addWidget(self.images, 1)
        self._panes_sized = False

        self._commit_timer = QTimer(singleShot=True, interval=500)
        self._commit_timer.timeout.connect(self._commit_crop)

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    def _fit_scale(self) -> float:
        if not self._orig_pixmap or self._orig_pixmap.isNull():
            return 1.0
        vp = self.area_orig.viewport().size()
        if vp.width() <= 0 or vp.height() <= 0:
            return 1.0
        return max(ZOOM_MIN, min(
            (vp.width() - 2) / self._orig_pixmap.width(),
            (vp.height() - 2) / self._orig_pixmap.height(),
        ))

    def _current_scale(self) -> float:
        return self._fit_scale() if self._fit else self._zoom

    def _zoom_by_step(self, steps: int, area=None, pos=None):
        cur = self._current_scale()
        if steps > 0:
            new = next((z for z in ZOOM_LEVELS if z > cur * 1.001), ZOOM_MAX)
        else:
            new = next((z for z in reversed(ZOOM_LEVELS) if z < cur / 1.001), ZOOM_MIN)
        self._set_zoom(new, area, pos)

    def _zoom_by_factor(self, factor: float, area=None, pos=None):
        self._set_zoom(max(ZOOM_MIN, min(ZOOM_MAX, self._current_scale() * factor)), area, pos)

    def _set_zoom(self, zoom: float, area=None, pos=None):
        self._fit = False
        self._zoom = zoom
        self._apply_zoom(area, pos)

    def _zoom_fit(self):
        self._fit = True
        self._apply_zoom()

    def _on_viewport_resized(self):
        if self._fit:
            self._apply_zoom()

    def _toggle_layout(self):
        horizontal = self.images.orientation() == Qt.Orientation.Horizontal
        self.images.setOrientation(
            Qt.Orientation.Vertical if horizontal else Qt.Orientation.Horizontal
        )
        self.btn_layout.setText("⇆" if horizontal else "⇅")
        self._equalize_panes()

    def _equalize_panes(self):
        # Equal values are distributed proportionally → a 50/50 split.
        self.images.setSizes([1_000_000, 1_000_000])

    def showEvent(self, event):
        super().showEvent(event)
        if not self._panes_sized:
            self._panes_sized = True
            self._equalize_panes()

    def _apply_zoom(self, area=None, pos=None):
        """Apply the current zoom to both panes, keeping the source point under
        *pos* (viewport coords of *area*; default: centre of the original pane)
        where it is."""
        area = area or self.area_orig
        view = area.widget()
        vp = area.viewport()
        if pos is None:
            pos = QPoint(vp.width() // 2, vp.height() // 2)
        new_s = self._current_scale()

        anchor = None
        if view.has_image() and view.scale() > 0:
            wp = view.mapFrom(vp, pos)
            reg = view.region()
            anchor = (wp.x() / view.scale() + reg.x(), wp.y() / view.scale() + reg.y())

        self._syncing_scroll = True
        for v in (self.view_orig, self.view_crop):
            v.set_scale(new_s)
        self._syncing_scroll = False

        if anchor and not self._fit:
            reg = view.region()
            area.horizontalScrollBar().setValue(round((anchor[0] - reg.x()) * new_s - pos.x()))
            area.verticalScrollBar().setValue(round((anchor[1] - reg.y()) * new_s - pos.y()))
        self._sync_scroll(area)

        self.lbl_zoom.setText(f"{round(new_s * 100)}%")
        self.btn_fit.setChecked(self._fit)
        self.area_orig.update_cursor()
        self.area_crop.update_cursor()

    def _sync_scroll(self, source):
        """Scroll the other pane so both show the same part of the frame."""
        if self._syncing_scroll or not self._current_info or not self._current_info.get("crop"):
            return
        _, _, cx, cy = parse_crop(self._current_info["crop"])
        s = self.view_orig.scale()
        other = self.area_crop if source is self.area_orig else self.area_orig
        sign = -1 if source is self.area_orig else 1
        self._syncing_scroll = True
        other.horizontalScrollBar().setValue(
            source.horizontalScrollBar().value() + sign * round(cx * s))
        other.verticalScrollBar().setValue(
            source.verticalScrollBar().value() + sign * round(cy * s))
        self._syncing_scroll = False

    # ------------------------------------------------------------------
    # Crop editing
    # ------------------------------------------------------------------

    def _crop_from_spinboxes(self) -> tuple[int, int, int, int]:
        """Current spinbox values, snapped to even numbers and kept in frame."""
        cx, cy = _snap(self.sp_x.value()), _snap(self.sp_y.value())
        cw, ch = _snap(self.sp_w.value()), _snap(self.sp_h.value())
        if self._current_info:
            w, h = self._current_info["width"], self._current_info["height"]
            cx, cy = min(cx, _snap(w - 2)), min(cy, _snap(h - 2))
            cw, ch = max(2, min(cw, _snap(w - cx))), max(2, min(ch, _snap(h - cy)))
        return cw, ch, cx, cy

    def _set_spinboxes(self, crop: tuple[int, int, int, int]):
        cw, ch, cx, cy = crop
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

    def _set_aspect_index(self, index: int, info_text: str):
        self._suppress_spinbox_signals = True
        self.cb_aspect.setCurrentIndex(index)
        self._suppress_spinbox_signals = False
        self.lbl_ar_info.setText(info_text)

    def _show_crop(self, crop: tuple[int, int, int, int]):
        """Make *crop* the file's crop and update both panes immediately.

        The change is reported to the main window (table + log) via
        _commit_crop, debounced for spinbox edits.
        """
        info = self._current_info
        if not info:
            return
        cw, ch, cx, cy = crop
        crop_str = f"crop={cw}:{ch}:{cx}:{cy}"
        info["crop"] = crop_str
        self.view_orig.set_crop(crop)
        if self._orig_pixmap:
            self.view_crop.set_region(QRect(cx, cy, cw, ch))
            self._sync_scroll(self.area_orig)
        self.lbl_crop_size.setText(f"{cw}×{ch}")
        self._update_info_label(crop_str)

    def _commit_crop(self):
        self._commit_timer.stop()
        info = self._current_info
        if info and info.get("crop"):
            self.crop_changed.emit(info["path"], info["crop"])

    def _on_aspect_ratio_changed(self, index: int):
        if self._suppress_spinbox_signals or not self._current_info:
            return
        info = self._current_info
        name, ratio = ASPECT_RATIOS[index]

        if name == "Custom":
            self.lbl_ar_info.setText("Free edit")
            return
        if name == "From detection":
            auto_crop = info.get("crop_auto")
            if not auto_crop:
                return
            crop = parse_crop(auto_crop)
        elif ratio is None:
            return
        else:
            crop = calc_crop_for_aspect(info["width"], info["height"], *ratio)
        cw, ch, *_ = crop
        prefix = "Auto-detected: " if name == "From detection" else ""
        self.lbl_ar_info.setText(f"{prefix}{cw}×{ch} ({cw / ch:.3f}:1)")
        self._set_spinboxes(crop)
        self._show_crop(crop)
        self._commit_crop()

    def _on_spinbox_changed(self, _value: int):
        if self._suppress_spinbox_signals or not self._current_info:
            return
        crop = self._crop_from_spinboxes()
        self._set_spinboxes(crop)  # reflect snapping/clamping
        if self.cb_aspect.currentIndex() != 0:
            self._set_aspect_index(0, "Free edit")
        self._show_crop(crop)
        self._commit_timer.start()

    def _on_canvas_crop_changed(self, cw: int, ch: int, cx: int, cy: int):
        """Called continuously while the user drags the crop overlay."""
        self._set_spinboxes((cw, ch, cx, cy))
        if self.cb_aspect.currentIndex() != 0:
            self._set_aspect_index(0, "Free edit — drag to adjust")
        self._show_crop((cw, ch, cx, cy))

    def _on_canvas_crop_committed(self, cw: int, ch: int, cx: int, cy: int):
        """Called once when the user releases the mouse after dragging."""
        self._show_crop((cw, ch, cx, cy))
        self._commit_crop()

    def _reset_crop(self):
        info = self._current_info
        if not info or not info.get("crop_auto"):
            return
        crop = parse_crop(info["crop_auto"])
        cw, ch, *_ = crop
        self._set_spinboxes(crop)
        self._set_aspect_index(1, f"Auto-detected: {cw}×{ch} ({cw / ch:.3f}:1)")
        self._show_crop(crop)
        self._commit_crop()

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

    def _flush_pending_commit(self):
        """Report a debounced spinbox edit before switching files."""
        if self._commit_timer.isActive():
            self._commit_crop()

    def load_file(self, info: dict):
        self._flush_pending_commit()
        self._frame_load_gen += 1
        self._orig_pixmap = None
        self._current_info = info
        crop = info.get("crop")

        if not crop:
            self._show_message(f"{Path(info['path']).name} — no crop data (run detection first)")
            return

        if "crop_auto" not in info:
            info["crop_auto"] = crop

        cw, ch, *_ = parse_crop(crop)
        if cw == info["width"] and ch == info["height"]:
            self._show_message(f"{Path(info['path']).name} — no black bars detected")
            return

        self._set_spinbox_limits(info)
        self._set_spinboxes(parse_crop(crop))
        self._set_crop_controls_enabled(True)
        self._set_aspect_index(1, f"Auto-detected: {cw}×{ch} ({cw / ch:.3f}:1)")
        self._update_info_label(crop)
        self._fit = True

        duration = info.get("duration", 0)
        self.slider.blockSignals(True)
        self.slider.setEnabled(True)
        self.slider.setRange(0, max(1, int(duration)))
        self.slider.setValue(min(30, int(duration / 2)))
        self.slider.blockSignals(False)
        self.lbl_time.setText(format_timestamp(self.slider.value()))
        self.lbl_duration.setText(format_timestamp(duration))
        self._update_frames()

    def clear(self):
        self._flush_pending_commit()
        self._frame_load_gen += 1
        self._current_info = None
        self._show_message("Select a file and run detection, then click Preview")
        self._set_aspect_index(0, "")

    # ------------------------------------------------------------------
    # Frame loading
    # ------------------------------------------------------------------

    def _show_message(self, text: str):
        self.lbl_info.setText(text)
        self._clear_images()
        self._set_crop_controls_enabled(False)
        self.slider.setEnabled(False)

    def _set_crop_controls_enabled(self, enabled: bool):
        for sp in (self.sp_w, self.sp_h, self.sp_x, self.sp_y):
            sp.setEnabled(enabled)
        self.btn_reset.setEnabled(enabled)
        self.cb_aspect.setEnabled(enabled)

    def _clear_images(self):
        self.view_orig.clear_frame()
        self.view_crop.set_image(None)
        self.lbl_orig_size.setText("")
        self.lbl_crop_size.setText("")
        self.lbl_loading.setText("")
        self.lbl_time.setText("0:00:00")
        self.lbl_duration.setText("0:00:00")
        self._orig_pixmap = None
        self.area_orig.update_cursor()
        self.area_crop.update_cursor()

    def _on_slider_moved(self, value: int):
        self.lbl_time.setText(format_timestamp(value))
        self._scrub_timer.start()

    def _update_frames(self):
        info = self._current_info
        if not info or not info.get("crop"):
            return
        timestamp = self.slider.value()
        path = info["path"]
        self._frame_load_gen += 1
        gen = self._frame_load_gen
        self.lbl_loading.setText("Loading frame…")

        def _load():
            self._frame_ready.emit(gen, extract_frame(path, timestamp))

        threading.Thread(target=_load, daemon=True).start()

    def _on_frame_loaded(self, gen: int, frame_path):
        pixmap = QPixmap(frame_path) if frame_path else None
        if frame_path:
            try:
                os.unlink(frame_path)  # the pixmap now holds the frame in memory
            except OSError:
                pass
        if gen != self._frame_load_gen:
            return  # a newer request superseded this one
        self.lbl_loading.setText("")
        info = self._current_info
        if pixmap is None or pixmap.isNull() or not info:
            self.lbl_orig_size.setText("Failed to extract frame")
            return

        cw, ch, cx, cy = parse_crop(info["crop"])
        self._orig_pixmap = pixmap
        self.view_orig.load_frame(pixmap, (cw, ch, cx, cy))
        self.view_crop.set_image(pixmap, QRect(cx, cy, cw, ch))
        self.lbl_orig_size.setText(f"{pixmap.width()}×{pixmap.height()}")
        self.lbl_crop_size.setText(f"{cw}×{ch}")
        self._apply_zoom()


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------


class BlackBarRemoveApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BlackBar Remove")
        if os.path.exists(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))
        self.setMinimumSize(960, 700)
        self.resize(1280, 900)
        self.setAcceptDrops(True)

        self.files: list[dict] = []
        self.crop_workers: list[CropDetectWorker] = []
        self.encode_worker: EncodeWorker | None = None
        self.encode_queue: list[dict] = []
        self._detect_index = 0
        self._active_detect_count = 0
        self._encode_index = 0

        self._build_ui()
        self._check_ffmpeg_on_startup()
        self._on_hw_mode_changed(0)

    # ------------------------------------------------------------------
    # Startup checks
    # ------------------------------------------------------------------

    def _check_ffmpeg_on_startup(self):
        """Warn the user if ffmpeg is not found or HW accel is unavailable."""
        try:
            result = run_hidden(
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

        hw_found = check_hw_available()
        hw_available = []
        if sys.platform in ("win32", "linux") and "amf" in hw_found:
            hw_available.append("AMF (AMD)")
        if sys.platform == "darwin" and "videotoolbox" in hw_found:
            hw_available.append("VideoToolbox")

        if hw_available:
            self._log(f"✔  ffmpeg found: {FFMPEG}  |  {', '.join(hw_available)} available")
        else:
            if sys.platform in ("win32", "linux"):
                self._log(
                    "⚠  No AMD AMF encoder found in this ffmpeg build.  "
                    "Install an ffmpeg build with --enable-amf "
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
        layout.setSpacing(6)

        # Input: one row — mode, path, browse (files/folders can also be dropped)
        input_group = QGroupBox("Input")
        input_row = QHBoxLayout(input_group)
        self.rb_file = QRadioButton("File")
        self.rb_folder = QRadioButton("Folder (batch)")
        self.rb_file.setChecked(True)
        self.le_input = QLineEdit()
        self.le_input.setPlaceholderText(
            "Browse, paste a path and press Enter, or drop a video file / folder onto the window…"
        )
        self.le_input.returnPressed.connect(
            lambda: self._load_files(self.le_input.text().strip())
        )
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse)
        input_row.addWidget(self.rb_file)
        input_row.addWidget(self.rb_folder)
        input_row.addWidget(self.le_input, 1)
        input_row.addWidget(btn_browse)
        layout.addWidget(input_group)

        # Settings: two compact rows on a grid
        settings_group = QGroupBox("Settings")
        grid = QGridLayout(settings_group)
        grid.setHorizontalSpacing(8)

        self.cb_hw = QComboBox()
        for label, _key in HW_MODES:
            self.cb_hw.addItem(label)
        self.cb_hw.setCurrentIndex(0)  # first mode for this platform is the default
        self.cb_hw.setToolTip(
            "AMF – HW Encode (AMD):        Software decode, AMD Radeon AMF hardware encode\n"
            "AMF – Full HW Pipeline (AMD): d3d11va decode + crop + AMF encode  (Windows, AMD)\n"
            "VideoToolbox – HW Encode:         Software decode, Apple VT hardware encode\n"
            "VideoToolbox – Full HW Pipeline:  Apple VT decode + crop + encode  (fastest on macOS)\n"
            "CPU – Software:          Fully software encode via libx264 / libx265"
        )
        self.cb_hw.currentIndexChanged.connect(self._on_hw_mode_changed)

        self.sp_quality = QSpinBox()
        self.sp_quality.setRange(1, 51)
        self.sp_quality.setValue(QUALITY_DEFAULT)
        self.sp_quality.setToolTip(
            "1 = best quality / largest file,  51 = worst / smallest\n"
            "AMF: constant QP  (-rc cqp; same 1–51 scale, rescaled for AV1)\n"
            "VideoToolbox: mapped to -q:v 100–1\n"
            "CPU: CRF value  (same scale applies for libx264/libx265)"
        )

        self.cb_preset = QComboBox()
        self.cb_preset.addItems(PRESETS)
        self.cb_preset.setCurrentText(PRESET_DEFAULT)
        self.cb_preset.setToolTip(
            "Encoding speed preset.  Slower = better compression.\n"
            "AMF (AMD) maps fast→speed, medium→balanced, slow→quality."
        )

        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(1, 120)
        self.sp_interval.setValue(15)
        self.sp_interval.setSuffix(" s")
        self.sp_interval.setToolTip("Seconds between frames sampled for black-bar detection.")

        self.le_suffix = QLineEdit(DEFAULT_SUFFIX)
        self.le_suffix.setMaximumWidth(140)
        self.le_suffix.setToolTip("Appended to the output file name, e.g. movie_cropped.mkv")

        self.chk_overwrite = QCheckBox("Overwrite original")
        self.chk_overwrite.setToolTip("Encode to a temporary file, then replace the source")
        self.chk_overwrite.toggled.connect(lambda on: self.le_suffix.setEnabled(not on))

        for row, cells in enumerate((
            (("HW mode:", self.cb_hw), ("Quality:", self.sp_quality), ("Preset:", self.cb_preset)),
            (("Sample interval:", self.sp_interval), ("Output suffix:", self.le_suffix),
             (None, self.chk_overwrite)),
        )):
            for i, (label, widget) in enumerate(cells):
                if label:
                    grid.addWidget(QLabel(label), row, i * 2,
                                   alignment=Qt.AlignmentFlag.AlignRight)
                grid.addWidget(widget, row, i * 2 + 1)
        grid.setColumnStretch(6, 1)
        layout.addWidget(settings_group)

        # Actions + progress on one row
        action_row = QHBoxLayout()
        self.btn_detect = QPushButton("Detect Black Bars")
        self.btn_detect.clicked.connect(self._start_detection)
        self.btn_preview = QPushButton("Preview")
        self.btn_preview.clicked.connect(self._preview_selected)
        self.btn_preview.setEnabled(False)
        self.btn_preview.setToolTip("Load preview for the selected file (or double-click a row)")
        self.btn_process = QPushButton("Process")
        self.btn_process.clicked.connect(self._start_processing)
        self.btn_process.setEnabled(False)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel_operation)
        self.btn_cancel.setEnabled(False)
        for b in (self.btn_detect, self.btn_preview, self.btn_process, self.btn_cancel):
            action_row.addWidget(b)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        action_row.addWidget(self.progress, 1)
        action_row.addStretch()
        layout.addLayout(action_row)

        # Resizable vertical splitter: table / preview / log
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["File", "Resolution", "Codec", "Detected Crop", "New Resolution", "Status"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.currentCellChanged.connect(self._on_table_selection_changed)
        self.table.cellDoubleClicked.connect(lambda *_: self._preview_selected())
        self.splitter.addWidget(self.table)

        self.preview = PreviewPanel()
        self.preview.crop_changed.connect(self._on_crop_changed)
        self.splitter.addWidget(self.preview)

        self.log_widget = QTextEdit()
        self.log_widget.setReadOnly(True)
        self.splitter.addWidget(self.log_widget)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([150, 620, 90])
        layout.addWidget(self.splitter, 1)

    # ------------------------------------------------------------------
    # Drag & drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = event.mimeData().urls()[0].toLocalFile()
        (self.rb_folder if os.path.isdir(path) else self.rb_file).setChecked(True)
        self.le_input.setText(path)
        self._load_files(path)

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------

    def _hw_mode_key(self) -> str:
        """Return the internal key for the currently selected HW mode."""
        return HW_MODES[self.cb_hw.currentIndex()][1]

    def _on_hw_mode_changed(self, _index: int):
        mode = self._hw_mode_key()
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
        if not path:
            return
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

        str_paths = [str(fp) for fp in paths]
        if not str_paths:
            self._log(f"No supported video files found in: {path}")
            self.btn_process.setEnabled(False)
            self.btn_preview.setEnabled(False)
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with ThreadPoolExecutor(max_workers=min(8, len(str_paths))) as pool:
                results = list(pool.map(get_video_info, str_paths))
        finally:
            QApplication.restoreOverrideCursor()
        self.files.extend(info for info in results if info)

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
        interval = effective_sample_interval(self.sp_interval.value(), info.get("duration", 0))
        note = (
            f"  (short clip: sampling every {interval:g} s)"
            if interval != self.sp_interval.value() else ""
        )
        self._log(f"Detecting: {Path(info['path']).name}{note}")
        worker = CropDetectWorker(
            info["path"],
            interval,
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
            f"preset={self.cb_preset.currentText()}]"
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
            suffix = self.le_suffix.text() or DEFAULT_SUFFIX
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
            preset=self.cb_preset.currentText(),
            on_progress=self._on_encode_progress,
            on_done=self._on_encode_done,
        )
        self.encode_worker.start()

    def _on_encode_progress(self, _filepath: str, pct: float):
        self.progress.setValue(int(pct))

    def _on_encode_done(self, filepath: str, success: bool, ffmpeg_tail: list[str]):
        info = self.encode_queue[self._encode_index]
        row = info["_row"]
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
            self._log(f"  Error encoding: {p.name}  — ffmpeg output:")
            for line in ffmpeg_tail or ["(no output captured)"]:
                self._log(f"    {line}")
            self._log(
                "  If this is a hardware-encoder error, try switching to CPU mode."
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
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    if os.path.exists(APP_ICON_PATH):
        app.setWindowIcon(QIcon(APP_ICON_PATH))
    window = BlackBarRemoveApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
