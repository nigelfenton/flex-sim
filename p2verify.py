#!/usr/bin/env python3
"""
p2verify.py — verify openHPSDR Protocol 2 geometry from a pcapng capture.

Written to answer one question from real hardware rather than from the spec:
what are the DDC (RX) and DUC (TX) frame layouts actually on the wire?

Answers, for an ANAN-G2 (board type 10 = SATURN):

    DDC (radio -> PC)   16-byte header: seq(4) ts(8) bitsPerSample(2) samplesPerFrame(2)
                        1428 IQ bytes = 238 samples of 24-bit I + 24-bit Q
    DUC (PC -> radio)    4-byte header: seq(4) only — no timestamp, no bits/nsamp
                        1440 IQ bytes = 240 samples

Both payloads are 1444 bytes and both divide exactly by 6, so both look
self-consistent — but applying the TX geometry to RX overruns by 12 bytes and
misparses every frame. A DUC packet read with the RX header yields
nsamp=0, bits=0, and those zeroes are sample data, not fields.

No dependencies beyond the standard library: this parses pcapng directly so it
runs anywhere Python does, without scapy or tshark.

Usage:
    python p2verify.py CAPTURE.pcapng [--radio-ip A.B.C.D] [--limit N]

Two traps this script exists to avoid:

  1. pcapng timestamp resolution is per-interface and is NOT always microseconds.
     These captures declare if_tsresol = 9 (nanoseconds). Assuming the default
     gives cadences 1000x too long. This script reads the IDB option.

  2. The capture wire may carry unrelated traffic. In these captures 163k packets
     of multicast RTP (port 5004) and PTP (319/320) dominate a top-N port summary
     and make the P2 session look absent. This script filters to unicast UDP
     between the two hosts on the P2 port range.

## Why this lives next to anan_sim.py

anan_sim.py and tools/p2stream.c both derive from Laurence Barker's openHPSDR
Protocol 2 documentation, so they agree with each other by construction --
that agreement is not evidence. This script reads captures from a REAL
ANAN-G2 and is therefore the INDEPENDENT arbiter: it is what caught that the
widely-quoted 1440 B / 240 sample figure is the TX geometry, and that applying
it to RX overruns every frame by 12 bytes. anan_sim.py's header documents the
corrected 238-sample / 16-byte-header values.

The reference captures (two sessions from N2JXL's ANAN-G2, about 490 MB) are
recordings of a private station and are NOT distributed with this repo. Point
this script at your own capture of any P2 client talking to a real radio.
tests/test_p2verify.py exercises it on a small synthetic capture.

Verified 2026-08-27: reproduces every figure published in
github.com/aethersdr/AetherSDR/issues/4970 against both reference captures.

73, Nigel G0JKN
"""

import argparse
import struct
import sys
from collections import defaultdict

# ── pcapng block types ────────────────────────────────────────────────────────
BT_SHB = 0x0A0D0D0A
BT_IDB = 0x00000001
BT_EPB = 0x00000006

P2_PORT_LO, P2_PORT_HI = 1024, 1114
DDC_HEADER_LEN = 16
DUC_HEADER_LEN = 4
SAMPLE_BYTES = 6            # 24-bit I + 24-bit Q


class Interface:
    __slots__ = ("tsresol_div",)

    def __init__(self, tsresol_div):
        self.tsresol_div = tsresol_div


def _parse_idb_options(body, endian):
    """Return the timestamp divisor for this interface (default microseconds)."""
    divisor = 1_000_000
    off = 8                                     # linktype(2) reserved(2) snaplen(4)
    while off + 4 <= len(body):
        code, length = struct.unpack(endian + "HH", body[off:off + 4])
        off += 4
        if code == 0:                           # opt_endofopt
            break
        value = body[off:off + length]
        if code == 9 and length >= 1:           # if_tsresol
            raw = value[0]
            if raw & 0x80:                      # high bit set => power of two
                divisor = 1 << (raw & 0x7F)
            else:
                divisor = 10 ** raw
        off += (length + 3) & ~3                # options are 4-byte aligned
    return divisor


