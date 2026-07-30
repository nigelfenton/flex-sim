#!/usr/bin/env python3
# tgxl_sim.py — standalone 4O3A Tuner Genius XL (:9010 direct) simulator for AetherSDR.
#
# Path-A spike (2026-07-07). Third of the 4O3A accessory family (with ag_sim.py /
# pgxl_sim.py). Emulates the DIRECT port-9010 channel AE's TgxlConnection speaks.
#
# Protocol (from AE src/core/TgxlConnection.{h,cpp} — decoder = encode spec):
#   - Direct TCP :9010. No UDP discovery — AE connects by configured IP; reconnect 5 s.
#   - SERVER SPEAKS FIRST: "V<version>\n" greeting on connect.
#   - AE then sends  info  and  status , and polls  status  at 1 Hz.
#   - Framing: command  C<seq>|<cmd>\n ; response  R<seq>|<code>|<body>  (code 0 = ok,
#     body = space-separated key=val). Device may also PUSH  S0|state <kvs>  /
#     S<seq>|status <kvs>  (object name then key=vals).
#   - BIDIRECTIONAL (unlike PGXL): AE sends
#       tune relay=<0-2> move=<+1|-1>   manual L/C relay step (adjustRelay)
#       autotune                        native port-9010 autotune (requestAutotune)
#
# Pinned :9010 keys (from AE's TunerModel — the decoder): relayC1/relayL/relayC2 are
# the three Pi-network banks (relay index 0=C1, 1=L, 2=C2); antA = selected antenna;
# fwd is FORWARD POWER IN dBm (not watts — AE converts); swr is RETURN LOSS IN dB,
# NEGATIVE from the TGXL (AE converts RL->SWR). bypassA = bypass state.
# NOTE: the tuner's OPERATE/BYPASS/STANDBY state (the #3594 desync) is authoritative on
# the radio's :4992 `atu`/`amplifier` stream, NOT this :9010 link.
#
# Run:   python3 tgxl_sim.py            # listens on :9010, interactive
#        python3 tgxl_sim.py --no-cli   # headless
# Then in AE: RadioSetup -> Tuner Genius XL -> connect to this host:9010.

import argparse
import math
import socket
import threading
import time

TGXL_PORT = 9010


def watts_to_dbm(w):
    """AE reads `fwd` as dBm and converts to watts (TunerModel)."""
    return 10.0 * math.log10(w) + 30.0 if w > 0 else 0.0


def swr_to_return_loss(swr):
    """AE reads `swr` as return loss in dB, NEGATIVE from the TGXL, then
    converts RL -> SWR. A good match => large negative RL; SWR 1.0 -> floor."""
    if swr <= 1.0:
        return -60.0
    gamma = (swr - 1.0) / (swr + 1.0)
    return 20.0 * math.log10(gamma)   # negative


class Tuner:
    """In-memory TGXL model for the :9010 direct channel.

    Relay indices per AE: 0=C1, 1=L, 2=C2 (Pi network). Values held in
    human-friendly units (watts / SWR ratio) and converted on the wire to the
    dBm / negative-return-loss the real TGXL sends (keys pinned from TunerModel).
    """
    def __init__(self):
        self.bypass = 0          # bypassA: 0 = in-line, 1 = bypassed
        self.relayC1 = 0         # relay 0
        self.relayL = 0          # relay 1
        self.relayC2 = 0         # relay 2
        self.antA = 1            # selected antenna
        self.fwd_w = 0.0         # forward power (watts, internal)
        self.swr = 1.0           # SWR ratio (internal)
        self.tuned = False
        self.keyed = False
        self.lock = threading.Lock()

    def _wire(self):
        return (f"fwd={watts_to_dbm(self.fwd_w):.1f} "
                f"swr={swr_to_return_loss(self.swr):.1f} "
                f"relayC1={self.relayC1} relayL={self.relayL} relayC2={self.relayC2} "
                f"antA={self.antA}")

    def status_body(self):
        with self.lock:
            return self._wire()

    def state_body(self):
        with self.lock:
            return (f"bypassA={self.bypass} relayC1={self.relayC1} "
                    f"relayL={self.relayL} relayC2={self.relayC2} "
                    f"tuned={1 if self.tuned else 0}")

    def key(self, on, tx_watts=100.0):
        with self.lock:
            self.keyed = on
            if on:
                self.fwd_w = tx_watts
                self.swr = 1.2 if self.tuned else 2.5
            else:
                self.fwd_w = 0.0
                self.swr = 1.0

    def autotune(self):
        with self.lock:
            self.tuned = True
            self.relayC1, self.relayL, self.relayC2 = 5, 7, 12
            self.swr = 1.15 if self.keyed else 1.0
            if self.keyed:
                self.fwd_w = max(self.fwd_w, 100.0)

    def step_relay(self, relay, move):
        """relay: 0=C1, 1=L, 2=C2; move: +1/-1."""
        with self.lock:
            if relay == 0:
                self.relayC1 = max(0, self.relayC1 + move)
            elif relay == 1:
                self.relayL = max(0, self.relayL + move)
            elif relay == 2:
                self.relayC2 = max(0, self.relayC2 + move)
            else:
                return
            self.tuned = False


