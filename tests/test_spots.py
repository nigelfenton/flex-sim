#!/usr/bin/env python3
"""Radio-side spots: what a spot-pushing program (DXLab Commander, N1MM, ...) and a GUI client see.

Wire contract from FlexLib (Radio.RequestSpot, Spot.cs, Radio.ParseSpotStatus):
  host -> radio  spot add callsign=<c> rx_freq=<MHz> [tx_freq= mode= color= ... trigger_action=]
                 spot set <i> k=v | spot remove <i> | spot clear | spot trigger <i> [pan=0x<hex>]
  radio -> host  spot <i> k=v ...  |  spot <i> removed  |  spot <i> triggered [pan=0x<hex>]
Spaces inside callsign/comment travel as 0x7f.

Checks:
  1. a second client ("Commander") adds a spot; the reply carries its index; the GUI client and
     any client that sent 'sub spot all' get 'spot <i> ...' with the fields FlexLib sends, with
     FlexLib's defaults (priority 5, trigger_action tune) when the adder leaves them out;
  2. a client that neither is the GUI client nor subscribed hears nothing;
  3. the GUI client triggers it with pan=: the adder hears 'spot <i> triggered pan=0x...', and
     trigger_action=tune retunes the GUI client's slice to rx_freq (and sets the mode);
  4. trigger_action=none leaves the slice alone;
  5. 'spot set' updates a field and reports it; 'spot remove' and 'spot clear' report removed;
  6. lifetime_seconds expires a spot by itself (removed status, gone from the radio);
  7. refusals: no rx_freq, unknown index, a non-numeric frequency.

Run: python3 tests/test_spots.py   (spawns its own sim on :5996/:8745)
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT, CTL, DAXTX_PORT = 5996, 8745, 4995
ROOT = Path(__file__).resolve().parent.parent
SP = "\x7f"

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    print(f"[{' OK ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if ok:
        passed += 1
    else:
        failed += 1


def wait_ctl(deadline=15.0):
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{CTL}/state", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.3)
    return False


class Client:
    def __init__(self):
        self.tcp = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        self.buf, self.seq, self.seen = b"", 0, []
        self.greeting = [self._line(5), self._line(5)]
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

    def mark(self):
        return len(self.seen)

    def wait_for(self, pattern, since=0, deadline=3.0):
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
        [sys.executable, str(ROOT / "flex_sim.py"), "--ip", "127.0.0.1", "--port", str(PORT), "--ctl-port", str(CTL)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, cwd=str(ROOT),
        env={**os.environ, "FLEXSIM_DAXTX_PORT": str(DAXTX_PORT)})
    clients = []
    try:
        if not wait_ctl():
            print("FAIL: sim control panel never came up")
            return 1
        gui = Client(); clients.append(gui)
        gui.cmd("client program AetherSDR")
        gui.cmd("client gui")
        for c in ("sub spot all", "sub slice all", "display panafall create x=100 y=100",
                  "slice create pan=0x40000000 freq=14.100000"):
            gui.cmd(c)
        cmdr = Client(); clients.append(cmdr)          # the spot pusher, e.g. DXLab Commander
        cmdr.cmd("client program Commander")
        cmdr.cmd("sub spot all")
        lurker = Client(); clients.append(lurker)      # neither GUI nor subscribed
        lurker.cmd("client program Lurker")

        # ---- 1. add ----
        g0, l0 = gui.mark(), lurker.mark()
        lines = cmdr.cmd(f"spot add callsign=W1AW rx_freq=14.074 mode=FT8 color=#FFFF0000 "
                         f"source=SpotCollector spotter_callsign=K1TTT comment=cq{SP}de{SP}w1aw lifetime_seconds=600")
        code, body = cmdr.reply(lines)
        check("add: reply OK and carries the new index", code == 0 and body.isdigit(), f"{code:X} {body!r}")
        idx = int(body) if body.isdigit() else -1
        st = gui.wait_for(rf"\|spot {idx} callsign=", since=g0)
        check("add: the GUI client gets 'spot <i> callsign=...'", st is not None, str(gui.seen[g0:]))
        f = kv(st) if st else {}
        check("add: rx_freq is normalised to MHz with 6 decimals, tx_freq defaults to it",
              f.get("rx_freq") == "14.074000" and f.get("tx_freq") == "14.074000", str(f))
        check("add: fields are echoed, 0x7f spaces intact",
              f.get("mode") == "FT8" and f.get("source") == "SpotCollector" and f.get("spotter_callsign") == "K1TTT"
              and f.get("comment") == f"cq{SP}de{SP}w1aw" and f.get("color") == "#FFFF0000", str(f))
        check("add: FlexLib defaults fill in (priority 5, trigger_action tune, a timestamp)",
              f.get("priority") == "5" and f.get("trigger_action") == "tune" and f.get("timestamp", "").isdigit(), str(f))
        check("add: the adder (subscribed) hears it too", cmdr.wait_for(rf"\|spot {idx} callsign=W1AW") is not None)
        lurker.pump(0.4)
        check("2. an unsubscribed non-GUI client hears nothing", not any("|spot " in x for x in lurker.seen[l0:]),
              str(lurker.seen[l0:]))

        # ---- 3. trigger with tune ----
        c0, g1 = cmdr.mark(), gui.mark()
        gui.cmd(f"spot trigger {idx} pan=0x40000000")
        trig = cmdr.wait_for(rf"\|spot {idx} triggered pan=0x40000000", since=c0)
        check("3. trigger: the adder hears 'spot <i> triggered pan=0x40000000'", trig is not None, str(cmdr.seen[c0:]))
        sl = gui.wait_for(r"\|slice 0 .*RF_frequency=14\.074", since=g1)
        check("3. trigger_action=tune: the GUI client's slice moves to the spot", sl is not None, str(gui.seen[g1:]))
        check("3. ... and takes the spot's mode", sl is not None and kv(sl).get("mode") == "FT8",
              str(kv(sl)) if sl else "")

        # ---- 4. trigger_action=none ----
        code, body = cmdr.reply(cmdr.cmd("spot add callsign=K1ABC rx_freq=7.030 trigger_action=none"))
        idx2 = int(body)
        g2 = gui.mark()
        gui.cmd(f"spot trigger {idx2} pan=0x40000000")
        gui.pump(0.4)
        check("4. trigger_action=none: triggered is reported but the slice does not move",
              cmdr.wait_for(rf"\|spot {idx2} triggered") is not None
              and not any("|slice 0 " in x and "RF_frequency=7.03" in x for x in gui.seen[g2:]), str(gui.seen[g2:]))

        # ---- 5. set / remove / clear ----
        g3 = gui.mark()
        code, _ = cmdr.reply(cmdr.cmd(f"spot set {idx} comment=QRV{SP}now"))
        st = gui.wait_for(rf"\|spot {idx} comment=QRV{SP}now", since=g3)
        check("5. set: OK and reported", code == 0 and st is not None, str(gui.seen[g3:]))
        g4 = gui.mark()
        code, _ = cmdr.reply(cmdr.cmd(f"spot remove {idx2}"))
        check("5. remove: OK and 'spot <i> removed' reported",
              code == 0 and gui.wait_for(rf"\|spot {idx2} removed", since=g4) is not None, str(gui.seen[g4:]))
        cmdr.cmd("spot add callsign=G0JKN rx_freq=21.074")
        g5 = gui.mark()
        code, _ = cmdr.reply(cmdr.cmd("spot clear"))
        gui.pump(0.4)
        removed = [x for x in gui.seen[g5:] if x.endswith(" removed")]
        check("5. clear: removes every remaining spot", code == 0 and len(removed) == 2, str(removed))

        # ---- 6. lifetime ----
        code, body = cmdr.reply(cmdr.cmd("spot add callsign=EXPIRE rx_freq=3.573 lifetime_seconds=1"))
        idx3 = int(body)
        g6 = gui.mark()
        gone = gui.wait_for(rf"\|spot {idx3} removed", since=g6, deadline=3.0)
        check("6. lifetime_seconds=1: the radio removes it by itself", gone is not None, str(gui.seen[g6:]))
        code, _ = cmdr.reply(cmdr.cmd(f"spot trigger {idx3}"))
        check("6. ... and it is really gone (trigger refused)", code != 0, f"{code:X}")

        # ---- 7. refusals ----
        for text in ("spot add callsign=NOFREQ", "spot remove 9999", "spot add callsign=X rx_freq=abc",
                     "spot set 9999 comment=x", "spot bogus"):
            code, _ = cmdr.reply(cmdr.cmd(text))
            check(f"7. refused: {text!r}", code != 0, f"{code:X}")
        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        for c in clients:
            try:
                c.close()
            except OSError:
                pass
        sim.kill()


def test_spots():
    rc = main()
    if rc == 0:
        return
    import pytest
    if passed == 0 and failed == 0:
        pytest.skip(f"flex_sim could not be started (ports {PORT}/{CTL}/{DAXTX_PORT} busy?)")
    assert rc == 0, f"{failed} spot check(s) failed - see output above"


if __name__ == "__main__":
    sys.exit(main())
