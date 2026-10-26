#!/usr/bin/env python3
# lp100_sim.py - TelePost LP-100A digital vector RF wattmeter simulator for AetherSDR.
#
# AetherSDR supports the LP-100A as a station peripheral (an RS-232 instrument, reached over a
# COM port or a raw TCP serial proxy such as ser2net). This sim is the raw-TCP side of that: it
# answers on a TCP port exactly as a meter behind ser2net does, so AetherSDR's LP-100A applet
# can be driven - gauges, range tracking, the coherence check, the shared-wire poll gate - with
# no meter, no serial adapter and no virtual COM port.
#
# SOURCES, AND WHERE THEY DISAGREE
#   - TelePost's "LP-100A Digital Vector RF Wattmeter Operations Manual", pp. 20-21: the command
#     set and the field order.
#   - AetherSDR's design note docs/architecture/lp-100a-wattmeter-design.md and
#     src/core/LpMeterProtocol.h, which measured a real meter (954 captured records) and
#     correct the manual where the wire disagrees. Where they differ, THE WIRE WINS here too:
#       * the body is 42 characters, not the manual's 41 (Z is 5 wide, "046.3");
#       * there are six alarm values (A walks 0..5) and three peak-hold modes (F walks 0..2).
#
# WHAT IS MODELLED
#   - The meter never pushes. Each 'P' gets one record: ';' LEADS it, there is no CR, LF or any
#     terminator, and the fields are fixed width:
#         ;PPPP.PP,ZZZ.Z,PPP.P,A,CALLSN,R,H,dBm,SWR      e.g. ;0100.00,075.0,000.0,0,SIM   ,1,0,50.0,1.50
#     Power 7 (zero-padded W), |Z| 5 (ohms), |phase| 5 (degrees, MAGNITUDE only - the meter never
#     sends the sign of the reactance), alarm 1, callsign 6 (space-padded), range 1 (0 High,
#     1 Mid, 2 Low), peak-hold 1 (0 Avg, 1 Peak, 2 Fast), dBm (signed, NOT zero-padded), SWR 4.
#   - Physics that agrees with itself. Under drive, |Z| and phase come from the configured load
#     and the SWR is computed from the ROUNDED |Z| and phase that go on the wire, so a client
#     that cross-checks them (AetherSDR's Reading::coherent does) sees what a real meter gives:
#     agreement to a few thousandths.
#   - Idle as captured: 0 W, |Z| ~46 ohm, phase ~88 degrees, SWR 1.00, dBm about -2.3 - which is
#     NOT physically consistent, because with no RF the bridge has nothing to measure.
#   - Key-up hold: in Peak (and, briefly, Avg) the meter keeps showing power and dBm for a while
#     after RF stops, while |Z|, phase and SWR fall straight back to idle. Those records are
#     incoherent on purpose; that is what AetherSDR's gauge dimming exists for.
#   - Auto-ranging: the range field follows power against three ceilings. The meter never sends
#     the ceilings themselves (they are set on the meter), so they are flags here.
#   - The single-byte commands: P (poll), A (alarm set point +1, wraps 5->0), F (Avg/Peak/Fast,
#     wraps), M (see below). None is acknowledged; the next record shows the change.
#   - A SHARED wire. A ser2net port mirrors one client's replies to every client, so a client
#     that never polls still receives records. Every reply here goes to every connected client.
#   - Faults, one-shot unless noted, to exercise a client's input checking:
#       banner    a ser2net-style connect banner (text, no ';') before the first record (sticky)
#       garbage   junk bytes before the next record
#       exponent  next record's power field is '01e+100' - seven characters, the canonical width
#       flip      next record has one digit changed - still well-formed; no checksum can catch it
#       wide-dbm  next record's dBm is 5 characters (-12.3), a 43-character body
#       truncate  next record is cut short
#       stall     stop answering polls while the TCP link stays up (sticky; 'stall off')
#       lag       every N s, hold one reply for M ms so the next poll queues behind it and both
#                 answers leave ~20 ms apart (sticky; 'lag off'). Measured on a real station in
#                 AetherSDR #6794: every ~8.0 s, a ~190 ms gap, then two records 20 ms apart.
#
# WHAT IS NOT MODELLED (unknown, or not needed by AetherSDR v1)
#   - 'M'. The manual calls it "mode / power range" and AetherSDR v1 never sends it; what it
#     does to the records is unverified, so it is counted and logged and changes nothing.
#   - Whether field 0 is FORWARD or NET power is unverified on hardware. Forward by default;
#     --net-power reports forward x (1 - |gamma|^2) instead, so a client can be tested both ways.
#   - The Avg-mode hold time (1.0 s here) and the SWR format above 9.99 ("10.0", one decimal to
#     keep 4 characters) are assumptions. The Peak hold (1.7 s) is from the design note.
#   - The meter's other display screens; this is the Watts screen throughout.
#   - Old firmware (38400 / 19200 baud, no dBm or SWR): not simulated - AetherSDR rejects it.
#
# Pure standard library, Python 3.8+.

