# flex-sim — synthetic SDR / VITA-49 spectrum generator

**Status:** design draft v0.1 · 2026-06-15 · Nigel (G0JKN) + Claude (research laptop, on the road)
**Working name:** `flex-sim` (placeholder — see Open Decisions)
**Relationship:** standalone sibling to [`tci-monitor`](https://github.com/nigelfenton/tci-monitor); together they form a closed-loop test rig.

---

## 1. Why
AetherSDR's waterfall/spectrum code churns constantly (#3578, #3586, #3457, #3182, #3031…) with **no synthetic test coverage** — every fix is eyeballed against whatever live signal happens to be on the air. Known waterfall bugs sit open and hard to reproduce: **#2126** (waterfall blanks/flickers during TX) and **#1916** (waterfall disappears after CW TX).

We need a controllable spectrum **source** that injects *known* patterns — min→max level ramps, calibration tones, sweeps, impulses, TX-blank repro — so waterfall behaviour becomes **reproducible and assertable** instead of "looks wrong to me."

## 2. What it is (and isn't)
- **IS:** a standalone app that **emulates a FlexRadio's data plane** — answers SmartSDR discovery + control, then streams synthetic **VITA-49** panadapter/FFT/meter data to AetherSDR, driving AE's *own* waterfall from a programmable signal engine.
- **ISN'T:** a TCI tool. TCI is AE's spectrum *output* to clients; this drives AE's *input* — a different plane. `tci-monitor` stays the passive observer; `flex-sim` is the active stimulus.
- **Closed loop:** `flex-sim` stimulates AE's input → `tci-monitor` observes AE's output (TCI spectrum, meters) → assert. Stimulus + measurement = a real test rig.

## 3. Goals / non-goals
**Goals:** deterministic, repeatable spectrum patterns; *accurate dB levels* (a calibrated test card); programmable rate / span / bin-count; reproduce known waterfall bugs on demand; eventually CI-able (golden-image or numeric readback).
**Non-goals (v1):** real IQ/audio streams, TX, full radio-control fidelity, being a usable "fake radio" to actually operate. Only enough fidelity to drive the spectrum/waterfall pipeline.

## 4. The protocol reality (the hard part — read this first)
To make AE render a waterfall, `flex-sim` must impersonate a Flex 6000 closely enough that AE will:
1. **Discover it** — periodic UDP discovery broadcast (Flex discovery datagram). *Exact port/format: CAPTURE from AE's discovery decoder or a real radio — do not guess.*
2. **Connect** — AE opens TCP **:4992**; `flex-sim` runs the control server speaking the FlexLib command grammar (`Cn|…` commands, `Rn|…` replies, `Sn|…` status). Must handle enough to: report version/handle, accept subscriptions, **create a panadapter** (`display pan create …` → returns a stream id), accept slice/meter subs, and signal data is flowing.
3. **Stream data** — VITA-49/UDP packets: panadapter/FFT frames (bin amplitudes → pan + waterfall) and meter packets, framed per Flex's VITA-49 usage (stream id, class id, packet type, payload), at the negotiated rate to AE's data port.

> ⚠️ **#1 risk / critical path:** the exact command set + VITA-49 framing. **Lift it from AE's own source** — AE already has the *decoder*, which is the precise *encode* spec — and/or **capture a real AE↔Flex 6300 session** (Nigel has the 6300/6700; do when home). Phase 0 exists to retire this risk before anything else is built.

## 5. Architecture (layers)
1. **Discovery responder** (UDP) — advertise "a radio" so AE lists/auto-connects it.
2. **Control server** (TCP 4992) — FlexLib command/reply/status; stream-id allocation; pan/slice/meter lifecycle.
3. **VITA-49 emitter** (UDP) — packetise FFT frames + meters at the negotiated rate to AE's data port.
4. **Signal engine** — produces FFT frames per the Test-Signal Spec (§7). Parameterised: span, center, bins, frame-rate, level (dBFS/dBm), pattern, animation. **Deterministic / seedable.**
5. **UI** (Qt) — pattern picker, level/rate/span controls, presets, start/stop, live subscription status.
6. **(Phase 3) readback/assert hook** — capture AE's TCI spectrum output via `tci-monitor`, or screen-grab, for golden-image / numeric assertion.

## 6. Tech stack
**Recommend C++/Qt + CMake** — same as `tci-monitor`: shared suite, build/release pipeline, UI components, and protocol-struct reuse with AE. High-rate VITA-49 streaming wants native perf.
**De-risk:** do a throwaway **Python protocol spike first** (sockets + `struct`) to nail the discovery+control handshake cheaply, *then* port to C++. Protocol-first.

## 7. Test-signal spec (the reusable gold)
Each pattern parameterised by span / center / bins / frame-rate; deterministic; serializable as a preset.
- `noise_floor(level)` — flat floor at dBFS L. Baseline.
- `ramp(min,max,period)` — whole-span level swept min→max. Tests level→colour mapping, **auto-black (#3586)**, clipping, floor consistency (#3483).
- `cal_tones([(freq,dBm)])` — fixed carriers at known levels → verify the **dB scale is accurate** (the literal test card).
- `swept_carrier(start,stop,rate)` — single tone across the span → frequency mapping + tile decode (**#3457**, the 1 GHz ceiling).
- `comb(n,spacing,level)` — multi-tone → dynamic range / simultaneous bins.
- `step(low,high,dt)` / `impulse(level,width)` — temporal/dynamic response: scroll timing, averaging, history reprojection (**#3578**), paced fallback (**#3182**).
- `tx_blank(pattern,blank_ms)` — deliberately drive the gap/zero-fill that mimics TX, to **reproduce #2126 / #1916 on demand.**
- `staircase` — center signal stepping floor→max in N even amplitude steps (renders as a colour ladder — reads AE's colormap/black-threshold live); `noise` — random noise band, tunable white→pink tilt. Signal width is set in **kHz** (`--width-khz`, tracks AE's span).
- `carrier` (added 2026-06-15) — steady tone on the VFO at a settable **dBm**; paired with the meter plane (below) it makes AE's **S-meter read the injected level** → a calibrated S-meter test card. Floor/level controls are now in **dBm with the S-unit shown** (S9 = −73 dBm, 6 dB/S-unit).
- **Meter plane (PCC 0x8002), added 2026-06-15:** the sim defines the slice S-meter (`meter … src=SLC nam=LEVEL unit=dBm`) and streams the dBm **at the VFO** each frame (`raw = dBm×128`), so AE's S-meter tracks the injected level for every pattern. Closes the dB-accuracy goal end-to-end (panadapter scale + S-meter). See `PROTOCOL.md` §5 / §8.6.
- **TX meter plane, added 2026-06-15:** keying the sim (panel **TX** toggle, or AE's own `transmit set mox=1`/`tune=1`) makes it emit `interlock state=TRANSMITTING` + `transmit` status and stream **FWDPWR** (dBm→W, `100 W = 50 dBm`) + **SWR** meters, so AE's TX power/SWR readouts move — no RF, emulated radio only. Meters stream continuously (~0 W / 1.0 SWR when not keyed) so the readout decays back on de-key. Settable W + SWR from the panel. See `PROTOCOL.md` §8.7.
- **CW + CWX keying, added 2026-06-15:** `cw` pattern (panel **CW normal** / **CW full break-in** buttons) sim-keys `CQ CQ CQ TST…` at 20 wpm; and the sim handles AE's **own CWX keyer** (`cwx send/wpm/qsk_enabled/clear`) so the keying originates from AE (authentic CW-TX path). Keying runs at 50 fps for sub-dit timing, holds a 0.6 s tail, follows AE's QSK, and reports `cwx queue=0` on drain. **Finding:** AE handles the waterfall across CW-TX gaps robustly (no #2126/#1916); the *audio* breaks are AE's local `CwxLocalKeyer` sidetone (the sim sends no audio) — a known AE area (PRs #3202/#2754/#2181), still reproducing on Windows v26.6.3. Also fixed a real **sim concurrency bug** (unserialized TCP writes from the stream + command threads). See `PROTOCOL.md` §8.8.
- **Interim live control (web), added 2026-06-15:** the spike serves a browser control panel (`--ctl-port`, default 8731) — pattern picker + live sliders for noise floor / signal level (dBm + S-unit) / signal width / noise-color, driven from the host browser, no restarts. (The full Qt GUI is still Phase 1; this is the interim bench.) The picker shows a per-pattern **"what to look for"** hint (incl. the relevant AE issue #) so the bench is self-documenting.
- **Faithful waterfall `AutoBlackLevel` (2026-06-16):** each waterfall tile now carries the frame's measured noise-floor raw intensity (`min(intens)`) in the `AutoBlackLevel` field, instead of a token value — so AE's radio-auto-black path (PR #3586) gets a real low/black point. Makes flex-sim a deterministic bench for verifying #3586 (drive the floor, confirm the waterfall floor tracks it evenly).

## 8. Phased plan
- **Phase 0 — protocol spike (retire the #1 risk):** minimal discovery + control so AE connects and opens a pan; emit ONE static flat frame; confirm *anything* renders in AE's waterfall. Python is fine here. **Proves the handshake before we invest.**
  - **Status (2026-06-15): ✅ LIVE-VALIDATED against real AetherSDR v26.6.3.** Sim in WSL2, AE on the Windows host (separate network stacks via `localhostForwarding=false`). Confirmed working: discovery, full handshake, **panadapter + waterfall both rendering**, **VFO 3.705/LSB**, coherent frequency, **S-meter reading the injected dBm**, **TX power/SWR meters** (key → 100 W/1.2, de-key → decays to 0/1.0), and **CW/CWX keying** (sim-keyed buttons + AE-driven CWX, normal & break-in). `loopback_test.py` (mock-AE) also passes offline across all patterns (now asserting the meter def + meter packets too). Findings on the CW-TX waterfall (robust, no #2126/#1916) and the AE-local CWX sidetone stutter are in `PROTOCOL.md` §8.8. Four AE quirks pinned along the way — see `PROTOCOL.md` §8 (RF_frequency status key; ±0.25 MHz waterfall XVTR tolerance; `blackThresh=160−black_level` colormap; plain-Hz tile freq).
- **Phase 1 — MVP:** C++/Qt; `noise_floor` + `ramp` + `cal_tones`; level/rate/span UI. Enough to exercise auto-black (#3586) and level accuracy.
- **Phase 2 — full pattern library + dynamics:** `swept_carrier`, `comb`, `step`, `impulse`, `tx_blank`.
- **Phase 3 — closed loop + CI:** `tci-monitor` readback / golden-image asserts; package as a regression fixture; candidate upstream contribution to AE.

## 9. Strategic upside
AE has **zero synthetic spectrum test coverage** for a fast-churning waterfall codebase. Scoped as a QA/regression fixture (CI-able with golden images), `flex-sim` becomes **the waterfall test rig the project doesn't have** — a high-value, non-disruptive thing to bring upstream once it's solid.

**"1 Aether, many radios" (Nigel, 2026-06-15):** because each emulated radio is just a discovery + control + VITA stream with its own serial/IP/port/stream-ids, the sim can advertise **N radios at once** (multiple instances, or one process serving many). That makes it a **multi-radio test bench (#3445) with zero hardware** — nobody can exercise AE's multi-radio support today without a rack of physical rigs. Design discovery + stream-id allocation to allow several radios to coexist on one host when we move past Phase 0.

### 9.1 What flex-sim tests — and what it can't (the data/render boundary)
flex-sim is a **data-correctness** rig: it injects *known* spectrum data (bins, levels, frequencies) and verifies AE **decodes and maps** it faithfully — the dB scale, frequency mapping, S-meter, auto-black, level→colour. The ruler suite (`run_tests.py`) certifies exactly this class.

It is **not** a **render-correctness** rig. Bugs that live *downstream of the data* — in compositing, rasterization, GPU-vs-software parity, z-order, anti-aliasing, line-width drawing — are invisible to flex-sim, because they don't depend on what the signal *is*. **Issue #3731** is the canonical example: a GPU-path z-order bug painted the panadapter **grid on top of the FFT trace** ("comb of vertical lines on the peaks"). flex-sim would send a perfect two-tone and the comb would appear *identically* — the defect ignores the data entirely. That bug needed a **golden-image** (pixel visual-diff) check, not a data check.

So the clean line: **flex-sim answers "is the data right?"; a golden-*image* rig answers "is the picture right?"** They're complementary, not the same tool. This is the boundary to keep in mind when scoping coverage — and it's *why* Phase 3's "golden-image asserts" is a distinct piece of work, not just more ruler patterns.

**Where flex-sim still helps the render rig:** it's the ideal **deterministic signal *source*** for capturing golden images. A known two-tone / `noise_cal` bed / `test_card` is a far better visual-diff subject than whatever live band noise happens to be on air — reproducible frame in, reproducible picture out. So flex-sim is the *stimulus* for a render-diff harness even though it can't do the diffing itself: data-rig and image-rig share flex-sim as their common, trustworthy input.

## 10. Open decisions (need Nigel)
1. **Name:** `flex-sim` / `flex-sim` / `spectrum-bench` / `vita-forge` / other?
2. **Stack:** confirm C++/Qt for the real tool, with a Python protocol-spike first? (recommended)
3. **Emulation target:** Flex 6000 specifically (matches AE's primary data plane) — or does AE have a simpler/generic SDR ingest we could target with less protocol surface? (Check AE source.)
4. **Repo:** standalone `nigelfenton/<name>` when we cut Phase 1.
5. **Capture plan:** grab a real AE↔Flex 6300 session when home to pin the exact handshake + VITA-49 framing, and/or read AE's decoder source.

## 11. When-home dependencies
- Read AE source (the VITA-49 **decoder = encode spec**, plus the FlexLib command handling) — needs hub/aurora reachable.
- Optional real-traffic capture from the 6300 — when home, when the Comcast/Lusby outage clears.
