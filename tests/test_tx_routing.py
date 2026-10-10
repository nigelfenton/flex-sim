#!/usr/bin/env python3
"""TX-routing test: the three radio behaviours AE's TCI server depends on.

Found driving AE's TCI server from CAT4OM (2026-10-10): each one was missing,
and each made a correct client look broken against the sim.

  1. `xmit 1` / `xmit 0` keys and unkeys the radio. AE's TCI `trx:N,true` and
     API clients such as CAT4OM's native Flex driver send this, not
     `transmit set mox=`. Unhandled, the radio never reported keyed and AE
     fail-closed the TCI PTT after its confirmation timeout.
  2. `slice create` announces the new slice in a status line BEFORE the reply.
     AE's TCI VFO-B route looks the slice up while handling the reply; finding
     nothing, it dropped it and created another on the next request.
  3. `slice set N tx=1` moves the radio's single TX slice and re-announces the
     slices whose tx flag changed. Ignored, split never reached AE.

Run: python3 tests/test_tx_routing.py   (spawns its own sim on :5994/:8734)
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT, CTL = 5994, 8734
DAXTX_PORT = 4994          # not 4991/4993: a running sim or test_txchain may own those
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


class Client:
    """Fake API client: sends C<seq>|cmd, returns every line up to R<seq>."""

    def __init__(self, host):
        self.tcp = socket.create_connection((host, PORT), timeout=5)
        self.tcp.settimeout(3)
        self.buf = b""
        self.seq = 0
        self.greeting = [self._line(), self._line()]       # V<version>, H<handle>

    def _line(self):
        while b"\n" not in self.buf:
            chunk = self.tcp.recv(4096)
            if not chunk:
                raise ConnectionError("sim closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="replace")

    def cmd(self, text, settle=0.3):
        """Lines received up to and including this command's reply, plus any
        status the sim sends just after it (within `settle` seconds)."""
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


def reply_code(lines, seq):
    r = next(line for line in lines if line.startswith(f"R{seq}|"))
    return int(r.split("|")[1], 16)


def slice_status(lines, idx):
    """The slice <idx> status lines, as key=value dicts, in arrival order."""
    out = []
    for line in lines:
        if line.startswith("S") and f"|slice {idx} " in line:
            body = line.split("|", 1)[1]
            out.append(dict(t.split("=", 1) for t in body.split() if "=" in t))
    return out


def last_tx(lines, idx):
    """The tx= flag in the newest slice <idx> status, or None if none came."""
    st = slice_status(lines, idx)
    return st[-1].get("tx") if st else None


def main():
    sim = subprocess.Popen(
        [sys.executable, str(ROOT / "flex_sim.py"), "--ip", "127.0.0.1",
         "--port", str(PORT), "--ctl-port", str(CTL)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, cwd=str(ROOT),
        env={**os.environ, "FLEXSIM_DAXTX_PORT": str(DAXTX_PORT)})
    try:
        if not wait_ctl():
            print("FAIL: sim control panel never came up")
            return 1
        c = Client("127.0.0.1")
        c.cmd("display panafall create x=100 y=100")

        # ---- 2. slice create: status before reply ----
        lines = c.cmd("slice create pan=0x40000000 freq=14.100000")
        r_at = next(i for i, line in enumerate(lines) if line.startswith(f"R{c.seq}|"))
        s_at = next((i for i, line in enumerate(lines) if "|slice 0 " in line and "in_use=1" in line), None)
        check("slice create: reply carries the new index 0",
              lines[r_at].endswith("|0"), lines[r_at])
        check("slice create: slice 0 status arrives BEFORE the reply",
              s_at is not None and s_at < r_at, f"status at {s_at}, reply at {r_at}")
        check("first slice is the TX slice", last_tx(lines, 0) == "1",
              str(slice_status(lines, 0)))

        lines = c.cmd("slice create pan=0x40000000 freq=7.074000")
        r_at = next(i for i, line in enumerate(lines) if line.startswith(f"R{c.seq}|"))
        s_at = next((i for i, line in enumerate(lines) if "|slice 1 " in line and "in_use=1" in line), None)
        check("second slice: status before reply too",
              s_at is not None and s_at < r_at, f"status at {s_at}, reply at {r_at}")
        check("second slice is not TX", last_tx(lines, 1) == "0",
              str(slice_status(lines, 1)))

        # ---- 3. slice set N tx=1 moves the single TX slice ----
        lines = c.cmd("slice set 1 tx=1")
        check("tx=1: reply OK", reply_code(lines, c.seq) == 0)
        check("tx=1: slice 1 re-announced as TX", last_tx(lines, 1) == "1", str(slice_status(lines, 1)))
        check("tx=1: slice 0 re-announced as NOT TX", last_tx(lines, 0) == "0", str(slice_status(lines, 0)))

        lines = c.cmd("slice set 0 tx=1")
        check("tx moves back to slice 0",
              last_tx(lines, 0) == "1" and last_tx(lines, 1) == "0",
              f"{slice_status(lines, 0)} {slice_status(lines, 1)}")

        lines = c.cmd("slice set 0 tx=1")
        check("tx=1 on the slice that already has TX re-announces nothing",
              not slice_status(lines, 0) and not slice_status(lines, 1), str(lines))

        # ---- 1. xmit keys and unkeys ----
        lines = c.cmd("xmit 1")
        check("xmit 1: reply OK", reply_code(lines, c.seq) == 0)
        check("xmit 1: interlock reports TRANSMITTING",
              any("interlock state=TRANSMITTING" in line for line in lines), str(lines))
        check("xmit 1: sim is transmitting", sim_state()["tx"] is True)

        lines = c.cmd("xmit 0")
        check("xmit 0: interlock reports READY",
              any("interlock state=READY" in line for line in lines), str(lines))
        check("xmit 0: sim back to RX", sim_state()["tx"] is False)

        for bad in ("xmit", "xmit on"):
            lines = c.cmd(bad)
            check(f"'{bad}': refused with an error code", reply_code(lines, c.seq) != 0)
            check(f"'{bad}': keys nothing", sim_state()["tx"] is False)

        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        sim.kill()


def test_tx_routing():
    """pytest entry point. One ordered walk (one sim) like test_txchain: the
    slices created early are the ones the tx= checks move. Skips rather than
    fails when the sim cannot start (ports busy) — an environment fact."""
    rc = main()
    if rc == 0:
        return
    import pytest
    if passed == 0 and failed == 0:
        pytest.skip(f"flex_sim could not be started (ports {PORT}/{CTL}/{DAXTX_PORT} busy?)")
    assert rc == 0, f"{failed} tx-routing check(s) failed — see output above"


if __name__ == "__main__":
    sys.exit(main())
