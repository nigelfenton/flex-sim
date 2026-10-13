#!/usr/bin/env python3
"""Licensing test: the radio's licence report, set at startup and changed live.

AetherSDR reads three licence objects (FlexLib): `license` (radio_id,
highest_major_version), `license subscription` (smartsdr+ / smartsdr+_early_access
with an expiration date) and `license feature` (name, enabled, reason). It also
treats a `dvk` command refused with 50004001 as "not licensed". Asked for by
K5PTB to test different licensing options against AE.

  1. Defaults: an active SmartSDR+ subscription, DVK enabled (reason plus).
  2. Startup flags: --subscription, --subscription-expires, --license-feature
     (repeatable, with reason), --no-dvk-license, --dvk-refuses.
  3. Live: /license on the control page changes the licence and pushes the new
     status to the connected client, without a reconnect. Bad input is refused
     and changes nothing.

Run: python3 tests/test_licensing.py   (spawns sims on :5997/:8739 and :5998/:8740)
"""

import datetime
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    print(f"[{' OK ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if ok:
        passed += 1
    else:
        failed += 1


class Sim:
    def __init__(self, port, ctl, daxtx, *extra):
        self.port, self.ctl = port, ctl
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "flex_sim.py"), "--ip", "127.0.0.1",
             "--port", str(port), "--ctl-port", str(ctl), *extra],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, cwd=str(ROOT),
            env={**os.environ, "FLEXSIM_DAXTX_PORT": str(daxtx)})

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.ctl}{path}", timeout=5) as r:
            return json.loads(r.read())

    def up(self, deadline=15.0):
        t0 = time.time()
        while time.time() - t0 < deadline:
            try:
                self.get("/state")
                return True
            except Exception:
                time.sleep(0.3)
        return False

    def kill(self):
        self.proc.kill()


