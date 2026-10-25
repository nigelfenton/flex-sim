#!/usr/bin/env python3
"""Pin the red-team fixes: the sim must keep working after hostile or careless input.

Each case here is a bug a red team of v0.3.0 found, written so it FAILS on v0.3.0:

  1. ONE oversize datagram to a listening port killed that port's thread (Windows raises
     WSAEMSGSIZE from recvfrom and every loop treated any OSError as "socket closed").
  2. A client that vanished killed the DDC Specific thread: the status and mic senders share
     the :1025/:1026 sockets, a closed client port returns ICMP unreachable, and Windows raises
     WSAECONNRESET from the NEXT recvfrom on that socket -- same "break". The sim then
     silently ignored every mask and rate packet until restarted.
  3. Hostile command lines (--tone nan, --supply-volts nan ...) started and then died in a
     thread with a traceback; they must be refused with a message.
  4. A client flapping the rate re-rendered the signal ring each time (a CPU storm); rings
     are now cached, and a tone is nudged a few Hz so its ring stays short.
  5. A thread that raises must be restarted with the traceback logged, never left dead
     while the process keeps answering discovery.
  6. --allow: only listed hosts may start or redirect a session.

The Windows-only failures (1, 2) pass on Linux either way -- the Windows CI job is what
guards them. The health probe rotates the DDC it enables, because a sim that has stopped
reading DDC Specific packets keeps streaming whatever mask it last accepted.

Run: python3 tests/test_anan_robustness.py   (spawns its own sims on loopback)
"""

import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import anan_sim  # noqa: E402
from test_anan_multi_ddc import (Sim, check, client, collect, ddc_spec, discover,  # noqa: E402
                                 general_packet, hp_packet, seqs)
import test_anan_multi_ddc as base  # noqa: E402

IP = "127.0.0.1"
_rot = [1]


def next_ddc():
    _rot[0] = 2 + (_rot[0] - 1) % 6
    n = _rot[0]
    _rot[0] += 1
    return n


def healthy(label):
    """Discover, enable a DDC the sim has NOT been told about recently, stream it from a fresh
    run edge: contiguous sequence from 0, status packets arriving."""
    ddc = next_ddc()
    s, ctl = client(), client()
    try:
        s.settimeout(3)
        s.sendto(bytes(60)[:4] + b"\x02" + bytes(55), (IP, 1024))
        try:
            s.recvfrom(2048)
        except socket.timeout:
            check(f"{label}: discovery still answered", False, "no reply")
            return False
        s.sendto(general_packet(), (IP, 1024))
        s.sendto(hp_packet(False), (IP, 1027))
        time.sleep(0.15)
        collect(s, 0.3, ports=range(1025, 1045))
        ctl.sendto(ddc_spec(1 << ddc, {ddc: 48}), (IP, 1025))
        time.sleep(0.05)
        s.sendto(hp_packet(True, centres={ddc: 14_175_000}), (IP, 1027))
        got = collect(s, 1.5, ports=(1035 + ddc, 1025))
        s.sendto(hp_packet(False), (IP, 1027))
        iq = seqs(got.get(1035 + ddc, []))
        ok = len(iq) >= 150 and iq[0] == 0 and all(b - a == 1 for a, b in zip(iq, iq[1:]))
        check(f"{label}: a fresh session on DDC{ddc} streams from seq 0 (DDC Specific thread alive)",
              ok, f"{len(iq)} packets, first {iq[:4]}")
        check(f"{label}: status packets still flow", len(got.get(1025, [])) >= 3,
              f"{len(got.get(1025, []))}")
        return ok
    finally:
        s.close()
        ctl.close()


# ------------------------------------------------------------------ wire cases
def case_oversize_datagrams_do_not_kill_listeners():
    with Sim() as sim:
        x = client()
        for port in (1024, 1025, 1026, 1027, 1028, 1029):
            for size in (1445, 9000, 65507):
                try:
                    x.sendto(bytes(size), (IP, port))
                except OSError:
                    pass
        time.sleep(0.5)
        x.close()
        healthy("after oversize datagrams (1445 / 9000 / 65507 B) at every port")
        log = sim.log()
    check("no traceback and no thread restart was needed", "Traceback" not in log and "crashed" not in log,
          log[-300:])


