#!/usr/bin/env python3
"""Multi-client test: a GUI client (AE's role) plus a bound non-GUI controller.

A real FlexRadio serves several API clients at once. The CTR2-Max front panel
reaches the radio through AetherSDR's CTR2 relay as a SECOND TCP connection
that never sends `client gui`: it watches `sub client all`, binds to the GUI
client with `client bind client_id=<uuid>`, then drives that client's slices.
Single-client, the sim never answered that second connection.

Checks:
  1. each connection gets V<version> and its own H<handle>;
  2. the controller sees the GUI client's `client 0x.. connected` status (and
     the GUI client never sees one for the non-GUI controller);
  3. bind succeeds (and an unknown client_id is refused);
  4. the controller tunes / sets mode / audio_level / filter on the GUI
     client's slice and the GUI client receives each resulting slice status,
     still carrying ITS client_handle;
  5. the controller keys TX; the GUI client sees it, owned by the GUI client;
  6. per-client cleanup: the controller leaving unkeys only what it keyed and
     leaves the GUI client's slice; another client coming and going does not
     unkey the GUI client's TX; the GUI client leaving frees its slice, unkeys,
     and the remaining controller sees `client 0x.. disconnected`;
  7. a non-GUI client's `client udpport` receives meter packets (PCC 0x8002).

Run: python3 tests/test_multiclient.py   (spawns its own sim on :5998/:8738)
"""

