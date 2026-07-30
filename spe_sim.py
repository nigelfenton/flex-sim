#!/usr/bin/env python3
# spe_sim.py — standalone SPE Expert amplifier simulator for AetherSDR.
#
# Desktop sibling of the Giga R1 quad-sim's SPE persona (2026-07-30). Emulates
# the raw-TCP (ser2net-style) link AE's SpeConnection speaks in Network mode,
# so SpeApplet + tests can run with no amplifier and no Giga on the bench.
#
# Protocol (verified against AE PR #4531's SpeProtocol.{h,cpp} — the amp-side
# byte model here reproduced the PR's own test vectors byte-identically, and
# the Giga port of this model passed a live wire suite against the shipping
# FrameParser, 21/21):
#   - POLL-ONLY: the amp NEVER speaks unprompted. No greeting (unlike
#     pgxl/tgxl_sim). Silence means "nobody is asking", by design.
#   - host -> amp:  55 55 55 | CNT | DATA | CHK | CR LF   (CNT=1 always from
#     AE; CHK = mod-256 sum of DATA = the command byte repeated)
#   - amp  -> host: AA AA AA | CNT | DATA | CHK... | CR LF — the 1-byte ACK
#     echoes the keystroke with a 1-byte checksum; the Status string carries
#     a 16-BIT checksum, LOW byte first. Wrong byte order = silently dropped.
#   - Status body: ASCII CSV, marker 'C' + 19 fields. NOT fixed-width — the
#     volts/amps fields are " %.1f", which is why the reference vectors are
#     67 chars in RX and 69 in TX. AE's parser splits on commas and trims.
#   - Band table has 60m at index 2 — 20m is index 05, not 04.
#   - Power-ON is an RFC 2217 control-line pulse, NOT protocol — out of scope
#     for a raw TCP sim (same limitation as the Giga persona).
#
# Run:   python3 spe_sim.py                # listens on :4531, interactive
#        python3 spe_sim.py --no-cli       # headless
# Then in AE: SPE applet -> Network -> <this-host>:4531.

import argparse
import socket
import threading
import time

SPE_PORT = 4531   # matches the Giga persona; AE's SPE PR number, off-band

HOST_SYNC = 0x55
AMP_SYNC = 0xAA

K_STATUS_REQUEST = 0x90
K_BACKLIGHT_ON = 0x82
K_BACKLIGHT_OFF = 0x83
KEY_BAND_DOWN = 0x02
KEY_BAND_UP = 0x03
KEY_ANTENNA = 0x04
KEY_TUNE = 0x09
KEY_SWITCH_OFF = 0x0A
KEY_POWER = 0x0B
KEY_OPERATE = 0x0D

BANDS = ["160m", "80m", "60m", "40m", "30m", "20m",
         "17m", "15m", "12m", "10m", "6m", "4m"]


class Amp:
    """Amp-side state. Model 13K / 15K / 20K (2K-FA always reports bank x)."""

    def __init__(self, model="15K"):
        self.lock = threading.Lock()
        self.model = model
        self.operate = False
        self.tx = False
        self.bank = "x" if model == "20K" else "A"
        self.input = 1
        self.band = 5            # 20m — index 5, NOT 4 (60m sits at 2)
        self.tx_ant = 1
        self.atu = "a"           # t tunable / b bypassed / a enabled
        self.rx_ant = "0r"
        self.power_lvl = "L"     # L / M / H
        self.out_w = 0
        self.swr_atu = 0.0
        self.swr_ant = 0.0
        self.pa_v = 0.0
        self.pa_a = 0.0
        self.t_up = 33
        self.t_lo = 0
        self.t_comb = 0
        self.warning = "N"
        self.alarm = "N"

    def status_payload(self) -> bytes:
        with self.lock:
            f = ["C", self.model,
                 "O" if self.operate else "S",
                 "T" if self.tx else "R",
                 self.bank, str(self.input), "%02d" % self.band,
                 "%d%s" % (self.tx_ant, self.atu), self.rx_ant,
                 self.power_lvl, "%04d" % self.out_w,
                 "%5.2f" % self.swr_atu, "%5.2f" % self.swr_ant,
                 " %.1f" % self.pa_v, " %.1f" % self.pa_a,
                 "%3d" % self.t_up, "%3d" % self.t_lo, "%3d" % self.t_comb,
                 self.warning, self.alarm]
            return ",".join(f).encode("ascii")

    def key(self, on, tx_watts=1350, tx_swr=1.10):
        with self.lock:
            self.tx = on
            if on:
                self.operate = True
                self.out_w = tx_watts
                self.swr_atu = tx_swr
                self.swr_ant = tx_swr + 0.15
                self.pa_v = 51.0
                self.pa_a = tx_watts / 45.0
            else:
                self.out_w = 0
                self.swr_atu = self.swr_ant = 0.0
                self.pa_v = self.pa_a = 0.0

    def keystroke(self, cmd):
        with self.lock:
            if cmd == KEY_OPERATE:
                self.operate = not self.operate
            elif cmd == KEY_BAND_UP:
                self.band = min(11, self.band + 1)
            elif cmd == KEY_BAND_DOWN:
                self.band = max(0, self.band - 1)
            elif cmd == KEY_ANTENNA:
                self.tx_ant = self.tx_ant % 4 + 1
            elif cmd == KEY_POWER:
                self.power_lvl = {"L": "M", "M": "H", "H": "L"}[self.power_lvl]
            elif cmd == KEY_TUNE:
                self.atu = "t"
            elif cmd == KEY_SWITCH_OFF:
                self.operate = False
                self.tx = False


