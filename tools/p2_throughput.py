#!/usr/bin/env python3
"""Measure how many P2 IQ packets/s anan_sim actually delivers at each rate.

Drives a sim the way a client does (discovery, DDC Specific with a rate, run bit)
and counts datagrams from the DDC's source port for a few seconds, against the
nominal rate/238. v0.1 managed ~540 packets/s on a Celeron J1900 whatever the
rate, i.e. two-thirds of nominal at 192k and a tenth at 1536k; v0.2 pre-renders
the signal so packet building stops being the limit.

  python3 tools/p2_throughput.py [--host 127.0.0.1] [--ddc 2] [--seconds 4]
Receiving on loopback of the machine running the sim measures the SIM; receiving
across a LAN also measures the receiver and the network.
"""
import argparse
import socket
import struct
import time

DDC_RATES_KHZ = (48, 96, 192, 384, 768, 1536)
SAMPLES = 238


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ddc", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--rates", type=int, nargs="*", default=list(DDC_RATES_KHZ))
    a = ap.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    d = bytearray(60)
    d[4] = 2
    s.sendto(bytes(d), (a.host, 1024))
    s.settimeout(3)
    try:
        s.recvfrom(2048)
    except socket.timeout:
        raise SystemExit(f"no discovery reply from {a.host}")
    g = bytearray(60)
    g[37] = 0x08
    s.sendto(bytes(g), (a.host, 1024))
    print(f"{'rate':>8} {'nominal':>9} {'measured':>9} {'of nominal':>11}")
    for khz in a.rates:
        spec = bytearray(1444)
        spec[4] = 2
        spec[7] = 1 << a.ddc
        struct.pack_into(">H", spec, 18 + 6 * a.ddc, khz)
        s.sendto(bytes(spec), (a.host, 1025))
        hp = bytearray(1444)
        hp[4] = 1
        s.sendto(bytes(hp), (a.host, 1027))
        port = 1035 + a.ddc
        t_end = time.time() + 1.0                    # let the rate change land
        while time.time() < t_end:
            try:
                s.settimeout(0.2)
                s.recvfrom(4096)
            except socket.timeout:
                pass
        n, t0 = 0, time.time()
        while time.time() - t0 < a.seconds:
            try:
                s.settimeout(0.5)
                data, frm = s.recvfrom(4096)
            except socket.timeout:
                continue
            if frm[1] == port:
                n += 1
        dt = time.time() - t0
        nominal = khz * 1000 / SAMPLES
        print(f"{khz:>6}k {nominal:>9.0f} {n/dt:>9.0f} {100*n/dt/nominal:>10.0f}%")
    s.sendto(bytes(bytearray(1444)), (a.host, 1027))     # run bit off


if __name__ == "__main__":
    main()
