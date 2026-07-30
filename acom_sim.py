#!/usr/bin/env python3
# acom_sim.py — standalone ACOM 600S amplifier simulator for AetherSDR.
#
# Desktop port of the Giga R1 quad-sim's ACOM persona, which was bench-proven
# against AE PR #4298 (merged) on 2026-07-18: AE's AcomApplet connected, drew
# live gauges, auto-detected the model tier from SystemConfig, and exercised
# the fault path. Every quirk below was paid for on that bench — port them
# exactly; do not "fix" them:
#   - Frame: 0x55 | addr | len | payload | cksum. len = TOTAL frame length
#     INCLUDING the start byte (sending len-1 made every frame parse one byte
#     short and decodeTelemetry() silently rejected all telemetry).
#     EXCEPTION: the ACK's len byte is 4 for its 5-byte frame — as shipped and
#     accepted by AE's parser; leave it alone.
#   - Checksum: final byte makes the mod-256 sum of the whole frame zero.
#   - All 16-bit telemetry values are LITTLE-ENDIAN (AcomProtocol.cpp le16()).
#   - Temperature raw = KELVIN, not Kelvin x10 (AE subtracts 273).
#   - Fault byte 0xFF = NO FAULT; 0x00 is a REAL code ("Hot switching").
#   - Telemetry (0x2F, 72 bytes) is PUSHED every 250 ms — but only after the
#     host enables it (0x92). 0x91 disables. Ack (0x86) answers everything.
#   - RequestMessage (0x02): one-shot fetch; AE asks for SystemConfig (0x11)
#     at connect for model-tier auto-detect; ErrorCodes (0x21) also served.
#
# Run:   python3 acom_sim.py               # listens on :9600, interactive
#        python3 acom_sim.py --no-cli      # headless
# Then in AE: ACOM applet -> Network -> <this-host>:9600.

import argparse
import socket
import threading
import time

ACOM_PORT = 9600

START = 0x55
ADDR_ACK = 0x86
ADDR_CMD = 0x81
ADDR_TELEM = 0x2F
ADDR_REQUEST = 0x02
ADDR_SYSCFG = 0x11
ADDR_ERRORS = 0x21
TELEM_EN = 0x92
TELEM_DIS = 0x91
CMD_STATUS = 0x01
CMD_MODE = 0x02
CMD_FAULT = 0x08
CMD_BAND = 0x09
MODE_STANDBY = 0x50
MODE_OPER_RX = 0x60
MODE_OPER_TX = 0x70

TELEM_LEN = 72
SYSCFG_LEN = 30
ERRORS_LEN = 23


class Amp:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode_byte = MODE_STANDBY
        self.fwd_power = 0        # W
        self.ref_power = 0        # W
        self.swr = 1.0
        self.inp_power = 0.0      # drive W
        self.temp_c = 25.0
        self.hv_supply = 65.0     # V
        self.pa_current = 0       # mA
        self.fault = 0xFF         # 0xFF = NO FAULT
        self.lpf_ch = 5
        self.fan_pwm = 0
        self.fw_ver = 3
        self.fw_sub = 12
        self.hard_faults = 0
        self.err_words = [0] * 10

    def key(self, on, watts=400, swr=1.15):
        with self.lock:
            if on:
                self.mode_byte = MODE_OPER_TX
                self.fwd_power = watts
                self.ref_power = int(watts * ((swr - 1) / (swr + 1)) ** 2)
                self.swr = swr
                self.inp_power = watts / 20.0
                self.pa_current = watts * 28
                self.fan_pwm = 8
            else:
                self.mode_byte = MODE_OPER_RX
                self.fwd_power = self.ref_power = 0
                self.swr = 1.0
                self.inp_power = 0.0
                self.pa_current = 0
                self.fan_pwm = 0


def finish_checksum(f: bytearray) -> bytes:
    f[-1] = (256 - (sum(f[:-1]) & 0xFF)) & 0xFF
    return bytes(f)


def frame_ack(to_addr: int) -> bytes:
    # len byte is 4 for this 5-byte frame — the one exception; see header.
    f = bytearray([START, ADDR_ACK, 4, to_addr, 0])
    return finish_checksum(f)


