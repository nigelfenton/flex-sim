#!/usr/bin/env python3
"""Red-team anan_sim: try to break it, then prove a normal client still works.

Every attack is followed by a HEALTH PROBE -- a clean discovery plus a real session (DDC2 at
48 kHz, the shape AetherSDR uses) that must stream with a contiguous sequence from 0 and
send status packets. That is the point: a sim whose thread has silently died keeps its
process alive and keeps answering discovery, so "it did not crash" proves nothing.

Findings are classified:
  CRASH        the sim process exited
  TRACEBACK    the sim printed a Python traceback (a thread died, even if the process lives)
  UNHEALTHY    the health probe failed after the attack (a stuck or dead thread)
  LEAK         the process's handle / fd count grew by more than a small margin
  SLOW         discovery or a mask change took too long while the sim was under load
  HIJACK       a packet from a stranger redirected the stream (informational: a real radio
               does the same -- but it makes the sim a traffic reflector, see --allow)

usage:
  python3 tools/p2_redteam.py                  # spawn a sim on loopback and attack it (default)
  python3 tools/p2_redteam.py --quick          # a short pass (CI-sized)
  python3 tools/p2_redteam.py --host H --pid N # attack a RUNNING sim (see the warning below)

Attacking a sim someone else is using disturbs them. Heavy phases (load, churn) are skipped
unless the sim was spawned here or --heavy is given.
"""

import argparse
import os
import random
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IP = "127.0.0.1"
PORTS_IN = (1024, 1025, 1026, 1027, 1028, 1029)
findings = []
checks = 0


def finding(kind, what, detail=""):
    findings.append((kind, what, detail))
    print(f"  !! {kind:10} {what}  {detail}")


def ok(what):
    global checks
    checks += 1
    print(f"  ok {what}")


# ------------------------------------------------------------------ packet builders
def ddc_spec(mask, rates=None, byte1363=None, size=1444):
    b = bytearray(size)
    if size > 7:
        b[7] = mask
    for n, khz in (rates or {}).items():
        o = 18 + 6 * n
        if o + 2 <= size:
            struct.pack_into(">H", b, o, khz)
    if byte1363 is not None and size > 1363:
        b[1363] = byte1363
    return bytes(b)


def hp(run, words=None, size=1444):
    b = bytearray(size)
    if size > 4:
        b[4] = 1 if run else 0
    for n, w in (words or {}).items():
        if 9 + 4 * n + 4 <= size:
            struct.pack_into(">I", b, 9 + 4 * n, w)
    return bytes(b)


def general(size=60, flags=0x08):
    b = bytearray(size)
    if size > 37:
        b[37] = flags
    return bytes(b)


def discovery():
    b = bytearray(60)
    b[4] = 2
    return bytes(b)


# ------------------------------------------------------------------ the sim under attack
class Target:
    def __init__(self, spawn=True, args=(), env=None, pid=None):
        self.spawn, self.pid, self.p = spawn, pid, None
        if spawn:
            self.p = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "anan_sim.py"), "--ip", IP, *args],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
                env={**os.environ, **(env or {})})
            self.pid = self.p.pid
            t0 = time.time()
            while time.time() - t0 < 20:
                line = self.p.stdout.readline().decode(errors="replace")
                if "waiting for discovery" in line:
                    break
                if not line and self.p.poll() is not None:
                    raise RuntimeError("sim exited at startup")
            else:
                raise RuntimeError("sim never came up")
            self.out = []
            import threading
            threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        for raw in self.p.stdout:
            self.out.append(raw.decode(errors="replace"))

    def alive(self):
        return self.p is None or self.p.poll() is None

    def output(self):
        return "".join(getattr(self, "out", []))

    def handles(self):
        """Open handles (Windows) or fds (Linux) of the sim process, or None."""
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes
                k32 = ctypes.windll.kernel32
                h = k32.OpenProcess(0x1000, False, self.pid)      # PROCESS_QUERY_LIMITED_INFORMATION
                if not h:
                    return None
                n = wintypes.DWORD()
                k32.GetProcessHandleCount(h, ctypes.byref(n))
                k32.CloseHandle(h)
                return n.value
            return len(os.listdir(f"/proc/{self.pid}/fd"))
        except Exception:
            return None

    def close(self):
        if self.p and self.p.poll() is None:
            self.p.kill()
            self.p.wait()


