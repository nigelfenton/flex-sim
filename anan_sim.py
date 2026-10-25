#!/usr/bin/env python3
# anan_sim.py — openHPSDR Protocol 2 (ANAN / Saturn) receiver simulator.
#
# Scope: enough of Protocol 2 that a client discovers a radio, completes the
# start-up handshake, and receives continuous I/Q from EVERY DDC it enables
# (v0.2: multi-DDC; v1 streamed only the lowest enabled one) — i.e. enough to
# paint a waterfall, or to run a second receiver. TX, wideband and mic are
# deliberately absent (see "NOT implemented" below).
#
# WHY THIS EXISTS
#   AetherSDR speaks HPSDR Protocol 1 (Metis) via its HL2 backend. The ANAN-G2
#   (Saturn FPGA) speaks Protocol 2 ONLY — no P1 firmware exists for it — so P2
#   support would be a sibling backend. Nobody can review that work without a
#   $3k radio on the bench unless a simulator exists first. AE issue #4815 is
#   the cautionary tale: a test gated on hardware nobody had passed silently on
#   every machine for months.
#
# PROTOCOL SOURCES (clean-room: facts consulted, no code copied)
#   - laurencebarker/Saturn (GPL-3.0), project_documentation/
#     "protocol 2 data transder.docx" + "protocol 2 data transfer sizes.xlsx",
#     by the G2's own firmware author. Port map, start-up order, packet sizes.
#   - laurencebarker/pihpsdr (GPL-3.0), src/new_discovery.c — discovery packet
#     and reply field offsets. Already a named reference in AetherSDR's
#     THIRD_PARTY_LICENSES for the P1 work.
#   Both are GPL-3.0, as is flex-sim, so adaptation would be permissible; the
#   clean-room posture is convention, not necessity.
#
# THE QUIRKS THAT WILL BITE YOU (all from the author's own notes)
#   - Discovery AND the General Packet both arrive on port 1024.
#   - The discovery REPLY goes to the requester's SOURCE port, not a fixed port.
#     Every outgoing stream then targets that same PC port. Do not assume 1024.
#   - The General Packet can RE-ASSIGN every port; zero means "use the default".
#     Thetis sends the defaults, but a conforming radio must honour a new set.
#   - There is NO run/stop packet. The run bit is embedded in the High Priority
#     packet (byte 4, bit 0) — that is what starts and stops the data threads.
#   - Everything is NETWORK BYTE ORDER (big-endian). Saturn does not implement
#     the protocol's byte-order-change mechanism.
#   - DDC (RX) I/Q packets are 16 B header + 1428 B = 238 samples at EVERY sample
#     rate. The rate changes the CADENCE, not the size: 4.96 ms at 48k, 2.48 at
#     96k, 1.24 at 192k. Samples are 24-bit I + 24-bit Q, big-endian.
#   - The sample rate is COMMANDED by the client, not configured on the radio:
#     the DDC Specific packet (port 1025) carries 6-byte per-DDC records from
#     byte 17 — ADC at 17+6n, rate at 18+6n as big-endian uint16 in kHz
#     (pihpsdr new_protocol.c writes (rate/1000)>>8 there). --rate is only the
#     pre-handshake default; NereusSDR proved this the hard way on 2026-08-20
#     by asking for 192k while the sim sat at 48k and would have streamed 4x
#     slow.
#     VERIFIED 2026-08-17 against N2JXL's real ANAN-G2 pcaps: the radio's own
#     samples/frame field reads 238, and 238*6 = 1428 fills the 1444 B payload
#     exactly. The "transfer sizes" spreadsheet's 1440 B / 240 samples is the
#     DUC (TX) geometry -- TX has a 4-byte header (seq only, no timestamp and no
#     bits/samples fields), so 4 + 240*6 = 1444 as well. Do not mix them up:
#     240 samples on RX overruns the payload by 12 bytes.
#   - The wire IQ is the CONJUGATE of the textbook analytic convention: a signal
#     ABOVE the DDC centre arrives at a NEGATIVE frequency. A sim that emits
#     I = cos, Q = sin for "+1 kHz" puts its tone 1 kHz BELOW the dial on a
#     correct client -- audible only in LSB. That was this sim's bug until
#     v0.3.1, found when AetherSDR's ANAN backend (whose polarity was measured
#     on a real G2 against WWV, and cross-checked with an RSP1B + SDR++ sharing
#     no code with it) drew the tone on the wrong side. See AetherSDR
#     docs/HERMES.md section 16.
#   - Frequencies in the High Priority packet (4 bytes per DDC at 9+4n) are a
#     PHASE WORD by default, not Hz: Hz = word * 122.88 MHz / 2^32 (Saturn's
#     ADC clock). The General Packet's byte 37 bit 3 selects it, and the
#     discovery reply's byte 21 advertises it. 10.000 MHz is 349525333.
#
#   - EVERY enabled DDC streams. DDC-Specific byte 7 is the enable mask; each
#     enabled DDC streams from its own source port 1035+n with its own sequence
#     counter (restarting at 0 at every run=1), its own commanded rate (record n)
#     and its own tuning (High Priority word n). MEASURED on a real G2 by KO9B
#     (2026-10-25): same ADC and frequency => packet k of each DDC covers the same
#     instants, a constant NCO phase offset apart, ~0.1 ms between them; at mixed
#     rates the streams ran at their own rates (202.6 and 811.2 pkt/s for 48k+192k).
#     The sim's per-DDC tones are an INVENTION so a second receiver is visibly a
#     different signal; a real G2 on one antenna gives both DDCs the same signal.
#   - ANY enabled DDC streams, whatever discovery advertises. MEASURED (KO9B):
#     discovery byte 20 says 4, but p2app reads enables for 10 DDCs and DDC4 streams
#     normally from :1039 -- the 4 is a literal in p2app.c. --enforce-ddc-count
#     reproduces the OTHER behaviour a client might meet (a radio that ignores a DDC
#     above its advertised count), which AetherSDR's PR #6697 names as a limit.
#   - DIVERSITY, MEASURED on a real G2 by KO9B (2026-10-25): DDC Specific byte 1363
#     (PC -> radio :1025; NOT High Priority) == 0x02 -- an equality test in p2app, not
#     a bit. Byte 7 enables DDC0 only and p2app enables DDC1 itself; the pair's rate
#     comes from DDC1's record (bytes 24-25). The DDC0 header still declares 238
#     samples but they are 119 interleaved DDC0/DDC1 pairs, the packet rate doubles
#     (201.5 -> 407.3 pkt/s at 48k, 811.8 -> 1619.3 at 192k), and NOTHING comes from
#     :1036. Pair order is DDC0 first (I0Q0 I1Q1) in the first session after power-up,
#     but after DDC0 has run on its own it came out SWAPPED in 4 of 10 trials
#     (p2app sets up its sample buffers and FIFO once, so a leftover odd sample count
#     shifts every pair): --pair-order d1d0 reproduces that. When DDC0 only ever
#     carries pairs the order never moved in 15 trials, and a client can find the
#     order itself by switching DDC1's input from TX samples (silent) to ADC1.
#     DDC1's signal here is the same tone 6 dB down and 45 degrees round: invented,
#     so a combiner has something to combine.
#   - DDC packet timestamps (bytes 4-11) are ALWAYS ZERO on a real G2. The mic stream
#     (:1026 -> PC) runs ~750 packets/s whenever the radio runs (64 x 16-bit samples:
#     132 B), and the status packet (:1025 -> PC) goes out about every 232 ms.
#   - Every session on a real G2 starts with a burst of up to ~76 packets in the
#     PREVIOUS session's DDC layout, numbered from 0, some from ports no longer
#     enabled (p2app does not reset the FIFO between sessions). --stale-burst
#     reproduces it; off by default because it breaks naive clients.
#
# NOT implemented: TX/DUC, wideband ADC, mic samples, memory-mapped access,
# PureSignal feedback, non-default port re-assignment (accepted and logged, not
# acted on).
#
# Run:   python3 anan_sim.py                 # defaults, pattern=tone
#        python3 anan_sim.py --rate 96000 --pattern noise
#        python3 anan_sim.py --ip 10.0.0.5   # bind a specific interface
#
# Then point a Protocol 2 client (Thetis, piHPSDR) at this host.

