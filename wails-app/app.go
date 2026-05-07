package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"

	wailsrt "github.com/wailsapp/wails/v2/pkg/runtime"
)

const maxDetectWorkers = 4

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type VideoInfo struct {
	Path       string  `json:"path"`
	Name       string  `json:"name"`
	Width      int     `json:"width"`
	Height     int     `json:"height"`
	VideoCodec string  `json:"video_codec"`
	AudioCodec string  `json:"audio_codec"`
	Duration   float64 `json:"duration"`
	Is10Bit    bool    `json:"is_10bit"`
	PixFmt     string  `json:"pix_fmt"`
	Crop       string  `json:"crop"`
	CropAuto   string  `json:"crop_auto"`
}

type EncodeSettings struct {
	HWMode         string `json:"hw_mode"`
	Quality        int    `json:"quality"`
	Preset         string `json:"preset"`
	LookAhead      bool   `json:"look_ahead"`
	SampleInterval int    `json:"sample_interval"`
	Suffix         string `json:"suffix"`
	Overwrite      bool   `json:"overwrite"`
}

type FFmpegStatus struct {
	Found    bool   `json:"found"`
	Path     string `json:"path"`
	QSVAvail bool   `json:"qsv_avail"`
	VTAvail  bool   `json:"vt_avail"`
	AMFAvail bool   `json:"amf_avail"`
	Platform string `json:"platform"`
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

type App struct {
	ctx        context.Context
	ffmpeg     string
	ffprobe    string
	files      []*VideoInfo
	cancelFunc context.CancelFunc
	mu         sync.Mutex
}

func NewApp() *App {
	return &App{
		ffmpeg:  findFFmpegTool("ffmpeg"),
		ffprobe: findFFmpegTool("ffprobe"),
	}
}

func (a *App) startup(ctx context.Context) {
	a.ctx = ctx
}

func (a *App) emit(event string, data interface{}) {
	wailsrt.EventsEmit(a.ctx, event, data)
}

func (a *App) log(msg string) {
	a.emit("log", msg)
}

// ---------------------------------------------------------------------------
// CheckFFmpeg
// ---------------------------------------------------------------------------

func (a *App) CheckFFmpeg() FFmpegStatus {
	status := FFmpegStatus{Platform: runtime.GOOS}

	if err := exec.Command(a.ffmpeg, "-version").Run(); err != nil {
		return status
	}
	status.Found = true
	status.Path = a.ffmpeg

	if out, err := exec.Command(a.ffmpeg, "-hide_banner", "-hwaccels").Output(); err == nil {
		lower := strings.ToLower(string(out))
		status.QSVAvail = strings.Contains(lower, "qsv")
		status.VTAvail = strings.Contains(lower, "videotoolbox")
	}
	// AMF is encoder-only — not listed in -hwaccels. Probe -encoders.
	if out, err := exec.Command(a.ffmpeg, "-hide_banner", "-encoders").Output(); err == nil {
		status.AMFAvail = strings.Contains(strings.ToLower(string(out)), "_amf")
	}
	return status
}

// ---------------------------------------------------------------------------
// LoadFiles
// ---------------------------------------------------------------------------

func (a *App) LoadFiles(path string) []*VideoInfo {
	a.mu.Lock()
	a.files = nil
	a.mu.Unlock()

	stat, err := os.Stat(path)
	if err != nil {
		a.log(fmt.Sprintf("Invalid path: %s", path))
		return nil
	}

	var paths []string
	if stat.IsDir() {
		entries, _ := os.ReadDir(path)
		for _, e := range entries {
			if !e.IsDir() && supportedExtensions[strings.ToLower(filepath.Ext(e.Name()))] {
				paths = append(paths, filepath.Join(path, e.Name()))
			}
		}
		sort.Strings(paths)
	} else {
		if !supportedExtensions[strings.ToLower(filepath.Ext(path))] {
			a.log(fmt.Sprintf("Unsupported file type: %s", filepath.Ext(path)))
			return nil
		}
		paths = []string{path}
	}

	var results []*VideoInfo
	for _, p := range paths {
		info, err := getVideoInfo(a.ffprobe, p)
		if err != nil {
			a.log(fmt.Sprintf("Could not read: %s (%v)", filepath.Base(p), err))
			continue
		}
		results = append(results, info)
	}

	a.mu.Lock()
	a.files = results
	a.mu.Unlock()

	a.log(fmt.Sprintf("Loaded %d video(s)", len(results)))
	return results
}

func (a *App) snapshotFiles() []*VideoInfo {
	a.mu.Lock()
	defer a.mu.Unlock()

	files := make([]*VideoInfo, len(a.files))
	for i, info := range a.files {
		if info == nil {
			continue
		}
		copyInfo := *info
		files[i] = &copyInfo
	}
	return files
}

func (a *App) updateDetectedCrop(path, crop string) {
	if crop == "" {
		return
	}

	a.mu.Lock()
	defer a.mu.Unlock()
	for _, info := range a.files {
		if info != nil && info.Path == path {
			info.Crop = crop
			info.CropAuto = crop
			return
		}
	}
}

func replaceFile(srcPath, dstPath string) error {
	if err := os.Rename(srcPath, dstPath); err == nil {
		return nil
	} else if !os.IsExist(err) && runtime.GOOS != "windows" {
		return err
	}

	backupPath := dstPath + ".bbr_backup"
	for i := 1; ; i++ {
		if _, err := os.Stat(backupPath); os.IsNotExist(err) {
			break
		}
		backupPath = fmt.Sprintf("%s.bbr_backup.%d", dstPath, i)
	}

	if err := os.Rename(dstPath, backupPath); err != nil {
		return err
	}
	if err := os.Rename(srcPath, dstPath); err != nil {
		if restoreErr := os.Rename(backupPath, dstPath); restoreErr != nil {
			return fmt.Errorf("%w; also failed to restore original: %v", err, restoreErr)
		}
		return err
	}
	_ = os.Remove(backupPath)
	return nil
}

// ---------------------------------------------------------------------------
// StartDetection
// ---------------------------------------------------------------------------

func (a *App) StartDetection(sampleInterval int) {
	files := a.snapshotFiles()

	if len(files) == 0 {
		a.log("No files loaded.")
		return
	}

	ctx, cancel := context.WithCancel(context.Background())
	a.mu.Lock()
	if a.cancelFunc != nil {
		a.cancelFunc()
	}
	a.cancelFunc = cancel
	a.mu.Unlock()

	go func() {
		sem := make(chan struct{}, maxDetectWorkers)
		var wg sync.WaitGroup
		var doneCount int64

		for idx, info := range files {
			select {
			case <-ctx.Done():
				wg.Wait()
				a.emit("detect:cancelled", nil)
				return
			default:
			}

			sem <- struct{}{}
			wg.Add(1)

			go func(row int, fi *VideoInfo) {
				defer func() { <-sem; wg.Done() }()

				a.emit("detect:start", map[string]interface{}{
					"row":      row,
					"filepath": fi.Path,
				})

				crop := runCropDetect(ctx, a.ffmpeg, fi.Path, sampleInterval)
				done := atomic.AddInt64(&doneCount, 1)

				a.updateDetectedCrop(fi.Path, crop)

				a.emit("detect:result", map[string]interface{}{
					"row":      row,
					"filepath": fi.Path,
					"crop":     crop,
					"done":     done,
					"total":    int64(len(files)),
				})
			}(idx, info)
		}

		wg.Wait()
		a.emit("detect:complete", nil)
	}()
}

// ---------------------------------------------------------------------------
// StartProcessing
// ---------------------------------------------------------------------------

func (a *App) StartProcessing(settings EncodeSettings) {
	files := a.snapshotFiles()

	type qItem struct {
		info    *VideoInfo
		origIdx int
	}
	var queue []qItem
	for i, info := range files {
		if info.Crop == "" {
			continue
		}
		cw, ch, _, _, err := parseCrop(info.Crop)
		if err != nil || (cw == info.Width && ch == info.Height) {
			continue
		}
		queue = append(queue, qItem{info, i})
	}

	if len(queue) == 0 {
		a.log("Nothing to process — no black bars detected.")
		return
	}

	suffix := settings.Suffix
	if suffix == "" {
		suffix = "_nocrop"
	}

	ctx, cancel := context.WithCancel(context.Background())
	a.mu.Lock()
	if a.cancelFunc != nil {
		a.cancelFunc()
	}
	a.cancelFunc = cancel
	a.mu.Unlock()

	a.log(fmt.Sprintf("Processing %d file(s) [mode=%s, quality=%d, preset=%s]",
		len(queue), settings.HWMode, settings.Quality, settings.Preset))

	go func() {
		for _, item := range queue {
			select {
			case <-ctx.Done():
				a.log("Encoding cancelled.")
				a.emit("encode:cancelled", nil)
				return
			default:
			}

			info := item.info
			ext := filepath.Ext(info.Path)
			base := strings.TrimSuffix(info.Path, ext)

			var outPath string
			if settings.Overwrite {
				outPath = base + "_tmp" + ext
			} else {
				outPath = base + suffix + ext
			}

			a.emit("encode:start", map[string]interface{}{
				"row":    item.origIdx,
				"output": filepath.Base(outPath),
			})
			a.log(fmt.Sprintf("Encoding: %s → %s", info.Name, filepath.Base(outPath)))

			args := buildEncodeArgs(info.Path, outPath, info.Crop, settings.HWMode,
				info, settings.Quality, settings.LookAhead, settings.Preset)

			success, errText := runEncode(ctx, a.ffmpeg, args, info.Duration, func(pct float64) {
				a.emit("encode:progress", map[string]interface{}{
					"row": item.origIdx,
					"pct": pct,
				})
			})

			if success && settings.Overwrite {
				if err := replaceFile(outPath, info.Path); err != nil {
					a.log(fmt.Sprintf("Warning: could not replace original: %v", err))
					success = false
				} else {
					a.log(fmt.Sprintf("  Replaced original: %s", info.Name))
				}
			}

			if !success {
				os.Remove(outPath)
			}

			a.emit("encode:done", map[string]interface{}{
				"row":     item.origIdx,
				"success": success,
			})

			if success {
				a.log(fmt.Sprintf("  Finished: %s", info.Name))
			} else {
				a.log(fmt.Sprintf("  Error encoding: %s (try CPU mode)", info.Name))
				if errText != "" {
					a.log(fmt.Sprintf("  ffmpeg: %s", errText))
				}
			}
		}

		a.emit("encode:complete", nil)
		a.log("All files processed.")
	}()
}

// ---------------------------------------------------------------------------
// CancelOperation
// ---------------------------------------------------------------------------

func (a *App) CancelOperation() {
	a.mu.Lock()
	if a.cancelFunc != nil {
		a.cancelFunc()
		a.cancelFunc = nil
	}
	a.mu.Unlock()
}

// ---------------------------------------------------------------------------
// GetFrame – returns base64-encoded PNG
// ---------------------------------------------------------------------------

func (a *App) GetFrame(filePath string, timestamp float64, cropFilter string) string {
	b64, err := extractFrameBase64(a.ffmpeg, filePath, timestamp, cropFilter)
	if err != nil {
		return ""
	}
	return b64
}

// ---------------------------------------------------------------------------
// UpdateCrop
// ---------------------------------------------------------------------------

func (a *App) UpdateCrop(filePath string, crop string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, info := range a.files {
		if info.Path == filePath {
			info.Crop = crop
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// CalcCropForAspect
// ---------------------------------------------------------------------------

func (a *App) CalcCropForAspect(origW, origH int, arW, arH float64) map[string]int {
	cw, ch, cx, cy := calcCropForAspect(origW, origH, arW, arH)
	return map[string]int{"w": cw, "h": ch, "x": cx, "y": cy}
}

// ---------------------------------------------------------------------------
// File dialogs
// ---------------------------------------------------------------------------

func (a *App) OpenFileDialog() string {
	path, err := wailsrt.OpenFileDialog(a.ctx, wailsrt.OpenDialogOptions{
		Title: "Select Video File",
		Filters: []wailsrt.FileFilter{
			{DisplayName: "Video Files", Pattern: "*.mp4;*.mkv;*.avi;*.mov;*.ts;*.flv;*.wmv;*.webm;*.m4v"},
			{DisplayName: "All Files", Pattern: "*"},
		},
	})
	if err != nil {
		return ""
	}
	return path
}

func (a *App) OpenFolderDialog() string {
	path, err := wailsrt.OpenDirectoryDialog(a.ctx, wailsrt.OpenDialogOptions{
		Title: "Select Folder",
	})
	if err != nil {
		return ""
	}
	return path
}
