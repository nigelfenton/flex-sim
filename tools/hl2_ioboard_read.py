#!/usr/bin/env python3
"""Read the N2ADR HL2IOBoard's status registers from a Hermes-Lite 2 over HPSDR Protocol 1.

Stand-alone twin of AetherSDR's `hl2` / `ioboard.status` (PR #6703, docs/HERMES.md "The IO board
status read"): one I2C2 read of four bytes from register 6, answered in the radio's EP6 ACK frame.

    reg 6  REG_INPUT_PINS      bit 0 EXTTR (1 = receive), bits 1..5 In1..In5
    reg 7  REG_ANTENNA_TUNER   0xEE send RF, 0 tuned, >= 0xF0 failed
    reg 8  REG_FAULT           0 = none
    reg 9  REG_FIRMWARE_MAJOR  >= 1 on a real board

RECEIVE ONLY, by construction: the MOX bit (C0 bit 0) is never set, no transmit samples and no
frequency or drive register is ever sent, and the tool refuses to start if the radio reports it is
already streaming to someone else.  It does start the radio's EP6 stream (the reply can only ride
on it) and stops it again on exit, so do not run it while AetherSDR (or anything else) is
connected to the same HL2.

    hl2_ioboard_read.py                      discover an HL2 on the LAN, read once
    hl2_ioboard_read.py --ip 192.168.1.50    directed (also works across a router / VPN)
    hl2_ioboard_read.py -n 30 --interval 1   30 reads, one a second, with a summary
    hl2_ioboard_read.py --json               one JSON object per read (machine readable)

Exit status: 0 every read answered by a real board, 1 some reads timed out / bus busy,
2 no board answered (empty bus, no HL2, or a refusal), 3 could not talk to the radio.
Standard library only; Python 3.8+.
"""
import argparse
import json
import socket
import statistics
import struct
import sys
import time

HPSDR_PORT = 1024
SYNC = b"\x7f\x7f\x7f"
FRAME = 512
PKT = 1032
BOARD_HL2 = 6                     # discovery reply byte 10
C0_RQST = 0x80                    # C0 bit 7: response request
C0_I2C2 = 0x7A                    # register 0x3d << 1: the external companion bus
RADDR_I2C2 = 0x3D                 # what the ACK frame reports in C0[6:1]
RADDR_REFUSED = 0x3F              # "I could not do that" (bus busy / not ready)
READ_DATA = 0x079D0600            # cookie 0x07 (read), stop|chip 0x1D, register 6
REPLY_DEADLINE_S = 0.042          # AetherSDR's deadline for one read


def timer_resolution_1ms():
    """Windows sleeps in ~15 ms steps by default; ask for 1 ms so the EP2 pacing is steady."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


def local_ip_towards(ip, port=HPSDR_PORT):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, port))
        return s.getsockname()[0]
    finally:
        s.close()


def discover(sock, ip, port=HPSDR_PORT, wait=2.0):
    """Protocol 1 discovery: directed to `ip`, or broadcast on the socket's interface."""
    req = b"\xef\xfe\x02" + bytes(60)
    target = ip or "255.255.255.255"
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    found = {}
    end = time.monotonic() + wait
    for _ in range(2):
        sock.sendto(req, (target, port))
    sock.settimeout(0.3)
    while time.monotonic() < end:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        if len(data) >= 11 and data[:2] == b"\xef\xfe" and data[2] in (2, 3):
            found[addr[0]] = dict(status=data[2], mac=":".join(f"{b:02X}" for b in data[3:9]),
                                  gateware=data[9], board=data[10])
    return found


def cc_bank(c0, data=0):
    return bytes([c0]) + struct.pack(">I", data)


def ep2(seq, bank_a, bank_b):
    pkt = bytearray(PKT)
    pkt[0:4] = b"\xef\xfe\x01\x02"
    pkt[4:8] = struct.pack(">I", seq & 0xFFFFFFFF)
    for off, bank in ((8, bank_a), (8 + FRAME, bank_b)):
        pkt[off:off + 3] = SYNC
        pkt[off + 3:off + 8] = bank
    return bytes(pkt)


def acks(pkt):
    """Yield (raddr, data) for every ACK frame (C0 bit 7 set) in one EP6 packet."""
    if len(pkt) != PKT or pkt[:3] != b"\xef\xfe\x01" or pkt[3] != 0x06:
        return
    for off in (8, 8 + FRAME):
        if pkt[off:off + 3] != SYNC:
            continue
        c0 = pkt[off + 3]
        if c0 & 0x80:
            yield (c0 >> 1) & 0x3F, struct.unpack(">I", pkt[off + 4:off + 8])[0]


def decode(data):
    pins, tuner, fault, fw = data & 0xFF, (data >> 8) & 0xFF, (data >> 16) & 0xFF, (data >> 24) & 0xFF
    return dict(inputPins=pins, receiving=bool(pins & 1), **{f"in{n}": bool(pins >> n & 1) for n in range(1, 6)},
                antennaTuner=tuner, fault=fault, firmwareMajor=fw,
                fromBoard=(pins & 0xC0) == 0 and fw != 0)


