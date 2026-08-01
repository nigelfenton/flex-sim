#!/usr/bin/env python3
"""Pin the waterfall tile frequency encoding to FlexLib "VitaFrequency" (Hz x 2^20).

AE >= #4412 (VitaTileFrequency.h) decodes FrameLowFreq/BinBandwidth as
raw / (2^20 * 1e6) MHz UNCONDITIONALLY — the old magnitude auto-detect that let
plain Hz through is gone. When the sim sent plain Hz, every tile mapped to
~13 Hz, failed the ±0.25 MHz pan-proximity check (PROTOCOL.md §8.3), and the
waterfall rendered black while the panadapter stayed correct. Found live on
Aether-gate's Pi appliance (RSP1a, 2026-07-31); fix mirrored from Aether-gate
commit a431886.

This test decodes exactly as AE does and asserts the tile lands on the pan, so
a regression to plain Hz (or a double-scaling) fails loudly.

Run: python3 tests/test_wf_packet.py   (no network, no sim process)
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from flex_sim import wf_packet

TILE_SUB = ">qqIHHIIHH"              # FrameLowFreq, BinBandwidth, dur, W, H, timecode, auto_black, W, 0
VITA_FREQ_TO_MHZ = 1048576.0 * 1e6   # AE's kVitaFrequencyToMhz


def _decode_like_ae(pkt, n_bins):
    sub_len = struct.calcsize(TILE_SUB)
    sub = pkt[-(sub_len + 2 * n_bins):-(2 * n_bins)]
    low_raw, binbw_raw = struct.unpack(">qq", sub[:16])
    return low_raw / VITA_FREQ_TO_MHZ, binbw_raw / VITA_FREQ_TO_MHZ


def main():
    low_hz, binbw_hz, bins = 13_926_700.0, 244.140625, 32
    pkt = wf_packet(0x42000000, 0, [0] * bins, low_hz, binbw_hz, timecode=1)
    low_mhz, binbw_mhz = _decode_like_ae(pkt, bins)

    # AE must land the tile at the pan frequency, not 2^20 below it.
    assert abs(low_mhz - low_hz / 1e6) < 1e-6, \
        f"tile decodes to {low_mhz} MHz — plain-Hz regression (AE #4412 has no fallback)"
    assert abs(binbw_mhz * 1e6 - binbw_hz) < 1e-3, \
        f"bin bandwidth decodes to {binbw_mhz * 1e6} Hz, expected {binbw_hz}"

    # The whole tile must span the pan width, not collapse near DC.
    high_mhz = low_mhz + binbw_mhz * bins
    assert high_mhz > low_mhz > 13.0, \
        f"tile spans {low_mhz}..{high_mhz} MHz — collapsed near DC"

    print(f"PASS: tile decodes to {low_mhz:.6f} MHz "
          f"(binbw {binbw_mhz * 1e6:.3f} Hz) — VitaFrequency encoding confirmed")


if __name__ == "__main__":
    main()