class TgxlServer:
    def __init__(self, tuner, version):
        self.t = tuner
        self.version = version
        self.clients = set()
        self.lock = threading.Lock()

    def push_state(self):
        msg = f"S0|state {self.t.state_body()}\n".encode()
        with self.lock:
            clients = set(self.clients)
        for c in clients:
            try:
                c.sendall(msg)
            except Exception:
                pass

    def handle_client(self, conn, addr):
        registered = False
        try:
            conn.settimeout(10)
            conn.sendall(f"V{self.version}\n".encode())    # server speaks first
            with self.lock:
                self.clients.add(conn)
            buf = ""
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data.decode(errors="ignore")
                if buf.startswith(("GET ", "POST ", "HEAD ")):
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                    return
                if not registered:
                    registered = True
                    print(f"[tcp] AE connected {addr}", flush=True)
                conn.settimeout(90)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("C"):
                        continue
                    seq, _, cmd = line[1:].partition("|")
                    self.handle_command(conn, seq, cmd.strip())
        except Exception as e:
            if registered:
                print(f"[tcp] client error {addr}: {e}", flush=True)
        finally:
            with self.lock:
                self.clients.discard(conn)
            conn.close()
            if registered:
                print(f"[tcp] {addr} disconnected", flush=True)

    def handle_command(self, conn, seq, cmd):
        c = cmd.lower()
        if c == "status":
            conn.sendall(f"R{seq}|0|{self.t.status_body()}\n".encode())
        elif c == "info":
            conn.sendall(f"R{seq}|0|model=TGXL fw={self.version}\n".encode())
        elif c == "autotune":
            print("[rx] autotune", flush=True)
            self.t.autotune()
            conn.sendall(f"R{seq}|0|\n".encode())
            self.push_state()
        elif c.startswith("tune "):
            kvs = dict(p.split("=", 1) for p in c.split()[1:] if "=" in p)
            try:
                relay = int(kvs.get("relay", -1)); move = int(kvs.get("move", 0))
            except ValueError:
                relay, move = -1, 0
            print(f"[rx] tune relay={relay} move={move}", flush=True)
            self.t.step_relay(relay, move)
            conn.sendall(f"R{seq}|0|\n".encode())
            self.push_state()
        else:
            conn.sendall(f"R{seq}|0|\n".encode())

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", TGXL_PORT))
        srv.listen(5)
        print(f"[tcp] TGXL simulator listening on :{TGXL_PORT}", flush=True)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn, addr),
                             daemon=True).start()


def cli(t):
    HELP = ("commands: key | unkey | power <W> | swr <v> | autotune | "
            "bypass <0|1> | relay <C1|L|C2> <pos> | status | help | quit")
    print(HELP, flush=True)
    while True:
        try:
            parts = input("tgxl> ").strip().split()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not parts:
            continue
        c = parts[0].lower()
        try:
            if c in ("quit", "exit"):
                return
            elif c == "help":
                print(HELP)
            elif c == "status":
                print("  " + t.status_body() + " | " + t.state_body())
            elif c == "key":
                t.key(True); print("  " + t.status_body())
            elif c == "unkey":
                t.key(False); print("  " + t.status_body())
            elif c == "power":
                t.fwd_w = float(parts[1])
            elif c == "swr":
                t.swr = float(parts[1])
            elif c == "autotune":
                t.autotune(); print("  tuned: " + t.state_body())
            elif c == "bypass":
                t.bypass = int(parts[1])
            elif c == "relay":
                name = parts[1].upper()
                pos = int(parts[2])
                if name == "C1":
                    t.relayC1 = pos
                elif name == "L":
                    t.relayL = pos
                elif name == "C2":
                    t.relayC2 = pos
                else:
                    print("  relay <C1|L|C2> <pos>")
            else:
                print(HELP)
        except (IndexError, ValueError):
            print("  bad args. " + HELP)


def main():
    ap = argparse.ArgumentParser(description="Standalone 4O3A Tuner Genius XL (:9010) simulator for AetherSDR")
    ap.add_argument("--version", default="1.5", help="firmware version in the V<ver> greeting")
    ap.add_argument("--no-cli", action="store_true", help="headless: serve only")
    args = ap.parse_args()

    t = Tuner()
    server = TgxlServer(t, args.version)
    threading.Thread(target=server.serve, daemon=True).start()
    print(f"[tgxl] tuner sim ready — AE connects by IP to <this-host>:{TGXL_PORT} "
          f"(no discovery); greeting V{args.version}", flush=True)
    if args.no_cli:
        print("[tgxl] headless — serving; Ctrl-C to stop", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    else:
        cli(t)


if __name__ == "__main__":
    main()
