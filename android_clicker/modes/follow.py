from ._base import BaseMode


class FollowMode(BaseMode):
    def interval(self):
        return self.config.get("interval", 100) / 1000.0

    def jitter_ms(self):
        return self.config.get("jitter_ms", 0)

    def tick(self):
        host_x, host_y = self.daemon.get_cursor_pos()

        if self.injector.coord_space == "host":
            jp = self.config.get("jitter_px", 0)
            if jp > 0:
                import random
                host_x += random.randint(-jp, jp)
                host_y += random.randint(-jp, jp)
            self.injector.tap(max(0, host_x), max(0, host_y))
        else:
            coords = self.daemon._translate(host_x, host_y)
            if coords is None:
                return
            ax, ay = coords
            jp = self.config.get("jitter_px", 0)
            if jp > 0:
                import random
                ax += random.randint(-jp, jp)
                ay += random.randint(-jp, jp)
            self.injector.tap(max(0, ax), max(0, ay))
