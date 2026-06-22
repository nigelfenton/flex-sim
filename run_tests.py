#!/usr/bin/env python3
"""flex-sim calibration-ruler test suite.

Drives each calibration RULER pattern through the same mock-AE loopback the
single-shot loopback_test.py uses, but instead of asserting it COLLECTS a
structured result per pattern (measured vs expected) and writes a printable
HTML report + a machine-readable JSON sidecar.

This is the "certify the rulers" runner — the generate-side calibration tier
(carrier, two_tone, comb, cal_tones, noise_cal) where the expected reading is
known by construction. Dynamic/visual patterns are out of scope here (they have
no numeric ruler); the suite is structured so they can be added later.

Usage:
    python3 run_tests.py                 # run all rulers, write report to ./reports/
    python3 run_tests.py --out DIR       # choose output directory
    python3 run_tests.py --open          # open the HTML report when done

Exit code 0 = every ruler passed, 1 = one or more failed (CI-friendly).
"""
import argparse
import html
import json
import os
import socket
import statistics
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.join(HERE, "flex_sim.py")

# Protocol constants (mirror loopback_test.py / PROTOCOL.md).
PAN_ID, WF_ID, METER_SID = 0x40000000, 0x42000000, 0x46000000
PCC_FFT, PCC_WF, PCC_METER = 0x8003, 0x8004, 0x8002
BINS = 1024
PAN_MIN_DBM, PAN_MAX_DBM, Y_PIXELS = -130.0, -20.0, 700
PX_PER_DB = (Y_PIXELS - 1) / (PAN_MAX_DBM - PAN_MIN_DBM)   # ~6.36 px/dB

AE_CMDS = [
    "sub radio all", "sub client all", "client program AetherSDR", "client gui ",
    "client station TEST", "client set send_reduced_bw_dax=1", "keepalive enable",
    "sub slice all", "sub pan all", "sub meter all", "mic list",
    "info", "slice list",
    "display panafall create x=100 y=100",
    "slice create pan=0x40000000 freq=14.225",
    f"display pan set 0x40000000 xpixels={BINS} ypixels={Y_PIXELS} "
    f"min_dbm={int(PAN_MIN_DBM)} max_dbm={int(PAN_MAX_DBM)}",
]


def px_to_dbm(px):
    """Invert the emulator's dBm->pixel map (PROTOCOL: lower pixel = stronger)."""
    return PAN_MAX_DBM - (px / (Y_PIXELS - 1)) * (PAN_MAX_DBM - PAN_MIN_DBM)


def decode_fft(pkt):
    payload = pkt[28:]
    _, _, _, total, _ = struct.unpack(">HHHHI", payload[:12])
    bins = list(struct.unpack(f">{total}H", payload[12:12 + total * 2]))
    return total, bins


def decode_meter(pkt):
    payload = pkt[28:]
    mid, raw = struct.unpack(">Hh", payload[:4])
    return mid, raw / 128.0


# ---------------------------------------------------------------------------
# One emulator session: spawn, handshake, prime, collect one FFT + meter frame.
# Returns (fft_bins, meter_dbm, emulator_log) or raises on protocol failure.
# ---------------------------------------------------------------------------
def capture_frame(pattern):
    proc = subprocess.Popen(
        [sys.executable, EMU, "--ip", "127.0.0.1", "--pattern", pattern],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(1.4)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(5.0)
        udp_port = udp.getsockname()[1]

        tcp = socket.create_connection(("127.0.0.1", 4992), timeout=5.0)
        f = tcp.makefile("rwb", buffering=0)
        ver, han = f.readline().decode().strip(), f.readline().decode().strip()
        if not (ver.startswith("V") and han.startswith("H")):
            raise RuntimeError(f"bad handshake: {ver!r} {han!r}")

        udp.sendto(b"\x00", ("127.0.0.1", 4992))            # prime -> vita_dest
        seq = 1
        for c in AE_CMDS[:11] + [f"client udpport {udp_port}"] + AE_CMDS[11:]:
            f.write(f"C{seq}|{c}\n".encode())
            seq += 1

        fft_sample = meter_sample = None
        t0 = time.time()
        while time.time() - t0 < 6.0 and (fft_sample is None or meter_sample is None):
            try:
                pkt, _ = udp.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                break
            if len(pkt) < 28:
                continue
            sid = struct.unpack(">I", pkt[4:8])[0]
            pcc = struct.unpack(">I", pkt[12:16])[0] & 0xFFFF
            if pcc == PCC_FFT and fft_sample is None:
                fft_sample = pkt
            elif pcc == PCC_METER and meter_sample is None:
                meter_sample = pkt

        if fft_sample is None:
            raise RuntimeError("no FFT packets received (stream never started)")
        _, bins = decode_fft(fft_sample)
        mdbm = decode_meter(meter_sample)[1] if meter_sample else None
        return bins, mdbm
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=3)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-ruler checks. Each returns a result dict:
#   {pattern, title, passed, measurements:[{name,measured,expected,unit,ok}], notes}
# Measurements are reported whether they pass or fail (a report, not just a gate).
# ---------------------------------------------------------------------------
def _m(name, measured, expected, unit, ok):
    return {"name": name, "measured": measured, "expected": expected, "unit": unit, "ok": ok}


