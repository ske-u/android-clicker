#!/usr/bin/env python3
"""Test minitouch injector on connected ADB device.

Usage:
    python test_minitouch.py --linux           # Waydroid
    python test_minitouch.py --windows         # BlueStacks

Before running:
    - Place minitouch_x86_64 binary in same directory (from OpenSTF/minitouch)
    - ADB must be on PATH
"""
import argparse
import os
import socket
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
BINARY = os.path.join(SCRIPT_DIR, "minitouch_x86_64")
REMOTE = "/data/local/tmp/minitouch"
PORT = 17171

ADDR_MAP = {
    "linux": "192.168.240.112:5555",
    "windows": "127.0.0.1:5555",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", default="adb")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--linux", action="store_true", help="Waydroid (%s)" % ADDR_MAP["linux"])
    group.add_argument("--windows", action="store_true", help="BlueStacks (%s)" % ADDR_MAP["windows"])
    args = ap.parse_args()
    if args.linux:
        addr = ADDR_MAP["linux"]
    elif args.windows:
        addr = ADDR_MAP["windows"]
    else:
        addr = ADDR_MAP["windows"]
    adb = args.adb
    ok = True
    sock = None

    def adb_run(cmd_str, **kw):
        cmd = cmd_str.split() if isinstance(cmd_str, str) else cmd_str
        return subprocess.run([adb] + cmd, capture_output=True, text=True,
                              timeout=10, **kw)

    def step(msg):
        print(f"  \u2022 {msg}...", end=" ", flush=True)

    def fail(msg):
        nonlocal ok
        ok = False
        print(f"FAIL: {msg}")

    def check(name, cond, detail=""):
        nonlocal ok
        if cond:
            print(f"PASS")
        else:
            ok = False
            print(f"FAIL" + (f"  ({detail})" if detail else ""))

    try:
        # --- connect ADB ---
        step("connect ADB")
        r = subprocess.run([adb, "connect", addr],
                           capture_output=True, text=True, timeout=5)
        check("connect", "connected" in r.stdout or "already" in r.stdout,
              r.stdout.strip())

        # --- push binary ---
        step("push minitouch")
        r = adb_run(f"push {BINARY} {REMOTE}")
        if r.returncode != 0:
            return fail(f"push failed: {r.stderr.strip()}")
        adb_run(f"shell chmod 755 {REMOTE}")
        print("OK")

        # --- forward port ---
        step("forward port")
        adb_run(f"forward tcp:{PORT} localabstract:minitouch")
        print("OK")

        # --- start minitouch ---
        step("start minitouch")
        subprocess.Popen([adb, "shell", REMOTE],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        print("OK")

        # --- connect and read handshake ---
        step("connect socket")
        sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        hb = sock.recv(1024)
        hdr = hb.decode().strip()
        print(f"OK  handshake: {hdr.split(chr(10))[0]}")

        # --- TAP TEST ---
        step("TAP: send tap at (500,500)")
        sock.sendall(b"d 0 500 500 50\nc\nu 0\nc\n")
        print("SENT  (check screen for tap at ~500,500)")

        time.sleep(1)

        # --- ZOOM TEST ---
        step("ZOOM: 2-finger pinch centered at (540,960)")
        cx, cy, dim = 540, 960, 1080  # min(1080,1920) for a typical FHD display
        start_pct, end_pct = 10, 50
        steps = 10
        half0 = dim * start_pct / 100 / 2
        sock.sendall(
            f"i 0 {cx - half0:.0f} {cy} 50\n"
            f"i 1 {cx + half0:.0f} {cy} 50\nc\n".encode()
        )
        for s in range(1, steps + 1):
            pct = start_pct + (end_pct - start_pct) * (s / steps)
            half = dim * pct / 100 / 2
            x1 = int(cx - half)
            x2 = int(cx + half)
            sock.sendall(f"m 0 {x1} {cy} 50\nm 1 {x2} {cy} 50\nc\n".encode())
            time.sleep(0.016)
        sock.sendall(b"u 0\nu 1\nc\n")
        print("SENT  (check screen for pinch zoom)")

        time.sleep(0.5)
        print("  Done. Observe the device screen for results.")

    except Exception as e:
        fail(f"exception: {e}")
    finally:
        step("cleanup")
        try:
            if sock:
                sock.close()
        except Exception:
            pass
        subprocess.run([adb, "forward", "--remove", f"tcp:{PORT}"],
                       capture_output=True, timeout=3)
        subprocess.run(
            [adb, "shell", "killall -9 minitouch 2>/dev/null || true"],
            capture_output=True, timeout=3,
        )
        print("OK")

    print(f"\n{'=== ALL TESTS PASSED ===' if ok else '=== SOME TESTS FAILED ==='}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
