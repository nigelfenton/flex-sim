#!/usr/bin/env python3
"""lp100_sim.py against the rules a client holds it to.

The sim exists to drive AetherSDR's LP-100A applet, so the reference here is AetherSDR's own
record check, transcribed from src/core/LpMeterProtocol.cpp (looksLikeRecord /
hasCanonicalNumericForm / decodeReading, tag v26.10.4): field count, comma offsets at the
canonical 42-character length, per-field width caps, strict numeric form (no exponent, sign only
on dBm), physical bounds, and the |Z|/phase-vs-SWR coherence check (tolerance 0.25, applied above
0.5 W). Every normal record must pass it; each injected fault must fail it - except the 5-character
dBm, which AetherSDR documents as a variant it must still accept.

Also checked: the meter never pushes, replies go to every client on the wire (ser2net), the
single-byte commands step their fields, auto-ranging, the key-up hold, stall, and the banner.

Run: python3 tests/test_lp100_sim.py   (or via pytest)
"""

import cmath
import json
import math
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIM = ROOT / "lp100_sim.py"

# ---- AetherSDR's rules, transcribed ------------------------------------------------------------
MAX_WIDTH = [7, 5, 5, 1, 6, 1, 1, 5, 4]
COMMA_OFFSETS = [7, 13, 19, 21, 28, 30, 32, 37]
RECORD_LENGTH = 42


def canonical_numeric(field, sign_allowed):
    if not field:
        return False
    i = 0
    if field[0] in "+-":
        if not sign_allowed:
            return False
        i = 1
    digits = points = 0
    for ch in field[i:]:
        if ch.isdigit():
            digits += 1
        elif ch == ".":
            points += 1
            if points > 1:
                return False
        else:
            return False
    return digits > 0


def ae_accepts(body):
    """True if AetherSDR's looksLikeRecord() would accept this record body (marker stripped)."""
    f = body.split(",")
    if len(f) != 9:
        return False
    if len(body) == RECORD_LENGTH:
        off = 0
        for i, want in enumerate(COMMA_OFFSETS):
            off += len(f[i])
            if off != want:
                return False
            off += 1
    if any(len(f[i]) > MAX_WIDTH[i] for i in range(9)):
        return False
    if len(f[4]) != 6 or any(not (c.isalnum() or c in "/- ") for c in f[4]):
        return False
    for i in (0, 1, 2, 7, 8):
        if not canonical_numeric(f[i], i == 7):
            return False
    for i in (3, 5, 6):
        if len(f[i].strip()) != 1 or not f[i].strip().isdigit():
            return False
    p, z, ph, dbm, swr = (float(f[i]) for i in (0, 1, 2, 7, 8))
    return (0 <= p <= 9999.99 and 0 <= z <= 9999.9 and -999.9 <= dbm <= 999.9
            and 0 <= ph <= 90 and 0.99 <= swr <= 99.99)


def implied_swr(z, ph):
    zc = cmath.rect(z, math.radians(ph))
    g = abs((zc - 50) / (zc + 50))
    return (1 + g) / (1 - g)


def coherent(body):
    f = body.split(",")
    p, z, ph, swr = float(f[0]), float(f[1]), float(f[2]), float(f[8])
    return p <= 0.5 or abs(implied_swr(z, ph) - swr) <= 0.25