import argparse
import cmath
import json
import math
import random
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METER_PORT = 2000      # a common ser2net raw port; AetherSDR's own example uses it
CTL_PORT = 8738        # control page / JSON (KPA uses 8737)
Z0 = 50.0

ALARM_NAMES = ["Off", "1.5", "2.0", "2.5", "3.0", "User"]
HOLD_NAMES = ["Avg", "Peak", "Fast"]
RANGE_NAMES = ["High", "Mid", "Low"]
HOLD_S = {0: 1.0, 1: 1.7, 2: 0.0}      # Avg is an assumption; Peak from the design note

BANNER = (b"\r\nser2net port 2000 device serial,/dev/ttyUSB0 [115200N81,CLOCAL,NOBREAK]"
          b" (Debian GNU/Linux)\r\n\r\nWelcome to the shack wattmeter - LP-100A on ttyUSB0."
          b" Raw TCP, data follows without framing.\r\nPlease do not hold the line open"
          b" longer than you need it.\r\n\r\n")
assert b";" not in BANNER, "a ';' in the banner would read as a record marker"

FAULTS_ONE_SHOT = ("garbage", "exponent", "flip", "wide-dbm", "truncate")


def log(*a):
    print("[lp100]", *a, flush=True)


def swr_from_z(z_ohms, phase_deg):
    """SWR of |Z| at phase (degrees) against 50 ohms - the same relation the client checks."""
    if z_ohms <= 0:
        return 99.99
    z = cmath.rect(z_ohms, math.radians(phase_deg))
    g = abs((z - Z0) / (z + Z0))
    if g >= 1.0:
        return 99.99
    return (1 + g) / (1 - g)


def gamma_mag(z_ohms, phase_deg):
    z = cmath.rect(z_ohms, math.radians(phase_deg))
    return abs((z - Z0) / (z + Z0))


def fmt_swr(s):
    s = max(1.0, min(s, 99.9))
    return f"{s:.2f}" if s < 9.995 else f"{s:.1f}"


def fmt_callsign(call):
    c = "".join(ch for ch in call.upper() if ch.isalnum() or ch in "/-")[:6]
    return c.ljust(6)


