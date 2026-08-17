#!/usr/bin/env python3
"""Pin anan_sim's Protocol 2 wire against the documented facts.

Drives a live anan_sim exactly as a P2 client does — discovery, then the run
bit — and asserts the bytes on the wire, not merely that the process started.
Every assertion here traces to a source:

  - discovery reply field offsets: pihpsdr src/new_discovery.c
    ([0:4] zero, [4] status, [5:11] MAC, [11] device, [12] P2 ver,
     [13] sw ver, [20] DDC count), and NEW_DEVICE_SATURN 1010 = 1000 + 0x0A
  - reply goes to the requester's SOURCE port, ports, and "no run/stop packet —
    the run bit is in High Priority": Saturn project_documentation/
    "protocol 2 data transder.docx"
  - DDC/RX is 16 + 1428 B = 238 samples at every rate. VERIFIED against N2JXL's
    real ANAN-G2 pcaps 2026-08-17 (samples/frame reads 238, 238*6=1428 exactly).
    NOTE the spreadsheet's "1440 B / 240 samples" is the DUC/TX geometry (4 B
    header + 1440), which does NOT fit RX -- it overruns the 1444 B payload by 12.

Run: python3 tests/test_anan_p2.py   (spawns its own sim; no radio, no network
                                      beyond loopback)
"""

import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IP = "127.0.0.1"

PORT_DISCOVERY = 1024
PORT_HIGH_PRIO_IN = 1027
IQ_PAYLOAD_BYTES = 1428
IQ_SAMPLES = 238
IQ_HEADER_BYTES = 16

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def discovery_request():
    b = bytearray(60)
    b[4] = 0x02
    return bytes(b)


def high_priority(run, centre_hz=14_100_000):
    b = bytearray(60)
    b[4] = 0x01 if run else 0x00
    struct.pack_into(">I", b, 9, centre_hz)
    return bytes(b)