def iter_packets(path):
    """Yield (timestamp_seconds, packet_bytes) from a pcapng file."""
    interfaces = []
    endian = "<"
    with open(path, "rb") as fh:
        while True:
            head = fh.read(8)
            if len(head) < 8:
                return
            btype = struct.unpack("<I", head[0:4])[0]
            if btype == BT_SHB:
                magic = fh.read(4)
                endian = "<" if magic == b"\x4d\x3c\x2b\x1a" else ">"
                blen = struct.unpack(endian + "I", head[4:8])[0]
                # 12 bytes consumed so far (type 4 + len 4 + magic 4).
                fh.read(blen - 12)
                interfaces = []          # a new section restarts interface ids
                continue
            blen = struct.unpack(endian + "I", head[4:8])[0]
            if blen < 12:
                return
            # 8 bytes consumed (type + length). The block body plus the trailing
            # duplicate-length field make up the remaining blen - 8.
            body = fh.read(blen - 8)
            if len(body) < blen - 8:
                return
            body = body[:-4]             # drop the trailing length field
            if btype == BT_IDB:
                interfaces.append(Interface(_parse_idb_options(body, endian)))
            elif btype == BT_EPB:
                iface, ts_hi, ts_lo, cap_len, _orig = struct.unpack(
                    endian + "IIIII", body[0:20])
                ts_raw = (ts_hi << 32) | ts_lo
                div = interfaces[iface].tsresol_div if iface < len(interfaces) else 1_000_000
                yield ts_raw / div, body[20:20 + cap_len]