def client():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((IP, 0))
    return s


def drain(s, seconds):
    t_end = time.time() + seconds
    while time.time() < t_end:
        s.settimeout(0.05)
        try:
            s.recvfrom(65535)
        except (socket.timeout, OSError):
            pass


_health_n = [1]


def health(tgt, label):
    """A clean client must discover the radio, enable a DDC with a NEW mask, and stream it with
    a contiguous sequence from 0 while status packets arrive. The DDC rotates (2..7) on every
    call: if the mask were the same each time, a dead DDC Specific thread would still pass,
    because the sim keeps streaming whatever mask it last accepted. The run bit is cycled
    0 -> 1 first, because the sim restarts its sequence counters on a run EDGE (as a real G2
    does), not merely when it sees run=1 again. Returns True if healthy."""
    _health_n[0] = 2 + (_health_n[0] - 1) % 6          # 2,3,4,5,6,7,2,...
    ddc = _health_n[0]
    _health_n[0] += 1
    port = 1035 + ddc
    if not tgt.alive():
        finding("CRASH", label, "sim process is gone")
        return False
    s = client()
    ctl = client()
    try:
        s.settimeout(3)
        s.sendto(discovery(), (IP, 1024))
        try:
            rep, _ = s.recvfrom(2048)
        except socket.timeout:
            finding("UNHEALTHY", label, "no discovery reply")
            return False
        s.sendto(general(), (IP, 1024))
        s.sendto(hp(False), (IP, 1027))                  # make sure the next run=1 is an EDGE
        time.sleep(0.15)
        drain(s, 0.3)                                    # packets the previous session left buffered
        ctl.sendto(ddc_spec(1 << ddc, {ddc: 48}), (IP, 1025))
        time.sleep(0.05)
        s.sendto(hp(True, {ddc: 14175000}), (IP, 1027))
        iq, st = [], 0
        t_end = time.time() + 1.6
        while time.time() < t_end:
            s.settimeout(0.2)
            try:
                d, frm = s.recvfrom(4096)
            except socket.timeout:
                continue
            if frm[1] == port:
                iq.append(struct.unpack_from(">I", d, 0)[0])
            elif frm[1] == 1025:
                st += 1
        s.sendto(hp(False), (IP, 1027))
        if len(iq) < 150:
            finding("UNHEALTHY", label, f"only {len(iq)} packets from DDC{ddc} (:{port}) in 1.6 s (want ~320): "
                    "the DDC Specific thread may be dead")
            return False
        if iq[0] != 0 or any(b - a != 1 for a, b in zip(iq, iq[1:])):
            finding("UNHEALTHY", label, f"DDC{ddc} sequence not contiguous from 0: {iq[:6]}")
            return False
        if st < 3:
            finding("UNHEALTHY", label, f"only {st} status packets in 1.6 s")
            return False
        return True
    finally:
        s.close()
        ctl.close()


def after(tgt, label, handles_before=None, max_growth=40):
    """Common tail of every attack: traceback scan, health probe, leak check."""
    time.sleep(0.4)
    healthy = health(tgt, label)
    out = tgt.output()
    if "Traceback" in out or "Exception in thread" in out:
        i = out.index("Traceback") if "Traceback" in out else out.index("Exception in thread")
        finding("TRACEBACK", label, out[i:i + 400].replace("\n", " | "))
        tgt.out.clear()
        healthy = False
    if healthy:
        ok(label)
    if handles_before is not None:
        now = tgt.handles()
        if now is not None and now - handles_before > max_growth:
            finding("LEAK", label, f"handles {handles_before} -> {now}")
    return healthy


# ------------------------------------------------------------------ attacks
def attack_malformed(tgt, quick):
    """Garbage of every length at every listening port: zero-length, tiny, the boundaries
    the parsers test (4, 5, 7, 8, 43, 44, 59, 60, 1363, 1364, 1444), a full MTU, and the
    maximum a UDP datagram can carry."""
    sizes = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 43, 44, 45, 59, 60, 61, 1362, 1363, 1364, 1443,
             1444, 1445, 1472, 9000, 65507]
    if quick:
        sizes = [0, 1, 5, 8, 44, 60, 1363, 1364, 1444, 65507]
    rng = random.Random(7)
    s = client()
    for port in PORTS_IN:
        for size in sizes:
            for fill in (b"\x00", b"\xff", None):
                data = rng.randbytes(size) if fill is None else fill * size
                try:
                    s.sendto(data, (IP, port))
                except OSError:
                    pass
    s.close()
    after(tgt, "malformed packets of every size at every port")


