package main

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
)

// ---------------------------------------------------------------------------
// Constants / maps
// ---------------------------------------------------------------------------

var qsvDecoders = map[string]string{
	"h264":       "h264_qsv",
	"hevc":       "hevc_qsv",
	"h265":       "hevc_qsv",
	"vp9":        "vp9_qsv",
	"av1":        "av1_qsv",
	"mpeg2video": "mpeg2_qsv",
	"vc1":        "vc1_qsv",
}

var qsvEncoders = map[string]string{
	"h264": "h264_qsv",
	"hevc": "hevc_qsv",
	"h265": "hevc_qsv",
	"av1":  "av1_qsv",
}

var swEncoders = map[string]string{
	"h264": "libx264",
	"hevc": "libx265",
	"h265": "libx265",
	"vp9":  "libvpx-vp9",
}

var vtEncoders = map[string]string{
	"h264":   "h264_videotoolbox",
	"hevc":   "hevc_videotoolbox",
	"h265":   "hevc_videotoolbox",
	"prores": "prores_videotoolbox",
}

var supportedExtensions = map[string]bool{
	".mp4": true, ".mkv": true, ".avi": true, ".mov": true,
	".ts": true, ".flv": true, ".wmv": true, ".webm": true, ".m4v": true,
}

var cropPattern = regexp.MustCompile(`crop=(\d+:\d+:\d+:\d+)`)
var progressTimePat = regexp.MustCompile(`out_time_ms=(\d+)`)

type tailWriter struct {
	buf []byte
	max int
}

func newTailWriter(max int) *tailWriter {
	return &tailWriter{max: max}
}

func (w *tailWriter) Write(p []byte) (int, error) {
	if w.max <= 0 {
		return len(p), nil
	}
	w.buf = append(w.buf, p...)
	if len(w.buf) > w.max {
		w.buf = w.buf[len(w.buf)-w.max:]
	}
	return len(p), nil
}

func (w *tailWriter) String() string {
	return strings.TrimSpace(string(w.buf))
}

// ---------------------------------------------------------------------------
// Tool discovery
// ---------------------------------------------------------------------------