def frame_ack(cmd: int) -> bytes:
    return bytes([AMP_SYNC] * 3 + [1, cmd, cmd & 0xFF]) + b"\r\n"


def frame_status(payload: bytes) -> bytes:
    s = sum(payload) & 0xFFFF
    return (bytes([AMP_SYNC] * 3 + [len(payload)]) + payload
            + bytes([s & 0xFF, (s >> 8) & 0xFF]) + b"\r\n")


class SpeServer:
    def __init__(self, amp):
        self.amp = amp

    def handle_client(self, conn, addr):
        print(f"[tcp] client connected {addr}", flush=True)
        buf = b""
        try:
            conn.settimeout(60)
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data
                while True:
                    i = buf.find(b"\x55\x55\x55")
                    if i < 0 or len(buf) < i + 5:
                        break
                    cnt = buf[i + 3]
                    if cnt < 1 or cnt > 72:
                        buf = buf[i + 1:]
                        continue
                    end = i + 4 + cnt + 1
                    if len(buf) < end:
                        break
                    payload = buf[i + 4:i + 4 + cnt]
                    ok = buf[end - 1] == (sum(payload) & 0xFF)
                    buf = buf[end:]
                    if buf[:2] == b"\r\n":
                        buf = buf[2:]
                    if not ok:
                        print("[tcp] checksum mismatch, dropped", flush=True)
                        continue
                    cmd = payload[0]
                    if cmd == K_STATUS_REQUEST:
                        conn.sendall(frame_status(self.amp.status_payload()))
                    elif cmd in (K_BACKLIGHT_ON, K_BACKLIGHT_OFF):
                        conn.sendall(frame_ack(cmd))
                    else:
                        self.amp.keystroke(cmd)
                        conn.sendall(frame_ack(cmd))
        except Exception as e:
            print(f"[tcp] client error {addr}: {e}", flush=True)
        finally:
            conn.close()
            print(f"[tcp] {addr} disconnected", flush=True)

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", SPE_PORT))
        srv.listen(5)
        print(f"[tcp] SPE simulator listening on :{SPE_PORT} (poll-only, "
              f"no greeting by design)", flush=True)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn, addr),
                             daemon=True).start()


def cli(amp):
    HELP = ("commands: model <13K|15K|20K> | oper <0|1> | key | unkey | "
            "power <W> | swr <v> | temp <C> | band <0-11> | level <L|M|H> | "
            "warn <ch> | alarm <ch> | status | help | quit")
    print(HELP, flush=True)
    while True:
        try:
            parts = input("spe> ").strip().split()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not parts:
            continue
        c = parts[0].lower()
        try:
            if c in ("quit", "exit"):
                return
            elif c == "help":
                print(HELP)
            elif c == "status":
                print("  " + amp.status_payload().decode())
            elif c == "key":
                amp.key(True)
                print("  " + amp.status_payload().decode())
            elif c == "unkey":
                amp.key(False)
            elif c == "model":
                m = parts[1].upper()
                if m in ("13K", "15K", "20K"):
                    amp.model = m
                    amp.bank = "x" if m == "20K" else "A"
            elif c == "oper":
                amp.operate = parts[1] == "1"
            elif c == "power":
                amp.out_w = int(parts[1])
            elif c == "swr":
                amp.swr_atu = float(parts[1])
                amp.swr_ant = amp.swr_atu + 0.15
            elif c == "temp":
                amp.t_up = int(parts[1])
            elif c == "band":
                amp.band = max(0, min(11, int(parts[1])))
                print("  band " + BANDS[amp.band])
            elif c == "level":
                if parts[1].upper() in "LMH":
                    amp.power_lvl = parts[1].upper()
            elif c == "warn":
                amp.warning = parts[1][0]
            elif c == "alarm":
                amp.alarm = parts[1][0]
            else:
                print(HELP)
        except (IndexError, ValueError):
            print("  bad args. " + HELP)


def main():
    ap = argparse.ArgumentParser(
        description="Standalone SPE Expert amplifier simulator for AetherSDR")
    ap.add_argument("--model", default="15K", choices=["13K", "15K", "20K"])
    ap.add_argument("--no-cli", action="store_true")
    args = ap.parse_args()

    amp = Amp(args.model)
    threading.Thread(target=SpeServer(amp).serve, daemon=True).start()
    print(f"[spe] {args.model}-FA sim ready — AE connects (Network mode) to "
          f"<this-host>:{SPE_PORT}; power-ON pulse is out of scope (RFC 2217)",
          flush=True)
    if args.no_cli:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    else:
        cli(amp)


if __name__ == "__main__":
    main()
