#!/usr/bin/env python3
"""Pin p2verify.py against a synthetic capture whose answers are known.

The real reference captures (a private station's ANAN-G2 sessions) are not in
this repo, so this builds a small pcapng in memory that carries exactly the
things p2verify exists to get right, and asserts it gets them right:

  - a DDC (RX) stream: 16 B header + 1428 B = 238 samples, declared by the frame
    itself (bits/sample = 24, samples/frame = 238), at the 48 k cadence
  - a DUC (TX) stream: 4 B header + 1440 B = 240 samples, all-zero payload
    ("keyed silence"), whose would-be header bytes are sample data
  - a discovery reply from an ANAN-G2 (board type 10 = SATURN)
  - TRAP 1: the interface declares if_tsresol = 9 (nanoseconds). Read as the
    default microseconds, the derived rate comes out ~1000x too low.
  - TRAP 2: multicast traffic shares the wire -- sent here to a port INSIDE the
    P2 range, so only the multicast filter can keep it out of the report.

Every expected value is derived from the construction below, not copied from
p2verify's output.

Run: python3 -m pytest tests/test_p2verify.py   (no network, no radio)
"""

import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
P2VERIFY = ROOT / "p2verify.py"

RADIO = "10.0.0.2"
PC = "10.0.0.1"

DDC_SAMPLES = 238
DDC_RATE = 48_000
DDC_FRAMES = 150
DUC_SAMPLES = 240
DUC_RATE = 192_000
DUC_FRAMES = 150
NS = 1_000_000_000


# --- pcapng construction (little-endian section) ------------------------------

def _block(btype, body):
    body += b"\x00" * ((-len(body)) % 4)            # pad the body to 4 bytes
    total = 12 + len(body)                          # type + len + body + len
    return struct.pack("<II", btype, total) + body + struct.pack("<I", total)


def _shb():
    # byte-order magic, version 1.0, section length unknown (-1), no options
    return _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))


def _idb_nanoseconds():
    options = struct.pack("<HHB3x", 9, 1, 9)        # if_tsresol = 10^-9
    options += struct.pack("<HH", 0, 0)             # opt_endofopt
    return _block(0x00000001, struct.pack("<HHI", 1, 0, 65535) + options)


def _epb(ts_ns, frame):
    head = struct.pack("<IIIII", 0, ts_ns >> 32, ts_ns & 0xFFFFFFFF,
                       len(frame), len(frame))
    return _block(0x00000006, head + frame)


def _udp_frame(src_ip, sport, dst_ip, dport, payload):
    eth = b"\x02" * 6 + b"\x04" * 6 + struct.pack("!H", 0x0800)
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                     bytes(map(int, src_ip.split("."))),
                     bytes(map(int, dst_ip.split("."))))
    return eth + ip + udp


def _build_capture(path):
    blocks = [_shb(), _idb_nanoseconds()]

    # discovery reply: 60 B from radio :1024, status 0x02, board 10, P2 3.9, fw 21
    reply = bytearray(60)
    reply[4] = 0x02
    reply[11] = 10
    reply[12] = 39
    reply[13] = 21
    reply[20] = 2
    blocks.append(_epb(0, _udp_frame(RADIO, 1024, PC, 50000, bytes(reply))))

    # DDC (RX): radio :1037 -> PC session port, self-describing header, non-zero IQ
    ddc_step = DDC_SAMPLES * NS // DDC_RATE
    iq = bytes((i * 37) & 0xFF for i in range(DDC_SAMPLES * 6))
    for n in range(DDC_FRAMES):
        payload = (struct.pack("!IQHH", n, n * DDC_SAMPLES, 24, DDC_SAMPLES) + iq)
        blocks.append(_epb(NS + n * ddc_step,
                           _udp_frame(RADIO, 1037, PC, 50001, payload)))

    # DUC (TX): PC -> radio :1029, sequence only, 1440 B of silence
    duc_step = DUC_SAMPLES * NS // DUC_RATE
    for n in range(DUC_FRAMES):
        payload = struct.pack("!I", n) + b"\x00" * (DUC_SAMPLES * 6)
        blocks.append(_epb(NS + n * duc_step,
                           _udp_frame(PC, 50002, RADIO, 1029, payload)))

    # TRAP 2: unrelated multicast on the same wire. Deliberately on a port INSIDE
    # the P2 range: on port 5004 the port filter alone would drop it, the
    # multicast filter would never be exercised, and this test would pass with
    # that filter deleted (a mutation run showed exactly that).
    for n in range(300):
        blocks.append(_epb(NS + n * 1_000_000,
                           _udp_frame("10.0.0.9", 1100, "239.1.1.1", 1100,
                                      b"\x80" * 172)))

    path.write_bytes(b"".join(blocks))


def _run(capture):
    result = subprocess.run([sys.executable, str(P2VERIFY), str(capture)],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _output():
    with tempfile.TemporaryDirectory() as tmp:
        capture = Path(tmp) / "synthetic_p2.pcapng"
        _build_capture(capture)
        return _run(capture)


# --- assertions ---------------------------------------------------------------

def test_discovery_reply_identifies_an_anan_g2():
    out = _output()
    assert "board type   0x0a (SATURN/ANAN-G2)" in out, out
    assert "protocol ver 3.9" in out, out


def test_rx_frames_classified_by_their_own_header_as_238_samples():
    out = _output()
    assert "DDC (RX): 16B header, 1428 IQ bytes, 238 samples/frame" in out, out
    assert "RX geometry CONFIRMED by the radio itself" in out, out


def test_tx_frames_classified_as_240_samples_and_flagged_silent():
    out = _output()
    assert "DUC (TX): 4B header, 1440 IQ bytes, 240 samples/frame" in out, out
    assert "keyed silence" in out, out


def test_nanosecond_timestamps_give_the_true_sample_rate():
    """TRAP 1. Microsecond assumption would report ~48 Hz, not ~48 kHz."""
    out = _output()
    rates = [float(r.replace(",", ""))
             for r in re.findall(r"derived sample rate ([\d,]+\.\d) Hz", out)]
    assert rates, out
    # samples * frames / (frames - 1) intervals: within 2 % of the nominal rate
    assert any(abs(r - DDC_RATE) / DDC_RATE < 0.02 for r in rates), rates
    assert any(abs(r - DUC_RATE) / DUC_RATE < 0.02 for r in rates), rates


def test_multicast_noise_is_not_reported_as_a_p2_flow():
    """TRAP 2."""
    out = _output()
    flows = out.split("--- flows (P2 ports, unicast) ---", 1)[1]
    assert "239.1.1.1" not in flows, flows


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
    print("all p2verify checks passed")