import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT, CTL = 5998, 8738
DAXTX_PORT = 4996          # not 4991/4993/4994: a running sim or another test may own those
ROOT = Path(__file__).resolve().parent.parent

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    print(f"[{' OK ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if ok:
        passed += 1
    else:
        failed += 1


def sim_state():
    with urllib.request.urlopen(f"http://127.0.0.1:{CTL}/state", timeout=5) as r:
        return json.loads(r.read())[0]


def wait_ctl(deadline=15.0):
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            sim_state()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_until(pred, deadline=3.0):
    t0 = time.time()
    while time.time() - t0 < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


class Client:
    """Fake API client. Every line received is kept in self.seen, so status
    pushed by ANOTHER client's command can be waited for."""

    def __init__(self):
        self.tcp = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        self.buf = b""
        self.seq = 0
        self.seen = []
        self.greeting = [self._line(5), self._line(5)]     # V<version>, H<handle>
        self.handle = self.greeting[1][1:]

    def _line(self, timeout):
        self.tcp.settimeout(timeout)
        while b"\n" not in self.buf:
            chunk = self.tcp.recv(4096)
            if not chunk:
                raise ConnectionError("sim closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        line = line.decode(errors="replace")
        self.seen.append(line)
        return line

    def pump(self, settle=0.3):
        try:
            while True:
                self._line(settle)
        except socket.timeout:
            pass

    def cmd(self, text, settle=0.3):
        """Send one command; return the lines up to its reply plus any that follow within `settle`."""
        self.seq += 1
        start = len(self.seen)
        self.tcp.sendall(f"C{self.seq}|{text}\n".encode())
        while not self._line(5).startswith(f"R{self.seq}|"):
            pass
        self.pump(settle)
        return self.seen[start:]

    def reply(self, lines):
        r = next(line for line in lines if line.startswith(f"R{self.seq}|"))
        code, body = r.split("|", 2)[1:]
        return int(code, 16), body

    def wait_for(self, pattern, deadline=3.0, since=0):
        rx = re.compile(pattern)
        t0 = time.time()
        while time.time() - t0 < deadline:
            hit = next((line for line in self.seen[since:] if rx.search(line)), None)
            if hit:
                return hit
            self.pump(0.1)
        return next((line for line in self.seen[since:] if rx.search(line)), None)

    def close(self):
        self.tcp.close()


def kv(line):
    return dict(t.split("=", 1) for t in line.split("|", 1)[1].split() if "=" in t)


def main():
    sim = subprocess.Popen(
        [sys.executable, str(ROOT / "flex_sim.py"), "--ip", "127.0.0.1",
         "--port", str(PORT), "--ctl-port", str(CTL)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, cwd=str(ROOT),
        env={**os.environ, "FLEXSIM_DAXTX_PORT": str(DAXTX_PORT)})
    clients = []
    try:
        if not wait_ctl():
            print("FAIL: sim control panel never came up")
            return 1

        # ---- GUI client (AE's connect sequence, trimmed) ----
        gui = Client(); clients.append(gui)
        gui_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        gui_udp.bind(("127.0.0.1", 0))
        for c in ("sub client all", "client program AetherSDR"):
            gui.cmd(c)
        code, cid = gui.reply(gui.cmd("client gui"))
        check("GUI: 'client gui' replies a client_id", code == 0 and len(cid) == 36, cid)
        for c in ("client station AE", "sub slice all", "sub tx all",
                  f"client udpport {gui_udp.getsockname()[1]}",
                  "display panafall create x=100 y=100",
                  "slice create pan=0x40000000 freq=14.100000"):
            gui.cmd(c)

        # ---- 1. controller connects: its own V/H ----
        ctl = Client(); clients.append(ctl)
        check("controller: gets V<version>", ctl.greeting[0].startswith("V"), ctl.greeting[0])
        check("controller: gets its own H<handle>",
              ctl.greeting[1].startswith("H") and ctl.handle != gui.handle,
              f"{gui.greeting} {ctl.greeting}")

        # ---- 2. sees the GUI client ----
        lines = ctl.cmd("sub client all")
        st = next((line for line in lines if f"|client 0x{gui.handle} connected" in line), None)
        check("controller: 'sub client all' reports the GUI client connected", st is not None, str(lines))
        st = kv(st) if st else {}
        check("controller: client status carries client_id/program/station",
              st.get("client_id") == cid and st.get("program") == "AetherSDR"
              and st.get("station") == "AE", str(st))
        lines = ctl.cmd("sub slice all")
        s0 = [line for line in lines if "|slice 0 " in line]
        check("controller: 'sub slice all' reports the existing slice, owned by the GUI client",
              s0 and kv(s0[-1]).get("client_handle") == f"0x{gui.handle}", str(lines))
        ctl.cmd("sub tx all")

        # ---- 3. bind ----
        code, _ = ctl.reply(ctl.cmd("client bind client_id=00000000-0000-0000-0000-000000000000"))
        check("bind to an unknown client_id is refused", code != 0)
        code, _ = ctl.reply(ctl.cmd(f"client bind client_id={cid}"))
        check("bind to the GUI client's client_id succeeds", code == 0)
        st = sim_state()
        me = next((c for c in st["clients"] if c["handle"] == f"0x{ctl.handle}"), {})
        check("/state shows the controller bound, non-GUI",
              me.get("bound_to") == f"0x{gui.handle}" and me.get("gui") is False, str(st["clients"]))

        # ---- 4. controller drives the GUI client's slice ----
        mark = len(gui.seen)
        ctl.cmd("slice tune 0 14.200000")
        hit = gui.wait_for(r"\|slice 0 .*RF_frequency=14\.200000", since=mark)
        check("controller 'slice tune': GUI client receives the slice status", hit is not None)
        check("...still with the GUI client's client_handle",
              hit is not None and kv(hit).get("client_handle") == f"0x{gui.handle}", str(hit))
        for cmd, pat in (("slice set 0 mode=CW", r"\|slice 0 .*mode=CW"),
                         ("slice set 0 audio_level=30", r"\|slice 0 .*audio_level=30"),
                         ("filt 0 100 500", r"\|slice 0 .*filter_lo=100 filter_hi=500")):
            mark = len(gui.seen)
            ctl.cmd(cmd)
            check(f"controller '{cmd}': GUI client receives it",
                  gui.wait_for(pat, since=mark) is not None)
        check("GUI client never hears of the non-GUI controller as a client",
              not any(f"client 0x{ctl.handle}" in line for line in gui.seen))

        # ---- 7. meters to the controller's udpport ----
        ctl_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ctl_udp.bind(("127.0.0.1", 0))
        ctl_udp.settimeout(3)
        ctl.cmd(f"client udpport {ctl_udp.getsockname()[1]}")
        pcc = None
        try:
            data = ctl_udp.recv(2048)
            pcc = struct.unpack(">H", data[14:16])[0] if len(data) >= 16 else None
        except socket.timeout:
            pass
        check("non-GUI client's udpport receives meter packets (PCC 0x8002)", pcc == 0x8002,
              f"pcc={pcc}")
        ctl_udp.close()

        # ---- 5. controller keys TX ----
        mark = len(gui.seen)
        code, _ = ctl.reply(ctl.cmd("xmit 1"))
        hit = gui.wait_for(r"\|interlock state=TRANSMITTING", since=mark)
        check("controller 'xmit 1': GUI client sees TRANSMITTING", code == 0 and hit is not None)
        check("...with the GUI client as tx_client_handle",
              hit is not None and kv(hit).get("tx_client_handle") == f"0x{gui.handle}", str(hit))

        # ---- 6. per-client cleanup ----
        mark = len(gui.seen)
        ctl.close(); clients.remove(ctl)
        check("controller leaving while it keyed: GUI client sees READY",
              gui.wait_for(r"\|interlock state=READY", since=mark) is not None)
        check("...and the radio is unkeyed", wait_until(lambda: sim_state()["tx"] is False))
        st = sim_state()
        check("...but the GUI client's slice survives", st["slices"] == 1, str(st["slices"]))
        check("...and only one client remains", len(st["clients"]) == 1, str(st["clients"]))

        ctl2 = Client(); clients.append(ctl2)
        ctl2.cmd("sub client all")
        gui.cmd("transmit set mox=1")
        other = Client()
        other.cmd("ping")
        other.close()
        time.sleep(0.5)
        check("another client coming and going leaves the GUI client's TX keyed",
              sim_state()["tx"] is True)

        gui.close(); clients.remove(gui)
        check("GUI client leaving: the controller sees 'client .. disconnected'",
              ctl2.wait_for(rf"\|client 0x{gui.handle} disconnected") is not None)
        check("...its slice is freed", wait_until(lambda: sim_state()["slices"] == 0))
        check("...and its TX unkeyed", sim_state()["tx"] is False)

        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        for c in clients:
            try:
                c.close()
            except OSError:
                pass
        sim.kill()


def test_multiclient():
    """pytest entry point. One ordered walk (one sim): later checks depend on
    the slice and TX state earlier ones set up. Skips rather than fails when
    the sim cannot start (ports busy) — an environment fact."""
    rc = main()
    if rc == 0:
        return
    import pytest
    if passed == 0 and failed == 0:
        pytest.skip(f"flex_sim could not be started (ports {PORT}/{CTL}/{DAXTX_PORT} busy?)")
    assert rc == 0, f"{failed} multi-client check(s) failed — see output above"


if __name__ == "__main__":
    sys.exit(main())
