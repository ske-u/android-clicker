#!/usr/bin/env python3
"""Test app-process injector on connected ADB device.

Usage:
    python test_appprocess.py --linux           # Waydroid
    python test_appprocess.py --windows         # BlueStacks

Before running:
    - Place injector.jar in same directory (compile from AppProcessInjector.java)
    - ADB must be on PATH
"""
import argparse
import os
import socket
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
JAR = os.path.join(SCRIPT_DIR, "injector.jar")
REMOTE = "/data/local/tmp/injector.jar"
PORT = 17000

ADDR_MAP = {
    "linux": "192.168.240.112:5555",
    "windows": "127.0.0.1:5555",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", default="adb")
    ap.add_argument("--local-jar", default=JAR,
                    help="Path to injector.jar (default: %s)" % JAR)
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
    proc = None
    sock = None

    def step(msg):
        print(f"  \u2022 {msg}...", end=" ", flush=True)

    def fail(msg):
        nonlocal ok
        ok = False
        print(f"FAIL: {msg}")

    try:
        # --- connect ADB ---
        step("connect ADB")
        r = subprocess.run([adb, "connect", addr],
                           capture_output=True, text=True, timeout=5)
        if "connected" not in r.stdout and "already" not in r.stdout:
            return fail(f"ADB connect failed: {r.stdout.strip()}")
        print("OK")

        # --- push JAR ---
        step("push injector.jar")
        r = subprocess.run([adb, "push", args.local_jar, REMOTE],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return fail(f"push failed: {r.stderr.strip()}")
        print("OK")

        # --- forward port ---
        step("forward port")
        subprocess.run([adb, "forward", f"tcp:{PORT}", f"tcp:{PORT}"],
                       capture_output=True, timeout=5)
        print("OK")

        # --- launch app_process ---
        step("launch app_process")
        proc = subprocess.Popen(
            [adb, "shell",
             f"CLASSPATH={REMOTE}",
             "app_process", "/", "com.clicker.Injector", str(PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for i in range(10):
            time.sleep(0.5)
            try:
                sock = socket.create_connection(("127.0.0.1", PORT), timeout=2)
                break
            except ConnectionRefusedError:
                continue
        else:
            return fail("app_process did not start in 5s")
        print("OK")

        # --- TAP TEST ---
        step("TAP: send tap at (500,500)")
        sock.sendall(b"T 500 500\n")
        resp = sock.recv(1024).decode().strip()
        if resp == "OK":
            print("PASS  (check screen for tap at ~500,500)")
        else:
            fail(f"expected OK, got {resp!r}")

        time.sleep(1)

        # --- ZOOM TEST ---
        step("ZOOM: server-side pinch at (540,960)")
        cx, cy, dim = 540, 960, 1080
        sock.sendall(f"Z {cx} {cy} 10 50 {dim:.0f} 10 16\n".encode())
        resp = sock.recv(1024).decode().strip()
        if resp == "OK":
            print("PASS  (check screen for pinch zoom)")
        else:
            fail(f"expected OK, got {resp!r}")

    except Exception as e:
        fail(f"exception: {e}")
    finally:
        step("cleanup")
        try:
            if sock:
                sock.sendall(b"Q\n")
                sock.recv(1024)
                sock.close()
        except Exception:
            pass
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        subprocess.run([adb, "forward", "--remove", f"tcp:{PORT}"],
                       capture_output=True, timeout=3)
        print("OK")

    print(f"\n{'=== ALL TESTS PASSED ===' if ok else '=== SOME TESTS FAILED ==='}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
