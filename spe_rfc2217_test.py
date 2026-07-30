#!/usr/bin/env python3
"""Exercise the three ser2net personalities with AE's exact power-ON bytes.

Byte sequences are taken from AetherSDR PR #4531 SpeProtocol.cpp /
SpeConnection.cpp, not invented here:
  buildWillComPortOption() -> FF FB 2C
  buildSetControl(c)       -> FF FA 2C 05 <c> FF F0
  powerOnStep()            -> (DTR on, RTS off) 100ms
                              (DTR off, RTS on) 1000ms   <- the pulse
                              (DTR on, RTS off)
"""
import socket
import subprocess
import sys
import time

PORT = 4599                      # scratch; the live bench owns 4531
IAC, SB, SE, WILL, DO, DONT = 0xFF, 0xFA, 0xF0, 0xFB, 0xFD, 0xFE
COM_PORT, SET_CONTROL = 0x2C, 0x05
DTR_ON, DTR_OFF, RTS_ON, RTS_OFF = 0x08, 0x09, 0x0B, 0x0C

fails = []


def check(cond, label, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def poll(s):
    """Send a status request; return the reply or b'' on silence."""
    s.sendall(bytes([0x55] * 3 + [1, 0x90, 0x90]) + b"\r\n")
    try:
        return s.recv(512)
    except socket.timeout:
        return b""


def set_control(s, ctrl):
    s.sendall(bytes([IAC, SB, COM_PORT, SET_CONTROL, ctrl, IAC, SE]))


def pulse(s):
    """AE's exact power-ON sequence, with its real pacing."""
    set_control(s, DTR_ON);  set_control(s, RTS_OFF); time.sleep(0.10)
    set_control(s, DTR_OFF); set_control(s, RTS_ON);  time.sleep(1.00)
    set_control(s, DTR_ON);  set_control(s, RTS_OFF); time.sleep(0.20)


def drain(s):
    try:
        return s.recv(4096)
    except socket.timeout:
        return b""


def run(mode, power="off"):
    p = subprocess.Popen(
        [sys.executable, "spe_sim.py", "--no-cli", "--port", str(PORT),
         "--port-mode", mode, "--power", power],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.2)
    return p


def stop(p):
    p.terminate()
    try:
        return p.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()[0]


print("\n=== 1. rfc2217: the pulse must actually switch the amp on ===")
p = run("rfc2217")
s = socket.create_connection(("127.0.0.1", PORT), timeout=3); s.settimeout(1.5)
check(poll(s) == b"", "amp is OFF -> polls get no reply (link stays up)")
s.sendall(bytes([IAC, WILL, COM_PORT]))
time.sleep(0.3)
reply = drain(s)
check(reply[:3] == bytes([IAC, DO, COM_PORT]),
      "WILL COM-PORT-OPTION -> proxy replies DO", reply[:3].hex())
pulse(s)
drain(s)
r = poll(s)
check(r[:3] == b"\xaa\xaa\xaa", "after the pulse the amp answers polls",
      r[4:4 + (r[3] if r else 0)].decode(errors="replace")[:40])
s.close()
log = stop(p)
check("power-ON pulse accepted" in log, "pulse recognised and logged")
check("resting state after the pulse: DTR=1" in log,
      "reports DTR left HIGH at rest (review finding 1 is observable)")

print("\n=== 2. telnet without rfc2217=true: silently does nothing ===")
p = run("telnet")
s = socket.create_connection(("127.0.0.1", PORT), timeout=3); s.settimeout(1.5)
s.sendall(bytes([IAC, WILL, COM_PORT]))
time.sleep(0.3)
reply = drain(s)
check(reply[:3] == bytes([IAC, DONT, COM_PORT]),
      "WILL COM-PORT-OPTION -> proxy replies DONT (refused)", reply[:3].hex())
pulse(s)
drain(s)
check(poll(s) == b"", "amp still OFF -- the pulse did nothing")
s.close()
log = stop(p)
check("SET-CONTROL" in log and "DISCARDED" in log, "discarded SET-CONTROL is logged")

print("\n=== 3. raw tcp accepter: IAC frames reach the amp as payload ===")
p = run("raw")
s = socket.create_connection(("127.0.0.1", PORT), timeout=3); s.settimeout(1.5)
s.sendall(bytes([IAC, WILL, COM_PORT]))
time.sleep(0.3)
check(drain(s) == b"", "no telnet layer -> no negotiation reply at all")
pulse(s)
drain(s)
check(poll(s) == b"", "amp still OFF -- the pulse did nothing")
s.close()
log = stop(p)
check("decode as RFC 2217" in log,
      "junk is diagnosed as RFC 2217 against a raw port, not silently dropped")

print("\n=== 4. regression: PR #4531's own status vectors still byte-identical ===")
sys.path.insert(0, ".")
import importlib
spe = importlib.import_module("spe_sim")
a = spe.Amp("20K"); a.band = 0
rx = "C,20K,S,R,x,1,00,1a,0r,L,0000, 0.00, 0.00, 0.0, 0.0, 33,  0,  0,N,N"
check(a.status_payload().decode() == rx, "RX vector (67 chars)",
      str(len(a.status_payload())))
b = spe.Amp("15K"); b.operate = True; b.tx = True; b.input = 2; b.band = 5
b.tx_ant = 2; b.atu = "b"; b.power_lvl = "H"; b.out_w = 1350
b.swr_atu, b.swr_ant = 1.10, 1.25; b.pa_v, b.pa_a = 47.5, 32.0
b.t_up, b.t_lo, b.t_comb, b.warning = 45, 40, 38, "S"
tx = "C,15K,O,T,A,2,05,2b,0r,H,1350, 1.10, 1.25, 47.5, 32.0, 45, 40, 38,S,N"
check(b.status_payload().decode() == tx, "TX vector (69 chars)",
      str(len(b.status_payload())))

print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