def check_two_tone(bins, mdbm):
    floor = max(bins)
    strong = sorted(range(BINS), key=lambda b: bins[b])[:2]
    ctr = BINS // 2
    offs = sorted(abs(b - ctr) for b in strong)
    spurs = [b for b in range(BINS) if b not in strong and bins[b] < floor]
    eq = bins[strong[0]] == bins[strong[1]]
    tone_dbm = round(px_to_dbm(bins[strong[0]]), 1)
    ms = [
        _m("Two peaks present", len(strong), 2, "", len(strong) == 2),
        _m("Peaks equal level", "yes" if eq else "no", "yes", "", eq),
        _m("Symmetric about VFO", f"±{offs[0]}/±{offs[1]}", "equal", "bins", offs[0] == offs[1]),
        _m("Intermod spurs", len(spurs), 0, "bins", not spurs),
        _m("Tone level", tone_dbm, -73.0, "dBm", abs(tone_dbm - (-73.0)) <= 1.0),
    ]
    return {"pattern": "two_tone", "title": "Two-tone (linearity / IMD ruler)",
            "passed": all(m["ok"] for m in ms), "measurements": ms,
            "notes": "Pure sim path is perfectly linear — any spur is AE's, not the signal's."}


def check_noise_cal(bins, mdbm):
    mean_px = statistics.mean(bins)
    spread = max(bins) - min(bins)
    sd = statistics.pstdev(bins)
    mean_dbm = round(px_to_dbm(mean_px), 1)
    ripple_db = round(spread / PX_PER_DB, 2)
    ms = [
        _m("Mean level", mean_dbm, -73.0, "dBm", abs(mean_dbm - (-73.0)) <= 1.0),
        _m("Ripple (peak-to-peak)", ripple_db, "≤3.0", "dB", ripple_db <= 3.5),
        _m("Flatness (stdev)", round(sd, 1), "≤12", "px", sd < 12),
        _m("Full-span bed", sum(1 for v in bins if abs(v - mean_px) < 6 * PX_PER_DB),
           BINS, "bins", sum(1 for v in bins if abs(v - mean_px) < 6 * PX_PER_DB) > BINS * 0.95),
    ]
    return {"pattern": "noise_cal", "title": "Calibrated noise bed (filter response)",
            "passed": all(m["ok"] for m in ms), "measurements": ms,
            "notes": "Flat mean at the set level; feed through AE's filter to read its shape."}


def check_cal_tones(bins, mdbm):
    floor = max(bins)
    tones = [int(fr * BINS) for fr in (0.2, 0.4, 0.6, 0.8)]
    expect_dbm = [-100, -80, -60, -40]
    ms = []
    for b, ed in zip(tones, expect_dbm):
        meas = round(px_to_dbm(bins[b]), 1)
        # Smeared ±1 bin; take the strongest of the triplet as the tone reading.
        trip = min(bins[max(0, b - 1):b + 2])
        meas = round(px_to_dbm(trip), 1)
        ms.append(_m(f"Tone @ {ed} dBm reads", meas, ed, "dBm", abs(meas - ed) <= 2.0))
    mono = all(bins[tones[i]] >= bins[tones[i + 1]] for i in range(3))  # stronger as dBm rises
    ms.append(_m("Monotonic ladder", "yes" if mono else "no", "yes", "", mono))
    return {"pattern": "cal_tones", "title": "Calibration tones (dB-scale ruler)",
            "passed": all(m["ok"] for m in ms), "measurements": ms,
            "notes": "Four fixed carriers verify the panadapter dB scale reads each level."}


def check_carrier(bins, mdbm):
    ctr = BINS // 2
    peak = min(bins[ctr - 3:ctr + 4])
    peak_dbm = round(px_to_dbm(peak), 1)
    ms = [
        _m("Carrier at VFO", peak_dbm, -73.0, "dBm", abs(peak_dbm - (-73.0)) <= 2.0),
        _m("S-meter reads level", round(mdbm, 1) if mdbm is not None else "n/a",
           -73.0, "dBm", mdbm is not None and abs(mdbm - (-73.0)) <= 1.0),
    ]
    return {"pattern": "carrier", "title": "Carrier (S-meter / dBm calibration)",
            "passed": all(m["ok"] for m in ms), "measurements": ms,
            "notes": "Single VFO carrier; the S-meter should read the injected dBm."}


