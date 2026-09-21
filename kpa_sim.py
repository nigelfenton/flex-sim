#!/usr/bin/env python3
# kpa_sim.py — Elecraft KPA1500 amplifier simulator for AetherSDR, with network PTT.
#
# Desktop port of the KPA persona in the Giga R1 dual-amp sketch
# (shack-experiments kpa1500/dual_amp_simulator), rebuilt against Elecraft's
# KPA1500 Programming Reference V3 (revised 6/1/2026 for firmware 03.0) rather
# than the Rev 2.03 the sketch was written from. Built for AetherSDR #4097
# (native KPA1500 network integration), whose open question is whether the amp
# can be keyed safely over the LAN.
#
# WHAT IS MODELLED
#   - The ASCII command set on TCP :1500 (one client) and on UDP :1500 (one
#     command per datagram, at most one response), as the reference describes.
#   - Network PTT, new in firmware 3.07: ^TX; / ^TXnn; key the amp in software,
#     in parallel with the rear-panel KEY IN line; ^RX; cancels; ^TQ; reports the
#     key status; ^TXnn; expires after nn seconds. That expiry is the watchdog
#     the reference tells control software to rely on ("send something like
#     ^TX60; periodically while key down is needed").
#   - The T/R relays, and whether they ever move with RF on them.
#   - The ACC connector INHIBIT line and ^NI; / ^NIx;, which says whether the
#     amp honours it: "1 (enabled) to use the INHIBIT line to keep the KPA1500
#     amplifier bypassed". Enabled and asserted, it holds the relays in RX like
#     STBY does, and asserting it mid-over drops them at once.
#   - The internal ATU: ^AM; / ^AMI; / ^AMB; (mode, Inline or Bypassed), ^AI; /
#     ^AI0; / ^AI1; (the bypass relays), ^AE; / ^AEn; / ^AEbb; / ^AEbbn; (which
#     antenna connectors are enabled: 0 both, 1 ANT1, 2 ANT2), ^FT; (full-search
#     tune), ^FE; (cancel) and ^TP; (tune in progress). A tune needs exciter RF to
#     make progress, as the reference says, and when it completes or is cancelled
#     the amp sends ^FT; unprompted, to the TCP client. The SWR the PA sees is the
#     tuned match with the ATU inline, otherwise the antenna's own SWR.
#
# THE INTERLOCK MODEL — what "hot switching" means here
#   The reference: "The KPA1500 requires approximately 5 milliseconds of ^TX or
#   KEY IN lead time before exciter RF arrives. Exciter RF should be removed
#   before issuing ^RX;". So the relays take ~5 ms to travel and must never move
#   while RF is present. This sim records a HOT SWITCH whenever:
#     1. the relays switch to TX while exciter RF is already present
#        (RF led the key: the exciter was keyed first);
#     2. RF arrives less than --relay-ms after the relays started moving to TX
#        (the key led the RF, but not by enough);
#     3. the relays drop back to RX while RF is still present (^RX, a ^TXnn
#        watchdog expiry, a KEY IN release, a fault, STBY or power-off while the
#        exciter is still transmitting).
#   ^TR (0-50 ms) delays the return to RX after a key RELEASE, as documented. A
#   fault, STBY or power-off drops the relays immediately.
#   Every key edge, relay movement and RF edge is timestamped in an event log,
#   with the measured LEAD (relays started moving -> RF on) and TAIL (RF off ->
#   relays back to RX), so a controller's sequencing is measured, not guessed.
#
# WHERE THE RF COMES FROM
#   A real amp sees its exciter's RF on the coax; this sim has to be told:
#     - UDP --rf-sense-port (default 1510): "RF 1 <watts>" / "RF 0" datagrams.
#       flex_sim.py --rf-sense 127.0.0.1:1510 sends one on every TX edge that AE
#       causes, so "AE sends ^TX, then keys the radio" is measured end to end.
#     - the prompt / control page: "rf <watts>", "rf 0".
#     - "cycle 1": an internal exciter that sequences an over correctly (KEY IN,
#       lead, ramp up, hold, ramp down, tail, release). This replaces the Giga
#       sketch's meter-only animation.
#
# DELIBERATE DEPARTURES FROM THE GIGA SKETCH (it was never checked against a
# real amp; these follow the V3 reference instead):
#   - SET commands get NO response ("SET commands do not generally result in a
#     RESPONSE message"). Only ^TX; and ^RX; answer, as documented. The sketch
#     echoed every SET.
#   - ^AN; answers "^ANn;" with the caret. The sketch sent "AN1;".
#   - ^ON is main POWER (^ON0; / ^ON1; / ^ON/;), not operate. The sketch
#     mirrored operate. With power off, only ;, ^I, ^SN and ^ON answer.
#   - ^FLC; clears the fault without changing mode. ^OS1; also clears any fault
#     except over-temperature (0x40), which clears when the temperature drops.
#
# SIM-DEFINED, NOT FROM THE REFERENCE (say so if a test depends on one):
#   - Hot switching is not a KPA1500 fault code. The sim logs it; it does not
#     fault the amp.
#   - ^TQ: the reference's table contradicts itself (its rows 2 and 3 both say
#     "^TX not expired"). Implemented as a bitmask: 1 = ^TX active, 2 = KEY IN
#     pulled down, 3 = both.
#   - ^VG TRINHIBIT bits: x01 = STANDBY (as in the reference's example),
#     x02 = fault, x04 = ACC INHIBIT. The real amp's bits are undocumented.
#   - The ATU: a tune takes 1.5 s of RF, always finds SWR 1.1, and leaves the
#     relays bypassed if the antenna is already better than 1.5:1. Antenna SWR
#     defaults to 1.8. Only the V1-style ^AE and ^AM forms (current band, or ^AEbb)
#     are implemented, not the 3.00 per-antenna forms. The reference gives ^AI's
#     bypassed reply as "^AT0;", which reads as a typo for ^AI0;, so the sim sends
#     ^AI0; - worth confirming on a real amp, since a client that follows the
#     reference literally would look for AT.
#   - ^NI defaults to 0 (INHIBIT line ignored); the real default is not stated.
#     The reference's heading calls the command ^NH, but its GET and SET formats
#     both say ^NI, so ^NI is what is implemented. No command reads the INHIBIT
#     line's own state; the sim exposes it on the prompt and page only.
#   - ^LQ bar thresholds (power: 31 LEDs linear to max power; SWR: one LED per
#     0.1 above 1.0). The status-LED bits are the reference's.
#   - Newest TCP connection wins, as in the sketch. The reference says only
#     that a single TCP client may be active.
#   - Gain 25x (60 W in for 1500 W out), 60% PA efficiency, input over 100 W
#     faults 0x60, heat sink at 80 C faults 0x40 and clears at 60 C.
#   - ^TX survives power-off: the reference lists only ^RX, the timeout, the
#     RESET button and ^BPT10; as removing it. So a ^TX; with no timeout, left
#     active across a power cycle, keys the amp the moment it goes to OPER.
#   - --firmware below 03.07 makes ^TX/^RX/^TQ unknown (no response), so a
#     client's firmware-version gating can be tested.
#
# Run:   python3 kpa_sim.py               # :1500 TCP+UDP, control page :8737,
#                                         # RF sense :1510, interactive prompt
#        python3 kpa_sim.py --no-cli      # headless
#        python3 kpa_sim.py --selftest
# Then point the client at <this-host>:1500, and open http://<this-host>:8737/.

