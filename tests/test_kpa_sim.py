#!/usr/bin/env python3
"""KPA1500 simulator tests: the command set, network PTT, and the T/R interlock.

The interlock tests are the point. Each one drives a keying sequence the way a
controller would -- ^TX / ^RX over TCP, exciter RF over the RF-sense UDP port --
and asserts whether the sim called it a hot switch. The good sequences must
produce ZERO hot switches and a measured lead; each bad one must produce exactly
the hot switch it is named for. A sim that never reported a hot switch would
pass every "good" test, so the bad ones are what prove the detector works.

The last test runs the real chain: flex_sim.py --rf-sense -> kpa_sim, with the
radio keyed the way AE keys it ('transmit set mox=1'), in both orders.

Run: python3 -m pytest tests/test_kpa_sim.py -v
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import kpa_sim  # noqa: E402


class Bench:
    """One amp, its TCP/UDP command server, and its RF-sense input, on free ports."""

    def __init__(self, **amp_kw):
        self.amp = kpa_sim.Amp(**amp_kw)
        self.srv = kpa_sim.KpaServer(self.amp, port=0, host="127.0.0.1").start()
        self.rf = kpa_sim.RfSense(self.amp, port=0, host="127.0.0.1").start()
        self.tcp = socket.create_connection(("127.0.0.1", self.srv.port), timeout=2)
        self.tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        wait_for(lambda: self.srv._client is not None, "TCP client registered")

    def ask(self, cmd, expect_reply=True, timeout=0.5):
        """Send one command; return its response, or None if none came."""
        self.tcp.sendall(cmd.encode())
        self.tcp.settimeout(timeout if expect_reply else 0.15)
        buf = b""
        try:
            while not buf.endswith(b";"):
                chunk = self.tcp.recv(256)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            pass
        return buf.decode() or None

    def rf_on(self, watts=50):
        self.udp.sendto(f"RF 1 {watts}\n".encode(), ("127.0.0.1", self.rf.port))
        wait_for(lambda: self.amp.rf_present, "RF seen by the amp")

    def rf_off(self):
        self.udp.sendto(b"RF 0\n", ("127.0.0.1", self.rf.port))
        wait_for(lambda: not self.amp.rf_present, "RF gone at the amp")

    def kinds(self):
        return [e["kind"] for e in self.amp.events]

    def hot(self):
        return [e for e in self.amp.events if e["kind"] == "HOT_SWITCH"]

    def close(self):
        for s in (self.tcp, self.udp):
            try:
                s.close()
            except OSError:
                pass
        self.srv.close()
        self.rf.close()


def wait_for(cond, what, timeout=2.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return
        time.sleep(0.002)
    raise AssertionError(f"timed out waiting for: {what}")


@pytest.fixture
def bench():
    b = Bench()
    yield b
    b.close()


# ---- the command set --------------------------------------------------------------

def test_identity_and_formats(bench):
    assert bench.ask(";") == ";"
    assert bench.ask("^i;") == "^KPA1500;"                 # any case is accepted
    assert bench.ask("^RVM;") == "^RVM03.07;"
    assert bench.ask("^SN;") == "^SN00123;"
    assert bench.ask("^AN;") == "^AN1;"                    # WITH the caret (the Giga sent AN1;)
    assert bench.ask("^WS;") == "^WS0000 011;"
    assert bench.ask("^VI;") == "^VI520 000;"
    assert bench.ask("^FL;") == "^FL00;"
    assert bench.ask("^LQ;") == "^LQ00000000000018;"      # reference example: STBY, ANT1, ATU IN
    assert bench.ask("^VG;").startswith("^VG TRINHIBIT x01 TR_STATE_RX")


def test_set_commands_get_no_reply(bench):
    assert bench.ask("^OS1;", expect_reply=False) is None
    assert bench.ask("^OS;") == "^OS1;"
    assert bench.ask("^BN07;", expect_reply=False) is None
    assert bench.ask("^BN;") == "^BN07;"
    assert bench.ask("^AN2;", expect_reply=False) is None
    assert bench.ask("^AN;") == "^AN2;"
    assert bench.ask("^TR20;", expect_reply=False) is None
    assert bench.ask("^TR;") == "^TR20;"
    assert bench.ask("^TR51;", expect_reply=False) is None  # out of range: ignored
    assert bench.ask("^TR;") == "^TR20;"


def test_udp_answers_one_command(bench):
    bench.udp.settimeout(1)
    bench.udp.sendto(b"^I;", ("127.0.0.1", bench.srv.port))
    assert bench.udp.recvfrom(64)[0] == b"^KPA1500;"


def test_power_off_sleeps(bench):
    bench.ask("^ON0;", expect_reply=False)
    assert bench.ask("^PWF;", expect_reply=False) is None  # asleep: no answer
    assert bench.ask("^I;") == "^KPA1500;"                 # these still answer
    assert bench.ask("^ON;") == "^ON0;"
    bench.ask("^ON1;", expect_reply=False)
    assert bench.ask("^PWF;") == "^PWF0000;"


# ---- network PTT and the T/R interlock ----------------------------------------------

def test_clean_over_measures_lead_and_tail(bench):
    bench.ask("^OS1;", expect_reply=False)
    assert bench.ask("^TX;") == "^TX;"
    assert bench.ask("^TQ;") == "^TQ1;"
    assert bench.amp.tr == "TX"
    time.sleep(0.030)                                      # 30 ms lead: 6x the 5 ms needed
    bench.rf_on(50)
    assert bench.ask("^PWF;") == "^PWF1250;"               # 50 W x 25
    assert bench.ask("^PWI;") == "^PWI0050;"
    assert bench.ask("^LQ;").endswith("1B;")               # ANT1 x10 + ATU IN x08 + OPER x02 + TX x01
    bench.rf_off()
    time.sleep(0.010)
    assert bench.ask("^RX;") == "^RX;"
    assert bench.ask("^TQ;") == "^TQ0;"
    assert bench.amp.tr == "RX"
    assert bench.hot() == []
    assert bench.amp.last_lead_ms >= 25
    assert "lead" in bench.kinds() and "tail" in bench.kinds()
    assert bench.amp.last_tail_ms >= 5


def test_rf_before_the_key_is_a_hot_switch(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.rf_on(50)                                        # exciter keyed FIRST
    assert bench.hot() == []                               # RF through the bypass is fine...
    bench.ask("^TX;")                                      # ...moving the relays under it is not
    hot = bench.hot()
    assert len(hot) == 1 and "RF led the key" in hot[0]["reason"]


def test_too_short_a_lead_is_a_hot_switch():
    b = Bench(relay_ms=50)                                 # wide window: robust on a slow runner
    try:
        b.ask("^OS1;", expect_reply=False)
        b.ask("^TX;")
        b.rf_on(50)                                        # immediately: well inside 50 ms
        hot = b.hot()
        assert len(hot) == 1 and "after the relays started moving" in hot[0]["reason"]
        assert hot[0]["lead_ms"] < 50
    finally:
        b.close()


def test_rx_with_rf_still_on_is_a_hot_switch(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.ask("^TX;")
    time.sleep(0.020)
    bench.rf_on(50)
    bench.ask("^RX;")                                      # unkeyed with the exciter still on
    hot = bench.hot()
    assert len(hot) == 1 and "dropped to RX" in hot[0]["reason"] and "^RX" in hot[0]["reason"]


def test_tr_delay_lets_rf_clear_before_the_relays_move(bench):
    """^TR exists for exciters that trail RF after the key is released."""
    bench.ask("^OS1;", expect_reply=False)
    bench.ask("^TR40;", expect_reply=False)
    bench.ask("^TX;")
    time.sleep(0.020)
    bench.rf_on(50)
    bench.ask("^RX;")                                      # released with RF still on...
    assert bench.amp.tr == "TX"                            # ...but ^TR holds the relays
    bench.rf_off()                                         # RF clears inside the 40 ms hold
    wait_for(lambda: bench.amp.tr == "RX", "relays back to RX after ^TR")
    assert bench.hot() == []


def test_tx_timeout_is_a_watchdog(bench):
    """^TXnn; expires if the controller stops refreshing it: the lost-link case."""
    bench.ask("^OS1;", expect_reply=False)
    assert bench.ask("^TX1;") == "^TX;"
    time.sleep(0.020)
    bench.rf_on(50)
    wait_for(lambda: bench.amp.tr == "RX", "^TX1 to expire", timeout=2.5)
    assert bench.ask("^TQ;") == "^TQ0;"
    assert "key_sw_expired" in bench.kinds()
    hot = bench.hot()
    assert len(hot) == 1 and "timeout expired" in hot[0]["reason"]


def test_tx_refresh_replaces_the_timeout(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.ask("^TX1;")
    time.sleep(0.6)
    bench.ask("^TX1;")                                     # refreshed before it ran out
    time.sleep(0.6)                                        # 1.2 s since the first ^TX1
    assert bench.amp.tr == "TX"
    assert "key_sw_expired" not in bench.kinds()


def test_key_in_is_parallel_with_network_ptt(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.amp.set_keyin(True)
    bench.ask("^TX;")
    assert bench.ask("^TQ;") == "^TQ3;"
    bench.ask("^RX;")
    assert bench.ask("^TQ;") == "^TQ2;"
    assert bench.amp.tr == "TX"                            # KEY IN still holds it keyed


def test_standby_never_moves_the_relays(bench):
    assert bench.ask("^OS;") == "^OS0;"
    bench.ask("^TX;")
    assert bench.ask("^TQ;") == "^TQ1;"                    # keyed, as far as ^TQ is concerned
    assert bench.amp.tr == "RX"                            # but STBY bypasses the PA
    bench.rf_on(50)
    assert bench.ask("^PWI;") == "^PWI0000;"               # "shown as 0 whenever the PA is bypassed"
    assert bench.ask("^PWF;") == "^PWF0050;"               # the exciter, straight through
    assert bench.hot() == []


def test_fault_drops_the_relays_under_rf(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.ask("^TX;")
    time.sleep(0.020)
    bench.rf_on(50)
    bench.amp.set_fault(0x20)                              # PA current fault mid-over
    assert bench.ask("^FL;") == "^FL20;"
    assert bench.ask("^OS;") == "^OS0;"                    # faults force STBY
    assert bench.amp.tr == "RX"
    assert len(bench.hot()) == 1
    bench.ask("^FLC;", expect_reply=False)
    assert bench.ask("^FL;") == "^FL00;"
    assert bench.ask("^OS;") == "^OS0;"                    # ^FLC does not change mode


def test_overdrive_faults_the_amp(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.ask("^TX;")
    time.sleep(0.020)
    bench.rf_on(150)
    assert bench.ask("^FL;") == "^FL60;"
    assert bench.amp.tr == "RX"


def test_over_temperature_clears_only_by_cooling(bench):
    bench.amp.set_temp(85)
    assert bench.ask("^FL;") == "^FL40;"
    bench.ask("^OS1;", expect_reply=False)
    assert bench.ask("^OS;") == "^OS0;"                    # refused while still hot
    bench.ask("^FLC;", expect_reply=False)
    assert bench.ask("^FL;") == "^FL40;"
    bench.amp.set_temp(55)
    assert bench.ask("^FL;") == "^FL00;"


def test_ni_get_and_set(bench):
    assert bench.ask("^NI;") == "^NI0;"                    # sim default: line ignored
    assert bench.ask("^NI1;", expect_reply=False) is None  # a SET: no reply
    assert bench.ask("^NI;") == "^NI1;"
    assert bench.ask("^NI2;", expect_reply=False) is None  # not a valid value: ignored
    assert bench.ask("^NI;") == "^NI1;"


def test_inhibit_line_is_ignored_while_ni_is_off(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.amp.set_inhibit_line(True)
    bench.ask("^TX;")
    assert bench.amp.tr == "TX"                            # ^NI0: the line does nothing
    assert "x00" in bench.ask("^VG;")


def test_inhibit_holds_the_amp_bypassed(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.ask("^NI1;", expect_reply=False)
    bench.amp.set_inhibit_line(True)
    bench.ask("^TX;")
    assert bench.ask("^TQ;") == "^TQ1;"                    # the key request stands...
    assert bench.amp.tr == "RX"                            # ...but the relays stay in RX
    vg = bench.ask("^VG;")
    assert "TRINHIBIT x04" in vg and "TR_STATE_RX" in vg
    assert int(bench.ask("^LQ;")[-3:-1], 16) & 0x01 == 0   # TX LED off
    bench.amp.set_inhibit_line(False)                      # released: the standing key takes
    assert bench.amp.tr == "TX"
    assert bench.hot() == []


def test_inhibit_asserted_mid_over_drops_the_relays(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.ask("^NI1;", expect_reply=False)
    bench.ask("^TX;")
    time.sleep(0.020)
    bench.rf_on(50)
    bench.amp.set_inhibit_line(True)                       # e.g. a sequencer pulling INHIBIT
    assert bench.amp.tr == "RX"
    hot = bench.hot()
    assert len(hot) == 1 and "ACC INHIBIT asserted" in hot[0]["reason"]


def test_enabling_ni_with_the_line_already_asserted_takes_effect(bench):
    bench.ask("^OS1;", expect_reply=False)
    bench.amp.set_inhibit_line(True)
    bench.ask("^TX;")
    assert bench.amp.tr == "TX"
    bench.ask("^NI1;", expect_reply=False)                 # now honoured: drops at once
    assert bench.amp.tr == "RX"


def test_old_firmware_has_no_network_ptt():
    b = Bench(firmware="02.03", operate=True)
    try:
        assert b.ask("^RVM;") == "^RVM02.03;"
        assert b.ask("^TX;", expect_reply=False) is None
        assert b.ask("^TQ;", expect_reply=False) is None
        assert b.amp.tr == "RX"
    finally:
        b.close()


def test_cycle_sequences_its_overs_cleanly():
    b = Bench(operate=True)
    try:
        b.amp.start_cycle(40)
        wait_for(lambda: b.amp.overs >= 1 and b.amp.last_tail_ms is not None,
                 "one full cycled over", timeout=5)
        b.amp.stop_cycle()
        assert b.hot() == []
        assert b.amp.min_lead_ms >= 15
    finally:
        b.amp.stop_cycle()
        b.close()


def test_control_page_json():
    amp = kpa_sim.Amp()
    srv = kpa_sim.start_control_server(amp, port=0, host="127.0.0.1")
    port = srv.server_address[1]
    try:
        def get(path):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
                return r.read()
        assert b"KPA1500" in get("/")
        json.loads(get("/cmd?c=oper+1"))
        json.loads(get("/cmd?c=tx"))
        s = json.loads(get("/state"))
        assert s["operate"] and s["tr"] == "TX" and s["tq"] == 1
        evs = json.loads(get("/events?since=0"))
        assert [e["kind"] for e in evs][-2:] == ["key_sw", "tr"]
    finally:
        srv.shutdown()


# ---- end to end: flex_sim.py --rf-sense -> kpa_sim ---------------------------------

FLEX_PORT, FLEX_CTL = 5995, 8735


def test_flex_sim_rf_sense_end_to_end():
    """AE's order of operations, reproduced: key the amp, then key the radio.

    Uses the radio's real 'transmit set mox=' path, so the RF edge the amp sees is
    the one AE would cause. Skips if a flex_sim cannot be spawned here.
    """
    b = Bench()
    sim = subprocess.Popen(
        [sys.executable, str(ROOT / "flex_sim.py"), "--port", str(FLEX_PORT),
         "--ctl-port", str(FLEX_CTL), "--rf-sense", f"127.0.0.1:{b.rf.port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT),
        env={**os.environ, "FLEXSIM_DAXTX_PORT": "4995"})
    try:
        host_ip = None
        t0 = time.time()
        while time.time() - t0 < 15 and host_ip is None:
            line = sim.stdout.readline().decode(errors="replace")
            if not line and sim.poll() is not None:
                break
            if "radio ip " in line:
                host_ip = line.split("radio ip ", 1)[1].split(",")[0].split(")")[0].strip()
        if not host_ip:
            pytest.skip("flex_sim did not start (ports busy?)")
        radio = socket.create_connection((host_ip, FLEX_PORT), timeout=5)
        radio.settimeout(0.3)
        seq = [0]

        def flex(cmd):
            seq[0] += 1
            radio.sendall(f"C{seq[0]}|{cmd}\n".encode())
            try:
                radio.recv(8192)
            except socket.timeout:
                pass

        flex("transmit set rfpower=40")
        b.ask("^OS1;", expect_reply=False)

        # Right order: amp first, radio after a proper lead.
        b.ask("^TX;")
        time.sleep(0.030)
        flex("transmit set mox=1")
        wait_for(lambda: b.amp.rf_present, "the radio's RF edge at the amp", timeout=3)
        assert b.amp.rf_w == 40
        flex("transmit set mox=0")
        wait_for(lambda: not b.amp.rf_present, "the radio's RF-off edge", timeout=3)
        time.sleep(0.010)
        b.ask("^RX;")
        assert b.hot() == [], b.hot()
        assert b.amp.last_lead_ms >= 25

        # Wrong order: radio first, then the amp. Exactly the #4097 hazard.
        flex("transmit set mox=1")
        wait_for(lambda: b.amp.rf_present, "RF before the key", timeout=3)
        b.ask("^TX;")
        hot = b.hot()
        assert len(hot) == 1 and "RF led the key" in hot[0]["reason"]
        flex("transmit set mox=0")
    finally:
        sim.kill()
        b.close()
