from abc import ABC, abstractmethod


class PlatformAdapter(ABC):
    @abstractmethod
    def notify(self, message: str) -> None:
        """Send a desktop notification."""

    @abstractmethod
    def get_window_bounds(self) -> tuple[int, int, int, int] | None:
        """Returns (x, y, width, height) of the target emulator window, or None."""

    @abstractmethod
    def get_cursor_position(self) -> tuple[int, int]:
        """Returns host cursor position (x, y)."""

    @abstractmethod
    def get_display_resolution(self) -> tuple[int, int] | None:
        """Returns (width, height) of primary monitor, or None."""

    @staticmethod
    def detect(config: dict | None = None) -> "PlatformAdapter":
        import sys
        if sys.platform == "linux":
            from android_clicker.platform.linux import LinuxAdapter
            return LinuxAdapter(config)
        elif sys.platform == "darwin":
            from android_clicker.platform.macos import MacAdapter
            return MacAdapter(config)
        elif sys.platform == "win32":
            from android_clicker.platform.windows import WindowsAdapter
            return WindowsAdapter(config)
        raise RuntimeError(f"unsupported platform: {sys.platform}")
