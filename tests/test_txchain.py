#!/usr/bin/env python3
"""txchain observer test — walks the #4510 'audio arrives but no RF' sequence.

Drives a live flex_sim through AE's exact command order (from the #4510 report
log) and asserts the /txchain verdict names each FIRST-missing stage in turn,
then flips the auto-adopt knob and proves the works-for-many population needs
no explicit tx=1. No AetherSDR required: the point of the observer is that the
radio side alone can testify to the chain, and this test IS that testimony.

Run: python3 tests/test_txchain.py   (spawns its own sim on :5993/:8733)
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT, CTL = 5993, 8733
DAXTX_PORT = 4993          # not 4991: a running sim owns that, and one process per box can bind it
ROOT = Path(__file__).resolve().parent.parent

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    print(f"[{' OK ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    if ok:
        passed += 1
    else:
        failed += 1


def ctl(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{CTL}{path}", timeout=5) as r:
        return json.loads(r.read())


def wait_ctl(deadline=15.0):
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            ctl("/txchain")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    sim = subprocess.Popen(
        [sys.executable, str(ROOT / "flex_sim.py"), "--port", str(PORT),
         "--ctl-port", str(CTL)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
        env={**os.environ, "FLEXSIM_DAXTX_PORT": str(DAXTX_PORT)})
    try:
        if not wait_ctl():
            print("FAIL: sim control panel never came up")
            return 1

        # Fake-AE control connection. Replies are asserted via /txchain, not
        # parsed from TCP — the observer is the interface under test.
        # The sim binds its TCP listener on ONE chosen interface IP (same-host
        # mode), and this box is multi-homed (LAN + Tailscale) — so neither
        # loopback nor gethostbyname() is reliable. The sim itself logs which
        # IP it bound; read it from the startup banner.
        host_ip = None
        t0 = time.time()
        while time.time() - t0 < 10 and host_ip is None:
            line = sim.stdout.readline().decode(errors="replace")
            if "radio ip " in line:
                host_ip = line.split("radio ip ", 1)[1].split(",")[0].split(")")[0].strip()
        if not host_ip:
            print("FAIL: could not learn the sim's bound IP from its banner")
            return 1
        tcp = socket.create_connection((host_ip, PORT), timeout=5)
        tcp.settimeout(0.5)

        def cmd(text, seq=[0]):
            seq[0] += 1
            tcp.sendall(f"C{seq[0]}|{text}\n".encode())
            try:
                tcp.recv(4096)          # drain; content not asserted
            except socket.timeout:
                pass
            time.sleep(0.2)

        def drain_greeting():
            try:
                tcp.recv(8192)
            except socket.timeout:
                pass

        drain_greeting()

        # ---- stage-by-stage: each step moves the first-missing pointer ----
        v = ctl("/txchain")
        check("no stream yet -> stage 2 named",
              "2:" in v["verdict"], v["verdict"])

        cmd("stream create type=dax_tx compression=NONE")
        v = ctl("/txchain")
        check("stream id is the real-firmware 0x84000000",
              v["stream_id"] == "0x84000000", str(v["stream_id"]))
        check("stream exists -> stage 3 named",
              "3:" in v["verdict"], v["verdict"])

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(20):             # ~AE cadence, content irrelevant
            udp.sendto(b"\x38\x84\x00\x00" + b"\x00" * 60, ("127.0.0.1", DAXTX_PORT))
            time.sleep(0.01)
        v = ctl("/txchain")
        check("audio flowing -> stage 4 named",
              "4:" in v["verdict"], v["verdict"])
        check("audio packets counted", v["audio_pkts"] >= 20,
              str(v["audio_pkts"]))

        cmd("transmit set dax=1")
        cmd("transmit set mox=1")
        for _ in range(10):             # keep stage 3 fresh across the checks
            udp.sendto(b"\x38\x84\x00\x00" + b"\x00" * 60, ("127.0.0.1", DAXTX_PORT))
            time.sleep(0.01)
        v = ctl("/txchain")
        check("THE #4510 REPRODUCTION: keyed+dax+audio but stage 5 named",
              "5:" in v["verdict"], v["verdict"])

        cmd("stream set 0x84000000 tx=1")
        for _ in range(10):
            udp.sendto(b"\x38\x84\x00\x00" + b"\x00" * 60, ("127.0.0.1", DAXTX_PORT))
            time.sleep(0.01)
        v = ctl("/txchain")
        check("tx=1 claimed -> RF WOULD BE PRODUCED",
              v["verdict"] == "RF WOULD BE PRODUCED", v["verdict"])

        # ---- release + remove tears the chain back down ----
        cmd("transmit set mox=0")
        cmd("stream remove 0x84000000")
        v = ctl("/txchain")
        check("stream removed -> stage 2 named again",
              "2:" in v["verdict"], v["verdict"])

        # ---- the works-for-many population: firmware auto-adopts tx=1 ----
        ctl("/txchain/adopt?on=1")
        cmd("stream create type=dax_tx compression=NONE")
        cmd("transmit set dax=1")
        cmd("transmit set mox=1")
        for _ in range(25):
            udp.sendto(b"\x38\x84\x00\x00" + b"\x00" * 60, ("127.0.0.1", DAXTX_PORT))
            time.sleep(0.01)
        v = ctl("/txchain")
        check("auto-adopt population: RF WOULD BE PRODUCED with NO tx=1 sent",
              v["verdict"] == "RF WOULD BE PRODUCED" and v["auto_adopt"],
              v["verdict"])

        print(f"\n{passed} passed, {failed} failed")
        return 0 if failed == 0 else 1
    finally:
        sim.kill()


if __name__ == "__main__":
    sys.exit(main())