class Meter:
    def __init__(self, callsign="SIM", ceilings=(700.0, 125.0, 25.0), net_power=False, seed=None):
        self.lock = threading.Lock()
        self.rng = random.Random(seed)
        self.callsign = fmt_callsign(callsign)
        self.ceilings = tuple(float(c) for c in ceilings)   # High, Mid, Low
        self.net_power = net_power
        self.fwd_w = 0.0            # forward power while keyed
        self.keyed = False
        self.load_z = 50.0          # load |Z| (ohms) and phase (degrees, signed allowed)
        self.load_ph = 0.0
        self.alarm = 0
        self.hold = 0               # 0 Avg, 1 Peak, 2 Fast
        self.m_count = 0
        self.held_w = 0.0           # power shown during the key-up hold
        self.hold_until = 0.0
        self.faults = set()         # one-shot faults pending
        self.stalled = False
        self.banner = False
        # Late replies, as measured on a real station (AetherSDR #6794): every lag_period_s the
        # meter holds one reply for lag_delay_ms, so the next poll queues behind it and both
        # answers leave ~20 ms apart. 0 = off.
        self.lag_period_s = 0.0
        self.lag_delay_ms = 0.0
        self.next_lag = 0.0
        self.lag_events = 0
        self.polls = 0
        self.replies = 0

    # ---- operator / control side -------------------------------------------------
    def key(self, watts):
        with self.lock:
            self.fwd_w = max(0.0, min(float(watts), 9999.99))
            self.keyed = self.fwd_w > 0
            self.hold_until = 0.0

    def unkey(self):
        with self.lock:
            if self.keyed:
                self.held_w = self._reported_power_locked()
                self.hold_until = time.monotonic() + HOLD_S.get(self.hold, 0.0)
            self.keyed = False

    def set_load(self, z_ohms, phase_deg):
        with self.lock:
            self.load_z = max(0.1, min(float(z_ohms), 999.9))
            self.load_ph = max(-90.0, min(float(phase_deg), 90.0))

    # ---- meter commands (from the wire) ------------------------------------------
    def wire_command(self, ch):
        with self.lock:
            if ch == "A":
                self.alarm = (self.alarm + 1) % 6
            elif ch == "F":
                self.hold = (self.hold + 1) % 3
            elif ch == "M":
                self.m_count += 1
        log(f"cmd {ch!r} -> alarm {ALARM_NAMES[self.alarm]}, hold {HOLD_NAMES[self.hold]}, M x{self.m_count}")

    # ---- the record ------------------------------------------------------------------
    def _reported_power_locked(self):
        p = self.fwd_w * (1 + self.rng.uniform(-0.003, 0.003))
        if self.net_power:
            p *= 1 - gamma_mag(self.load_z, self.load_ph) ** 2
        return max(0.0, min(p, 9999.99))

    def _range_for(self, watts):
        high, mid, low = self.ceilings
        if watts <= low:
            return 2
        if watts <= mid:
            return 1
        return 0

    def record(self):
        """One record body plus the leading ';' (bytes), and the fault applied (or None)."""
        with self.lock:
            now = time.monotonic()
            if self.keyed:
                power = self._reported_power_locked()
                z = round(self.load_z * (1 + self.rng.uniform(-0.002, 0.002)), 1)
                ph = round(min(abs(self.load_ph) + self.rng.uniform(-0.1, 0.1), 90.0), 1)
                # A magnitude: never negative, and never NEGATIVE ZERO either - max(-0.0, 0.0) keeps
                # -0.0, which formats as "-00.0", a sign on a field that must not carry one.
                ph = max(ph, 0.0) + 0.0
                swr = swr_from_z(z, ph)
            else:
                power = self.held_w if now < self.hold_until else 0.0
                z = round(46.3 + self.rng.uniform(-0.2, 0.2), 1)
                ph = round(88.5 + self.rng.uniform(-0.3, 0.3), 1)
                swr = 1.0
            if power >= 0.005:
                dbm = 10 * math.log10(power * 1000.0)
            else:
                dbm = -2.3 + self.rng.uniform(-0.1, 0.1)
            fields = [
                f"{power:07.2f}",
                f"{z:05.1f}",
                f"{ph:05.1f}",
                str(self.alarm),
                self.callsign,
                str(self._range_for(power)),
                str(self.hold),
                f"{dbm:.1f}",
                fmt_swr(swr),
            ]
            fault = None
            for f in FAULTS_ONE_SHOT:
                if f in self.faults:
                    self.faults.discard(f)
                    fault = f
                    break
            self.replies += 1
        body = ",".join(fields)
        prefix = b""
        if fault == "exponent":
            fields[0] = "01e+100"
            body = ",".join(fields)
        elif fault == "wide-dbm":
            fields[7] = "-12.3"
            body = ",".join(fields)
        elif fault == "flip":
            digits = [i for i, ch in enumerate(body) if ch.isdigit()]
            i = self.rng.choice(digits)
            body = body[:i] + str((int(body[i]) + 1 + self.rng.randrange(8)) % 10) + body[i + 1:]
        elif fault == "truncate":
            body = body[: len(body) // 2]
        elif fault == "garbage":
            prefix = bytes(self.rng.randrange(32, 127) for _ in range(23)).replace(b";", b"#")
        return prefix + b";" + body.encode("ascii"), fault

    def state(self):
        with self.lock:
            return {
                "keyed": self.keyed, "forward_w": self.fwd_w,
                "load_ohms": self.load_z, "load_phase_deg": self.load_ph,
                "load_swr": round(swr_from_z(self.load_z, abs(self.load_ph)), 3),
                "alarm": ALARM_NAMES[self.alarm], "peak_hold": HOLD_NAMES[self.hold],
                "m_presses": self.m_count, "callsign": self.callsign.strip(),
                "ceilings_w": {"high": self.ceilings[0], "mid": self.ceilings[1], "low": self.ceilings[2]},
                "power_field": "net" if self.net_power else "forward",
                "stalled": self.stalled, "banner": self.banner,
                "lag": ({"period_s": self.lag_period_s, "delay_ms": self.lag_delay_ms, "events": self.lag_events}
                        if self.lag_period_s else None),
                "pending_faults": sorted(self.faults),
                "polls": self.polls, "replies": self.replies,
            }


class MeterServer:
    """Raw TCP, like a ser2net 'raw' port: bytes in, bytes out, every reply to every client."""

    def __init__(self, meter, port=METER_PORT, host="0.0.0.0", latency_ms=8.0):
        self.meter = meter
        self.latency = max(0.0, latency_ms) / 1000.0
        self.clients = []
        self.clients_lock = threading.Lock()
        self.reply_lock = threading.Lock()   # one reply on the wire at a time, as on a UART
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    def start(self):
        threading.Thread(target=self._accept, daemon=True).start()
        log(f"LP-100A on raw TCP :{self.port} (ser2net-style; every reply goes to every client)")
        return self

    def _accept(self):
        while True:
            try:
                c, addr = self.sock.accept()
            except OSError:
                return
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.clients_lock:
                self.clients.append(c)
            log(f"client {addr[0]}:{addr[1]} connected ({len(self.clients)} on the wire)")
            if self.meter.banner:
                try:
                    c.sendall(BANNER)
                except OSError:
                    pass
            threading.Thread(target=self._client, args=(c, addr), daemon=True).start()

    def _drop(self, c):
        with self.clients_lock:
            if c in self.clients:
                self.clients.remove(c)
        try:
            c.close()
        except OSError:
            pass

    def _broadcast(self, data):
        with self.clients_lock:
            targets = list(self.clients)
        for c in targets:
            try:
                c.sendall(data)
            except OSError:
                self._drop(c)

    def _reply_delay(self):
        """Seconds to wait before this reply: normal latency, a lag event, or the 20 ms pair gap."""
        m = self.meter
        now = time.monotonic()
        if m.lag_period_s > 0 and now >= m.next_lag:
            m.next_lag = now + m.lag_period_s
            m.lag_events += 1
            self._pair_next = True
            return m.lag_delay_ms / 1000.0
        if getattr(self, "_pair_next", False):
            self._pair_next = False
            return 0.020                 # the queued poll's answer follows ~20 ms behind
        return self.latency

    def _client(self, c, addr):
        try:
            while True:
                data = c.recv(256)
                if not data:
                    break
                for byte in data:
                    ch = chr(byte)
                    if ch == "P":
                        self.meter.polls += 1
                        if self.meter.stalled:
                            continue
                        with self.reply_lock:
                            delay = self._reply_delay()
                            if delay:
                                time.sleep(delay)
                            rec, fault = self.meter.record()
                            self._broadcast(rec)
                        if fault:
                            log(f"fault {fault!r} applied: {rec!r}")
                    elif ch in "AFM":
                        self.meter.wire_command(ch)
                    # anything else: ignored, as the meter does
        except OSError:
            pass
        self._drop(c)
        log(f"client {addr[0]}:{addr[1]} disconnected")


HELP = """commands:
  key <watts>            key up at this forward power      unkey
  load <ohms> [deg]      load |Z| and phase (default 0)    swr <ratio>   resistive load at that SWR
  hold avg|peak|fast     peak-hold mode                     alarm <0-5>   SWR alarm set point
  fault <name>           one-shot: garbage exponent flip wide-dbm truncate
  stall on|off           stop/start answering polls (link stays up)
  lag on [s] [ms]|off    every s seconds hold one reply ms, the next 20 ms behind it (default 8 s, 100 ms)
  banner on|off          ser2net-style banner on each new connection
  status                 show the state                     quit"""


def command(meter, srv, line):
    p = line.split()
    if not p:
        return ""
    c, a = p[0].lower(), p[1:]
    try:
        if c == "key" and a:
            meter.key(float(a[0]))
            return f"keyed at {meter.fwd_w:.1f} W into {meter.load_z:.1f} ohm / {meter.load_ph:.1f} deg"
        if c == "unkey":
            meter.unkey()
            return "unkeyed"
        if c == "load" and a:
            meter.set_load(float(a[0]), float(a[1]) if len(a) > 1 else 0.0)
            return f"load {meter.load_z:.1f} ohm / {meter.load_ph:.1f} deg (SWR {swr_from_z(meter.load_z, abs(meter.load_ph)):.2f})"
        if c == "swr" and a:
            s = max(1.0, float(a[0]))
            meter.set_load(Z0 * s, 0.0)
            return f"load {meter.load_z:.1f} ohm resistive (SWR {s:.2f})"
        if c == "hold" and a and a[0].lower() in ("avg", "peak", "fast"):
            with meter.lock:
                meter.hold = ("avg", "peak", "fast").index(a[0].lower())
            return f"peak-hold {HOLD_NAMES[meter.hold]}"
        if c == "alarm" and a and a[0].isdigit() and 0 <= int(a[0]) <= 5:
            with meter.lock:
                meter.alarm = int(a[0])
            return f"alarm {ALARM_NAMES[meter.alarm]}"
        if c == "fault" and a and a[0] in FAULTS_ONE_SHOT:
            with meter.lock:
                meter.faults.add(a[0])
            return f"fault {a[0]} armed for the next reply"
        if c == "stall" and a and a[0] in ("on", "off"):
            meter.stalled = a[0] == "on"
            return f"stall {a[0]}"
        if c == "lag" and a and a[0] in ("on", "off"):
            if a[0] == "off":
                meter.lag_period_s = 0.0
                return "lag off"
            period = float(a[1]) if len(a) > 1 else 8.0
            delay = float(a[2]) if len(a) > 2 else 100.0
            if not (0.2 <= period <= 600 and 0 < delay <= 5000):
                return "lag on [period 0.2..600 s] [delay 1..5000 ms]"
            meter.lag_period_s, meter.lag_delay_ms = period, delay
            meter.next_lag = time.monotonic() + period
            return f"lag on: every {period:g} s one reply is held {delay:g} ms, and the next follows 20 ms after it"
        if c == "banner" and a and a[0] in ("on", "off"):
            meter.banner = a[0] == "on"
            return f"banner {a[0]} (new connections)"
        if c == "status":
            return json.dumps(meter.state(), indent=1)
        if c in ("help", "?"):
            return HELP
    except ValueError as e:
        return f"bad value: {e}"
    return "unknown command; 'help' lists them"


def start_control_server(meter, srv, port=CTL_PORT, host="0.0.0.0"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/state":
                self._send(json.dumps(meter.state()))
            elif u.path == "/cmd":
                out = command(meter, srv, q.get("c", [""])[0])
                self._send(json.dumps({"out": out, "state": meter.state()}))
            elif u.path == "/":
                self._send("<pre>LP-100A sim\n\nGET /state\nGET /cmd?c=key%20100\n\n" + HELP + "</pre>",
                           "text/html; charset=utf-8")
            else:
                self.send_error(404)

    httpd = ThreadingHTTPServer((host, port), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"control on http://<this-host>:{httpd.server_address[1]}/  (/state, /cmd?c=...)")
    return httpd


def selftest():
    m = Meter(seed=1)
    ok = True

    def chk(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))

    rec, _ = m.record()
    chk("idle record is ';' + 42 characters", rec[:1] == b";" and len(rec) == 43, rec)
    m.set_load(75.0, 0.0)
    m.key(100.0)
    body = m.record()[0][1:].decode()
    f = body.split(",")
    chk("comma offsets match the measured layout", [i for i, ch in enumerate(body) if ch == ","]
        == [7, 13, 19, 21, 28, 30, 32, 37], body)
    chk("75 ohm resistive reads SWR 1.50", f[8] == "1.50", f[8])
    chk("100 W is the Mid range with 700/125/25 ceilings", f[5] == "1", f[5])
    chk("dBm is 10 log10(mW)", abs(float(f[7]) - 50.0) < 0.1, f[7])
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="TelePost LP-100A wattmeter simulator (raw TCP, ser2net-style)")
    ap.add_argument("--port", type=int, default=METER_PORT, help="raw TCP meter port (default 2000)")
    ap.add_argument("--ctl-port", type=int, default=CTL_PORT, help="control page / JSON port (0 = off)")
    ap.add_argument("--host", default="0.0.0.0", help="interface to bind")
    ap.add_argument("--callsign", default="SIM", help="callsign field (6 characters, space-padded)")
    ap.add_argument("--ceilings", default="700,125,25",
                    help="High,Mid,Low range ceilings in W (set on a real meter, never sent)")
    ap.add_argument("--net-power", action="store_true",
                    help="report net power (forward x (1-|gamma|^2)) instead of forward")
    ap.add_argument("--latency-ms", type=float, default=8.0, help="delay before each reply")
    ap.add_argument("--banner", action="store_true", help="send a ser2net-style banner on connect")
    ap.add_argument("--lag", default="", metavar="SECONDS,MS",
                    help="late replies from start: every SECONDS hold one reply MS (e.g. 8,100)")
    ap.add_argument("--seed", type=int, default=None, help="random seed for the measurement jitter")
    ap.add_argument("--no-cli", action="store_true", help="headless")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    try:
        ceilings = tuple(float(x) for x in args.ceilings.split(","))
        assert len(ceilings) == 3 and ceilings[0] > ceilings[1] > ceilings[2] > 0
    except (ValueError, AssertionError):
        ap.error("--ceilings wants three falling positive numbers, e.g. 700,125,25")
    if not (0 <= args.latency_ms <= 5000):
        ap.error("--latency-ms must be 0..5000")
    meter = Meter(args.callsign, ceilings, args.net_power, args.seed)
    meter.banner = args.banner
    srv = MeterServer(meter, args.port, args.host, args.latency_ms).start()
    if args.lag:
        try:
            period_s, delay_ms = (float(x) for x in args.lag.split(","))
        except ValueError:
            ap.error("--lag wants SECONDS,MS, e.g. 8,100")
        out = command(meter, srv, f"lag on {period_s} {delay_ms}")
        if not out.startswith("lag on:"):
            ap.error(f"--lag: {out}")
        log(out)
    if args.ctl_port:
        start_control_server(meter, srv, args.ctl_port, args.host)
    log("ready")
    if args.no_cli:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return
    print(HELP, flush=True)
    while True:
        try:
            line = input("lp100> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line.lower() in ("quit", "exit"):
            return
        out = command(meter, srv, line)
        if out:
            print(out)


if __name__ == "__main__":
    main()