import argparse
import math
import random
import socket
import struct
import sys
import threading
import time

VERSION = "0.3.0"

# --- ports (PC -> SDR unless noted). Saturn doc, "Port Numbers to Use". -------
PORT_DISCOVERY = 1024          # discovery AND General Packet to SDR
PORT_DDC_SPEC = 1025           # DDC Specific from PC
PORT_DUC_SPEC = 1026           # DUC Specific from PC
PORT_HIGH_PRIO_IN = 1027       # High Priority from PC (carries the RUN bit)
PORT_DDC_AUDIO = 1028          # speaker audio from PC
PORT_DUC_IQ = 1029             # DUC0 I/Q from PC
PORT_HIGH_PRIO_OUT = 1025      # High Priority -> PC
PORT_DDC0_IQ_OUT = 1035        # DDC0 I/Q -> PC (DDC0-9 = 1035..1044)

# --- discovery reply layout (pihpsdr new_discovery.c field offsets) ----------
# [0:4] zero  [4] status  [5:11] MAC  [11] device id  [12] P2 version
# [13] software version  [20] number of DDCs  [21] frequency is a phase word
# [23] beta version
REPLY_LEN = 60
STATUS_IDLE = 0x02             # 2 = idle/available, 3 = already sending
DEVICE_SATURN = 0x0A           # pihpsdr NEW_DEVICE_SATURN 1010 = 1000 + 0x0A
P2_VERSION = 43                # protocol 4.3 (MEASURED on a real G2, KO9B; was 39)
P2APP_BUILD = 46               # discovery byte 23: the real G2's p2app build
# Gateware ("software") version in the discovery reply. MEASURED on a real G2 by
# KO9B (2026-10-25): byte 13 = 27, byte 20 = 4 (and board 10, protocol 4.3, p2app
# build 46). Both the sim's earlier guesses were right. NereusSDR had logged "fw: 21"
# against v0.1 -- hence --gateware, to reproduce an older radio.
SW_VERSION = 27
DEFAULT_DDC_COUNT = 4
# DDC-Specific byte 7 is a mask for DDC0-7. A real p2app reads enables for 10 DDCs
# (ports 1035-1044); where DDC8-9 live in the packet has not been measured, so the
# sim stops at 8.
MAX_DDC = 8

# --- DDC Specific packet (port 1025) ------------------------------------------
# Per-DDC records are 6 bytes starting at byte 17: ADC assignment at 17+6n,
# sample rate at 18+6n as a big-endian uint16 in kHz (pihpsdr new_protocol.c).
# DDC0's rate therefore lives at bytes [18:20].
DDC0_RATE_OFFSET = 18
DDC1_RATE_OFFSET = DDC0_RATE_OFFSET + 6      # bytes 24-25: the pair's rate in diversity
# DDC Specific byte 1363 == 0x02 selects diversity (p2app tests EQUALITY, not a bit).
DDC_SPEC_DIVERSITY_OFFSET = 1363
DDC_SPEC_DIVERSITY_VALUE = 0x02
DDC_RATES = (48000, 96000, 192000, 384000, 768000, 1536000)

# --- DDC I/Q framing (real-hardware pcap, 2026-08-17) -------------------------
IQ_SAMPLES_PER_PACKET = 238
IQ_BYTES_PER_SAMPLE = 6        # 24-bit I + 24-bit Q
IQ_PAYLOAD_BYTES = IQ_SAMPLES_PER_PACKET * IQ_BYTES_PER_SAMPLE   # 1428
IQ_HEADER_BYTES = 16           # seq(4) + timestamp(8) + bits/sample(2) + samples/frame(2)

FULL_SCALE_24 = (1 << 23) - 1

# --- High Priority frequency words ---------------------------------------------
PHASE_WORD_CLOCK_HZ = 122_880_000   # Saturn ADC clock: Hz = word * clock / 2^32
GENERAL_FREQ_FLAGS = 37             # General Packet byte; bit 3 set = phase word
HP_FREQ_OFFSET = 9                  # High Priority: 4-byte word per DDC at 9+4n

