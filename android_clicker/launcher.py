import json
import os
import socket
import subprocess
import sys
import time

try:
    from PyQt6.QtCore import QTimer, Qt
    from PyQt6.QtWidgets import (
        QApplication, QComboBox, QFrame, QHBoxLayout, QLabel,
        QLineEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
    )
    HAS_PYQT6 = True
except ImportError:
    HAS_PYQT6 = False

from .config import load_config, parse_value, save_config, list_modeconfigs

from .daemon import LAUNCHER_SOCKET, SOCKET_FAMILY
from .widget_utils import DARK_QSS, DaemonClient, disable_right_click, send_cmd, singleton_lock


def _get_nested(d, path, default=None):
    for p in path.split("."):
        if isinstance(d, dict):
            d = d.get(p, {})
        else:
            return default
    return d if not isinstance(d, dict) or d else default


def _set_nested(d, path, value):
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


DAEMON_READY_TIMEOUT = 5.0


def _status_color(running):
    return "#ddd" if running else "#666"


class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._daemon_proc = None
        self._overlay_running = False
        self._mode_list = []
        self._daemon_starting = False
        self._daemon_start_time = 0.0
        self._drag_pos = None
        self._dc = DaemonClient(timeout=1.0)

        self._init_ui()
        self._update_overlay(False)
        self._init_config_btn()
        self._apply_styles()
        self._update_all()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(100)

        cfg = load_config()
        if cfg.get("launcher", {}).get("auto_start_daemon", False):
            if not self._dc.send("ping"):
                self._start_daemon()

    def _init_ui(self):
        self.setWindowTitle("android-clicker-launcher")
        self.setMinimumSize(240, 170)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        daemon_row = QHBoxLayout()
        daemon_row.setSpacing(6)
        self._daemon_dot = QLabel("\u25cf")
        self._daemon_dot.setFixedWidth(14)
        daemon_row.addWidget(self._daemon_dot)
        daemon_row.addWidget(QLabel("Daemon"))
        daemon_row.addStretch()
        self._daemon_status = QLabel("Stopped")
        self._daemon_status.setFixedWidth(56)
        daemon_row.addWidget(self._daemon_status)
        self._daemon_btn = QPushButton("Start")
        self._daemon_btn.setFixedWidth(60)
        self._daemon_btn.clicked.connect(self._toggle_daemon)
        daemon_row.addWidget(self._daemon_btn)
        layout.addLayout(daemon_row)

        overlay_row = QHBoxLayout()
        overlay_row.setSpacing(6)
        self._overlay_dot = QLabel("\u25cf")
        self._overlay_dot.setFixedWidth(14)
        overlay_row.addWidget(self._overlay_dot)
        overlay_row.addWidget(QLabel("Overlay"))
        overlay_row.addStretch()
        self._overlay_status = QLabel("Stopped")
        self._overlay_status.setMinimumWidth(56)
        overlay_row.addWidget(self._overlay_status)
        self._overlay_btn = QPushButton("Start")
        self._overlay_btn.setFixedWidth(60)
        self._overlay_btn.clicked.connect(self._toggle_overlay)
        overlay_row.addWidget(self._overlay_btn)
        layout.addLayout(overlay_row)

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(6)
        toggle_row.addWidget(QLabel("Clicking:"))
        self._active_lbl = QLabel("INACTIVE")
        self._active_lbl.setFixedWidth(80)
        toggle_row.addWidget(self._active_lbl)
        toggle_row.addStretch()
        self._toggle_btn = QPushButton("Toggle")
        self._toggle_btn.setFixedWidth(70)
        self._toggle_btn.clicked.connect(self._toggle_clicking)
        toggle_row.addWidget(self._toggle_btn)
        layout.addLayout(toggle_row)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        mode_row.addWidget(QLabel("Mode:"))
        self._mode_combo = QComboBox()
        disable_right_click(self._mode_combo)
        self._mode_combo.currentTextChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo, stretch=1)
        layout.addLayout(mode_row)

        self._host_lbl = QLabel("Host: (\u2014, \u2014)")
        layout.addWidget(self._host_lbl)
        self._android_lbl = QLabel("Android: (\u2014, \u2014)")
        layout.addWidget(self._android_lbl)

        layout.addStretch()

    def _init_config_btn(self):
        self._config_btn = QPushButton("\u2630", self)
        self._config_btn.setFixedSize(22, 22)
        self._config_btn.setStyleSheet("""
            QPushButton {
                background: #333; color: #bbb; border: 1px solid #555;
                border-radius: 3px; padding: 0px; font-size: 12px;
            }
            QPushButton:hover { background: #444; }
            QPushButton:pressed { background: #555; }
        """)
        self._config_btn.clicked.connect(self._open_config_editor)
        self._reposition_config_btn()

        try:
            import tomlkit  # noqa: F401
        except ImportError:
            self._config_btn.setEnabled(False)

    def _reposition_config_btn(self):
        if hasattr(self, '_config_btn'):
            self._config_btn.setGeometry(self.width() - 30, self.height() - 30, 24, 24)

    def resizeEvent(self, event):
        self._reposition_config_btn()
        super().resizeEvent(event)

    def _apply_styles(self):
        self.setStyleSheet(DARK_QSS)

    def _update_daemon(self, running):
        self._daemon_dot.setStyleSheet(f"color: {_status_color(running)};")
        self._daemon_status.setText("Running" if running else "Stopped")
        external = running and self._daemon_proc is None
        self._daemon_btn.setText("Stop" if running else "Start")
        self._daemon_btn.setEnabled(not self._daemon_starting)
        if not running:
            self._mode_combo.setEnabled(False)
            self._update_overlay(False)
            self._toggle_btn.setEnabled(False)
            self._active_lbl.setStyleSheet("font-weight: bold; color: #555;")
            self._overlay_btn.setEnabled(False)

    def _update_overlay(self, running):
        self._overlay_dot.setStyleSheet(f"color: {_status_color(running)};")
        self._overlay_status.setText("Running" if running else "Stopped")
        self._overlay_btn.setText("Stop" if running else "Start")
        self._overlay_btn.setEnabled(True)
        self._overlay_running = running

    def _populate_modes(self, mode=None):
        disk_modes = sorted(m for m in list_modeconfigs() if ".template" not in m)
        if disk_modes != self._mode_list:
            self._mode_list = list(disk_modes)
            current = self._mode_combo.currentText()
            self._mode_combo.blockSignals(True)
            self._mode_combo.clear()
            for m in disk_modes:
                self._mode_combo.addItem(m)
            if current and current in self._mode_list:
                target = current
            elif mode and mode in self._mode_list:
                target = mode
            else:
                target = load_config().get("mode", "follow") or "follow"
                if target not in self._mode_list:
                    target = disk_modes[0] if disk_modes else ""
            if target:
                self._mode_combo.setCurrentText(target)
            self._mode_combo.blockSignals(False)

    def _update_from_status(self, data):
        self._update_daemon(True)

        active = data.get("active", False)
        mode = data.get("mode", "?")

        self._active_lbl.setText("ACTIVE" if active else "INACTIVE")
        self._active_lbl.setStyleSheet(
            "font-weight: bold; color: #ddd;" if active
            else "font-weight: bold; color: #888;"
        )

        self._populate_modes(mode=mode)
        self._mode_combo.setEnabled(True)
        self._toggle_btn.setEnabled(True)

        if self._mode_combo.currentText() != mode:
            self._mode_combo.blockSignals(True)
            idx = self._mode_combo.findText(mode)
            if idx >= 0:
                self._mode_combo.setCurrentIndex(idx)
            self._mode_combo.blockSignals(False)

        host = data.get("host", {})
        android = data.get("android")
        hx, hy = host.get("x", "\u2014"), host.get("y", "\u2014")
        self._host_lbl.setText(f"Host: ({hx}, {hy})")
        if android:
            ax, ay = android.get("x", "\u2014"), android.get("y", "\u2014")
            self._android_lbl.setText(f"Android: ({ax}, {ay})")
        else:
            self._android_lbl.setText("Android: (\u2014, \u2014)")

        os_ = data.get("overlay_state", {})
        if "overlay_running" in os_:
            self._update_overlay(os_["overlay_running"])

    def _poll(self):
        now = time.monotonic()
        if self._daemon_starting:
            if self._dc.send("ping"):
                self._daemon_starting = False
                self._update_daemon(True)
                self._daemon_btn.setEnabled(True)
                return
            if now - self._daemon_start_time > DAEMON_READY_TIMEOUT:
                self._daemon_starting = False
                if self._daemon_proc and self._daemon_proc.poll() is None:
                    self._daemon_proc.terminate()
                    try:
                        self._daemon_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._daemon_proc.kill()
                self._daemon_proc = None
                self._daemon_dot.setStyleSheet("color: #e55;")
                self._daemon_status.setText("Failed")
                self._daemon_btn.setText("Start")
                self._daemon_btn.setEnabled(True)
            return

        resp = self._dc.send("cursor_pos")
        if resp and resp.get("ok"):
            self._update_from_status(resp["data"])
        else:
            self._update_daemon(False)

    def _toggle_daemon(self):
        if self._dc.send("ping"):
            self._stop_daemon()
        else:
            self._start_daemon()

    def _start_daemon(self):
        self._daemon_proc = subprocess.Popen(
            [sys.executable, "-m", "android_clicker.cli", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._daemon_starting = True
        self._daemon_start_time = time.monotonic()
        self._daemon_btn.setEnabled(False)
        self._daemon_status.setText("Starting...")
        self._daemon_dot.setStyleSheet("color: #ddd;")

    def _stop_daemon(self):
        send_cmd("stop")
        if self._daemon_proc is not None:
            try:
                self._daemon_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._daemon_proc.terminate()
                try:
                    self._daemon_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._daemon_proc.kill()
            self._daemon_proc = None
        self._update_daemon(False)

    def _toggle_overlay(self):
        if not self._dc.send("ping"):
            self._overlay_status.setText("Need daemon")
            return
        if self._overlay_running:
            send_cmd("overlay", action="stop")
            self._update_overlay(False)
        else:
            resp = send_cmd("overlay", action="start")
            if resp and resp.get("ok"):
                self._update_overlay(True)

    def _toggle_clicking(self):
        resp = send_cmd("toggle")
        if resp and resp.get("ok"):
            on_ = resp.get("message") == "on"
            self._active_lbl.setText("ACTIVE" if on_ else "INACTIVE")
            self._active_lbl.setStyleSheet(
                "font-weight: bold; color: #ddd;" if on_
                else "font-weight: bold; color: #888;"
            )

    def _on_mode_changed(self, name):
        if name and self._dc.send("ping"):
            send_cmd("mode", mode=name)

    def _update_all(self):
        self._populate_modes()
        resp = self._dc.send("cursor_pos")
        if resp and resp.get("ok"):
            self._update_from_status(resp["data"])
            return
        self._update_daemon(False)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
            event.accept()

    def closeEvent(self, event):
        self._timer.stop()
        self._dc.close()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def _open_config_editor(self):
        if hasattr(self, '_config_window') and self._config_window.isVisible():
            self._config_window.raise_()
            return
        self._config_window = ConfigEditorWindow()
        self._config_window.show()


_CONFIG_DISPLAY_NAMES = {
    "auto_start_daemon": "autostart daemon",
    "active":            "active",
    "mode":              "mode",
    "android_width":     "android width",
    "android_height":    "android height",
    "host_width":        "host width",
    "host_height":       "host height",
    "enabled":           "enabled",
    "notify_backend":    "notify backend",
    "connect":           "connect",
    "connect_timeout":   "connect timeout",
    "screen_cap_timeout": "screencap timeout",
    "exe_path":            "ADB exe path",
    "toggle_clicking":   "toggle clicking",
    "launcher":          "launcher",
    "overlay":           "overlay",
    "overlay_toggle":    "overlay toggle",
    "burst_clicks_plus": "burst clicks +",
    "burst_clicks_minus": "burst clicks -",
    "uinput":              "uinput",
    "target_app":          "target app",
}


class ConfigEditorWindow(QWidget):
    _FIELDS = [
        ("Launcher", [
            ("auto_start_daemon", "launcher.auto_start_daemon", "combo", ["false", "true"]),
        ]),
        ("General", [
            ("active", "active", "combo", ["false", "true"]),
            ("mode", "mode", "combo_mode", None),
        ]),
        ("Input", [
            ("uinput", "uinput", "combo", ["false", "true"]),
        ]),
        ("Display", [
            ("android_width", "display.android_width", "text", None),
            ("android_height", "display.android_height", "text", None),
            ("host_width", "display.host_width", "text", None),
            ("host_height", "display.host_height", "text", None),
            ("target_app", "display.target_app", "text", None),
        ]),
        ("Notifications", [
            ("enabled", "notifications.enabled", "combo", ["false", "true"]),
            ("notify_backend", "notifications.notify_backend", "combo", ["hyprctl", "libnotify"]),
        ]),
        ("ADB", [
            ("connect", "adb.connect", "text", None),
            ("connect_timeout", "adb.connect_timeout", "text", None),
            ("screen_cap_timeout", "adb.screen_cap_timeout", "text", None),
            ("exe_path", "adb.exe_path", "text", None),
        ]),
        ("Hotkeys", [
            ("toggle_clicking", "hotkeys.toggle_clicking", "text", None),
            ("launcher", "hotkeys.launcher", "text", None),
            ("overlay", "hotkeys.overlay", "text", None),
            ("overlay_toggle", "hotkeys.overlay_toggle", "text", None),
            ("burst_clicks_plus", "hotkeys.burst_clicks_plus", "text", None),
            ("burst_clicks_minus", "hotkeys.burst_clicks_minus", "text", None),
        ]),
    ]

    def __init__(self):
        super().__init__()
        self._drag_pos = None
        self.setWindowTitle("android-clicker config")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(500, 800)
        self.setStyleSheet(DARK_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        form = QVBoxLayout(content)
        form.setSpacing(4)
        form.setContentsMargins(0, 0, 0, 0)

        self._widgets = {}  # path → widget
        mode_names = sorted(list_modeconfigs())

        for section_name, fields in self._FIELDS:
            hl = QLabel(section_name)
            hl.setStyleSheet("font-weight: bold; color: #ddd; font-size: 13px; margin-top: 6px;")
            form.addWidget(hl)

            for label, path, wtype, wargs in fields:
                row = QHBoxLayout()
                row.setSpacing(10)
                lbl = QLabel(_CONFIG_DISPLAY_NAMES.get(label, label))
                lbl.setFixedWidth(100)
                row.addWidget(lbl)

                if wtype == "combo":
                    w = QComboBox()
                    if sys.platform != "linux" and path in ("notifications.notify_backend", "uinput"):
                        w.addItem("Linux-only setting")
                        w.setEnabled(False)
                    else:
                        for item in wargs:
                            w.addItem(item)
                    disable_right_click(w)
                elif wtype == "combo_mode":
                    w = QComboBox()
                    for m in mode_names:
                        w.addItem(m)
                    if "follow" not in mode_names:
                        w.addItem("follow")
                    disable_right_click(w)
                elif wtype == "spin":
                    lo, hi = wargs
                    w = QSpinBox()
                    w.setRange(lo, hi)
                    disable_right_click(w)
                elif wtype == "text":
                    w = QLineEdit()
                    if sys.platform != "win32" and path == "adb.exe_path":
                        w.setPlaceholderText("Windows-only setting")
                        w.setEnabled(False)
                    else:
                        ph = {
                            "display.android_width": "e.g. 1920  (empty = auto-detect)",
                            "display.android_height": "e.g. 1080  (empty = auto-detect)",
                            "display.host_width": "e.g. 1920  (empty = auto-detect)",
                            "display.host_height": "e.g. 1080  (empty = auto-detect)",
                            "display.target_app": "e.g. Waydroid (empty = auto-detect)",
                            "adb.connect_timeout": "e.g. 5  (empty = default)",
                            "adb.screen_cap_timeout": "e.g. 15  (1-15s, empty = default)",
                            "adb.connect": "e.g. 192.168.240.112:5555  (empty = auto-detect)",
                            "hotkeys.toggle_clicking": "e.g. alt+space  (empty = disabled)",
                            "hotkeys.launcher": "e.g. ctrl+alt+l  (empty = disabled)",
                            "hotkeys.overlay": "e.g. meta+o  (empty = disabled)",
                            "hotkeys.overlay_toggle": "e.g. shift+h  (empty = disabled)",
                            "hotkeys.burst_clicks_plus": "e.g. ctrl+up  (empty = disabled)",
                            "hotkeys.burst_clicks_minus": "e.g. ctrl+down  (empty = disabled)",
                        }.get(path, "")
                        if ph:
                            w.setPlaceholderText(ph)
                    disable_right_click(w)
                row.addWidget(w, stretch=1)
                form.addLayout(row)
                self._widgets[path] = w

        form.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, stretch=1)

        self._save_btn = QPushButton("Save && Restart Daemon")
        self._save_btn.clicked.connect(self._save)
        layout.addWidget(self._save_btn)

        self._load_data()
        self._connect_dependencies()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
            event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def _load_data(self):
        data = load_config()
        for path, w in self._widgets.items():
            val = _get_nested(data, path)
            if isinstance(w, QComboBox):
                s = str(val).lower() if val else ""
                idx = w.findText(s)
                if idx >= 0:
                    w.setCurrentIndex(idx)
            elif isinstance(w, QSpinBox):
                w.setValue(int(val) if val else 0)
            elif isinstance(w, QLineEdit):
                w.setText(str(val) if val else "")

    def _connect_dependencies(self):
        enabled = self._widgets.get("notifications.enabled")
        backend = self._widgets.get("notifications.notify_backend")
        if sys.platform != "linux":
            if backend:
                backend.setEnabled(False)
        elif enabled and backend:
            def _sync(enabled_text):
                backend.setEnabled(enabled_text == "true")

            enabled.currentTextChanged.connect(_sync)
            _sync(enabled.currentText())

    def _save(self):
        data = {}
        for path, w in self._widgets.items():
            if isinstance(w, QComboBox):
                if w.count() == 0:
                    continue
                if sys.platform != "linux" and path in ("notifications.notify_backend", "uinput"):
                    continue
                text = w.currentText()
                if w.count() == 2 and w.itemText(0) in ("false", "true"):
                    v = text == "true"
                else:
                    v = text
            elif isinstance(w, QSpinBox):
                v = w.value()
            elif isinstance(w, QLineEdit):
                v = w.text().strip()
                if not v:
                    _set_nested(data, path, None)
                    continue
                v = parse_value(v)
            _set_nested(data, path, v)
        save_config(data)
        if send_cmd("ping"):
            send_cmd("stop")
            for _ in range(20):
                if not os.path.exists("/tmp/android-clicker.sock"):
                    break
                time.sleep(0.1)
            subprocess.Popen(
                [sys.executable, "-m", "android_clicker.cli", "start"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.close()


def main():
    if not HAS_PYQT6:
        print("error: PyQt6 is required for launcher (pip install PyQt6)", file=sys.stderr)
        sys.exit(1)

    singleton_lock(LAUNCHER_SOCKET, "launcher")

    app = QApplication(sys.argv)
    w = LauncherWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
