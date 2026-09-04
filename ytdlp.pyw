import os
import sys
import json
import re
import requests
import tempfile
import threading
import time
# --- Force yt-dlp-ejs to be imported so it is registered ---
try:
    import yt_dlp_ejs  # noqa: F401
except ImportError:
    yt_dlp_ejs = None

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QListWidget,
    QRadioButton, QProgressBar, QFileDialog, QMessageBox,
    QTextEdit, QStyledItemDelegate, QStyleOptionViewItem, QStyle,
    QDialog
)
from PySide6.QtCore import QThread, Signal, Qt, QObject, QPoint, QTimer
from yt_dlp import YoutubeDL
from PySide6.QtGui import QIcon
CURRENT_VERSION = "1.3"
UPDATE_URL = "https://api.github.com/repos/Lol2546/Youtube-Video-Downloader/releases/latest"

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

LOG_MESSAGES = []
LOG_WINDOW = None
LOG_TEXT = None

class LogEmitter(QObject):
    message = Signal(str)

LOG_EMITTER = LogEmitter()

def add_log(text):
    if not text:
        return

    text = str(text).rstrip()

    # Show in VS Code
    print(text)

    # Save for Log window
    LOG_MESSAGES.append(text)

    # Safely send to GUI
    LOG_EMITTER.message.emit(text)


class LogYoutubeDL(YoutubeDL):

    def to_screen(self, message, *args, **kwargs):
        add_log(message)

    def to_stderr(self, message, *args, **kwargs):
        add_log(message)
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
    opts = {
        "quiet": False,
        "cookiefile": cookies,
        "ffmpeg_location": os.path.join(base, "ffmpeg.exe"),
    }

    # 🔹 Optional portable Node.js support
    node_path = os.path.join(base, "node.exe")
    if os.path.exists(node_path):
        opts["js_runtimes"] = {
            "node": {
                "path": node_path
            }
        }
        
    return opts


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
    max_n = -1
    if not os.path.exists(folder):
        return 0

    for name in os.listdir(folder):
        if name.endswith(".part"):
            name = name[:-5]

        base, _ = os.path.splitext(name)

        if base == title:
            max_n = max(max_n, 0)
            continue

        m = re.fullmatch(rf"{re.escape(title)} \((\d+)\)", base)
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

def get_best_audio_format(formats):
    audio_formats = [
        a for a in formats
        if a.get("vcodec") == "none"
        and a.get("abr")
    ]

    if not audio_formats:
        return None

    # Never select DRC audio when a normal version exists
    normal_audio = [
        a for a in audio_formats
        if "-drc" not in a.get("format_id", "")
    ]

    if normal_audio:
        audio_formats = normal_audio

    opus_formats = [
        a for a in audio_formats
        if "opus" in (a.get("acodec") or "").lower()
    ]

    if opus_formats:
        return max(opus_formats, key=lambda x: x.get("abr", 0))

    return max(audio_formats, key=lambda x: x.get("abr", 0))


def format_sample_rate(rate):
    if not rate:
        return "Unknown"

    if rate % 1000 == 0:
        return f"{rate // 1000} kHz"

    return f"{rate / 1000:.1f} kHz"

