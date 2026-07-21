import json
import os
import socket
import sys

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QWidget

from .daemon import SOCKET_PATH as DAEMON_SOCKET, SOCKET_FAMILY


DARK_QSS = """
    *:focus { outline: none; }
    QMessageBox { background: #2a2a2a; }
    QMessageBox QLabel { color: #ddd; }
    QScrollBar:vertical { width: 0px; background: transparent; }
    QScrollBar::handle:vertical { background: transparent; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar:horizontal { height: 0px; background: transparent; }
    QScrollBar::handle:horizontal { background: transparent; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QWidget { background: #2a2a2a; color: #ddd; font-size: 13px; }
    QLabel { color: #bbb; font-size: 12px; }
    QPushButton {
        background: #333; color: #bbb; border: 1px solid #555;
        border-radius: 3px; padding: 4px 8px; font-size: 12px;
    }
    QPushButton:hover { background: #444; }
    QPushButton:pressed { background: #555; }
    QPushButton:disabled {
        background: #181818; color: #555; border: 1px solid #333;
    }
    QToolButton {
        background: #333; color: #bbb; border: 1px solid #555;
        border-radius: 3px; padding: 0px 0px; font-size: 19px;
    }
    QToolButton:hover { background: #444; }
    QToolButton:pressed { background: #555; }
    QComboBox {
        background: #222; color: #ddd; border: 1px solid #555;
        border-radius: 3px; padding: 2px 4px; font-size: 12px;
    }
    QComboBox:disabled {
        background: #181818; color: #555; border: 1px solid #333;
    }
    QComboBox::drop-down { border: none; width: 18px; }
    QComboBox QAbstractItemView {
        background: #222; color: #ddd;
        selection-background-color: #444; border: 1px solid #555;
    }
    QSpinBox, QDoubleSpinBox {
        background: #222; color: #ddd; border: 1px solid #555;
        border-radius: 3px; padding: -2px 0px; font-size: 12px;
    }
    QSpinBox:disabled, QDoubleSpinBox:disabled {
        background: #181818; color: #555; border: 1px solid #333;
    }
    QSpinBox::up-button, QDoubleSpinBox::up-button {
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 20px; border: 1px solid #555;
        background: #333; border-radius: 3px;
        padding: 0px; font-size: 2px;
    }
    QSpinBox::down-button, QDoubleSpinBox::down-button {
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 20px; border: 1px solid #555;
        background: #333; border-radius: 3px;
        padding: 0px; font-size: 2px;
    }
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
        background: #444;
    }
    QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
    QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {
        background: #555;
    }
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow,
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
        width: 20px; height: 20px;
    }
    QLineEdit {
        background: #222; color: #ddd; border: 1px solid #555;
        border-radius: 3px; padding: 2px 4px; font-size: 12px;
    }
    QLineEdit:disabled {
        background: #181818; color: #555; border: 1px solid #333;
    }
    QSlider::groove:horizontal {
        background: #333; border-radius: 3px; height: 6px;
    }
    QSlider::handle:horizontal {
        background: #888; border: 1px solid #555;
        width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    }
    QSlider::handle:horizontal:hover { background: #aaa; }
    QSlider::handle:horizontal:pressed { background: #bbb; }
    QSlider::sub-page:horizontal {
        background: #555; border-radius: 3px;
    }
"""

SEND_CMD_TIMEOUT = 1.0


def send_cmd(cmd, **kwargs):
    payload = {"cmd": cmd}
    if kwargs:
        payload["args"] = kwargs
    sock = None
    try:
        sock = socket.socket(SOCKET_FAMILY, socket.SOCK_STREAM)
        sock.settimeout(SEND_CMD_TIMEOUT)
        sock.connect(DAEMON_SOCKET)
        sock.send(json.dumps(payload).encode())
        data = json.loads(sock.recv(65536).decode())
        return data
    except Exception:
        return None
    finally:
        if sock:
            sock.close()


class DaemonClient:
    def __init__(self, timeout=1.0):
        self._timeout = timeout
        self._sock = None

    def _ensure_connected(self):
        if self._sock is not None:
            return True
        try:
            self._sock = socket.socket(SOCKET_FAMILY, socket.SOCK_STREAM)
            self._sock.settimeout(self._timeout)
            self._sock.connect(DAEMON_SOCKET)
            return True
        except Exception:
            self._sock = None
            return False

    def send(self, cmd, **kwargs):
        payload = {"cmd": cmd}
        if kwargs:
            payload["args"] = kwargs
        if self._sock is None and not self._ensure_connected():
            return None
        try:
            self._sock.send(json.dumps(payload).encode())
            return json.loads(self._sock.recv(65536).decode())
        except Exception:
            self.close()
            if not self._ensure_connected():
                return None
            try:
                self._sock.send(json.dumps(payload).encode())
                return json.loads(self._sock.recv(65536).decode())
            except Exception:
                return None

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


def singleton_lock(socket_addr, name):
    if sys.platform != "win32":
        try:
            os.unlink(socket_addr)
        except FileNotFoundError:
            pass
    lock = socket.socket(SOCKET_FAMILY, socket.SOCK_STREAM)
    try:
        lock.bind(socket_addr)
        lock.listen(1)
    except OSError:
        print(f"{name} already running", file=sys.stderr)
        sys.exit(1)


class _NoRightClickFilter(QObject):
    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.ContextMenu:
            return True
        if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
            return True
        return super().eventFilter(obj, event)


_no_right_click_filter = _NoRightClickFilter()


def disable_right_click(w):
    w.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    w.installEventFilter(_no_right_click_filter)
    for child in w.findChildren(QWidget):
        child.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        child.installEventFilter(_no_right_click_filter)
