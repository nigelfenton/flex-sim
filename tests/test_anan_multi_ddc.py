#!/usr/bin/env python3
"""Pin anan_sim v0.2: several DDCs at once, DDCs the radio lacks, and the
EXPERIMENTAL diversity framing.

What is and is not evidence here, so a green run is not over-read:

  - Every DDC framing fact (16 + 1428 B, 238 samples, the conjugate handedness)
    is verified against real-G2 captures and is pinned in test_anan_p2.py.
  - Several DDCs streaming AT ONCE, each from its own port 1035+n with its own
    sequence counter and commanded rate, follows the documented per-DDC ports
    (Saturn project_documentation) but has NO real-radio capture behind it yet.
  - The diversity framing (High Priority byte 1363 bit 1 -> DDC0 packets carry 119
    interleaved DDC0/DDC1 pairs) comes from one real-G2 owner's description, and
    the packet CADENCE and DDC1's own stream are GUESSES. These tests pin what the
    sim DOES so a change to it is deliberate; they do not prove a real G2 agrees.
  - The signal-source tests (pure functions, no sockets) are about the SIM's
    generator: phase-continuous across the ring seam, handedness, and that a
    packet is a slice, not a per-sample Python loop (the v0.1 sender topped out
    near 540 packets/s on a Celeron J1900; this generator does millions/s).

Run: python3 tests/test_anan_multi_ddc.py   (spawns its own sims on loopback)
"""

import cmath
import math
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import anan_sim  # noqa: E402

IP = "127.0.0.1"
PORT_DISCOVERY, PORT_DDC_SPEC, PORT_HP_IN = 1024, 1025, 1027
IQ_HEADER = 16
IQ_SAMPLES = 238
IQ_BYTES = IQ_HEADER + IQ_SAMPLES * 6
FULL_SCALE_24 = (1 << 23) - 1
TONE_HZ = 1000.0

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------- helpers
def s24(b):
    v = int.from_bytes(b, "big")
    return v - (1 << 24) if v & 0x800000 else v


def samples_of(raw):
    """24-bit I/Q bytes (6 per sample) -> list of complex."""
    return [complex(s24(raw[o:o + 3]), s24(raw[o + 3:o + 6]))
            for o in range(0, len(raw), 6)]


def tone_hz_of(iq, rate):
    """Signed tone frequency from the mean phase step (what a client sees)."""
    steps = [cmath.phase(b * a.conjugate()) for a, b in zip(iq, iq[1:])]
    return sum(steps) / len(steps) * rate / (2 * math.pi)


def hz_to_word(hz):
    return round(hz * (1 << 32) / anan_sim.PHASE_WORD_CLOCK_HZ)


def general_packet():
    """General Packet to :1024. Byte 37 bit 3 = High Priority frequencies are
    PHASE WORDS (what a real client sends); all-zero would tell the sim they are
    Hz and the tuning would decode as nonsense."""
    b = bytearray(60)
    b[37] = 0x08
    return bytes(b)


def hp_packet(run, diversity=False, centres=None):
    """High Priority from the PC: run bit, a phase word per DDC, and (diversity)
    byte 1363 bit 1 -- 1444 B like piHPSDR's buffer, so byte 1363 exists."""
    b = bytearray(1444)
    b[4] = 0x01 if run else 0x00
    for n, hz in (centres or {}).items():
        struct.pack_into(">I", b, 9 + 4 * n, hz_to_word(hz))
    if diversity:
        b[1363] = 0x02
    return bytes(b)


def ddc_spec(mask, rates_khz):
    """DDC Specific: enable mask in byte 7, rate record n at 18+6n (kHz, BE u16)."""
    b = bytearray(1444)
    b[4] = 2
    b[7] = mask
    for n, khz in rates_khz.items():
        b[17 + 6 * n] = 0
        struct.pack_into(">H", b, 18 + 6 * n, khz)
    return bytes(b)


