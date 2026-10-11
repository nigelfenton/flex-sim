#!/usr/bin/env python3
"""TX audio-state test: the radio behaviours AE's client-side voice keyer
(aethersdr/AetherSDR#6292) leans on, found testing it against the sim (2026-10-11).

  1. `mic list` includes PC, `mic input <src>` selects it, and the transmit
     status reports `mic_selection=` and `dax=` so the client reads them back.
     #6292 refuses to record unless the radio says the mic source is PC.
  2. `sub license all` reports `license feature name=digital_voice_keyer
     enabled=0|1` (`--no-dvk-license` turns it off). AE fails open without it,
     so every sim looked licensed and the unlicensed path was untestable.
  3. `stream create type=remote_audio_tx` gets its own id; it shared netcw's.
  4. `sub tx all` is answered with the current TX state, so a reconnecting
     client learns the radio is keyed.
  5. A client that disconnects while keyed is unkeyed, as on a real radio.

Run: python3 tests/test_tx_audio_state.py   (spawns its own sims on :5995/:8735 and :5996/:8736)
"""

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

    def state(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.ctl}/state", timeout=5) as r:
            return json.loads(r.read())[0]

    def up(self, deadline=15.0):
        t0 = time.time()
        while time.time() - t0 < deadline:
            try:
                self.state()
                return True
            except Exception:
                time.sleep(0.3)
        return False

    def kill(self):
        self.proc.kill()


class Client:
    """Fake API client: sends C<seq>|cmd, returns every line up to R<seq>."""

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

    def cmd(self, text, settle=0.3):
        self.seq += 1
        self.tcp.sendall(f"C{self.seq}|{text}\n".encode())
        lines = []
        while True:
            line = self._line()
            lines.append(line)
            if line.startswith(f"R{self.seq}|"):
                break
        self.tcp.settimeout(settle)
        try:
            while True:
                lines.append(self._line())
        except socket.timeout:
            pass
        finally:
            self.tcp.settimeout(3)
        return lines

    def close(self):
        self.tcp.close()


def reply_body(lines, seq):
    return next(line for line in lines if line.startswith(f"R{seq}|")).split("|", 2)[2]


def status_kvs(lines, prefix):
    """key=value dicts of the status lines whose object starts with `prefix`."""
    out = []
    for line in lines:
        if line.startswith("S") and "|" in line:
            body = line.split("|", 1)[1]
            if body.startswith(prefix):
                out.append(dict(t.split("=", 1) for t in body.split() if "=" in t))
    return out


def main():
    sim = Sim(5995, 8735, 4995)
    unlicensed = Sim(5996, 8736, 4996, "--no-dvk-license")
    try:
        if not (sim.up() and unlicensed.up()):
            print("FAIL: sim control panel never came up")
            return 1

        c = Client(5995)

        # ---- 1. mic source ----
        lines = c.cmd("mic list")
        check("mic list includes PC", "PC" in reply_body(lines, c.seq).split(","), reply_body(lines, c.seq))
        lines = c.cmd("mic input PC")
        tx = status_kvs(lines, "transmit ")
        check("mic input PC is reported back as mic_selection=PC",
              bool(tx) and tx[-1].get("mic_selection") == "PC", str(tx))
        check("the transmit status also carries dax=", bool(tx) and tx[-1].get("dax") == "0", str(tx))
        lines = c.cmd("transmit set dax=1")
        tx = status_kvs(lines, "transmit ")
        check("dax=1 is echoed in the transmit status", bool(tx) and tx[-1].get("dax") == "1", str(tx))
        c.cmd("transmit set dax=0")

        # ---- 2. licence ----
        lines = c.cmd("sub license all")
        lic = status_kvs(lines, "license feature")
        check("licensed sim reports digital_voice_keyer enabled=1",
              any(d.get("name") == "digital_voice_keyer" and d.get("enabled") == "1" for d in lic), str(lic))
        u = Client(5996)
        lines = u.cmd("sub license all")
        lic = status_kvs(lines, "license feature")
        check("--no-dvk-license reports digital_voice_keyer enabled=0",
              any(d.get("name") == "digital_voice_keyer" and d.get("enabled") == "0" for d in lic), str(lic))
        u.close()

        # ---- 3. stream ids ----
        netcw = reply_body(c.cmd("stream create netcw"), c.seq)
        rtx = reply_body(c.cmd("stream create type=remote_audio_tx compression=opus"), c.seq)
        check("remote_audio_tx gets an id of its own", rtx.startswith("0x") and rtx != netcw,
              f"netcw={netcw} remote_audio_tx={rtx}")

        # ---- 4 + 5. keyed client drops; the next client sees RX ----
        c.cmd("xmit 1")
        check("sim keyed", sim.state()["tx"] is True)
        c.close()
        t0 = time.time()
        while time.time() - t0 < 5 and sim.state()["tx"]:
            time.sleep(0.1)
        check("client disconnecting while keyed unkeys the radio", sim.state()["tx"] is False)

        c2 = Client(5995)
        lines = c2.cmd("sub tx all")
        tx = status_kvs(lines, "transmit ")
        il = status_kvs(lines, "interlock ")
        check("sub tx all is answered with the TX state",
              bool(tx) and tx[-1].get("mox") == "0" and bool(il) and il[-1].get("state") == "READY",
              f"{tx} {il}")
        c2.cmd("xmit 1")
        lines = c2.cmd("sub tx all")
        il = status_kvs(lines, "interlock ")
        check("...and reports TRANSMITTING while keyed", bool(il) and il[-1].get("state") == "TRANSMITTING", str(il))
        c2.cmd("xmit 0")
        c2.close()

        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        sim.kill()
        unlicensed.kill()


def test_tx_audio_state():
    """pytest entry point. Skips rather than fails when the sims cannot start
    (ports busy) — an environment fact, not a regression."""
    rc = main()
    if rc == 0:
        return
    import pytest
    if passed == 0 and failed == 0:
        pytest.skip("flex_sim could not be started (ports 5995/8735/4995 or 5996/8736/4996 busy?)")
    assert rc == 0, f"{failed} tx-audio-state check(s) failed — see output above"


if __name__ == "__main__":
    sys.exit(main())