def attack_hostile_fields(tgt, quick):
    s, ctl = client(), client()
    ctl.sendto(ddc_spec(0xFF), (IP, 1025))                         # every mask bit
    s.sendto(general(), (IP, 1024))
    s.sendto(hp(True, {n: 0xFFFFFFFF for n in range(8)}), (IP, 1027))
    time.sleep(0.5)
    s.sendto(hp(False), (IP, 1027))
    ctl.sendto(ddc_spec(0x00), (IP, 1025))
    after(tgt, "mask 0xFF (all DDCs), all tuning words 0xFFFFFFFF, then mask 0")
    for khz in (0, 1, 2, 47, 49, 191, 1535, 1537, 40000, 65535):
        ctl.sendto(ddc_spec(0x04, {n: khz for n in range(8)}), (IP, 1025))
    after(tgt, "non-P2 sample-rate records (0, 1, 47, 49, 1537, 65535) in every DDC record")
    for port in (0, 1, 1024, 1025, 1035, 65535):
        g = bytearray(general())
        struct.pack_into(">H", g, 5, port)                          # port re-assignment
        s.sendto(bytes(g), (IP, 1024))
    after(tgt, "General Packet port re-assignments (0, 1, 1024, 1025, 1035, 65535)")
    s.close()
    ctl.close()


def attack_flapping(tgt, quick):
    secs = 2.0 if quick else 5.0
    s, ctl = client(), client()
    s.sendto(general(), (IP, 1024))
    t_end = time.time() + secs
    n = 0
    while time.time() < t_end:                                      # run bit flapping
        s.sendto(hp(n % 2 == 0, {2: 14175000}), (IP, 1027))
        ctl.sendto(ddc_spec(0x04, {2: 48}), (IP, 1025))
        n += 1
    after(tgt, f"run bit flapped {n} times in {secs:.0f} s")
    h0 = tgt.handles()
    rng = random.Random(3)
    t_end = time.time() + secs
    n = 0
    while time.time() < t_end:                                      # mask flapping: bind/close churn
        ctl.sendto(ddc_spec(rng.randrange(256), {i: rng.choice((48, 96, 192)) for i in range(8)}),
                   (IP, 1025))
        s.sendto(hp(True, {i: 14175000 for i in range(8)}), (IP, 1027))
        n += 1
        time.sleep(0.004)
    s.sendto(hp(False), (IP, 1027))
    after(tgt, f"enable mask randomised {n} times in {secs:.0f} s (socket bind/close churn)", h0)
    t_end = time.time() + secs
    n = 0
    while time.time() < t_end:                                      # rate flapping: ring re-render storm
        ctl.sendto(ddc_spec(0x04, {2: (48, 1536)[n % 2]}), (IP, 1025))
        s.sendto(hp(True, {2: 14175000}), (IP, 1027))
        n += 1
        time.sleep(0.002)
    s.sendto(hp(False), (IP, 1027))
    after(tgt, f"DDC2 rate flapped 48k<->1536k {n} times in {secs:.0f} s")
    t_end = time.time() + secs
    n = 0
    while time.time() < t_end:                                      # diversity flapping
        ctl.sendto(ddc_spec(0x01, {0: 48, 1: (48, 96)[n % 2]}, byte1363=(0x02, 0x00)[n % 2]), (IP, 1025))
        s.sendto(hp(True, {0: 14175000}), (IP, 1027))
        n += 1
        time.sleep(0.003)
    s.sendto(hp(False), (IP, 1027))
    after(tgt, f"diversity byte 1363 flapped {n} times with a mismatched DDC1 rate")
    s.close()
    ctl.close()


def count_iq(sock, seconds, port=1037):
    n, t_end = 0, time.time() + seconds
    while time.time() < t_end:
        sock.settimeout(0.05)
        try:
            d, frm = sock.recvfrom(4096)
            if frm[1] == port:
                n += 1
        except socket.timeout:
            pass
    return n


