#!/usr/bin/env python3
# pgxl_sim.py — standalone 4O3A Power Genius XL (amplifier) simulator for AetherSDR.
#
# Path-A spike (2026-07-07). Sibling of ag_sim.py / the TGXL spec. Emulates the
# direct :9008 PGXL telemetry link that AE's PgxlConnection speaks, so AE's Amp
# applet + status-bar AMP widget + TX meters can be exercised with no amplifier.
#
# Protocol (from AE's src/core/PgxlConnection.{h,cpp} — the decoder is the spec):
#   - Direct TCP on :9008. NO UDP discovery — AE connects by configured IP
#     (RadioSetupDialog -> PGXL). Reconnects every 5 s.
#   - SERVER SPEAKS FIRST: send a "V<version>\n" greeting line on connect.
#   - AE then sends  C<seq>|info  and  C<seq>|status , and polls  status  at 5 Hz.
#   - Framing: command  C<seq>|<cmd>\n  (LF); response  R<seq>|<code>|<body>\n
#     where <body> is space-separated key=val telemetry (code 0 = ok).
#   - READ-ONLY: AE only ever sends info/status on this link. OPERATE/STANDBY is
#     controlled via the radio's amplifier stream, NOT :9008 — so the sim just
#     answers polls; there is no control to honour.
#
# Telemetry keys AE reads (MainWindow_Wiring.cpp status handler):
#   state   amp state string (OPERATE/STANDBY/...)   -> setState
#   temp    PA temp C; may be dual "A/B" e.g. 40.5/38 -> setTemp (+ setTempB)
#   tempb   PA module B temp (firmware variant)       -> setTempB
#   id      drain current (A)                         -> setDrainCurrent
#   vdd     drain voltage (V)                         -> setDrainVoltage
#   vac     mains voltage (V)                         -> setMainsVoltage
#   fanmode fan mode                                  -> setFanMode
#   meffa   module-A efficiency (%)                   -> setMeff
#   peakfwd peak forward power (W)                    -> forward power
#   swr     SWR                                       -> setSwr
#
# Run:   python3 pgxl_sim.py                 # listens on :9008, interactive
#        python3 pgxl_sim.py --no-cli        # headless (staged/background)
# Then in AE: RadioSetup -> Power Genius XL -> connect to this host:9008.

import argparse
import math
import socket
import threading
import time

PGXL_PORT = 9008


class Amp:
    """In-memory PGXL telemetry model (stand-in for real amp hardware).

    Wire-unit note (pinned from AE's MainWindow_Wiring PGXL handler): `peakfwd`
    is FORWARD POWER IN dBm (AE converts to watts via pow(10,(dBm-30)/10)) and
    `swr` is RETURN LOSS IN dB (AE takes abs() then converts to SWR) — NOT watts
    and NOT an SWR ratio. AE also SKIPS peakfwd->S-meter while state==STANDBY, so
    the amp must report a non-STANDBY state (OPERATE/TRANSMIT) for power to show.
    Values held human-friendly (watts / ratio) and converted on the wire.
    """
    def __init__(self):
        self.state = "STANDBY"
        self.temp = 38.0        # PA module A temp (C)
        self.tempb = 36.5       # PA module B temp (C)
        self.id = 0.0           # drain current (A)
        self.vdd = 52.0         # drain voltage (V)
        self.vac = 240.0        # mains voltage (V)
        self.fanmode = "auto"
        self.meffa = 0.0        # efficiency (%)
        self.peakfwd_w = 0.0    # peak forward power (watts, internal)
        self.swr = 1.0          # SWR ratio (internal)
        self.keyed = False
        self.lock = threading.Lock()

    def status_body(self):
        with self.lock:
            dbm = 10.0 * math.log10(self.peakfwd_w) + 30.0 if self.peakfwd_w > 0 else 0.0
            if self.swr <= 1.0:
                rl = -60.0
            else:
                g = (self.swr - 1.0) / (self.swr + 1.0)
                rl = 20.0 * math.log10(g)   # return loss, dB (negative)
            return (f"state={self.state} temp={self.temp:.1f} tempb={self.tempb:.1f} "
                    f"id={self.id:.1f} vdd={self.vdd:.1f} vac={self.vac:.0f} "
                    f"fanmode={self.fanmode} meffa={self.meffa:.0f} "
                    f"peakfwd={dbm:.1f} swr={rl:.1f}")

    def key(self, on, tx_watts=1200.0, tx_swr=1.3):
        """Simulate keying: state TRANSMIT + forward power + current + SWR + temp."""
        with self.lock:
            self.keyed = on
            if on:
                self.state = "TRANSMIT"   # non-STANDBY so AE feeds peakfwd to S-meter
                self.peakfwd_w = tx_watts
                self.swr = tx_swr
                self.id = 18.0
                self.meffa = 65.0
                self.temp = min(self.temp + 3.0, 70.0)
                self.tempb = min(self.tempb + 3.0, 70.0)
            else:
                self.state = "OPERATE"
                self.peakfwd_w = 0.0
                self.swr = 1.0
                self.id = 0.0
                self.meffa = 0.0