class Client:
    def __init__(self, port):
        self.tcp = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.tcp.settimeout(3)
        self.buf = b""
        self.seq = 0
        self.greeting = [self._line(), self._line()]

    def _line(self):
        while b"\n" not in self.buf:
            chunk = self.tcp.recv(4096)
            if not chunk:
                raise ConnectionError("sim closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="replace")

    def drain(self, settle=0.5):
        lines = []
        self.tcp.settimeout(settle)
        try:
            while True:
                lines.append(self._line())
        except socket.timeout:
            pass
        finally:
            self.tcp.settimeout(3)
        return lines

    def cmd(self, text, settle=0.3):
        self.seq += 1
        self.tcp.sendall(f"C{self.seq}|{text}\n".encode())
        lines = []
        while True:
            line = self._line()
            lines.append(line)
            if line.startswith(f"R{self.seq}|"):
                break
        return lines + self.drain(settle)

    def close(self):
        self.tcp.close()


def objs(lines, prefix):
    """key=value dicts of status lines whose object starts with `prefix ` (exact object)."""
    out = []
    for line in lines:
        if line.startswith("S") and "|" in line:
            body = line.split("|", 1)[1]
            head = body.split(" name=")[0] if prefix != "license" else body.split(" ")[0]
            if body.startswith(prefix + " ") and (prefix != "license" or head == "license"):
                out.append(dict(t.split("=", 1) for t in body.split() if "=" in t))
    return out


def reply_code(lines, seq):
    return int(next(l for l in lines if l.startswith(f"R{seq}|")).split("|")[1], 16)


def main():
    today = datetime.date.today()
    expired = (today - datetime.timedelta(days=30)).isoformat()
    a = Sim(5997, 8739, 4997)
    b = Sim(5998, 8740, 4998, "--subscription", "early-access", "--subscription-expires", expired,
            "--license-feature", "digital_voice_keyer=1:built_in",
            "--license-feature", "multiflex=0", "--dvk-refuses")
    bad = subprocess.run([sys.executable, str(ROOT / "flex_sim.py"), "--license-feature", "dvk=yes",
                          "--port", "5999"], capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    try:
        if not (a.up() and b.up()):
            print("FAIL: sim control panel never came up")
            return 1

        # ---- 1. defaults ----
        c = Client(5997)
        lines = c.cmd("sub license all")
        lic = objs(lines, "license")
        sub = objs(lines, "license subscription")
        feat = objs(lines, "license feature")
        check("license object carries radio_id and highest_major_version",
              bool(lic) and lic[0].get("radio_id") and lic[0].get("highest_major_version") == "4", str(lic))
        check("default subscription: smartsdr+, active (expires after today)",
              bool(sub) and sub[0].get("name") == "smartsdr+"
              and sub[0].get("expiration", "")[:10] > today.isoformat(), str(sub))
        check("default DVK feature enabled with reason plus",
              any(f.get("name") == "digital_voice_keyer" and f.get("enabled") == "1"
                  and f.get("reason") == "plus" for f in feat), str(feat))
        lines = c.cmd("dvk set_name 1 test")
        check("dvk commands accepted when not refusing", reply_code(lines, c.seq) == 0)

        # ---- 3. live changes reach the connected client ----
        r = a.get("/license?feature=digital_voice_keyer&enabled=0")
        pushed = c.drain()
        f = objs(pushed, "license feature")
        check("live: DVK off is pushed to the client without a reconnect",
              r.get("ok") and any(x.get("name") == "digital_voice_keyer" and x.get("enabled") == "0"
                                  for x in f), str(pushed[-3:]))
        a.get("/license?subscription=none")
        pushed = c.drain()
        check("live: subscription none sends no subscription line",
              not objs(pushed, "license subscription") and objs(pushed, "license feature"), str(pushed))
        a.get(f"/license?subscription=smartsdr%2B&expires={expired}")
        pushed = c.drain()
        s2 = objs(pushed, "license subscription")
        check("live: an expired smartsdr+ subscription is reported with its past date",
              bool(s2) and s2[0].get("expiration", "")[:10] == expired, str(s2))
        a.get("/license?dvk_refuses=1")
        c.drain()
        lines = c.cmd("dvk record 1")
        check("live: dvk_refuses=1 refuses dvk commands with 50004001",
              reply_code(lines, c.seq) == 0x50004001, str(lines[:2]))
        before = a.get("/license")["license"]
        for q in ("feature=dvk%20x&enabled=1", "feature=digital_voice_keyer&enabled=2",
                  "subscription=gold", "expires=2026-13-40", "dvk_refuses=yes"):
            r = a.get("/license?" + q)
            check(f"live: bad input refused ({q})", r.get("ok") is False and r.get("error"), str(r))
        check("bad input changed nothing", a.get("/license")["license"] == before)
        check("/state carries the licence", a.get("/state")[0].get("license") == before)
        c.close()

        # ---- 2. startup flags ----
        d = Client(5998)
        lines = d.cmd("sub license all")
        sub = objs(lines, "license subscription")
        feat = {f.get("name"): f for f in objs(lines, "license feature")}
        check("--subscription early-access is reported as smartsdr+_early_access",
              bool(sub) and sub[0].get("name") == "smartsdr+_early_access", str(sub))
        check("--subscription-expires sets the expiry date",
              bool(sub) and sub[0].get("expiration", "")[:10] == expired, str(sub))
        check("--license-feature sets enabled and reason",
              feat.get("digital_voice_keyer", {}).get("reason") == "built_in"
              and feat.get("multiflex", {}).get("enabled") == "0", str(feat))
        lines = d.cmd("dvk record 1")
        check("--dvk-refuses refuses dvk commands with 50004001", reply_code(lines, d.seq) == 0x50004001)
        d.close()

        check("a malformed --license-feature is rejected at startup",
              bad.returncode == 2 and "NAME=0|1" in bad.stderr, bad.stderr[-200:])

        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        a.kill()
        b.kill()


def test_licensing():
    """pytest entry point. Skips rather than fails when the sims cannot start."""
    rc = main()
    if rc == 0:
        return
    import pytest
    if passed == 0 and failed == 0:
        pytest.skip("flex_sim could not be started (ports 5997/8739 or 5998/8740 busy?)")
    assert rc == 0, f"{failed} licensing check(s) failed — see output above"


if __name__ == "__main__":
    sys.exit(main())
