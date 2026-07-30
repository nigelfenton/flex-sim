#!/usr/bin/env python3
# ag_sim.py — standalone 4O3A Antenna Genius simulator for AetherSDR.
#
# Path-A spike (2026-07-07): the proven AG-protocol server from
# nigelfenton/shackswitch (shackswitch-v2/main.py, the `ag_*` block) lifted out
# and made hardware-free — the relay driver (`bridge_call`) and persisted config
# (`load_config`/`get_profile`) are replaced by the in-memory State model below.
# Wire behaviour is kept identical to the ShackSwitch, which AetherSDR already
# discovers and drives (src/models/AntennaGeniusModel.* — the decoder is the spec).
#
# Derived from G0JKN ShackSwitch (GPL-3.0). No hardware required.
#
# Run:   python3 ag_sim.py                 # advertises name=AntennaGenius, 8 ants
#        python3 ag_sim.py --name ShackSwitch   # exercise AE's isShackSwitch() path
#        python3 ag_sim.py --antennas 6 --version 1.0
# Then:  AetherSDR should discover it; type `help` at the sim prompt to drive it.

import argparse
import socket
import threading
import time

AG_PORT = 9007
BCAST_INTERVAL = 5  # seconds

# id, name, freq_start(MHz), freq_stop(MHz)
BANDS = [
    (1, "160m", 1.8, 2.0), (2, "80m", 3.5, 4.0), (3, "60m", 5.3, 5.4),
    (4, "40m", 7.0, 7.3), (5, "30m", 10.1, 10.15), (6, "20m", 14.0, 14.35),
    (7, "17m", 18.068, 18.168), (8, "15m", 21.0, 21.45), (9, "12m", 24.89, 24.99),
    (10, "10m", 28.0, 29.7), (11, "6m", 50.0, 54.0),
]
ALL_BANDS_MASK = sum(1 << (b[0] - 1) for b in BANDS)


class Port:
    def __init__(self):
        self.auto = 1
        self.band = 0
        self.rxant = 0
        self.txant = 0
        self.tx = 0
        self.inhibit = 0

    def status(self):
        return (f"auto={self.auto} band={self.band} rxant={self.rxant} "
                f"txant={self.txant} tx={self.tx} inhibit={self.inhibit}")


class State:
    """In-memory stand-in for ShackSwitch config + hardware relays."""
    def __init__(self, name, serial, version, n_antennas, webport, mode):
        self.name = name
        self.serial = serial
        self.version = version
        self.n_antennas = n_antennas
        self.webport = webport
        self.mode = mode
        # Every antenna legal on every band by default; name Ant_1..Ant_N.
        self.antennas = {i: {"name": f"Ant_{i}", "tx": ALL_BANDS_MASK,
                             "rx": ALL_BANDS_MASK} for i in range(1, n_antennas + 1)}
        self.ports = {1: Port(), 2: Port()}
        self.lock = threading.Lock()


class AgServer:
    def __init__(self, state, ip_override=None):
        self.st = state
        self.ip_override = ip_override
        self.clients = set()
        self.clients_lock = threading.Lock()

    # ── discovery ──────────────────────────────────────────────────────────
    def local_ip(self):
        if self.ip_override:
            return self.ip_override
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def broadcaster(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            try:
                pkt = (
                    f"AG ip={self.local_ip()} port={AG_PORT} v={self.st.version} "
                    f"serial={self.st.serial} name={self.st.name} ports=2 "
                    f"antennas={self.st.n_antennas} webport={self.st.webport} "
                    f"mode={self.st.mode}\r\n"
                ).encode()
                sock.sendto(pkt, ("255.255.255.255", AG_PORT))
            except Exception as e:
                print(f"[beacon] error: {e}", flush=True)
            time.sleep(BCAST_INTERVAL)

    # ── async push ─────────────────────────────────────────────────────────
    def push(self, msg):
        with self.clients_lock:
            clients = set(self.clients)
        dead = set()
        for conn in clients:
            try:
                conn.sendall(msg.encode())
            except Exception:
                dead.add(conn)
        if dead:
            with self.clients_lock:
                self.clients.difference_update(dead)

    def push_port(self, p):
        self.push(f"S0|port {p} {self.st.ports[p].status()}\r\n")

    # ── command handler ────────────────────────────────────────────────────
    def handle_command(self, conn, seq, cmd):
        c = cmd.lower().strip()
        st = self.st

        if c == "antenna list":
            out = []
            for i in range(1, st.n_antennas + 1):
                a = st.antennas[i]
                out.append(f"R{seq}|00|antenna {i} name={a['name']} "
                           f"tx={hex(a['tx'])} rx={hex(a['rx'])} inband={hex(a['tx'])}\r\n")
            out.append(f"R{seq}|00|\r\n")
            conn.sendall("".join(out).encode())

        elif c == "band list":
            out = [f"R{seq}|00|band {b[0]} name={b[1]} "
                   f"freq_start={b[2]} freq_stop={b[3]}\r\n" for b in BANDS]
            out.append(f"R{seq}|00|\r\n")
            conn.sendall("".join(out).encode())

        elif c.startswith("port get"):
            try:
                p = int(c.split()[-1])
            except ValueError:
                p = 1
            if p not in st.ports:
                p = 1
            conn.sendall(f"R{seq}|00|port {p} {st.ports[p].status()}\r\n".encode())

        elif c.startswith("port set "):
            parts = c.split()
            try:
                p = int(parts[2])
            except (IndexError, ValueError):
                conn.sendall(f"R{seq}|00|\r\n".encode())
                return
            kvs = dict(kv.split("=", 1) for kv in parts[3:] if "=" in kv)
            with st.lock:
                port = st.ports.get(p)
                if port is not None:
                    if "rxant" in kvs:
                        port.rxant = int(kvs["rxant"])
                    if "txant" in kvs:
                        port.txant = int(kvs["txant"])
            print(f"[port set] port {p} rxant={kvs.get('rxant')} "
                  f"txant={kvs.get('txant')}  (relay: no-op, sim)", flush=True)
            conn.sendall(f"R{seq}|00|\r\n".encode())
            if p in st.ports:
                self.push_port(p)

        elif c == "sub port all":
            conn.sendall(f"R{seq}|00|\r\n".encode())
            for p in (1, 2):
                conn.sendall(f"S0|port {p} {st.ports[p].status()}\r\n".encode())

        elif c == "sub relay":
            conn.sendall(f"R{seq}|00|\r\n".encode())
            for i in range(1, st.n_antennas + 1):
                on = any(pt.rxant == i or pt.txant == i for pt in st.ports.values())
                conn.sendall(f"S0|relay={i} state={'on' if on else 'off'}\r\n".encode())

        else:
            # info get, ping, other subs → bare ack (matches the proven device;
            # identity comes from the UDP beacon).
            conn.sendall(f"R{seq}|00|\r\n".encode())

    # ── per-client loop ────────────────────────────────────────────────────
    def handle_client(self, conn, addr):
        registered = False
        try:
            conn.settimeout(5)
            conn.sendall(f"V{self.st.version} AG\r\n".encode())  # server speaks first
            buf = ""
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data.decode(errors="ignore")
                # Drop Arduino-platform HTTP health probes; never register them.
                if buf.startswith(("GET ", "POST ", "HEAD ")):
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                    return
                if not registered:
                    registered = True
                    with self.clients_lock:
                        self.clients.add(conn)
                    print(f"[tcp] client registered {addr}", flush=True)
                conn.settimeout(60)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    print(f"[rx] {line}", flush=True)
                    if line.startswith("C"):
                        head, _, rest = line[1:].partition("|")
                        if rest:
                            self.handle_command(conn, head, rest.strip())
        except Exception as e:
            if registered:
                print(f"[tcp] client error {addr}: {e}", flush=True)
        finally:
            with self.clients_lock:
                self.clients.discard(conn)
            conn.close()
            if registered:
                print(f"[tcp] {addr} disconnected", flush=True)

    def tcp_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", AG_PORT))
        srv.listen(5)
        print(f"[tcp] AG simulator listening on :{AG_PORT}", flush=True)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn, addr),
                             daemon=True).start()