# --- streams the radio sends that are not DDC I/Q (MEASURED, KO9B, 2026-10-25) ----
PORT_MIC_OUT = 1026                 # mic -> PC, from the same :1026 the radio listens on
MIC_PACKETS_PER_S = 750             # ~750/s whenever the radio runs
MIC_SAMPLES = 64                    # 64 x 16-bit mono = 48 kHz; 4 B seq + 128 B = 132 B
STATUS_PERIOD_S = 0.232             # HP status every ~232 ms on receive
STALE_BURST_PACKETS = 76            # observed bursts: 76, 49 and 12 packets; the worst case
# Status packet constants a G2 reports at receive (bytes as measured):
STATUS_PLL_LOCKED = 0x10            # byte 4: bit0 PTT, bit1 dot, bit2 dash, bit4 PLL locked
STATUS_EXCITER = 63                 # bytes 6-7
STATUS_FORWARD = 14                 # bytes 14-15
STATUS_REVERSE = 14                 # bytes 22-23
STATUS_UNDERFLOW = 0x0C             # byte 30: DUC + speaker underflow (nothing is sent)
STATUS_AIN6 = 1586                  # bytes 49-50: AIN6, which is NOT the supply on a G2
STATUS_AIN4 = 4                     # bytes 55-56 (median 4, range 0-16 in KO9B's run)
STATUS_FIFO_DEPTH = 1522            # bytes 31-32: median of 63-4165 in KO9B's run
# ADC peak-hold medians KO9B measured on HIS antennas (bytes 39-40 / 41-42); a real
# G2 never reads 0 with an antenna on it, so these beat a flat zero for AE's meters.
DEFAULT_ADC0_PEAK = 11768
DEFAULT_ADC1_PEAK = 3151
STATUS_BYTE59 = 0x07
SUPPLY_DIVIDER = 23.0 / 1.1         # volts = counts * 23.0/1.1 * 5/4095 (AIN3, bytes 57-58)


def phase_word_to_hz(word):
    return word * PHASE_WORD_CLOCK_HZ / (1 << 32)


def log(*a):
    """A log line must never be able to kill the thread that wrote it. On Windows a
    piped stdout is cp1252, which cannot encode the warning sign in the sim's own
    messages: print() raised UnicodeEncodeError, the DDC Specific thread died, and
    the sim silently stopped reading mask and rate packets (found by the v0.2 tests
    on Windows -- invisible on Linux and in any shell that sets PYTHONIOENCODING)."""
    msg = " ".join(str(x) for x in (time.strftime("%H:%M:%S"), *a))
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)
    except (OSError, ValueError):      # stdout closed / broken pipe: never fatal
        pass


