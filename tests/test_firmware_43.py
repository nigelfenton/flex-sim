#!/usr/bin/env python3
"""Firmware 4.3 test: SmartSDR v4.3.16's wire changes, behind --firmware 4.3.

The behaviours aethersdr/AetherSDR#6437 maps (FlexLib v4.1.5 -> v4.3.16), each
checked against BOTH a default sim (old firmware: nothing changes) and a 4.3 one:

  1. the version the radio reports (discovery, the V line)
  2. PTT Override: transmit status ptt_mic_override=, set and validated (#6442)
  3. the interlock source list (MIC,VOX,...; VOX was SW before 4.3)
  4. what a hardware PTT puts on the air: the analog mic before 4.3; the
     selected source on 4.3 unless PTT Override is on
  5. slice DSP levels: nrl_level/anfl_level/nrs_level status keys, NRS 0..50,
     nrf_level gone
  6. per-slice sample_rate and the 48/96 kHz audio class codes
  7. APD state= lifecycle and the ant/freq/rfpower equalizer line
  8. UDP keepalives (udp_register / ping) counted, and no longer mistaken for
     DAX TX audio
  9. DAX TX rate must match the TX slice's on 4.3 (txchain stage 3b)

Run: python3 tests/test_firmware_43.py   (spawns sims on :5993/:8741 and :5994/:8742)
"""

import json
import os
import socket
import struct
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
        self.port, self.ctl, self.daxtx = port, ctl, daxtx
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "flex_sim.py"), "--ip", "127.0.0.1",
             "--port", str(port), "--ctl-port", str(ctl), *extra],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, cwd=str(ROOT),
            env={**os.environ, "FLEXSIM_DAXTX_PORT": str(daxtx)})

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.ctl}{path}", timeout=5) as r:
            return json.loads(r.read())

    def state(self):
        return self.get("/state")[0]

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

    def drain(self, settle=0.4):
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


def code(lines, seq):
    return int(next(l for l in lines if l.startswith(f"R{seq}|")).split("|")[1], 16)


def reply_body(lines, seq):
    return next(l for l in lines if l.startswith(f"R{seq}|")).split("|", 2)[2]


def kv_of(lines, prefix):
    """key=value dict of the LAST status line whose body starts with prefix."""
    out = None
    for l in lines:
        if l.startswith("S") and "|" in l:
            body = l.split("|", 1)[1]
            if body.startswith(prefix):
                out = dict(t.split("=", 1) for t in body.split() if "=" in t)
    return out or {}


AUDIO_PCCS = {0x03E3, 0x03E4, 0x03E5, 0x0123, 0x0124, 0x0125}


def vita(pcc, sid=0x08000001):
    """A minimal VITA-49 packet with the given class code (header only + 16 B)."""
    word0 = (0x1 << 28) | 11
    return struct.pack(">IIIIIII", word0, sid, 0x00001C2D, (0x534C << 16) | pcc, 0, 0, 0) + bytes(16)