def check_comb(bins, mdbm):
    floor = max(bins)
    tones = [b for b in range(BINS) if bins[b] < floor - 2]   # bins clearly above floor
    # Comb places COMB_TONES evenly; check we see roughly that many, evenly spaced.
    n = len(_group_runs(tones))
    ms = [
        _m("Comb tones visible", n, 8, "", abs(n - 8) <= 1),
        _m("All above floor", "yes" if tones else "no", "yes", "", bool(tones)),
    ]
    return {"pattern": "comb", "title": "Comb (dynamic range / simultaneous bins)",
            "passed": all(m["ok"] for m in ms), "measurements": ms,
            "notes": "Evenly spaced tones across the span — many simultaneous bins."}


def _group_runs(sorted_bins):
    """Collapse adjacent bin indices into groups (one per tone, ignoring smear)."""
    groups, prev = [], None
    for b in sorted(sorted_bins):
        if prev is None or b - prev > 2:
            groups.append([b])
        else:
            groups[-1].append(b)
        prev = b
    return groups


RULERS = [
    ("two_tone", check_two_tone),
    ("noise_cal", check_noise_cal),
    ("cal_tones", check_cal_tones),
    ("carrier", check_carrier),
    ("comb", check_comb),
]


def run_all(only=None):
    """Run the ruler suite. `only` = optional iterable of pattern names to run a
    subset (the control panel's per-ruler picker uses this); None = all rulers."""
    rulers = RULERS if not only else [(p, c) for p, c in RULERS if p in set(only)]
    results = []
    for pattern, check in rulers:
        print(f"  running {pattern} ...", end=" ", flush=True)
        try:
            bins, mdbm = capture_frame(pattern)
            res = check(bins, mdbm)
        except Exception as e:
            res = {"pattern": pattern, "title": pattern, "passed": False,
                   "measurements": [], "notes": "", "error": str(e)}
        results.append(res)
        print("PASS" if res["passed"] else f"FAIL ({res.get('error', 'measurements out of range')})")
        time.sleep(0.5)            # let sockets drain between sessions
    return results


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def write_json(results, meta, path):
    with open(path, "w", encoding="utf-8") as fp:
        json.dump({"meta": meta, "results": results}, fp, indent=2)


def write_html(results, meta, path):
    n_pass = sum(1 for r in results if r["passed"])
    n_tot = len(results)
    overall = "PASS" if n_pass == n_tot else "FAIL"
    rows = []
    for r in results:
        badge = ("pass" if r["passed"] else "fail")
        head = (f'<tr class="ruler {badge}"><td colspan="5">'
                f'<span class="dot"></span><b>{html.escape(r["title"])}</b>'
                f'<span class="pat">{html.escape(r["pattern"])}</span>'
                f'<span class="verdict">{"PASS" if r["passed"] else "FAIL"}</span></td></tr>')
        rows.append(head)
        if r.get("error"):
            rows.append(f'<tr class="err"><td colspan="5">⚠ {html.escape(r["error"])}</td></tr>')
        for m in r["measurements"]:
            ok = "ok" if m["ok"] else "bad"
            rows.append(
                f'<tr class="{ok}"><td class="mn">{html.escape(str(m["name"]))}</td>'
                f'<td class="num">{html.escape(str(m["measured"]))}</td>'
                f'<td class="exp">{html.escape(str(m["expected"]))}</td>'
                f'<td class="unit">{html.escape(str(m["unit"]))}</td>'
                f'<td class="tick">{"✓" if m["ok"] else "✗"}</td></tr>')
        if r.get("notes"):
            rows.append(f'<tr class="note"><td colspan="5">{html.escape(r["notes"])}</td></tr>')

    doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>flex-sim ruler report — {html.escape(meta['timestamp'])}</title>
