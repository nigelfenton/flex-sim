#!/usr/bin/env python3
# station.py — Phase-0 integrated station-rack launcher.
#
# Starts the three 4O3A accessory sims (Antenna Genius / Power Genius XL /
# Tuner Genius XL) in ONE process, and gives you ONE prompt to drive them all —
# so meters/relays actually move in AE instead of sitting at their headless
# defaults. One launch instead of three nohup-and-hope; nothing silently
# un-started. No shared model yet (that's Phase 1) — each sim keeps its own,
# this just co-hosts + co-drives them.
#
# Optionally co-launches flex_sim.py (the radio) as a subprocess with --with-radio.
#
# Run:   python3 station.py             # accessories + unified prompt
#        python3 station.py --no-cli     # headless (staged/background)
#        python3 station.py --with-radio # also spawn flex_sim.py (defaults)

import argparse
import os
import socket
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import acom_sim
import ag_sim
import pgxl_sim
import spe_sim
import tgxl_sim


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class Station:
    def __init__(self, args):
        # Antenna Genius
        self.ag_state = ag_sim.State(args.ag_name, "G0JKN-SIM", "2.0",
                                     args.antennas, 5000, "master")
        self.ag = ag_sim.AgServer(self.ag_state)
        # Power Genius XL
        self.amp = pgxl_sim.Amp()
        self.pgxl = pgxl_sim.PgxlServer(self.amp, "2.11")
        # Tuner Genius XL
        self.tuner = tgxl_sim.Tuner()
        self.tgxl = tgxl_sim.TgxlServer(self.tuner, "1.5")
        # SPE Expert (poll-only; AE PR #4531)
        self.spe_amp = spe_sim.Amp("15K")
        self.spe = spe_sim.SpeServer(self.spe_amp)
        # ACOM 600S (merged AE #4298)
        self.acom_amp = acom_sim.Amp()
        self.acom = acom_sim.AcomServer(self.acom_amp)
        self.radio_proc = None

    def start(self, with_radio, radio_port=None, pattern=None):
        threading.Thread(target=self.ag.tcp_server, daemon=True).start()
        threading.Thread(target=self.ag.broadcaster, daemon=True).start()
        threading.Thread(target=self.pgxl.serve, daemon=True).start()
        threading.Thread(target=self.tgxl.serve, daemon=True).start()
        threading.Thread(target=self.spe.serve, daemon=True).start()
        threading.Thread(target=self.acom.serve, daemon=True).start()
        if with_radio:
            flex = os.path.join(HERE, "flex_sim.py")
            if os.path.exists(flex):
                cmd = [sys.executable, flex]
                if radio_port:
                    cmd += ["--port", str(radio_port)]
                # Pass the pattern through: without it the radio runs the
                # default 'ramp', which produces no usable TX drive -- so the
                # amp gauges sit at zero and it looks like the AMP meters are
                # broken when they are fine (bench 2026-07-30).
                if pattern:
                    cmd += ["--pattern", pattern]
                self.radio_proc = subprocess.Popen(cmd)
                print(f"[radio] spawned flex_sim.py on :{radio_port or 4992} "
                      f"(pid {self.radio_proc.pid})", flush=True)
            else:
                print("[radio] flex_sim.py not found — skipping", flush=True)

    def stop(self):
        if self.radio_proc and self.radio_proc.poll() is None:
            self.radio_proc.terminate()

    def connect_table(self):
        ip = local_ip()
        return (
            "\n  Point AetherSDR at this host (Radio Setup -> Peripherals -> Manual IP):\n"
            f"    Antenna Genius (AG)   {ip}:9007   (also auto-discovers via UDP beacon)\n"
            f"    Power Genius XL (PGXL){ip}:9008\n"
            f"    Tuner Genius XL (TGXL){ip}:9010\n"
            f"    SPE Expert (SPE)      {ip}:4531   (Network mode; poll-only)\n"
            f"    ACOM 600S (ACOM)      {ip}:9600\n")