class Sim:
    """A live anan_sim on loopback. Context manager so a failed assertion cannot
    leave a process holding the fixed ports for the next test."""

    def __init__(self, *args, env=None):
        self.args = list(args)
        self.env = env

    def __enter__(self):
        self.p = subprocess.Popen(
            [sys.executable, str(ROOT / "anan_sim.py"), "--ip", IP, *self.args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
            env={**os.environ, **self.env} if self.env else None)
        t0 = time.time()
        while time.time() - t0 < 15:
            line = self.p.stdout.readline().decode(errors="replace")
            if "waiting for discovery" in line:
                return self
            if not line and self.p.poll() is not None:
                break
        self.p.kill()
        raise RuntimeError("sim never came up")

    def log(self):
        """Everything the sim printed, read after it is stopped."""
        self.p.kill()
        self.p.wait()
        return self.p.stdout.read().decode(errors="replace")

    def __exit__(self, *exc):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait()
        return False


def client():
    """One socket for the whole session, like Thetis/NereusSDR (the sim streams
    to the source of the General / HP-in packets)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((IP, 0))
    return s


def discover(s):
    req = bytearray(60)
    req[4] = 0x02
    s.settimeout(5)
    s.sendto(bytes(req), (IP, PORT_DISCOVERY))
    reply, _ = s.recvfrom(2048)
    return reply


def collect(s, seconds, ports=range(1035, 1045), limit=4000):
    """Every IQ datagram for `seconds`, as {source_port: [(t, bytes)]}. Demuxed
    by the radio-side SOURCE port the way a real client does; HP status (:1025)
    shares the socket and is skipped."""
    out = {}
    t_end = time.time() + seconds
    n = 0
    while time.time() < t_end and n < limit:
        s.settimeout(max(0.05, t_end - time.time()))
        try:
            d, frm = s.recvfrom(4096)
        except socket.timeout:
            break
        if frm[1] in ports and d:
            out.setdefault(frm[1], []).append((time.time(), d))
            n += 1
    return out


def seqs(pkts):
    return [struct.unpack_from(">I", d, 0)[0] for _, d in pkts]


def cadence(pkts):
    return (pkts[-1][0] - pkts[0][0]) / (len(pkts) - 1)


def iq_of(pkts, upto=3):
    """Concatenated payload samples of the first few packets."""
    return samples_of(b"".join(d[IQ_HEADER:] for _, d in pkts[:upto]))


# ---------------------------------------------------------------- pure tests
def case_tone_ring_is_phase_continuous_across_the_seam():
    """A ring that wraps with a phase jump clicks once per period -- and is
    invisible if you only look inside one packet. Step through several full ring
    cycles at awkward rate/tone pairs and bound the per-sample phase error."""
    for rate, tone in ((48000, 1000.0), (96000, 777.0), (192000, 1000.0), (48000, 1234.0)):
        src = anan_sim.IqSource("tone", rate, tone)
        iq = samples_of(b"".join(src.take(IQ_SAMPLES) for _ in range(src.period * 3 // IQ_SAMPLES + 3)))
        nominal = -2 * math.pi * round(tone) / rate          # conjugate handedness
        worst = max(abs(cmath.phase(b * a.conjugate()) - nominal) for a, b in zip(iq, iq[1:]))
        check(f"tone {tone:g} Hz @ {rate} S/s: phase step stays on nominal across ring wraps "
              f"(period {src.period})", worst < 1e-4, f"worst {worst:.2e} rad")


def case_tone_handedness_and_amplitude():
    src = anan_sim.IqSource("tone", 48000, 1000.0)
    iq = samples_of(src.take(IQ_SAMPLES))
    f = tone_hz_of(iq, 48000)
    check("+1000 Hz tone arrives at -1000 Hz (real HPSDR wire handedness)",
          abs(f + 1000.0) < 20, f"measured {f:+.1f} Hz")
    peak = max(abs(z) for z in iq)
    check("tone amplitude is a quarter of full scale", abs(peak - FULL_SCALE_24 * 0.25) < FULL_SCALE_24 * 0.01,
          f"peak {peak:.0f}")


def case_generator_is_a_slice_not_a_per_sample_loop():
    """v0.1 built packets sample by sample and managed ~540/s on the hub. A
    packet must now be a slice. Bound is deliberately loose (a Celeron, a CI
    runner): 20,000 packets in under 2 s is 10,000/s; the real figure is millions."""
    src = anan_sim.IqSource("tone", 1536000, 1000.0)
    t = time.perf_counter()
    for _ in range(20000):
        src.take(IQ_SAMPLES)
    dt = time.perf_counter() - t
    check("20,000 packets of tone generated in < 2 s (>= 10k packets/s)", dt < 2.0, f"{dt:.2f} s")


def case_interleave_layout_and_other_patterns():
    a = b"".join(bytes([0xA0 + i] * 6) for i in range(4))
    b = b"".join(bytes([0xB0 + i] * 6) for i in range(4))
    out = anan_sim.interleave_pairs(a, b)
    want = b"".join(a[i * 6:i * 6 + 6] + b[i * 6:i * 6 + 6] for i in range(4))
    check("interleave_pairs lays samples out a0 b0 a1 b1 ...", out == want)
    check("zero pattern is silence", anan_sim.IqSource("zero", 48000, 1000.0).take(IQ_SAMPLES) == bytes(1428))
    n1 = anan_sim.IqSource("noise", 48000, 1000.0, seed=3).take(IQ_SAMPLES)
    n2 = anan_sim.IqSource("noise", 48000, 1000.0, seed=3).take(IQ_SAMPLES)
    n3 = anan_sim.IqSource("noise", 48000, 1000.0, seed=4).take(IQ_SAMPLES)
    check("noise is reproducible per seed and differs between seeds", n1 == n2 and n1 != n3)
    check("each DDC carries a distinct tone: DDC0-2 base, DDC3 2x, DDC4 3x",
          [anan_sim.ddc_tone_hz(1000.0, n) for n in range(5)] == [1000.0, 1000.0, 1000.0, 2000.0, 3000.0])


# ---------------------------------------------------------------- wire tests
def case_multi_ddc_streams_run_at_once_with_independent_rates():
    with Sim() as sim:
        s = client()
        reply = discover(s)
        check("discovery advertises 4 DDCs by default", reply[20] == 4, f"got {reply[20]}")
        check("discovery reports gateware 27 by default", reply[13] == 27, f"got {reply[13]}")

        # RX1 on DDC2 @48k, RX2 on DDC3 @96k -- the second-receiver shape.
        ctl = client()
        ctl.sendto(ddc_spec(0x0C, {2: 48, 3: 96}), (IP, PORT_DDC_SPEC))
        s.sendto(general_packet(), (IP, PORT_DISCOVERY))                      # General: session = s
        s.sendto(hp_packet(True, centres={2: 14_175_000, 3: 7_100_000}), (IP, PORT_HP_IN))
        got = collect(s, 2.0)
        ports = sorted(got)
        check("both enabled DDCs stream, each from its own port (1037, 1038)",
              ports == [1037, 1038], f"streams from {ports}")
        if ports == [1037, 1038]:
            for p in ports:
                d = got[p][0][1]
                check(f"port {p}: {IQ_BYTES} B, declares 24 bits / {IQ_SAMPLES} samples",
                      len(d) == IQ_BYTES and struct.unpack_from(">HH", d, 12) == (24, IQ_SAMPLES),
                      f"{len(d)} B {struct.unpack_from('>HH', d, 12)}")
                sq = seqs(got[p])
                check(f"port {p}: its own sequence counter, +1 per packet",
                      all(b - a == 1 for a, b in zip(sq, sq[1:])), f"{sq[:6]}")
            c48, c96 = cadence(got[1037]), cadence(got[1038])
            check("rates are per DDC: the 96k stream runs ~2x the 48k packet rate",
                  1.4 < c48 / c96 < 2.8, f"48k {c48*1000:.2f} ms vs 96k {c96*1000:.2f} ms")
            f2 = tone_hz_of(iq_of(got[1037]), 48000)
            f3 = tone_hz_of(iq_of(got[1038]), 96000)
            check("DDC2 carries the base tone (-1000 Hz on the wire)", abs(f2 + 1000) < 25, f"{f2:+.1f}")
            check("DDC3 carries a distinct 2x tone (-2000 Hz on the wire)", abs(f3 + 2000) < 50, f"{f3:+.1f}")

        # Drop DDC3 from the mask: its stream must stop, DDC2's must not.
        ctl.sendto(ddc_spec(0x04, {2: 48}), (IP, PORT_DDC_SPEC))
        time.sleep(0.4)
        collect(s, 0.3)                                                 # drain in-flight
        later = collect(s, 1.0)
        check("clearing a DDC's mask bit stops only that DDC's stream",
              1038 not in later and 1037 in later, f"streams {sorted(later)}")

        # Re-enable it: a fresh stream, sequence restarting from zero.
        ctl.sendto(ddc_spec(0x0C, {2: 48, 3: 96}), (IP, PORT_DDC_SPEC))
        again = collect(s, 1.0)
        first = seqs(again.get(1038, []))[:1]
        check("a re-enabled DDC starts a fresh stream at sequence 0", first == [0], f"first seq {first}")

        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        ctl.close()
        s.close()
        log = sim.log()
    check("log: enable mask names both DDCs and their ports", "streaming DDC[2, 3]" in log
          and "[1037, 1038]" in log, "mask line missing")
    check("log: DDC3 tuning decoded from its own High Priority word",
          "DDC3 centre -> 7.100000 MHz" in log and "DDC2 centre -> 14.175000 MHz" in log,
          "centre lines missing")


def case_a_ddc_the_radio_does_not_have_is_not_streamed():
    """The case AetherSDR PR #6697 documents as its own limit: a client enables
    DDC2 on a radio that reports fewer than three. Nothing streams -- and DDC0
    (which the radio does have) still does, so the silence is about the DDC."""
    # cp1252 output is what a piped stdout is on Windows; the warning line below
    # contains a character it cannot encode. Until v0.2 that killed the DDC Specific
    # thread (print raised), so the DDC0 check at the end failed on Windows/CI only.
    enc = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    with Sim("--ddc", "2", env=enc) as sim:
        s = client()
        reply = discover(s)
        check("--ddc 2 advertises 2 DDCs", reply[20] == 2, f"got {reply[20]}")
        ctl = client()
        ctl.sendto(ddc_spec(0x04, {2: 48}), (IP, PORT_DDC_SPEC))
        s.sendto(general_packet(), (IP, PORT_DISCOVERY))
        s.sendto(hp_packet(True, centres={2: 14_175_000}), (IP, PORT_HP_IN))
        got = collect(s, 1.5)
        check("enabling DDC2 on a 2-DDC radio streams nothing", got == {}, f"streams {sorted(got)}")
        ctl.sendto(ddc_spec(0x01, {0: 48}), (IP, PORT_DDC_SPEC))
        got = collect(s, 1.5)
        check("a DDC the radio does have (DDC0) still streams", 1035 in got, f"streams {sorted(got)}")
        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        ctl.close()
        s.close()
        log = sim.log()
    if "DDC2 enabled but" not in log:
        print("--- sim log ---")
        print(log)
    check("log says why DDC2 is silent", "DDC2 enabled but this radio advertises only 2 DDCs" in log)


def case_diversity_framing_experimental():
    """EXPERIMENTAL. Pins what the sim does for High Priority byte 1363 bit 1:
    DDC0's header still says 238 samples, but they are 119 interleaved
    DDC0/DDC1 pairs, DDC1 gets no stream of its own, and the packet rate doubles.
    The cadence and the missing DDC1 stream are guesses -- see the module docstring."""
    with Sim() as sim:
        s = client()
        discover(s)
        ctl = client()
        ctl.sendto(ddc_spec(0x03, {0: 48, 1: 48}), (IP, PORT_DDC_SPEC))
        s.sendto(general_packet(), (IP, PORT_DISCOVERY))

        s.sendto(hp_packet(True, diversity=False, centres={0: 14_175_000}), (IP, PORT_HP_IN))
        plain = collect(s, 1.5)
        check("without diversity DDC0 and DDC1 each stream", {1035, 1036} <= set(plain),
              f"streams {sorted(plain)}")
        c_plain = cadence(plain[1035]) if len(plain.get(1035, [])) > 5 else None

        s.sendto(hp_packet(True, diversity=True, centres={0: 14_175_000}), (IP, PORT_HP_IN))
        collect(s, 0.3)                                                 # drain the switch-over
        div = collect(s, 1.5)
        check("with diversity DDC1 has no stream of its own", set(div) == {1035}, f"streams {sorted(div)}")
        if 1035 in div and len(div[1035]) > 5:
            d = div[1035][0][1]
            check("diversity DDC0 packet is still 16+1428 B and declares 238 samples",
                  len(d) == IQ_BYTES and struct.unpack_from(">HH", d, 12) == (24, IQ_SAMPLES),
                  f"{len(d)} B {struct.unpack_from('>HH', d, 12)}")
            iq = iq_of(div[1035], upto=2)
            ddc0, ddc1 = iq[0::2], iq[1::2]
            a0 = sum(abs(z) for z in ddc0) / len(ddc0)
            a1 = sum(abs(z) for z in ddc1) / len(ddc1)
            check("odd slots (DDC1) are ~6 dB below even slots (DDC0)", 0.4 < a1 / a0 < 0.6, f"ratio {a1/a0:.2f}")
            check("both DDCs carry the same tone (-1000 Hz at their own 48k rate)",
                  abs(tone_hz_of(ddc0, 48000) + 1000) < 25 and abs(tone_hz_of(ddc1, 48000) + 1000) < 25,
                  f"{tone_hz_of(ddc0, 48000):+.1f} / {tone_hz_of(ddc1, 48000):+.1f}")
            dphi = math.degrees(cmath.phase(sum(b * a.conjugate() for a, b in zip(ddc0, ddc1))))
            check("DDC1 is 45 degrees round from DDC0 (so a combiner has something to do)",
                  abs(dphi + 45) < 3, f"phase difference {dphi:+.1f} deg")
            if c_plain:
                c_div = cadence(div[1035])
                check("DDC0 packet rate doubles in diversity (119 pairs per packet) [guess]",
                      c_div < c_plain * 0.75, f"plain {c_plain*1000:.2f} ms -> diversity {c_div*1000:.2f} ms")

        s.sendto(hp_packet(True, diversity=False, centres={0: 14_175_000}), (IP, PORT_HP_IN))
        collect(s, 0.3)
        back = collect(s, 1.2)
        check("clearing the diversity bit restores separate DDC0 / DDC1 streams",
              {1035, 1036} <= set(back), f"streams {sorted(back)}")
        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        ctl.close()
        s.close()
        log = sim.log()
    check("log flags the diversity framing as EXPERIMENTAL", "diversity ON" in log and "EXPERIMENTAL" in log)


def _run(fn):
    try:
        fn()
    except RuntimeError as e:
        check(f"{fn.__name__}: sim started", False, str(e))


def main():
    for fn in (case_tone_ring_is_phase_continuous_across_the_seam,
               case_tone_handedness_and_amplitude,
               case_generator_is_a_slice_not_a_per_sample_loop,
               case_interleave_layout_and_other_patterns,
               case_multi_ddc_streams_run_at_once_with_independent_rates,
               case_a_ddc_the_radio_does_not_have_is_not_streamed,
               case_diversity_framing_experimental):
        print(fn.__name__)
        _run(fn)
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def _pytest(fn):
    global passed, failed
    passed = failed = 0
    try:
        fn()
    except RuntimeError as e:
        import pytest
        pytest.skip(f"anan_sim could not be started (ports 1024-1044 busy?): {e}")
    assert failed == 0, f"{failed} check(s) failed -- see output above"


def test_pure_signal_source():
    _pytest(lambda: [f() for f in (case_tone_ring_is_phase_continuous_across_the_seam,
                                   case_tone_handedness_and_amplitude,
                                   case_generator_is_a_slice_not_a_per_sample_loop,
                                   case_interleave_layout_and_other_patterns)])


def test_wire_multi_ddc():
    _pytest(case_multi_ddc_streams_run_at_once_with_independent_rates)


def test_wire_missing_ddc():
    _pytest(case_a_ddc_the_radio_does_not_have_is_not_streamed)


def test_wire_diversity():
    _pytest(case_diversity_framing_experimental)


if __name__ == "__main__":
    sys.exit(main())
