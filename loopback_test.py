#!/usr/bin/env python3
"""Loopback validator for the Phase-0 emulator — mock-AE on 127.0.0.1, no real AE.

Spawns flex_sim.py and plays AE's role: TCP connect, read V/H, drive the
command sequence, fire the 0x00 prime, then assert the emulator (a) completes the
handshake, (b) emits display pan + waterfall status, (c) streams valid VITA-49
FFT (0x8003) + waterfall (0x8004) packets, and (d) the FFT *content* matches the
selected pattern. Runs on loopback so the firewall is irrelevant.

Usage:  python3 loopback_test.py [pattern]      (default: cal_tones)
"""
import os, socket, struct, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
EMU = os.path.join(HERE, "flex_sim.py")
PAN_ID, WF_ID, METER_SID = 0x40000000, 0x42000000, 0x46000000
PCC_FFT, PCC_WF, PCC_METER = 0x8003, 0x8004, 0x8002
BINS = 1024
PATTERN = sys.argv[1] if len(sys.argv) > 1 else "cal_tones"

AE_CMDS = [
    "sub radio all", "sub client all", "client program AetherSDR", "client gui ",
    "client station TEST", "client set send_reduced_bw_dax=1", "keepalive enable",
    "sub slice all", "sub pan all", "sub meter all", "mic list",
    "info", "slice list",
    "display panafall create x=100 y=100",
    "slice create pan=0x40000000 freq=14.225",
    "display pan set 0x40000000 xpixels=1024 ypixels=700 min_dbm=-130 max_dbm=-20",
]


def decode_fft(pkt):
    sb, nb, bs, tb, fi = struct.unpack(">HHHHI", pkt[28:40])
    bins = struct.unpack(">%dH" % nb, pkt[40:40 + nb * 2])
    return tb, bins   # totalBins, pixel values (0=top/strong .. 699=bottom/weak)


def decode_meter(pkt):
    mid, raw = struct.unpack(">Hh", pkt[28:32])
    return mid, raw / 128.0   # AE: dBm = raw / 128.0