def le16(f: bytearray, i: int, v: int):
    v = max(0, min(0xFFFF, int(v)))
    f[i] = v & 0xFF
    f[i + 1] = v >> 8


def frame_telem(a: Amp) -> bytes:
    f = bytearray(TELEM_LEN)
    f[0], f[1], f[2] = START, ADDR_TELEM, TELEM_LEN
    with a.lock:
        f[3] = a.mode_byte
        le16(f, 16, a.temp_c + 273.15)        # KELVIN, not x10
        le16(f, 20, a.inp_power * 10.0)       # W x10
        le16(f, 22, a.fwd_power)              # W
        le16(f, 24, a.ref_power)              # W
        le16(f, 26, a.swr * 100.0)            # x100
        le16(f, 40, a.hv_supply * 10.0)       # V x10
        le16(f, 44, a.pa_current)             # mA
        f[66] = a.fault                       # 0xFF = none
        f[69] = ((a.fan_pwm & 0x0F) << 4) | (a.lpf_ch & 0x0F)
    return finish_checksum(f)


def frame_syscfg(a: Amp) -> bytes:
    f = bytearray(SYSCFG_LEN)
    f[0], f[1], f[2] = START, ADDR_SYSCFG, SYSCFG_LEN
    p = 3
    f[p + 0] = 1                              # amplifierType: 1 = 600S
    f[p + 1] = a.fw_ver
    f[p + 2] = a.fw_sub
    for i, ch in enumerate(b"SIMAMP600S00"):  # serial at payload[13..24]
        f[p + 13 + i] = ch
    f[p + 25] = a.hard_faults
    return finish_checksum(f)


def frame_errors(a: Amp) -> bytes:
    f = bytearray(ERRORS_LEN)
    f[0], f[1], f[2] = START, ADDR_ERRORS, ERRORS_LEN
    for i, w in enumerate(a.err_words):       # 10 le16 words
        le16(f, 3 + i * 2, w)
    return finish_checksum(f)


class AcomServer:
    def __init__(self, amp):
        self.amp = amp
        self.telem_enabled = False

    def handle_packet(self, conn, addr_b, payload):
        conn.sendall(frame_ack(addr_b))       # always ACK first
        if addr_b == TELEM_EN:
            self.telem_enabled = True
            print("[tcp] telemetry ON", flush=True)
        elif addr_b == TELEM_DIS:
            self.telem_enabled = False
            print("[tcp] telemetry OFF", flush=True)
        elif addr_b == ADDR_REQUEST and payload:
            want = payload[0]
            if want == ADDR_SYSCFG:
                conn.sendall(frame_syscfg(self.amp))
            elif want == ADDR_ERRORS:
                conn.sendall(frame_errors(self.amp))
            elif want == ADDR_TELEM:
                conn.sendall(frame_telem(self.amp))
        elif addr_b == ADDR_CMD and payload:
            cmd = payload[0]
            with self.amp.lock:
                if cmd == CMD_MODE and len(payload) >= 2:
                    self.amp.mode_byte = payload[1]
                    print(f"[tcp] mode -> 0x{payload[1]:02X}", flush=True)
                elif cmd == CMD_FAULT:
                    self.amp.fault = 0xFF     # clear: 0xFF = none, NOT 0
                    print("[tcp] fault cleared", flush=True)
                elif cmd == CMD_BAND and len(payload) >= 3:
                    self.amp.lpf_ch = payload[2]
            if cmd == CMD_STATUS:
                conn.sendall(frame_telem(self.amp))

    def handle_client(self, conn, addr):
        print(f"[tcp] client connected {addr}", flush=True)
        self.telem_enabled = False
        buf = b""
        last_telem = 0.0
        try:
            conn.settimeout(0.25)
            while True:
                try:
                    data = conn.recv(1024)
                    if not data:
                        break
                    buf += data
                except socket.timeout:
                    pass
                # Parse frames: 0x55 | addr | len(total) | payload | cksum
                while True:
                    i = buf.find(bytes([START]))
                    if i < 0 or len(buf) < i + 3:
                        break
                    total = buf[i + 2]
                    if total < 4 or total > 80:
                        buf = buf[i + 1:]
                        continue
                    if len(buf) < i + total:
                        break
                    frame = buf[i:i + total]
                    buf = buf[i + total:]
                    if sum(frame) & 0xFF != 0:
                        print("[tcp] bad checksum, dropped", flush=True)
                        continue
                    self.handle_packet(conn, frame[1], frame[3:-1])
                if self.telem_enabled and time.time() - last_telem >= 0.25:
                    conn.sendall(frame_telem(self.amp))
                    last_telem = time.time()
        except Exception as e:
            print(f"[tcp] client error {addr}: {e}", flush=True)
        finally:
            conn.close()
            print(f"[tcp] {addr} disconnected", flush=True)

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", ACOM_PORT))
        srv.listen(5)
        print(f"[tcp] ACOM 600S simulator listening on :{ACOM_PORT}", flush=True)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn, addr),
                             daemon=True).start()


