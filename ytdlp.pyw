import os
import sys
import json
import re
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QListWidget,
    QRadioButton, QProgressBar, QFileDialog, QMessageBox
)
from PySide6.QtCore import QThread, Signal
from yt_dlp import YoutubeDL
from PySide6.QtGui import QIcon

# ================= SETTINGS =================
APPDATA_DIR = os.path.join(os.getenv("LOCALAPPDATA"), "ytdlp_gui")
SETTINGS_FILE = os.path.join(APPDATA_DIR, "settings.json")
os.makedirs(APPDATA_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads"),
    "cookies_file": ""
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_SETTINGS.copy()

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

SETTINGS = load_settings()

# ================= HELPERS =================

def app_dir():
    if getattr(sys, "frozen", False):
        # running as exe
        return os.path.dirname(sys.executable)
    else:
        # running as script
        return os.path.dirname(os.path.abspath(__file__))


ANSI_ESCAPE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

def clean_error_message(msg: str) -> str:
    # remove ANSI colors
    msg = ANSI_ESCAPE.sub("", msg)

    # remove leading ERROR:
    msg = msg.replace("ERROR:", "").strip()

    # remove [generic], [youtube], etc.
    msg = re.sub(r"\[[^\]]+\]", "", msg)

    # collapse extra spaces
    msg = re.sub(r"\s+", " ", msg).strip()

    return msg



def sizeof_fmt(num):
    if not num:
        return None
    for unit in ["B", "KB", "MB", "GB"]:
        if num < 1024:
            return f"{num:.2f} {unit}"
        num /= 1024
    return None

def get_ydl_base_opts():
    cookies = SETTINGS.get("cookies_file")

    if not cookies or not os.path.exists(cookies):
        raise FileNotFoundError("cookies.txt not selected")

    base = app_dir()

    return {
        "quiet": True,
        "cookiefile": cookies,
        "ffmpeg_location": os.path.join(base, "ffmpeg.exe"),
        "js_runtimes": {"node": {}},
    }





def format_eta(seconds):
    if not seconds:
        return "0s"

    seconds = int(seconds)

    if seconds < 60:
        return f"{seconds}s"

    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def get_next_autonumber(folder, title):
    pattern = re.compile(rf"^{re.escape(title)} \((\d+)\)\.")
    max_n = 0

    if not os.path.exists(folder):
        return 1

    for name in os.listdir(folder):
        m = pattern.match(name)
        if m:
            max_n = max(max_n, int(m.group(1)))

    return max_n + 1

def codec_priority(vcodec):
    v = (vcodec or "").lower()
    if v.startswith("av01"): return 4
    if v.startswith("vp9") or v.startswith("vp09"): return 3
    if v.startswith("hvc1") or v.startswith("hev1"): return 2
    if v.startswith("avc1"): return 1
    return 0

def audio_codec_family(acodec):
    acodec = (acodec or "").lower()
    if acodec == "opus": return "opus"
    if acodec.startswith("mp4a"): return "aac"
    return acodec

# ================= DOWNLOAD THREAD =================
class DownloadWorker(QThread):
        progress = Signal(int, str)
        finished = Signal()
        error = Signal(str)

        def __init__(self, url, ydl_opts, download_dir):
            super().__init__()
            self.url = url
            self.ydl_opts = ydl_opts
            self.download_dir = download_dir
            self.pause_requested = False
            self.cancel_requested = False

        def cleanup_partial_files(self):
            try:
                for name in os.listdir(self.download_dir):
                    if name.endswith(".part"):
                        try:
                            os.remove(os.path.join(self.download_dir, name))
                        except:
                            pass
            except:
                pass

        def run(self):
            try:
                self.ydl_opts["progress_hooks"] = [self.hook]

                opts = self.ydl_opts.copy()
                opts.update(get_ydl_base_opts())

                with YoutubeDL(opts) as ydl:
                    ydl.download([self.url])


                if not self.cancel_requested:
                    self.finished.emit()

            except Exception as e:
                if self.cancel_requested:
                    self.cleanup_partial_files()
                else:
                    self.error.emit(str(e))



        def hook(self, d):
            # 🔴 CANCEL
            if self.cancel_requested:
                raise Exception("Cancelled")

            # ⏸ PAUSE (soft pause)
            while self.pause_requested:
                self.msleep(200)

            if d["status"] == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                speed = d.get("speed") or 0
                eta = d.get("eta") or 0

                percent = int(downloaded * 100 / total) if total else 0
                eta_text = format_eta(eta)

                text = (
                    f"{sizeof_fmt(downloaded)} / {sizeof_fmt(total)} "
                    f"@ {sizeof_fmt(speed)}/s | ETA {eta_text}"
                )

                self.progress.emit(percent, text)





# ================= GUI =================
class YTDLPGui(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Youtube Video Downloader")
        self.resize(850, 600)

        self.worker = None
        self.is_downloading = False
        self.is_paused = False
        self.last_status_text = "Idle"
        self.current_ydl_opts = None
        self.current_url = None
        self.current_title = ""

        self.was_cancelled = False

        self.allow_progress_updates = True
        self.current_basename = ""

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter YouTube URL")

        self.video_radio = QRadioButton("Video")
        self.audio_radio = QRadioButton("Audio")
        self.video_radio.setChecked(True)

        self.fetch_btn = QPushButton("Fetch formats")
        self.download_btn = QPushButton("Download")

        self.pause_btn = QPushButton("⏸")
        self.pause_btn.setFixedWidth(32)
        self.pause_btn.setEnabled(False)

        self.folder_btn = QPushButton("Change download folder")
        self.cookies_btn = QPushButton("Select cookies.txt")

        self.list_widget = QListWidget()
        from PySide6.QtGui import QFont

        mono = QFont("Consolas")  # Windows monospace
        mono.setStyleHint(QFont.Monospace)
        self.list_widget.setFont(mono)

        self.progress = QProgressBar()
        self.status = QLabel("Idle")
        status_row = QHBoxLayout()
        status_row.addWidget(self.status)
        status_row.addStretch()
        status_row.addWidget(self.pause_btn)


        top = QHBoxLayout()
        top.addWidget(self.video_radio)
        top.addWidget(self.audio_radio)
        top.addStretch()
        top.addWidget(self.cookies_btn)
        top.addWidget(self.folder_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.url_input)
        layout.addLayout(top)
        layout.addWidget(self.fetch_btn)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.progress)
        layout.addLayout(status_row)
        layout.addWidget(self.download_btn)


        self.fetch_btn.clicked.connect(self.fetch_formats)
        self.download_btn.clicked.connect(self.start_or_cancel)
        self.pause_btn.clicked.connect(self.pause_or_resume)
        self.folder_btn.clicked.connect(self.change_folder)
        self.cookies_btn.clicked.connect(self.select_cookies)


        self.info = None
        self.formats = []

    def select_cookies(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select cookies.txt",
            SETTINGS.get("cookies_file", ""),
            "Cookies (*.txt);;All files (*)"
        )

        if path:
            SETTINGS["cookies_file"] = path
            save_settings(SETTINGS)

            QMessageBox.information(
                self,
                "Cookies selected",
                "cookies.txt saved successfully."
            )


    # ================= SETTINGS =================
    def change_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Download Folder", SETTINGS["download_dir"]
        )
        if folder:
            SETTINGS["download_dir"] = folder
            save_settings(SETTINGS)

    # ================= FETCH =================
    def fetch_formats(self):
        self.list_widget.clear()
        
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(
                self,
                "Invalid URL",
                "Please enter a valid YouTube URL."
            )
            return

        try:
            with YoutubeDL(get_ydl_base_opts()) as ydl:
                self.info = ydl.extract_info(self.url_input.text(), download=False)
        except FileNotFoundError:
            QMessageBox.warning(
                self,
                "Cookies required",
                "Please select cookies.txt first."
            )
            return
        
        except Exception as e:
            msg = clean_error_message(str(e))
            low = msg.lower()

            # ❌ Random text / invalid URL
            if "not a valid url" in low:
                QMessageBox.warning(
                    self,
                    "Invalid URL",
                    "The text you entered is not a valid YouTube link.\n"
                    "Please paste a full video URL and try again."
                )

            # 🔐 Cookies / login issue
            elif "sign in" in low or "cookie" in low or "login" in low:
                QMessageBox.warning(
                    self,
                    "Cookies required",
                    "YouTube requires login.\n\n"
                    "Your cookies.txt is missing, expired, or invalid.\n"
                    "Please re-export and select cookies.txt again."
                )

            # ❗ Everything else
            else:
                QMessageBox.critical(
                    self,
                    "Error",
                    msg
                )

            return




        formats = self.info.get("formats", [])

        if self.audio_radio.isChecked():
            self.formats = [
                f for f in formats
                if f.get("vcodec") == "none"
                and f.get("abr")
                and (f.get("filesize") or f.get("filesize_approx"))
            ]

            self.formats.sort(key=lambda x: x["abr"], reverse=True)

            for i, f in enumerate(self.formats, start=1):
                size = sizeof_fmt(f.get("filesize") or f.get("filesize_approx"))
                codec = f"{f['acodec']}".ljust(9)
                abr = f"{f['abr']} kbps".ljust(12)
                ext = f"{f['ext']}".ljust(4)
                size_txt = size.rjust(7)

                self.list_widget.addItem(
                    f"{i:>2}. {codec} | {abr} | {ext} | {size_txt}"
                )


        else:
            self.formats = [
                f for f in formats
                if f.get("vcodec")
                and f.get("height") is not None
                and f["height"] >= 144
                and f.get("fps")
                and (f.get("filesize") or f.get("filesize_approx"))
            ]


            self.formats.sort(
                key=lambda f: (f["height"], f["fps"], codec_priority(f["vcodec"])),
                reverse=True
            )

            for i, f in enumerate(self.formats, start=1):
                size = sizeof_fmt(f.get("filesize") or f.get("filesize_approx"))
                res = f"{f['height']}p".ljust(5)
                fps = f"{f['fps']}fps".ljust(3)
                codec = f"{f['vcodec']}".ljust(13)
                size_txt = size.rjust(4)

                self.list_widget.addItem(
                    f"{i:>2}. {res} | {fps} | {codec} | {size_txt}"
                )


    # ================= DOWNLOAD CONTROL =================
    def start_or_cancel(self):
        if self.is_downloading:
            self.cancel_download()
            return
        
        self.current_is_audio = self.audio_radio.isChecked()

        row = self.list_widget.currentRow()
        if row < 0:
            return

        self.current_url = self.url_input.text().strip()
        with YoutubeDL({"quiet": True}) as ydl:
            real_path = ydl.prepare_filename(self.info)

        # strip extension → exact basename yt-dlp uses
        self.current_basename = os.path.splitext(real_path)[0]
        f = self.formats[row]
        title = self.info.get("title", "video")
        start_n = get_next_autonumber(SETTINGS["download_dir"], title)


        outtmpl = os.path.join(
            SETTINGS["download_dir"],
            "%(title)s (%(autonumber)d).%(ext)s"
        )

        if self.audio_radio.isChecked():
            self.current_ydl_opts = {
                "format": f["format_id"],
                "outtmpl": outtmpl,
                "quiet": True,
                "autonumber_start": start_n,
            }

        else:
            audio = max(
                (a for a in self.info["formats"] if a.get("vcodec") == "none" and a.get("abr")),
                key=lambda x: x["abr"]
            )
            self.current_ydl_opts = {
                "format": f"{f['format_id']}+{audio['format_id']}",
                "outtmpl": outtmpl,
                "merge_output_format": "mp4",
                "quiet": True,
                "autonumber_start": start_n,
                "postprocessor_args": {
                    "ffmpeg": ["-c:v", "copy", "-c:a", "aac", "-q:a", "2"]
                }
            }


        self.start_worker()

    def start_worker(self):
        # 🔧 reset cancel state for NEW download
        self.was_cancelled = False
        self.allow_progress_updates = True

        self.worker = DownloadWorker(
            self.current_url,
            self.current_ydl_opts,
            SETTINGS["download_dir"]
        )

        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.done)
        self.worker.error.connect(self.failed)
        self.worker.start()

        self.is_downloading = True
        self.is_paused = False
        self.download_btn.setText("Cancel")
        self.pause_btn.setText("⏸")
        self.pause_btn.setEnabled(True)

    def pause_or_resume(self):
        if not self.worker:
            return

        # ---- PAUSE ----
        if not self.is_paused:
            self.worker.pause_requested = True
            self.is_paused = True
            self.pause_btn.setText("▶")
            self.status.setText(self.last_status_text)
            return

        # ---- RESUME ----
        self.worker.pause_requested = False
        self.is_paused = False
        self.pause_btn.setText("⏸")



    def cancel_download(self):
        if self.worker:
            self.was_cancelled = True

            # stop pause loop if active
            self.worker.pause_requested = False

            # request cancel
            self.worker.cancel_requested = True

            # ✅ DELETE partial files immediately
            self.worker.cleanup_partial_files()

        self.allow_progress_updates = False
        self.status.setText("Cancelled")

        self.download_btn.setText("Download")
        self.pause_btn.setEnabled(False)
        self.is_downloading = False







    # ================= UI =================
    def update_progress(self, percent, text):
        if not self.allow_progress_updates:
            return
        self.last_status_text = text
        self.progress.setValue(percent)
        self.status.setText(text)


    def done(self):
        if self.was_cancelled or self.is_paused:
            return

        # 🚫 block any late yt-dlp progress hooks
        self.allow_progress_updates = False

        # ✅ force final UI text
        self.progress.setValue(100)
        self.status.setText("Download finished")

        self.reset_ui("Download finished")




    def failed(self, msg):
        if self.was_cancelled or self.is_paused:
            return

        clean_msg = clean_error_message(msg)
        low = clean_msg.lower()


        if "sign in" in low or "cookie" in low or "login" in low:
            QMessageBox.warning(
                self,
                "Cookies required",
                "Download failed because YouTube requires login.\n\n"
                "Your cookies.txt may be missing, expired, or invalid.\n"
                "Please re-export and select cookies.txt again."
            )
            self.reset_ui("Cookies required")
        else:
            QMessageBox.critical(
                self,
                "Download error",
                clean_msg
            )
            self.reset_ui("Error")



        


    def reset_ui(self, text):
        self.worker = None
        self.is_downloading = False
        self.is_paused = False
        self.was_cancelled = False

        self.download_btn.setText("Download")
        self.pause_btn.setText("⏸")
        self.pause_btn.setEnabled(False)

        self.progress.setValue(0)
        self.status.setText(text)   # ← now safe



# ================= MAIN =================
if __name__ == "__main__":
    app = QApplication(sys.argv)

    icon_path = os.path.join(app_dir(), "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))   # 🔥 THIS LINE WAS MISSING

    win = YTDLPGui()
    win.show()
    sys.exit(app.exec())