# ================= DOWNLOAD THREAD =================
class DownloadWorker(QThread):
    progress = Signal(int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(
        self,
        url,
        ydl_opts,
        download_dir,
        info,
        is_audio,
        video_format_id=None,
        audio_format_id=None,
        video_total=0,
        audio_total=0
    ):
        super().__init__()
        self.url = url
        self.ydl_opts = ydl_opts
        self.download_dir = download_dir
        self.info = info
        self.is_audio = is_audio

        self.video_format_id = video_format_id
        self.audio_format_id = audio_format_id

        self.video_total = video_total or 0
        self.audio_total = audio_total or 0

        self.video_downloaded = 0
        self.audio_downloaded = 0

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
            opts["quiet"] = False

            add_log("WORKER STARTED")
            add_log(f"FORMAT: {opts.get('format')}")

            with LogYoutubeDL(opts) as ydl:
                add_log("STARTING DOWNLOAD")
                result = ydl.download([self.url])
                add_log(f"DOWNLOAD RESULT: {result}")

            if not self.cancel_requested:
                self.finished.emit()

        except Exception as e:
            add_log(f"DOWNLOAD ERROR: {repr(e)}")

            if self.cancel_requested:
                self.cleanup_partial_files()
            else:
                self.error.emit(str(e))

    def hook(self, d):

        # 🔴 CANCEL
        if self.cancel_requested:
            raise Exception("Cancelled")

        # ⏸ PAUSE
        while self.pause_requested:
            self.msleep(200)

        if d["status"] not in ("downloading", "finished"):
            return

        # ---------------------------------------------------------
        # VIDEO MODE: COMBINED VIDEO + AUDIO PROGRESS
        # ---------------------------------------------------------
        if not self.is_audio and self.video_format_id and self.audio_format_id:

            info_dict = d.get("info_dict") or {}

            format_id = str(
                info_dict.get("format_id")
                or d.get("format_id")
                or ""
            )

            downloaded = d.get("downloaded_bytes", 0) or 0
            total = (
                d.get("total_bytes")
                or d.get("total_bytes_estimate")
                or 0
            )

            # Update video progress
            if format_id == str(self.video_format_id):

                self.video_downloaded = downloaded

                if total:
                    self.video_total = total

                if d["status"] == "finished":
                    self.video_downloaded = self.video_total

            # Update audio progress
            elif format_id == str(self.audio_format_id):

                self.audio_downloaded = downloaded

                if total:
                    self.audio_total = total

                if d["status"] == "finished":
                    self.audio_downloaded = self.audio_total

            else:
                return

            # Combined total
            combined_downloaded = (
                self.video_downloaded +
                self.audio_downloaded
            )

            combined_total = (
                self.video_total +
                self.audio_total
            )

            if combined_total:
                percent = int(
                    combined_downloaded * 100 / combined_total
                )
                percent = min(percent, 100)
            else:
                percent = 0

            # Current download speed
            speed = d.get("speed") or 0

            # Combined ETA
            if speed and combined_total:
                remaining = combined_total - combined_downloaded
                eta = int(remaining / speed)
            else:
                eta = 0

            text = (
                f"{sizeof_fmt(combined_downloaded)} / "
                f"{sizeof_fmt(combined_total)} "
                f"@ {sizeof_fmt(speed)}/s | "
                f"ETA {format_eta(eta)}"
            )

            self.progress.emit(percent, text)

            return

        # ---------------------------------------------------------
        # AUDIO MODE: EXISTING BEHAVIOR
        # ---------------------------------------------------------
        if d["status"] == "downloading":

            downloaded = d.get("downloaded_bytes", 0)
            total = (
                d.get("total_bytes")
                or d.get("total_bytes_estimate")
            )

            speed = d.get("speed") or 0
            eta = d.get("eta") or 0

            percent = (
                int(downloaded * 100 / total)
                if total
                else 0
            )

            eta_text = format_eta(eta)

            text = (
                f"{sizeof_fmt(downloaded)} / "
                f"{sizeof_fmt(total)} "
                f"@ {sizeof_fmt(speed)}/s | "
                f"ETA {eta_text}"
            )

            self.progress.emit(percent, text)


class NoHoverHeaderDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.data(Qt.ItemDataRole.UserRole) == "header":
            option = QStyleOptionViewItem(option)
            option.state &= ~QStyle.StateFlag.State_MouseOver

        super().paint(painter, option, index)

class FetchFormatsWorker(QThread):
    finished = Signal(object)
    error = Signal(Exception)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            with LogYoutubeDL(get_ydl_base_opts()) as ydl:
                info = ydl.extract_info(self.url, download=False)
            self.finished.emit(info)
        except Exception as e:
            self.error.emit(e)

# ================= GUI =================
class YTDLPGui(QWidget):
    update_ui_signal = Signal(int, float, str)
    update_finished_signal = Signal()
    update_error_signal = Signal(str)
    def __init__(self):
        super().__init__()
        self.update_ui_signal.connect(self.update_download_ui)
        self.update_finished_signal.connect(self.show_update_finished)
        self.update_error_signal.connect(self.show_update_error)
        self.setWindowTitle("Youtube Video Downloader")
        self.resize(850, 600)

        self.worker = None
        self.is_downloading = False
        self.is_paused = False
        self.last_status_text = "Paused"
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
        self.log_btn = QPushButton("Log")
        self.log_btn.setFixedWidth(45)
        self.update_btn = QPushButton("Check for Updates")
        self.update_btn.setFixedWidth(120)

        self.list_widget = QListWidget()
        self.list_widget.setMouseTracking(True)
        self.list_widget.setItemDelegate(
            NoHoverHeaderDelegate(self.list_widget)
        )
        from PySide6.QtGui import QFont

        mono = QFont("Consolas")  # Windows monospace
        mono.setStyleHint(QFont.Monospace)
        self.list_widget.setFont(mono)

        self.progress = QProgressBar()
        self.status = QLabel("")
        status_row = QHBoxLayout()
        status_row.addWidget(self.status)
        status_row.addStretch()
        status_row.addWidget(self.pause_btn)


        top = QHBoxLayout()
        top.addWidget(self.video_radio)
        top.addWidget(self.audio_radio)
        top.addStretch()
        top.addWidget(self.update_btn)
        top.addWidget(self.log_btn)
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
        self.log_btn.clicked.connect(self.show_log)
        self.update_btn.clicked.connect(self.check_for_updates)


        self.info = None
        self.formats = []
        self.fetch_worker = None

    def check_for_updates(self):
        try:
            response = requests.get(UPDATE_URL, timeout=10)
            response.raise_for_status()

            release_data = response.json()
            latest_version = release_data["tag_name"].lstrip("v")

            if tuple(map(int, latest_version.split("."))) > tuple(map(int, CURRENT_VERSION.split("."))):
                self.show_update_prompt(latest_version, release_data)
            else:
                msg = QDialog(self)
                msg.setWindowTitle("No Update Found")
                msg.setFixedSize(200, 110)

                layout = QVBoxLayout(msg)

                line1 = QLabel("Your program is up to date")
                line1.setAlignment(Qt.AlignmentFlag.AlignCenter)

                line2 = QLabel(f"Current version: v{CURRENT_VERSION}")
                line2.setAlignment(Qt.AlignmentFlag.AlignCenter)

                ok_button = QPushButton("OK")
                ok_button.setFixedWidth(80)
                ok_button.clicked.connect(msg.accept)

                layout.addStretch()
                layout.addWidget(line1)
                layout.addWidget(line2)
                layout.addSpacing(15)
                layout.addWidget(
                    ok_button,
                    alignment=Qt.AlignmentFlag.AlignCenter
                )
                layout.addStretch()

                msg.exec()

        except Exception as e:
            QMessageBox.critical(
                self,
                "Update Error",
                f"Could not check for updates.\n\n{e}"
            )


    def show_update_prompt(self, latest_version, release_data):
        size = 0

        for asset in release_data.get("assets", []):
            if asset["name"].lower().endswith(".exe"):
                size = asset["size"]
                break

        size_mb = size // (1024 * 1024)

        answer = QMessageBox.question(
            self,
            "Update Available",
            f"A new version (v{latest_version}) is available!\n\n"
            f"Current version: v{CURRENT_VERSION}\n"
            f"Update size: {size_mb} MB\n\n"
            "Do you want to download and install it?",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No
        )

        if answer == QMessageBox.StandardButton.Yes:
            self.download_and_run_installer(release_data)

    def download_and_run_installer(self, release_data):
        try:
            installer_url = None
            total_size = 0

            for asset in release_data.get("assets", []):
                if asset["name"].lower().endswith(".exe"):
                    installer_url = asset["browser_download_url"]
                    total_size = asset.get("size", 0)
                    break

            if not installer_url:
                QMessageBox.critical(
                    self,
                    "Update Error",
                    "No installer (.exe) was found in the latest release."
                )
                return

            installer_path = os.path.join(
                tempfile.gettempdir(),
                f"YoutubeDownloaderUpdate_{int(time.time())}.exe"
            )

            update_dialog = QDialog(self)
            update_dialog.setWindowTitle("Downloading Update")
            update_dialog.setFixedSize(450, 180)
            update_dialog.setModal(False)

            layout = QVBoxLayout(update_dialog)

            update_label = QLabel("Downloading update...")
            layout.addWidget(update_label)

            update_progress = QProgressBar()
            update_progress.setRange(0, 100)
            layout.addWidget(update_progress)

            update_speed_label = QLabel("Speed: Calculating...")
            layout.addWidget(update_speed_label)

            update_eta_label = QLabel("ETA: Calculating...")
            layout.addWidget(update_eta_label)

            update_dialog.show()
            update_dialog.raise_()
            update_dialog.activateWindow()

            self.update_dialog = update_dialog
            self.update_progress = update_progress
            self.update_speed_label = update_speed_label
            self.update_eta_label = update_eta_label
            self.update_installer_path = installer_path
            self.update_total_size = total_size

            threading.Thread(
                target=self.download_update_thread,
                args=(installer_url,),
                daemon=True
            ).start()

        except Exception as e:
            QMessageBox.critical(
                self,
                "Update Error",
                f"Failed to start the update.\n\n{e}"
            )

    def download_update_thread(self, installer_url):
        try:
            last_update_time = time.time()
            last_downloaded = 0
            downloaded = 0

            response = requests.get(
                installer_url,
                stream=True,
                timeout=30
            )
            response.raise_for_status()

            total_size = self.update_total_size

            with open(self.update_installer_path, "wb") as file:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        file.write(chunk)
                        downloaded += len(chunk)

                        now = time.time()

                        if now - last_update_time >= 0.2 or (
                            total_size and downloaded >= total_size
                        ):
                            interval = now - last_update_time

                            speed = (
                                (downloaded - last_downloaded) / interval
                                if interval > 0
                                else 0
                            )

                            if total_size:
                                percent = int(
                                    downloaded * 100 / total_size
                                )
                                percent = min(percent, 100)
                            else:
                                percent = 0

                            if speed > 0 and total_size:
                                remaining = total_size - downloaded
                                eta_seconds = int(
                                    max(0, remaining) / speed
                                )

                                minutes, seconds = divmod(
                                    eta_seconds, 60
                                )

                                if minutes:
                                    eta_text = f"{minutes}m {seconds}s"
                                else:
                                    eta_text = f"{seconds}s"
                            else:
                                eta_text = "Calculating..."

                            self.update_ui_signal.emit(
                                percent,
                                speed,
                                eta_text
                            )

                            last_update_time = now
                            last_downloaded = downloaded

            self.update_ui_signal.emit(100, 0, "0s")
            self.update_finished_signal.emit()

        except Exception as e:
            self.update_error_signal.emit(str(e))

    def root_after_download_finished(self):
        self.update_finished_signal.emit()

    def root_after_download_error(self, error):
        self.update_error_signal.emit(error)

    def show_update_finished(self):
        self.update_progress.setValue(100)
        self.update_speed_label.setText("Speed: Complete")
        self.update_eta_label.setText("ETA: 0s")
        self.update_dialog.close()

        answer = QMessageBox.information(
            self,
            "Update Downloaded",
            "The update has been downloaded.\n\n"
            "Click OK to start the installer.",
            QMessageBox.StandardButton.Ok
        )

        if answer == QMessageBox.StandardButton.Ok:
            if not os.path.exists(self.update_installer_path):
                QMessageBox.critical(
                    self,
                    "Installer Error",
                    "The update finished downloading, but the installer file was not found.\n\n"
                    f"Expected location:\n{self.update_installer_path}"
                )
                return

            try:
                installer_path = os.path.abspath(self.update_installer_path)

                # Start the installer through Windows Shell.
                # This makes it independent from the downloader GUI.
                os.startfile(installer_path)

            except Exception as e:
                QMessageBox.critical(
                    self,
                    "Installer Error",
                    "The update was downloaded, but the installer could not be started.\n\n"
                    f"{e}\n\n"
                    f"Installer location:\n{self.update_installer_path}"
                )

    def show_update_error(self, error):
        if hasattr(self, "update_dialog"):
            self.update_dialog.close()

        QMessageBox.critical(
            self,
            "Update Error",
            f"Failed to download the update.\n\n{error}"
        )


    def update_download_ui(self, percent, speed, eta):
        self.update_progress.setValue(percent)

        if speed > 0:
            self.update_speed_label.setText(
                f"Speed: {sizeof_fmt(speed)}/s"
            )
        else:
            self.update_speed_label.setText(
                "Speed: Complete"
            )

        self.update_eta_label.setText(
            f"ETA: {eta}"
        )

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

    def show_log(self):
        global LOG_WINDOW, LOG_TEXT

        if LOG_WINDOW is not None:
            LOG_WINDOW.show()
            LOG_WINDOW.raise_()
            LOG_WINDOW.activateWindow()
            return

        LOG_WINDOW = QWidget()
        LOG_WINDOW.setWindowTitle("Log")
        LOG_WINDOW.resize(self.width(), self.height())

        LOG_TEXT = QTextEdit()
        LOG_TEXT.setReadOnly(True)
        LOG_TEXT.setPlainText("\n".join(LOG_MESSAGES))
        LOG_EMITTER.message.connect(LOG_TEXT.append)

        layout = QVBoxLayout(LOG_WINDOW)
        layout.addWidget(LOG_TEXT)

        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.center().x() - LOG_WINDOW.width() // 2
        y = screen.center().y() - LOG_WINDOW.height() // 2
        LOG_WINDOW.move(x, y)

        LOG_WINDOW.destroyed.connect(self.close_log)

        LOG_WINDOW.show()
        LOG_WINDOW.raise_()
        LOG_WINDOW.activateWindow()

    def close_log(self):
        global LOG_WINDOW, LOG_TEXT
        LOG_WINDOW = None
        LOG_TEXT = None


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
        # Reset old download status when fetching new formats
        self.progress.setValue(0)
        self.status.setText("")

        url = self.url_input.text().strip()

        if "youtube.com/watch" in url and "v=" in url:
            url = url.split("&")[0]

        if not url:
            QMessageBox.warning(
                self,
                "Invalid URL",
                "Please enter a valid YouTube URL."
            )
            return

        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching...")

        self.fetch_worker = FetchFormatsWorker(url)
        self.fetch_worker.finished.connect(self.fetch_formats_finished)
        self.fetch_worker.error.connect(self.fetch_formats_error)
        self.fetch_worker.start()
        
    def fetch_formats_finished(self, info):
        self.info = info
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch formats")

        formats = self.info.get("formats", [])

        if self.audio_radio.isChecked():
            AUDIO_CODEC_WIDTH = 8
            AUDIO_BITRATE_WIDTH = 14
            AUDIO_EXTENSION_WIDTH = 9
            AUDIO_SIZE_WIDTH = 10

            self.list_widget.addItem(
                f"{'':4}| "
                f"{'Codec':^{AUDIO_CODEC_WIDTH}} | "
                f"{'Bitrate':^{AUDIO_BITRATE_WIDTH}} | "
                f"{'Extension':^{AUDIO_EXTENSION_WIDTH}} | "
                f"{'Size':^{AUDIO_SIZE_WIDTH}} |"
            )

            header_item = self.list_widget.item(0)
            header_item.setFlags(
                header_item.flags() & ~Qt.ItemIsSelectable
            )
            header_item.setData(
                Qt.ItemDataRole.UserRole,
                "header"
            )

            self.formats = [
                f for f in formats
                if f.get("vcodec") == "none"
                and f.get("abr")
                and "-drc" not in f.get("format_id", "")
                and (
                    f.get("filesize")
                    or f.get("filesize_approx")
                )
            ]

            best_formats = {}

            for f in self.formats:
                codec = audio_codec_family(f.get("acodec"))

                if (
                    codec not in best_formats
                    or f["abr"] > best_formats[codec]["abr"]
                ):
                    best_formats[codec] = f

            self.formats = list(best_formats.values())

            self.formats.sort(
                key=lambda x: x["abr"],
                reverse=True
            )

            mp3_format = {
                "format_id": "MP3",
                "abr": 320,
                "acodec": "mp3",
                "ext": "mp3",
                "filesize": None,
                "is_mp3": True
            }

            self.formats.append(mp3_format)

            wav_format = {
                "format_id": "WAV",
                "abr": None,
                "acodec": "pcm",
                "ext": "wav",
                "filesize": None,
                "is_wav": True
            }

            self.formats.append(wav_format)

            for i, f in enumerate(self.formats, start=1):

                if f.get("is_mp3"):
                    source_audio = max(
                        (
                            a for a in self.info["formats"]
                            if a.get("vcodec") == "none"
                            and a.get("abr")
                            and "-drc" not in a.get("format_id", "")
                        ),
                        key=lambda x: x["abr"]
                    )

                    source_size = (
                        source_audio.get("filesize")
                        or source_audio.get("filesize_approx")
                    )

                    if source_size:
                        duration = self.info.get("duration")

                        if duration:
                            mp3_size = (
                                (320 * 1000 / 8) * duration
                            )
                            size_txt = "~" + sizeof_fmt(mp3_size)
                        else:
                            size_txt = "Unknown"
                    else:
                        size_txt = "Unknown"

                elif f.get("is_wav"):
                    source_audio = get_best_audio_format(
                        self.info["formats"]
                    )

                    if source_audio:
                        sample_rate = source_audio.get("asr")
                        bit_depth = 16

                        f["sample_rate"] = sample_rate
                        f["bit_depth"] = bit_depth

                        duration = self.info.get("duration")

                        if duration and sample_rate:
                            wav_size = (
                                sample_rate
                                * (bit_depth / 8)
                                * 2
                                * duration
                            )
                            size_txt = "~" + sizeof_fmt(wav_size)
                        else:
                            size_txt = "Unknown"
                    else:
                        f["sample_rate"] = None
                        f["bit_depth"] = None
                        size_txt = "Unknown"

                else:
                    size = sizeof_fmt(
                        f.get("filesize")
                        or f.get("filesize_approx")
                    )
                    size_txt = size

                if f.get("is_wav"):
                    codec = "WAV".ljust(AUDIO_CODEC_WIDTH)

                    abr = (
                        f"{format_sample_rate(f.get('sample_rate'))}, "
                        f"{f.get('bit_depth')} bit"
                    ).ljust(AUDIO_BITRATE_WIDTH)

                else:
                    if f["acodec"].startswith("mp4a"):
                        codec = "mp4a/AAC".ljust(
                            AUDIO_CODEC_WIDTH
                        )
                    else:
                        codec = f["acodec"].upper().ljust(
                            AUDIO_CODEC_WIDTH
                        )

                    abr = f"{f['abr']} kbps".ljust(
                        AUDIO_BITRATE_WIDTH
                    )

                ext = f["ext"].ljust(
                    AUDIO_EXTENSION_WIDTH
                )

                size_txt = size_txt.strip().rjust(
                    AUDIO_SIZE_WIDTH
                )

                self.list_widget.addItem(
                    f"{i:>2}. | {codec} | {abr} | "
                    f"{ext} | {size_txt} |"
                )

        else:
            video_by_resolution_codec = {}

            for f in formats:
                if (
                    f.get("vcodec")
                    and f.get("height") is not None
                    and f["height"] >= 144
                    and f.get("fps")
                    and (
                        f.get("filesize")
                        or f.get("filesize_approx")
                    )
                ):
                    vcodec = f["vcodec"].lower()

                    if vcodec.startswith("av01"):
                        codec_family = "av1"
                    elif (
                        vcodec.startswith("vp09")
                        or vcodec.startswith("vp9")
                    ):
                        codec_family = "vp9"
                    elif (
                        vcodec.startswith("hvc1")
                        or vcodec.startswith("hev1")
                    ):
                        codec_family = "h.265/hevc"
                    elif vcodec.startswith("avc1"):
                        codec_family = "h.264"
                    else:
                        codec_family = vcodec

                    key = (
                        f["height"],
                        codec_family
                    )

                    current = video_by_resolution_codec.get(key)

                    if (
                        current is None
                        or f["fps"] > current["fps"]
                        or (
                            f["fps"] == current["fps"]
                            and (
                                f.get("filesize")
                                or f.get("filesize_approx")
                            ) > (
                                current.get("filesize")
                                or current.get("filesize_approx")
                            )
                        )
                    ):
                        video_by_resolution_codec[key] = f

            video_candidates = list(
                video_by_resolution_codec.values()
            )

            video_candidates.sort(
                key=lambda f: (
                    f["height"],
                    f["fps"],
                    codec_priority(f["vcodec"])
                ),
                reverse=True
            )

            self.formats = video_candidates

            for i, f in enumerate(
                self.formats,
                start=1
            ):
                video_size = (
                    f.get("filesize")
                    or f.get("filesize_approx")
                    or 0
                )

                aac_audio_formats = [
                    a for a in self.info["formats"]
                    if a.get("vcodec") == "none"
                    and a.get("abr")
                    and audio_codec_family(a.get("acodec")) == "aac"
                    and (
                        a.get("filesize")
                        or a.get("filesize_approx")
                    )
                    and "-drc" not in a.get("format_id", "")
                ]

                if aac_audio_formats:
                    best_aac_audio = max(
                        aac_audio_formats,
                        key=lambda x: x["abr"]
                    )

                    audio_size = (
                        best_aac_audio.get("filesize")
                        or best_aac_audio.get("filesize_approx")
                        or 0
                    )
                else:
                    audio_size = 0

                combined_size = video_size + audio_size

                size = sizeof_fmt(combined_size)

                res = f"{f['height']}p".ljust(5)
                fps = f"{f['fps']}fps".ljust(5)

                vcodec = (
                    f.get("vcodec") or ""
                ).lower()

                if vcodec.startswith("av01"):
                    codec = "av1"
                elif (
                    vcodec.startswith("vp09")
                    or vcodec.startswith("vp9")
                ):
                    codec = "vp9"
                elif (
                    vcodec.startswith("hvc1")
                    or vcodec.startswith("hev1")
                ):
                    codec = "h.265/hevc"
                elif vcodec.startswith("avc1"):
                    codec = "h.264"
                else:
                    codec = f["vcodec"]

                container = "mp4".ljust(3)
                size_txt = (
                    size or "Unknown"
                ).ljust(10)

                self.list_widget.addItem(
                    f"{i:>2}. {res} | {fps} | "
                    f"{codec:<5} | {container} | {size_txt}"
                )

        self.fetch_worker = None

    def fetch_formats_error(self, error):
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch formats")
        self.fetch_worker = None

        if isinstance(error, FileNotFoundError):
            QMessageBox.warning(
                self,
                "Cookies required",
                "Please select cookies.txt first."
            )
            return

        msg = clean_error_message(str(error))
        low = msg.lower()

        if "not a valid url" in low:
            QMessageBox.warning(
                self,
                "Invalid URL",
                "The text you entered is not a valid YouTube link.\n"
                "Please paste a full video URL and try again."
            )

        elif (
            "sign in" in low
            or "cookie" in low
            or "login" in low
        ):
            QMessageBox.warning(
                self,
                "Cookies required",
                "YouTube requires login.\n\n"
                "Your cookies.txt is missing, expired, or invalid.\n"
                "Please re-export and select cookies.txt again."
            )

        else:
            QMessageBox.critical(
                self,
                "Error",
                msg
            )


    # ================= DOWNLOAD CONTROL =================
    def start_or_cancel(self):
        if self.is_downloading:
            self.cancel_download()
            return
        
        self.current_is_audio = self.audio_radio.isChecked()

        row = self.list_widget.currentRow()

        # Audio header occupies row 0
        if self.audio_radio.isChecked():
            if row <= 0:
                return
            row -= 1
        else:
            if row < 0:
                return

        self.current_url = self.url_input.text().strip()
        # Remove YouTube playlist/mix parameters from a video URL
        if "youtube.com/watch" in self.current_url and "v=" in self.current_url:
            self.current_url = self.current_url.split("&")[0]
        with YoutubeDL({"quiet": True}) as ydl:
            real_path = ydl.prepare_filename(self.info)

        # strip extension → exact basename yt-dlp uses
        self.current_basename = os.path.splitext(real_path)[0]
        f = self.formats[row]
        title = self.info.get("title", "video")
        start_n = get_next_autonumber(SETTINGS["download_dir"], title)


        if start_n == 0:
            filename_template = f"{title}.%(ext)s"
        else:
            filename_template = f"{title} ({start_n}).%(ext)s"

        outtmpl = os.path.join(
            SETTINGS["download_dir"],
            filename_template
        )

        if self.audio_radio.isChecked():
            if f.get("is_mp3"):
                # Download the best available opus audio, then convert it to MP3
                audio = get_best_audio_format(self.info["formats"])

                add_log(f"MP3 SOURCE FORMAT: {audio['format_id']}")
                add_log(f"MP3 SOURCE INFO: {audio}")

                self.current_ydl_opts = {
                    "format": audio["format_id"],
                    "outtmpl": outtmpl,
                    "quiet": True,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "320",
                        }
                    ],
                }

            elif f.get("is_wav"):
                # Download the best available opus audio, then convert it to lossless WAV
                audio = get_best_audio_format(self.info["formats"])

                add_log(f"WAV SOURCE FORMAT: {audio['format_id']}")
                add_log(f"WAV SOURCE INFO: {audio}")

                self.current_ydl_opts = {
                    "format": audio["format_id"],
                    "outtmpl": outtmpl,
                    "quiet": True,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "wav",
                        }
                    ],
                    "postprocessor_args": {
                        "ffmpeg": ["-c:a", "pcm_s16le"]
                    },
                }

            else:
                add_log(f"SELECTED AUDIO FORMAT ID: {f['format_id']}")
                add_log(f"SELECTED AUDIO FORMAT INFO: {f}")
                # Existing Audio behavior - unchanged
                self.current_ydl_opts = {
                    "format": f["format_id"],
                    "outtmpl": outtmpl,
                    "quiet": True,
                }

        else:
            audio = max(
                (
                    a for a in self.info["formats"]
                    if a.get("vcodec") == "none"
                    and a.get("abr")
                    and audio_codec_family(a.get("acodec")) == "aac"
                    and (a.get("filesize") or a.get("filesize_approx"))
                    and "-drc" not in a.get("format_id", "")
                ),
                key=lambda x: x["abr"]
            )

            add_log(f"SELECTED FORMAT: {f['format_id']}")
            add_log(f"SELECTED FORMAT INFO: {f}")
            add_log(f"AUDIO FORMAT: {audio['format_id']}")

            video_format_id = f["format_id"]
            audio_format_id = audio["format_id"]

            video_total = (
                f.get("filesize")
                or f.get("filesize_approx")
                or 0
            )

            audio_total = (
                audio.get("filesize")
                or audio.get("filesize_approx")
                or 0
            )

            combined_total = video_total + audio_total

            add_log(f"VIDEO SIZE: {sizeof_fmt(video_total)}")
            add_log(f"AUDIO SIZE: {sizeof_fmt(audio_total)}")
            add_log(f"COMBINED SIZE: {sizeof_fmt(combined_total)}")

            self.current_ydl_opts = {
                "format": f"{video_format_id}+{audio_format_id}",
                "outtmpl": outtmpl,
                "merge_output_format": "mp4",
                "quiet": True,
                "postprocessor_args": {
                    "ffmpeg": ["-c:v", "copy", "-c:a", "copy"]
                }
            }

        self.status.setText("Downloading...")
        if self.audio_radio.isChecked():
            self.start_worker()
        else:
            self.start_worker(
                video_format_id,
                audio_format_id,
                video_total,
                audio_total
            )
        

    def start_worker(
    self,
    video_format_id=None,
    audio_format_id=None,
    video_total=0,
    audio_total=0
    ):
        # 🔧 reset cancel state for NEW download
        self.was_cancelled = False
        self.allow_progress_updates = True

        self.worker = DownloadWorker(
            self.current_url,
            self.current_ydl_opts,
            SETTINGS["download_dir"],
            self.info,
            self.current_is_audio,
            video_format_id,
            audio_format_id,
            video_total,
            audio_total
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
        self.status.setText("Downloading...")


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
        app.setWindowIcon(QIcon(icon_path))

    win = YTDLPGui()
    win.show()
    sys.exit(app.exec())


        