def main():
    proc = subprocess.Popen([sys.executable, EMU, "--ip", "127.0.0.1", "--pattern", PATTERN],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(1.2)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0)); udp.settimeout(4.0)
        udp_port = udp.getsockname()[1]

        tcp = socket.create_connection(("127.0.0.1", 4992), timeout=4.0)
        f = tcp.makefile("rwb", buffering=0)
        ver, han = f.readline().decode().strip(), f.readline().decode().strip()
        assert ver.startswith("V") and han.startswith("H"), (ver, han)
        print(f"[{PATTERN}] handshake:", ver, "/", han)

        udp.sendto(b"\x00", ("127.0.0.1", 4992))            # prime -> sets vita_dest
        seq = 1
        for c in AE_CMDS[:11] + [f"client udpport {udp_port}"] + AE_CMDS[11:]:
            f.write(f"C{seq}|{c}\n".encode()); seq += 1

        tcp.settimeout(1.5)
        statuses = []
        t0 = time.time()
        while time.time() - t0 < 2.0:
            try:
                line = f.readline()
            except (socket.timeout, TimeoutError):
                break
            if not line:
                break
            s = line.decode(errors="replace").strip()
            if s.startswith("S"):
                statuses.append(s)
        assert any("display pan 0x" in s and "waterfall=0x" in s for s in statuses), "no pan status"
        assert any("display waterfall 0x" in s for s in statuses), "no waterfall status"
        assert any("|meter " in s and "10.nam=LEVEL" in s for s in statuses), "no SLC LEVEL meter def"
        #                                  ^ slice-0 S-meter = SLICE_METER_BASE+0 (per-slice meter ids)

        seen = {PCC_FFT: 0, PCC_WF: 0, PCC_METER: 0}
        fft_sample = meter_sample = None
        t0 = time.time()
        while time.time() - t0 < 4.0 and any(v == 0 for v in seen.values()):
            try:
                data, _ = udp.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                break
            if len(data) < 28:
                continue
            sid = struct.unpack(">I", data[4:8])[0]
            pcc = struct.unpack(">I", data[12:16])[0] & 0xFFFF
            if pcc == PCC_FFT:
                assert sid == PAN_ID; seen[PCC_FFT] += 1; fft_sample = fft_sample or data
            elif pcc == PCC_WF:
                assert sid == WF_ID; seen[PCC_WF] += 1
            elif pcc == PCC_METER:
                assert sid == METER_SID; seen[PCC_METER] += 1; meter_sample = meter_sample or data
        assert seen[PCC_FFT] > 0 and seen[PCC_WF] > 0 and seen[PCC_METER] > 0, ("no packets", seen)

        mid, mdbm = decode_meter(meter_sample)
        assert mid == 10, ("meter id", mid)            # slice-0 S-meter (SLICE_METER_BASE+0)
        assert -160.0 < mdbm < 30.0, ("meter dBm out of range", mdbm)
        print(f"[{PATTERN}] S-meter (VFO) id={mid} -> {mdbm:.1f} dBm")

        # ---- content check on a real FFT frame ----
        tb, bins = decode_fft(fft_sample)
        assert tb == BINS, ("totalBins", tb)
        lo, hi = min(bins), max(bins)
        print(f"[{PATTERN}] status OK -FFT={seen[PCC_FFT]} WF={seen[PCC_WF]} -"
              f"pixels min={lo}(strongest) max={hi}(weakest)")
        if PATTERN == "cal_tones":
            floor = max(bins)                               # weakest = floor
            tones = [int(fr * BINS) for fr in (0.2, 0.4, 0.6, 0.8)]
            vals = [bins[b] for b in tones]
            print(f"[{PATTERN}] tone-bin pixels {vals} vs floor {floor} (lower = stronger)")
            assert all(v < floor for v in vals), "cal tones not stronger than floor!"
            assert vals[3] < vals[0], "expected -40dBm tone stronger than -100dBm tone"
        elif PATTERN == "two_tone":
            # Golden ruler: exactly two equal peaks, symmetric about centre, and
            # everything else at the floor (no intermod spurs in a pure sim path).
            floor = max(bins)                               # weakest pixel = floor
            strong = sorted(range(BINS), key=lambda b: bins[b])[:2]  # 2 lowest-pixel = 2 strongest
            assert bins[strong[0]] == bins[strong[1]], \
                f"two-tone peaks not equal level: {bins[strong[0]]} vs {bins[strong[1]]}"
            assert bins[strong[0]] < floor, "tones not stronger than floor"
            ctr = BINS // 2
            off0, off1 = sorted(abs(b - ctr) for b in strong)
            assert off0 == off1, f"tones not symmetric about VFO: offsets {off0},{off1}"
            spurs = [b for b in range(BINS) if b not in strong and bins[b] < floor]
            assert not spurs, f"intermod spur(s) present at bins {spurs[:8]} (sim path must be linear)"
            print(f"[{PATTERN}] two equal tones at bins {sorted(strong)} "
                  f"(±{off0} about {ctr}), floor {floor}, NO spurs — ruler verified")
        elif PATTERN == "noise_cal":
            # Calibrated bed: full-span noise whose ripple is TIGHT in absolute pixel
            # terms (the whole point — a flat reference, not the ±12 dB textured `noise`).
            # The ±1.5 dB ripple maps to only a few pixels, so the right test is "the
            # full pixel spread is small," NOT a relative-sigma outlier hunt (which would
            # paradoxically punish a tighter bed). pixel/dBm scale here ~ rows/100 dB.
            import statistics
            mean_px = statistics.mean(bins)
            full_spread = max(bins) - min(bins)            # total pixel excursion
            assert full_spread <= 40, \
                f"noise_cal ripple too wide: {full_spread} px peak-to-peak (expect tight bed)"
            assert statistics.pstdev(bins) < 12, \
                f"noise_cal stdev {statistics.pstdev(bins):.1f} px — not a flat bed"
            print(f"[{PATTERN}] flat bed: mean px {mean_px:.0f}, "
                  f"peak-to-peak {full_spread} px, stdev {statistics.pstdev(bins):.1f} — calibrated")
        elif PATTERN == "noise_floor":
            assert lo == hi, "noise_floor should be flat"

        print(f"\n*** LOOPBACK PASS [{PATTERN}] — handshake + VITA stream + content verified ***")
        return 0
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=3)[0]
        except Exception:
            out = ""
        tail = "\n".join(l for l in (out or "").splitlines() if "[<-]" not in l)
        if tail:
            print("\n--- emulator log ---\n" + tail)


if __name__ == "__main__":
    sys.exit(main())