def one_read(sock, radio, seq):
    """Send the read until it is answered or the deadline passes.  Returns (kind, data, rtt_s, seq)."""
    idle = cc_bank(0x00, 0)                      # config bank: 48 kHz, 1 RX, no MOX (all zero)
    read = cc_bank(C0_I2C2 | C0_RQST, READ_DATA)
    sent = time.monotonic()
    sock.sendto(ep2(seq, read, idle), radio)
    seq += 1
    deadline = sent + REPLY_DEADLINE_S
    last_send = sent
    sock.settimeout(0.005)
    while True:
        now = time.monotonic()
        if now >= deadline:
            return "timeout", None, None, seq
        if now - last_send >= 0.005:             # keep the EP2 stream alive while we wait
            sock.sendto(ep2(seq, idle, idle), radio)
            seq += 1
            last_send = now
        try:
            pkt, _ = sock.recvfrom(2048)
        except socket.timeout:
            continue
        for raddr, data in acks(pkt):
            if raddr == RADDR_I2C2:
                return "ok", data, time.monotonic() - sent, seq
            if raddr == RADDR_REFUSED:
                return "busy", None, time.monotonic() - sent, seq


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", help="HL2 address (directed discovery); default: broadcast discovery")
    ap.add_argument("-n", "--count", type=int, default=1, help="number of reads (default 1)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between reads (default 1)")
    ap.add_argument("--json", action="store_true", help="one JSON object per read")
    ap.add_argument("--port", type=int, default=HPSDR_PORT, help=argparse.SUPPRESS)   # tests only
    ap.add_argument("--local", help="local IP to bind (default: the interface towards the radio)")
    args = ap.parse_args()
    timer_resolution_1ms()

    local = args.local or (local_ip_towards(args.ip, args.port) if args.ip else "0.0.0.0")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind((local, 0))
    try:
        radios = discover(sock, args.ip, args.port)
    except OSError as e:
        print(f"discovery failed: {e}", file=sys.stderr)
        return 3
    hl2s = {ip: r for ip, r in radios.items() if r["board"] == BOARD_HL2}
    if not hl2s:
        what = f"at {args.ip}" if args.ip else "on this LAN"
        print(f"no Hermes-Lite 2 answered discovery {what}" +
              (f" (saw board ids {sorted({r['board'] for r in radios.values()})})" if radios else ""), file=sys.stderr)
        return 3
    if len(hl2s) > 1 and not args.ip:
        print("several HL2s answered; pick one with --ip:", ", ".join(hl2s), file=sys.stderr)
        return 3
    ip, info = next(iter(hl2s.items()))
    if info["status"] != 2:
        print(f"{ip} reports it is busy (status {info['status']}): someone else is connected. Not starting.",
              file=sys.stderr)
        return 3
    radio = (ip, args.port)
    if not args.json:
        print(f"HL2 {ip}  MAC {info['mac']}  gateware {info['gateware']}  idle")

    results, seq = [], 0
    sock.sendto(b"\xef\xfe\x04\x01" + bytes(60), radio)          # start IQ stream (receive)
    try:
        # prime: a few EP2 packets so the radio is settled before the first read
        for _ in range(10):
            sock.sendto(ep2(seq, cc_bank(0x00, 0), cc_bank(0x00, 0)), radio)
            seq += 1
            time.sleep(0.003)
        for i in range(args.count):
            kind, data, rtt, seq = one_read(sock, radio, seq)
            row = {"n": i + 1, "result": kind}
            if kind == "ok":
                row.update(decode(data), raw=f"0x{data:08X}", rttMs=round(rtt * 1000, 1))
            results.append(row)
            if args.json:
                print(json.dumps(row), flush=True)
            elif kind == "ok":
                print(f"#{i+1:<3} {rtt*1000:5.1f} ms  raw {row['raw']}  pins 0x{row['inputPins']:02X} "
                      f"({'RX' if row['receiving'] else 'TX'}; In1..5 "
                      f"{''.join('1' if row[f'in{n}'] else '0' for n in range(1, 6))})  "
                      f"ATU 0x{row['antennaTuner']:02X}  fault {row['fault']}  fw {row['firmwareMajor']}"
                      f"{'' if row['fromBoard'] else '   <-- NOT a board (empty bus?)'}", flush=True)
            elif kind == "busy":
                print(f"#{i+1:<3} I2C bus busy (0x3F) - retryable", flush=True)
            else:
                print(f"#{i+1:<3} no answer in {REPLY_DEADLINE_S*1000:.0f} ms - retryable", flush=True)
            if i + 1 < args.count:
                end = time.monotonic() + args.interval
                while time.monotonic() < end:                    # keep the stream alive between reads
                    sock.sendto(ep2(seq, cc_bank(0x00, 0), cc_bank(0x00, 0)), radio)
                    seq += 1
                    time.sleep(0.005)
    finally:
        try:
            sock.sendto(b"\xef\xfe\x04\x00" + bytes(60), radio)  # stop the stream
        except OSError:
            pass
        sock.close()

    ok = [r for r in results if r["result"] == "ok"]
    board = [r for r in ok if r["fromBoard"]]
    if not args.json and len(results) > 1:
        rtts = [r["rttMs"] for r in ok]
        regs = {r["raw"] for r in ok}
        print(f"\n{len(ok)}/{len(results)} answered"
              + (f", RTT median {statistics.median(rtts):.1f} ms (min {min(rtts):.1f}, max {max(rtts):.1f})" if rtts else "")
              + f"; distinct register words: {sorted(regs) if regs else 'none'}")
    if not ok:
        return 3 if results and all(r["result"] == "timeout" for r in results) else 2
    if not board:
        return 2
    return 0 if len(board) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