def dispatch(st, line):
    """Unified drive prompt. Returns False to quit."""
    parts = line.split()
    if not parts:
        return True
    dev = parts[0].lower()
    a = parts[1:]
    try:
        if dev in ("quit", "exit"):
            return False
        if dev == "help":
            print(HELP); return True
        if dev == "status":
            print("  AG   : port1=" + st.ag_state.ports[1].status()
                  + " | port2=" + st.ag_state.ports[2].status())
            print("  PGXL : " + st.amp.status_body())
            print("  TGXL : " + st.tuner.status_body() + " | " + st.tuner.state_body())
            print("  SPE  : " + st.spe_amp.status_payload().decode())
            print("  ACOM : mode=0x%02X fwd=%dW swr=%.2f temp=%.1fC fault=0x%02X"
                  % (st.acom_amp.mode_byte, st.acom_amp.fwd_power,
                     st.acom_amp.swr, st.acom_amp.temp_c, st.acom_amp.fault))
            return True

        if dev == "ag":
            # ag <port 1|2> <antenna N>
            p, ant = int(a[0]), int(a[1])
            with st.ag_state.lock:
                st.ag_state.ports[p].rxant = ant
                st.ag_state.ports[p].txant = ant
            st.ag.push_port(p)
            print(f"  AG port {p} -> antenna {ant}")
        elif dev == "pgxl":
            c = a[0].lower()
            if c == "key": st.amp.key(True)
            elif c == "unkey": st.amp.key(False)
            elif c == "power": st.amp.peakfwd_w = float(a[1])
            elif c == "swr": st.amp.swr = float(a[1])
            elif c == "temp": st.amp.temp = float(a[1])
            elif c == "state": st.amp.state = a[1].upper()
            else: print("  pgxl: key|unkey|power <W>|swr <v>|temp <C>|state <s>"); return True
            print("  PGXL : " + st.amp.status_body())
        elif dev == "tgxl":
            c = a[0].lower()
            if c == "key": st.tuner.key(True)
            elif c == "unkey": st.tuner.key(False)
            elif c == "autotune": st.tuner.autotune(); st.tgxl.push_state()
            elif c == "swr": st.tuner.swr = float(a[1])
            elif c == "power": st.tuner.fwd_w = float(a[1])
            elif c == "relay":
                r, mv = int(a[1]), int(a[2]); st.tuner.step_relay(r, mv); st.tgxl.push_state()
            else: print("  tgxl: key|unkey|autotune|swr <v>|power <W>|relay <1|2> <+/-1>"); return True
            print("  TGXL : " + st.tuner.status_body() + " | " + st.tuner.state_body())
        elif dev == "spe":
            c = a[0].lower()
            if c == "key": st.spe_amp.key(True)
            elif c == "unkey": st.spe_amp.key(False)
            elif c == "oper": st.spe_amp.operate = a[1] == "1"
            elif c == "power": st.spe_amp.out_w = int(a[1])
            elif c == "swr":
                st.spe_amp.swr_atu = float(a[1])
                st.spe_amp.swr_ant = st.spe_amp.swr_atu + 0.15
            elif c == "temp": st.spe_amp.t_up = int(a[1])
            elif c == "model":
                m = a[1].upper()
                if m in ("13K", "15K", "20K"):
                    st.spe_amp.model = m
                    st.spe_amp.bank = "x" if m == "20K" else "A"
            elif c == "warn": st.spe_amp.warning = a[1][0]
            elif c == "alarm": st.spe_amp.alarm = a[1][0]
            else:
                print("  spe: key|unkey|oper <0|1>|power <W>|swr <v>|temp <C>|"
                      "model <13K|15K|20K>|warn <ch>|alarm <ch>"); return True
            print("  SPE  : " + st.spe_amp.status_payload().decode())
        elif dev == "acom":
            c = a[0].lower()
            if c == "key": st.acom_amp.key(True)
            elif c == "unkey": st.acom_amp.key(False)
            elif c == "oper":
                st.acom_amp.mode_byte = (acom_sim.MODE_OPER_RX if a[1] == "1"
                                         else acom_sim.MODE_STANDBY)
            elif c == "power": st.acom_amp.fwd_power = int(a[1])
            elif c == "swr": st.acom_amp.swr = float(a[1])
            elif c == "temp": st.acom_amp.temp_c = float(a[1])
            elif c == "fault": st.acom_amp.fault = int(a[1], 16)
            else:
                print("  acom: key|unkey|oper <0|1>|power <W>|swr <v>|temp <C>|"
                      "fault <hex, FF=none>"); return True
            print("  ACOM : mode=0x%02X fwd=%dW swr=%.2f fault=0x%02X"
                  % (st.acom_amp.mode_byte, st.acom_amp.fwd_power,
                     st.acom_amp.swr, st.acom_amp.fault))
        else:
            print(HELP)
    except (IndexError, ValueError):
        print("  bad args. " + HELP)
    return True


HELP = ("commands:\n"
        "  status                          show all three\n"
        "  ag <port 1|2> <antenna N>       select an antenna\n"
        "  pgxl key|unkey|power <W>|swr <v>|temp <C>|state <s>\n"
        "  tgxl key|unkey|autotune|swr <v>|power <W>|relay <1|2> <+/-1>\n"
        "  help | quit")


def main():
    ap = argparse.ArgumentParser(description="Phase-0 station-rack launcher (AG+PGXL+TGXL+SPE+ACOM in one process)")
    ap.add_argument("--ag-name", default="AntennaGenius",
                    help="AG beacon name (ShackSwitch hits AE's isShackSwitch path)")
    ap.add_argument("--antennas", type=int, default=8)
    ap.add_argument("--with-radio", action="store_true", help="also spawn flex_sim.py")
    # ⚠ Stays at 4992 on purpose. Other radios on this network also answer
    # discovery (the hub's noise bench, aether-gate instances, the real 6700),
    # so AE may pair with the WRONG one while still taking peripherals from
    # here -- pick this sim by SERIAL (FLEXSIM00) in AE's radio list. Moving to
    # another port avoids the collision but AE then fails to complete the
    # connection at all (bench-tested 2026-07-30), which is worse.
    ap.add_argument("--pattern", default="carrier",
                    help="signal pattern for the spawned radio (default carrier: "
                         "gives a real S-meter reading and TX drive, unlike 'ramp')")
    ap.add_argument("--radio-port", type=int, default=4992,
                    help="control/data port for the spawned radio "
                         "(default 5992; avoids a :4992 collision)")
    ap.add_argument("--no-cli", action="store_true", help="headless: serve only")
    args = ap.parse_args()

    st = Station(args)
    st.start(args.with_radio, args.radio_port, args.pattern)
    print("[station] AG :9007 + PGXL :9008 + TGXL :9010 + SPE :4531 + ACOM :9600 up in one process")
    print(st.connect_table())

    try:
        if args.no_cli:
            print("[station] headless — serving; Ctrl-C to stop", flush=True)
            threading.Event().wait()
        else:
            print(HELP)
            while True:
                try:
                    line = input("station> ").strip()
                except EOFError:
                    break
                if not dispatch(st, line):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        st.stop()


if __name__ == "__main__":
    main()