def case_a_vanished_client_does_not_kill_ddc_specific():
    """The sim streams status, mic and IQ to a client that closes its socket without stopping.
    The closed port answers ICMP unreachable; Windows reports it on the next recvfrom of the
    same socket."""
    with Sim() as sim:
        for round_ in range(3):
            s, ctl = client(), client()
            ctl.sendto(ddc_spec(0x04, {2: 48}), (IP, 1025))
            s.sendto(general_packet(), (IP, 1024))
            s.sendto(hp_packet(True, centres={2: 14_175_000}), (IP, 1027))
            time.sleep(0.4)
            s.close()                     # vanish: no run=0, the sim keeps sending to a dead port
            ctl.close()
            time.sleep(1.2)               # long enough for status (232 ms) and mic to hit the ICMP
        healthy("after a client vanished three times without stopping")
        log = sim.log()
    check("no traceback and no thread restart was needed", "Traceback" not in log and "crashed" not in log,
          log[-300:])


def case_garbage_of_every_size_at_every_port():
    import random
    rng = random.Random(11)
    with Sim() as sim:
        x = client()
        for port in (1024, 1025, 1026, 1027, 1028, 1029):
            for size in (0, 1, 3, 4, 5, 7, 8, 43, 44, 59, 60, 61, 1362, 1363, 1364, 1444, 1500):
                for fill in (b"\x00", b"\xff", None):
                    data = rng.randbytes(size) if fill is None else fill * size
                    try:
                        x.sendto(data, (IP, port))
                    except OSError:
                        pass
        time.sleep(0.4)
        x.close()
        healthy("after garbage of every parser-boundary size at every port")
        log = sim.log()
    check("no traceback and no thread restart was needed", "Traceback" not in log and "crashed" not in log,
          log[-300:])