def main():
    old = Sim(5993, 8741, 4993)
    new = Sim(5994, 8742, 4994, "--firmware", "4.3")
    try:
        if not (old.up() and new.up()):
            print("FAIL: sim control panel never came up")
            return 1

        # ---- 1. version ----
        a, b = Client(5993), Client(5994)
        check("default sim reports its long-standing version", a.greeting[0] == "V3.3.28.0", a.greeting)
        check("--firmware 4.3 reports v4.3.16.42034", b.greeting[0] == "V4.3.16.42034", b.greeting)
        check("/state carries the firmware", new.state()["firmware"] == "4.3.16.42034"
              and new.state()["fw43"] is True and old.state()["fw43"] is False)

        for c in (a, b):
            c.cmd("slice create freq=14.200000 mode=USB")

        # ---- 2. PTT Override ----
        ta = kv_of(a.cmd("sub tx all"), "transmit ")
        tb = kv_of(b.cmd("sub tx all"), "transmit ")
        check("old firmware never reports ptt_mic_override", "ptt_mic_override" not in ta, ta)
        check("4.3 reports ptt_mic_override=0 by default", tb.get("ptt_mic_override") == "0", tb)
        tb = kv_of(b.cmd("transmit set ptt_mic_override=1"), "transmit ")
        check("4.3 sets ptt_mic_override=1 and reports it", tb.get("ptt_mic_override") == "1", tb)
        lines = b.cmd("transmit set ptt_mic_override=2")
        check("4.3 refuses ptt_mic_override=2 (Bad field)", code(lines, b.seq) == 0x5000002D)
        check("...and keeps the old value", new.state()["ptt_mic_override"] is True)
        b.cmd("transmit set ptt_mic_override=0")
        a.cmd("transmit set ptt_mic_override=1")
        check("old firmware ignores ptt_mic_override", old.state()["ptt_mic_override"] is False)

        # ---- 3. interlock source list ----
        old.get("/hwptt?src=MIC&on=1"); old.get("/hwptt?src=VOX&on=1")
        new.get("/hwptt?src=MIC&on=1"); new.get("/hwptt?src=VOX&on=1")
        ia, ib = kv_of(a.drain(), "interlock "), kv_of(b.drain(), "interlock ")
        check("old firmware names one source (hardware wins)", ia.get("source") == "MIC", ia)
        check("4.3 lists every latched source, hardware first", ib.get("source") == "MIC,VOX", ib)
        check("hardware PTT keys the radio", ib.get("state") == "TRANSMITTING", ib)
        ib = kv_of(b.cmd("transmit set mox=1"), "interlock ")
        check("4.3 adds SW after the hardware and VOX", ib.get("source") == "MIC,VOX,SW", ib)
        b.cmd("transmit set mox=0")
        for sim in (old, new):
            sim.get("/hwptt?src=MIC&on=0")
        ia, ib = kv_of(a.drain(), "interlock "), kv_of(b.drain(), "interlock ")
        check("old firmware reports VOX as SW", ia.get("source") == "SW", ia)
        check("4.3 reports VOX as VOX", ib.get("source") == "VOX", ib)
        for sim in (old, new):
            sim.get("/hwptt?src=VOX&on=0")
        check("bad /hwptt input is refused", new.get("/hwptt?src=FOOT&on=1")["ok"] is False)

        # ---- 4. what a hardware PTT transmits ----
        for c in (a, b):
            c.cmd("transmit set dax=1")
        sa = old.get("/hwptt?src=ACC&on=1")
        sb = new.get("/hwptt?src=ACC&on=1")
        check("old firmware: hardware PTT forces the analog mic over DAX", sa["tx_audio_source"] == "MIC", sa)
        check("4.3: hardware PTT transmits the selected source (DAX)", sb["tx_audio_source"] == "DAX", sb)
        b.cmd("transmit set ptt_mic_override=1")
        check("4.3 with PTT Override: back to the analog mic", new.state()["tx_audio_source"] == "MIC")
        b.cmd("transmit set ptt_mic_override=0")
        for sim in (old, new):
            sim.get("/hwptt?src=ACC&on=0")
        for c in (a, b):
            c.cmd("transmit set dax=0")
        check("unkeyed again", new.state()["tx"] is False and new.state()["tx_audio_source"] is None)

        # ---- 5. DSP levels ----
        sa = kv_of(a.cmd("slice set 0 lms_nr_level=40 speex_nr_level=80 nrf_level=10"), "slice 0 ")
        sb = kv_of(b.cmd("slice set 0 lms_nr_level=40 speex_nr_level=80 nrf_level=10"), "slice 0 ")
        check("old firmware echoes the command names",
              sa.get("lms_nr_level") == "40" and sa.get("speex_nr_level") == "80"
              and sa.get("nrf_level") == "10", sa)
        check("4.3 reports nrl_level", sb.get("nrl_level") == "40" and "lms_nr_level" not in sb, sb)
        check("4.3 clamps NRS to 50 and reports nrs_level", sb.get("nrs_level") == "50", sb)
        check("4.3 drops nrf_level", "nrf_level" not in sb, sb)
        sb = kv_of(b.cmd("slice set 0 lms_anf_level=30"), "slice 0 ")
        check("4.3 reports anfl_level", sb.get("anfl_level") == "30", sb)

        # ---- 6. sample_rate + 48/96 kHz audio ----
        sa = kv_of(a.cmd("slice set 0 sample_rate=48000"), "slice 0 ")
        check("old firmware has no sample_rate", "sample_rate" not in sa, sa)
        sb = kv_of(b.cmd("slice set 0 sample_rate=48000"), "slice 0 ")
        check("4.3 sets and reports sample_rate=48000", sb.get("sample_rate") == "48000", sb)
        lines = b.cmd("slice set 0 sample_rate=44100")
        check("4.3 refuses a rate it does not have", code(lines, b.seq) == 0x5000002D)

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(2)
        b.cmd(f"client udpport {udp.getsockname()[1]}")
        b.cmd("stream create type=remote_audio_rx compression=none")
        pccs, t0, n = set(), time.time(), 0
        while time.time() - t0 < 1.0:
            try:
                d = udp.recv(4096)
            except socket.timeout:
                break
            if len(d) >= 16:
                pcc = struct.unpack(">H", d[14:16])[0]
                if pcc in AUDIO_PCCS:                  # the port also carries pan/wf/meters
                    pccs.add(pcc)
                    n += 1
        rate = n / max(1e-3, time.time() - t0)
        check("a 48 kHz slice streams class 0x03E4", pccs == {0x03E4}, sorted(hex(x) for x in pccs))
        check("...at twice the 24 kHz packet rate (~375/s)", 300 < rate < 450, f"{rate:.0f}/s")
        b.cmd("slice set 0 sample_rate=96000")
        time.sleep(0.3)
        pccs = set()
        for _ in range(50):
            try:
                d = udp.recv(4096)
                pccs.add(struct.unpack(">H", d[14:16])[0])
            except socket.timeout:
                break
        check("a 96 kHz slice switches to class 0x03E5 live", 0x03E5 in pccs, sorted(hex(x) for x in pccs))
        b.cmd("client set send_reduced_bw_dax=1")
        time.sleep(0.3)
        pccs = set()
        for _ in range(50):
            try:
                d = udp.recv(4096)
                pccs.add(struct.unpack(">H", d[14:16])[0])
            except socket.timeout:
                break
        check("reduced bandwidth at 96 kHz uses class 0x0125", 0x0125 in pccs, sorted(hex(x) for x in pccs))
        b.cmd("client set send_reduced_bw_dax=0")
        udp.close()

        # ---- 7. APD ----
        pa = kv_of(a.cmd("sub apd all"), "apd ")
        pb = kv_of(b.cmd("sub apd all"), "apd enable")
        check("old firmware: no APD state key", "state" not in pa and pa.get("enable") == "0", pa)
        check("4.3: APD state=OFF while disabled", pb.get("state") == "OFF", pb)
        pb = kv_of(b.cmd("apd enable=1"), "apd enable")
        check("4.3: enabled and unkeyed -> IDLE", pb.get("state") == "IDLE", pb)
        lines = b.cmd("transmit set mox=1")
        pb, eq = kv_of(lines, "apd enable"), kv_of(lines, "apd equalizer_active")
        check("4.3: keyed -> ACTIVE", pb.get("state") == "ACTIVE", pb)
        check("4.3: equalizer line carries ant/freq/rfpower",
              eq.get("equalizer_active") == "1" and eq.get("ant") == "ANT1" and "freq" in eq
              and "rfpower" in eq, eq)
        b.cmd("transmit set mox=0")

        # ---- 8. keepalives ----
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(3):
            tx.sendto(b"client udp_register handle=0x00000001", ("127.0.0.1", new.daxtx))
        tx.sendto(b"client ping handle=0x00000001", ("127.0.0.1", new.daxtx))
        time.sleep(0.3)
        ka = new.state()["udp_keepalive"]
        check("udp_register keepalives are counted", ka["udp_register"] == 3, ka)
        check("ping keepalives are counted", ka["ping"] == 1 and ka["last"] == "ping", ka)
        check("keepalives are not counted as DAX TX audio", new.get("/txchain")["audio_pkts"] == 0,
              new.get("/txchain"))

        # ---- 9. DAX TX rate vs the TX slice (4.3) ----
        b.cmd("slice set 0 sample_rate=48000")
        tx.sendto(vita(0x03E3), ("127.0.0.1", new.daxtx))
        time.sleep(0.2)
        ch = new.get("/txchain")
        check("4.3: 24 kHz TX audio into a 48 kHz TX slice fails stage 3b",
              ch["stages"].get("3b: TX audio rate matches the TX slice") is False, ch)
        tx.sendto(vita(0x03E4), ("127.0.0.1", new.daxtx))
        time.sleep(0.2)
        ch = new.get("/txchain")
        check("4.3: 48 kHz TX audio passes stage 3b",
              ch["stages"].get("3b: TX audio rate matches the TX slice") is True, ch)
        check("old firmware has no stage 3b",
              not any(k.startswith("3b") for k in old.get("/txchain")["stages"]))
        tx.close()

        a.close(); b.close()
        bad = subprocess.run([sys.executable, str(ROOT / "flex_sim.py"), "--firmware", "5"],
                             capture_output=True, text=True, cwd=str(ROOT), timeout=30)
        check("a malformed --firmware is rejected at startup",
              bad.returncode == 2 and "preset" in bad.stderr, bad.stderr[-200:])

        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        old.kill()
        new.kill()


def test_firmware_43():
    """pytest entry point. Skips rather than fails when the sims cannot start."""
    rc = main()
    if rc == 0:
        return
    import pytest
    if passed == 0 and failed == 0:
        pytest.skip("flex_sim could not be started (ports 5993/8741 or 5994/8742 busy?)")
    assert rc == 0, f"{failed} firmware-4.3 check(s) failed; see output above"


if __name__ == "__main__":
    sys.exit(main())
