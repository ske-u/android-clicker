import json
import os
import signal
import socket
import sys

try:
    from PyQt6.QtCore import QEvent, QMimeData, QPoint, QSocketNotifier, QTimer, Qt
    from PyQt6.QtGui import QColor, QCursor, QDrag, QIcon, QPainter, QPen
    from PyQt6.QtWidgets import (
        QApplication, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
        QLineEdit, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox,
        QToolButton, QVBoxLayout, QWidget,
    )
    HAS_PYQT6 = True
except ImportError:
    HAS_PYQT6 = False

from .config import (
    parse_value,
)
from .daemon import SOCKET_PATH as DAEMON_SOCKET
from .injectors import METHODS
from .widget_utils import DARK_QSS, disable_right_click, send_cmd, singleton_lock


OVERLAY_SOCKET = "/tmp/android-clicker-overlay.sock"

RESIZE_MARGIN = 4
MIN_WIDTH = 120
MAX_WIDTH = 1000





COMBO_KEYS = {
    "method": METHODS,
    "action": ["click", "click_cursor", "wait", "screencap_check", "notify", "log", "run_mode", "run", "zoom"],
    "cycle_mode": ["clicks", "delay"],
}

TOOLTIPS = {
    "action": "Type of sequence step",
    "x": "X coordinate in Android space",
    "y": "Y coordinate in Android space",
    "clicks": "Number of times to fire this click",
    "repeat_scalar": "Restart sequence when finished",
    "interval": "ms between each click in a repeat burst",
    "jitter_px": "px random offset on click x/y",
    "jitter_ms": "ms random offset on interval",
    "wait_jitter": "ms random offset on wait duration (\u00b1)",
    "default_wait_ms": "ms delay before each sequence step",
    "method": "Injection backend",
    "zoom": "Enable uinput zoom gesture",
    "ms": "Duration in milliseconds",
    "message": "Notification or log message text",
    "mode": "Target mode for run_mode",
    "autohide": "Auto-hide sidebar while capturing",
    "select_shows_sidebar": "Show sidebar when clicking a dot on the overlay",
    "duration_ms": "How long to run the target mode (ms)",
    "cmd": "Shell command to execute",
    "timeout_ms": "Command timeout in ms",
    "start": "Initial spread as % of window (5-95)",
    "end": "Final spread as % of window (5-95)",
    "colour": "Hex colour to match (e.g. 32343B)",
    "tol": "Colour tolerance (0-255)",
    "then": "Step index to jump to on match",
    "else": "Step index on no match (-1 = next)",
    "w": "Region width in Android pixels",
    "h": "Region height in Android pixels",
    "clicks": "Number of taps at this point",
    "cycle": "Cycle through points",
    "cycle_mode": "How to cycle through points (clicks or time)",
    "cycle_clicks": "Clicks per point before cycling",
    "jitter_clicks": "Random offset on cycle_clicks",
    "cycle_delay": "ms before advancing to next point",
    "jitter_timer": "Random offset on reset_timer (ms)",
    "reset_timer": "ms before cycling back to first point",
    "duration": "Duration of zoom gesture in ms",
}


class _HelpPopup(QWidget):
    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet("background: #181818;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(300)
        self._label.setStyleSheet("color: #e8e8e8; font-size: 13px;")
        layout.addWidget(self._label)
        self.adjustSize()
        self._anchor = None
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check_cursor)

    def show_help(self, text, anchor):
        self._label.setText(text)
        fm = self._label.fontMetrics()
        tw = fm.horizontalAdvance(text)
        self._label.setFixedWidth(min(tw + 10, 300))
        self._anchor = anchor
        self.adjustSize()
        pos = QCursor.pos()
        self.move(pos.x() + 15, pos.y() + 5)
        screen = QApplication.primaryScreen().availableGeometry()
        if self.geometry().right() > screen.right():
            self.move(screen.right() - self.width(), self.y())
        if self.geometry().bottom() > screen.bottom():
            self.move(self.x(), screen.bottom() - self.height())
        self.show()
        self._timer.start()

    def _check_cursor(self):
        if not self._anchor:
            return
        cursor = QCursor.pos()
        ag = self._anchor.geometry().translated(self._anchor.mapToGlobal(QPoint(0, 0)))
        combined = ag.united(self.geometry()).adjusted(-20, -20, 20, 20)
        if not combined.contains(cursor):
            self.hide()
            self._timer.stop()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)


_help_popup = None


def _add_button_help(button, key):
    class _Filter(QWidget):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
                _show_help_popup(TOOLTIPS.get(key, ""), obj)
                event.accept()
                return True
            return super().eventFilter(obj, event)
    button.installEventFilter(_Filter(button))


def _show_help_popup(text, anchor):
    global _help_popup
    if _help_popup is None:
        _help_popup = _HelpPopup()
    _help_popup.show_help(text, anchor)


def _add_label_help(label, key):
    orig = label.mousePressEvent
    def handler(event, _key=key, _label=label):
        if event.button() == Qt.MouseButton.RightButton:
            _show_help_popup(TOOLTIPS.get(_key, ""), _label)
            event.accept()
        elif orig:
            orig(event)
    label.mousePressEvent = handler


DISPLAY_NAMES = {
    "click_cursor": "cursor click",
    "jitter_clicks": "jitter clicks",
    "screencap_check": "colour check",
    "run_mode": "run mode",
    "screen_cap": "colour",
    "jitter_ms": "jitter ms",
    "jitter_px": "jitter px",
    "default_wait_ms": "wait",
    "wait_jitter": "wait jitter",
    "duration_ms": "duration ms",
    "timeout_ms": "timeout ms",
}


def _make_key_label(key, width, tooltip_key=None):
    display = DISPLAY_NAMES.get(key, key)
    kl = QLabel(f"{display}:")
    kl.setFixedWidth(width)
    _add_label_help(kl, tooltip_key or key)
    return kl


def _widget_for(key, val):
    if isinstance(val, bool):
        w = QComboBox()
        for text in ("false", "true"):
            w.addItem(text, text)
        w.setCurrentText("true" if val else "false")
        disable_right_click(w)
        return w
    if isinstance(val, str) and key in COMBO_KEYS:
        w = QComboBox()
        for item in COMBO_KEYS[key]:
            w.addItem(DISPLAY_NAMES.get(item, item), item)
        idx = w.findData(val)
        if idx >= 0:
            w.setCurrentIndex(idx)
        disable_right_click(w)
        return w
    if isinstance(val, float):
        w = QDoubleSpinBox()
        w.setRange(-999999, 999999)
        w.setDecimals(3)
        w.setValue(val)
        disable_right_click(w)
        return w
    if isinstance(val, int):
        w = QSpinBox()
        if key in ("start", "end"):
            w.setRange(5, 95)
        else:
            w.setRange(-999999, 999999)
        w.setValue(val)
        disable_right_click(w)
        return w
    w = QLineEdit(str(val))
    disable_right_click(w)
    return w


def _widget_value(w, key=None):
    if isinstance(w, QComboBox):
        t = w.currentData()
        if t in ("true", "false"):
            return t == "true"
        return t
    if isinstance(w, QSpinBox):
        return w.value()
    if isinstance(w, QDoubleSpinBox):
        return w.value()
    if key in ("colour", "message", "cmd"):
        return w.text()
    return parse_value(w.text())


ACTION_FIELDS = {
    "click":           ["action", "x", "y", "clicks", "interval", "jitter_ms", "jitter_px"],
    "click_cursor":    ["action", "clicks", "interval", "jitter_ms", "jitter_px"],
    "wait":            ["action", "ms", "wait_jitter"],
    "screencap_check": ["action", "x", "y", "w", "h", "colour", "tol", "then", "checks", "else"],


    "notify":          ["action", "message"],
    "log":             ["action", "message"],
    "run_mode":        ["action", "mode", "duration_ms"],
    "run":             ["action", "cmd", "timeout_ms"],
    "zoom":            ["action", "x", "y", "start", "end", "duration"],
}

ACTION_DEFAULTS = {
    "click":           {"x": 0, "y": 0, "clicks": 1, "interval": 200, "jitter_ms": 5, "jitter_px": 5},
    "click_cursor":    {"clicks": 1, "interval": 200, "jitter_ms": 5, "jitter_px": 5},
    "wait":            {"ms": 1000, "wait_jitter": 0},
    "screencap_check": {"x": 0, "y": 0, "w": 20, "h": 20, "colour": "000000", "tol": 1, "then": 0, "checks": [], "else": -1},
    "notify":          {"message": ""},
    "log":             {"message": ""},
    "run_mode":        {"mode": "", "duration_ms": 30000},
    "run":             {"cmd": "", "timeout_ms": 5000},
    "zoom":            {"x": 500, "y": 500, "start": 10, "end": 90, "duration": 300},
}