def attack_hijack(tgt, quick):
    """Two clients fighting over the session, and a stranger redirecting the stream. Measured
    one socket at a time: a socket that is not being read simply buffers, so counting both in
    one loop with short timeouts under-counts the one that is receiving."""
    a, b, ctl = client(), client(), client()
    ctl.sendto(ddc_spec(0x04, {2: 48}), (IP, 1025))
    a.sendto(general(), (IP, 1024))
    a.sendto(hp(False), (IP, 1027))
    time.sleep(0.2)
    a.sendto(hp(True, {2: 14175000}), (IP, 1027))
    before = count_iq(a, 1.0)
    b.sendto(hp(True, {2: 14175000}), (IP, 1027))                  # a stranger says "stream to me"
    time.sleep(0.2)
    got_a = count_iq(a, 1.0)
    got_b = count_iq(b, 1.0)
    if before > 100 and got_b > 100 and got_a < 5:
        finding("HIJACK", "one unauthenticated High Priority packet from another host moved the stream",
                f"legitimate client {before} -> {got_a} IQ packets/s, the stranger got {got_b}. A real "
                "radio behaves the same, but it also lets anyone aim the sim's stream (up to ~75 Mbit/s "
                "at 1536k) at a spoofed victim address. Mitigation: --allow CIDR")
    elif before > 100 and got_a > 100:
        ok(f"stream stayed with the first client ({before} -> {got_a}/s); stranger got {got_b}")
    else:
        finding("UNHEALTHY", "hijack scenario", f"before {before}, after A {got_a}, B {got_b}")
    for _ in range(300):                                            # alternate: pc_addr ping-pong
        a.sendto(hp(True, {2: 14175000}), (IP, 1027))
        b.sendto(hp(True, {2: 14175000}), (IP, 1027))
    a.sendto(hp(False), (IP, 1027))
    after(tgt, "two clients alternating High Priority packets 600 times")
    a.close()
    b.close()
    ctl.close()


def attack_discovery_flood(tgt, quick):
    """Tens of thousands of discovery probes as fast as Python can send them (the reply is the
    same size as the probe, so there is no amplification, but the sim must not choke or leak),
    from several sockets at once."""
    secs = 2.0 if quick else 4.0
    socks = [client() for _ in range(4)]
    probe = discovery()
    sent, t_end = 0, time.time() + secs
    while time.time() < t_end:
        for s in socks:
            try:
                s.sendto(probe, (IP, 1024))
                sent += 1
            except OSError:
                pass
    for s in socks:
        drain(s, 0.2)
        s.close()
    h = tgt.handles()
    after(tgt, f"{sent} discovery probes in {secs:.0f} s from 4 sockets", h)


def attack_load(tgt, quick):
    """Four DDCs at 1536 kHz (~26,000 packets/s) while a second client asks for discovery
    and a mask change: the control plane must stay responsive under data-plane load."""
    s, ctl, probe = client(), client(), client()
    ctl.sendto(ddc_spec(0x3C, {n: 1536 for n in range(2, 6)}), (IP, 1025))
    s.sendto(general(), (IP, 1024))
    s.sendto(hp(True, {n: 14175000 for n in range(2, 6)}), (IP, 1027))
    drain(s, 1.0)
    lat = []
    for _ in range(5 if quick else 10):
        t0 = time.time()
        probe.settimeout(3)
        probe.sendto(discovery(), (IP, 1024))
        try:
            probe.recvfrom(2048)
            lat.append(time.time() - t0)
        except socket.timeout:
            lat.append(3.0)
        drain(s, 0.15)
    worst = max(lat)
    if worst > 1.0:
        finding("SLOW", "discovery reply under 4 x 1536k load", f"worst {worst*1000:.0f} ms")
    else:
        ok(f"discovery answered under 4 x 1536k load (worst {worst*1000:.0f} ms)")
    t0 = time.time()
    ctl.sendto(ddc_spec(0x1C, {n: 1536 for n in range(2, 5)}), (IP, 1025))   # drop DDC5
    gone = None
    t_end = time.time() + 3
    last5 = t0
    while time.time() < t_end:
        s.settimeout(0.05)
        try:
            d, frm = s.recvfrom(4096)
            if frm[1] == 1040:
                last5 = time.time()
        except socket.timeout:
            pass
    gone = last5 - t0
    if gone > 1.0:
        finding("SLOW", "a DDC dropped from the mask kept streaming under load", f"{gone*1000:.0f} ms")
    else:
        ok(f"mask change took effect under load ({gone*1000:.0f} ms)")
    s.sendto(hp(False), (IP, 1027))
    drain(s, 0.5)
    after(tgt, "sustained 4 x 1536k load")
    s.close()
    ctl.close()
    probe.close()