import argparse
import collections
import datetime
import json
import re
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KPA_PORT = 1500
CTL_PORT = 8737
RF_SENSE_PORT = 1510
DEFAULT_FIRMWARE = "03.07"
PTT_FIRMWARE = (3, 7)          # ^TX / ^RX / ^TQ are "new in 3.07"
TOGGLE_FIRMWARE = (3, 2)       # ^ON/; is "new in version 03.02"
RELAY_MS = 5.0                 # the reference's "approximately 5 milliseconds"
RF_THRESHOLD_W = 1.0           # below this there is "no RF" for hot-switch purposes
DEFAULT_DRIVE_W = 50.0         # "RF 1" with no watts given
GAIN = 25.0
EFFICIENCY = 0.60
MAX_DRIVE_W = 100.0
TEMP_FAULT_C = 80
TEMP_CLEAR_C = 60
TUNE_RF_S = 1.5                # seconds of exciter RF a full-search tune takes
TUNE_STOP_SWR = 1.5            # antenna already this good: the tune leaves the ATU bypassed
TUNED_SWR = 1.1                # the match a tune finds

# Every timestamp comes from perf_counter, NOT time.monotonic(): on Windows
# monotonic() ticks only every ~15.6 ms, which cannot resolve a 5 ms relay window
# (a 10 ms tail measured as 0.0 in 1 run in 4). perf_counter is monotonic too.
_clock = time.perf_counter

# Fault codes, from the reference's ^FL table.
FAULTS = {
    0x00: "No fault",
    0x10: "Watchdog timer reset",
    0x20: "PA current too high",
    0x40: "Temperature too high",
    0x60: "Input power too high",
    0x61: "Gain too low",
    0x70: "Invalid frequency",
    0x80: "50V supply out of range",
    0x81: "5V supply out of range",
    0x82: "10V supply out of range",
    0x83: "12V supply out of range",
    0x84: "-12V supply out of range",
    0x85: "5V or 400V LPF supply not detected",
    0x90: "Reflected power too high",
    0x91: "SWR very high (antenna not connected?)",
    0x92: "ATU could not find a match",
    0xB0: "Dissipated power too high",
}


def _fw_tuple(fw):
    return tuple(int(x) for x in fw.split("."))