class _CheckItem(QWidget):
    def __init__(self, item, on_remove, on_edit=None):
        super().__init__()
        self._item = dict(item)
        self._on_remove = on_remove
        self._on_edit = on_edit
        self._expanded = False
        self._field_edits = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(1)

        row = QHBoxLayout()
        row.setSpacing(4)

        self._toggle_btn = QToolButton()
        self._toggle_btn.setText("\u25b6")
        self._toggle_btn.setFixedSize(16, 16)
        self._toggle_btn.setAutoRaise(True)
        self._toggle_btn.setStyleSheet(
            "QToolButton { font-size: 5px; }"
        )
        self._toggle_btn.clicked.connect(self._toggle)
        row.addWidget(self._toggle_btn)

        self._summary = QLabel(self._summary_text())
        row.addWidget(self._summary, stretch=1)

        remove_btn = QToolButton()
        remove_btn.setText("\u2715")
        remove_btn.setFixedSize(16, 16)
        remove_btn.setAutoRaise(True)
        remove_btn.setStyleSheet(
            "QToolButton { font-size: 7px; }"
        )
        remove_btn.clicked.connect(lambda: self._on_remove(self))
        row.addWidget(remove_btn)

        layout.addLayout(row)

        self._detail = QWidget()
        self._detail.setVisible(False)
        detail_layout = QVBoxLayout(self._detail)
        detail_layout.setContentsMargins(18, 0, 0, 2)
        detail_layout.setSpacing(2)

        for k, v in self._item.items():
            row2 = QHBoxLayout()
            row2.setSpacing(4)
            kl = _make_key_label(k, 85)
            row2.addWidget(kl)
            w = _widget_for(k, v)
            if isinstance(w, QComboBox):
                w.currentTextChanged.connect(lambda *_, key=k, wgt=w: self._on_field_edit(key, wgt))
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(lambda *_, key=k, wgt=w: self._on_field_edit(key, wgt))
            else:
                w.editingFinished.connect(lambda *_, key=k, wgt=w: self._on_field_edit(key, wgt))
            row2.addWidget(w, stretch=1)
            self._field_edits[k] = w
            detail_layout.addLayout(row2)

        layout.addWidget(self._detail)

    def _summary_text(self):
        parts = []
        for k in ("colour", "tol", "then"):
            if k in self._item:
                parts.append(f"{k}: {self._item[k]}")
        return ", ".join(parts)

    def _toggle(self):
        self._expanded = not self._expanded
        self._detail.setVisible(self._expanded)
        self._toggle_btn.setText("\u25bc" if self._expanded else "\u25b6")

    def _on_field_edit(self, key, widget):
        self._item[key] = _widget_value(widget, key)
        self._summary.setText(self._summary_text())
        if self._on_edit:
            self._on_edit()

    def get_item(self):
        return dict(self._item)


class _DropContainer(QWidget):
    def __init__(self, sidebar, array_key):
        super().__init__()
        self._sidebar = sidebar
        self._array_key = array_key
        self.setAcceptDrops(True)
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().setSpacing(0)

        self._drop_indicator = QFrame(self)
        self._drop_indicator.setFixedHeight(2)
        self._drop_indicator.setStyleSheet("background: #ffb217;")
        self._drop_indicator.hide()

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-array-item"):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if not event.mimeData().hasFormat("application/x-array-item"):
            return
        data = json.loads(bytes(event.mimeData().data("application/x-array-item")).decode())
        if data.get("array_key") != self._array_key:
            return
        y = event.position().toPoint().y()
        idx = self._target_idx(y)
        self._show_at(idx)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._drop_indicator.hide()

    def dropEvent(self, event):
        self._drop_indicator.hide()
        if not event.mimeData().hasFormat("application/x-array-item"):
            return
        data = json.loads(bytes(event.mimeData().data("application/x-array-item")).decode())
        if data.get("array_key") != self._array_key:
            return
        from_idx = data["source_index"]
        y = event.position().toPoint().y()
        to_idx = self._target_idx(y)
        self._sidebar._move_array_item(self._array_key, from_idx, to_idx)
        event.acceptProposedAction()

    def _target_idx(self, y):
        widgets = self._sidebar._array_widgets.get(self._array_key, {}).get("widgets", [])
        if not widgets:
            return 0
        for i, w in enumerate(widgets):
            wy = w.geometry().center().y()
            if y < wy:
                return i
        return len(widgets)

    def _show_at(self, idx):
        widgets = self._sidebar._array_widgets.get(self._array_key, {}).get("widgets", [])
        if not widgets:
            self._drop_indicator.move(0, 0)
            self._drop_indicator.resize(self.width(), 2)
            self._drop_indicator.show()
            return
        if idx >= len(widgets):
            w = widgets[-1]
            y = w.geometry().bottom()
        elif idx <= 0:
            w = widgets[0]
            y = w.geometry().top()
        else:
            above = widgets[idx - 1]
            below = widgets[idx]
            y = (above.geometry().bottom() + below.geometry().top()) // 2
        self._drop_indicator.move(0, y)
        self._drop_indicator.resize(self.width(), 2)
        self._drop_indicator.raise_()
        self._drop_indicator.show()