def attack_startup_args():
    """Hostile command lines: the sim must refuse them with a clean message, or run -- never
    start and then die in a thread."""
    cases = [("--allow", "not-a-network"), ("--tone", "nan"), ("--tone", "inf"), ("--tone", "-1000"), ("--tone", "0"),
             ("--tone", "1e12"), ("--tone", "777"), ("--ddc", "0"), ("--ddc", "9"),
             ("--ddc", "300"), ("--gateware", "-1"), ("--gateware", "300"), ("--supply-volts", "-5"),
             ("--supply-volts", "1e9"), ("--supply-volts", "nan"), ("--adc-peak", "70000"),
             ("--adc-peak", "-1"), ("--adc-overload", "99"), ("--mac", "zz"), ("--mac", "1:2:3")]
    for flag, val in cases:
        label = f"startup {flag} {val}"
        try:
            t = Target(args=(flag, val))
        except RuntimeError as e:
            r = subprocess.run([sys.executable, str(ROOT / "anan_sim.py"), "--ip", IP, flag, val],
                               capture_output=True, text=True, timeout=30, cwd=str(ROOT))
            msg = (r.stdout + r.stderr)
            if "Traceback" in msg:
                finding("TRACEBACK", label, msg.strip().splitlines()[-1][:160])
            else:
                ok(f"{label}: refused cleanly")
            continue
        try:
            if health(t, label) and "Traceback" not in t.output():
                ok(f"{label}: runs and a normal client still works")
            elif "Traceback" in t.output():
                finding("TRACEBACK", label, t.output()[-300:].replace("\n", " | "))
        finally:
            t.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-startup", action="store_true", help="skip the hostile-command-line phase")
    ap.add_argument("--heavy", action="store_true", help="allow load/churn against a sim you did not spawn")
    ap.add_argument("--pid", type=int, default=None, help="attack a RUNNING sim with this pid (loopback)")
    ap.add_argument("--sim-args", nargs="*", default=[], help="arguments for the spawned sim")
    a = ap.parse_args()

    tgt = Target(spawn=a.pid is None, args=a.sim_args, pid=a.pid)
    t0 = time.time()
    try:
        print("baseline")
        if not health(tgt, "baseline"):
            print("sim unhealthy before any attack; stopping")
            return 2
        ok("baseline: discovery + a clean DDC2 session")
        h0 = tgt.handles()
        print(f"  handles at start: {h0}")
        phases = [("malformed input", attack_malformed), ("hostile field values", attack_hostile_fields),
                  ("two clients / hijack", attack_hijack), ("discovery flood", attack_discovery_flood)]
        if a.pid is None or a.heavy:
            phases += [("flapping and churn", attack_flapping), ("sustained load", attack_load)]
        for name, fn in phases:
            print(f"\n== {name}")
            fn(tgt, a.quick)
        h1 = tgt.handles()
        print(f"\n  handles at end of the attacks: {h1} (start {h0})")
        if h0 is not None and h1 is not None and h1 - h0 > 40:
            finding("LEAK", "whole run", f"handles {h0} -> {h1}")
        if a.pid is None and not a.skip_startup:
            tgt.close()                       # the next sims need the ports the main one holds
            print("\n== hostile command lines")
            attack_startup_args()
    finally:
        tgt.close()
    print(f"\n{checks} checks passed, {len(findings)} finding(s) in {time.time()-t0:.0f} s")
    for k, w, d in findings:
        print(f"  {k:10} {w}\n             {d}")
    return 0 if not [f for f in findings if f[0] != "HIJACK"] else 1


if __name__ == "__main__":
    sys.exit(main())
