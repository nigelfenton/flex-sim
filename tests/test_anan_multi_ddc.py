#!/usr/bin/env python3
"""Pin anan_sim v0.3: several DDCs at once, DDCs above the advertised count, diversity,
sessions, the mic stream and the status packet -- as MEASURED on a real G2.

Where each fact comes from, so a green run is not over-read:

  - 16 + 1428 B, 238 samples, the conjugate handedness: real-G2 captures (N2JXL),
    pinned in test_anan_p2.py.
  - Everything below marked [KO9B] was measured on a real G2 by Sotiris KO9B and
    reported 2026-10-25 (three run logs): discovery bytes (13 = 27, 20 = 4, 12 = 43,
    23 = 46), DDC packet timestamps always zero, per-DDC sequence counters from 0 at
    run=1, several DDCs at once on their own ports, an enabled DDC above the
    advertised count streaming normally, diversity = DDC Specific byte 1363 == 0x02
    (equality, not a bit) with the packet rate doubling and nothing from :1036,
    the ~76-packet stale burst at session start, the ~750 pkt/s mic stream, and the
    status packet's byte map and ~232 ms cadence.
  - NOT measured, and pinned only as "what the sim does": the sim's per-DDC tones, DDC1's
    -6 dB / 45 degree offset in diversity, the stale burst's CONTENT, and the mic
    stream being silence.
  - The signal-source tests (pure functions, no sockets) are about the SIM's generator:
    phase-continuous across the ring seam, handedness, and that a packet is a slice,
    not a per-sample Python loop.

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


def hp_packet(run, centres=None, byte1363=None):
    """High Priority from the PC: run bit and a phase word per DDC, 1444 B like
    piHPSDR's buffer. byte1363 is set ONLY to prove High Priority is NOT where
    diversity lives (it is in DDC Specific)."""
    b = bytearray(1444)
    b[4] = 0x01 if run else 0x00
    for n, hz in (centres or {}).items():
        struct.pack_into(">I", b, 9 + 4 * n, hz_to_word(hz))
    if byte1363 is not None:
        b[1363] = byte1363
    return bytes(b)


def ddc_spec(mask, rates_khz, diversity=None):
    """DDC Specific: enable mask in byte 7, rate record n at 18+6n (kHz, BE u16).
    `diversity` sets byte 1363 to that VALUE (a real client uses 0x02; p2app tests
    equality)."""
    b = bytearray(1444)
    b[4] = 2
    b[7] = mask
    for n, khz in rates_khz.items():
        b[17 + 6 * n] = 0
        struct.pack_into(">H", b, 18 + 6 * n, khz)
    if diversity is not None:
        b[1363] = diversity
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


def start_session(s, ctl, mask, rates, centres=None, diversity=None):
    """The shape of a real session: General (session = s), DDC Specific, High
    Priority with the run bit. DDC Specific goes first so the mask is in place."""
    ctl.sendto(ddc_spec(mask, rates, diversity), (IP, PORT_DDC_SPEC))
    s.sendto(general_packet(), (IP, PORT_DISCOVERY))
    s.sendto(hp_packet(True, centres=centres), (IP, PORT_HP_IN))


def collect(s, seconds, ports=range(1035, 1045), limit=6000):
    """Every datagram from the given radio-side SOURCE ports for `seconds`, as
    {source_port: [(t, bytes)]}. Demuxed by source port the way a real client does."""
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


def case_supply_scale_matches_the_measured_g2():
    """[KO9B] volts = counts x 23.0/1.1 x 5/4095, and 532 counts = 13.58 V (checked against
    a meter 13.68 vs 13.65). The sim derives counts from --supply-volts with the inverse."""
    volts = lambda c: c * anan_sim.SUPPLY_DIVIDER * 5 / 4095
    check("532 counts reads 13.58 V by the measured formula", abs(volts(532) - 13.58) < 0.01, f"{volts(532):.3f}")
    sim = anan_sim.AnanSim(IP, bytes(6), 48000, "tone", 1000.0, 4)
    check("the sim's default supply is 532 counts (13.58 V)", sim.supply_counts == 532, f"{sim.supply_counts}")
    pkt = sim.status_packet()
    check("status packet is 60 bytes with the measured constants",
          len(pkt) == 60 and pkt[4] == 0x10 and pkt[30] == 0x0C and pkt[59] == 0x07
          and struct.unpack_from(">H", pkt, 6)[0] == 63 and struct.unpack_from(">H", pkt, 14)[0] == 14
          and struct.unpack_from(">H", pkt, 22)[0] == 14 and struct.unpack_from(">H", pkt, 49)[0] == 1586
          and struct.unpack_from(">H", pkt, 57)[0] == 532, f"{pkt.hex()}")
    check("bytes 49-50 (the spec's 'supply') are AIN6, NOT the rail: the rail is 57-58",
          struct.unpack_from(">H", pkt, 49)[0] != struct.unpack_from(">H", pkt, 57)[0])


# ---------------------------------------------------------------- wire tests
def case_multi_ddc_streams_run_at_once_with_independent_rates():
    with Sim() as sim:
        s = client()
        reply = discover(s)
        check("[KO9B] discovery: protocol 4.3 (byte 12 = 43), gateware 27, 4 DDCs, p2app build 46",
              (reply[12], reply[13], reply[20], reply[23]) == (43, 27, 4, 46),
              f"got {(reply[12], reply[13], reply[20], reply[23])}")
        check("discovery: board 10 (Saturn), status idle, phase-word flag set",
              (reply[11], reply[4], reply[21]) == (10, 2, 1))

        # RX1 on DDC2 @48k, RX2 on DDC3 @96k -- the second-receiver shape.
        ctl = client()
        start_session(s, ctl, 0x0C, {2: 48, 3: 96}, centres={2: 14_175_000, 3: 7_100_000})
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
                check(f"[KO9B] port {p}: timestamp bytes 4-11 are always zero",
                      all(dd[4:12] == bytes(8) for _, dd in got[p]), "non-zero timestamp seen")
                sq = seqs(got[p])
                check(f"port {p}: its own sequence counter, +1 per packet, starting at 0",
                      sq[0] == 0 and all(b - a == 1 for a, b in zip(sq, sq[1:])), f"{sq[:6]}")
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

        # [KO9B] "starting from 0 at run=1": a NEW run restarts the counters of streams that
        # never went away.
        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        time.sleep(0.4)
        collect(s, 0.3)
        s.sendto(hp_packet(True, centres={2: 14_175_000, 3: 7_100_000}), (IP, PORT_HP_IN))
        fresh = collect(s, 1.0)
        f1037, f1038 = seqs(fresh.get(1037, []))[:1], seqs(fresh.get(1038, []))[:1]
        check("[KO9B] run 0 -> 1 restarts every stream's sequence at 0 (both still enabled)",
              f1037 == [0] and f1038 == [0], f"first seqs {f1037} {f1038}")

        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        ctl.close()
        s.close()
        log = sim.log()
    check("log: enable mask names both DDCs and their ports", "streaming DDC[2, 3]" in log
          and "[1037, 1038]" in log, "mask line missing")
    check("log: DDC3 tuning decoded from its own High Priority word",
          "DDC3 centre -> 7.100000 MHz" in log and "DDC2 centre -> 14.175000 MHz" in log,
          "centre lines missing")


def case_ddc_above_the_advertised_count_streams_like_a_real_g2():
    """[KO9B] DDC4 (above the advertised 4) streams normally from :1039 and nothing stalls:
    p2app reads enables for 10 DDCs and the 4 is a literal. The 'ignore it' behaviour of
    sim v0.2 was a guess, and wrong."""
    with Sim() as sim:
        s = client()
        discover(s)
        ctl = client()
        start_session(s, ctl, 0x10 | 0x01, {0: 48, 4: 48}, centres={0: 14_175_000, 4: 7_100_000})
        got = collect(s, 1.5)
        check("[KO9B] DDC4 streams from :1039 although discovery advertises 4 DDCs",
              1039 in got and 1035 in got, f"streams {sorted(got)}")
        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        ctl.close()
        s.close()
        sim.log()


def case_enforce_ddc_count_reproduces_a_radio_that_ignores_it():
    """The OTHER behaviour a client might meet, which AetherSDR PR #6697 names as a limit:
    a radio that ignores a DDC above its advertised count. Opt-in (--enforce-ddc-count);
    NOT what a real G2 does. Also forces cp1252 output, the Windows-pipe encoding: the
    warning line below contains a character it cannot encode, and until v0.2 that killed the
    DDC Specific thread (print raised), so the DDC0 check at the end failed on Windows only."""
    enc = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    with Sim("--ddc", "2", "--enforce-ddc-count", env=enc) as sim:
        s = client()
        reply = discover(s)
        check("--ddc 2 advertises 2 DDCs", reply[20] == 2, f"got {reply[20]}")
        ctl = client()
        start_session(s, ctl, 0x04, {2: 48}, centres={2: 14_175_000})
        got = collect(s, 1.5)
        check("with --enforce-ddc-count, enabling DDC2 on a 2-DDC radio streams nothing",
              got == {}, f"streams {sorted(got)}")
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


def case_diversity_as_measured_on_a_real_g2():
    """[KO9B] DDC Specific byte 1363 == 0x02 (equality; NOT High Priority): the DDC0 header
    still says 238 samples but they are 119 interleaved DDC0/DDC1 pairs, the packet rate
    doubles, nothing comes from :1036, and the pair's rate is DDC1's record (bytes 24-25).
    Byte 7 enables DDC0 only. DDC1's -6 dB / 45 degree offset is the sim's invention."""
    with Sim() as sim:
        s = client()
        discover(s)
        ctl = client()

        # plain DDC0 + DDC1 first, as the reference cadence
        start_session(s, ctl, 0x03, {0: 48, 1: 48}, centres={0: 14_175_000})
        plain = collect(s, 1.5)
        check("without diversity DDC0 and DDC1 each stream", {1035, 1036} <= set(plain),
              f"streams {sorted(plain)}")
        c_plain = cadence(plain[1035]) if len(plain.get(1035, [])) > 5 else None

        # High Priority byte 1363 is NOT diversity ([KO9B]: it lives in DDC Specific)
        s.sendto(hp_packet(True, centres={0: 14_175_000}, byte1363=0x02), (IP, PORT_HP_IN))
        collect(s, 0.3)
        hp_only = collect(s, 1.0)
        check("[KO9B] High Priority byte 1363 does NOT trigger diversity (it is a DDC Specific byte)",
              {1035, 1036} <= set(hp_only), f"streams {sorted(hp_only)}")

        # 0x03 contains the 0x02 bit but p2app tests EQUALITY: not diversity
        ctl.sendto(ddc_spec(0x03, {0: 48, 1: 48}, diversity=0x03), (IP, PORT_DDC_SPEC))
        collect(s, 0.3)
        eq = collect(s, 1.0)
        check("[KO9B] byte 1363 == 0x03 is NOT diversity (equality test, not a bit)",
              {1035, 1036} <= set(eq), f"streams {sorted(eq)}")

        # the real shape: byte 7 = DDC0 only, byte 1363 == 0x02, both rate records the same
        ctl.sendto(ddc_spec(0x01, {0: 48, 1: 48}, diversity=0x02), (IP, PORT_DDC_SPEC))
        collect(s, 0.4)                                                 # drain the switch-over
        div = collect(s, 1.5)
        check("[KO9B] with diversity DDC1 has no stream of its own (nothing from :1036)",
              set(div) == {1035}, f"streams {sorted(div)}")
        if 1035 in div and len(div[1035]) > 5:
            d = div[1035][0][1]
            check("diversity DDC0 packet is still 16+1428 B and declares 238 samples",
                  len(d) == IQ_BYTES and struct.unpack_from(">HH", d, 12) == (24, IQ_SAMPLES),
                  f"{len(d)} B {struct.unpack_from('>HH', d, 12)}")
            iq = iq_of(div[1035], upto=2)
            ddc0, ddc1 = iq[0::2], iq[1::2]
            a0 = sum(abs(z) for z in ddc0) / len(ddc0)
            a1 = sum(abs(z) for z in ddc1) / len(ddc1)
            check("DDC0 first (I0Q0 I1Q1): odd slots (DDC1) ~6 dB below even slots (DDC0)",
                  0.4 < a1 / a0 < 0.6, f"ratio {a1/a0:.2f}")
            check("both DDCs carry the same tone (-1000 Hz at their own 48k rate)",
                  abs(tone_hz_of(ddc0, 48000) + 1000) < 25 and abs(tone_hz_of(ddc1, 48000) + 1000) < 25,
                  f"{tone_hz_of(ddc0, 48000):+.1f} / {tone_hz_of(ddc1, 48000):+.1f}")
            dphi = math.degrees(cmath.phase(sum(b * a.conjugate() for a, b in zip(ddc0, ddc1))))
            check("DDC1 is 45 degrees round from DDC0 (so a combiner has something to do)",
                  abs(dphi + 45) < 3, f"phase difference {dphi:+.1f} deg")
            if c_plain:
                c_div = cadence(div[1035])
                check("[KO9B] the DDC0 packet rate doubles in diversity (119 pairs per packet)",
                      c_div < c_plain * 0.75, f"plain {c_plain*1000:.2f} ms -> diversity {c_div*1000:.2f} ms")

        # [KO9B] the pair rate is DDC1's record (bytes 24-25), not DDC0's
        ctl.sendto(ddc_spec(0x01, {0: 48, 1: 96}, diversity=0x02), (IP, PORT_DDC_SPEC))
        collect(s, 0.4)
        mixed = collect(s, 1.5)
        if 1035 in mixed and len(mixed[1035]) > 5 and c_plain:
            c_mix = cadence(mixed[1035])
            check("[KO9B] the pair's rate comes from DDC1's record: 96k pairs run ~4x the 48k plain packet rate",
                  c_mix < c_plain * 0.4, f"plain 48k {c_plain*1000:.2f} ms vs diversity (rec1=96k) {c_mix*1000:.2f} ms")
        else:
            check("diversity with a 96k DDC1 record still streams from :1035", False, f"streams {sorted(mixed)}")

        # clearing 1363 restores separate DDC0 / DDC1 streams
        ctl.sendto(ddc_spec(0x03, {0: 48, 1: 48}, diversity=0x00), (IP, PORT_DDC_SPEC))
        collect(s, 0.4)
        back = collect(s, 1.2)
        check("clearing byte 1363 restores separate DDC0 / DDC1 streams",
              {1035, 1036} <= set(back), f"streams {sorted(back)}")
        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        ctl.close()
        s.close()
        log = sim.log()
    check("log: diversity ON names the DDC Specific byte", "diversity ON (DDC Specific byte 1363 == 0x02)" in log)


def case_pair_order_can_be_swapped():
    """[KO9B] DDC0 first in the first session after power-up, but SWAPPED in 4 of 10 trials
    after DDC0 had run alone. --pair-order d1d0 reproduces the swapped order so a client's
    order detection can be exercised."""
    with Sim("--pair-order", "d1d0") as sim:
        s = client()
        discover(s)
        ctl = client()
        start_session(s, ctl, 0x01, {0: 48, 1: 48}, centres={0: 14_175_000}, diversity=0x02)
        got = collect(s, 1.5)
        if 1035 in got and len(got[1035]) > 3:
            iq = iq_of(got[1035], upto=2)
            a0 = sum(abs(z) for z in iq[0::2]) / len(iq[0::2])
            a1 = sum(abs(z) for z in iq[1::2]) / len(iq[1::2])
            check("--pair-order d1d0: the quiet DDC1 half now comes FIRST (even slots)",
                  0.4 < a0 / a1 < 0.6, f"even/odd amplitude {a0/a1:.2f}")
        else:
            check("--pair-order d1d0 streams", False, f"streams {sorted(got)}")
        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        ctl.close()
        s.close()
        sim.log()


def case_status_packet_cadence_and_options():
    """[KO9B] status packets leave :1025 about every 232 ms at receive, in the measured
    byte layout. Also proves the options reach the wire: --supply-volts, --adc-overload."""
    with Sim("--supply-volts", "12.0", "--adc-overload", "2", "--adc-peak", "5000") as sim:
        s = client()
        discover(s)
        ctl = client()
        start_session(s, ctl, 0x04, {2: 48}, centres={2: 14_175_000})
        got = collect(s, 2.6, ports=(1025,), limit=50)
        pk = got.get(1025, [])
        check("status packets arrive from :1025 on the session socket", len(pk) >= 6, f"got {len(pk)}")
        if len(pk) >= 6:
            gaps = sorted(b[0] - a[0] for a, b in zip(pk, pk[1:]))
            med = gaps[len(gaps) // 2]
            check("[KO9B] status cadence is ~232 ms", 0.18 < med < 0.30, f"median {med*1000:.0f} ms")
            d = pk[3][1]
            check("[KO9B] status: 60 B, byte 4 = 0x10 (PLL locked), byte 30 = 0x0C, byte 59 = 0x07",
                  len(d) == 60 and d[4] == 0x10 and d[30] == 0x0C and d[59] == 0x07,
                  f"len {len(d)} b4 {d[4]:#x} b30 {d[30]:#x} b59 {d[59]:#x}")
            volts = struct.unpack_from(">H", d, 57)[0] * anan_sim.SUPPLY_DIVIDER * 5 / 4095
            check("--supply-volts 12.0 reaches AIN3 (bytes 57-58)", abs(volts - 12.0) < 0.05, f"{volts:.2f} V")
            check("--adc-overload 2 sets byte 5 bit 1 (ADC1)", d[5] == 0x02, f"byte 5 = {d[5]:#x}")
            check("--adc-peak 5000 reaches both peak-holds (39-40 and 41-42)",
                  struct.unpack_from(">H", d, 39)[0] == 5000 and struct.unpack_from(">H", d, 41)[0] == 5000)
            sq = [struct.unpack_from(">I", x, 0)[0] for _, x in pk]
            check("status packets carry an incrementing sequence", all(b - a == 1 for a, b in zip(sq, sq[1:])),
                  f"{sq[:6]}")
        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        ctl.close()
        s.close()
        sim.log()


def case_mic_stream_runs_from_1026():
    """[KO9B] ~750 packets/s from :1026 whenever the radio runs: 4 B sequence + 64 x
    16-bit samples = 132 B (the size N2JXL's capture shows). The sim has no microphone, so
    it is silence -- the framing and rate are what is pinned."""
    with Sim() as sim:
        s = client()
        discover(s)
        ctl = client()
        # nothing before the run bit
        pre = collect(s, 0.6, ports=(1026,))
        check("no mic stream before the run bit", 1026 not in pre, f"got {len(pre.get(1026, []))}")
        start_session(s, ctl, 0x04, {2: 48}, centres={2: 14_175_000})
        got = collect(s, 1.5, ports=(1026,), limit=3000)
        pk = got.get(1026, [])
        check("[KO9B] mic stream flows from :1026 at a few hundred packets/s or more",
              len(pk) >= 560, f"{len(pk)} packets in 1.5 s (want ~1125 at 750/s; bound is loose for CI)")
        if pk:
            d = pk[0][1]
            check("mic packet is 132 B (4 B sequence + 64 x 16-bit)", len(d) == 132, f"{len(d)} B")
            sq = seqs(pk)
            check("mic sequence increments by 1", all(b - a == 1 for a, b in zip(sq, sq[1:])), f"{sq[:6]}")
        s.sendto(hp_packet(False), (IP, PORT_HP_IN))
        time.sleep(0.3)
        collect(s, 0.3, ports=(1026,))
        after = collect(s, 0.6, ports=(1026,))
        check("mic stream stops with the run bit", 1026 not in after, f"got {len(after.get(1026, []))}")
        ctl.close()
        s.close()
        sim.log()


def case_stale_burst_is_opt_in_and_matches_the_observed_shape():
    """[KO9B] every session on a real G2 starts with a burst of up to ~76 packets in the
    PREVIOUS session's DDC layout, numbered from 0, some from ports no longer enabled
    (observed: 76, 49 and 12). A client must drop them. --stale-burst reproduces it; it is
    off by default because it breaks naive clients."""
    for flag in ("--stale-burst", None):
        with Sim(*([flag] if flag else [])) as sim:
            s = client()
            discover(s)
            ctl = client()
            start_session(s, ctl, 0x0C, {2: 48, 3: 48}, centres={2: 14_175_000, 3: 7_100_000})
            collect(s, 0.6)
            s.sendto(hp_packet(False), (IP, PORT_HP_IN))
            time.sleep(0.4)
            collect(s, 0.3)
            # session 2 enables DDC2 only: DDC3 is gone from the layout
            ctl.sendto(ddc_spec(0x04, {2: 48}), (IP, PORT_DDC_SPEC))
            time.sleep(0.3)
            s.sendto(hp_packet(True, centres={2: 14_175_000}), (IP, PORT_HP_IN))
            got = collect(s, 1.0)
            old = got.get(1038, [])
            if flag:
                sq = seqs(old)
                check("--stale-burst: a burst arrives from :1038, a port no longer enabled",
                      len(old) == anan_sim.STALE_BURST_PACKETS, f"{len(old)} packets")
                check("the burst is numbered from 0 and contiguous", sq == list(range(len(sq))), f"{sq[:5]}...")
                check("the live DDC2 stream still runs alongside", 1037 in got, f"streams {sorted(got)}")
            else:
                check("by default there is NO stale burst (it breaks naive clients)", old == [],
                      f"got {len(old)} packets from :1038")
            s.sendto(hp_packet(False), (IP, PORT_HP_IN))
            ctl.close()
            s.close()
            sim.log()


def case_default_mac_is_never_a_real_radios():
    """The default MAC is locally administered, so it cannot collide with a Hermes-Lite 2 (whose factory MAC is
    00:1c:c0:a2:13:dd) or any genuine G2; --mac still overrides it."""
    hl2_factory = bytes.fromhex("001cc0a213dd")
    want = bytes.fromhex(anan_sim.DEFAULT_MAC.replace(":", ""))
    check("the default MAC is not a Hermes-Lite 2's factory MAC", want != hl2_factory, anan_sim.DEFAULT_MAC)
    check("the default MAC is locally administered and unicast (first octet bit 1 set, bit 0 clear)",
          (want[0] & 0x03) == 0x02, f"first octet {want[0]:#04x}")
    with Sim() as sim:
        s = client()
        reply = discover(s)
        check("discovery answers with the default MAC at [5:11]", reply[5:11] == want, reply[5:11].hex())
        s.close()
        sim.log()
    with Sim("--mac", "00:1c:c0:a2:13:dd") as sim:
        s = client()
        reply = discover(s)
        check("--mac still overrides it", reply[5:11] == hl2_factory, reply[5:11].hex())
        s.close()
        sim.log()


def main():
    for fn in (case_default_mac_is_never_a_real_radios,
               case_tone_ring_is_phase_continuous_across_the_seam,
               case_tone_handedness_and_amplitude,
               case_generator_is_a_slice_not_a_per_sample_loop,
               case_interleave_layout_and_other_patterns,
               case_supply_scale_matches_the_measured_g2,
               case_multi_ddc_streams_run_at_once_with_independent_rates,
               case_ddc_above_the_advertised_count_streams_like_a_real_g2,
               case_enforce_ddc_count_reproduces_a_radio_that_ignores_it,
               case_diversity_as_measured_on_a_real_g2,
               case_pair_order_can_be_swapped,
               case_status_packet_cadence_and_options,
               case_mic_stream_runs_from_1026,
               case_stale_burst_is_opt_in_and_matches_the_observed_shape):
        print(fn.__name__)
        try:
            fn()
        except RuntimeError as e:
            check(f"{fn.__name__}: sim started", False, str(e))
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def _pytest(*fns):
    global passed, failed
    passed = failed = 0
    try:
        for fn in fns:
            fn()
    except RuntimeError as e:
        import pytest
        pytest.skip(f"anan_sim could not be started (ports 1024-1044 busy?): {e}")
    assert failed == 0, f"{failed} check(s) failed -- see output above"


def test_discovery_mac():
    _pytest(case_default_mac_is_never_a_real_radios)


def test_pure_signal_source():
    _pytest(case_tone_ring_is_phase_continuous_across_the_seam,
            case_tone_handedness_and_amplitude,
            case_generator_is_a_slice_not_a_per_sample_loop,
            case_interleave_layout_and_other_patterns,
            case_supply_scale_matches_the_measured_g2)


def test_wire_multi_ddc():
    _pytest(case_multi_ddc_streams_run_at_once_with_independent_rates,
            case_ddc_above_the_advertised_count_streams_like_a_real_g2)


def test_wire_enforced_ddc_count():
    _pytest(case_enforce_ddc_count_reproduces_a_radio_that_ignores_it)


def test_wire_diversity():
    _pytest(case_diversity_as_measured_on_a_real_g2, case_pair_order_can_be_swapped)


def test_wire_status_mic_and_session():
    _pytest(case_status_packet_cadence_and_options, case_mic_stream_runs_from_1026,
            case_stale_burst_is_opt_in_and_matches_the_observed_shape)


if __name__ == "__main__":
    sys.exit(main())