class _ArrayItem(QWidget):
    def __init__(self, parent_widget, array_key, index, item, on_remove, disabled_actions=None):
        super().__init__()
        self._parent_widget = parent_widget
        self._array_key = array_key
        self._index = index
        self._item = item
        self._on_remove = on_remove
        self._disabled_actions = disabled_actions or set()
        self._disabled = item.get("action") in self._disabled_actions
        self._expanded = False
        self._selected = False
        self._referenced_colour = None
        self._field_edits = {}
        self._check_widgets = []
        self._capture_p1 = None
        self._drag_start_pos = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(1)

        self._header_frame = QFrame()
        header_layout = QHBoxLayout(self._header_frame)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)

        self._drag_handle = QToolButton()
        self._drag_handle.setText("\u22ee")
        self._drag_handle.setFixedSize(14, 14)
        self._drag_handle.setAutoRaise(True)
        self._drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_handle.setStyleSheet("QToolButton { padding: 0px; font-size: 10px; }")
        self._drag_handle.installEventFilter(self)
        header_layout.addWidget(self._drag_handle)

        self._summary = QLabel(self._summary_text())
        self._summary.installEventFilter(self)
        header_layout.addWidget(self._summary, stretch=1)

        remove_btn = QToolButton()
        remove_btn.setText("\u2715")
        remove_btn.setFixedSize(18, 18)
        remove_btn.setAutoRaise(True)
        remove_btn.setStyleSheet(
            "QToolButton { padding: 0px 0px; font-size: 8px; }"
        )
        remove_btn.clicked.connect(lambda: self._on_remove(self._array_key, self._index))
        header_layout.addWidget(remove_btn)

        layout.addWidget(self._header_frame)

        self._detail = QWidget()
        self._detail.setVisible(False)
        self._detail_layout = QVBoxLayout(self._detail)
        self._detail_layout.setContentsMargins(22, 0, 0, 2)
        self._detail_layout.setSpacing(2)
        self._rebuild_detail()

        layout.addWidget(self._detail)

        self._refresh_style()

    def _summary_text(self):
        item = self._item
        if "action" in item:
            parts = [DISPLAY_NAMES.get(item["action"], item["action"])]
            for k in ("x", "y", "ms", "message", "mode"):
                if k in item and item[k] is not None:
                    parts.append(f"<b>{item[k]}</b>")
            return " ".join(parts)
        s = f"(<b>{item.get('x', '?')}</b>, <b>{item.get('y', '?')}</b>)"
        if "clicks" in item:
            s += f" x<b>{item['clicks']}</b>"
        return s

    def eventFilter(self, obj, event):
        if obj is self._drag_handle:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_start_pos = event.globalPosition().toPoint()
                return True
            elif event.type() == QEvent.Type.MouseMove and self._drag_start_pos:
                if (event.globalPosition().toPoint() - self._drag_start_pos).manhattanLength() > 5:
                    self._start_drag()
                    self._drag_start_pos = None
                return True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_start_pos = None
                return True
            return False
        if obj is self._summary and event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self._parent_widget._select_array_item(self._array_key, self._index)
                return True
        return super().eventFilter(obj, event)

    def _start_drag(self):
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData("application/x-array-item", json.dumps({
            "array_key": self._array_key,
            "source_index": self._index,
        }).encode())
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def set_selected(self, selected):
        self._selected = selected
        self._refresh_style()

    def set_referenced(self, colour=None):
        self._referenced_colour = colour
        self._refresh_style()

    def _refresh_style(self):
        parts = []
        if self._referenced_colour:
            c = self._referenced_colour
            if c == "default":
                c = "#ddd"
            elif not c.startswith("#"):
                c = "#" + c
            parts.append(f"border: 1px solid {c}; border-radius: 2px;")
        elif self._selected:
            parts.append("border: 1px solid #ddd; border-radius: 2px;")
        if self._disabled:
            parts.append("color: #555;")
        if parts:
            self._summary.setStyleSheet(" ".join(parts))
        else:
            self._summary.setStyleSheet("")

    def _clear_detail(self):
        self._field_edits = {}
        self._check_widgets = []
        self._capture_p1 = None
        while self._detail_layout.count():
            item = self._detail_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            l = item.layout()
            if l:
                while l.count():
                    li = l.takeAt(0)
                    w2 = li.widget()
                    if w2:
                        w2.deleteLater()

    def _rebuild_detail(self):
        self._clear_detail()

        action = self._item.get("action")
        if action and action in ACTION_FIELDS:
            keys = ACTION_FIELDS[action]
            for k in list(self._item):
                if k not in keys:
                    if action != "run_mode":
                        del self._item[k]
            for k, v in ACTION_DEFAULTS.get(action, {}).items():
                if k not in self._item:
                    self._item[k] = v
        else:
            keys = list(self._item.keys())
            if "clicks" not in keys:
                keys.append("clicks")

        for k in keys:
            v = self._item.get(k)
            if v is None and k != "clicks":
                continue

            if isinstance(v, list) and k == "checks" and action == "screencap_check":
                checks_container = QWidget()
                self._checks_layout = QVBoxLayout(checks_container)
                self._checks_layout.setContentsMargins(0, 2, 0, 0)
                self._checks_layout.setSpacing(2)

                header_row = QHBoxLayout()
                self._checks_header = QLabel(f"checks ({len(v)} items)")
                header_row.addWidget(self._checks_header, stretch=1)
                add_btn = QPushButton("+ Add")
                add_btn.setFixedHeight(18)
                add_btn.setStyleSheet(
                    "QPushButton { background: #333; color: #bbb; border: 1px solid #555;"
                    " border-radius: 3px; padding: 0px 6px; font-size: 10px; }"
                )
                add_btn.clicked.connect(self._add_check)
                header_row.addWidget(add_btn)
                self._checks_layout.addLayout(header_row)

                for check_data in v:
                    cw = _CheckItem(check_data, self._remove_check, self._parent_widget._mark_dirty)
                    self._check_widgets.append(cw)
                    self._checks_layout.addWidget(cw)

                self._detail_layout.addWidget(checks_container)
                continue

            row2 = QHBoxLayout()
            row2.setSpacing(4)
            kl = _make_key_label(k, 85)
            row2.addWidget(kl)
            if k == "clicks" and k not in self._item:
                w = QLineEdit()
                disable_right_click(w)
            elif action == "run_mode" and k == "mode":
                w = QComboBox()
                modes_list = [self._parent_widget._mode_combo.itemText(i)
                              for i in range(self._parent_widget._mode_combo.count())]
                filtered = [m for m in modes_list
                            if m.startswith("fixed.") or m.startswith("custom.")
                            if m != self._parent_widget._mode_name]
                w.addItem("", "")
                for m in filtered:
                    w.addItem(m, m)
                if not filtered:
                    w.setEnabled(False)
                w.blockSignals(True)
                idx = w.findData(self._item.get("mode", ""))
                if idx >= 0:
                    w.setCurrentIndex(idx)
                w.blockSignals(False)
                disable_right_click(w)
            else:
                w = _widget_for(k, v)
            if isinstance(w, QComboBox):
                w.currentTextChanged.connect(lambda *_, key=k, wgt=w: self._on_field_edit(key, wgt))
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(lambda *_, key=k, wgt=w: self._on_field_edit(key, wgt))
            else:
                w.editingFinished.connect(lambda *_, key=k, wgt=w: self._on_field_edit(key, wgt))
            row2.addWidget(w, stretch=1)
            self._field_edits[k] = w
            self._detail_layout.addLayout(row2)

        for w in self._field_edits.values():
            w.setEnabled(not self._disabled)

        if action == "run_mode" and self._item.get("mode"):
            self._add_override_fields(self._item["mode"])

    def _add_override_fields(self, mode_name):
        resp = send_cmd("read_mode", mode=mode_name)
        if not resp or not resp.get("ok"):
            return
        target = resp.get("data", {})
        overrides = {k: v for k, v in target.items()
                     if k not in ("action", "mode", "duration_ms", "method",
                                  "screen_cap", "zoom", "points", "sequence")
                     and not isinstance(v, (list, dict))}
        if not overrides:
            return
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #888; margin-top: 4px;")
        self._detail_layout.addWidget(sep)
        for key, default_val in overrides.items():
            val = self._item.get(key, default_val)
            row = QHBoxLayout()
            row.setSpacing(4)
            kl = _make_key_label(key, 85)
            row.addWidget(kl)
            w = _widget_for(key, val)
            if isinstance(w, QComboBox):
                w.currentTextChanged.connect(lambda *_, k=key, wgt=w: self._on_field_edit(k, wgt))
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(lambda *_, k=key, wgt=w: self._on_field_edit(k, wgt))
            else:
                w.editingFinished.connect(lambda *_, k=key, wgt=w: self._on_field_edit(k, wgt))
            row.addWidget(w, stretch=1)
            self._field_edits[key] = w
            w.setEnabled(not self._disabled)
            self._detail_layout.addLayout(row)

    def _toggle(self):
        self._expanded = not self._expanded
        self._detail.setVisible(self._expanded)

    def _on_field_edit(self, key, widget):
        if key == "clicks" and isinstance(widget, QLineEdit) and not widget.text():
            self._item.pop("clicks", None)
        else:
            self._item[key] = _widget_value(widget, key)
        self._summary.setText(self._summary_text())
        self._parent_widget._mark_dirty()
        self._parent_widget._push_dots()
        if key == "action":
            self._rebuild_detail()
            self._disabled = self._item.get("action") in self._parent_widget._disabled_actions
            self._refresh_style()
            for w in self._field_edits.values():
                w.setEnabled(not self._disabled)
        elif key == "mode" and self._item.get("action") == "run_mode":
            standard = set(ACTION_FIELDS["run_mode"])
            for k in list(self._item):
                if k not in standard:
                    del self._item[k]
            self._rebuild_detail()
        elif key in ("then", "colour", "else") and self._item.get("action") == "screencap_check":
            self._parent_widget._update_referenced()

    def _handle_capture(self, cx, cy):
        action = self._item.get("action")
        if action in ("click", "zoom"):
            self._item["x"] = cx
            self._item["y"] = cy
            self._sync_field("x", cx)
            self._sync_field("y", cy)
            self._summary.setText(self._summary_text())
            self._parent_widget._exit_capture()
        elif action == "screencap_check":
            if self._capture_p1 is None:
                self._capture_p1 = (cx, cy)
                self._item["x"] = cx
                self._item["y"] = cy
                self._sync_field("x", cx)
                self._sync_field("y", cy)
                self._summary.setText(self._summary_text())
            else:
                x1, y1 = self._capture_p1
                x2, y2 = cx, cy
                nx = min(x1, x2)
                ny = min(y1, y2)
                nw = abs(x2 - x1)
                nh = abs(y2 - y1)
                self._item["x"] = nx
                self._item["y"] = ny
                self._item["w"] = nw
                self._item["h"] = nh
                self._sync_field("x", nx)
                self._sync_field("y", ny)
                self._sync_field("w", nw)
                self._sync_field("h", nh)
                self._summary.setText(self._summary_text())
                self._capture_p1 = None
                self._parent_widget._exit_capture()

    def _sync_field(self, key, value):
        w = self._field_edits.get(key)
        if isinstance(w, QSpinBox):
            w.setValue(value)
        elif isinstance(w, QDoubleSpinBox):
            w.setValue(value)
        elif isinstance(w, QLineEdit):
            w.setText(str(value))

    def _add_check(self):
        default = {"colour": "000000", "tol": 1, "then": 0}
        self._item.setdefault("checks", []).append(default)
        self._parent_widget._mark_dirty()
        cw = _CheckItem(dict(default), self._remove_check, self._parent_widget._mark_dirty)
        self._check_widgets.append(cw)
        self._checks_layout.insertWidget(self._checks_layout.count() - 1, cw)
        self._checks_header.setText(f"checks ({len(self._check_widgets)} items)")

    def _remove_check(self, widget):
        idx = self._check_widgets.index(widget)
        self._item["checks"].pop(idx)
        self._parent_widget._mark_dirty()
        self._check_widgets.pop(idx)
        self._checks_layout.removeWidget(widget)
        widget.deleteLater()
        self._checks_header.setText(f"checks ({len(self._check_widgets)} items)")

    def get_item(self):
        item = dict(self._item)
        if self._check_widgets:
            item["checks"] = [w.get_item() for w in self._check_widgets]
        return item

    def update_index(self, idx):
        self._index = idx


