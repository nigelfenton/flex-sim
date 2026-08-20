#!/usr/bin/env python3
# anan_sim.py — openHPSDR Protocol 2 (ANAN / Saturn) receiver simulator.
#
# v1 scope: enough of Protocol 2 that a client discovers a radio, completes the
# start-up handshake, and receives a continuous DDC0 I/Q stream — i.e. enough to
# paint a waterfall. TX, multi-DDC, wideband and mic are deliberately absent
# (see "NOT implemented" below).
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
#
# NOT implemented (v1): TX/DUC, DDC1-9, wideband ADC, mic samples, memory-mapped
# access, non-default port re-assignment (accepted and logged, not acted on).
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

VERSION = "0.1.0"

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
# [13] software version  [20] number of DDCs  [23] beta version
REPLY_LEN = 60
STATUS_IDLE = 0x02             # 2 = idle/available, 3 = already sending
DEVICE_SATURN = 0x0A           # pihpsdr NEW_DEVICE_SATURN 1010 = 1000 + 0x0A
P2_VERSION = 39                # protocol 3.9
SW_VERSION = 21

# --- DDC Specific packet (port 1025) ------------------------------------------
# Per-DDC records are 6 bytes starting at byte 17: ADC assignment at 17+6n,
# sample rate at 18+6n as a big-endian uint16 in kHz (pihpsdr new_protocol.c).
# DDC0's rate therefore lives at bytes [18:20].
DDC0_RATE_OFFSET = 18
DDC_RATES = (48000, 96000, 192000, 384000, 768000, 1536000)

# --- DDC I/Q framing (real-hardware pcap, 2026-08-17) -------------------------
IQ_SAMPLES_PER_PACKET = 238
IQ_BYTES_PER_SAMPLE = 6        # 24-bit I + 24-bit Q
IQ_PAYLOAD_BYTES = IQ_SAMPLES_PER_PACKET * IQ_BYTES_PER_SAMPLE   # 1428
IQ_HEADER_BYTES = 16           # seq(4) + timestamp(8) + bits/sample(2) + samples/frame(2)

FULL_SCALE_24 = (1 << 23) - 1


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


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


