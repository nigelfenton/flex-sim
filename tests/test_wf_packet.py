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


# One 20 m tile, decoded the way AE decodes it. Shared by every case below so a
# regression shows up in whichever assertion it actually breaks.
LOW_HZ, BINBW_HZ, BINS = 13_926_700.0, 244.140625, 32


def _tile():
    pkt = wf_packet(0x42000000, 0, [0] * BINS, LOW_HZ, BINBW_HZ, timecode=1)
    return _decode_like_ae(pkt, BINS)


def test_tile_lands_on_the_pan_frequency():
    """Plain Hz would decode 2^20 low — the black-waterfall regression."""
    low_mhz, _ = _tile()
    assert abs(low_mhz - LOW_HZ / 1e6) < 1e-6, \
        f"tile decodes to {low_mhz} MHz — plain-Hz regression (AE #4412 has no fallback)"


def test_bin_bandwidth_survives_the_encoding():
    _, binbw_mhz = _tile()
    assert abs(binbw_mhz * 1e6 - BINBW_HZ) < 1e-3, \
        f"bin bandwidth decodes to {binbw_mhz * 1e6} Hz, expected {BINBW_HZ}"


def test_tile_spans_the_pan_width():
    """Catches a collapse near DC even if the low edge happened to look right."""
    low_mhz, binbw_mhz = _tile()
    high_mhz = low_mhz + binbw_mhz * BINS
    assert high_mhz > low_mhz > 13.0, \
        f"tile spans {low_mhz}..{high_mhz} MHz — collapsed near DC"


def main():
    """Standalone entry point — kept so `python3 tests/test_wf_packet.py` still
    works on a box without pytest (the Pi appliance, a bare sim host)."""
    test_tile_lands_on_the_pan_frequency()
    test_bin_bandwidth_survives_the_encoding()
    test_tile_spans_the_pan_width()
    low_mhz, binbw_mhz = _tile()
    print(f"PASS: tile decodes to {low_mhz:.6f} MHz "
          f"(binbw {binbw_mhz * 1e6:.3f} Hz) — VitaFrequency encoding confirmed")


if __name__ == "__main__":
    main()