class SidebarPanel(QFrame):
    def __init__(self, parent_overlay):
        super().__init__(parent_overlay)
        self._parent_overlay = parent_overlay
        self._resizing = False
        self._resize_start_x = 0
        self._resize_start_width = 0
        self._mode_name = None
        self._scalar_edits = {}
        self._array_widgets = {}
        self._capturing = False
        self._capture_array_key = None
        self._capture_item = None
        self._last_cursor = None
        self._android_w = None
        self._android_h = None
        self._selected_array = None
        self._selected_idx = -1
        self._data = {}
        self._disabled_actions = set()
        self._uinput_available = False
        self._dirty = False
        self._select_shows_sidebar = True

        self.setGeometry(0, 0, 250, parent_overlay.height())
        self.setMouseTracking(True)
        self.setStyleSheet("SidebarPanel { background: #2a2a2a; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 34, 8, 8)
        layout.setSpacing(4)

        # --- Mode editor header ---
        edit_row = QHBoxLayout()
        edit_row.setSpacing(4)
        edit_lbl = QLabel("Edit:")
        edit_lbl.setFixedWidth(28)
        edit_row.addWidget(edit_lbl)
        self._mode_combo = QComboBox()
        disable_right_click(self._mode_combo)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        edit_row.addWidget(self._mode_combo, stretch=1)

        self._create_btn = QToolButton()
        self._create_btn.setText("+")
        self._create_btn.setFixedSize(22, 22)
        self._create_btn.setAutoRaise(False)
        self._create_btn.setToolTip("Create new mode")
        self._create_btn.clicked.connect(self._toggle_create_form)
        edit_row.addWidget(self._create_btn)

        self._delete_btn = QToolButton()
        self._delete_btn.setText("\u2212")
        self._delete_btn.setFixedSize(22, 22)
        self._delete_btn.setAutoRaise(False)
        self._delete_btn.setToolTip("Delete mode")
        self._delete_btn.clicked.connect(self._delete_mode)
        edit_row.addWidget(self._delete_btn)

        layout.addLayout(edit_row)

        # --- Inline create form (initially hidden) ---
        self._create_form = QWidget()
        self._create_form.setVisible(False)
        create_layout = QHBoxLayout(self._create_form)
        create_layout.setContentsMargins(0, 0, 0, 0)
        create_layout.setSpacing(4)

        self._create_name = QLineEdit()
        disable_right_click(self._create_name)
        self._create_name.setPlaceholderText("name")
        create_layout.addWidget(self._create_name, stretch=1)

        self._create_type = QComboBox()
        disable_right_click(self._create_type)
        self._create_type.addItems(["custom", "fixed"])
        create_layout.addWidget(self._create_type)

        self._create_do = QToolButton()
        self._create_do.setText("\u002b")
        self._create_do.setFixedSize(22, 22)
        self._create_do.setAutoRaise(False)
        self._create_do.setToolTip("Create")
        self._create_do.clicked.connect(self._create_mode)
        create_layout.addWidget(self._create_do)

        self._create_cancel = QToolButton()
        self._create_cancel.setText("\u2212")
        self._create_cancel.setFixedSize(22, 22)
        self._create_cancel.setAutoRaise(False)
        self._create_cancel.setToolTip("Cancel")
        self._create_cancel.clicked.connect(self._hide_create_form)
        create_layout.addWidget(self._create_cancel)

        layout.addWidget(self._create_form)

        # --- Config scroll area ---
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll_layout.setSpacing(3)
        self._scroll.setWidget(self._scroll_content)
        layout.addWidget(self._scroll, stretch=1)

        # --- Feedback ---
        self._feedback = QLabel("")
        self._feedback.setWordWrap(True)
        self._feedback.setVisible(False)
        self._feedback.setStyleSheet(
            "QLabel { background: #3a3a3a; color: #ccc; padding: 4px 6px; "
            "border-radius: 3px; font-size: 11px; }"
        )
        layout.addWidget(self._feedback)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(lambda: self._feedback.setVisible(False))

        # --- Bottom action buttons ---
        self._cursor_btn = QPushButton("Cursor")
        self._cursor_btn.setEnabled(False)
        self._cursor_btn.setStyleSheet(
            "QPushButton { background: #333; color: #bbb; border: 1px solid #555;"
            " border-radius: 3px; padding: 4px 8px; font-size: 12px; }"
            "QPushButton:hover { background: #444; }"
            "QPushButton:pressed { background: #555; }"
            "QPushButton:disabled { background: #181818; color: #555; border: 1px solid #333; }"
        )
        self._cursor_btn.clicked.connect(self._bottom_cursor)
        layout.addWidget(self._cursor_btn)

        self._add_btn = QPushButton("Add")
        self._add_btn.setStyleSheet(
            "QPushButton { background: #ddd; color: #222; border: 1px solid #ddd;"
            " border-radius: 3px; padding: 4px 8px; font-size: 12px; }"
            "QPushButton:hover { background: #eee; }"
            "QPushButton:pressed { background: #ccc; }"
            "QPushButton:disabled { background: #181818; color: #555; border: 1px solid #333; }"
        )
        self._add_btn.clicked.connect(self._add_bottom)
        layout.addWidget(self._add_btn)

        self._save_btn = QPushButton("Save changes")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._save)
        layout.addWidget(self._save_btn)

        self._resize_btn = QPushButton("Resize")
        self._resize_btn.setToolTip("Resize/reposition overlay to target window")
        self._resize_btn.clicked.connect(self._resize_to_target)
        layout.addWidget(self._resize_btn)

        # --- OP slider ---
        op_row = QHBoxLayout()
        op_row.setSpacing(4)
        op_lbl = QLabel("OP")
        op_lbl.setFixedWidth(22)
        op_row.addWidget(op_lbl)
        self._op_slider = QSlider(Qt.Orientation.Horizontal)
        self._op_slider.setRange(0, 255)
        self._op_slider.setValue(self._parent_overlay._alpha)
        self._op_slider.valueChanged.connect(self._on_alpha_changed)
        op_row.addWidget(self._op_slider, stretch=1)
        self._op_val = QLabel(str(self._parent_overlay._alpha))
        self._op_val.setFixedWidth(28)
        self._op_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        op_row.addWidget(self._op_val)
        layout.addLayout(op_row)

        # --- Poll timer ---
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_cursor)
        self._timer.start(100)

        self.show()

    # --- Alpha ---

    def _on_alpha_changed(self, value):
        self._parent_overlay._alpha = value
        self._op_val.setText(str(value))
        self._parent_overlay.update()

    # --- Resize ---

    def _in_resize_zone(self, pos):
        return pos.x() >= self.width() - RESIZE_MARGIN

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._in_resize_zone(event.pos()):
            self._resizing = True
            self._resize_start_x = event.globalPosition().x()
            self._resize_start_width = self.width()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            dx = event.globalPosition().x() - self._resize_start_x
            w = max(MIN_WIDTH, min(MAX_WIDTH, int(self._resize_start_width + dx)))
            self.setFixedWidth(w)
            event.accept()
            return
        if self._in_resize_zone(event.pos()):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._resizing:
            self._resizing = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


    # --- Cursor polling ---

    def _poll_cursor(self):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.connect(DAEMON_SOCKET)
            sock.send(json.dumps({"cmd": "cursor_pos"}).encode())
            data = json.loads(sock.recv(65536).decode())
            sock.close()
            if data.get("ok"):
                d = data["data"]
                self._last_cursor = d
                self._uinput_available = d.get("uinput_available", False)
                ar = d.get("android_resolution")
                if ar:
                    self._android_w = ar["w"]
                    self._android_h = ar["h"]
                h = d["host"]
                self._parent_overlay._overlay_host_lbl.setText(f"Host: {h['x']}, {h['y']}")
                a = d.get("android")
                self._parent_overlay._overlay_android_lbl.setText(
                    f"Android: {a['x']}, {a['y']}" if a else "Android: \u2014, \u2014"
                )
                if self._mode_combo.count() == 0 and "modes" in d:
                    self._populate_modes(d["modes"], select_mode=d.get("mode"))

                # If current sidebar mode was deleted, switch to a valid mode
                if "modes" in d and self._mode_name and self._mode_name not in d["modes"]:
                    daemon_mode = d.get("mode", "")
                    target = (daemon_mode if daemon_mode in d["modes"]
                              else next((m for m in d["modes"] if ".template" not in m), ""))
                    if target:
                        self._mode_combo.blockSignals(True)
                        self._mode_combo.clear()
                        for mname in d["modes"]:
                            if ".template" not in mname:
                                self._mode_combo.addItem(mname)
                        self._mode_combo.setCurrentText(target)
                        self._mode_combo.blockSignals(False)
                        self._mode_name = target
                        resp = send_cmd("read_mode", mode=target)
                        self._rebuild_config(resp.get("data", {}) if resp else {})

                # Overlay state from daemon
                os_ = d.get("overlay_state", {})
                ow = self._parent_overlay
                new_visible = os_.get("visible", ow._overlay_visible)
                if new_visible != ow._overlay_visible:
                    ow._overlay_visible = new_visible
                    ow.setVisible(new_visible)
                if os_.get("quit"):
                    if self._confirm_discard():
                        QApplication.quit()
                    return
            else:
                self._clear_status()
        except Exception:
            self._clear_status()

    def _clear_status(self):
        self._parent_overlay._overlay_host_lbl.setText("Host: \u2014, \u2014")
        self._parent_overlay._overlay_android_lbl.setText("Android: \u2014, \u2014")

    # --- Mode dropdown ---

    def _populate_modes(self, modes, select_mode=None):
        self._mode_combo.blockSignals(True)
        self._mode_combo.clear()
        for name in modes:
            if ".template" not in name:
                self._mode_combo.addItem(name)
        if self._mode_combo.count() > 0:
            if select_mode and select_mode in modes:
                self._mode_combo.setCurrentText(select_mode)
            if not self._mode_combo.currentText():
                self._mode_combo.setCurrentIndex(0)
            self._on_mode_changed(self._mode_combo.currentIndex())
        self._mode_combo.blockSignals(False)

    def _on_mode_changed(self, idx):
        if not self._confirm_discard():
            if self._mode_name:
                self._mode_combo.blockSignals(True)
                self._mode_combo.setCurrentText(self._mode_name)
                self._mode_combo.blockSignals(False)
            return
        name = self._mode_combo.currentText()
        if not name:
            return
        self._mode_name = name
        resp = send_cmd("read_mode", mode=name)
        self._rebuild_config(resp.get("data", {}) if resp else {})

    def _resize_to_target(self):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.connect(DAEMON_SOCKET)
            sock.send(json.dumps({"cmd": "cursor_pos"}).encode())
            data = json.loads(sock.recv(65536).decode())
            sock.close()
            if not data.get("ok"):
                return
            d = data["data"]
            wb = d.get("window_bounds")
            if wb is not None:
                self._parent_overlay._win_bounds = wb
                self._parent_overlay.setGeometry(*wb)
                self._parent_overlay.update()
            ar = d.get("android_resolution")
            if ar:
                self._android_w = ar["w"]
                self._android_h = ar["h"]
                self._parent_overlay._android_w = self._android_w
                self._parent_overlay._android_h = self._android_h
        except Exception:
            pass

    def _toggle_create_form(self):
        visible = not self._create_form.isVisible()
        self._create_form.setVisible(visible)
        if visible:
            self._create_name.setFocus()
            self._create_name.selectAll()

    def _hide_create_form(self):
        self._create_form.setVisible(False)
        self._create_name.clear()
        self._feedback.setVisible(False)

    def _show_feedback(self, msg, timeout=5000):
        self._feedback.setText(msg)
        self._feedback.setVisible(True)
        self._feedback_timer.start(timeout)

    def _create_mode(self):
        name = self._create_name.text().strip()
        if not name:
            self._show_feedback("Name cannot be empty")
            return

        if not self._confirm_discard():
            return

        type_ = self._create_type.currentText()
        full_name = f"{type_}.{name}"

        resp = send_cmd("create_mode", name=name, type=type_)
        if not resp or not resp.get("ok"):
            self._show_feedback(resp.get("error", "failed to create mode") if resp else "daemon not running")
            return

        self._mode_combo.blockSignals(True)
        self._mode_combo.addItem(full_name)
        self._mode_combo.setCurrentText(full_name)
        self._mode_combo.blockSignals(False)

        self._hide_create_form()
        self._mode_name = full_name
        data = resp.get("data", {})
        self._rebuild_config(data)

        self._show_feedback(f"Created mode '{full_name}'")

    def _delete_mode(self):
        name = self._mode_combo.currentText()
        if not name:
            return
        mb = QMessageBox(self)
        mb.setWindowTitle("Delete mode")
        mb.setText(f"Delete mode '{name}'? This cannot be undone.")
        mb.setIcon(QMessageBox.Icon.NoIcon)
        mb.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        for btn in mb.buttons():
            btn.setIcon(QIcon())
        ret = mb.exec()
        if ret != QMessageBox.StandardButton.Yes:
            return
        resp = send_cmd("delete_mode", mode=name)
        if not resp or not resp.get("ok"):
            self._show_feedback(resp.get("error", "failed to delete mode") if resp else "daemon not running")
            return
        # Refresh mode list from daemon — it may have recreated special modes (follow)
        modes_resp = send_cmd("list_modes")
        if modes_resp and modes_resp.get("ok"):
            self._mode_combo.blockSignals(True)
            self._mode_combo.clear()
            for mname in modes_resp["modes"]:
                if ".template" not in mname:
                    self._mode_combo.addItem(mname)

            new_name = self._mode_combo.currentText() if self._mode_combo.count() > 0 else ""
            target = new_name or (modes_resp["modes"][0] if modes_resp["modes"] else "")
            idx = self._mode_combo.findText(target)
            if idx >= 0:
                self._mode_combo.setCurrentIndex(idx)
            self._mode_combo.blockSignals(False)

            if target:
                self._mode_name = target
                resp = send_cmd("read_mode", mode=target)
                self._rebuild_config(resp.get("data", {}) if resp else {})
            else:
                self._mode_name = None
                self._rebuild_config({})
        else:
            self._mode_name = None
            self._rebuild_config({})

        self._show_feedback(f"Deleted mode '{name}'")

    # --- Config builder ---

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            l = item.layout()
            if l:
                self._clear_layout(l)

    def _rebuild_config(self, data):
        self._mark_clean()
        self._deselect_all()
        self._data = data
        self._scalar_edits = {}
        self._array_widgets = {}
        self._disabled_actions = set()
        if not self._uinput_available:
            self._disabled_actions.add("zoom")
        if data.get("screen_cap") is False:
            self._disabled_actions.add("screencap_check")
        if sys.platform != "linux":
            self._disabled_actions.add("zoom")
            self._data["method"] = "adb-pipe"

        self._clear_layout(self._scroll_layout)

        has_config = bool(data)

        for key, val in data.items():
            if key in ("points", "sequence", "zoom"):
                continue
            if isinstance(val, list) or isinstance(val, dict):
                continue
            row = QHBoxLayout()
            row.setSpacing(4)
            kl = _make_key_label(key, 85, "repeat_scalar" if key == "repeat" else None)
            row.addWidget(kl)
            w = _widget_for(key, val)
            if sys.platform != "linux":
                if key == "method":
                    w.blockSignals(True)
                    w.clear()
                    w.addItem("adb-pipe", "adb-pipe")
                    w.blockSignals(False)
                    w.setEnabled(False)
                    w.setToolTip("uinput requires Linux, falling back to adb-pipe")
            elif key == "method" and not self._uinput_available:
                w.blockSignals(True)
                w.clear()
                w.addItem("adb-pipe", "adb-pipe")
                w.blockSignals(False)
                w.setEnabled(False)
                w.setToolTip("uinput disabled in global config (set uinput=true to enable)")
            if isinstance(w, QComboBox):
                w.currentTextChanged.connect(lambda *_, k=key, wgt=w: self._on_scalar_edit(k, wgt))
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(lambda *_, k=key, wgt=w: self._on_scalar_edit(k, wgt))
            else:
                w.editingFinished.connect(lambda *_, k=key, wgt=w: self._on_scalar_edit(k, wgt))
            row.addWidget(w, stretch=1)
            self._scalar_edits[key] = w
            self._scroll_layout.addLayout(row)

        if any(k in data for k in ("points", "sequence")):
            sep = QFrame()
            sep.setFixedHeight(1)
            sep.setStyleSheet("background: #ddd;")
            self._scroll_layout.addWidget(sep)

        for array_key in ("points", "sequence"):
            if array_key in data:
                self._add_array_section(array_key, data[array_key])

        self._scroll_layout.addStretch()
        self._save_btn.setEnabled(has_config)
        self._feedback.setVisible(False)
        self._add_btn.setEnabled("points" in data or "sequence" in data)
        self._push_dots()
        self._update_cursor_btn()

    def _on_scalar_edit(self, key, widget):
        self._data[key] = _widget_value(widget, key)
        self._mark_dirty()
        self._push_dots()
        if key == "screen_cap":
            self._update_disabled_actions()

    def _select_from_dots_idx(self, dots_idx):
        data = getattr(self, "_data", {})
        points = data.get("points", [])
        if dots_idx < len(points):
            self._select_array_item("points", dots_idx, allow_deselect=False)
        else:
            vis_idx = dots_idx - len(points)
            count = 0
            for i, s in enumerate(data.get("sequence", [])):
                if s.get("action") in ("click", "zoom", "screencap_check"):
                    if count == vis_idx:
                        self._select_array_item("sequence", i, allow_deselect=False)
                        if self._select_shows_sidebar and not self.isVisible():
                            self.show()
                            self.raise_()
                            self.parent()._toggle_btn.raise_()
                            self.parent()._autohide_btn.raise_()
                            self.parent()._select_btn.raise_()
                            self.parent()._capture_btn.raise_()
                        return
                    count += 1
        if self._select_shows_sidebar and not self.isVisible():
            self.show()
            self.raise_()
            self.parent()._toggle_btn.raise_()
            self.parent()._autohide_btn.raise_()
            self.parent()._select_btn.raise_()
            self.parent()._capture_btn.raise_()

    def _ensure_visible(self, array_key, idx):
        info = self._array_widgets.get(array_key)
        if info and 0 <= idx < len(info["widgets"]):
            w = info["widgets"][idx]
            QTimer.singleShot(0, lambda w=w: self._scroll.ensureWidgetVisible(w, 0, 200))

    def _update_disabled_actions(self):
        self._disabled_actions = set()
        if not self._uinput_available:
            self._disabled_actions.add("zoom")
        if self._data.get("screen_cap") is False:
            self._disabled_actions.add("screencap_check")
        if sys.platform != "linux":
            self._disabled_actions.add("zoom")
        for info in self._array_widgets.values():
            for w in info["widgets"]:
                w._disabled = w._item.get("action") in self._disabled_actions
                w._refresh_style()
                for fw in w._field_edits.values():
                    fw.setEnabled(not w._disabled)

    # --- Selection ---

    def _select_array_item(self, array_key, index, allow_deselect=True):
        if allow_deselect and self._selected_array == array_key and self._selected_idx == index:
            info = self._array_widgets.get(array_key)
            if info and 0 <= index < len(info["widgets"]):
                info["widgets"][index]._toggle()
                if not info["widgets"][index]._expanded:
                    self._deselect_all()
            self._scroll_content.layout().invalidate()
            self._scroll_content.layout().activate()
            return

        self._scroll_content.setUpdatesEnabled(False)

        if self._selected_array and self._selected_idx >= 0:
            info = self._array_widgets.get(self._selected_array)
            if info and self._selected_idx < len(info["widgets"]):
                old = info["widgets"][self._selected_idx]
                if old._expanded:
                    old._toggle()
        self._deselect_all()
        self._selected_array = array_key
        self._selected_idx = index
        info = self._array_widgets.get(array_key)
        if info and 0 <= index < len(info["widgets"]):
            info["widgets"][index].set_selected(True)
            if not info["widgets"][index]._expanded:
                info["widgets"][index]._toggle()
        self._scroll_content.setUpdatesEnabled(True)
        self._scroll_content.layout().invalidate()
        self._scroll_content.layout().activate()
        if info and 0 <= index < len(info["widgets"]):
            self._ensure_visible(array_key, index)
        self._push_dots()
        self._update_cursor_btn()

    def _deselect_all(self):
        if self._selected_array and self._selected_idx >= 0:
            info = self._array_widgets.get(self._selected_array)
            if info and self._selected_idx < len(info["widgets"]):
                info["widgets"][self._selected_idx].set_selected(False)
        self._selected_array = None
        self._selected_idx = -1
        self._clear_referenced()
        self._push_dots()
        self._update_cursor_btn()
        self._update_referenced()

    # --- Bottom action helpers ---

    def _add_bottom(self):
        if self._selected_array:
            self._add_array_item(self._selected_array)
        elif "points" in (self._data or {}):
            self._add_array_item("points")
        elif "sequence" in (self._data or {}):
            self._add_array_item("sequence")

    def _bottom_cursor(self):
        if self._capturing:
            self._exit_capture()
            return
        if not self._can_capture():
            return
        if self._selected_array == "sequence":
            info = self._array_widgets.get("sequence")
            if info and 0 <= self._selected_idx < len(info["widgets"]):
                self._toggle_capture(self._selected_array, capture_item=info["widgets"][self._selected_idx])
                return
        key = self._selected_array if self._selected_array else "points"
        self._toggle_capture(key)

    def _can_capture(self):
        if "points" in (self._data or {}):
            return True
        if not self._selected_array or self._selected_idx < 0:
            return False
        if self._selected_array == "sequence":
            arr = (self._data or {}).get("sequence", [])
            if 0 <= self._selected_idx < len(arr):
                return arr[self._selected_idx].get("action") in ("click", "zoom", "screencap_check")
        return False

    # --- Referenced item highlighting ---

    def _clear_referenced(self):
        for info in self._array_widgets.values():
            for w in info["widgets"]:
                if w._referenced_colour:
                    w.set_referenced(None)

    def _set_referenced_from_item(self, item):
        entries = []
        then_val = item.get("then")
        if isinstance(then_val, int) and then_val >= 0:
            entries.append((then_val, item.get("colour") or "default"))
        for check in item.get("checks", []):
            ct = check.get("then")
            if isinstance(ct, int) and ct >= 0:
                entries.append((ct, check.get("colour") or "default"))
        else_idx = item.get("else", -1)
        if isinstance(else_idx, int):
            if else_idx >= 0:
                entries.append((else_idx, "default"))
            elif else_idx == -1:
                entries.append((self._selected_idx + 1, "default"))
        info = self._array_widgets.get("sequence")
        if info:
            for idx, colour in entries:
                if 0 <= idx < len(info["widgets"]):
                    info["widgets"][idx].set_referenced(colour)

    def _update_referenced(self):
        self._clear_referenced()
        if self._selected_array == "sequence" and self._selected_idx >= 0:
            arr = (self._data or {}).get("sequence", [])
            if 0 <= self._selected_idx < len(arr):
                item = arr[self._selected_idx]
                if item.get("action") == "screencap_check":
                    self._set_referenced_from_item(item)

    # ---

    def _update_cursor_btn(self):
        self._cursor_btn.setEnabled(self._can_capture())

    # --- Dots push ---

    def _push_dots(self):
        dots = []
        selected_idx = -1
        data = getattr(self, "_data", {})
        method = data.get("method", "adb-pipe")
        for p in data.get("points", []):
            dots.append({"type": "dot", "ax": p.get("x", 0), "ay": p.get("y", 0)})
        seq_dot_offset = len(dots)
        for s in data.get("sequence", []):
            a = s.get("action")
            if a == "click":
                dots.append({"type": "dot", "ax": s.get("x", 0), "ay": s.get("y", 0)})
            elif a == "zoom":
                dots.append({"type": "zoom", "ax": s.get("x", 0), "ay": s.get("y", 0)})
            elif a == "screencap_check":
                dots.append({"type": "rect", "ax": s.get("x", 0), "ay": s.get("y", 0),
                             "aw": s.get("w", 20), "ah": s.get("h", 20)})
        if self._selected_array == "points" and self._selected_idx >= 0:
            if self._selected_idx < len(data.get("points", [])):
                selected_idx = self._selected_idx
        elif self._selected_array == "sequence" and self._selected_idx >= 0:
            count = 0
            for i, s in enumerate(data.get("sequence", [])):
                if i == self._selected_idx:
                    selected_idx = seq_dot_offset + count
                    break
                if s.get("action") in ("click", "zoom", "screencap_check"):
                    count += 1
        if self._android_w is not None:
            self._parent_overlay._android_w = self._android_w
            self._parent_overlay._android_h = self._android_h
        self._parent_overlay.set_dots(dots, selected_idx)

    # --- Array editor ---

    def _add_array_section(self, array_key, items):
        container = _DropContainer(self, array_key)
        container_layout = container.layout()
        container_layout.setContentsMargins(0, 4, 0, 0)
        container_layout.setSpacing(2)

        header = QLabel(f"{array_key} ({len(items)} items)")
        f = header.font()
        f.setBold(True)
        header.setFont(f)
        container_layout.addWidget(header)
        container.installEventFilter(self)

        item_widgets = []
        for idx, item in enumerate(items):
            aw = _ArrayItem(self, array_key, idx, item, self._remove_array_item, self._disabled_actions)
            item_widgets.append(aw)
            container_layout.addWidget(aw)

        self._array_widgets[array_key] = {
            "container": container,
            "widgets": item_widgets,
        }
        self._scroll_layout.addWidget(container)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                for key, info in self._array_widgets.items():
                    if obj == info["container"]:
                        pos = event.position().toPoint()
                        skip = False
                        for w in info["widgets"]:
                            if w._expanded:
                                dg = w._detail.geometry()
                                dx = w.pos().x() + dg.x()
                                dy = w.pos().y() + dg.y()
                                if dx <= pos.x() <= dx + dg.width() and dy <= pos.y() <= dy + dg.height():
                                    skip = True
                                    break
                        if not skip:
                            self._select_closest_array_item(key, pos)
                        return True
        return super().eventFilter(obj, event)

    def _select_closest_array_item(self, key, pos):
        info = self._array_widgets.get(key)
        if not info or not info["widgets"]:
            return
        click_y = pos.y()
        best_idx = 0
        best_dist = abs(click_y - info["widgets"][0].geometry().center().y())
        for i, w in enumerate(info["widgets"]):
            dist = abs(click_y - w.geometry().center().y())
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        self._select_array_item(key, best_idx)

    def _add_array_item(self, array_key, index=None):
        default = {"x": 0, "y": 0}
        if array_key == "sequence":
            default = {"action": "click", "x": 0, "y": 0}
        arr = self._data.setdefault(array_key, [])
        if index is not None:
            arr.insert(index, default)
        else:
            arr.append(default)
        self._mark_dirty()

        info = self._array_widgets.get(array_key)
        if not info:
            return
        container_layout = info["container"].layout()
        if index is not None:
            idx = index
        else:
            idx = len(info["widgets"])
        aw = _ArrayItem(self, array_key, idx, default, self._remove_array_item, self._disabled_actions)
        info["widgets"].insert(idx, aw)
        container_layout.insertWidget(1 + idx, aw)

        for i, w in enumerate(info["widgets"]):
            w.update_index(i)

        header = container_layout.itemAt(0).widget()
        if isinstance(header, QLabel):
            header.setText(f"{array_key} ({len(info['widgets'])} items)")
        self._save_btn.setEnabled(True)
        self._push_dots()
        self._select_array_item(array_key, idx)

    def _toggle_capture(self, array_key, capture_item=None):
        self._capturing = True
        self._capture_array_key = array_key
        self._capture_item = capture_item
        if self._parent_overlay._autohide_sidebar:
            self.hide()
        self._parent_overlay._capture_btn.show()
        self._parent_overlay._capture_btn.raise_()
        self._parent_overlay._capture_btn.setStyleSheet("""
            QPushButton {
                background: #ddd; color: #222; border: 1px solid #ddd;
                border-radius: 3px; padding: 4px 8px; font-size: 12px;
            }
            QPushButton:hover { background: #eee; }
            QPushButton:pressed { background: #ccc; }
        """)
        self._cursor_btn.setText("Stop")

    def _add_captured_point(self):
        if not self._last_cursor:
            return
        if not self._last_cursor.get("android"):
            return
        cx, cy = self._last_cursor["android"]["x"], self._last_cursor["android"]["y"]

        if self._capture_item:
            self._capture_item._handle_capture(cx, cy)
            self._mark_dirty()
            return

        pt = {"x": cx, "y": cy}
        arr = self._data.setdefault(self._capture_array_key, [])
        if self._selected_array == self._capture_array_key and self._selected_idx >= 0:
            insert_idx = self._selected_idx + 1
            arr.insert(insert_idx, pt)
        else:
            insert_idx = len(arr)
            arr.append(pt)
        self._mark_dirty()

        info = self._array_widgets.get(self._capture_array_key)
        if not info:
            return
        container_layout = info["container"].layout()
        idx = insert_idx
        aw = _ArrayItem(self, self._capture_array_key, idx, pt, self._remove_array_item, self._disabled_actions)
        info["widgets"].insert(idx, aw)
        container_layout.insertWidget(1 + idx, aw)
        for i, w in enumerate(info["widgets"]):
            w.update_index(i)
        header = container_layout.itemAt(0).widget()
        if isinstance(header, QLabel):
            header.setText(f"{self._capture_array_key} ({len(info['widgets'])} items)")
        self._save_btn.setEnabled(True)
        self._select_array_item(self._capture_array_key, idx)

    def _exit_capture(self):
        self._capturing = False
        self._capture_array_key = None
        self._capture_item = None
        self._parent_overlay._capture_btn.hide()
        self._parent_overlay._capture_btn.setStyleSheet("""
            QPushButton {
                background: #333; color: #bbb; border: 1px solid #555;
                border-radius: 3px; padding: 4px 8px; font-size: 12px;
            }
            QPushButton:hover { background: #444; }
            QPushButton:pressed { background: #555; }
        """)
        self._cursor_btn.setText("Cursor")
        if self._parent_overlay._autohide_sidebar:
            self.show()

    def _remove_array_item(self, array_key, index):
        arr = self._data.get(array_key, [])
        if 0 <= index < len(arr):
            arr.pop(index)
        self._mark_dirty()

        info = self._array_widgets.get(array_key)
        if not info or index >= len(info["widgets"]):
            return

        container_layout = info["container"].layout()
        aw = info["widgets"].pop(index)
        container_layout.removeWidget(aw)
        aw.deleteLater()

        if self._selected_array == array_key:
            if self._selected_idx == index:
                self._deselect_all()
            elif self._selected_idx > index:
                self._selected_idx -= 1

        for i, w in enumerate(info["widgets"]):
            w.update_index(i)

        header = container_layout.itemAt(0).widget()
        if isinstance(header, QLabel):
            header.setText(f"{array_key} ({len(info['widgets'])} items)")
        self._save_btn.setEnabled(True)
        self._push_dots()

    def _move_array_item(self, array_key, from_idx, to_idx):
        arr = self._data.get(array_key, [])
        if not arr or from_idx == to_idx or not (0 <= from_idx < len(arr) and 0 <= to_idx <= len(arr)):
            return
        item = arr.pop(from_idx)
        if from_idx < to_idx:
            to_idx -= 1
        arr.insert(to_idx, item)
        self._mark_dirty()

        info = self._array_widgets.get(array_key)
        if not info:
            return
        container_layout = info["container"].layout()
        w = info["widgets"].pop(from_idx)
        info["widgets"].insert(to_idx, w)
        container_layout.insertWidget(1 + to_idx, w)

        for i, wgt in enumerate(info["widgets"]):
            wgt.update_index(i)

        if self._selected_array == array_key:
            sel = self._selected_idx
            if sel == from_idx:
                sel = to_idx
            elif sel > from_idx and sel <= to_idx:
                sel -= 1
            elif sel >= to_idx and sel < from_idx:
                sel += 1
            self._selected_idx = sel

        header = container_layout.itemAt(0).widget()
        if isinstance(header, QLabel):
            header.setText(f"{array_key} ({len(info['widgets'])} items)")
        self._save_btn.setEnabled(True)
        self._push_dots()

    # --- Dirty tracking ---

    def _mark_dirty(self):
        if self._dirty:
            return
        self._dirty = True
        self._refresh_dirty_ui()

    def _mark_clean(self):
        self._dirty = False
        self._refresh_dirty_ui()
        self._strip_all_asterisks()

    def _refresh_dirty_ui(self):
        if self._dirty:
            self._save_btn.setText("Save changes \u25cf")
            self._save_btn.setStyleSheet(
                "QPushButton { background: #ddd; color: #222; border: 1px solid #ddd;"
                " border-radius: 3px; padding: 4px 8px; font-size: 12px; }"
            )
        else:
            self._save_btn.setText("Save changes")
            self._save_btn.setStyleSheet("")

    def _strip_all_asterisks(self):
        for i in range(self._mode_combo.count()):
            t = self._mode_combo.itemText(i)
            if t.endswith(" *"):
                self._mode_combo.setItemText(i, t[:-2])
        txt = self._mode_combo.currentText()
        if txt.endswith(" *"):
            self._mode_combo.setItemText(self._mode_combo.currentIndex(), txt[:-2])

    def _confirm_discard(self):
        if not self._dirty:
            return True
        mb = QMessageBox(self)
        mb.setWindowTitle("Unsaved changes")
        mb.setText("You have unsaved changes. Save before proceeding?")
        mb.setIcon(QMessageBox.Icon.NoIcon)
        mb.setStandardButtons(
            QMessageBox.StandardButton.Save |
            QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel,
        )
        for btn in mb.buttons():
            btn.setIcon(QIcon())
        ret = mb.exec()
        if ret == QMessageBox.StandardButton.Save:
            self._save()
            return True
        elif ret == QMessageBox.StandardButton.Discard:
            return True
        return False

    # --- Save ---

    def _save(self):
        data = {}
        for key, widget in self._scalar_edits.items():
            data[key] = _widget_value(widget, key)
        for array_key in ("points", "sequence"):
            info = self._array_widgets.get(array_key)
            if info:
                items = [w.get_item() for w in info["widgets"]]
                if items or array_key in self._data:
                    data[array_key] = items
        self._data = data
        self._mark_clean()
        self._push_dots()
        send_cmd("save_mode", mode=self._mode_name, data=data)
        self._show_feedback("Saved to file")