class AnanSim:
    def __init__(self, ip, mac, rate, pattern, tone_hz, ddc_count):
        self.ip = ip
        self.mac = mac
        self.rate = rate
        self.pattern = pattern
        self.tone_hz = tone_hz
        self.ddc_count = ddc_count

        self.run = False                 # set by the High Priority run bit
        self.stop = False
        self.pc_addr = None              # (host, port) learned from discovery
        self.centre_hz = 14_100_000      # DDC0 NCO, set by DDC Specific
        self.seq_iq = 0
        self.seq_hp = 0
        self.phase = 0.0
        self.lock = threading.Lock()
        self.socks = []

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
                self.pc_addr = addr           # reply to the SOURCE port
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
        b[13] = SW_VERSION
        b[20] = self.ddc_count
        return bytes(b)

    def handle_general(self, data, addr):
        """General Packet to SDR — may re-assign every port. v1 logs only."""
        if len(data) < 60:
            return
        # Ports live at [5:] as big-endian uint16 pairs; zero means "default".
        wanted = struct.unpack_from(">H", data, 5)[0] if len(data) >= 7 else 0
        if wanted not in (0, PORT_DDC_SPEC):
            log(f"[gen] ⚠ client asked to re-assign ports (first={wanted}) — "
                f"v1 ignores this and keeps the defaults")
        else:
            log(f"[gen] General Packet from {addr[0]} — default ports")

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
            run = bool(data[4] & 0x01)
            if run != self.run:
                self.run = run
                log(f"[hp ] RUN bit -> {int(run)} "
                    f"({'streaming DDC0' if run else 'stopped'})")
            # DDC0 centre frequency: big-endian uint32 at [9:13] in the P2
            # high-priority packet. Track it so the tone lands where asked.
            if len(data) >= 13:
                f = struct.unpack_from(">I", data, 9)[0]
                if f and f != self.centre_hz:
                    self.centre_hz = f
                    log(f"[hp ] DDC0 centre -> {f/1e6:.6f} MHz")

    # -- DDC specific (1025): the client commands the DDC0 sample rate here ---
    def serve_ddc_spec(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.ip, PORT_DDC_SPEC))
        self.socks.append(s)
        seen = False
        while not self.stop:
            try:
                data, _ = s.recvfrom(2048)
            except OSError:
                break
            if not seen:
                seen = True
                log(f"[ddc] first packet ({len(data)} B) — accepted")
            if len(data) < DDC0_RATE_OFFSET + 2:
                continue
            khz = struct.unpack_from(">H", data, DDC0_RATE_OFFSET)[0]
            rate = khz * 1000
            if not khz or rate == self.rate:
                continue
            if rate not in DDC_RATES:
                log(f"[ddc] ⚠ commanded DDC0 rate {khz} kHz is not a P2 rate — ignored")
                continue
            self.rate = rate
            log(f"[ddc] DDC0 rate -> {rate} S/s "
                f"(cadence {IQ_SAMPLES_PER_PACKET/rate*1000:.3f} ms/packet, "
                f"size stays {IQ_HEADER_BYTES + IQ_PAYLOAD_BYTES} B)")

    # -- DUC specific (1026): accepted, minimally parsed ----------------------
    def serve_spec(self, port, name):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.ip, port))
        self.socks.append(s)
        seen = False
        while not self.stop:
            try:
                data, _ = s.recvfrom(2048)
            except OSError:
                break
            if not seen:
                seen = True
                log(f"[{name}] first packet ({len(data)} B) — accepted")

    # -- DDC0 I/Q out (to PC port learned at discovery, from 1035) -----------
    def stream_ddc0(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.ip, PORT_DDC0_IQ_OUT))
        self.socks.append(s)
        period = IQ_SAMPLES_PER_PACKET / self.rate      # 4.96 ms @ 48k
        log(f"[iq ] DDC0 ready: {IQ_PAYLOAD_BYTES} B / {IQ_SAMPLES_PER_PACKET} samples "
            f"every {period*1000:.3f} ms at {self.rate} S/s")
        next_at = time.perf_counter()
        while not self.stop:
            if not (self.run and self.pc_addr):
                time.sleep(0.02)
                next_at = time.perf_counter()
                continue
            pkt = self.iq_packet()
            try:
                s.sendto(pkt, self.pc_addr)
            except OSError:
                pass
            self.seq_iq += 1
            period = IQ_SAMPLES_PER_PACKET / self.rate  # live: DDC Specific may re-rate us
            next_at += period
            slack = next_at - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            else:
                next_at = time.perf_counter()      # we fell behind; resync

    def iq_packet(self):
        hdr = struct.pack(">IQHH", self.seq_iq & 0xFFFFFFFF,
                          int(time.time() * 1e9) & 0xFFFFFFFFFFFFFFFF,
                          24, IQ_SAMPLES_PER_PACKET)
        return hdr + self.iq_payload()

    def iq_payload(self):
        out = bytearray(IQ_PAYLOAD_BYTES)
        step = 2.0 * math.pi * self.tone_hz / self.rate
        amp = int(FULL_SCALE_24 * 0.25)
        for n in range(IQ_SAMPLES_PER_PACKET):
            if self.pattern == "noise":
                i = random.randint(-amp // 8, amp // 8)
                q = random.randint(-amp // 8, amp // 8)
            elif self.pattern == "zero":
                i = q = 0
            else:                                   # tone (default)
                self.phase += step
                if self.phase > 2 * math.pi:
                    self.phase -= 2 * math.pi
                i = int(amp * math.cos(self.phase))
                q = int(amp * math.sin(self.phase))
            o = n * IQ_BYTES_PER_SAMPLE
            out[o:o+3] = (i & 0xFFFFFF).to_bytes(3, "big")
            out[o+3:o+6] = (q & 0xFFFFFF).to_bytes(3, "big")
        return bytes(out)

    # -- high priority out (to PC:1025): 50 ms in RX --------------------------
    def stream_high_priority(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socks.append(s)
        while not self.stop:
            if self.run and self.pc_addr:
                b = bytearray(60)
                struct.pack_into(">I", b, 0, self.seq_hp & 0xFFFFFFFF)
                self.seq_hp += 1
                try:
                    s.sendto(bytes(b), (self.pc_addr[0], PORT_HIGH_PRIO_OUT))
                except OSError:
                    pass
            time.sleep(0.050)                      # 50 ms in RX; 1 ms in TX

    def start(self):
        log(f"anan-sim {VERSION} — openHPSDR Protocol 2 (Saturn/ANAN-G2) RX only")
        log(f"  ip={self.ip} rate={self.rate} pattern={self.pattern} "
            f"tone={self.tone_hz} Hz ddc={self.ddc_count}")
        log(f"  MAC {':'.join(f'{x:02x}' for x in self.mac)}  device 0x{DEVICE_SATURN:02x} (Saturn)")
        for target, args in ((self.serve_discovery, ()),
                             (self.serve_high_priority, ()),
                             (self.serve_ddc_spec, ()),
                             (self.serve_spec, (PORT_DUC_SPEC, "duc")),
                             (self.stream_ddc0, ()),
                             (self.stream_high_priority, ())):
            threading.Thread(target=target, args=args, daemon=True).start()
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
                    help="initial DDC0 sample rate — the client's DDC Specific "
                         "packet overrides it (changes packet CADENCE, not size)")
    ap.add_argument("--pattern", default="tone", choices=["tone", "noise", "zero"])
    ap.add_argument("--tone", type=float, default=1000.0, help="tone offset from centre, Hz")
    ap.add_argument("--ddc", type=int, default=2, help="DDC count to advertise")
    ap.add_argument("--mac", default="00:1c:c0:a2:13:dd", help="MAC in the discovery reply")
    args = ap.parse_args()

    try:
        mac = bytes(int(x, 16) for x in args.mac.split(":"))
        if len(mac) != 6:
            raise ValueError
    except ValueError:
        sys.exit(f"bad --mac {args.mac!r}: want six hex octets like 00:1c:c0:a2:13:dd")

    AnanSim(args.ip or local_ip(), mac, args.rate, args.pattern,
            args.tone, args.ddc).start()


if __name__ == "__main__":
    main()