def local_ip():
    """The interface address that reaches the LAN (not 127.0.0.1)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

class IqSource:
    """Pre-rendered, phase-continuous I/Q for ONE DDC.

    v0.1 built every packet one sample at a time in Python (238 x two to_bytes
    per packet) and topped out near 540 packets/s on the hub's Celeron J1900 --
    about two-thirds of a real G2's 807 packets/s at 192k and a tenth of what
    1536k wants. A tone is periodic, so render ONE whole number of cycles once
    (and again only when the rate changes) and hand out slices of it: a packet
    is then a single bytes slice, no per-sample Python at all.

    The tone is rounded to a whole number of Hz so that period = rate / gcd(rate,
    tone) is an integer and the ring wraps phase-continuously (no click at the
    seam). Noise is a fixed 64k-sample ring: it repeats every ~1.4 s at 48k,
    which is fine for exercising a waterfall and is NOT a statistics source.
    """

    NOISE_SAMPLES = 65536

    def __init__(self, pattern, rate, tone_hz, amp_scale=1.0, phase_deg=0.0, seed=0):
        self.pattern = pattern
        self.rate = rate
        self.tone_hz = tone_hz
        self.amp_scale = amp_scale
        self.phase_deg = phase_deg
        self.seed = seed
        self.pos = 0
        self._render()

    def set_rate(self, rate):
        """A new rate changes the tone's period, so the ring is rebuilt. The
        tone phase restarts; a rate change already discontinuously restarts the
        client's DSP, so there is nothing to be continuous with."""
        if rate != self.rate:
            self.rate = rate
            self.pos = 0
            self._render()

    @staticmethod
    def _pack(i, q):
        return (i & 0xFFFFFF).to_bytes(3, "big") + (q & 0xFFFFFF).to_bytes(3, "big")

    def _render(self):
        amp = int(FULL_SCALE_24 * 0.25 * self.amp_scale)
        if self.pattern == "zero":
            self.period = 1
            unit = bytes(IQ_BYTES_PER_SAMPLE)
            self.ring = unit * (IQ_SAMPLES_PER_PACKET + 1)
            return
        if self.pattern == "noise":
            rng = random.Random(self.seed)
            lim = max(1, amp // 8)
            ring = bytearray()
            for _ in range(self.NOISE_SAMPLES):
                ring += self._pack(rng.randint(-lim, lim), rng.randint(-lim, lim))
            self.period = self.NOISE_SAMPLES
            self.ring = bytes(ring) + bytes(ring[:IQ_SAMPLES_PER_PACKET * IQ_BYTES_PER_SAMPLE])
            return
        f = int(round(self.tone_hz))
        period = self.rate // math.gcd(self.rate, abs(f)) if f else 1
        phase0 = math.radians(self.phase_deg)
        ring = bytearray()
        for n in range(period):
            th = 2.0 * math.pi * f * n / self.rate + phase0
            # Conjugate of the analytic convention, like the real wire:
            # -sin puts a +tone_hz offset ABOVE the dial on a correct client.
            ring += self._pack(int(amp * math.cos(th)), int(-amp * math.sin(th)))
        self.period = period
        reps = IQ_SAMPLES_PER_PACKET // period + 2
        self.ring = bytes(ring) * reps

    def take(self, nsamples):
        """The next nsamples samples as 24-bit I/Q bytes (6 bytes each)."""
        o = self.pos * IQ_BYTES_PER_SAMPLE
        out = self.ring[o:o + nsamples * IQ_BYTES_PER_SAMPLE]
        self.pos = (self.pos + nsamples) % self.period
        return out


def interleave_pairs(a, b):
    """Two equal-length runs of 6-byte samples -> a0 b0 a1 b1 ... (the diversity
    DDC0/DDC1 pair framing). Strided slice assignment: no per-sample loop."""
    assert len(a) == len(b) and len(a) % IQ_BYTES_PER_SAMPLE == 0
    out = bytearray(2 * len(a))
    w = 2 * IQ_BYTES_PER_SAMPLE
    for k in range(IQ_BYTES_PER_SAMPLE):
        out[k::w] = a[k::IQ_BYTES_PER_SAMPLE]
        out[IQ_BYTES_PER_SAMPLE + k::w] = b[k::IQ_BYTES_PER_SAMPLE]
    return bytes(out)


def ddc_tone_hz(base_hz, ddc):
    """Tone offset for DDC n: DDC0-2 carry base_hz (DDC2 is RX1 on a Thetis-family
    client, DDC0/1 the diversity pair), DDC3 carries 2x, DDC4 3x ... so a second
    receiver is audibly and visibly a different signal from the first."""
    return base_hz * max(1, ddc - 1)


class DdcStream:
    """One enabled DDC's outgoing stream: own socket (source port 1035+n),
    sequence counter, rate and signal source."""

    def __init__(self, ip, ddc, rate, pattern, tone_hz):
        self.ddc = ddc
        self.rate = rate
        self.seq = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((ip, PORT_DDC0_IQ_OUT + ddc))
        # DDC1 is the same tone 6 dB down and 45 degrees round: indistinguishable
        # from DDC0 it would be useless as a diversity pair.
        self.source = IqSource(pattern, rate, ddc_tone_hz(tone_hz, ddc),
                               amp_scale=0.5 if ddc == 1 else 1.0,
                               phase_deg=45.0 if ddc == 1 else 0.0, seed=ddc)
        self.next_at = time.perf_counter()
        self.pkts = 0

    def set_rate(self, rate):
        self.rate = rate
        self.source.set_rate(rate)
        self.next_at = time.perf_counter()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class AnanSim:
    def __init__(self, ip, mac, rate, pattern, tone_hz, ddc_count,
                 gateware=SW_VERSION, pair_order="d0d1", enforce_ddc_count=False,
                 stale_burst=False, supply_volts=13.58, adc_peak=None, adc_overload=0):
        self.ip = ip
        self.mac = mac
        self.rate = rate                 # default for a DDC with no commanded rate yet
        self.pattern = pattern
        self.tone_hz = tone_hz
        self.ddc_count = ddc_count
        self.gateware = gateware
        self.pair_order = pair_order     # diversity pair order on the wire
        self.enforce_ddc_count = enforce_ddc_count
        self.stale_burst = stale_burst
        self.supply_counts = max(0, min(4095, round(supply_volts / SUPPLY_DIVIDER * 4095 / 5)))
        self.adc0_peak = DEFAULT_ADC0_PEAK if adc_peak is None else adc_peak
        self.adc1_peak = DEFAULT_ADC1_PEAK if adc_peak is None else adc_peak
        self.adc_overload = adc_overload & 0x03

        self.run = False                 # set by the High Priority run bit
        self.run_epoch = 0               # +1 on every run 0 -> 1: a new session
        self.last_run_layout = set()     # DDCs that were streaming in the previous run
        self.sock_1026 = None            # mic leaves from the DUC Specific socket
        self.stop = False
        self.pc_addr = None              # (host, port) of the client SESSION
        # DDC-Specific byte 7: bit n = DDCn streams. Starts as DDC0-only, the v0.1
        # behaviour that p2stream.c and a bare run-bit client depend on: a REAL
        # radio would stream nothing until a DDC Specific packet enables a DDC,
        # so this is a deliberate convenience, replaced the moment one arrives.
        self.enable_mask = 0x01
        self.ddc_rates = {}              # n -> commanded S/s (DDC Specific record n)
        self.centre_hz = {}              # n -> NCO Hz (High Priority word n)
        self.diversity = False           # DDC Specific byte 1363 == 0x02
        self.freq_is_phase_word = True   # the P2 default; General byte 37 bit 3
        self.seq_hp = 0
        self.lock = threading.RLock()    # spec + HP threads both touch mask/centres
        self.hp_words = {}               # n -> latest raw High Priority word, enabled or not
        self.socks = []
        self._warned = set()

    def _warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            log(msg)

    # -- discovery + general (port 1024) -------------------------------------
    def serve_discovery(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.ip, PORT_DISCOVERY))
        self.socks.append(s)
        log(f"[disc] listening on {self.ip}:{PORT_DISCOVERY}")
        while not self.stop:
            try:
                data, addr = s.recvfrom(2048)
            except OSError:
                break
            if len(data) < 5:
                continue
            # P2 discovery: [0:4] == 0, [4] == 0x02. A P1 probe (EF FE ...) also
            # lands here; ignore it rather than answering with a P2 reply.
            if data[0:4] == b"\x00\x00\x00\x00" and data[4] == 0x02:
                # Reply to the probe's source port — but do NOT stream there.
                # The real G2 (N2JXL pcap, 2026-08-20) streams to the SESSION
                # port (General/HP source), never the discovery port: Thetis
                # probes from one socket and runs the session on another.
                s.sendto(self.discovery_reply(), addr)
                log(f"[disc] P2 discovery from {addr[0]}:{addr[1]} -> replied "
                    f"(device=0x{DEVICE_SATURN:02x} Saturn, {self.ddc_count} DDC)")
            elif data[0:2] == b"\xef\xfe":
                log(f"[disc] P1 (Metis) probe from {addr[0]} — ignored, this radio is P2-only")
            else:
                # A General Packet also arrives here once discovery has happened.
                self.handle_general(data, addr)

    def discovery_reply(self):
        b = bytearray(REPLY_LEN)
        b[4] = STATUS_IDLE
        b[5:11] = self.mac
        b[11] = DEVICE_SATURN
        b[12] = P2_VERSION
        b[13] = self.gateware
        b[20] = self.ddc_count
        b[21] = 1                        # High Priority frequencies are phase words
        b[23] = P2APP_BUILD
        return bytes(b)

    def _learn_session(self, addr):
        """All radio->PC streams target the client's SESSION source port
        (verified against N2JXL's real-G2 pcap: DDC IQ, mic and HP status all
        went to :62520, the General/HP source — not :62517, the discovery
        source). Learned from General and HP-in so it survives a sim restart
        mid-session."""
        if addr != self.pc_addr:
            self.pc_addr = addr
            log(f"[gen] session -> {addr[0]}:{addr[1]} (streams target this port)")

    def handle_general(self, data, addr):
        """General Packet to SDR — may re-assign every port. v1 logs only."""
        if len(data) < 60:
            return
        self._learn_session(addr)
        # Ports live at [5:] as big-endian uint16 pairs; zero means "default".
        wanted = struct.unpack_from(">H", data, 5)[0] if len(data) >= 7 else 0
        if wanted not in (0, PORT_DDC_SPEC):
            log(f"[gen] ⚠ client asked to re-assign ports (first={wanted}) — "
                f"v1 ignores this and keeps the defaults")
        else:
            log(f"[gen] General Packet from {addr[0]} — default ports")
        phase = bool(data[GENERAL_FREQ_FLAGS] & 0x08)
        if phase != self.freq_is_phase_word:
            self.freq_is_phase_word = phase
            log(f"[gen] frequencies arrive as {'phase words' if phase else 'Hz'}")

    # -- high priority in (port 1027): carries the RUN bit --------------------
    def serve_high_priority(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.ip, PORT_HIGH_PRIO_IN))
        self.socks.append(s)
        while not self.stop:
            try:
                data, addr = s.recvfrom(2048)
            except OSError:
                break
            if len(data) < 5:
                continue
            self._learn_session(addr)
            run = bool(data[4] & 0x01)
            if run != self.run:
                if run:
                    self.run_epoch += 1          # a new session: seqs restart at 0
                self.run = run
                log(f"[hp ] RUN bit -> {int(run)} "
                    f"({'streaming' if run else 'stopped'})")
            # Tuning of every DDC: big-endian uint32 at 9+4n, a phase word unless
            # the General Packet switched to Hz. ALL words are remembered, so a
            # DDC enabled by a DDC Specific packet that arrives AFTER this one
            # still picks up its tuning at once. Logged and kept only -- each
            # tone is an offset from wherever the client tuned.
            with self.lock:
                for n in range(MAX_DDC):
                    off = HP_FREQ_OFFSET + 4 * n
                    if len(data) >= off + 4:
                        self.hp_words[n] = struct.unpack_from(">I", data, off)[0]
                self._note_centres()

    def _note_centres(self):
        """Log (and keep) the tuning of every ENABLED DDC whose word changed.
        Caller holds self.lock."""
        for n in range(MAX_DDC):
            word = self.hp_words.get(n, 0)
            if not (self.enable_mask >> n) & 1 or not word:
                continue
            hz = round(phase_word_to_hz(word)) if self.freq_is_phase_word else word
            if hz != self.centre_hz.get(n):
                self.centre_hz[n] = hz
                log(f"[hp ] DDC{n} centre -> {hz/1e6:.6f} MHz")

    # -- DDC specific (1025): enable mask + a commanded rate PER DDC ----------
    def serve_ddc_spec(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.ip, PORT_DDC_SPEC))
        self.socks.append(s)
        self.sock_1025 = s     # HP-out shares it: radio sends HP FROM :1025
        seen = False
        while not self.stop:
            try:
                data, _ = s.recvfrom(2048)
            except OSError:
                break
            if not seen:
                seen = True
                log(f"[ddc] first packet ({len(data)} B) — accepted")
            # Evidence log: mask + first four per-DDC rate records, whenever
            # the CmdRx content changes (diagnoses a client writing the rate
            # into the wrong DDC's record).
            if len(data) >= 44:
                prefix = bytes(data[:44])
                if prefix != getattr(self, "_last_cmdrx", None):
                    self._last_cmdrx = prefix
                    rates = [struct.unpack_from(">H", data, 18 + 6 * i)[0]
                             for i in range(4)]
                    log(f"[ddc] CmdRx mask=0x{data[7]:02x} "
                        f"rates(kHz) DDC0-3={rates}")
            if len(data) < 8:
                continue
            # buf[7] = DDC enable bitmask (bit n = DDCn), per Thetis CmdRx
            # (NereusSDR P2CodecOrionMkII::composeCmdRx). Thetis-family clients
            # run RX1 on DDC2 -- DDC0/1 are reserved for PureSignal feedback and
            # diversity -- which is why N2JXL's real-G2 capture streams from
            # port 1037, not 1035. EVERY enabled DDC streams (v0.2).
            mask = data[7]
            with self.lock:
                if mask != self.enable_mask:
                    on = [n for n in range(MAX_DDC) if (mask >> n) & 1]
                    log(f"[ddc] enable mask 0x{self.enable_mask:02x} -> 0x{mask:02x} "
                        f"-> streaming DDC{on} "
                        f"(ports {[PORT_DDC0_IQ_OUT + n for n in on]})")
                    self.enable_mask = mask
                    self._note_centres()
                # Diversity: byte 1363 of THIS packet, tested for EQUALITY with 0x02
                # exactly as p2app does (KO9B, 2026-10-25) -- not High Priority, not
                # a bit. Byte 7 then enables DDC0 only; the pair rate is DDC1's record.
                if len(data) > DDC_SPEC_DIVERSITY_OFFSET:
                    div = data[DDC_SPEC_DIVERSITY_OFFSET] == DDC_SPEC_DIVERSITY_VALUE
                    if div != self.diversity:
                        self.diversity = div
                        log(f"[ddc] diversity {'ON' if div else 'off'} (DDC Specific byte "
                            f"{DDC_SPEC_DIVERSITY_OFFSET} == 0x{data[DDC_SPEC_DIVERSITY_OFFSET]:02x})"
                            f"{': DDC0 carries 119 interleaved DDC0/DDC1 pairs at the rate in DDC1 record' if div else ''}")
                # A rate record exists for EVERY DDC (a client leaves the disabled ones
                # stale or zero), and diversity reads DDC1's although DDC1 is not in the
                # mask -- so parse them all, log only the ones that matter.
                for n in range(MAX_DDC):
                    off = DDC0_RATE_OFFSET + 6 * n
                    if len(data) < off + 2:
                        continue
                    khz = struct.unpack_from(">H", data, off)[0]
                    rate = khz * 1000
                    if not khz or rate == self.ddc_rates.get(n):
                        continue
                    live = (mask >> n) & 1 or (n == 1 and self.diversity)
                    if rate not in DDC_RATES:
                        if live:
                            self._warn_once(("badrate", n, khz),
                                            f"[ddc] \u26a0 commanded DDC{n} rate {khz} kHz "
                                            f"is not a P2 rate - ignored")
                        continue
                    self.ddc_rates[n] = rate
                    if live:
                        log(f"[ddc] DDC{n} rate -> {rate} S/s "
                            f"(cadence {IQ_SAMPLES_PER_PACKET/rate*1000:.3f} ms/packet, "
                            f"size stays {IQ_HEADER_BYTES + IQ_PAYLOAD_BYTES} B)")

    # -- DUC specific (1026): accepted, minimally parsed ----------------------
    def serve_spec(self, port, name):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.ip, port))
        self.socks.append(s)
        if port == PORT_DUC_SPEC:
            self.sock_1026 = s     # the mic stream leaves FROM :1026, like HP-out from :1025
        seen = False
        while not self.stop:
            try:
                data, _ = s.recvfrom(2048)
            except OSError:
                break
            if not seen:
                seen = True
                log(f"[{name}] first packet ({len(data)} B) — accepted")

    # -- DDC I/Q out (to the client's SESSION port, from 1035+n) --------------
    # Clients demux streams by the radio-side SOURCE port, so each enabled DDC
    # owns a socket bound to 1035 + n; one is opened when its mask bit appears and
    # closed when it goes (a re-enabled DDC starts a fresh stream, seq 0).
    def _wanted(self):
        """({ddc: rate}, diversity_active) for the streams that should run now."""
        with self.lock:
            mask, diversity = self.enable_mask, self.diversity
            # A real G2 streams ANY enabled DDC regardless of the count it advertises
            # (KO9B: DDC4 streams from :1039); --enforce-ddc-count opts into a radio
            # that ignores one above its advertised count.
            limit = min(MAX_DDC, self.ddc_count) if self.enforce_ddc_count else MAX_DDC
            want = {}
            for n in range(MAX_DDC):
                if not (mask >> n) & 1:
                    continue
                if n >= limit:
                    self._warn_once(("nodev", n),
                                    f"[ddc] \u26a0 DDC{n} enabled but this radio advertises "
                                    f"only {self.ddc_count} DDCs and --enforce-ddc-count "
                                    f"is set -- not streaming it")
                    continue
                want[n] = self.ddc_rates.get(n, self.rate)
            # Diversity: DDC1 rides inside DDC0's packets, never its own, and the
            # pair runs at the rate in DDC1's record (bytes 24-25), not DDC0's.
            div = diversity and 0 in want
            if div:
                want.pop(1, None)
                want[0] = self.ddc_rates.get(1, want[0])
            return want, div

    def stream_ddcs(self):
        streams = {}
        seen_epoch = self.run_epoch
        stat_t0 = time.perf_counter()
        log(f"[iq ] DDC ready: {IQ_PAYLOAD_BYTES} B / {IQ_SAMPLES_PER_PACKET} samples "
            f"per packet at every rate (cadence = {IQ_SAMPLES_PER_PACKET}/rate)")
        while not self.stop:
            want, div = self._wanted()
            for n in [n for n in streams if n not in want]:
                streams.pop(n).close()
                log(f"[iq ] DDC{n} stream stopped (port {PORT_DDC0_IQ_OUT + n})")
            for n, rate in want.items():
                st = streams.get(n)
                if st is None:
                    try:
                        streams[n] = DdcStream(self.ip, n, rate, self.pattern, self.tone_hz)
                    except OSError as e:
                        self._warn_once(("bind", n), f"[iq ] ⚠ cannot bind DDC{n} source port "
                                        f"{PORT_DDC0_IQ_OUT + n}: {e}")
                        continue
                    log(f"[iq ] DDC{n} stream started: source port "
                        f"{PORT_DDC0_IQ_OUT + n}, {rate} S/s, tone "
                        f"{ddc_tone_hz(self.tone_hz, n):g} Hz above the dial")
                elif st.rate != rate:
                    st.set_rate(rate)
            if not (self.run and self.pc_addr and streams):
                time.sleep(0.02)
                now = time.perf_counter()
                for st in streams.values():
                    st.next_at = now
                continue
            if self.run_epoch != seen_epoch:
                # run went 0 -> 1: a NEW session. Sequence counters restart at 0
                # (KO9B: "starting from 0 at run=1"), and a real G2 first flushes a
                # burst in the previous layout (p2app does not reset its FIFO).
                seen_epoch = self.run_epoch
                if self.stale_burst:
                    self._stale_burst(self.last_run_layout - set(streams))
                for st in streams.values():
                    st.seq = 0
                    st.next_at = time.perf_counter()
            self.last_run_layout = set(streams)
            # Windows sleep() is ~15 ms grained; per-packet pacing at 1.24 ms
            # (192k) is impossible. Sleep to the earliest deadline (capped, so a
            # mask or rate change is noticed within 20 ms), then send EVERY packet
            # due in one burst -- clients buffer; what they cannot survive is the
            # old "fell behind -> resync" branch silently DROPPING the backlog
            # (heard as ~3 Hz clicking: bursts of tone, then starvation).
            slack = min(st.next_at for st in streams.values()) - time.perf_counter()
            if slack > 0:
                time.sleep(min(slack, 0.02))
            now = time.perf_counter()
            for st in streams.values():
                paired = st.ddc == 0 and div
                nsamp = IQ_SAMPLES_PER_PACKET // 2 if paired else IQ_SAMPLES_PER_PACKET
                period = nsamp / st.rate
                sent = 0
                while st.next_at <= now and sent < 64:
                    if paired:
                        d1 = streams.get(1)
                        partner = d1.source if d1 else self._diversity_partner(st)
                        # A diversity pair is the SAME instant on two antennas, so
                        # both halves must come from the same sample index; the only
                        # difference left is the deliberate -6 dB / 45 degrees.
                        partner.pos = st.source.pos % partner.period
                        a, b = st.source.take(nsamp), partner.take(nsamp)
                        # DDC0 first in the first session after power-up; after DDC0 has
                        # run alone a real G2 swapped them in 4 of 10 trials.
                        payload = interleave_pairs(a, b) if self.pair_order == "d0d1" \
                            else interleave_pairs(b, a)
                    else:
                        payload = st.source.take(nsamp)
                    # Timestamp bytes 4-11 are ALWAYS ZERO on a real G2 (KO9B).
                    hdr = struct.pack(">IQHH", st.seq & 0xFFFFFFFF, 0,
                                      24, IQ_SAMPLES_PER_PACKET)
                    try:
                        st.sock.sendto(hdr + payload, self.pc_addr)
                    except OSError:
                        pass
                    st.seq += 1
                    st.pkts += 1
                    st.next_at += period
                    sent += 1
                if sent >= 64:                 # hopelessly behind (paused/debugged)
                    st.next_at = time.perf_counter()
            if now - stat_t0 >= 5.0:
                for st in streams.values():
                    per = (IQ_SAMPLES_PER_PACKET // 2) if (st.ddc == 0 and div) else IQ_SAMPLES_PER_PACKET
                    got = st.pkts / (now - stat_t0)
                    log(f"[iq ] DDC{st.ddc}: {got:.0f} pkt/s (nominal {st.rate / per:.0f}) "
                        f"at {st.rate} S/s")
                    st.pkts = 0
                stat_t0 = now

    def _diversity_partner(self, st0):
        """DDC1's signal for the diversity pair when DDC1 itself is not enabled
        (a real client enables DDC0 only; p2app enables DDC1 itself)."""
        src = getattr(self, "_div_partner", None)
        if src is None:
            src = IqSource(self.pattern, st0.rate, ddc_tone_hz(self.tone_hz, 1),
                           amp_scale=0.5, phase_deg=45.0, seed=1)
            self._div_partner = src
        src.set_rate(st0.rate)
        return src

    def _stale_burst(self, gone):
        """Up to STALE_BURST_PACKETS packets, numbered from 0, from each port of the
        PREVIOUS session's layout that the new one no longer enables. A real G2 does
        this at the start of every session after the first (p2app does not reset
        its FIFO between sessions); a client must drop them, not draw them."""
        for n in sorted(gone):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.ip, PORT_DDC0_IQ_OUT + n))
            except OSError:
                continue
            try:
                rate = self.ddc_rates.get(n, self.rate)
                src = IqSource(self.pattern, rate, ddc_tone_hz(self.tone_hz, n), seed=n)
                for seq in range(STALE_BURST_PACKETS):
                    hdr = struct.pack(">IQHH", seq, 0, 24, IQ_SAMPLES_PER_PACKET)
                    try:
                        s.sendto(hdr + src.take(IQ_SAMPLES_PER_PACKET), self.pc_addr)
                    except OSError:
                        break
            finally:
                s.close()
            log(f"[iq ] stale burst: {STALE_BURST_PACKETS} packets from port "
                f"{PORT_DDC0_IQ_OUT + n} (DDC{n}, not in this session's layout)")

    # -- mic stream: FROM :1026 TO the session port, ~750 packets/s while running.
    # 64 x 16-bit samples (48 kHz) + a 4 B sequence = 132 B, the size the N2JXL
    # capture shows. The sim has no microphone: it is silence. The sequence
    # restarting at each run=1 is ASSUMED (measured only for the DDC streams).
    def stream_mic(self):
        while not self.stop and self.sock_1026 is None:
            time.sleep(0.05)
        period = 1.0 / MIC_PACKETS_PER_S
        silence = bytes(MIC_SAMPLES * 2)
        seq, seen_epoch = 0, self.run_epoch
        next_at = time.perf_counter()
        while not self.stop:
            if not (self.run and self.pc_addr):
                time.sleep(0.02)
                next_at = time.perf_counter()
                continue
            if self.run_epoch != seen_epoch:
                seen_epoch, seq = self.run_epoch, 0
            slack = next_at - time.perf_counter()
            if slack > 0:
                time.sleep(min(slack, 0.02))
            now = time.perf_counter()
            sent = 0
            while next_at <= now and sent < 16:
                try:
                    self.sock_1026.sendto(struct.pack(">I", seq & 0xFFFFFFFF) + silence,
                                          self.pc_addr)
                except OSError:
                    pass
                seq += 1
                next_at += period
                sent += 1
            if sent >= 16:
                next_at = time.perf_counter()

    # -- high priority out (the "status" packet): FROM radio :1025 TO the session
    # port, about every 232 ms on receive (KO9B: p2app sleeps 400 x 0.5 ms). Clients
    # demux by the radio-side SOURCE port, so this must leave from :1025 -- reuse the
    # DDC Specific socket, which is already bound there. Layout MEASURED on a real G2.
    def status_packet(self):
        b = bytearray(60)
        struct.pack_into(">I", b, 0, self.seq_hp & 0xFFFFFFFF)
        b[4] = STATUS_PLL_LOCKED                         # PTT/dot/dash clear
        b[5] = self.adc_overload                         # bit0 = ADC0 (ANT1), bit1 = ADC1
        struct.pack_into(">H", b, 6, STATUS_EXCITER)
        struct.pack_into(">H", b, 14, STATUS_FORWARD)
        struct.pack_into(">H", b, 22, STATUS_REVERSE)
        b[30] = STATUS_UNDERFLOW
        struct.pack_into(">H", b, 31, STATUS_FIFO_DEPTH)  # DDC FIFO depth
        struct.pack_into(">H", b, 39, self.adc0_peak)    # ADC0 peak-hold
        struct.pack_into(">H", b, 41, self.adc1_peak)    # ADC1 peak-hold
        struct.pack_into(">H", b, 55, STATUS_AIN4)
        struct.pack_into(">H", b, 49, STATUS_AIN6)       # AIN6 -- NOT the supply
        struct.pack_into(">H", b, 57, self.supply_counts)  # AIN3 = the supply rail
        b[59] = STATUS_BYTE59
        return bytes(b)

    def stream_high_priority(self):
        while not self.stop and getattr(self, "sock_1025", None) is None:
            time.sleep(0.05)
        while not self.stop:
            if self.run and self.pc_addr:
                pkt = self.status_packet()
                self.seq_hp += 1
                try:
                    self.sock_1025.sendto(pkt, self.pc_addr)
                except OSError:
                    pass
            time.sleep(STATUS_PERIOD_S)

    def start(self):
        if sys.platform == "win32":
            # 1 ms system timer so the IQ pacing sleep wakes ~on time instead
            # of on the default ~15.6 ms tick. Without this the sender leans
            # entirely on the burst catch-up and audio arrives lumpy.
            try:
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception:
                pass
        log(f"anan-sim {VERSION} — openHPSDR Protocol 2 (Saturn/ANAN-G2) RX only, multi-DDC")
        log(f"  ip={self.ip} rate={self.rate} pattern={self.pattern} "
            f"tone={self.tone_hz} Hz ddc={self.ddc_count} gateware={self.gateware} "
            f"pair-order={self.pair_order}"
            f"{' enforce-ddc-count' if self.enforce_ddc_count else ''}"
            f"{' stale-burst' if self.stale_burst else ''}")
        log(f"  MAC {':'.join(f'{x:02x}' for x in self.mac)}  device 0x{DEVICE_SATURN:02x} (Saturn)")
        for target, args in ((self.serve_discovery, ()),
                             (self.serve_high_priority, ()),
                             (self.serve_ddc_spec, ()),
                             (self.serve_spec, (PORT_DUC_SPEC, "duc")),
                             (self.stream_ddcs, ()),
                             (self.stream_mic, ()),
                             (self.stream_high_priority, ())):
            threading.Thread(target=target, args=args, daemon=True).start()
        t0 = time.time()
        while time.time() - t0 < 3.0 and not (len(self.socks) >= 4
                                              and getattr(self, "sock_1025", None)):
            time.sleep(0.01)
        log("  waiting for discovery…  (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            log("shutting down")
            self.stop = True
            for s in self.socks:
                try:
                    s.close()
                except OSError:
                    pass


def main():
    ap = argparse.ArgumentParser(description="openHPSDR Protocol 2 (ANAN/Saturn) RX simulator")
    ap.add_argument("--ip", default=None, help="interface to bind (default: autodetect)")
    ap.add_argument("--rate", type=int, default=48000, choices=list(DDC_RATES),
                    help="default sample rate for a DDC the client has not commanded "
                         "one for -- the DDC Specific packet overrides it per DDC "
                         "(changes packet CADENCE, not size)")
    ap.add_argument("--pattern", default="tone", choices=["tone", "noise", "zero"])
    ap.add_argument("--tone", type=float, default=1000.0, help="tone offset from centre, Hz")
    ap.add_argument("--ddc", type=int, default=DEFAULT_DDC_COUNT,
                    help="DDC count to ADVERTISE in the discovery reply (default "
                         f"{DEFAULT_DDC_COUNT}, what a real G2 says). A real G2 streams "
                         "any enabled DDC regardless -- see --enforce-ddc-count")
    ap.add_argument("--enforce-ddc-count", action="store_true",
                    help="stream nothing for an enabled DDC above --ddc, like a radio "
                         "that lacks it (NOT what a real G2 does)")
    ap.add_argument("--pair-order", default="d0d1", choices=["d0d1", "d1d0"],
                    help="diversity pair order on the wire: DDC0 first (a real G2's "
                         "first session after power-up) or DDC1 first (what it did in "
                         "4 of 10 trials after DDC0 had run alone)")
    ap.add_argument("--stale-burst", action="store_true",
                    help="at each run=1 send a burst of packets from ports of the "
                         "previous session's layout (a real G2 does this); a client "
                         "must drop them")
    ap.add_argument("--supply-volts", type=float, default=13.58,
                    help="supply rail the status packet reports (AIN3, bytes 57-58)")
    ap.add_argument("--adc-peak", type=int, default=None,
                    help="ADC0 AND ADC1 peak-hold in the status packet (0..65535); "
                         f"default is KO9B's measured medians {DEFAULT_ADC0_PEAK}/{DEFAULT_ADC1_PEAK}")
    ap.add_argument("--adc-overload", type=int, default=0,
                    help="status byte 5 ADC overload bits: 1 = ADC0, 2 = ADC1, 3 = both")
    ap.add_argument("--gateware", type=int, default=SW_VERSION,
                    help="gateware version byte in the discovery reply "
                         f"(default {SW_VERSION}; unconfirmed against a real G2)")
    ap.add_argument("--mac", default="00:1c:c0:a2:13:dd", help="MAC in the discovery reply")
    args = ap.parse_args()

    try:
        mac = bytes(int(x, 16) for x in args.mac.split(":"))
        if len(mac) != 6:
            raise ValueError
    except ValueError:
        sys.exit(f"bad --mac {args.mac!r}: want six hex octets like 00:1c:c0:a2:13:dd")

    if not 1 <= args.ddc <= MAX_DDC:
        sys.exit(f"bad --ddc {args.ddc}: want 1..{MAX_DDC}")
    AnanSim(args.ip or local_ip(), mac, args.rate, args.pattern,
            args.tone, args.ddc, args.gateware, args.pair_order,
            args.enforce_ddc_count, args.stale_burst, args.supply_volts,
            args.adc_peak, args.adc_overload).start()


if __name__ == "__main__":
    main()
