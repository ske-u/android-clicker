import math
import random
import subprocess
import sys
import time

from ._base import BaseMode
from ..screen_utils import colour_in_rect, screencap_adb
from ..injectors import ensure_adb


class CustomMode(BaseMode):
    def __init__(self, config, injector, daemon, name, mode_data=None):
        super().__init__(config, injector, daemon, name)
        self.mode_data = mode_data
        self.idx = 0
        self.next_time = 0.0
        self._run_mode_target = None
        self._run_mode_end = 0.0
        self._run_mode_key = None
        self._saved_cfg = None
        self._repeat_remaining = 0
        self._repeat_x = 0
        self._repeat_y = 0
        self._repeat_interval = 0
        self._repeat_jit_ms = 0
        self._repeat_jp = 0
        self._repeat_cursor = False
        self._repeat_area = False
        self._repeat_cx = 0
        self._repeat_cy = 0
        self._repeat_w = 100
        self._repeat_h = 100
        self._repeat_angle = 0
        self._repeat_min_dist = 0
        self._repeat_unique = False
        self._repeat_positions = None
        self._repeat_prev = None
        self._init_screencap()
        self._cap_timeout = getattr(self.daemon, "screen_cap_timeout", 15)
        self._run_proc = None
        self._run_start = 0.0
        self._run_timeout = 0.0
        self._zoom_state = None
        self._then_idx = None

    def _get_config(self):
        if self.mode_data is not None:
            return self.mode_data
        return self.config

    def _init_screencap(self):
        cfg = self._get_config()
        if not cfg.get("screen_cap") or self.injector.coord_space != "host":
            return
        ok = ensure_adb(self.daemon.adb_connect, timeout=self.daemon.adb_timeout)
        if not ok:
            print("screen_cap init failed: no ADB device", file=sys.stderr)
            cfg["screen_cap"] = False

    def interval(self):
        cfg = self._get_config()
        return cfg.get("interval", 200) / 1000.0

    def jitter_ms(self):
        cfg = self._get_config()
        return cfg.get("jitter_ms", 5)

    def reset(self):
        self.idx = 0
        self.next_time = 0.0
        self._run_mode_target = None
        self._repeat_remaining = 0
        self._repeat_cursor = False
        self._repeat_area = False
        self._repeat_positions = None
        self._repeat_prev = None
        self._zoom_state = None
        self._then_idx = None
        if self._run_proc is not None:
            self._run_proc.kill()
            self._run_proc.wait()
            self._run_proc = None
        if self._saved_cfg is not None and self._run_mode_key:
            target = self.daemon.modes.get(self._run_mode_key)
            if target:
                target.config.clear()
                target.config.update(self._saved_cfg)
        self._run_mode_key = None
        self._saved_cfg = None

    def _advance_next(self, wait_ms=None):
        if wait_ms is not None:
            ms = wait_ms
        else:
            ms = self._get_config().get("default_wait_ms", 50)
        self.next_time = time.monotonic() + max(0.001, ms / 1000.0)

    def _click_at(self, x, y, jp=0):
        if jp:
            x += random.randint(-jp, jp)
            y += random.randint(-jp, jp)
        if self.injector.coord_space == "host":
            x, y = self.daemon._android_to_host(x, y)
        self.injector.tap(max(0, x), max(0, y))

    def _rand_point_in_area(self, cx, cy, w, h, angle_deg):
        u = random.uniform(-w / 2, w / 2)
        v = random.uniform(-h / 2, h / 2)
        cr = math.cos(math.radians(angle_deg))
        sr = math.sin(math.radians(angle_deg))
        return cx + u * cr - v * sr, cy + u * sr + v * cr

    def _advance_idx(self, then=None):
        if then is not None and then >= 0:
            self.idx = then
        elif self._then_idx is not None and self._then_idx >= 0:
            self.idx = self._then_idx
            self._then_idx = None
        else:
            self.idx += 1

    def tick(self):
        now = time.monotonic()

        if self._run_mode_target:
            if now >= self._run_mode_end:
                if self._saved_cfg is not None and self._run_mode_target:
                    self._run_mode_target.config.clear()
                    self._run_mode_target.config.update(self._saved_cfg)
                self._run_mode_target = None
                self._run_mode_key = None
                self._saved_cfg = None
                self._advance_idx()
                self._advance_next()
                return
            else:
                self._run_mode_target.tick()
                ti = self._run_mode_target.interval()
                jit_ms = self._get_config().get("jitter_ms", 5)
                jit = (random.random() - 0.5) * 2 * jit_ms / 1000.0
                self.next_time = now + max(0.001, ti + jit)
                return

        if self._run_proc is not None:
            ret = self._run_proc.poll()
            if ret is not None:
                self._run_proc = None
                self._advance_idx()
                self._advance_next()
            elif self._run_timeout > 0 and now - self._run_start >= self._run_timeout:
                self._run_proc.kill()
                self._run_proc.wait()
                self._run_proc = None
                self._advance_idx()
                self._advance_next()
            else:
                self.next_time = now + 0.05
            return

        if self._zoom_state is not None:
            zs = self._zoom_state
            ui, e = self.daemon._shared_uinput
            progress = zs["step"] / zs["steps"]
            spread_pct = zs["start_pct"] + (zs["end_pct"] - zs["start_pct"]) * progress
            half = zs["dimension"] * spread_pct / 100 / 2
            sx = int(zs["center_x"] - half)
            dx = int(zs["center_x"] + half)
            ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
            ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, sx)
            ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 100)
            ui.write(e.EV_ABS, e.ABS_MT_SLOT, 1)
            ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, dx)
            ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 100)
            ui.syn()
            zs["step"] += 1
            if zs["step"] > zs["steps"]:
                ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
                ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
                ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 0)
                ui.write(e.EV_ABS, e.ABS_MT_SLOT, 1)
                ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
                ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 0)
                ui.write(e.EV_KEY, e.BTN_TOUCH, 0)
                ui.syn()
                self._zoom_state = None
                self._advance_idx()
                self._advance_next()
            else:
                self.next_time = now + zs["step_delay"]
            return

        cfg = self._get_config()
        seq = cfg.get("sequence", [])

        if self._repeat_remaining > 0:
            if self._repeat_area:
                rx, ry = self._rand_point_in_area(
                    self._repeat_cx, self._repeat_cy,
                    self._repeat_w, self._repeat_h,
                    self._repeat_angle,
                )
                ok = True
                if self._repeat_unique and self._repeat_positions is not None:
                    if (int(rx), int(ry)) in self._repeat_positions:
                        ok = False
                if ok and self._repeat_min_dist > 0 and self._repeat_prev is not None:
                    dx = rx - self._repeat_prev[0]
                    dy = ry - self._repeat_prev[1]
                    if dx * dx + dy * dy < self._repeat_min_dist * self._repeat_min_dist:
                        ok = False
                if not ok:
                    self._repeat_unique = False
                    self._repeat_positions = None
                    self._repeat_min_dist = 0
                else:
                    if self._repeat_positions is not None:
                        self._repeat_positions.add((int(rx), int(ry)))
                    self._repeat_prev = (rx, ry)
                self._click_at(rx, ry, self._repeat_jp)
            elif self._repeat_cursor:
                x, y = self._repeat_x, self._repeat_y
                if self._repeat_jp:
                    x += random.randint(-self._repeat_jp, self._repeat_jp)
                    y += random.randint(-self._repeat_jp, self._repeat_jp)
                self.injector.tap(max(0, x), max(0, y))
            else:
                self._click_at(self._repeat_x, self._repeat_y, self._repeat_jp)
            self._repeat_remaining -= 1
            if self._repeat_remaining > 0:
                jit = (random.random() - 0.5) * 2 * self._repeat_jit_ms / 1000.0
                self.next_time = now + max(0.001, self._repeat_interval / 1000.0 + jit)
            else:
                self._advance_idx()
                self._advance_next()
            return

        if not seq or self.idx >= len(seq):
            if cfg.get("repeat", True) and seq:
                self.idx = 0
            elif seq:
                self.daemon.active = False
            self._advance_next()
            return

        step = seq[self.idx]
        action = step.get("action")

        if action == "click":
            if "x" not in step or "y" not in step:
                print("skip click: missing x/y")
                self._advance_idx(step.get("then"))
                self._advance_next()
                return
            clicks = step.get("clicks", 1)
            click_jp = step.get("jitter_px", cfg.get("jitter_px", 0))
            click_interval = step.get("interval", cfg.get("interval", 200))
            click_jit_ms = step.get("jitter_ms", cfg.get("jitter_ms", 5))
            if clicks > 1:
                self._then_idx = step.get("then")
                self._repeat_x = step.get("x", 0)
                self._repeat_y = step.get("y", 0)
                self._repeat_jp = click_jp
                self._repeat_interval = click_interval
                self._repeat_jit_ms = click_jit_ms
                self._click_at(self._repeat_x, self._repeat_y, click_jp)
                self._repeat_remaining = clicks - 1
                jit = (random.random() - 0.5) * 2 * click_jit_ms / 1000.0
                self.next_time = now + max(0.001, click_interval / 1000.0 + jit)
            else:
                self._click_at(step.get("x", 0), step.get("y", 0), click_jp)
                self._advance_idx(step.get("then"))
                self._advance_next()

        elif action == "click_cursor":
            clicks = step.get("clicks", 1)
            click_jp = step.get("jitter_px", cfg.get("jitter_px", 0))
            click_interval = step.get("interval", cfg.get("interval", 200))
            click_jit_ms = step.get("jitter_ms", cfg.get("jitter_ms", 5))

            host_x, host_y = self.daemon.get_cursor_pos()

            if self.injector.coord_space == "host":
                base_x, base_y = host_x, host_y
            else:
                coords = self.daemon._translate(host_x, host_y)
                if coords is None:
                    self._advance_idx(step.get("then"))
                    self._advance_next()
                    return
                base_x, base_y = coords

            if clicks > 1:
                self._then_idx = step.get("then")
                self._repeat_x = base_x
                self._repeat_y = base_y
                self._repeat_jp = click_jp
                self._repeat_interval = click_interval
                self._repeat_jit_ms = click_jit_ms
                self._repeat_cursor = True
                x, y = base_x, base_y
                if click_jp:
                    x += random.randint(-click_jp, click_jp)
                    y += random.randint(-click_jp, click_jp)
                self.injector.tap(max(0, x), max(0, y))
                self._repeat_remaining = clicks - 1
                jit = (random.random() - 0.5) * 2 * click_jit_ms / 1000.0
                self.next_time = now + max(0.001, click_interval / 1000.0 + jit)
            else:
                x, y = base_x, base_y
                if click_jp:
                    x += random.randint(-click_jp, click_jp)
                    y += random.randint(-click_jp, click_jp)
                self.injector.tap(max(0, x), max(0, y))
                self._advance_idx(step.get("then"))
                self._advance_next()

        elif action == "area":
            if "x" not in step or "y" not in step or "w" not in step or "h" not in step:
                print("skip area: missing x/y/w/h")
                self._advance_idx(step.get("then"))
                self._advance_next()
                return
            clicks = step.get("clicks", 1)
            click_jp = step.get("jitter_px", cfg.get("jitter_px", 0))
            click_interval = step.get("interval", cfg.get("interval", 200))
            click_jit_ms = step.get("jitter_ms", cfg.get("jitter_ms", 5))
            cx = step.get("x", 0)
            cy = step.get("y", 0)
            w = step.get("w", 100)
            h = step.get("h", 100)
            angle = step.get("angle", 0)
            min_dist = step.get("min_dist", 0)
            unique = step.get("unique", False)

            if clicks > 1:
                self._then_idx = step.get("then")
                self._repeat_cx = cx
                self._repeat_cy = cy
                self._repeat_w = w
                self._repeat_h = h
                self._repeat_angle = angle
                self._repeat_jp = click_jp
                self._repeat_interval = click_interval
                self._repeat_jit_ms = click_jit_ms
                self._repeat_min_dist = min_dist
                self._repeat_unique = unique
                self._repeat_area = True
                self._repeat_positions = set() if unique else None
                self._repeat_prev = None
                rx, ry = self._rand_point_in_area(cx, cy, w, h, angle)
                if self._repeat_positions is not None:
                    self._repeat_positions.add((int(rx), int(ry)))
                self._repeat_prev = (rx, ry)
                self._click_at(rx, ry, click_jp)
                self._repeat_remaining = clicks - 1
                if self._repeat_remaining > 0:
                    jit = (random.random() - 0.5) * 2 * click_jit_ms / 1000.0
                    self.next_time = now + max(0.001, click_interval / 1000.0 + jit)
                else:
                    self._advance_idx(step.get("then"))
                    self._advance_next()
            else:
                rx, ry = self._rand_point_in_area(cx, cy, w, h, angle)
                self._click_at(rx, ry, click_jp)
                self._advance_idx(step.get("then"))
                self._advance_next()

        elif action == "wait":
            ms = step.get("ms", 1000)
            wj = step.get("wait_jitter", cfg.get("wait_jitter", 0))
            if wj:
                jit = (random.random() - 0.5) * 2 * wj
                ms = max(0, ms + jit)
            self._advance_idx(step.get("then"))
            self._advance_next(wait_ms=ms)

        elif action == "screencap_check":
            if not cfg.get("screen_cap"):
                self._advance_idx(step.get("then"))
                self._advance_next()
                return
            if "x" not in step or "y" not in step or "w" not in step or "h" not in step:
                print("skip screencap_check: missing x/y/w/h")
                self._advance_idx(step.get("then"))
                self._advance_next()
                return
            cap = screencap_adb(timeout=self._cap_timeout)
            checks = step.get("checks")
            if checks:
                for check in checks:
                    if cap and colour_in_rect(
                        cap,
                        step.get("x", 0), step.get("y", 0),
                        step.get("w", 1), step.get("h", 1),
                        check.get("colour", "000000"),
                        check.get("tol", 15),
                    ):
                        then = check.get("then")
                        self.idx = then if then is not None and then >= 0 else self.idx + 1
                        break
                else:
                    else_idx = step.get("else", -1)
                    self.idx = else_idx if else_idx >= 0 else self.idx + 1
            else:
                if cap and colour_in_rect(
                    cap,
                    step.get("x", 0), step.get("y", 0),
                    step.get("w", 1), step.get("h", 1),
                    step.get("colour", "000000"),
                    step.get("tol", 15),
                ):
                    then = step.get("then")
                    self.idx = then if then is not None and then >= 0 else self.idx + 1
                else:
                    else_idx = step.get("else", -1)
                    self.idx = else_idx if else_idx >= 0 else self.idx + 1
            self._advance_next()

        elif action == "stop":
            self.daemon.active = False

        elif action == "notify":
            self.daemon.platform.notify(step.get("message", ""))
            self._advance_idx(step.get("then"))
            self._advance_next()

        elif action == "log":
            print(step.get("message", ""))
            self._advance_idx(step.get("then"))
            self._advance_next()

        elif action == "run":
            cmd = step.get("cmd")
            timeout_ms = step.get("timeout_ms", 5000)
            try:
                if isinstance(cmd, list):
                    proc = subprocess.Popen(cmd)
                elif isinstance(cmd, str):
                    proc = subprocess.Popen(cmd, shell=True)
                else:
                    self._advance_idx(step.get("then"))
                    self._advance_next()
                    return
                self._then_idx = step.get("then")
                self._run_proc = proc
                self._run_start = now
                self._run_timeout = timeout_ms / 1000.0
            except Exception as e:
                print(f"run error: {e}", file=sys.stderr)
                self._advance_idx(step.get("then"))
                self._advance_next()
            return

        elif action == "run_mode":
            target_name = step.get("mode")
            if target_name and target_name != self.name:
                target = self.daemon.modes.get(target_name)
                if target:
                    self._then_idx = step.get("then")
                    self._run_mode_target = target
                    dur = step.get("duration_ms", 0)
                    self._run_mode_end = float('inf') if dur <= 0 else now + dur / 1000.0
                    target.reset()

                    overrides = {k: v for k, v in step.items()
                                 if k not in ("action", "mode", "duration_ms")}
                    if overrides:
                        self._run_mode_key = target_name
                        self._saved_cfg = dict(target.config)
                        target.config.update(overrides)

                    return

            self._advance_idx(step.get("then"))
            self._advance_next()

        elif action == "zoom":
            if "x" not in step or "y" not in step:
                print("skip zoom: missing x/y")
                self._advance_idx(step.get("then"))
                self._advance_next()
                return
            shared = self.daemon._shared_uinput
            if shared is None:
                self._advance_idx(step.get("then"))
                self._advance_next()
                return
            self._then_idx = step.get("then")
            x = step.get("x", 0)
            y = step.get("y", 0)
            start_pct = max(5, min(95, step.get("start", 10)))
            end_pct = max(5, min(95, step.get("end", 90)))
            duration = step.get("duration", 200)
            dimension = min(self.daemon.host_w, self.daemon.host_h)
            steps_n = max(2, int(duration / 16))
            step_delay = max(0.001, duration / 1000.0 / steps_n)
            center_x, center_y = self.daemon._android_to_host(x, y)
            half_start = dimension * start_pct / 100 / 2
            lx = int(center_x - half_start)
            rx = int(center_x + half_start)
            ui, e = shared
            ui.write(e.EV_ABS, e.ABS_MT_SLOT, 0)
            ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, 1)
            ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, lx)
            ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, center_y)
            ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 100)
            ui.write(e.EV_ABS, e.ABS_MT_SLOT, 1)
            ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, 2)
            ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, rx)
            ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, center_y)
            ui.write(e.EV_ABS, e.ABS_MT_PRESSURE, 100)
            ui.write(e.EV_KEY, e.BTN_TOUCH, 1)
            ui.syn()
            self._zoom_state = {
                "center_x": center_x, "center_y": center_y,
                "dimension": dimension,
                "start_pct": start_pct, "end_pct": end_pct,
                "steps": steps_n, "step": 1, "step_delay": step_delay,
            }
            self.next_time = now + step_delay

        else:
            self._advance_idx(step.get("then"))
            self._advance_next()

        if self.idx >= len(seq):
            if cfg.get("repeat", True):
                self.idx = 0