func findFFmpegTool(name string) string {
	if path, err := exec.LookPath(name); err == nil {
		return path
	}
	home, _ := os.UserHomeDir()
	var candidates []string
	switch runtime.GOOS {
	case "windows":
		exe := name + ".exe"
		candidates = []string{
			`C:\ffmpeg\bin\` + exe,
			`C:\Program Files\ffmpeg\bin\` + exe,
			filepath.Join(home, "ffmpeg", "bin", exe),
			filepath.Join(home, "scoop", "apps", "ffmpeg", "current", "bin", exe),
		}
	case "darwin":
		candidates = []string{
			"/opt/homebrew/bin/" + name,
			"/usr/local/bin/" + name,
			filepath.Join(home, "bin", name),
		}
	default:
		candidates = []string{
			"/usr/bin/" + name,
			"/usr/local/bin/" + name,
			"/snap/bin/" + name,
			filepath.Join(home, "bin", name),
		}
	}
	for _, c := range candidates {
		if _, err := os.Stat(c); err == nil {
			return c
		}
	}
	return name
}

// ---------------------------------------------------------------------------
// Video info
// ---------------------------------------------------------------------------

type ffprobeResult struct {
	Streams []struct {
		CodecType string `json:"codec_type"`
		CodecName string `json:"codec_name"`
		Width     int    `json:"width"`
		Height    int    `json:"height"`
		PixFmt    string `json:"pix_fmt"`
	} `json:"streams"`
	Format struct {
		Duration string `json:"duration"`
	} `json:"format"`
}

func getVideoInfo(ffprobePath, path string) (*VideoInfo, error) {
	cmd := exec.Command(ffprobePath,
		"-v", "quiet",
		"-print_format", "json",
		"-show_streams", "-show_format",
		path,
	)
	out, err := cmd.Output()
	if err != nil {
		return nil, err
	}
	var probe ffprobeResult
	if err := json.Unmarshal(out, &probe); err != nil {
		return nil, err
	}
	info := &VideoInfo{Path: path, Name: filepath.Base(path)}
	for _, s := range probe.Streams {
		if s.CodecType == "video" && info.VideoCodec == "" {
			info.VideoCodec = strings.ToLower(s.CodecName)
			info.Width = s.Width
			info.Height = s.Height
			info.PixFmt = s.PixFmt
			info.Is10Bit = strings.Contains(s.PixFmt, "10")
		} else if s.CodecType == "audio" && info.AudioCodec == "" {
			info.AudioCodec = s.CodecName
		}
	}
	if info.VideoCodec == "" {
		return nil, fmt.Errorf("no video stream in %s", filepath.Base(path))
	}
	if d, err := strconv.ParseFloat(probe.Format.Duration, 64); err == nil {
		info.Duration = d
	}
	return info, nil
}

// ---------------------------------------------------------------------------
// Frame extraction
// ---------------------------------------------------------------------------

func extractFrameBase64(ffmpegPath, path string, timestamp float64, cropFilter string) (string, error) {
	tmp, err := os.CreateTemp("", "bbr_frame_*.png")
	if err != nil {
		return "", err
	}
	tmp.Close()
	defer os.Remove(tmp.Name())

	vf := "null"
	if cropFilter != "" {
		vf = cropFilter
	}
	cmd := exec.Command(ffmpegPath,
		"-ss", strconv.FormatFloat(timestamp, 'f', 3, 64),
		"-i", path,
		"-vf", vf,
		"-frames:v", "1",
		"-y", tmp.Name(),
	)
	if err := cmd.Run(); err != nil {
		return "", err
	}
	data, err := os.ReadFile(tmp.Name())
	if err != nil || len(data) == 0 {
		return "", fmt.Errorf("empty frame output")
	}
	return base64.StdEncoding.EncodeToString(data), nil
}

// ---------------------------------------------------------------------------
// Crop detect
// ---------------------------------------------------------------------------

func runCropDetect(ctx context.Context, ffmpegPath, path string, sampleInterval int) string {
	cmd := exec.CommandContext(ctx, ffmpegPath,
		"-i", path,
		"-vf", fmt.Sprintf("fps=1/%d,cropdetect=24:16:0", sampleInterval),
		"-f", "null", "-",
	)
	out, _ := cmd.CombinedOutput()

	matches := cropPattern.FindAllStringSubmatch(string(out), -1)
	if len(matches) == 0 {
		return ""
	}
	counts := make(map[string]int)
	for _, m := range matches {
		counts[m[1]]++
	}
	best, bestCount := "", 0
	for k, v := range counts {
		if v > bestCount {
			bestCount = v
			best = k
		}
	}
	if best != "" {
		return "crop=" + best
	}
	return ""
}

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

func buildEncodeArgs(srcPath, outPath, cropFilter, hwMode string, info *VideoInfo, quality int, lookAhead bool, preset string) []string {
	codec := strings.ToLower(info.VideoCodec)

	var preInput []string
	vf := cropFilter
	var encoder string
	var encArgs []string

	switch hwMode {
	case "qsv", "qsv_fullhw":
		if codec == "h264" && info.Is10Bit {
			encoder = "libx264"
			encArgs = []string{"-crf", strconv.Itoa(quality), "-preset", preset}
		} else {
			enc := qsvEncoders[codec]
			if enc == "" {
				enc = "h264_qsv"
			}
			encoder = enc
			encArgs = []string{"-global_quality", strconv.Itoa(quality), "-preset", preset}
			if lookAhead && hwMode != "qsv_fullhw" {
				encArgs = append(encArgs, "-look_ahead", "1", "-look_ahead_depth", "40")
			}
		}
		if hwMode == "qsv_fullhw" {
			if dec := qsvDecoders[codec]; dec != "" {
				preInput = []string{"-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", dec}
				vf = fmt.Sprintf("hwdownload,format=nv12,%s,hwupload=extra_hw_frames=64", cropFilter)
			}
		}

	case "vt", "vt_fullhw":
		enc := vtEncoders[codec]
		if enc == "" {
			enc = "h264_videotoolbox"
		}
		encoder = enc
		vtQ := 1.0 - (float64(quality)-1)/50.0
		if vtQ < 0.01 {
			vtQ = 0.01
		}
		encArgs = []string{"-q:v", fmt.Sprintf("%.2f", vtQ), "-allow_sw", "1"}
		if hwMode == "vt_fullhw" {
			preInput = []string{"-hwaccel", "videotoolbox", "-hwaccel_output_format", "videotoolbox"}
			vf = fmt.Sprintf("hwdownload,format=nv12,%s", cropFilter)
		}

	default: // cpu
		enc := swEncoders[codec]
		if enc == "" {
			enc = "libx264"
		}
		encoder = enc
		encArgs = []string{"-crf", strconv.Itoa(quality), "-preset", preset}
	}

	args := append(preInput,
		"-i", srcPath,
		"-vf", vf,
		"-c:v", encoder,
	)
	args = append(args, encArgs...)
	args = append(args,
		"-c:a", "copy",
		"-c:s", "copy",
		"-map", "0",
		"-progress", "pipe:1",
		"-y", outPath,
	)
	return args
}

func runEncode(ctx context.Context, ffmpegPath string, args []string, duration float64, onProgress func(float64)) (bool, string) {
	cmd := exec.CommandContext(ctx, ffmpegPath, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return false, err.Error()
	}
	errTail := newTailWriter(8192)
	cmd.Stderr = errTail
	if err := cmd.Start(); err != nil {
		return false, err.Error()
	}
	scanner := bufio.NewScanner(stdout)
	for scanner.Scan() {
		line := scanner.Text()
		if m := progressTimePat.FindStringSubmatch(line); len(m) > 1 {
			if us, err := strconv.ParseInt(m[1], 10, 64); err == nil && duration > 0 {
				pct := float64(us) / 1_000_000 / duration * 100
				if pct > 100 {
					pct = 100
				}
				onProgress(pct)
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return false, err.Error()
	}
	if err := cmd.Wait(); err != nil {
		errText := errTail.String()
		if errText == "" {
			errText = err.Error()
		}
		return false, errText
	}
	return true, ""
}

// ---------------------------------------------------------------------------
// Crop math
// ---------------------------------------------------------------------------

func parseCrop(crop string) (w, h, x, y int, err error) {
	s := strings.TrimPrefix(crop, "crop=")
	parts := strings.Split(s, ":")
	if len(parts) != 4 {
		return 0, 0, 0, 0, fmt.Errorf("invalid crop: %s", crop)
	}
	vals := [4]int{}
	for i, p := range parts {
		v, e := strconv.Atoi(strings.TrimSpace(p))
		if e != nil {
			return 0, 0, 0, 0, e
		}
		vals[i] = v
	}
	return vals[0], vals[1], vals[2], vals[3], nil
}

func calcCropForAspect(origW, origH int, arW, arH float64) (cropW, cropH, offX, offY int) {
	target := arW / arH
	current := float64(origW) / float64(origH)
	if absF(current-target) < 0.001 {
		return origW, origH, 0, 0
	}
	var nw, nh int
	if current > target {
		nh = origH
		nw = int(float64(origH) * target)
	} else {
		nw = origW
		nh = int(float64(origW) / target)
	}
	nw -= nw % 2
	nh -= nh % 2
	if nw > origW {
		nw = origW
	}
	if nh > origH {
		nh = origH
	}
	ox := (origW - nw) / 2
	oy := (origH - nh) / 2
	ox -= ox % 2
	oy -= oy % 2
	return nw, nh, ox, oy
}

func absF(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}