class Amp:
    def __init__(self, firmware=DEFAULT_FIRMWARE, relay_ms=RELAY_MS,
                 serial="00123", max_power=1500, operate=False):
        self.lock = threading.RLock()
        self.t0 = _clock()
        self.firmware = firmware
        self.fw = _fw_tuple(firmware)
        self.relay_ms = float(relay_ms)
        self.serial = serial
        self.max_power = int(max_power)
        # panel and configuration state
        self.powered = True
        self.operate = operate
        self.fault = 0
        self.band = 5              # 20 m
        self.antenna = 1
        # ATU. The SWR the PA sees depends on it: with the ATU inline (mode I and
        # relays in) it is the tuned match; otherwise the antenna's own SWR.
        self.atu_mode = "I"        # ^AM: "I" inline or "B" bypassed
        self.atu_inline = True     # ^AI: the bypass relays (can be out with mode I)
        self.ant_swr = 1.8         # antenna SWR with the ATU bypassed
        self.tuned_swr = 1.1       # SWR through the ATU after a tune
        self.ae = {}               # ^AE per band: 0 = ANT1+ANT2, 1 = ANT1, 2 = ANT2
        self.tuning = False
        self._tune_rf_s = 0.0
        self.push = []             # callables that deliver unsolicited replies (^FT;)
        self.temp = 25
        self.pa_voltage = 52.0     # V
        self.fan_min = 0
        self.tr_delay_ms = 0
        self.ip = "0.0.0.0"
        # keying
        self.sw_tx = False
        self.sw_deadline = None    # monotonic time ^TXnn; expires, or None
        self._sw_gen = 0
        self.keyin = False
        self.ni_enabled = False    # ^NI: honour the ACC INHIBIT line?
        self.inhibit_line = False  # the ACC connector INHIBIT input itself
        self.tr = "RX"             # where the T/R relays are
        self.tr_at = self.t0       # when they last started moving
        self._rx_gen = 0
        # exciter RF at the input
        self.rf_w = 0.0
        self.rf_at = None
        self.rf_off_at = None
        # measurement
        self.events = collections.deque(maxlen=5000)
        self.n = 0
        self.reset_counters()
        # internal exciter ("cycle")
        self.cycling = False
        self.cycle_drive = DEFAULT_DRIVE_W

    # ---- event log -------------------------------------------------------
    def reset_counters(self):
        with self.lock:
            self.events.clear()
            self.hot_switches = 0
            self.overs = 0
            self.last_lead_ms = None
            self.min_lead_ms = None
            self.last_tail_ms = None

    def _ms(self, t):
        return round((t - self.t0) * 1000.0, 3)

    def _event(self, now, kind, **kw):
        self.n += 1
        ev = {"n": self.n, "t_ms": self._ms(now),
              "wall": datetime.datetime.now().isoformat(timespec="milliseconds"),
              "kind": kind, **kw}
        self.events.append(ev)
        if kind == "HOT_SWITCH":
            print(f"[kpa] !! HOT SWITCH: {kw.get('reason')}", flush=True)
        elif kind in ("tr", "key_sw", "key_sw_expired", "keyin", "fault", "mode", "power"):
            detail = " ".join(f"{k}={v}" for k, v in kw.items())
            print(f"[kpa] {kind} {detail}", flush=True)
        return ev

    def _hot(self, now, reason, **kw):
        self.hot_switches += 1
        self._event(now, "HOT_SWITCH", reason=reason, rf_w=round(self.rf_w, 1), **kw)

    # ---- derived state -----------------------------------------------------
    @property
    def ptt_supported(self):
        return self.fw >= PTT_FIRMWARE

    @property
    def rf_present(self):
        return self.rf_w >= RF_THRESHOLD_W

    @property
    def key_request(self):
        return self.sw_tx or self.keyin

    @property
    def atu_in_line(self):
        return self.atu_mode == "I" and self.atu_inline

    @property
    def swr(self):
        """The SWR the PA sees: the tuned match through the ATU, else the antenna."""
        return self.tuned_swr if self.atu_in_line else self.ant_swr

    @swr.setter
    def swr(self, value):
        if self.atu_in_line:
            self.tuned_swr = value
        else:
            self.ant_swr = value

    @property
    def inhibited(self):
        return self.ni_enabled and self.inhibit_line

    @property
    def want_tx(self):
        return (self.powered and self.operate and self.fault == 0 and not self.inhibited
                and self.key_request)

    def tq(self):
        return (1 if self.sw_tx else 0) | (2 if self.keyin else 0)

    def telemetry(self):
        """Meter readings as the amp would report them right now."""
        with self.lock:
            # "Input power is shown as 0 whenever the amplifier's PA is bypassed
            # (when in mode STBY, or during ATU tuning)."
            pa = self.tr == "TX" and not self.tuning
            rf = self.rf_w if self.rf_present else 0.0
            fwd = min(float(self.max_power), rf * GAIN) if pa else rf
            gamma = (self.swr - 1.0) / (self.swr + 1.0)
            dc_in = fwd / EFFICIENCY if pa else 0.0
            return {
                "fwd_w": fwd,
                "ref_w": fwd * gamma * gamma,
                "input_w": rf if pa else 0.0,      # "shown as 0 whenever the PA is bypassed"
                "pa_current_a": dc_in / self.pa_voltage,
                "dissipated_w": max(0.0, dc_in - fwd),
                "fan": max(self.fan_min, 3 if pa else 0),
            }

    # ---- the T/R relays ------------------------------------------------------
    def _relay_to(self, pos, cause, now):
        if self.tr == pos:
            return
        self.tr = pos
        self.tr_at = now
        self._event(now, "tr", to=pos, cause=cause)
        if pos == "TX":
            self.overs += 1
            if self.rf_present:
                lead = -(now - self.rf_at) * 1000.0 if self.rf_at else 0.0
                self._record_lead(round(lead, 3))
                self._hot(now, f"relays switched to TX with {self.rf_w:.0f} W already "
                               f"present: RF led the key by {-lead:.1f} ms ({cause})",
                          lead_ms=round(lead, 3))
        else:
            if self.rf_present:
                self._hot(now, f"relays dropped to RX with {self.rf_w:.0f} W still "
                               f"present ({cause})")
            elif self.rf_off_at is not None and self.rf_at is not None \
                    and self.rf_off_at >= self.rf_at:
                tail = round((now - self.rf_off_at) * 1000.0, 3)
                self.last_tail_ms = tail
                self._event(now, "tail", ms=tail)

    def _record_lead(self, lead):
        self.last_lead_ms = lead
        if self.min_lead_ms is None or lead < self.min_lead_ms:
            self.min_lead_ms = lead

    def _evaluate(self, now, cause, release=False):
        if self.want_tx:
            self._rx_gen += 1                      # cancel any pending ^TR hold
            self._relay_to("TX", cause, now)
        elif self.tr == "TX":
            if release and self.tr_delay_ms > 0 and self.powered and self.operate \
                    and self.fault == 0:
                self._rx_gen += 1
                gen = self._rx_gen
                t = threading.Timer(self.tr_delay_ms / 1000.0, self._deferred_rx,
                                    args=(gen, cause))
                t.daemon = True
                t.start()
            else:
                self._relay_to("RX", cause, now)

    def _deferred_rx(self, gen, cause):
        with self.lock:
            if gen != self._rx_gen or self.want_tx or self.tr != "TX":
                return
            self._relay_to("RX", f"{cause}, after ^TR {self.tr_delay_ms} ms",
                           _clock())

    # ---- keying --------------------------------------------------------------
    def sw_key(self, timeout_s=None, who="?"):
        """^TX; (no timeout) or ^TXnn;. Replaces any prior timeout."""
        with self.lock:
            now = _clock()
            self._sw_gen += 1
            gen = self._sw_gen
            self.sw_tx = True
            self.sw_deadline = now + timeout_s if timeout_s else None
            if timeout_s:
                t = threading.Timer(timeout_s, self._sw_expire, args=(gen,))
                t.daemon = True
                t.start()
            self._event(now, "key_sw", on=True, timeout_s=timeout_s, by=who)
            self._evaluate(now, "^TX" if not timeout_s else f"^TX{timeout_s}")

    def sw_unkey(self, who="?", cause="^RX"):
        with self.lock:
            now = _clock()
            self._sw_gen += 1
            was = self.sw_tx
            self.sw_tx = False
            self.sw_deadline = None
            if was:
                self._event(now, "key_sw", on=False, by=who, cause=cause)
            self._evaluate(now, cause, release=True)

    def _sw_expire(self, gen):
        with self.lock:
            if gen != self._sw_gen or not self.sw_tx:
                return
            now = _clock()
            self.sw_tx = False
            self.sw_deadline = None
            self._event(now, "key_sw_expired")
            self._evaluate(now, "^TX timeout expired", release=True)

    def set_keyin(self, on):
        """The rear-panel KEY IN line."""
        with self.lock:
            now = _clock()
            if self.keyin == bool(on):
                return
            self.keyin = bool(on)
            self._event(now, "keyin", on=self.keyin)
            self._evaluate(now, "KEY IN " + ("down" if on else "released"),
                           release=not on)

    # ---- the ATU ------------------------------------------------------------
    def _push(self, text):
        for deliver in list(self.push):
            try:
                deliver(text)
            except OSError:
                pass

    def start_tune(self, who="?"):
        """^FT; a full-search tune. It needs exciter RF to make progress; when it
        completes or is cancelled the amp sends ^FT; unprompted."""
        with self.lock:
            if self.tuning:
                return
            self.tuning = True
            self._tune_rf_s = 0.0
            self._event(_clock(), "tune", state="started", by=who)
        threading.Thread(target=self._tune_loop, daemon=True).start()

    def cancel_tune(self, who="?"):
        """^FE;"""
        with self.lock:
            if not self.tuning:
                return
            self.tuning = False
            self._event(_clock(), "tune", state="cancelled", by=who)
        self._push("^FT;")

    def _tune_loop(self):
        last = _clock()
        while True:
            time.sleep(0.02)
            with self.lock:
                if not self.tuning:
                    return
                now = _clock()
                if self.rf_present:
                    self._tune_rf_s += now - last
                last = now
                if self._tune_rf_s < TUNE_RF_S:
                    continue
                self.tuning = False
                self.atu_mode = "I"
                # "the SWR of the antenna without the ATU is sufficiently low"
                # leaves the relays bypassed; otherwise the ATU goes inline.
                self.atu_inline = self.ant_swr > TUNE_STOP_SWR
                self.tuned_swr = min(self.ant_swr, TUNED_SWR)
                self._event(now, "tune", state="done", inline=self.atu_inline,
                            swr=round(self.swr, 1))
            self._push("^FT;")
            return

    def set_inhibit_line(self, on):
        """The ACC connector INHIBIT input. Only acts while ^NI1 is set."""
        with self.lock:
            now = _clock()
            if self.inhibit_line == bool(on):
                return
            self.inhibit_line = bool(on)
            self._event(now, "inhibit_line", on=self.inhibit_line,
                        honoured=self.ni_enabled)
            self._evaluate(now, "ACC INHIBIT " + ("asserted" if on else "released"))

    def set_ni(self, enabled, who="?"):
        """^NIx;: whether the INHIBIT line keeps the amplifier bypassed."""
        with self.lock:
            now = _clock()
            if self.ni_enabled == bool(enabled):
                return
            self.ni_enabled = bool(enabled)
            self._event(now, "ni", enabled=self.ni_enabled, by=who)
            self._evaluate(now, "^NI" + ("1" if enabled else "0"))

    # ---- modes, power, faults --------------------------------------------------
    def set_operate(self, on, who="?"):
        with self.lock:
            now = _clock()
            if on:
                if self.fault == 0x40:
                    self._event(now, "mode_refused",
                                reason="over-temperature fault clears only by cooling")
                    return
                if self.fault:
                    self._event(now, "fault_cleared", code=f"{self.fault:02X}", by="^OS1")
                    self.fault = 0
            if self.operate == bool(on):
                return
            self.operate = bool(on)
            self._event(now, "mode", mode="OPER" if on else "STBY", by=who)
            self._evaluate(now, "OPER" if on else "STBY")

    def set_power(self, on):
        with self.lock:
            now = _clock()
            if self.powered == bool(on):
                return
            self.powered = bool(on)
            if not on:
                self.operate = False
            self._event(now, "power", on=self.powered)
            self._evaluate(now, "power " + ("on" if on else "off"))

    def reset_button(self, who="?"):
        """RESET tap (^BPT10;): removes ^TX."""
        self.sw_unkey(who=who, cause="RESET")

    def set_fault(self, code, why="injected"):
        with self.lock:
            now = _clock()
            self.fault = code & 0xFF
            if self.fault:
                self.operate = False               # "Faults cause the KPA1500 to switch to Mode STBY"
                self._event(now, "fault", code=f"{self.fault:02X}",
                            name=FAULTS.get(self.fault, "?"), why=why)
                self._evaluate(now, f"fault {self.fault:02X}")
            else:
                self._event(now, "fault_cleared", code="00", by=why)

    def clear_fault(self, who="^FLC"):
        with self.lock:
            now = _clock()
            if self.fault == 0x40 and self.temp > TEMP_CLEAR_C:
                self._event(now, "fault_clear_refused",
                            reason="temperature faults are cleared by temperature change")
                return
            if self.fault:
                self._event(now, "fault_cleared", code=f"{self.fault:02X}", by=who)
            self.fault = 0                         # ^FLC; does not change Mode

    def set_temp(self, c):
        with self.lock:
            self.temp = int(c)
            if self.temp >= TEMP_FAULT_C and self.fault == 0:
                self.set_fault(0x40, why=f"heat sink {self.temp} C")
            elif self.fault == 0x40 and self.temp <= TEMP_CLEAR_C:
                self._event(_clock(), "fault_cleared", code="40",
                            by=f"cooled to {self.temp} C")
                self.fault = 0

    # ---- exciter RF at the input ------------------------------------------------
    def set_rf(self, watts, source="?"):
        with self.lock:
            now = _clock()
            was = self.rf_present
            self.rf_w = max(0.0, float(watts))
            if not was and self.rf_present:
                self.rf_at = now
                path = "PA" if self.tr == "TX" else "bypass"
                self._event(now, "rf", on=True, w=round(self.rf_w, 1), source=source,
                            path=path)
                if self.tr == "TX":
                    lead = round((now - self.tr_at) * 1000.0, 3)
                    self._record_lead(lead)
                    if lead < self.relay_ms:
                        self._hot(now, f"RF arrived {lead:.2f} ms after the relays started "
                                       f"moving to TX; they need {self.relay_ms:g} ms",
                                  lead_ms=lead)
                    else:
                        self._event(now, "lead", ms=lead)
            elif was and not self.rf_present:
                self.rf_off_at = now
                self._event(now, "rf", on=False, source=source)
            if self.tr == "TX" and self.rf_w > MAX_DRIVE_W and self.fault == 0:
                self.set_fault(0x60, why=f"{self.rf_w:.0f} W drive > {MAX_DRIVE_W:.0f} W")

    # ---- the command set ----------------------------------------------------
    def handle(self, raw, who="?"):
        """One command, e.g. '^PWF;'. Returns the response text, or None."""
        c = raw.strip().upper()
        if not c.endswith(";"):
            return None
        if c == ";":                               # null command: wakeup / link test
            return ";"
        with self.lock:
            # Answered even while the main supplies are off ("sleeping").
            if c == "^I;":
                return "^KPA1500;"
            if c == "^SN;":
                return f"^SN{self.serial};"
            if c == "^ON;":
                return f"^ON{1 if self.powered else 0};"
            if c in ("^ON0;", "^ON1;"):
                self.set_power(c == "^ON1;")
                return None
            if c == "^ON/;" and self.fw >= TOGGLE_FIRMWARE:
                self.set_power(not self.powered)
                return None
            if not self.powered:
                return None

            if c == "^RV;":
                return f"^RV{self.firmware};"
            if c == "^RVM;":
                return f"^RVM{self.firmware};"

            # ---- network PTT (3.07+) ----
            if c.startswith("^TX") or c in ("^RX;", "^TQ;"):
                if not self.ptt_supported:
                    return None
                if c == "^TQ;":
                    return f"^TQ{self.tq()};"
                if c == "^RX;":
                    self.sw_unkey(who=who)
                    return "^RX;"
                m = re.fullmatch(r"\^TX(\d{0,2});", c)
                if not m:
                    return None
                secs = int(m.group(1)) if m.group(1) else None
                if secs is not None and not 1 <= secs <= 99:
                    return None
                self.sw_key(secs, who=who)
                return "^TX;"
            if c == "^BPT10;":
                self.reset_button(who=who)
                return None

            t = self.telemetry()
            swr10 = min(999, int(round(self.swr * 10)))
            if c == "^WS;":
                return f"^WS{min(9999, int(t['fwd_w'])):04d} {swr10:03d};"
            if c == "^PWF;":
                return f"^PWF{min(9999, int(t['fwd_w'])):04d};"
            if c == "^PWR;":
                return f"^PWR{min(9999, int(t['ref_w'])):04d};"
            if c == "^PWI;":
                return f"^PWI{min(9999, int(t['input_w'])):04d};"
            if c == "^PWD;":
                return f"^PWD{min(9999, int(t['dissipated_w'])):04d};"
            if c == "^SW;":
                return f"^SW{swr10:03d};"
            if c == "^TM;":
                return f"^TM{max(0, min(999, self.temp)):03d};"
            if c == "^VI;":
                return (f"^VI{int(round(self.pa_voltage * 10)):03d} "
                        f"{min(999, int(t['pa_current_a'])):03d};")
            if c == "^PC;":
                return f"^PC{min(999, int(t['pa_current_a'])):03d};"
            if c == "^FS;":
                return f"^FS{t['fan']};"
            if c == "^FL;":
                return f"^FL{self.fault:02X};"
            if c == "^FLC;":
                self.clear_fault(who=who)
                return None
            if c == "^OS;":
                return f"^OS{1 if self.operate else 0};"
            if c in ("^OS0;", "^OS1;"):
                self.set_operate(c == "^OS1;", who=who)
                return None
            if c == "^BN;":
                return f"^BN{self.band:02d};"
            m = re.fullmatch(r"\^BN(\d\d);", c)
            if m:
                if 0 <= int(m.group(1)) <= 10:
                    self.band = int(m.group(1))
                return None
            if c == "^AN;":
                return f"^AN{self.antenna};"
            if c in ("^AN0;", "^AN00;", "^AN+;"):
                self.antenna = 2 if self.antenna == 1 else 1
                return None
            m = re.fullmatch(r"\^AN(\d{1,2});", c)
            if m:
                if int(m.group(1)) in (1, 2):      # ANT1 / ANT2 on the rear panel
                    self.antenna = int(m.group(1))
                return None
            if c == "^TR;":
                return f"^TR{self.tr_delay_ms:02d};"
            m = re.fullmatch(r"\^TR(\d\d);", c)
            if m:
                if 0 <= int(m.group(1)) <= 50:
                    self.tr_delay_ms = int(m.group(1))
                return None
            # ---- ATU ----
            if c == "^AM;":
                return f"^AM{self.atu_mode};"
            if c in ("^AMI;", "^AMB;"):
                self.atu_mode = c[3]
                self.atu_inline = self.atu_mode == "I"
                return None
            if c == "^AI;":
                return f"^AI{1 if self.atu_inline else 0};"
            if c in ("^AI0;", "^AI1;"):
                self.atu_inline = c == "^AI1;"
                return None
            if c == "^AE;":
                return f"^AE{self.ae.get(self.band, 0)};"
            if c in ("^AE0;", "^AE1;", "^AE2;"):
                self.ae[self.band] = int(c[3])
                return None
            m = re.fullmatch(r"\^AE(\d\d)([012]?);", c)
            if m and 0 <= int(m.group(1)) <= 10:
                bb = int(m.group(1))
                if not m.group(2):
                    return f"^AE{bb:02d}{self.ae.get(bb, 0)};"
                self.ae[bb] = int(m.group(2))
                return None
            if c == "^FT;":
                self.start_tune(who=who)
                return None                        # the reply comes when the tune ends
            if c == "^FE;":
                self.cancel_tune(who=who)
                return None
            if c == "^TP;":
                return f"^TP{1 if self.tuning else 0};"
            if c == "^NI;":
                return f"^NI{1 if self.ni_enabled else 0};"
            if c in ("^NI0;", "^NI1;"):
                self.set_ni(c == "^NI1;", who=who)
                return None
            if c == "^IP;":
                return f"^IP {self.ip};"
            if c == "^LQ;":
                return f"^LQ{self._lq(t)};"
            if c == "^VG;":
                return self._vg()
        return None

    def _lq(self, t):
        tx = self.tr == "TX"
        n = min(31, int(round(31 * t["fwd_w"] / self.max_power))) if tx else 0
        power = (1 << n) - 1
        if tx and t["fwd_w"] > 0:
            leds = 1 + min(9, int(round((self.swr - 1.0) * 10)))
            swr = (1 << leds) - 1
        else:
            swr = 0
        mm = ((0x80 if self.fault else 0) | (0x20 if self.antenna == 2 else 0x10)
              # ATU IN follows the mode; ATU BYP the relays. With mode I and the
              # relays bypassed "both ATU LEDs are illuminated", per ^AI.
              | (0x08 if self.atu_mode == "I" else 0)
              | (0x04 if not self.atu_in_line else 0) | (0x02 if self.operate else 0)
              | (0x01 if tx else 0))
        return f"{power:08X}{swr:04X}{mm:02X}"

    def _vg(self):
        tx = self.tr == "TX"
        inhibit = ((0x01 if not self.operate else 0) | (0x02 if self.fault else 0)
                   | (0x04 if self.inhibited else 0))
        return (f"^VG TRINHIBIT x{inhibit:02X} TR_STATE_{'TX' if tx else 'RX'} "
                f"3R:{0 if tx else 1} 3T:{1 if tx else 0} Bias:{1 if tx else 0} "
                f"PA {'OPER' if self.operate else 'STBY'} KeyIn:{1 if self.keyin else 0}"
                f"{'' if self.operate else ' STANDBY'};")

    # ---- readback ----------------------------------------------------------
    def state(self):
        with self.lock:
            t = self.telemetry()
            left = None
            if self.sw_deadline is not None:
                left = round(max(0.0, self.sw_deadline - _clock()), 2)
            return {
                "firmware": self.firmware, "ptt_supported": self.ptt_supported,
                "powered": self.powered, "operate": self.operate,
                "fault": f"{self.fault:02X}", "fault_name": FAULTS.get(self.fault, "?"),
                "band": self.band, "antenna": self.antenna, "temp_c": self.temp,
                "swr": self.swr, "ant_swr": self.ant_swr, "atu_mode": self.atu_mode,
                "atu_inline": self.atu_inline, "tuning": self.tuning,
                "antenna_enable": self.ae.get(self.band, 0),
                "tr": self.tr, "tr_delay_ms": self.tr_delay_ms,
                "relay_ms": self.relay_ms,
                "sw_tx": self.sw_tx, "sw_timeout_left_s": left,
                "keyin": self.keyin, "tq": self.tq(),
                "ni_enabled": self.ni_enabled, "inhibit_line": self.inhibit_line,
                "inhibited": self.inhibited,
                "rf_w": round(self.rf_w, 1),
                "fwd_w": round(t["fwd_w"], 1), "ref_w": round(t["ref_w"], 1),
                "input_w": round(t["input_w"], 1),
                "pa_current_a": round(t["pa_current_a"], 1),
                "dissipated_w": round(t["dissipated_w"], 1),
                "overs": self.overs, "hot_switches": self.hot_switches,
                "last_lead_ms": self.last_lead_ms, "min_lead_ms": self.min_lead_ms,
                "last_tail_ms": self.last_tail_ms, "cycling": self.cycling,
                "events": self.n,
            }

    def events_since(self, since=0):
        with self.lock:
            return [e for e in self.events if e["n"] > since]

    # ---- internal exciter -----------------------------------------------------
    def start_cycle(self, drive=None):
        with self.lock:
            if drive:
                self.cycle_drive = float(drive)
            if self.cycling:
                return
            self.cycling = True
        threading.Thread(target=self._cycle_loop, daemon=True).start()

    def stop_cycle(self):
        self.cycling = False

    def _cycle_loop(self):
        """One correctly sequenced over after another: key, lead, ramp, hold,
        ramp down, tail, release."""
        try:
            while self.cycling:
                self.set_keyin(True)
                time.sleep(0.020)                  # 20 ms lead, 4x the documented 5 ms
                for i in range(1, 11):
                    self.set_rf(self.cycle_drive * i / 10, source="cycle")
                    time.sleep(0.04)
                t_end = _clock() + 1.0
                while self.cycling and _clock() < t_end:
                    time.sleep(0.05)
                for i in range(9, -1, -1):
                    self.set_rf(self.cycle_drive * i / 10, source="cycle")
                    time.sleep(0.04)
                time.sleep(0.020)                  # 20 ms tail before release
                self.set_keyin(False)
                t_end = _clock() + 1.5
                while self.cycling and _clock() < t_end:
                    time.sleep(0.05)
        finally:
            self.set_rf(0, source="cycle")         # RF off FIRST, then release
            time.sleep(0.020)
            self.set_keyin(False)