def selftest():
    a = Amp()
    a.temp_c, a.fwd_power, a.swr, a.hv_supply = 54.0, 400, 1.5, 65.0
    a.inp_power, a.pa_current = 20.0, 11200
    t = frame_telem(a)
    ok = True
    def chk(name, cond):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + name)
        ok = ok and cond
    chk("telemetry length byte = 72 (total incl start)", t[2] == 72 and len(t) == 72)
    chk("frame checksum sums to zero", sum(t) & 0xFF == 0)
    chk("temp LE Kelvin (54C -> 327)", t[16] | (t[17] << 8) == 327)
    chk("fwd power LE at 22", t[22] | (t[23] << 8) == 400)
    chk("swr x100 LE at 26", t[26] | (t[27] << 8) == 150)
    chk("hv x10 LE at 40", t[40] | (t[41] << 8) == 650)
    chk("fault byte 0xFF = none", t[66] == 0xFF)
    ack = frame_ack(0x92)
    chk("ack: 5 bytes, len byte 4, checksum zero",
        len(ack) == 5 and ack[2] == 4 and sum(ack) & 0xFF == 0)
    sc = frame_syscfg(a)
    chk("syscfg: 30 bytes, type=600S, checksum zero",
        len(sc) == 30 and sc[3] == 1 and sum(sc) & 0xFF == 0)
    er = frame_errors(a)
    chk("errors: 23 bytes, checksum zero", len(er) == 23 and sum(er) & 0xFF == 0)
    print("SELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def cli(amp):
    HELP = ("commands: oper <0|1> | key | unkey | power <W> | swr <v> | "
            "temp <C> | fault <hex|FF> | band <ch> | status | help | quit")
    print(HELP, flush=True)
    while True:
        try:
            parts = input("acom> ").strip().split()
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
                print(f"  mode=0x{amp.mode_byte:02X} fwd={amp.fwd_power}W "
                      f"swr={amp.swr} temp={amp.temp_c}C fault=0x{amp.fault:02X}")
            elif c == "key":
                amp.key(True)
            elif c == "unkey":
                amp.key(False)
            elif c == "oper":
                amp.mode_byte = MODE_OPER_RX if parts[1] == "1" else MODE_STANDBY
            elif c == "power":
                amp.fwd_power = int(parts[1])
            elif c == "swr":
                amp.swr = float(parts[1])
            elif c == "temp":
                amp.temp_c = float(parts[1])
            elif c == "fault":
                amp.fault = int(parts[1], 16)
            elif c == "band":
                amp.lpf_ch = int(parts[1])
            else:
                print(HELP)
        except (IndexError, ValueError):
            print("  bad args. " + HELP)


def main():
    ap = argparse.ArgumentParser(
        description="Standalone ACOM 600S simulator for AetherSDR (#4298)")
    ap.add_argument("--no-cli", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    amp = Amp()
    threading.Thread(target=AcomServer(amp).serve, daemon=True).start()
    print(f"[acom] 600S sim ready — AE connects to <this-host>:{ACOM_PORT}",
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
