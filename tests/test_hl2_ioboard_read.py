#!/usr/bin/env python3
"""tools/hl2_ioboard_read.py against a fake Hermes-Lite 2 on loopback.

The tool is meant to be pointed at real hardware, so what it must NOT do matters as much as what
it does.  The fake radio therefore records every C&C bank the tool sends and the test asserts:

  * the read is exactly  FA 07 9D 06 00  (I2C2 address 0x3d<<1 with the response-request bit,
    cookie 0x07 = read, stop|chip 0x1D, register 6);
  * RECEIVE ONLY: no bank ever has the MOX bit (C0 bit 0) set, and nothing but the config bank
    (C0 0x00) and that one read bank is ever sent -- no frequency, no drive, no I2C write;
  * no transmit payload: every EP2 payload byte is zero;
  * it starts the stream before reading and stops it afterwards;
  * it refuses (and sends nothing) when the radio says it is busy, or is not an HL2;
  * what it concludes from the reply: a real board, an empty bus (0xFF), a refusal (0x3F), a
    silent radio, each with its own exit status.

Run: python3 tests/test_hl2_ioboard_read.py   (or via pytest)
"""

import json
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "hl2_ioboard_read.py"
SYNC = b"\x7f\x7f\x7f"
READ_BANK = bytes([0xFA, 0x07, 0x9D, 0x06, 0x00])
BOARD_WORD = 0x02000001        # pins 0x01 (EXTTR = receive), ATU 0, fault 0, firmware major 2


class FakeHl2(threading.Thread):
    def __init__(self, reply="board", status=2, board=6):
        super().__init__(daemon=True)
        self.reply, self.status, self.board = reply, status, board
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.banks = []                # every 5-byte C&C bank received in EP2 frames
        self.payload_nonzero = 0
        self.events = []               # "start" / "stop"
        self.stop_flag = False
        self.seq = 0

    def ep6(self, ack_word):
        pkt = bytearray(1032)
        pkt[0:4] = b"\xef\xfe\x01\x06"
        pkt[4:8] = struct.pack(">I", self.seq)
        self.seq += 1
        free = bytes([0x00, 0, 0, 0, 0])            # free-running response (ACK bit clear)
        other = bytes([0x80 | (0x01 << 1), 0, 0, 0, 0x55])    # an ACK for a DIFFERENT register
        frames = [(ack_word if ack_word else other), free]
        for off, bank in zip((8, 520), frames):
            pkt[off:off + 3] = SYNC
            pkt[off + 3:off + 8] = bank
        return bytes(pkt)

    def run(self):
        client = None
        while not self.stop_flag:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if data[:3] == b"\xef\xfe\x02":                              # discovery
                mac = bytes([0x00, 0x1C, 0xC0, 0xA2, 0x13, 0xDD])
                reply = bytes([0xEF, 0xFE, self.status]) + mac + bytes([74, self.board]) + bytes(49)
                self.sock.sendto(reply, addr)
            elif data[:4] == b"\xef\xfe\x04\x01":
                client = addr
                self.events.append("start")
            elif data[:4] == b"\xef\xfe\x04\x00":
                client = None
                self.events.append("stop")
            elif len(data) == 1032 and data[:4] == b"\xef\xfe\x01\x02":
                got_read = False
                for off in (8, 520):
                    bank = data[off + 3:off + 8]
                    self.banks.append(bank)
                    got_read = got_read or bank == READ_BANK
                    if any(data[off + 8:off + 512]):
                        self.payload_nonzero += 1
                if client:
                    word = None
                    if got_read and self.reply != "silent":
                        if self.reply == "board":
                            word = BOARD_WORD
                        elif self.reply == "empty":
                            word = 0xFFFFFFFF
                        elif self.reply == "busy":
                            word = 0x079D0600
                    if word is not None:
                        c0 = 0x80 | ((0x3F if self.reply == "busy" else 0x3D) << 1)
                        self.sock.sendto(self.ep6(bytes([c0]) + struct.pack(">I", word)), client)
                    else:
                        self.sock.sendto(self.ep6(None), client)          # only noise
        self.sock.close()

    def finish(self):
        self.stop_flag = True
        self.join(2)


def run_tool(radio, count=3):
    r = subprocess.run([sys.executable, str(TOOL), "--ip", "127.0.0.1", "--port", str(radio.port),
                        "-n", str(count), "--interval", "0.05", "--json"],
                       capture_output=True, text=True, timeout=60)
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
    return r.returncode, rows, r.stderr


def with_radio(**kw):
    radio = FakeHl2(**kw)
    radio.start()
    return radio


def test_real_board_is_read_and_only_that_is_sent():
    radio = with_radio(reply="board")
    try:
        rc, rows, err = run_tool(radio)
    finally:
        time.sleep(0.2)
        radio.finish()
    assert rc == 0, (rc, err)
    assert len(rows) == 3 and all(r["result"] == "ok" for r in rows), rows
    r = rows[0]
    assert r["raw"] == "0x02000001" and r["inputPins"] == 1 and r["receiving"] is True
    assert r["antennaTuner"] == 0 and r["fault"] == 0 and r["firmwareMajor"] == 2 and r["fromBoard"] is True
    assert not any(r[f"in{n}"] for n in range(1, 6))
    # the read, exactly as the gateware wants it
    assert READ_BANK in radio.banks
    # receive only: nothing but the zero config bank and the read bank was ever sent
    odd = {b.hex() for b in radio.banks} - {"0000000000", READ_BANK.hex()}
    assert not odd, f"unexpected C&C banks sent: {odd}"
    assert all(b[0] & 0x01 == 0 for b in radio.banks), "MOX bit was set"
    assert radio.payload_nonzero == 0, "transmit payload bytes were sent"
    assert radio.events == ["start", "stop"], radio.events


def test_empty_bus_is_not_a_board():
    radio = with_radio(reply="empty")
    try:
        rc, rows, _ = run_tool(radio)
    finally:
        radio.finish()
    assert rc == 2 and rows and all(r["result"] == "ok" and r["fromBoard"] is False for r in rows), (rc, rows)


def test_bus_busy_refusal_is_reported_not_read():
    radio = with_radio(reply="busy")
    try:
        rc, rows, _ = run_tool(radio)
    finally:
        radio.finish()
    assert rc == 2 and rows and all(r["result"] == "busy" for r in rows), (rc, rows)


def test_silent_radio_times_out():
    radio = with_radio(reply="silent")
    try:
        rc, rows, _ = run_tool(radio, count=2)
    finally:
        radio.finish()
    assert rc == 3 and rows and all(r["result"] == "timeout" for r in rows), (rc, rows)


def test_busy_or_wrong_radio_is_refused_without_starting_a_stream():
    for kw in ({"status": 3}, {"board": 1}):
        radio = with_radio(reply="board", **kw)
        try:
            rc, rows, err = run_tool(radio)
        finally:
            time.sleep(0.2)
            radio.finish()
        assert rc == 3 and not rows, (kw, rc, rows, err)
        assert radio.events == [] and radio.banks == [], f"{kw}: something was sent: {radio.events} {radio.banks}"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:      # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failed else 0)