# ---- network: TCP + UDP :1500 ----------------------------------------------------
class KpaServer:
    def __init__(self, amp, port=KPA_PORT, host="0.0.0.0"):
        self.amp = amp
        self.host = host
        self.port = port
        self._client = None
        self._client_lock = threading.Lock()

    def start(self):
        """Bind TCP and UDP now (so a bind failure raises here), then serve."""
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind((self.host, self.port))
        self.port = self.tcp.getsockname()[1]
        self.tcp.listen(5)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((self.host, self.port))
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._udp_loop, daemon=True).start()
        print(f"[kpa] KPA1500 simulator (firmware {self.amp.firmware}) listening on "
              f"TCP+UDP :{self.port}", flush=True)
        return self

    def serve(self):
        """station.py-style blocking entry point."""
        self.start()
        threading.Event().wait()

    def close(self):
        for s in (self.tcp, self.udp):
            try:
                s.close()
            except OSError:
                pass

    def _accept_loop(self):
        while True:
            try:
                conn, addr = self.tcp.accept()
            except OSError:
                return                             # listener closed
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._client_lock:
                old, self._client = self._client, conn
                if self._push_to_client not in self.amp.push:
                    self.amp.push.append(self._push_to_client)
            if old is not None:                    # newest connection wins
                try:
                    old.close()
                except OSError:
                    pass
            threading.Thread(target=self._client_loop, args=(conn, addr),
                             daemon=True).start()

    def _push_to_client(self, text):
        """Unsolicited replies (a finished tune's ^FT;) go to the TCP client."""
        with self._client_lock:
            conn = self._client
        if conn is not None:
            conn.sendall(text.encode("ascii"))

    def _client_loop(self, conn, addr):
        who = f"tcp {addr[0]}:{addr[1]}"
        print(f"[kpa] client connected {who}", flush=True)
        buf = ""
        try:
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data.decode("ascii", errors="replace")
                while ";" in buf:
                    cmd, buf = buf.split(";", 1)
                    resp = self.amp.handle(cmd + ";", who=who)
                    if resp:
                        conn.sendall(resp.encode("ascii"))
                if len(buf) > 64:                  # no terminator in sight: drop it
                    buf = ""
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            print(f"[kpa] client {who} disconnected", flush=True)

    def _udp_loop(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(512)
            except ConnectionResetError:
                # Windows reports an ICMP port-unreachable from an EARLIER reply
                # (the client had gone) on the next recvfrom. Not fatal.
                continue
            except OSError:
                return                             # socket closed
            text = data.decode("ascii", errors="replace").strip()
            if not text.endswith(";"):
                continue
            # "Send only one command and expect at most one response."
            resp = self.amp.handle(text.split(";", 1)[0] + ";", who=f"udp {addr[0]}")
            if resp:
                self.udp.sendto(resp.encode("ascii"), addr)


class RfSense:
    """UDP 'RF 1 <watts>' / 'RF 0' from an exciter (flex_sim.py --rf-sense)."""

    def __init__(self, amp, port=RF_SENSE_PORT, host="0.0.0.0"):
        self.amp = amp
        self.host = host
        self.port = port

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()
        print(f"[kpa] RF sense listening on UDP :{self.port}", flush=True)
        return self

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(256)
            except ConnectionResetError:
                continue
            except OSError:
                return                             # socket closed
            for line in data.decode("ascii", errors="replace").splitlines():
                parts = line.strip().upper().split()
                if len(parts) < 2 or parts[0] != "RF":
                    continue
                try:
                    on = parts[1] == "1"
                    watts = float(parts[2]) if on and len(parts) > 2 else (
                        DEFAULT_DRIVE_W if on else 0.0)
                except ValueError:
                    continue
                self.amp.set_rf(watts, source=f"rf-sense {addr[0]}")


# ---- the text command language (prompt, station.py and the control page) --------
HELP = ("kpa commands:\n"
        "  status                     meters, keying and the interlock counters\n"
        "  oper 0|1                   STBY / OPER\n"
        "  power 0|1                  main supplies off / on\n"
        "  keyin 0|1                  the rear-panel KEY IN line\n"
        "  inhibit 0|1 | ni 0|1       the ACC INHIBIT line / whether it is honoured (^NI)\n"
        "  tx [secs] | rx             network PTT, same as ^TX / ^TXnn / ^RX\n"
        "  rf <W>                     exciter drive at the input (0 = RF off)\n"
        "  cycle 0|1 [W]              a correctly sequenced over, repeated\n"
        "  fault <hex> | clear        inject / clear a fault (e.g. fault 60)\n"
        "  temp <C> | swr <v> | band <bb> | ant 1|2 | trdelay <ms> | relay <ms>\n"
        "  atu I|B | antswr <v> | tune 1|0   ATU mode, antenna SWR, start/cancel a tune\n"
        "  events [n]                 the last n events (default 15)\n"
        "  reset                      clear the event log and counters\n"
        "  help")


def status_line(amp):
    s = amp.state()
    lead = "-" if s["min_lead_ms"] is None else f"{s['min_lead_ms']:.2f} ms"
    return (f"  {'ON ' if s['powered'] else 'OFF'} {'OPER' if s['operate'] else 'STBY'} "
            f"fault={s['fault']}{' INHIBITED' if s['inhibited'] else ''} T/R={s['tr']} ^TX={int(s['sw_tx'])} KEYIN={int(s['keyin'])} "
            f"TQ={s['tq']} | in {s['rf_w']:.0f} W  out {s['fwd_w']:.0f} W  SWR {s['swr']:.1f} "
            f"{s['temp_c']} C | overs={s['overs']} HOT SWITCHES={s['hot_switches']} "
            f"min lead={lead}")


def command(amp, line):
    """Run one text command; return what to print."""
    p = line.split()
    if not p:
        return ""
    c, a = p[0].lower(), p[1:]
    try:
        if c == "help":
            return HELP
        if c == "status":
            return status_line(amp)
        if c == "oper":
            amp.set_operate(a[0] == "1", who="cli")
        elif c == "power":
            amp.set_power(a[0] == "1")
        elif c == "keyin":
            amp.set_keyin(a[0] == "1")
        elif c == "inhibit":
            amp.set_inhibit_line(a[0] == "1")
        elif c == "ni":
            amp.set_ni(a[0] == "1", who="cli")
        elif c == "tx":
            amp.sw_key(int(a[0]) if a else None, who="cli")
        elif c == "rx":
            amp.sw_unkey(who="cli")
        elif c == "rf":
            amp.set_rf(float(a[0]), source="cli")
        elif c == "cycle":
            if a[0] == "1":
                amp.start_cycle(float(a[1]) if len(a) > 1 else None)
            else:
                amp.stop_cycle()
        elif c == "fault":
            if a[0].lower() == "clear":
                amp.clear_fault(who="cli")
            else:
                amp.set_fault(int(a[0], 16))
        elif c == "clear":
            amp.clear_fault(who="cli")
        elif c == "temp":
            amp.set_temp(int(a[0]))
        elif c == "atu":
            with amp.lock:
                amp.atu_mode = "B" if a[0].upper() == "B" else "I"
                amp.atu_inline = amp.atu_mode == "I"
        elif c == "antswr":
            with amp.lock:
                amp.ant_swr = max(1.0, float(a[0]))
        elif c == "tune":
            if a[0] == "1":
                amp.start_tune(who="cli")
            else:
                amp.cancel_tune(who="cli")
        elif c == "swr":
            with amp.lock:
                amp.swr = max(1.0, float(a[0]))
        elif c == "band":
            with amp.lock:
                amp.band = max(0, min(10, int(a[0])))
        elif c == "ant":
            with amp.lock:
                amp.antenna = 2 if a[0] == "2" else 1
        elif c == "trdelay":
            with amp.lock:
                amp.tr_delay_ms = max(0, min(50, int(a[0])))
        elif c == "relay":
            with amp.lock:
                amp.relay_ms = max(0.0, float(a[0]))
        elif c == "events":
            n = int(a[0]) if a else 15
            evs = list(amp.events)[-n:]
            return "\n".join(
                f"  {e['t_ms']:>11.3f} ms  {e['kind']:<15} "
                + " ".join(f"{k}={v}" for k, v in e.items()
                           if k not in ("n", "t_ms", "wall", "kind"))
                for e in evs) or "  (no events)"
        elif c == "reset":
            amp.reset_counters()
        else:
            return HELP
    except (IndexError, ValueError):
        return "  bad args\n" + HELP
    return status_line(amp)


# ---- control page ------------------------------------------------------------
PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KPA1500 sim</title>
<style>
:root{color-scheme:dark}
body{margin:0;padding:16px;background:#0d1117;color:#e6edf3;font:14px system-ui,sans-serif}
h1{font-size:18px;margin:0 0 12px}
.row{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:6px 10px;cursor:pointer}
button:hover{border-color:#22b8d8}
input{width:70px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:5px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:12px 0}
.cell{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:8px}
.k{color:#8b949e;font-size:12px}.v{font-size:18px;font-weight:600}
.tx{color:#f85149}.ok{color:#3fb950}.warn{color:#f0aa3c}
#ev{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:8px;
    font:12px ui-monospace,monospace;white-space:pre;overflow-x:auto;max-height:340px;overflow-y:auto}
</style>
<h1>Elecraft KPA1500 simulator &middot; network PTT &amp; T/R interlock bench</h1>
<div class="grid" id="g"></div>
<div class="row">
 <button onclick="c('power 1')">Power on</button><button onclick="c('power 0')">Power off</button>
 <button onclick="c('oper 1')">OPER</button><button onclick="c('oper 0')">STBY</button>
 <button onclick="c('tx')">^TX</button><button onclick="c('tx 5')">^TX5</button><button onclick="c('rx')">^RX</button>
 <button onclick="c('keyin 1')">KEY IN down</button><button onclick="c('keyin 0')">KEY IN up</button>
 <button onclick="c('ni 1')">^NI1</button><button onclick="c('ni 0')">^NI0</button>
 <button onclick="c('inhibit 1')">INHIBIT on</button><button onclick="c('inhibit 0')">INHIBIT off</button>
</div>
<div class="row">
 <input id="w" value="50"> W <button onclick="c('rf '+w.value)">RF on</button><button onclick="c('rf 0')">RF off</button>
 <button onclick="c('cycle 1 '+w.value)">Cycle on</button><button onclick="c('cycle 0')">Cycle off</button>
 <input id="f" value="60"> <button onclick="c('fault '+f.value)">Inject fault</button><button onclick="c('clear')">Clear fault</button>
 <button onclick="c('reset')">Reset log</button>
</div>
<div class="row">
 <button onclick="c('tune 1')">ATU tune</button><button onclick="c('tune 0')">Cancel tune</button>
 <button onclick="c('atu I')">ATU inline</button><button onclick="c('atu B')">ATU bypass</button>
 <input id="asw" value="1.8"> ant SWR <button onclick="c('antswr '+asw.value)">Set</button>
</div>
<div id="ev"></div>
<script>
function c(x){fetch('/cmd?c='+encodeURIComponent(x)).then(poll)}
function cell(k,v,cls){return '<div class="cell"><div class="k">'+k+'</div><div class="v '+(cls||'')+'">'+v+'</div></div>'}
let since=0,lines=[];
function poll(){
 fetch('/state').then(r=>r.json()).then(s=>{
  g.innerHTML=cell('Power',s.powered?'ON':'OFF')+cell('Mode',s.operate?'OPER':'STBY',s.operate?'ok':'')
  +cell('Fault',s.fault+' '+s.fault_name,s.fault!='00'?'tx':'')
  +cell('T/R relays',s.tr,s.tr=='TX'?'tx':'')
  +cell('^NI / INHIBIT line',(s.ni_enabled?'1':'0')+' / '+(s.inhibit_line?'on':'off'),s.inhibited?'tx':'')
  +cell('^TX / KEY IN / ^TQ',(s.sw_tx?'1':'0')+' / '+(s.keyin?'1':'0')+' / '+s.tq+(s.sw_timeout_left_s!=null?' ('+s.sw_timeout_left_s+'s)':''))
  +cell('Drive in',s.rf_w+' W')+cell('Output',s.fwd_w+' W',s.fwd_w>0?'tx':'')
  +cell('SWR / temp',s.swr.toFixed(1)+' / '+s.temp_c+' C')
  +cell('ATU',(s.tuning?'TUNING':(s.atu_mode=='I'?'mode I':'mode B')+(s.atu_inline?', in':', bypassed'))+' (ant '+s.ant_swr.toFixed(1)+')',s.tuning?'warn':'')
  +cell('Overs',s.overs)+cell('HOT SWITCHES',s.hot_switches,s.hot_switches?'tx':'ok')
  +cell('Min lead',s.min_lead_ms==null?'-':s.min_lead_ms+' ms',s.min_lead_ms!=null&&s.min_lead_ms<s.relay_ms?'tx':'')
  +cell('Last tail',s.last_tail_ms==null?'-':s.last_tail_ms+' ms');
 });
 fetch('/events?since='+since).then(r=>r.json()).then(evs=>{
  evs.forEach(e=>{since=e.n;let d=Object.keys(e).filter(k=>!['n','t_ms','wall','kind'].includes(k)).map(k=>k+'='+e[k]).join(' ');
   lines.push((e.kind=='HOT_SWITCH'?'!! ':'   ')+e.t_ms.toFixed(3).padStart(12)+' ms  '+e.kind.padEnd(15)+d)});
  lines=lines.slice(-200);ev.textContent=lines.slice().reverse().join('\\n');
 });
}
setInterval(poll,300);poll();
</script>
"""


def start_control_server(amp, port=CTL_PORT, host="0.0.0.0"):
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
            if u.path == "/":
                self._send(PAGE, "text/html; charset=utf-8")
            elif u.path == "/state":
                self._send(json.dumps(amp.state()))
            elif u.path == "/events":
                since = int(q.get("since", ["0"])[0] or 0)
                self._send(json.dumps(amp.events_since(since)))
            elif u.path == "/cmd":
                out = command(amp, q.get("c", [""])[0])
                self._send(json.dumps({"out": out, "state": amp.state()}))
            else:
                self.send_error(404)

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[kpa] control page on http://<this-host>:{srv.server_address[1]}/", flush=True)
    return srv


# ---- self-test ------------------------------------------------------------------
def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + name)
        ok = ok and cond

    a = Amp(relay_ms=20)
    chk("null command answers ;", a.handle(";") == ";")
    chk("^I; identifies", a.handle("^i;") == "^KPA1500;")
    chk("^RVM; reports 03.07", a.handle("^RVM;") == "^RVM03.07;")
    chk("^AN; has its caret", a.handle("^AN;") == "^AN1;")
    chk("SET gets no response", a.handle("^OS1;") is None and a.handle("^OS;") == "^OS1;")
    chk("^WS; idle format", a.handle("^WS;") == "^WS0000 011;")
    chk("^TX; answers ^TX;", a.handle("^TX;") == "^TX;")
    chk("^TQ; = 1 after ^TX", a.handle("^TQ;") == "^TQ1;")
    time.sleep(0.05)
    a.set_rf(50, "selftest")
    chk("lead measured, no hot switch", a.hot_switches == 0 and a.last_lead_ms >= 20)
    chk("output 1250 W from 50 W", a.handle("^PWF;") == "^PWF1250;")
    a.set_rf(0, "selftest")
    chk("^RX; answers ^RX;", a.handle("^RX;") == "^RX;")
    chk("clean over: 0 hot switches", a.hot_switches == 0 and a.tr == "RX")
    a.set_rf(50, "selftest")
    a.handle("^TX;")
    chk("RF before the key IS a hot switch", a.hot_switches == 1)
    b = Amp(firmware="02.03", operate=True)
    chk("firmware 02.03 ignores ^TX", b.handle("^TX;") is None and b.tr == "RX")
    print("SELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description="Elecraft KPA1500 simulator with network PTT (AetherSDR #4097)")
    ap.add_argument("--port", type=int, default=KPA_PORT, help="TCP+UDP command port")
    ap.add_argument("--ctl-port", type=int, default=CTL_PORT,
                    help="control page / JSON port (0 = off)")
    ap.add_argument("--rf-sense-port", type=int, default=RF_SENSE_PORT,
                    help="UDP port for 'RF 1 <W>' / 'RF 0' from the exciter (0 = off)")
    ap.add_argument("--firmware", default=DEFAULT_FIRMWARE,
                    help="reported firmware; below 03.07 there is no network PTT")
    ap.add_argument("--relay-ms", type=float, default=RELAY_MS,
                    help="T/R relay travel time; RF sooner than this after the key is "
                         "a hot switch (default 5, the reference's figure)")
    ap.add_argument("--oper", action="store_true", help="start in OPER, not STBY")
    ap.add_argument("--no-cli", action="store_true", help="headless")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    amp = Amp(firmware=args.firmware, relay_ms=args.relay_ms, operate=args.oper)
    KpaServer(amp, args.port).start()
    if args.rf_sense_port:
        RfSense(amp, args.rf_sense_port).start()
    if args.ctl_port:
        start_control_server(amp, args.ctl_port)
    if args.no_cli:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return
    print(HELP, flush=True)
    while True:
        try:
            line = input("kpa> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line.lower() in ("quit", "exit"):
            return
        out = command(amp, line)
        if out:
            print(out)


if __name__ == "__main__":
    main()
