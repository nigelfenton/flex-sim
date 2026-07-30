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
#   - Power-ON is an RFC 2217 control-line pulse, not amp protocol. It is
#     IN scope here: a ser2net proxy is exactly what AE talks to in Network
#     mode, so this sim emulates the PROXY as well as the amp. --port-mode
#     picks which ser2net accepter you are pretending to be, because the
#     three behave very differently and only one of them works:
#       rfc2217  `telnet(rfc2217=true)` — negotiates, honours SET-CONTROL,
#                and the DTR/RTS pulse really does switch the amp on.
#       telnet   plain `telnet,<port>` — a telnet layer that REFUSES
#                COM-PORT-OPTION (replies DONT). Control lines never move.
#       raw      `tcp,<port>` — no telnet layer at all, so AE's IAC frames
#                are forwarded into the amp's UART as payload and land in
#                this parser as junk. Logged, not silently swallowed.
#     Start the amp switched OFF (--power off) to make the pulse mean
#     something: a real Expert answers no polls until it is powered up, and
#     with ser2net the TCP link stays up throughout, which is the whole
#     reason AE needs poll-silence detection separate from disconnect.
#
# Run:   python3 spe_sim.py                       # :4531, interactive, amp ON
#        python3 spe_sim.py --no-cli              # headless
#        python3 spe_sim.py --power off           # amp off; press ON in AE
#        python3 spe_sim.py --port-mode telnet    # reproduce a non-2217 proxy
#        python3 spe_sim.py --port-mode raw       # reproduce a raw accepter
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

# RFC 2217 / telnet. Values match AE's SpeProtocol.h Rfc2217 block exactly;
# a mismatch here would look identical to "the proxy ignored us", which is
# the very failure this sim exists to tell apart.
IAC = 0xFF
SE = 0xF0
SB = 0xFA
WILL, WONT, DO, DONT = 0xFB, 0xFC, 0xFD, 0xFE
COM_PORT_OPTION = 0x2C
SET_CONTROL = 0x05
# Server->client replies live at command + 100 (RFC 2217 §2), which is what
# a real ser2net emits after acting on SET-CONTROL. AE parses none of it --
# sending it anyway is deliberate: it proves AE's FrameParser resyncs past
# non-frame bytes instead of mis-framing them.
SET_CONTROL_RESP = SET_CONTROL + 100

# SET-CONTROL values AE sends. Anything else is a control request we do not
# model (flow control, break) and is logged rather than guessed at.
CONTROL_LINES = {0x08: ("DTR", True), 0x09: ("DTR", False),
                 0x0B: ("RTS", True), 0x0C: ("RTS", False)}

# A power pulse has to be a deliberate hold, not a transient. AE holds RTS
# for 1000 ms; anything past a few hundred ms is unambiguously the pulse and
# not line noise or an idle-state write.
PULSE_MIN_S = 0.3

BANDS = ["160m", "80m", "60m", "40m", "30m", "20m",
         "17m", "15m", "12m", "10m", "6m", "4m"]


