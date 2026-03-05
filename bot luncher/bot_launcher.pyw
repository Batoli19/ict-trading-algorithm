"""
ICT Trading Bot desktop launcher.
"""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, QRectF
from PyQt5.QtGui import (
    QColor,
    QIcon,
    QPixmap,
    QPainter,
    QFont,
    QLinearGradient,
    QPen,
    QPainterPath,
)
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QMenu,
    QSplashScreen,
    QSystemTrayIcon,
)


class TradingBotLauncher:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.bot_process = None
        self._dashboard_wait_start = None
        self._dashboard_poll_timer = None
        self._start_error = ""
        self.repo_root = Path(__file__).resolve().parent
        self.bot_dir = self.repo_root / "python"
        self.config_path = self.repo_root / "config" / "settings.json"
        self.api_host, self.api_port = self._load_api_target()
        self.dashboard_url = f"http://{self.api_host}:{self.api_port}"
        self.local_dashboard_url = (
            f"http://{'127.0.0.1' if self.api_host in ('0.0.0.0', '::') else self.api_host}:{self.api_port}"
        )

        self.setup_splash_screen()
        self.setup_system_tray()

    def _load_api_target(self):
        host = "127.0.0.1"
        port = 5000
        try:
            if self.config_path.exists():
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
                api_cfg = cfg.get("api", {})
                host = str(api_cfg.get("host", host)).strip() or host
                port = int(api_cfg.get("port", port))
        except Exception:
            host, port = "127.0.0.1", 5000
        return host, port

    def setup_splash_screen(self):
        """Create a premium splash screen (rendered pixmap)."""
        self.splash_w = 700
        self.splash_h = 440

        # Track progress for a premium "loading" feel
        self._splash_progress = 0.08

        # Load icon from your folder
        self.splash_icon_path = self.repo_root / "bot icons" / "bot algo.png"
        self._splash_icon_pix = QPixmap(str(self.splash_icon_path)) if self.splash_icon_path.exists() else QPixmap()

        # Create splash using a pixmap we render
        self.splash = QSplashScreen()
        self.splash.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.splash.setFixedSize(self.splash_w, self.splash_h)

        # Initial paint
        self._render_splash("Initializing...", progress=self._splash_progress)

        # Center on screen
        screen = self.app.primaryScreen()
        if screen is not None:
            rect = screen.availableGeometry()
            x = rect.x() + (rect.width() - self.splash_w) // 2
            y = rect.y() + (rect.height() - self.splash_h) // 2
            self.splash.move(x, y)

    def update_splash_content(self, status_text, progress=None, detail_text=None):
        """Update splash visuals with premium styling + progress bar."""
        if progress is not None:
            self._splash_progress = max(0.0, min(1.0, float(progress)))
        else:
            # Nudge progress forward subtly each stage
            self._splash_progress = max(self._splash_progress, min(0.95, self._splash_progress + 0.12))

        self._render_splash(status_text, progress=self._splash_progress, detail_text=detail_text)
        QApplication.processEvents()

    def _render_splash(self, status_text, progress=0.1, detail_text=None):
        """Paint a premium, scalable splash screen into a pixmap and apply to QSplashScreen."""
        w, h = self.splash_w, self.splash_h
        px = QPixmap(w, h)
        px.setDevicePixelRatio(self.app.devicePixelRatio())
        px.fill(Qt.transparent)

        # Wall Street scheme
        BG = QColor("#0a1929")          # Dark navy
        PANEL = QColor("#0f2438")       # Slightly lighter panel
        PANEL2 = QColor("#0b1c2c")      # Depth layer
        BORDER = QColor(255, 255, 255, 18)
        TEXT = QColor("#d4d4d4")        # Silver chrome text
        MUTED = QColor("#9aa7b4")
        GOLD = QColor("#FFD700")
        GREEN = QColor("#228B22")

        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        # Background gradient (clean, no glow)
        bg_grad = QLinearGradient(0, 0, 0, h)
        bg_grad.setColorAt(0.0, BG)
        bg_grad.setColorAt(1.0, QColor("#07121d"))
        p.fillRect(0, 0, w, h, bg_grad)

        # Main rounded panel (glass-like but subtle)
        panel_rect = QRectF(26, 22, w - 52, h - 44)
        panel_path = QPainterPath()
        panel_path.addRoundedRect(panel_rect, 22, 22)

        panel_grad = QLinearGradient(panel_rect.left(), panel_rect.top(), panel_rect.right(), panel_rect.bottom())
        panel_grad.setColorAt(0.0, PANEL)
        panel_grad.setColorAt(1.0, PANEL2)
        p.fillPath(panel_path, panel_grad)

        # Border
        pen = QPen(BORDER)
        pen.setWidthF(1.2)
        p.setPen(pen)
        p.drawPath(panel_path)

        # Top accent line (gold -> green)
        accent = QLinearGradient(panel_rect.left(), panel_rect.top(), panel_rect.right(), panel_rect.top())
        accent.setColorAt(0.0, GOLD)
        accent.setColorAt(1.0, GREEN)
        p.setPen(QPen(accent, 2.2))
        p.drawLine(int(panel_rect.left() + 22), int(panel_rect.top() + 22), int(panel_rect.right() - 22), int(panel_rect.top() + 22))

        # Icon badge
        badge_x, badge_y = 64, 92
        badge_size = 112
        badge_rect = QRectF(badge_x, badge_y, badge_size, badge_size)

        badge_path = QPainterPath()
        badge_path.addRoundedRect(badge_rect, 28, 28)

        badge_grad = QLinearGradient(badge_rect.left(), badge_rect.top(), badge_rect.right(), badge_rect.bottom())
        badge_grad.setColorAt(0.0, QColor(255, 255, 255, 28))
        badge_grad.setColorAt(1.0, QColor(0, 0, 0, 28))
        p.fillPath(badge_path, badge_grad)

        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.drawPath(badge_path)

        # Place icon centered inside badge (clip)
        if not self._splash_icon_pix.isNull():
            clip = QPainterPath()
            clip.addRoundedRect(badge_rect.adjusted(10, 10, -10, -10), 20, 20)
            p.save()
            p.setClipPath(clip)
            icon_target = badge_rect.adjusted(14, 14, -14, -14)
            p.drawPixmap(icon_target.toRect(), self._splash_icon_pix)
            p.restore()
        else:
            # Fallback simple mark
            p.setPen(QPen(GOLD, 2))
            p.setFont(QFont("Segoe UI", 28, QFont.Bold))
            p.drawText(badge_rect.toRect(), Qt.AlignCenter, "ICT")

        # Title block
        title_x = 200
        title_y = 92

        # Title
        p.setPen(TEXT)
        title_font = QFont("Segoe UI", 28, QFont.Black)
        title_font.setLetterSpacing(QFont.PercentageSpacing, 102)
        p.setFont(title_font)
        p.drawText(title_x, title_y + 28, "ICT TRADING BOT")

        # Subtitle
        p.setPen(MUTED)
        sub_font = QFont("Segoe UI", 12, QFont.Medium)
        p.setFont(sub_font)
        p.drawText(title_x, title_y + 56, "Desktop Launcher • Automated Execution • Dashboard Ready")

        # Divider
        p.setPen(QPen(QColor(255, 255, 255, 16), 1.0))
        p.drawLine(64, 230, w - 64, 230)

        # Status pill (sharp, high contrast)
        pill_rect = QRectF(64, 258, w - 128, 56)
        pill_path = QPainterPath()
        pill_path.addRoundedRect(pill_rect, 18, 18)

        pill_grad = QLinearGradient(pill_rect.left(), pill_rect.top(), pill_rect.right(), pill_rect.bottom())
        pill_grad.setColorAt(0.0, QColor(255, 255, 255, 18))
        pill_grad.setColorAt(1.0, QColor(0, 0, 0, 20))
        p.fillPath(pill_path, pill_grad)

        p.setPen(QPen(QColor(255, 255, 255, 18), 1.0))
        p.drawPath(pill_path)

        # Status text
        p.setPen(QColor("#e6edf3"))
        p.setFont(QFont("Segoe UI", 13, QFont.DemiBold))
        p.drawText(pill_rect.adjusted(18, 10, -18, -10).toRect(), Qt.AlignVCenter | Qt.AlignLeft, status_text)

        # Optional detail text (small)
        if detail_text:
            p.setPen(MUTED)
            p.setFont(QFont("Segoe UI", 10, QFont.Normal))
            p.drawText(pill_rect.adjusted(18, 30, -18, -10).toRect(), Qt.AlignLeft | Qt.AlignVCenter, detail_text)

        # Progress bar (gold to green)
        bar_rect = QRectF(64, 330, w - 128, 10)
        bar_bg = QPainterPath()
        bar_bg.addRoundedRect(bar_rect, 5, 5)
        p.fillPath(bar_bg, QColor(255, 255, 255, 14))

        fill_w = max(10.0, (bar_rect.width()) * max(0.02, min(1.0, progress)))
        fill_rect = QRectF(bar_rect.left(), bar_rect.top(), fill_w, bar_rect.height())
        fill_path = QPainterPath()
        fill_path.addRoundedRect(fill_rect, 5, 5)

        fill_grad = QLinearGradient(fill_rect.left(), fill_rect.top(), fill_rect.right(), fill_rect.top())
        fill_grad.setColorAt(0.0, GOLD)
        fill_grad.setColorAt(1.0, GREEN)
        p.fillPath(fill_path, fill_grad)

        # Footer
        p.setPen(QColor(255, 255, 255, 110))
        p.setFont(QFont("Segoe UI", 9, QFont.Medium))
        p.drawText(64, h - 48, "v2.0 • Prop Firm Ready • Adaptive Learning • MT5 Connected")

        p.setPen(QColor(255, 255, 255, 70))
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(w - 260, h - 48, "© ICT Trading Bot • Command Center")

        p.end()

        self.splash.setPixmap(px)

    def setup_system_tray(self):
        """Create system tray icon."""
        self.tray = QSystemTrayIcon()

        icon_path = self.repo_root / "bot_icon.ico"
        if icon_path.exists():
            self.tray.setIcon(QIcon(str(icon_path)))
        else:
            self.tray.setIcon(self.app.style().standardIcon(
                self.app.style().SP_ComputerIcon
            ))

        self.tray.setToolTip("ICT Trading Bot")

        menu = QMenu()

        dashboard_action = QAction("Open Dashboard", None)
        dashboard_action.triggered.connect(self.open_dashboard)
        menu.addAction(dashboard_action)

        menu.addSeparator()

        self.status_action = QAction("Status: Starting...", None)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)

        menu.addSeparator()

        quit_action = QAction("Quit Bot", None)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_clicked)

    def on_tray_clicked(self, reason):
        """Handle tray icon click."""
        if reason == QSystemTrayIcon.DoubleClick:
            self.open_dashboard()

    def start_bot_process(self):
        """Start the bot in background (hidden)."""
        try:
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                creation_flags = subprocess.CREATE_NO_WINDOW
            else:
                startupinfo = None
                creation_flags = 0

            self.bot_process = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=str(self.bot_dir),
                startupinfo=startupinfo,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            return True
        except Exception as e:
            self._start_error = str(e)
            return False

    def _is_dashboard_ready(self):
        host_to_probe = "127.0.0.1" if self.api_host in ("0.0.0.0", "::") else self.api_host
        try:
            with socket.create_connection((host_to_probe, self.api_port), timeout=1.0):
                return True
        except OSError:
            return False

    def wait_for_dashboard(self, timeout_seconds=30):
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            if self.bot_process and self.bot_process.poll() is not None:
                return False
            if self._is_dashboard_ready():
                return True
            time.sleep(0.5)
        return False

    def open_dashboard(self):
        """Open dashboard in browser."""
        webbrowser.open(self.local_dashboard_url)

    def request_graceful_shutdown(self):
        """Ask API server to shutdown engine before process terminate."""
        try:
            req = urllib.request.Request(
                f"{self.local_dashboard_url}/api/shutdown",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2):
                return True
        except (urllib.error.URLError, TimeoutError):
            return False

    def _start_stage_engine(self):
        self.update_splash_content("Booting execution environment...", progress=0.18, detail_text="Initializing runtime + config")
        QTimer.singleShot(900, self._start_stage_launch_bot)

    def _start_stage_launch_bot(self):
        self.update_splash_content("Starting trading engine...", progress=0.36, detail_text="Launching core process")
        success = self.start_bot_process()
        if not success:
            detail = f"Startup error: {self._start_error}" if self._start_error else "Startup error"
            self.update_splash_content("Failed to start bot", progress=1.0, detail_text=detail)
            QTimer.singleShot(2500, self._fail_and_quit)
            return

        self.update_splash_content("Connecting to MetaTrader 5...", progress=0.52, detail_text="Preparing broker bridge")
        QTimer.singleShot(1200, self._start_stage_wait_dashboard)

    def _start_stage_wait_dashboard(self):
        self.update_splash_content("Starting dashboard server...", progress=0.68, detail_text="Waiting for API to become available")
        self._dashboard_wait_start = time.monotonic()
        self._dashboard_poll_timer = QTimer(self.app)
        self._dashboard_poll_timer.timeout.connect(self._poll_dashboard_readiness)
        self._dashboard_poll_timer.start(500)

    def _poll_dashboard_readiness(self):
        if self.bot_process and self.bot_process.poll() is not None:
            self._dashboard_poll_timer.stop()
            self._finish_startup(False)
            return

        if self._is_dashboard_ready():
            self._dashboard_poll_timer.stop()
            self._finish_startup(True)
            return

        if self._dashboard_wait_start is not None and (time.monotonic() - self._dashboard_wait_start) >= 20:
            self._dashboard_poll_timer.stop()
            self._finish_startup(False)

    def _finish_startup(self, ready):
        if ready:
            self.update_splash_content("Loading dashboard...", progress=0.92, detail_text="Finalizing UI + endpoints")
            self.open_dashboard()
            QTimer.singleShot(500, self._show_running_state)
            return

        self.update_splash_content("Bot started • Dashboard still warming up...", progress=0.86, detail_text="You can open the dashboard from the tray icon")
        QTimer.singleShot(1500, self._show_running_state)

    def _show_running_state(self):
        self.tray.setVisible(True)
        self.status_action.setText("Status: Running")
        self.tray.showMessage(
            "ICT Trading Bot",
            "Bot is running! Double-click icon to open dashboard.",
            QSystemTrayIcon.Information,
            3000,
        )
        self.splash.close()

    def _fail_and_quit(self):
        self.splash.close()
        self.quit_app()

    def quit_app(self):
        """Clean shutdown."""
        if self.bot_process:
            try:
                self.request_graceful_shutdown()
                self.bot_process.wait(timeout=5)
            except Exception:
                pass

            if self.bot_process.poll() is None:
                try:
                    self.bot_process.terminate()
                    self.bot_process.wait(timeout=5)
                except Exception:
                    try:
                        self.bot_process.kill()
                    except Exception:
                        pass

        self.tray.hide()
        QApplication.quit()

    def run(self):
        """Run the launcher."""
        self.splash.show()
        QApplication.processEvents()
        QTimer.singleShot(500, self._start_stage_engine)
        sys.exit(self.app.exec_())


if __name__ == "__main__":
    launcher = TradingBotLauncher()
    launcher.run()
