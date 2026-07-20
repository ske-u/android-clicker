import json
import subprocess

from android_clicker.platform import PlatformAdapter


class LinuxAdapter(PlatformAdapter):
    def __init__(self, config: dict | None = None):
        self._notify_backend = "hyprctl"
        self._target_window = "Waydroid"
        if config:
            self._notify_backend = (
                config.get("notifications", {})
                .get("notify_backend", "hyprctl")
            )
            self._target_window = config.get("display", {}).get("target_window", "Waydroid")

    def notify(self, message: str) -> None:
        if self._notify_backend == "libnotify":
            subprocess.run(
                ["notify-send", message],
                capture_output=True,
            )
        else:
            subprocess.run(
                ["hyprctl", "notify", "-1", "3000", "rgb(ff1ea3)", message],
                capture_output=True,
            )

    def get_window_bounds(self) -> tuple[int, int, int, int] | None:
        try:
            r = subprocess.run(
                ["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=5
            )
            for c in json.loads(r.stdout):
                cls_ = c.get("class", "")
                if self._target_window in cls_:
                    return c["at"][0], c["at"][1], c["size"][0], c["size"][1]
        except Exception:
            pass
        return None

    def get_cursor_position(self) -> tuple[int, int]:
        try:
            r = subprocess.run(
                ["hyprctl", "cursorpos"], capture_output=True, text=True, timeout=5
            )
            parts = r.stdout.strip().split(",")
            return int(parts[0]), int(parts[1])
        except Exception:
            return 0, 0

    def get_display_resolution(self) -> tuple[int, int]:
        try:
            r = subprocess.run(
                ["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=5
            )
            monitors = json.loads(r.stdout)
            if monitors:
                m = monitors[0]
                return m["width"], m["height"]
        except Exception:
            pass
        return None