class Amp:
    """Amp-side state. Model 13K / 15K / 20K (2K-FA always reports bank x)."""

    def __init__(self, model="15K", powered=True):
        self.lock = threading.Lock()
        self.model = model
        # A switched-off Expert is not a disconnected one: with ser2net the
        # socket stays up and the amp simply stops answering. Everything
        # below still holds its last state so power-cycling is realistic.
        self.powered = powered
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
    def __init__(self, amp, port_mode="rfc2217", port=SPE_PORT):
        self.amp = amp
        self.port_mode = port_mode
        self.port = port
        self.lines = {"DTR": False, "RTS": False}
        self.negotiated = False   # did the client ask for COM-PORT-OPTION?
        self._rts_since = None

    # ── the proxy half ──────────────────────────────────────────────────

    def split_telnet(self, data, conn):
        """Peel IAC sequences off the stream; return (amp_bytes, tail).

        `tail` is an incomplete sequence straddling a read boundary -- it
        MUST be carried to the next read rather than dropped, or a pulse
        that happens to split across two TCP segments is silently lost,
        which would look exactly like the bug being investigated.
        """
        clean = bytearray()
        i, n = 0, len(data)
        while i < n:
            if data[i] != IAC:
                clean.append(data[i]); i += 1; continue
            if i + 1 >= n:
                return bytes(clean), data[i:]
            cmd = data[i + 1]
            if cmd == IAC:                     # escaped literal 0xFF
                clean.append(IAC); i += 2
            elif cmd in (WILL, WONT, DO, DONT):
                if i + 2 >= n:
                    return bytes(clean), data[i:]
                self.on_negotiate(cmd, data[i + 2], conn)
                i += 3
            elif cmd == SB:
                end = data.find(bytes([IAC, SE]), i + 2)
                if end < 0:
                    return bytes(clean), data[i:]
                self.on_subneg(data[i + 2:end], conn)
                i = end + 2
            else:
                i += 2                          # 2-byte command we ignore
        return bytes(clean), b""

    def on_negotiate(self, cmd, opt, conn):
        if opt != COM_PORT_OPTION or cmd != WILL:
            return
        if self.port_mode == "rfc2217":
            self.negotiated = True
            conn.sendall(bytes([IAC, DO, COM_PORT_OPTION]))
            print("[2217] client WILL COM-PORT-OPTION -> replied DO "
                  "(accepted; control lines are live)", flush=True)
        else:
            conn.sendall(bytes([IAC, DONT, COM_PORT_OPTION]))
            print("[2217] client WILL COM-PORT-OPTION -> replied DONT. This "
                  "port is `telnet` WITHOUT rfc2217=true, so SET-CONTROL is "
                  "refused and DTR/RTS will NOT move. AE does not read this "
                  "reply, so from its side the pulse will appear to succeed.",
                  flush=True)

    def on_subneg(self, payload, conn):
        if len(payload) < 3 or payload[0] != COM_PORT_OPTION:
            return
        if payload[1] != SET_CONTROL:
            print(f"[2217] sub-negotiation {payload[1]} ignored (not "
                  f"SET-CONTROL)", flush=True)
            return
        ctrl = payload[2]
        if self.port_mode != "rfc2217":
            print(f"[2217] SET-CONTROL {ctrl:#04x} DISCARDED -- option was "
                  f"refused. Nothing moves; the amp will not switch on.",
                  flush=True)
            return
        if ctrl not in CONTROL_LINES:
            print(f"[2217] SET-CONTROL {ctrl:#04x} not a DTR/RTS value "
                  f"-- not modelled", flush=True)
            return
        name, state = CONTROL_LINES[ctrl]
        self.set_line(name, state)
        # What a real ser2net does next. AE ignores it; see SET_CONTROL_RESP.
        conn.sendall(bytes([IAC, SB, COM_PORT_OPTION, SET_CONTROL_RESP,
                            ctrl, IAC, SE]))

    def set_line(self, name, state):
        prev = self.lines[name]
        self.lines[name] = state
        if prev != state:
            print(f"[line] {name} {'HIGH' if state else 'LOW '}   "
                  f"(DTR={int(self.lines['DTR'])} RTS={int(self.lines['RTS'])})",
                  flush=True)
        # RTS is the power-switch line: a HIGH held long enough, then
        # released, is the press. Edge-triggered on the RELEASE so the
        # measured width can be reported -- a pulse that is too short is a
        # far more useful message than silence.
        if name == "RTS":
            if state:
                self._rts_since = time.monotonic()
            elif self._rts_since is not None:
                width = time.monotonic() - self._rts_since
                self._rts_since = None
                if width >= PULSE_MIN_S:
                    self.power_pulse(width)
                else:
                    print(f"[pwr ] RTS pulse {width*1000:.0f} ms -- shorter "
                          f"than {PULSE_MIN_S*1000:.0f} ms, ignored as a "
                          f"transient", flush=True)

    def power_pulse(self, width):
        with self.amp.lock:
            was = self.amp.powered
            self.amp.powered = True
        outcome = ("was already on" if was
                   else "is now ON and will answer polls")
        print(f"[pwr ] power-ON pulse accepted ({width*1000:.0f} ms on RTS) "
              f"-- amp {outcome}", flush=True)
        # Finding worth surfacing on the bench rather than in review: the
        # sequence's last step drives DTR HIGH and leaves it there. On this
        # amp that line is the power switch, so its resting level is not a
        # cosmetic detail. Report it, take no view on which level is right.
        print(f"[line] resting state after the pulse: "
              f"DTR={int(self.lines['DTR'])} RTS={int(self.lines['RTS'])}",
              flush=True)

    def handle_client(self, conn, addr):
        print(f"[tcp] client connected {addr}  (port-mode={self.port_mode}, "
              f"amp {'ON' if self.amp.powered else 'OFF'})", flush=True)
        buf = b""
        pending = b""      # incomplete IAC sequence carried across reads
        try:
            conn.settimeout(60)
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                if self.port_mode == "raw":
                    # No telnet layer: everything, IAC included, is handed
                    # to the amp's UART verbatim. That is the whole failure
                    # mode -- see the junk report in the resync path below.
                    buf += data
                else:
                    clean, pending = self.split_telnet(pending + data, conn)
                    buf += clean
                while True:
                    i = buf.find(b"\x55\x55\x55")
                    if i < 0 or len(buf) < i + 5:
                        if i > 0 or (i < 0 and buf):
                            self.report_junk(buf if i < 0 else buf[:i])
                            buf = b"" if i < 0 else buf[i:]
                        break
                    if i > 0:
                        self.report_junk(buf[:i])
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
                    if not self.amp.powered:
                        # A switched-off amp is deaf, not disconnected. The
                        # socket stays open on purpose: that is what makes
                        # AE's respondingChanged path testable at all.
                        continue
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

    def report_junk(self, junk):
        """Say what was thrown away, and why it is probably there.

        Silence here is how a raw-mode port hides: AE's SET-CONTROL frames
        vanish into the amp's byte stream, the amp does nothing, and AE
        still reports the pulse complete. Naming the bytes turns that into
        a diagnosis instead of a mystery.
        """
        if not junk:
            return
        if bytes([IAC, SB, COM_PORT_OPTION]) in junk or \
           bytes([IAC, WILL, COM_PORT_OPTION]) in junk:
            print(f"[junk] discarded {len(junk)} B that decode as RFC 2217 "
                  f"COM-PORT-OPTION frames: {junk.hex()}\n"
                  f"       This port is RAW, so the proxy forwarded them to "
                  f"the amp as payload instead of acting on them. The amp "
                  f"cannot switch on this way -- and AE is not told.",
                  flush=True)
        else:
            print(f"[junk] discarded {len(junk)} B before sync: {junk.hex()}",
                  flush=True)

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        print(f"[tcp] SPE simulator listening on :{self.port} (poll-only, "
              f"no greeting by design), port-mode={self.port_mode}",
              flush=True)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn, addr),
                             daemon=True).start()