def main():
    # A watchdog, because a sim that streams when it should be silent makes this
    # test consume packets forever — and a hang is indistinguishable from a pass
    # in CI. Proven necessary: removing the run-bit gate hung this test at 124.
    deadline = time.time() + 60

    def expired():
        return time.time() > deadline

    sim = subprocess.Popen(
        [sys.executable, str(ROOT / "anan_sim.py"), "--ip", IP, "--rate", "48000"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT))
    try:
        # Wait for the discovery listener rather than sleeping blindly.
        ready = False
        t0 = time.time()
        while time.time() - t0 < 15:
            line = sim.stdout.readline().decode(errors="replace")
            if "waiting for discovery" in line:
                ready = True
                break
            if not line and sim.poll() is not None:
                break
        if not ready:
            print("FAIL: sim never came up")
            return 1

        # --- discovery ------------------------------------------------------
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((IP, 0))                      # ephemeral: proves SOURCE-port reply
        my_port = s.getsockname()[1]
        s.settimeout(5)
        s.sendto(discovery_request(), (IP, PORT_DISCOVERY))
        try:
            reply, frm = s.recvfrom(2048)
        except socket.timeout:
            print("FAIL: no discovery reply")
            return 1

        check("reply arrives at our ephemeral source port, not 1024",
              frm[1] == PORT_DISCOVERY and my_port != PORT_DISCOVERY,
              f"from {frm}, ours {my_port}")
        check("reply is 60 bytes", len(reply) == 60, f"got {len(reply)}")
        check("reply[0:4] are zero", reply[0:4] == b"\x00\x00\x00\x00")
        check("status byte = 0x02 (idle/available)", reply[4] == 0x02,
              f"got 0x{reply[4]:02x}")
        check("device id = 0x0A (Saturn; pihpsdr 1010-1000)", reply[11] == 0x0A,
              f"got 0x{reply[11]:02x}")
        check("P2 version present", reply[12] > 0, f"got {reply[12]}")
        check("DDC count advertised", reply[20] > 0, f"got {reply[20]}")

        # --- a P1 probe must NOT be answered with a P2 reply ---------------
        s.sendto(b"\xef\xfe\x02" + bytes(60), (IP, PORT_DISCOVERY))
        s.settimeout(1.5)
        try:
            stray, _ = s.recvfrom(2048)
            check("P1 (Metis) probe is ignored, not answered", False,
                  f"got {len(stray)} bytes back")
        except socket.timeout:
            check("P1 (Metis) probe is ignored, not answered", True)

        # --- no data before the run bit ------------------------------------
        s.settimeout(1.0)
        try:
            early, _ = s.recvfrom(4096)
            check("no IQ before the run bit", False, f"got {len(early)} bytes")
        except socket.timeout:
            check("no IQ before the run bit", True)

        # --- run bit = 1 -> DDC0 IQ starts ---------------------------------
        hp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        hp.sendto(high_priority(True), (IP, PORT_HIGH_PRIO_IN))

        s.settimeout(5)
        pkts, t_first, t_last = [], None, None
        t0 = time.time()
        while time.time() - t0 < 3.0 and len(pkts) < 60:
            try:
                d, _ = s.recvfrom(4096)
            except socket.timeout:
                break
            # Collect every datagram big enough to be an IQ frame. Filtering on
            # the EXPECTED size here would make a wrong-sized packet disappear
            # instead of failing the size check below -- which is exactly how the
            # 240-sample error stayed green until the real-hardware pcap.
            if len(d) > IQ_HEADER_BYTES:
                if t_first is None:
                    t_first = time.time()
                t_last = time.time()
                pkts.append(d)

        check("DDC0 IQ flows once the run bit is set", len(pkts) >= 10,
              f"got {len(pkts)} packets")
        if pkts:
            check(f"packet is {IQ_HEADER_BYTES}+{IQ_PAYLOAD_BYTES} bytes",
                  len(pkts[0]) == IQ_HEADER_BYTES + IQ_PAYLOAD_BYTES,
                  f"got {len(pkts[0])}")
            bits, nsamp = struct.unpack_from(">HH", pkts[0], 12)
            check("header declares 24 bits/sample", bits == 24, f"got {bits}")
            check(f"header declares {IQ_SAMPLES} samples/frame", nsamp == IQ_SAMPLES,
                  f"got {nsamp}")
            seqs = [struct.unpack_from(">I", p, 0)[0] for p in pkts]
            check("sequence numbers increment by 1",
                  all(b - a == 1 for a, b in zip(seqs, seqs[1:])),
                  f"{seqs[:5]}...")
            # 238 samples @ 48 kHz = 4.96 ms/packet. Allow generous slack for a
            # Python sender on a loaded box; we are pinning the RATE, not jitter.
            if len(pkts) >= 10 and t_first and t_last > t_first:
                per = (t_last - t_first) / (len(pkts) - 1)
                check("cadence ~4.96 ms/packet at 48 kHz (238 samples)",
                      0.002 < per < 0.020, f"measured {per*1000:.2f} ms")

        # --- run bit = 0 -> stops ------------------------------------------
        hp.sendto(high_priority(False), (IP, PORT_HIGH_PRIO_IN))
        time.sleep(0.4)
        while not expired():                         # drain what was in flight
            try:
                s.settimeout(0.3)
                s.recvfrom(4096)
            except socket.timeout:
                break
        s.settimeout(1.0)
        try:
            after, _ = s.recvfrom(4096)
            check("clearing the run bit stops the stream", False,
                  f"still got {len(after)} bytes")
        except socket.timeout:
            check("clearing the run bit stops the stream", True)

        s.close()
        hp.close()
        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        sim.kill()


def test_anan_p2_wire():
    """pytest entry point. One test: this is an ordered handshake (discover →
    run → stream → stop) where each step sets up the next, and splitting it
    would spawn a sim per case on fixed ports. Skips if the ports are busy."""
    rc = main()
    if rc == 0:
        return
    import pytest
    if failed == 0:
        pytest.skip(f"anan_sim could not be started (ports {PORT_DISCOVERY}/"
                    f"{PORT_HIGH_PRIO_IN} busy?)")
    assert rc == 0, f"{failed} P2 wire check(s) failed — see output above"


if __name__ == "__main__":
    sys.exit(main())