def parse_udp(pkt):
    """Return (src_ip, src_port, dst_ip, dst_port, payload) or None."""
    if len(pkt) < 14 or struct.unpack("!H", pkt[12:14])[0] != 0x0800:
        return None                              # not IPv4 over Ethernet
    ip = pkt[14:]
    if len(ip) < 20 or (ip[0] >> 4) != 4:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if ip[9] != 17:                              # not UDP
        return None
    src_ip = ".".join(str(b) for b in ip[12:16])
    dst_ip = ".".join(str(b) for b in ip[16:20])
    udp = ip[ihl:]
    if len(udp) < 8:
        return None
    src_port, dst_port, length = struct.unpack("!HHH", udp[0:6])
    return src_ip, src_port, dst_ip, dst_port, udp[8:length]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture")
    ap.add_argument("--radio-ip", help="restrict to this radio address")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N packets (0 = whole file)")
    args = ap.parse_args()

    flows = defaultdict(lambda: {"n": 0, "sizes": set(), "t0": None, "t1": None,
                                 "seqs": [], "nonzero_payload": 0,
                                 "ddc_selfdescribes": None})
    discovery_replies = []
    seen = 0

    for ts, pkt in iter_packets(args.capture):
        seen += 1
        if args.limit and seen > args.limit:
            break
        parsed = parse_udp(pkt)
        if not parsed:
            continue
        src_ip, src_port, dst_ip, dst_port, payload = parsed

        # Trap 2: ignore multicast/broadcast noise sharing the wire.
        if dst_ip.startswith(("224.", "239.")) or dst_ip == "255.255.255.255":
            if len(payload) == 63 or len(payload) == 60:
                pass                             # keep discovery broadcasts
            else:
                continue
        if not (P2_PORT_LO <= src_port <= P2_PORT_HI
                or P2_PORT_LO <= dst_port <= P2_PORT_HI):
            continue
        if args.radio_ip and args.radio_ip not in (src_ip, dst_ip):
            continue

        key = (src_ip, src_port, dst_ip, dst_port)
        f = flows[key]
        f["n"] += 1
        f["sizes"].add(len(payload))
        if f["t0"] is None:
            f["t0"] = ts
        f["t1"] = ts
        if len(payload) >= 4:
            f["seqs"].append(struct.unpack("!I", payload[0:4])[0])

        # Discovery reply: 60 bytes, byte 4 == 0x02
        if len(payload) == 60 and src_port == 1024 and payload[4] == 0x02:
            discovery_replies.append((src_ip, dst_ip, dst_port, bytes(payload)))

        # Is anything past the header actually non-zero? (silence detector)
        if len(payload) == 1444 and any(payload[DUC_HEADER_LEN:]):
            f["nonzero_payload"] += 1

        # Does this frame describe itself as a DDC frame? Decided once per flow.
        if len(payload) == 1444 and f["ddc_selfdescribes"] is None:
            bits = struct.unpack("!H", payload[12:14])[0]
            nsamp = struct.unpack("!H", payload[14:16])[0]
            f["ddc_selfdescribes"] = (
                bits == 24 and DDC_HEADER_LEN + nsamp * SAMPLE_BYTES == 1444)

    print(f"\n=== {args.capture} ===")
    print(f"packets scanned: {seen}\n")

    if discovery_replies:
        ip, dst, dport, body = discovery_replies[0]
        print("--- discovery reply ---")
        print(f"  radio {ip} -> {dst}:{dport}")
        print(f"  NOTE: reply goes to the requester's SOURCE port ({dport}), not 1024")
        print(f"  board type   {body[11]:#04x} ({'SATURN/ANAN-G2' if body[11] == 10 else '?'})")
        print(f"  protocol ver {body[12] / 10:.1f}")
        print(f"  firmware     {body[13]}")
        print(f"  DDCs         {body[20]}")
        print(f"  freq format  {'phase word' if body[21] else 'Hz'}")
        print()

    if not flows:
        print("!! No P2 traffic matched.")
        print("!! If you used --limit, the session may start later in the file:")
        print("!!   in the N2JXL captures nothing appears until ~200k packets in.")
        print("!! Re-run without --limit before concluding the capture has no P2.")
        print("!! (This is the same mistake as judging by a top-N port summary.)")
        print()

    print("--- flows (P2 ports, unicast) ---")
    for (sip, sp, dip, dp), f in sorted(flows.items(), key=lambda kv: -kv[1]["n"]):
        if f["n"] < 10:
            continue
        sizes = sorted(f["sizes"])
        span = (f["t1"] - f["t0"]) if f["t0"] is not None else 0.0
        rate = f["n"] / span if span > 0 else 0.0
        print(f"  {sip}:{sp} -> {dip}:{dp}  {f['n']:>7} pkt  "
              f"sizes={sizes if len(sizes) <= 3 else str(sizes[:3]) + '...'}  "
              f"{rate:8.1f} pkt/s over {span:.3f}s")

        if 1444 in sizes and f["n"] > 100:
            # Direction is decided by the frame's OWN declared fields, never by
            # port number: a DDC frame declares bits/sample=24 and a sample
            # count that accounts for exactly 1444 bytes. Anything else is a
            # DUC frame whose "fields" are really sample data.
            is_ddc = f["ddc_selfdescribes"]
            direction = "DDC (RX)" if is_ddc else "DUC (TX)"
            hdr = DDC_HEADER_LEN if is_ddc else DUC_HEADER_LEN
            samples = (1444 - hdr) // SAMPLE_BYTES
            derived = samples * f["n"] / span if span > 0 else 0
            print(f"      -> {direction}: {hdr}B header, "
                  f"{1444 - hdr} IQ bytes, {samples} samples/frame")
            print(f"      -> derived sample rate {derived:,.1f} Hz "
                  f"(DERIVE it; do not assume 48k)")
            mbps = f["n"] * (1444 + 42) * 8 / span / 1e6 if span > 0 else 0
            print(f"      -> {mbps:.2f} Mbit/s on the wire (payload + 42B framing)")
            if f["nonzero_payload"] == 0:
                print("      -> ** payload is ALL ZERO - keyed silence. "
                      "Useless as a modulation reference. **")

            seqs = f["seqs"]
            gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
            print(f"      -> seq starts at {seqs[0]}, {gaps} non-consecutive steps")

    # The headline check: what the radio says about its own RX frames.
    print("\n--- self-describing DDC header check ---")
    for (sip, sp, dip, dp), f in flows.items():
        if 1444 not in f["sizes"] or f["n"] < 100:
            continue
        # Re-read one frame to show the declared fields.
        for ts, pkt in iter_packets(args.capture):
            parsed = parse_udp(pkt)
            if not parsed:
                continue
            s_ip, s_p, d_ip, d_p, payload = parsed
            if (s_ip, s_p, d_ip, d_p) != (sip, sp, dip, dp) or len(payload) != 1444:
                continue
            bits = struct.unpack("!H", payload[12:14])[0]
            nsamp = struct.unpack("!H", payload[14:16])[0]
            if bits == 24 and nsamp * SAMPLE_BYTES + DDC_HEADER_LEN == 1444:
                print(f"  {sip}:{sp} -> {dip}:{dp}  "
                      f"declares bits/sample={bits}, samples/frame={nsamp}  "
                      f"=> RX geometry CONFIRMED by the radio itself")
            else:
                print(f"  {sip}:{sp} -> {dip}:{dp}  "
                      f"bits={bits} nsamp={nsamp} -- not a DDC frame "
                      f"(these are SAMPLES read as fields => this is a DUC/TX stream)")
            break
    print()


if __name__ == "__main__":
    sys.exit(main())
