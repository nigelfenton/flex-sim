#!/usr/bin/env python3
"""spe_bench.py — scripted bench of AE's SpeConnection against spe_sim.

Runs the sim in-process, connects a client that speaks exactly what AE's
SpeConnection sends (100 ms status poll + keystrokes), and asserts the amp-side
state machine and the status vectors the branch's parser consumes.

This is the wire half of the bench. The parse half is spe_protocol_test, which
runs the branch's OWN SpeProtocol.cpp against the same byte model.
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spe_sim  # noqa: E402

HOST_SYNC = 0x55
K_STATUS_REQUEST = 0x90
SPE_PORT = spe_sim.SPE_PORT

IAC, WILL, DO, DONT = 0xFF, 0xFB, 0xFD, 0xFE
COM_PORT_OPTION = 0x2C

fails = []


def check(cond, desc, got=None, want=None):
    tag = "[ OK ]" if cond else "[FAIL]"
    extra = ""
    if got is not None:
        extra = "  (got %r, want %r)" % (got, want)
    print("%s %s%s" % (tag, desc, extra), flush=True)
    if not cond:
        fails.append(desc)


def host_frame(cmd):
    """Exactly what AE sends: 55 55 55 | CNT=1 | DATA | CHK | CR LF."""
    return bytes([HOST_SYNC] * 3 + [1, cmd, cmd & 0xFF]) + b"\r\n"


def read_reply(sock, timeout=2.0):
    sock.settimeout(timeout)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\r\n"):
            break
    return buf


def parse_status(frame):
    """Amp -> host: AA AA AA | CNT | DATA | CHK-LO CHK-HI | CR LF."""
    i = frame.find(b"\xaa\xaa\xaa")
    if i < 0:
        return None, "no AA sync"
    cnt = frame[i + 3]
    payload = frame[i + 4:i + 4 + cnt]
    chk = frame[i + 4 + cnt:i + 6 + cnt]
    calc = sum(payload) & 0xFFFF
    lo, hi = calc & 0xFF, (calc >> 8) & 0xFF
    if len(chk) < 2 or chk[0] != lo or chk[1] != hi:
        return None, "checksum mismatch (16-bit, LOW byte first)"
    return payload.decode("ascii", "replace"), None


def main():
    amp = spe_sim.Amp("15K")
    threading.Thread(target=spe_sim.SpeServer(amp).serve, daemon=True).start()
    time.sleep(0.5)

    s = socket.create_connection(("127.0.0.1", spe_sim.SPE_PORT), timeout=5)

    # --- 1. Poll-only discipline: the amp must not greet. -----------------
    s.settimeout(1.0)
    greeted = b""
    try:
        greeted = s.recv(64)
    except socket.timeout:
        pass
    check(greeted == b"", "the amp never speaks unprompted (no greeting)",
          greeted, b"")

    # --- 2. Status vector shape ------------------------------------------
    s.sendall(host_frame(K_STATUS_REQUEST))
    raw = read_reply(s)
    payload, err = parse_status(raw)
    check(err is None, "status frame carries a valid 16-bit checksum, low byte first",
          err or "valid", "valid")
    fields = payload.split(",") if payload else []
    check(len(fields) == 20, "status body is marker 'C' + 19 fields",
          len(fields), 20)
    check(fields[0] == "C", "status marker is 'C'", fields[0] if fields else None, "C")
    check(fields[1] == "15K", "model ID reports 15K", fields[1] if fields else None, "15K")
    check(fields[2] == "S", "amp starts in STANDBY", fields[2] if fields else None, "S")
    check(fields[3] == "R", "amp starts in RX", fields[3] if fields else None, "R")

    # --- 3. The 60m band-table trap: 20m is index 05, not 04. ------------
    check(fields[6] == "05", "default band field is 05 (20m — 60m occupies index 2)",
          fields[6] if fields else None, "05")
    check(spe_sim.BANDS[5] == "20m", "band table puts 20m at index 5",
          spe_sim.BANDS[5], "20m")
    check(spe_sim.BANDS[2] == "60m", "band table puts 60m at index 2",
          spe_sim.BANDS[2], "60m")

    # --- 4. Non-fixed-width V/A — the trap I flagged in review. ----------
    amp.key(True, tx_watts=1350)
    s.sendall(host_frame(K_STATUS_REQUEST))
    tx_payload, _ = parse_status(read_reply(s))
    check(tx_payload is not None, "TX status frame parses")
    if tx_payload:
        tx_fields = tx_payload.split(",")
        check(tx_fields[3] == "T", "TX flag flips to T under key",
              tx_fields[3], "T")
        check(tx_fields[10] == "1350", "power field is zero-padded to 4 chars",
              tx_fields[10], "1350")
        # " %.1f" — one leading space, variable total width.
        check(tx_fields[13] == " 51.0",
              "PA volts is ' %.1f' — NOT fixed-width (the V/A trap)",
              tx_fields[13], " 51.0")
        check(len(tx_payload) == 69,
              "TX reference vector is 69 chars (RX is 67) — width varies with V/A",
              len(tx_payload), 69)

    amp.key(False)
    s.sendall(host_frame(K_STATUS_REQUEST))
    rx_payload, _ = parse_status(read_reply(s))
    check(rx_payload is not None and len(rx_payload) == 67,
          "RX reference vector is 67 chars",
          len(rx_payload) if rx_payload else None, 67)

    # --- 5. Per-level rescale: the HGauge defect's live source. ----------
    # LOW/MID/HIGH nominals for 1.5K-FA are 500/1000/1500 per spe_protocol_test.
    seen = []
    for _ in range(3):
        s.sendall(host_frame(spe_sim.KEY_POWER))
        read_reply(s)
        s.sendall(host_frame(K_STATUS_REQUEST))
        p, _ = parse_status(read_reply(s))
        seen.append(p.split(",")[9] if p else None)
    check(seen == ["M", "H", "L"],
          "POWER key cycles the level L->M->H->L (each a new gauge axis)",
          seen, ["M", "H", "L"])

    # --- 6. Keystroke ACK echoes the command. ----------------------------
    # OPERATE is a TOGGLE, and key(True) above left the amp in OPERATE (a real
    # amp cannot transmit from standby), so establish the starting state
    # explicitly rather than assuming it — assert the transition, both ways.
    s.sendall(host_frame(K_STATUS_REQUEST))
    p0, _ = parse_status(read_reply(s))
    before = p0.split(",")[2] if p0 else None
    check(before == "O",
          "keying left the amp in OPERATE (TX implies OPERATE)", before, "O")

    s.sendall(host_frame(spe_sim.KEY_OPERATE))
    ack = read_reply(s)
    check(ack.find(b"\xaa\xaa\xaa") >= 0 and spe_sim.KEY_OPERATE in ack,
          "keystroke is ACKed with the command echoed back")
    s.sendall(host_frame(K_STATUS_REQUEST))
    p, _ = parse_status(read_reply(s))
    check(p.split(",")[2] == "S" if p else False,
          "OPERATE toggles O -> S", p.split(",")[2] if p else None, "S")

    s.sendall(host_frame(spe_sim.KEY_OPERATE))
    read_reply(s)
    s.sendall(host_frame(K_STATUS_REQUEST))
    p, _ = parse_status(read_reply(s))
    check(p.split(",")[2] == "O" if p else False,
          "OPERATE toggles back S -> O", p.split(",")[2] if p else None, "O")

    # --- 7. SWITCH OFF, then poll silence (Miguel's detector). -----------
    s.sendall(host_frame(spe_sim.KEY_SWITCH_OFF))
    read_reply(s)
    s.sendall(host_frame(K_STATUS_REQUEST))
    p, _ = parse_status(read_reply(s))
    check(p.split(",")[2] == "S" if p else False,
          "SWITCH OFF returns the amp to STANDBY",
          p.split(",")[2] if p else None, "S")

    # --- 8. A malformed frame must be dropped, not desync the stream. ----
    s.sendall(bytes([HOST_SYNC] * 3 + [1, K_STATUS_REQUEST, 0x00]) + b"\r\n")
    bad = read_reply(s, timeout=1.0)
    check(bad == b"", "a bad checksum is dropped silently, no reply", bad, b"")
    s.sendall(host_frame(K_STATUS_REQUEST))
    after, err2 = parse_status(read_reply(s))
    check(after is not None and err2 is None,
          "the link still answers correctly after a dropped frame")

    s.close()

    # --- 9. Port-mode negotiation: what AE's scanComPortOptionReply sees. -
    # AetherSDR PR #4531 added scanComPortOptionReply(), so AE now DOES read
    # the DO/DONT answer and reports it. Assert the two personalities give the
    # two different replies AE distinguishes -- and that a raw port gives
    # neither, which AE reports as "nothing", not as a refusal.
    for offset, (mode, want_verb, label) in enumerate((
            ("rfc2217", DO, "telnet(rfc2217=true) answers IAC DO -> accepted"),
            ("telnet", DONT, "plain telnet answers IAC DONT -> refused"),
            ("raw", None, "a raw tcp accepter answers no negotiation at all"))):
        port = SPE_PORT + 101 + offset   # distinct off-band scratch per mode
        amp2 = spe_sim.Amp("15K")
        srv = spe_sim.SpeServer(amp2, port_mode=mode, port=port)
        threading.Thread(target=srv.serve, daemon=True).start()
        time.sleep(0.4)
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(bytes([IAC, WILL, COM_PORT_OPTION]))
        reply = read_reply(c, timeout=1.0)
        if want_verb is None:
            got = reply.find(bytes([IAC])) < 0 or COM_PORT_OPTION not in reply
            check(got, label, reply, b"(no COM-PORT-OPTION reply)")
        else:
            got = bytes([IAC, want_verb, COM_PORT_OPTION]) in reply
            check(got, label, reply, bytes([IAC, want_verb, COM_PORT_OPTION]))
        c.close()

    print()
    if fails:
        print("%d bench check(s) FAILED:" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("0 bench check(s) failed.")
    return 0


if __name__ == "__main__":
    rc = main()
    # The sim's server threads are daemons and keep printing; letting the
    # interpreter finalize normally can trip
    #   Fatal Python error: _enter_buffered_busy ... at interpreter shutdown
    # which overwrites our exit code with 127 and makes a clean run look like
    # a crash to CI. Flush what we own, then leave without running finalizers.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
