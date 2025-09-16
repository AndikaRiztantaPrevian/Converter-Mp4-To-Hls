#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MP4 → HLS Converter (No-Reencode by default)
- Drag & drop multiple .mp4 files
- Let user choose output folder
- Each output is an individual HLS folder zipped after success
- Cross-platform: Windows & macOS (Linux too)

Dependencies:
  pip install PySide6

Requirements:
  - FFmpeg & FFprobe available in PATH, or placed in a local ./bin/ next to this script:
      Windows: ./bin/ffmpeg.exe and ./bin/ffprobe.exe
      macOS:  ./bin/ffmpeg and ./bin/ffprobe (chmod +x)

Notes:
  - "No quality loss" is achieved by transmuxing (copying streams) into HLS segments
    using -c:v copy -c:a copy, which requires H.264/AAC in the source MP4 for HLS
    compatibility. Otherwise, file is skipped (default) or you can enable transcoding.
"""

import os
import sys
import shutil
import json
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple
import zipfile

from PySide6.QtCore import Qt, QMimeData, Signal, QObject, QThread
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QListWidget, QListWidgetItem,
    QMessageBox, QCheckBox, QProgressBar, QTextEdit, QFrame, QAbstractItemView,
    QSpinBox
)

APP_TITLE = "MP4 → HLS (No-Reencode)"
APP_NAME = "bikindesign_mp4_to_hls"

def get_config_path() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.getenv("APPDATA", Path.home()))
    elif sys.platform == "darwin":  # macOS
        base = Path.home() / "Library" / "Application Support"
    else:  # Linux/Unix
        base = Path.home() / ".config"

    cfg_dir = base / APP_NAME
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return cfg_dir / "settings.json"

CONFIG_FILE = get_config_path()


# ---------- FFmpeg discovery ----------

def _exe_name(name: str) -> str:
    if sys.platform.startswith("win"):
        return f"{name}.exe"
    return name


def find_ffmpeg_binaries() -> Tuple[str, str]:
    """Try to find ffmpeg and ffprobe in PATH or local ./bin folder."""
    candidates = [
        shutil.which("ffmpeg"),
        str(Path(__file__).parent / "bin" / _exe_name("ffmpeg"))
    ]
    ffmpeg_path = next((c for c in candidates if c and Path(c).exists()), None)

    candidates_probe = [
        shutil.which("ffprobe"),
        str(Path(__file__).parent / "bin" / _exe_name("ffprobe"))
    ]
    ffprobe_path = next((c for c in candidates_probe if c and Path(c).exists()), None)

    if not ffmpeg_path or not ffprobe_path:
        raise FileNotFoundError(
            "FFmpeg/FFprobe not found. Please install FFmpeg and ensure both 'ffmpeg' and 'ffprobe' are in PATH,\n"
            "or place them in a './bin' folder next to this script."
        )
    return ffmpeg_path, ffprobe_path

# ---------- Probing utilities ----------

def ffprobe_streams(ffprobe_path: str, input_path: str) -> dict:
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        input_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout)


def get_duration_seconds(meta: dict) -> float:
    try:
        return float(meta.get("format", {}).get("duration", 0.0))
    except Exception:
        return 0.0


def codecs_are_hls_friendly(meta: dict) -> bool:
    v_ok, a_ok = False, True  # audio may be absent
    for s in meta.get("streams", []):
        if s.get("codec_type") == "video":
            v_ok = s.get("codec_name") in ("h264",)
        if s.get("codec_type") == "audio":
            a_ok = s.get("codec_name") in ("aac", "mp3", "ac3")  # HLS can handle a few
    return v_ok and a_ok

# ---------- Conversion worker (thread) ----------

@dataclass
class Job:
    src: Path
    out_root: Path
    skip_if_incompatible: bool = True
    enable_transcode_if_needed: bool = False  # Off by default per spec
    segment_seconds: int = 6


class Signals(QObject):
    log = Signal(str)
    progress = Signal(int)  # 0..100 overall
    file_progress = Signal(str, int)  # filename, 0..100
    file_done = Signal(str, str)  # filename, status OK/SKIP/FAIL
    all_done = Signal()


class ConverterThread(QThread):
    def __init__(self, jobs: List[Job]):
        super().__init__()
        self.jobs = jobs
        self.sig = Signals()
        self._stop = False

    def run(self):
        try:
            ffmpeg, ffprobe = find_ffmpeg_binaries()
        except Exception as e:
            self.sig.log.emit(f"ERROR: {e}")
            self.sig.all_done.emit()
            return

        total = len(self.jobs)
        for i, job in enumerate(self.jobs, start=1):
            if self._stop:
                break
            try:
                self._process_one(ffmpeg, ffprobe, job)
                self.sig.file_done.emit(job.src.name, "OK")
            except SkipError as e:
                self.sig.log.emit(f"SKIP: {job.src.name} — {e}")
                self.sig.file_done.emit(job.src.name, "SKIP")
            except Exception as e:
                self.sig.log.emit(f"FAIL: {job.src.name} — {e}")
                self.sig.file_done.emit(job.src.name, "FAIL")
            self.sig.progress.emit(int(i * 100 / total))

        self.sig.all_done.emit()

    def stop(self):
        self._stop = True

    # ---- core per-file processing ----
    def _process_one(self, ffmpeg: str, ffprobe: str, job: Job):
        src = job.src
        out_dir = job.out_root / (src.stem + "_hls")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        meta = ffprobe_streams(ffprobe, str(src))
        duration = max(1.0, get_duration_seconds(meta))
        hls_ok = codecs_are_hls_friendly(meta)

        if not hls_ok and job.skip_if_incompatible and not job.enable_transcode_if_needed:
            raise SkipError("Codecs not HLS-friendly (need H.264 video and AAC/MP3/AC3 audio).")

        m3u8_path = out_dir / "playlist.m3u8"
        seg_tmpl = out_dir / "segment_%05d.ts"

        # Build ffmpeg args
        args = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-i", str(src),
        ]
        if hls_ok:
            args += ["-c:v", "copy", "-c:a", "copy"]
        else:
            # Transcode path (if user later enables)
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k"]

        args += [
            "-movflags", "+faststart",
            "-f", "hls",
            "-hls_time", str(job.segment_seconds),
            "-hls_list_size", "0",
            "-hls_segment_filename", str(seg_tmpl),
            str(m3u8_path),
        ]

        # Run and live-parse progress from stderr ("time=")
        with subprocess.Popen(args, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1) as proc:
            last_update = time.time()
            while True:
                line = proc.stderr.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                # Parse a rough progress if we find time= in status lines
                if "time=" in line:
                    tpos = None
                    try:
                        # Extract HH:MM:SS.xx after time=
                        tstr = line.split("time=")[-1].split(" ")[0].strip()
                        h, m, s = tstr.split(":")
                        cur = float(h) * 3600 + float(m) * 60 + float(s)
                        pct = max(0, min(100, int(cur * 100 / duration)))
                        # Emit less frequently to avoid UI thrash
                        if time.time() - last_update > 0.05:
                            self.sig.file_progress.emit(src.name, pct)
                            last_update = time.time()
                    except Exception:
                        pass

            rc = proc.wait()
            if rc != 0:
                raise RuntimeError("ffmpeg failed — check logs and ensure codecs are supported.")

        # Zip the folder
        zip_path = job.out_root / f"{src.stem}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for root, _, files in os.walk(out_dir):
                for f in files:
                    p = Path(root) / f
                    # Simpan relatif dari out_dir agar isi zip bersih
                    arcname = os.path.relpath(p, out_dir)
                    zf.write(p, arcname=arcname)

        # Hapus folder HLS setelah di-zip
        shutil.rmtree(out_dir, ignore_errors=True)

        self.sig.log.emit(f"DONE: {src.name} → {zip_path.name}")
        self.sig.file_progress.emit(src.name, 100)



class SkipError(Exception):
    pass

def load_last_output() -> Optional[Path]:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if "last_output" in data:
                p = Path(data["last_output"])
                if p.exists():
                    return p
        except Exception:
            pass
    return None


def save_last_output(path: Path):
    data = {"last_output": str(path)}
    try:
        data["segment_seconds"] = int(getattr(MainWindow.instance(), "segment_input").value())
    except Exception:
        pass
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------- UI ----------

class DropList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setMinimumHeight(180)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if not e.mimeData().hasUrls():
            return super().dropEvent(e)
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.suffix.lower() == ".mp4" and p.exists():
                self.add_path(p)
        e.acceptProposedAction()

    def add_path(self, p: Path):
        # Avoid duplicates
        for i in range(self.count()):
            if self.item(i).data(Qt.UserRole) == str(p):
                return
        item = QListWidgetItem(p.name)
        item.setToolTip(str(p))
        item.setData(Qt.UserRole, str(p))
        self.addItem(item)

    def paths(self) -> List[Path]:
        out = []
        for i in range(self.count()):
            out.append(Path(self.item(i).data(Qt.UserRole)))
        return out

    def remove_selected(self):
        for i in sorted([idx.row() for idx in self.selectedIndexes()], reverse=True):
            self.takeItem(i)

    def clear_all(self):
        self.clear()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(900, 560)

        # Widgets
        self.drop = DropList()
        self.out_label = QLabel("Output folder: —")
        self.btn_pick_out = QPushButton("Pilih Folder Output…")
        self.btn_convert = QPushButton("Convert")
        self.btn_convert.setEnabled(False)
        self.btn_clear = QPushButton("Clear List")
        self.btn_remove = QPushButton("Remove Selected")
        self.chk_zip = QCheckBox("Zip hasil (otomatis)")
        self.chk_zip.setChecked(True)
        self.chk_skip = QCheckBox("Skip file yang codec-nya tidak HLS-friendly (no re-encode)")
        self.chk_skip.setChecked(True)
        self.chk_trans = QCheckBox("Jika tidak compatible, transcode otomatis (bisa kurangi kualitas)")
        self.chk_trans.setChecked(False)
        self.progress_all = QProgressBar()
        self.progress_all.setValue(0)
        self.log = QTextEdit()
        self.log.setReadOnly(True)

        # Layout
        top = QWidget()
        self.setCentralWidget(top)
        v = QVBoxLayout(top)

        hdr = QLabel("Drop file .mp4 ke area di bawah ini (multi-file didukung)")
        hdr.setStyleSheet("font-weight:600;")
        v.addWidget(hdr)
        v.addWidget(self.drop)

        row_btns = QHBoxLayout()
        row_btns.addWidget(self.btn_remove)
        row_btns.addWidget(self.btn_clear)
        row_btns.addStretch(1)
        v.addLayout(row_btns)

        v.addWidget(self._hline())

        outrow = QHBoxLayout()
        outrow.addWidget(self.out_label)
        outrow.addStretch(1)
        outrow.addWidget(self.btn_pick_out)
        v.addLayout(outrow)

        v.addWidget(self._hline())

        v.addWidget(self.chk_zip)
        v.addWidget(self.chk_skip)
        v.addWidget(self.chk_trans)

        row_seg = QHBoxLayout()
        self.segment_label = QLabel("Segment duration (detik):")
        self.segment_input = QSpinBox()
        self.segment_input.setMinimum(1)
        self.segment_input.setMaximum(60)
        self.segment_input.setValue(6)
        row_seg.addWidget(self.segment_label)
        row_seg.addWidget(self.segment_input)
        v.addLayout(row_seg)

        ctrls = QHBoxLayout()
        ctrls.addWidget(self.progress_all)
        ctrls.addWidget(self.btn_convert)
        v.addLayout(ctrls)

        v.addWidget(self._hline())
        v.addWidget(QLabel("Log:"))
        v.addWidget(self.log, 1)

        # Actions
        self.btn_pick_out.clicked.connect(self.pick_out_folder)
        self.btn_clear.clicked.connect(self.drop.clear_all)
        self.btn_remove.clicked.connect(self.drop.remove_selected)
        self.btn_convert.clicked.connect(self.start_convert)

        self.out_dir: Optional[Path] = None
        self.worker: Optional[ConverterThread] = None

        # Menu: Help → FFmpeg status
        menubar = self.menuBar()
        helpmenu = menubar.addMenu("Help")
        act = QAction("Check FFmpeg", self)
        act.triggered.connect(self.check_ffmpeg)
        helpmenu.addAction(act)

        self.update_convert_enabled()
        self.drop.model().rowsInserted.connect(self.update_convert_enabled)
        self.drop.model().rowsRemoved.connect(self.update_convert_enabled)

        last = load_last_output()
        if last:
            self.out_dir = last
            self.out_label.setText(f"Output folder: {self.out_dir}")
            self.update_convert_enabled()

    def _hline(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def check_ffmpeg(self):
        try:
            ffmpeg, ffprobe = find_ffmpeg_binaries()
            self._log(f"FFmpeg: {ffmpeg}\nFFprobe: {ffprobe}")
        except Exception as e:
            self._log(f"ERROR: {e}")
            QMessageBox.warning(self, APP_TITLE, str(e))

    def pick_out_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Pilih Folder Output")
        if path:
            self.out_dir = Path(path)
            self.out_label.setText(f"Output folder: {self.out_dir}")
            save_last_output(self.out_dir)
            self.update_convert_enabled()

    def update_convert_enabled(self):
        ok = self.drop.count() > 0 and self.out_dir is not None
        self.btn_convert.setEnabled(ok)

    def start_convert(self):
        if not self.out_dir:
            QMessageBox.information(self, APP_TITLE, "Pilih folder output dulu.")
            return
        if self.drop.count() == 0:
            QMessageBox.information(self, APP_TITLE, "Masukkan minimal 1 file MP4.")
            return

        seg_seconds = int(self.segment_input.value())
        jobs = [
            Job(
                src=Path(item.data(Qt.UserRole)),
                out_root=self.out_dir,
                skip_if_incompatible=self.chk_skip.isChecked(),
                enable_transcode_if_needed=self.chk_trans.isChecked(),
                segment_seconds=seg_seconds,
            )
            for item in [self.drop.item(i) for i in range(self.drop.count())]
        ]

        self.progress_all.setValue(0)
        self.log.clear()
        self.worker = ConverterThread(jobs)
        s = self.worker.sig
        s.log.connect(self._log)
        s.progress.connect(self.progress_all.setValue)
        s.file_progress.connect(self._on_file_progress)
        s.file_done.connect(self._on_file_done)
        s.all_done.connect(self._on_all_done)
        self.btn_convert.setEnabled(False)
        self.worker.start()

    def _on_file_progress(self, name: str, pct: int):
        self.statusBar().showMessage(f"{name}: {pct}%")

    def _on_file_done(self, name: str, status: str):
        self._log(f"{status}: {name}")

    def _on_all_done(self):
        self._log("Semua pekerjaan selesai.")
        self.btn_convert.setEnabled(True)
        self.statusBar().clearMessage()

    def _log(self, msg: str):
        self.log.append(msg)
        self.log.ensureCursorVisible()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
