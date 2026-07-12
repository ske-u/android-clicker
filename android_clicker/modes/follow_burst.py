import random

from ._base import BaseMode


class FollowBurstMode(BaseMode):
    def __init__(self, config, injector, daemon, name):
        super().__init__(config, injector, daemon, name)
        self._remaining = 0

    def interval(self):
        return self.config.get("interval", 100) / 1000.0

    def jitter_ms(self):
        return self.config.get("jitter_ms", 0)

    def reset(self):
        clicks = self.config.get("clicks", 10)
        jc = self.config.get("jitter_clicks", 0)
        self._remaining = max(1, clicks + random.randint(-jc, jc))

    def tick(self):
        if self._remaining <= 0:
            return

        host_x, host_y = self.daemon.get_cursor_pos()

        if self.injector.coord_space == "host":
            jp = self.config.get("jitter_px", 0)
            if jp:
                host_x += random.randint(-jp, jp)
                host_y += random.randint(-jp, jp)
            self.injector.tap(max(0, host_x), max(0, host_y))
        else:
            coords = self.daemon._translate(host_x, host_y)
            if coords is None:
                return
            ax, ay = coords
            jp = self.config.get("jitter_px", 0)
            if jp:
                ax += random.randint(-jp, jp)
                ay += random.randint(-jp, jp)
            self.injector.tap(max(0, ax), max(0, ay))

        self._remaining -= 1
        if self._remaining <= 0:
            self.daemon.active = False