def cli(server):
    st = server.st
    HELP = ("commands: set <port 1|2> <ant> | band <port> <bandid 0-11> | "
            "tx <port> <0|1> | status | clients | help | quit")
    print(HELP, flush=True)
    while True:
        try:
            line = input("ag> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd in ("quit", "exit"):
                return
            elif cmd == "help":
                print(HELP)
            elif cmd == "status":
                for p in (1, 2):
                    print(f"  port {p}: {st.ports[p].status()}")
            elif cmd == "clients":
                print(f"  {len(server.clients)} connected")
            elif cmd == "set":
                p, a = int(parts[1]), int(parts[2])
                with st.lock:
                    st.ports[p].rxant = a
                    st.ports[p].txant = a
                server.push_port(p)
                print(f"  port {p} -> antenna {a} (pushed)")
            elif cmd == "band":
                p, b = int(parts[1]), int(parts[2])
                with st.lock:
                    st.ports[p].band = b
                server.push_port(p)
                print(f"  port {p} band -> {b} (pushed)")
            elif cmd == "tx":
                p, v = int(parts[1]), int(parts[2])
                with st.lock:
                    st.ports[p].tx = v
                server.push_port(p)
                print(f"  port {p} tx -> {v} (pushed)")
            else:
                print(HELP)
        except (IndexError, ValueError):
            print("  bad args. " + HELP)


def main():
    ap = argparse.ArgumentParser(description="Standalone 4O3A Antenna Genius simulator for AetherSDR")
    ap.add_argument("--name", default="AntennaGenius",
                    help="beacon name (use ShackSwitch to hit AE's isShackSwitch path)")
    ap.add_argument("--serial", default="G0JKN-SIM")
    ap.add_argument("--version", default="2.0", help="protocol version in greeting/beacon")
    ap.add_argument("--antennas", type=int, default=8)
    ap.add_argument("--webport", type=int, default=5000)
    ap.add_argument("--mode", default="master", choices=["master", "slave"])
    ap.add_argument("--ip", default=None, help="override advertised IP in the beacon")
    ap.add_argument("--no-broadcast", action="store_true",
                    help="skip UDP discovery (use AE manual connect by IP)")
    ap.add_argument("--no-cli", action="store_true",
                    help="headless: skip the interactive prompt, just broadcast + serve "
                         "(for running as a background/staged device)")
    args = ap.parse_args()

    st = State(args.name, args.serial, args.version, args.antennas,
               args.webport, args.mode)
    server = AgServer(st, ip_override=args.ip)

    threading.Thread(target=server.tcp_server, daemon=True).start()
    if not args.no_broadcast:
        threading.Thread(target=server.broadcaster, daemon=True).start()
        print(f"[beacon] advertising name={st.name} antennas={st.n_antennas} "
              f"v={st.version} on :{AG_PORT}", flush=True)
    if args.no_cli:
        print("[sim] headless — broadcasting + serving; Ctrl-C to stop", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    else:
        cli(server)


if __name__ == "__main__":
    main()