def cli(amp, server):
    HELP = ("commands: model <13K|15K|20K> | oper <0|1> | key | unkey | "
            "power <W> | swr <v> | temp <C> | band <0-11> | level <L|M|H> | "
            "warn <ch> | alarm <ch> | status | help | quit\n"
            "          switch <on|off>  -- amp power (off = deaf to polls, "
            "link stays up)\n"
            "          lines            -- DTR/RTS state + power\n"
            "          mode [rfc2217|telnet|raw]  -- ser2net personality")
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
                print("  " + amp.status_payload().decode()
                      + ("" if amp.powered else "   [amp is OFF -- not sent]"))
            elif c == "lines":
                print("  DTR=%d RTS=%d   amp=%s   port-mode=%s   "
                      "com-port-option=%s"
                      % (server.lines["DTR"], server.lines["RTS"],
                         "ON" if amp.powered else "OFF", server.port_mode,
                         "accepted" if server.negotiated else "not negotiated"))
            elif c == "switch":
                amp.powered = parts[1].lower() in ("on", "1", "true")
                print("  amp %s" % ("ON" if amp.powered else
                                    "OFF (deaf to polls; link stays up)"))
            elif c == "mode":
                if len(parts) > 1:
                    if parts[1] in ("rfc2217", "telnet", "raw"):
                        server.port_mode = parts[1]
                        server.negotiated = False
                        print("  port-mode %s (reconnect AE to renegotiate)"
                              % parts[1])
                    else:
                        print("  modes: rfc2217 | telnet | raw")
                else:
                    print("  port-mode %s" % server.port_mode)
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
    ap.add_argument("--port", type=int, default=SPE_PORT)
    ap.add_argument("--port-mode", default="rfc2217",
                    choices=["rfc2217", "telnet", "raw"],
                    help="which ser2net accepter to impersonate: rfc2217 "
                         "= telnet(rfc2217=true) and the power-ON pulse "
                         "works; telnet = COM-PORT-OPTION refused; raw = "
                         "tcp accepter, IAC frames reach the amp as payload")
    ap.add_argument("--power", default="on", choices=["on", "off"],
                    help="initial amp power. `off` is the interesting one: "
                         "the amp answers nothing until a power-ON pulse "
                         "arrives, while the TCP link stays up throughout")
    args = ap.parse_args()

    amp = Amp(args.model, powered=(args.power == "on"))
    server = SpeServer(amp, port_mode=args.port_mode, port=args.port)
    threading.Thread(target=server.serve, daemon=True).start()
    state = ("ON" if amp.powered else
             "OFF -- press ON in the applet to send the RFC 2217 pulse")
    print(f"[spe] {args.model}-FA sim ready — AE connects (Network mode) to "
          f"<this-host>:{args.port}, port-mode={args.port_mode}. "
          f"Amp is {state}.", flush=True)
    if args.no_cli:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    else:
        cli(amp, server)


if __name__ == "__main__":
    main()