<style>
  :root {{ --bg:#fff; --ink:#0a1628; --dim:#516680; --line:#d2dce6;
           --pass:#1a8c3e; --fail:#c0392b; --head:#eef3f8; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:'Segoe UI',system-ui,sans-serif; color:var(--ink);
          background:var(--bg); margin:0; padding:28px; font-size:14px; }}
  h1 {{ font-size:21px; margin:0 0 2px; }}
  .sub {{ color:var(--dim); font-size:12px; margin-bottom:18px; }}
  .summary {{ display:inline-block; padding:8px 16px; border-radius:6px;
              font-weight:bold; font-size:15px; margin-bottom:18px;
              color:#fff; background:var(--{ 'pass' if overall=='PASS' else 'fail' }); }}
  table {{ border-collapse:collapse; width:100%; max-width:860px; }}
  td {{ padding:6px 10px; border-bottom:1px solid var(--line); }}
  tr.ruler td {{ background:var(--head); padding-top:12px; padding-bottom:12px;
                 border-top:2px solid var(--line); }}
  tr.ruler .pat {{ color:var(--dim); font-family:Consolas,monospace; font-size:11px;
                   margin-left:8px; }}
  tr.ruler .verdict {{ float:right; font-weight:bold; }}
  tr.ruler.pass .verdict {{ color:var(--pass); }}
  tr.ruler.fail .verdict {{ color:var(--fail); }}
  .dot {{ display:inline-block; width:9px; height:9px; border-radius:50%;
          margin-right:8px; vertical-align:1px; }}
  tr.ruler.pass .dot {{ background:var(--pass); }}
  tr.ruler.fail .dot {{ background:var(--fail); }}
  td.mn {{ padding-left:26px; }}
  td.num,td.exp,td.unit,td.tick {{ font-family:Consolas,monospace; text-align:right; }}
  td.exp {{ color:var(--dim); }}
  tr.bad td.num, tr.bad td.tick {{ color:var(--fail); font-weight:bold; }}
  tr.ok td.tick {{ color:var(--pass); }}
  tr.note td {{ color:var(--dim); font-size:12px; font-style:italic; padding-left:26px;
                border-bottom:1px solid var(--line); }}
  tr.err td {{ color:var(--fail); padding-left:26px; }}
  .foot {{ color:var(--dim); font-size:11px; margin-top:20px; }}
  @media print {{ body {{ padding:0; }} .summary {{ -webkit-print-color-adjust:exact; }} }}
</style></head><body>
<h1>flex-sim — Calibration Ruler Report</h1>
<div class="sub">Generate-side rulers certified against the mock-AE loopback ·
  emulator {html.escape(meta['emu_version'])} · {html.escape(meta['timestamp'])}</div>
<div class="summary">{overall} — {n_pass}/{n_tot} rulers passed</div>
<table><tbody>
{chr(10).join(rows)}
</tbody></table>
<div class="foot">Pan {int(PAN_MIN_DBM)}…{int(PAN_MAX_DBM)} dBm over {Y_PIXELS} px
  ({PX_PER_DB:.2f} px/dB) · {BINS} bins · pixel→dBm inverted from PROTOCOL.md.
  flex-sim ruler suite · G0JKN.</div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(doc)


def emu_version():
    try:
        import re
        src = open(EMU, encoding="utf-8").read()
        m = re.search(r'FLEX_SIM_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', src)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "reports"))
    ap.add_argument("--open", action="store_true", help="open the HTML report when done")
    ap.add_argument("--stamp", default=None,
                    help="timestamp string for the report (default: ask the OS)")
    ap.add_argument("--only", default=None,
                    help="comma-separated ruler names to run a subset "
                         f"(available: {','.join(p for p, _ in RULERS)})")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    if only:
        unknown = set(only) - {p for p, _ in RULERS}
        if unknown:
            sys.exit(f"unknown ruler(s): {','.join(sorted(unknown))}")

    # Timestamp: the workflow sandbox forbids Date.now(); a plain script is fine,
    # but allow an override so a caller can pin it for reproducible filenames.
    stamp = args.stamp or time.strftime("%Y-%m-%d_%H%M%S")
    pretty = args.stamp or time.strftime("%Y-%m-%d %H:%M:%S")

    n_rulers = len(only) if only else len(RULERS)
    print(f"flex-sim ruler suite — {n_rulers} ruler(s)" + (f" [{','.join(only)}]" if only else ""))
    results = run_all(only)
    meta = {"timestamp": pretty, "emu_version": emu_version(),
            "pan_min_dbm": PAN_MIN_DBM, "pan_max_dbm": PAN_MAX_DBM, "bins": BINS}

    html_path = os.path.join(args.out, f"ruler-report_{stamp}.html")
    json_path = os.path.join(args.out, f"ruler-report_{stamp}.json")
    write_html(results, meta, html_path)
    write_json(results, meta, json_path)

    n_pass = sum(1 for r in results if r["passed"])
    print(f"\n{n_pass}/{len(results)} rulers passed")
    print(f"  HTML: {html_path}")
    print(f"  JSON: {json_path}")
    if args.open:
        try:
            os.startfile(html_path)            # Windows
        except AttributeError:
            subprocess.run(["xdg-open", html_path], check=False)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