# ---- harness -------------------------------------------------------------------------------------
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sim:
    def __init__(self, *args):
        self.port, self.ctl = free_port(), free_port()
        self.args = list(args)

    def __enter__(self):
        self.p = subprocess.Popen(
            [sys.executable, str(SIM), "--no-cli", "--host", "127.0.0.1", "--port", str(self.port),
             "--ctl-port", str(self.ctl), "--seed", "7", "--latency-ms", "2", *self.args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < 10:
            line = self.p.stdout.readline().decode(errors="replace")
            if "ready" in line:
                return self
            if not line and self.p.poll() is not None:
                break
        self.p.kill()
        raise RuntimeError("lp100_sim never came up")

    def __exit__(self, *exc):
        self.p.kill()
        self.p.wait(5)

    def cmd(self, c):
        url = f"http://127.0.0.1:{self.ctl}/cmd?c=" + urllib.parse.quote(c)
        return json.loads(urllib.request.urlopen(url, timeout=5).read())

    def client(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        s.settimeout(0.6)
        return s


import urllib.parse  # noqa: E402  (after the harness to keep the transcription block together)


def read_all(s, wait=0.4):
    end = time.time() + wait
    buf = b""
    while time.time() < end:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def poll(s, wait=0.4):
    s.sendall(b"P")
    data = read_all(s, wait)
    i = data.find(b";")
    return data, (data[i + 1:].decode("ascii", "replace") if i >= 0 else None)


# ---- tests -----------------------------------------------------------------------------------
def test_idle_record_layout_and_rules():
    with Sim() as sim:
        s = sim.client()
        data, body = poll(s)
        assert data.startswith(b";") and not data.endswith((b"\r", b"\n")), data
        assert len(body) == RECORD_LENGTH, (len(body), body)
        assert ae_accepts(body), body
        f = body.split(",")
        assert float(f[0]) == 0.0 and f[8] == "1.00" and f[5] == "2", body   # idle: 0 W, SWR 1.00, Low
        assert f[4] == "SIM   ", f[4]
        assert float(f[2]) > 85, "idle phase sits near 90 degrees, as captured"


def test_meter_never_pushes():
    with Sim() as sim:
        s = sim.client()
        assert read_all(s, 0.8) == b"", "the meter answers P and nothing else"
        sim.cmd("key 100")
        assert read_all(s, 0.8) == b"", "keying does not make it push either"


def test_drive_is_coherent_ranges_and_dbm():
    with Sim() as sim:
        s = sim.client()
        sim.cmd("load 75 0")
        sim.cmd("key 100")
        for _ in range(20):
            _, body = poll(s, 0.2)
            assert ae_accepts(body) and coherent(body), body
            f = body.split(",")
            assert abs(float(f[8]) - 1.50) <= 0.02, body
            assert f[5] == "1", "100 W is the Mid range with 700/125/25 ceilings"
            assert abs(float(f[7]) - 50.0) < 0.1, "dBm = 10 log10(mW)"
        sim.cmd("load 30 40")                 # reactive: phase must stay a MAGNITUDE
        sim.cmd("key 500")
        _, body = poll(s)
        f = body.split(",")
        assert ae_accepts(body) and coherent(body), body
        assert f[5] == "0", "500 W is the High range"
        sim.cmd("load 30 -40")                # capacitive: same |phase| on the wire
        _, body2 = poll(s)
        assert abs(float(body2.split(",")[2]) - float(f[2])) < 0.5, (body, body2)
        sim.cmd("key 10")
        _, body3 = poll(s)
        assert body3.split(",")[5] == "2", "10 W is the Low range"


def test_commands_step_alarm_and_hold_and_wrap():
    with Sim() as sim:
        s = sim.client()
        seen = []
        for _ in range(7):
            _, body = poll(s, 0.2)
            seen.append(body.split(",")[3])
            s.sendall(b"A")
            time.sleep(0.05)
        assert seen == ["0", "1", "2", "3", "4", "5", "0"], seen
        modes = []
        for _ in range(4):
            _, body = poll(s, 0.2)
            modes.append(body.split(",")[6])
            s.sendall(b"F")
            time.sleep(0.05)
        assert modes == ["0", "1", "2", "0"], modes
        s.sendall(b"M")                          # accepted, changes nothing modelled
        time.sleep(0.05)
        assert sim.cmd("status")["state"]["m_presses"] == 1


def test_key_up_hold_is_incoherent_then_clears():
    with Sim() as sim:
        s = sim.client()
        sim.cmd("hold peak")
        sim.cmd("swr 2")
        sim.cmd("key 100")
        _, live = poll(s, 0.2)
        assert coherent(live)
        sim.cmd("unkey")
        _, held = poll(s, 0.2)
        f = held.split(",")
        assert float(f[0]) > 90 and f[8] == "1.00", "Peak holds power while SWR reverts"
        assert ae_accepts(held) and not coherent(held), "the held record is incoherent on purpose"
        time.sleep(1.9)
        _, idle = poll(s, 0.2)
        assert float(idle.split(",")[0]) == 0.0, idle
        sim.cmd("hold fast")
        sim.cmd("key 100")
        sim.cmd("unkey")
        _, fast = poll(s, 0.2)
        assert float(fast.split(",")[0]) == 0.0, "Fast has no hold"


def test_shared_wire_mirrors_replies():
    with Sim() as sim:
        a, b = sim.client(), sim.client()
        time.sleep(0.1)
        a.sendall(b"P")
        got_a, got_b = read_all(a), read_all(b)
        assert got_a.startswith(b";") and got_a == got_b, (got_a, got_b)


def test_faults():
    with Sim() as sim:
        s = sim.client()
        for fault, accept in (("exponent", False), ("truncate", False), ("wide-dbm", True)):
            sim.cmd(f"fault {fault}")
            data, body = poll(s)
            assert ae_accepts(body) is accept, (fault, body)
            if fault == "exponent":
                assert body.startswith("01e+100,"), body
            if fault == "wide-dbm":
                assert len(body) == 43 and body.split(",")[7] == "-12.3", body
        _, clean = poll(s)
        assert ae_accepts(clean), "faults are one-shot"
        sim.cmd("fault garbage")
        data, body = poll(s)
        assert not data.startswith(b";") and b";" in data and ae_accepts(body), data
        sim.cmd("fault flip")
        _, flipped = poll(s)
        _, after = poll(s)
        assert len(flipped) == len(after), "a flipped digit keeps the length - no checksum catches it"
        sim.cmd("stall on")
        assert poll(s, 0.6)[0] == b"", "stalled: no reply"
        assert sim.cmd("status")["state"]["stalled"] is True
        sim.cmd("stall off")
        assert poll(s)[1] is not None, "answers again after the stall"


def test_lag_holds_one_reply_and_pairs_the_next():
    """The pattern measured in AetherSDR #6794: a long gap, then two records ~20 ms apart, and still
    exactly one reply per poll - nothing unsolicited."""
    with Sim() as sim:
        assert sim.cmd("lag on 0.6 150")["out"].startswith("lag on:")
        s = sim.client()
        s.settimeout(0.05)
        stamps, sent, buf = [], 0, b""
        t_end = time.monotonic() + 2.2
        next_poll = time.monotonic()
        while time.monotonic() < t_end:
            if time.monotonic() >= next_poll:
                s.sendall(b"P")
                sent += 1
                next_poll += 0.100
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                continue
            now = time.monotonic()
            stamps += [now] * chunk.count(b";")
            buf += chunk
        time.sleep(0.4)                                  # let the last replies land
        s.settimeout(0.3)
        try:
            buf += s.recv(4096)
        except socket.timeout:
            pass
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        pairs = [i for i in range(1, len(gaps)) if gaps[i - 1] >= 0.14 and gaps[i] <= 0.045]
        assert pairs, f"no long-gap-then-pair pattern in gaps {[round(g * 1000) for g in gaps]}"
        assert buf.count(b";") == sent, (buf.count(b";"), sent)
        assert sim.cmd("status")["state"]["lag"]["events"] >= 2
        sim.cmd("lag off")
        assert sim.cmd("status")["state"]["lag"] is None


def test_banner_has_no_marker():
    with Sim("--banner") as sim:
        s = sim.client()
        banner = read_all(s, 0.5)
        assert len(banner) > 150 and b";" not in banner, banner
        data, body = poll(s)
        assert ae_accepts(body), body


def test_bad_command_lines_are_refused():
    for args in (["--ceilings", "25,125,700"], ["--ceilings", "abc"], ["--latency-ms", "-5"]):
        r = subprocess.run([sys.executable, str(SIM), "--no-cli", "--port", "0", "--ctl-port", "0", *args],
                           capture_output=True, text=True, timeout=10)
        assert r.returncode != 0 and "error" in r.stderr.lower(), (args, r.stderr[-200:])


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failed else 0)