class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("android-overlay")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._alpha = 25
        self.setCursor(Qt.CursorShape.CrossCursor)

        self._dots_data = []
        self._selected_dot_idx = -1
        self._drag_idx = -1
        self._drag_off_x = 0
        self._drag_off_y = 0
        self._drag_ref = None
        self._drag_handle = None
        self._drag_start_rect = None
        self._drag_start_mouse = None
        self._win_bounds = None
        self._android_w = None
        self._android_h = None
        self._overlay_visible = True
        self._autohide_sidebar = True
        self._sidebar = SidebarPanel(self)
        self._sidebar._resize_to_target()

        def _btn_ss():
            return (
                "QPushButton { background: #333; color: #bbb; border: 1px solid #555;"
                " border-radius: 3px; padding: 4px 8px; font-size: 12px; }"
                "QPushButton:hover { background: #444; }"
                "QPushButton:pressed { background: #555; }"
            )

        self._toggle_btn = QPushButton("\u2630", self)
        self._toggle_btn.setGeometry(5, 5, 28, 28)
        self._toggle_btn.setStyleSheet(_btn_ss())
        self._toggle_btn.clicked.connect(self._toggle_sidebar)

        self._autohide_btn = QPushButton("H", self)
        self._autohide_btn.setGeometry(33, 5, 28, 28)
        self._autohide_btn.clicked.connect(self._toggle_autohide)
        _add_button_help(self._autohide_btn, "autohide")

        self._select_btn = QPushButton("S", self)
        self._select_btn.setGeometry(61, 5, 28, 28)
        self._select_btn.clicked.connect(self._toggle_select_sidebar)
        _add_button_help(self._select_btn, "select_shows_sidebar")

        self._refresh_on_btn(self._autohide_btn, self._autohide_sidebar)
        self._refresh_on_btn(self._select_btn, self._sidebar._select_shows_sidebar)

        self._capture_btn = QPushButton("\u25cf", self)
        self._capture_btn.setGeometry(89, 5, 28, 28)
        self._capture_btn.setVisible(False)
        self._capture_btn.setStyleSheet(_btn_ss())
        self._capture_btn.clicked.connect(self._exit_capture)

        self._overlay_host_lbl = QLabel("Host: \u2014, \u2014", self)
        self._overlay_host_lbl.setMinimumWidth(140)
        self._overlay_android_lbl = QLabel("Android: \u2014, \u2014", self)
        self._overlay_android_lbl.setMinimumWidth(140)

    def _toggle_sidebar(self):
        if self._sidebar.isVisible():
            self._sidebar.hide()
        else:
            self._sidebar.show()
            self._sidebar.raise_()
            self._toggle_btn.raise_()
            self._autohide_btn.raise_()
            self._select_btn.raise_()
            self._capture_btn.raise_()

    def _refresh_on_btn(self, btn, on):
        if on:
            ss = ("QPushButton { background: #222; color: #999; border: 1px solid #444;"
                  " border-radius: 3px; padding: 4px 8px; font-size: 12px; }"
                  "QPushButton:hover { background: #2a2a2a; }"
                  "QPushButton:pressed { background: #1a1a1a; }")
        else:
            ss = ("QPushButton { background: #333; color: #bbb; border: 1px solid #555;"
                  " border-radius: 3px; padding: 4px 8px; font-size: 12px; }"
                  "QPushButton:hover { background: #444; }"
                  "QPushButton:pressed { background: #555; }")
        btn.setStyleSheet(ss)

    def _toggle_autohide(self):
        self._autohide_sidebar = not self._autohide_sidebar
        self._refresh_on_btn(self._autohide_btn, self._autohide_sidebar)

    def _toggle_select_sidebar(self):
        self._sidebar._select_shows_sidebar = not self._sidebar._select_shows_sidebar
        self._refresh_on_btn(self._select_btn, self._sidebar._select_shows_sidebar)

    # --- Dots overlay ---

    def set_dots(self, dots_data, selected_idx=-1):
        self._dots_data = dots_data
        self._selected_dot_idx = selected_idx
        self.update()

    def _to_overlay(self, ax, ay):
        if self._win_bounds and self._android_w is not None and self._android_w > 0:
            wx, wy, ww, wh = self._win_bounds
            hx = wx + int(ax / self._android_w * ww)
            hy = wy + int(ay / self._android_h * wh)
            return hx - self.x(), hy - self.y()
        return ax - self.x(), ay - self.y()

    def _to_android(self, ox, oy):
        if self._win_bounds and self._android_w is not None and self._android_w > 0:
            wx, wy, ww, wh = self._win_bounds
            ax = int((ox + self.x() - wx) / ww * self._android_w)
            ay = int((oy + self.y() - wy) / wh * self._android_h)
            return ax, ay
        return ox + self.x(), oy + self.y()

    def _draw_dot(self, p, ox, oy, highlight):
        color = QColor("#ffb217") if highlight else QColor("#64c8ff")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(ox - 4, oy - 4, 8, 8)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color.darker(120), 1.5))
        p.drawEllipse(ox - 6, oy - 6, 12, 12)

    def _draw_zoom(self, p, ox, oy, highlight):
        color = QColor("#ffb217") if highlight else QColor("#64c8ff")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(ox - 4, oy - 4, 8, 8)
        p.setPen(QPen(color, 1.5))
        p.drawLine(ox - 10, oy, ox + 10, oy)
        p.drawLine(ox, oy - 10, ox, oy + 10)

    def _handle_positions(self, ox, oy, ow, oh):
        return [("tl", ox, oy), ("br", ox + ow, oy + oh)]

    def _draw_rect(self, p, ox, oy, ow, oh, highlight):
        color = QColor("#ffb217") if highlight else QColor("#ff6400")
        p.setPen(QPen(color, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(ox, oy, ow, oh)
        if highlight:
            p.setBrush(QColor("#ffb217"))
            p.setPen(Qt.PenStyle.NoPen)
            for _, hx, hy in self._handle_positions(ox, oy, ow, oh):
                p.drawRect(hx - 3, hy - 3, 6, 6)

    def _hit_test_rect_handle(self, pos, idx, d):
        ox, oy = self._to_overlay(d.get("ax", 0), d.get("ay", 0))
        aw, ah = d.get("aw", 20), d.get("ah", 20)
        if self._win_bounds and self._android_w is not None and self._android_w > 0:
            _, _, ww, wh = self._win_bounds
            ow = int(aw / self._android_w * ww)
            oh = int(ah / self._android_h * wh)
        else:
            ow, oh = aw, ah
        for name, hx, hy in self._handle_positions(ox, oy, ow, oh):
            if abs(pos.x() - hx) <= 10 and abs(pos.y() - hy) <= 10:
                return name
        return None

    def _hit_test_dots(self, pos):
        best = None
        best_d2 = 225.0
        for idx, d in enumerate(self._dots_data):
            ax, ay = d.get("ax", 0), d.get("ay", 0)
            ox, oy = self._to_overlay(ax, ay)
            dx = pos.x() - ox
            dy = pos.y() - oy
            t = d.get("type")
            if t == "rect":
                aw, ah = d.get("aw", 20), d.get("ah", 20)
                if self._win_bounds and self._android_w is not None and self._android_w > 0:
                    _, _, ww, wh = self._win_bounds
                    ow = int(aw / self._android_w * ww)
                    oh = int(ah / self._android_h * wh)
                else:
                    ow, oh = aw, ah
                if -5 <= dx <= ow + 5 and -5 <= dy <= oh + 5:
                    return idx
            else:
                d2 = dx*dx + dy*dy
                if d2 < best_d2:
                    best_d2 = d2
                    best = idx
        return best

    def resizeEvent(self, event):
        if hasattr(self, "_sidebar"):
            self._sidebar.setFixedHeight(self.height())
        if hasattr(self, "_overlay_host_lbl"):
            self._overlay_host_lbl.move(self.width() - self._overlay_host_lbl.width() - 58, 8)
            self._overlay_android_lbl.move(self.width() - self._overlay_android_lbl.width() - 58, 26)
        super().resizeEvent(event)

    def moveEvent(self, event):
        self.update()
        super().moveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        if self._alpha > 0:
            p.fillRect(self.rect(), QColor(0, 0, 0, self._alpha))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for idx, d in enumerate(self._dots_data):
            ox, oy = self._to_overlay(d.get("ax", 0), d.get("ay", 0))
            t = d.get("type")
            highlight = idx == self._selected_dot_idx
            if t == "dot":
                self._draw_dot(p, ox, oy, highlight)
            elif t == "zoom":
                self._draw_zoom(p, ox, oy, highlight)
            elif t == "rect":
                aw = d.get("aw", 20)
                ah = d.get("ah", 20)
                if self._win_bounds and self._android_w is not None and self._android_w > 0:
                    _, _, ww, wh = self._win_bounds
                    ow = int(aw / self._android_w * ww)
                    oh = int(ah / self._android_h * wh)
                else:
                    ow, oh = aw, ah
                self._draw_rect(p, ox, oy, ow, oh, highlight)

    def closeEvent(self, event):
        if self._sidebar._confirm_discard():
            event.accept()
            QApplication.quit()
        else:
            event.ignore()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            QApplication.quit()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._sidebar._capturing:
                if not self._capture_btn.geometry().contains(event.pos()):
                    self._sidebar._add_captured_point()
                    event.accept()
                    return
            else:
                idx = self._hit_test_dots(event.pos())
                if idx is not None:
                    self._sidebar._select_from_dots_idx(idx)
                    d = self._dots_data[idx]
                    t = d.get("type")
                    if t in ("dot", "zoom", "rect"):
                        self._drag_idx = idx
                        self._drag_ref = (self._sidebar._selected_array, self._sidebar._selected_idx)
                        if t == "rect":
                            handle = self._hit_test_rect_handle(event.pos(), idx, d)
                            if handle:
                                self._drag_handle = handle
                                self._drag_start_rect = (d["ax"], d["ay"], d["aw"], d["ah"])
                                self._drag_start_mouse = self._to_android(event.pos().x(), event.pos().y())
                        if not self._drag_handle:
                            self._drag_handle = ""
                            ox, oy = self._to_overlay(d["ax"], d["ay"])
                            self._drag_off_x = event.pos().x() - ox
                            self._drag_off_y = event.pos().y() - oy
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_idx >= 0:
            self._update_drag(event.pos())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_idx >= 0 and event.button() == Qt.MouseButton.LeftButton:
            self._update_drag(event.pos())
            self._sidebar._mark_dirty()
            self._sidebar._push_dots()
            self._drag_idx = -1
            self._drag_ref = None
            self._drag_handle = None
            self._drag_start_rect = None
            self._drag_start_mouse = None
        super().mouseReleaseEvent(event)

    def _update_drag(self, pos):
        d = self._dots_data[self._drag_idx]
        if self._drag_handle:
            sax, say, saw, sah = self._drag_start_rect
            smx, smy = self._drag_start_mouse
            cmx, cmy = self._to_android(pos.x(), pos.y())
            dx = cmx - smx
            dy = cmy - smy
            if self._drag_handle == "tl":
                nx = sax + dx
                ny = say + dy
                nw = saw - dx
                nh = sah - dy
                if nw < 10:
                    nw = 10
                    nx = sax + saw - 10
                if nh < 10:
                    nh = 10
                    ny = say + sah - 10
            else:
                nx = sax
                ny = say
                nw = saw + dx
                nh = sah + dy
                if nw < 10:
                    nw = 10
                if nh < 10:
                    nh = 10
            d["ax"] = nx
            d["ay"] = ny
            d["aw"] = nw
            d["ah"] = nh
            updates = {"x": nx, "y": ny, "w": nw, "h": nh}
        else:
            new_ox = pos.x() - self._drag_off_x
            new_oy = pos.y() - self._drag_off_y
            new_ax, new_ay = self._to_android(new_ox, new_oy)
            d["ax"] = new_ax
            d["ay"] = new_ay
            updates = {"x": new_ax, "y": new_ay}
        if self._drag_ref:
            ak, ai = self._drag_ref
            data = getattr(self._sidebar, "_data", {})
            arr = data.get(ak)
            if arr and 0 <= ai < len(arr):
                for key, v in updates.items():
                    arr[ai][key] = v
                info = self._sidebar._array_widgets.get(ak)
                if info and 0 <= ai < len(info["widgets"]):
                    w = info["widgets"][ai]
                    for key, v in updates.items():
                        field = w._field_edits.get(key)
                        if field:
                            field.blockSignals(True)
                            if isinstance(field, (QSpinBox, QDoubleSpinBox)):
                                field.setValue(v)
                            elif isinstance(field, QLineEdit):
                                field.setText(str(v))
                            field.blockSignals(False)
                    w._summary.setText(w._summary_text())
        self.update()

    def _exit_capture(self):
        self._sidebar._exit_capture()


def main():
    if not HAS_PYQT6:
        print("error: PyQt6 is required for overlay (pip install PyQt6)", file=sys.stderr)
        sys.exit(1)

    singleton_lock(OVERLAY_SOCKET, "overlay")

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)

    rsock, wsock = socket.socketpair()
    wsock.setblocking(False)
    old_fd = signal.set_wakeup_fd(wsock.fileno())

    notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Type.Read)

    def handle_signal():
        notifier.setEnabled(False)
        try:
            rsock.recv(4096)
        except BlockingIOError:
            pass
        app.quit()

    notifier.activated.connect(handle_signal)
    signal.signal(signal.SIGINT, lambda sig, frame: None)

    w = OverlayWindow()
    w.show()
    ret = app.exec()

    notifier.setEnabled(False)
    rsock.close()
    wsock.close()
    if old_fd != -1:
        signal.set_wakeup_fd(old_fd)

    sys.exit(ret)