class PgxlServer:
    def __init__(self, amp, version):
        self.amp = amp
        self.version = version

    def handle_client(self, conn, addr):
        registered = False
        try:
            conn.settimeout(10)
            conn.sendall(f"V{self.version}\n".encode())   # server speaks first
            buf = ""
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data.decode(errors="ignore")
                # Drop stray HTTP health probes without registering.
                if buf.startswith(("GET ", "POST ", "HEAD ")):
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                    return
                if not registered:
                    registered = True
                    print(f"[tcp] AE connected {addr}", flush=True)
                conn.settimeout(60)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("C"):
                        continue
                    seq, _, cmd = line[1:].partition("|")
                    cmd = cmd.strip().lower()
                    if cmd == "status":
                        conn.sendall(f"R{seq}|0|{self.amp.status_body()}\n".encode())
                    elif cmd == "info":
                        conn.sendall(f"R{seq}|0|model=PGXL fw={self.version}\n".encode())
                    else:
                        conn.sendall(f"R{seq}|0|\n".encode())  # ack unknowns
        except Exception as e:
            if registered:
                print(f"[tcp] client error {addr}: {e}", flush=True)
        finally:
            conn.close()
            if registered:
                print(f"[tcp] {addr} disconnected", flush=True)

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", PGXL_PORT))
        srv.listen(5)
        print(f"[tcp] PGXL simulator listening on :{PGXL_PORT}", flush=True)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn, addr),
                             daemon=True).start()


def cli(amp):
    HELP = ("commands: state <OPERATE|STANDBY|FAULT> | power <W> | swr <v> | "
            "temp <C> [tempB] | vdd <V> | vac <V> | id <A> | fan <mode> | "
            "key | unkey | status | help | quit")
    print(HELP, flush=True)
    while True:
        try:
            parts = input("pgxl> ").strip().split()
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
                print("  " + amp.status_body())
            elif c == "key":
                amp.key(True)
                print("  keyed: " + amp.status_body())
            elif c == "unkey":
                amp.key(False)
                print("  unkeyed: " + amp.status_body())
            elif c == "state":
                amp.state = parts[1].upper()
            elif c == "power":
                amp.peakfwd_w = float(parts[1])
            elif c == "swr":
                amp.swr = float(parts[1])
            elif c == "temp":
                amp.temp = float(parts[1])
                if len(parts) > 2:
                    amp.tempb = float(parts[2])
            elif c == "vdd":
                amp.vdd = float(parts[1])
            elif c == "vac":
                amp.vac = float(parts[1])
            elif c == "id":
                amp.id = float(parts[1])
            elif c == "fan":
                amp.fanmode = parts[1]
            else:
                print(HELP)
        except (IndexError, ValueError):
            print("  bad args. " + HELP)


def main():
    ap = argparse.ArgumentParser(description="Standalone 4O3A Power Genius XL simulator for AetherSDR")
    ap.add_argument("--version", default="2.11", help="firmware version in the V<ver> greeting")
    ap.add_argument("--no-cli", action="store_true",
                    help="headless: serve only, no interactive prompt")
    args = ap.parse_args()

    amp = Amp()
    server = PgxlServer(amp, args.version)
    threading.Thread(target=server.serve, daemon=True).start()
    print(f"[pgxl] amp sim ready — AE connects by IP to <this-host>:{PGXL_PORT} "
          f"(no discovery); greeting V{args.version}", flush=True)
    if args.no_cli:
        print("[pgxl] headless — serving; Ctrl-C to stop", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    else:
        cli(amp)


if __name__ == "__main__":
    main()