def case_flapping_keeps_the_control_plane_alive():
    with Sim() as sim:
        s, ctl = client(), client()
        s.sendto(general_packet(), (IP, 1024))
        t_end = time.time() + 2.5
        n = 0
        while time.time() < t_end:
            ctl.sendto(ddc_spec(0x04, {2: (48, 1536)[n % 2]}, diversity=(0x02, 0x00)[(n // 3) % 2]), (IP, 1025))
            s.sendto(hp_packet(n % 5 != 0, centres={2: 14_175_000}), (IP, 1027))
            n += 1
            time.sleep(0.002)
        s.sendto(hp_packet(False), (IP, 1027))
        s.close()
        ctl.close()
        healthy(f"after {n} rate/run/diversity flaps in 2.5 s")
        sim.log()


def case_allow_blocks_a_stranger_but_not_discovery():
    with Sim("--allow", "10.255.255.0/24") as sim:
        s, ctl = client(), client()
        reply = discover(s)
        check("discovery still answers a host outside --allow (it is a 60-byte reply to a 60-byte probe)",
              len(reply) == 60)
        ctl.sendto(ddc_spec(0x04, {2: 48}), (IP, 1025))
        s.sendto(general_packet(), (IP, 1024))
        s.sendto(hp_packet(True, centres={2: 14_175_000}), (IP, 1027))
        got = collect(s, 1.2)
        check("a host outside --allow cannot start a session (no IQ)", got == {}, f"streams {sorted(got)}")
        s.close()
        ctl.close()
        log = sim.log()
    check("the refusal is logged, so a wrong --allow is diagnosable", "ignored: not in --allow" in log)
    with Sim("--allow", "127.0.0.0/8") as sim:
        s, ctl = client(), client()
        discover(s)
        ctl.sendto(ddc_spec(0x04, {2: 48}), (IP, 1025))
        s.sendto(general_packet(), (IP, 1024))
        s.sendto(hp_packet(True, centres={2: 14_175_000}), (IP, 1027))
        got = collect(s, 1.2)
        check("a host inside --allow can", 1037 in got, f"streams {sorted(got)}")
        s.sendto(hp_packet(False), (IP, 1027))
        s.close()
        ctl.close()
        sim.log()


# ------------------------------------------------------------------ command line + pure cases
def case_hostile_command_lines_are_refused_cleanly():
    bad = [("--tone", "nan"), ("--tone", "inf"), ("--tone", "1e12"), ("--gateware", "-1"),
           ("--gateware", "300"), ("--supply-volts", "nan"), ("--supply-volts", "-5"),
           ("--supply-volts", "1e9"), ("--adc-peak", "70000"), ("--adc-overload", "99"),
           ("--ddc", "0"), ("--ddc", "300"), ("--allow", "not-a-network")]
    for flag, val in bad:
        r = subprocess.run([sys.executable, str(ROOT / "anan_sim.py"), "--ip", IP, flag, val],
                           capture_output=True, text=True, timeout=30, cwd=str(ROOT))
        msg = r.stdout + r.stderr
        check(f"{flag} {val}: refused with a message, exit != 0, no traceback",
              r.returncode != 0 and "Traceback" not in msg and msg.strip() != "", f"rc {r.returncode}: {msg[-160:]}")
    for flag, val in (("--ddc", "10"), ("--tone", "-1000"), ("--tone", "0")):
        with Sim(flag, val) as sim:
            healthy(f"{flag} {val} is accepted and works")
            sim.log()


def case_rings_are_cached_and_tones_nudged():
    anan_sim._RING_CACHE.clear()
    a = anan_sim.IqSource("tone", 1536000, 1000.0)
    b = anan_sim.IqSource("tone", 1536000, 1000.0)
    check("two sources with the same parameters share ONE rendered ring (no re-render storm)",
          a.ring is b.ring)
    a.set_rate(48000)
    a.set_rate(1536000)
    check("flapping the rate 1536k -> 48k -> 1536k re-uses the cached ring", a.ring is b.ring)
    t0 = time.perf_counter()
    for i in range(500):
        a.set_rate((48000, 1536000)[i % 2])
    dt = time.perf_counter() - t0
    check("500 rate flaps cost milliseconds, not a re-render each", dt < 0.5, f"{dt:.2f} s")
    f = anan_sim.tone_for_ring(1536000, 777.0)
    check("an odd tone at 1536k is nudged a few Hz so its ring stays short (777 -> 776, not a 512,000-sample ring)",
          abs(f - 777) <= 3 and 1536000 // __import__("math").gcd(1536000, f) <= anan_sim.MAX_RING_SAMPLES, f"{f}")
    c = anan_sim.IqSource("tone", 1536000, 777.0)
    check("and the rendered ring really is short", c.period <= anan_sim.MAX_RING_SAMPLES, f"{c.period}")
    check("1000 Hz is untouched at every rate",
          all(anan_sim.tone_for_ring(r, 1000.0) == 1000 for r in anan_sim.DDC_RATES))


def case_a_crashing_thread_is_restarted_not_silently_dead():
    sim = anan_sim.AnanSim(IP, bytes(6), 48000, "tone", 1000.0, 4)
    calls = []

    def flaky():
        calls.append(time.time())
        if len(calls) < 3:
            raise RuntimeError("boom")

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sim._supervised(flaky)()
    out = buf.getvalue()
    check("a thread body that raises twice is run a third time (restarted)", len(calls) == 3, f"{len(calls)} calls")
    check("each crash is logged WITH its traceback", out.count("crashed") == 2 and "RuntimeError: boom" in out, out[-200:])
    sim.stop = True
    n = len(calls)
    sim._supervised(flaky)()
    check("a stopped sim does not restart anything", len(calls) == n)


def main():
    for fn in (case_rings_are_cached_and_tones_nudged,
               case_a_crashing_thread_is_restarted_not_silently_dead,
               case_hostile_command_lines_are_refused_cleanly,
               case_oversize_datagrams_do_not_kill_listeners,
               case_a_vanished_client_does_not_kill_ddc_specific,
               case_garbage_of_every_size_at_every_port,
               case_flapping_keeps_the_control_plane_alive,
               case_allow_blocks_a_stranger_but_not_discovery):
        print(fn.__name__)
        try:
            fn()
        except RuntimeError as e:
            check(f"{fn.__name__}: sim started", False, str(e))
    print(f"\n{base.passed} passed, {base.failed} failed")
    return 0 if base.failed == 0 else 1


def _pytest(*fns):
    base.passed = base.failed = 0
    try:
        for fn in fns:
            fn()
    except RuntimeError as e:
        import pytest
        pytest.skip(f"anan_sim could not be started (ports 1024-1044 busy?): {e}")
    assert base.failed == 0, f"{base.failed} check(s) failed -- see output above"


def test_pure_robustness():
    _pytest(case_rings_are_cached_and_tones_nudged, case_a_crashing_thread_is_restarted_not_silently_dead)


def test_command_lines():
    _pytest(case_hostile_command_lines_are_refused_cleanly)


def test_hostile_packets():
    _pytest(case_oversize_datagrams_do_not_kill_listeners, case_a_vanished_client_does_not_kill_ddc_specific,
            case_garbage_of_every_size_at_every_port)


def test_flapping_and_allow():
    _pytest(case_flapping_keeps_the_control_plane_alive, case_allow_blocks_a_stranger_but_not_discovery)


if __name__ == "__main__":
    sys.exit(main())
