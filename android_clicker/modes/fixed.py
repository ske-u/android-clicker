import random
import time

from ._base import BaseMode


class FixedMode(BaseMode):
    def __init__(self, config, injector, daemon, name):
        super().__init__(config, injector, daemon, name)
        self.idx = 0
        self.clicks_this_point = 0
        self.last_point_switch = time.monotonic()
        self.reset_timer_start = time.monotonic()

    def interval(self):
        return self.config.get("interval", 50) / 1000.0

    def jitter_ms(self):
        return self.config.get("jitter_ms", 0)

    def reset(self):
        self.idx = 0
        self.clicks_this_point = 0
        self.last_point_switch = time.monotonic()
        self.reset_timer_start = time.monotonic()

    def tick(self):
        points = self.config.get("points", [])
        if not points:
            return

        repeat = self.config.get("repeat", True)

        reset_timer = self.config.get("reset_timer", 0)
        if reset_timer > 0:
            jt = self.config.get("jitter_timer", 0)
            effective = max(0.001, (reset_timer + random.uniform(-jt, jt)) / 1000.0)
            if time.monotonic() - self.reset_timer_start >= effective:
                if not repeat:
                    self.daemon.active = False
                    return
                self.idx = 0
                self.clicks_this_point = 0
                self.reset_timer_start = time.monotonic()

        idx = self.idx % len(points)
        p = points[idx]

        x, y = p["x"], p["y"]

        jp = self.config.get("jitter_px", 0)
        if jp > 0:
            x += random.randint(-jp, jp)
            y += random.randint(-jp, jp)

        if self.injector.coord_space == "host":
            hx, hy = self.daemon._android_to_host(x, y)
            self.injector.tap(max(0, hx), max(0, hy))
        else:
            self.injector.tap(max(0, x), max(0, y))

        if len(points) > 1:
            if self.config.get("cycle_mode") == "clicks":
                self.clicks_this_point += 1
                point_clicks = p.get("clicks")
                limit = point_clicks or self.config.get("cycle_clicks", 20)
                cj = self.config.get("jitter_clicks", 0)
                limit = max(1, limit + random.randint(-cj, cj))
                if self.clicks_this_point >= limit:
                    self.idx = (idx + 1) % len(points)
                    self.clicks_this_point = 0
                    if self.idx == 0:
                        self.reset_timer_start = time.monotonic()
            else:
                now = time.monotonic()
                delay = self.config.get("cycle_delay", 1000) / 1000.0
                jit_s = self.config.get("jitter_ms", 0) / 1000.0
                jit = (random.random() - 0.5) * 2 * jit_s
                delay = max(0.001, delay + jit)
                if now - self.last_point_switch >= delay:
                    self.idx = (idx + 1) % len(points)
                    self.last_point_switch = now
                    if self.idx == 0:
                        self.reset_timer_start = time.monotonic()

        if not repeat:
            if len(points) > 1:
                if self.idx == 0 and idx == len(points) - 1:
                    self.daemon.active = False
            else:
                self.daemon.active = False
