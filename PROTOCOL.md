# flex-sim — FlexRadio-emulation protocol spec (Phase-0 reference)

**Extracted 2026-06-15 from `aethersdr/AetherSDR` source** (AE's *decoder* is the exact *encode* spec).
Citations are `file:line` in that repo. `[EXPLICIT]` = read directly in code; `[INFERRED]` = deduced;
`[CAPTURE]` = confirm against a real Flex 6300↔AE traffic capture before relying on it.

This is the **standard SmartSDR/FlexLib text protocol**: discovery = plaintext UDP key=value; control =
line-based ASCII over TCP; only bulk spectrum/audio = VITA-49 over UDP.

---

## 1. Discovery  (UDP broadcast → :4992)
- AE binds **UDP 4992** (`AnyIPv4`, ShareAddress) and passively listens. `[EXPLICIT RadioDiscovery.h:131 DISCOVERY_PORT=4992]`
- Emulator **broadcasts** a plaintext, space-separated `key=value` datagram. Verbatim example:
  ```
  name=flex-sim model=FLEX-6600 serial=EMULATE01 version=3.3.28.0 ip=<emu-ip> port=4992 status=Available mf_enable=1 max_licensed_version=3
  ```
- **`serial` is the ONLY hard requirement** (empty serial → ignored). `ip` defaults to sender addr, `port` defaults to 4992. `[EXPLICIT RadioDiscovery.cpp:onReadyRead]`
- For a clean single-client entry: include `model`,`version`,`status=Available`,`mf_enable=1`, and advertise **no** `gui_client_*` (else multiFLEX-conflict dialog pauses AE). `[EXPLICIT RadioModel.cpp:~2075]`
- **Cadence:** AE drops a radio after **5000 ms** unseen → broadcast every ~1 s (SmartSDR norm). `[EXPLICIT RadioDiscovery.h STALE_TIMEOUT_MS=5000]`

## 2. Control connect  (TCP :4992 — radio speaks first)
On TCP accept, emulator immediately sends (ASCII, `\n`-terminated):
1. `V1.4.0.0\n`  — protocol version. `[EXPLICIT CommandParser.cpp case 'V']`
2. `H1A2B3C4D\n` — client handle (any nonzero 32-bit **hex**). **Receiving `H` flips AE to Connected.** `[EXPLICIT RadioConnection.cpp case Handle]`

**Wire grammar** `[EXPLICIT CommandParser.cpp]`:
| Dir | Form | Notes |
|---|---|---|
| AE→radio | `C<seq>\|<command>\n` | `seq` decimal, from 1, ++ per command |
| radio→AE | `R<seq>\|<hexcode>\|<body>` | **code is HEX, 0=OK**; `seq` decimal; `body` = reply payload (also k=v) |
| radio→AE | `S<hexhandle>\|<object> <k=v…>` | status; object ends at last space before first `=` |
| radio→AE | `V…` / `H…` / `M<hex>\|<text>` | version / handle / message |
- Bare OK reply = `R<seq>|0|`. Heartbeat: AE sends `C<seq>|ping` → reply `R<seq>|0|`. AE writes a `0x04` byte before TCP close (ignore).

## 3. Connect sequence  (discovered → pan flowing)  `[EXPLICIT RadioModel.cpp]`
Reply `R<seq>|0|` to everything unless noted. Order AE drives after `H`:
- **A. multiFLEX peek:** `sub radio all`, `sub client all` (emit a `radio` status w/ `mf_enable=1`).
- **B. GUI register (this order):** `client program AetherSDR` · `client gui <maybe-empty>` *(reply body = a UUID; AE persists it)* · `client station <name>` · `client set send_reduced_bw_dax=1` · `client set enforce_network_mtu=1 network_mtu=1450` · `keepalive enable`.
- **C. subscriptions (back-to-back):** `sub slice all`,`sub pan all`,`sub tx all`,`sub atu all`,`sub amplifier all`,`sub meter all`,`sub audio all`,`sub gps all`,`sub apd all`,`sub client all`,`sub xvtr all`; then `mic list` (reply body e.g. `MIC,LINE`). (A later batch: `sub tnf/memories/cwx/dax/daxiq/radio/codec/dvk/navtex/usb_cable/spot/waveform/license all` — OK to all; none required for pan.)
- **D. UDP data-port discovery (how the radio learns where to send VITA-49):**
  1. AE binds an ephemeral local UDP port and **sends a single `0x00` byte from it to `radio_ip:4992` (UDP)**, re-primed every 250 ms for ≤2 s. → **Emulator captures that datagram's source IP:port = the VITA-49 destination.** `[EXPLICIT PanadapterStream.cpp:start]`
  2. AE also sends `client udpport <localUdpPort>` (TCP). Reply `R|0|`. `[EXPLICIT RadioModel.cpp:~2278]`
- **E. info/slices/pan:** `info` (reply comma-kv incl. `model=…,chassis_serial=…`) · `slice list` (**empty body → AE goes standalone and creates its own pan**) · `stream create netcw` / `stream create type=remote_audio_tx compression=opus` (reply hex id or error — tolerated).
- **Pan creation (standalone path):**
  - AE → `display panafall create x=100 y=100` → **reply `R|0|0x40000000,0x42000000`** = **`<panId>,<waterfallId>`** (comma form, pan id first; keyed `pan=0x40000000 waterfall=0x42000000` also accepted). `[EXPLICIT RadioStatusOwnership.h:48 parsePanafallCreatePanId + tests/radio_status_ownership_test.cpp:266-274]`
  - AE → `slice create pan=0x40000000 freq=14.225000` → reply `R|0|0` (slice index).
  - AE → `display pan set 0x40000000 xpixels=<W> ypixels=<H>` and `… min_dbm=-130 max_dbm=-40` → reply OK (update your y_pixels/dbm so scaling matches §5).

## 4. Required STATUS lines  (the crucial bit — AE won't decode until it gets these)
Emit as `S<handle>|…` using the **same handle from `H`**. `client_handle` match = ownership; without it AE *defers* the status. `[EXPLICIT RadioModel.cpp:~4298 classifyOwnedStatus]`
```
S1A2B3C4D|display pan 0x40000000 client_handle=0x1A2B3C4D waterfall=0x42000000 center=14.225 bandwidth=0.2 min_dbm=-130 max_dbm=-20 x_pixels=1024 y_pixels=700 fps=25 ant_list=ANT1
S1A2B3C4D|display waterfall 0x42000000 client_handle=0x1A2B3C4D panadapter=0x40000000 line_duration=100 auto_black=1 black_level=15 color_gain=50
S1A2B3C4D|slice 0 client_handle=0x1A2B3C4D pan=0x40000000 freq=14.225000 mode=USB
```
- **pan id `0x40000000` = the VITA-49 Stream ID for FFT packets.** `[EXPLICIT PanadapterModel.h panStreamId()]`
- **`waterfall=0x42000000` tells AE the waterfall stream id; that = the VITA-49 Stream ID for tile packets.** `[EXPLICIT PanadapterModel.h wfStreamId()]`
- `y_pixels` drives the dBm↔pixel conversion (§5). FlexRadio id convention: pan `0x4000xxxx`, waterfall `0x4200xxxx`.
- AE may reply `display panafall set 0x42000000 auto_black=0 …` — reply OK.

## 5. VITA-49 UDP wire format  (all BIG-ENDIAN)  `[EXPLICIT PanadapterStream.cpp]`
**28-byte header (7 words), payload starts at byte 28:**
| Word | Bytes | Field | AE uses |
|---|---|---|---|
| 0 | 0–3 | header | type nibble=**0x3**; **T-bit `0x04000000`** (bit26, +4-byte trailer if set); **seq = bits 19:16** (mod-16 continuity) |
| 1 | 4–7 | **Stream ID** | must = pan/wf/meter id |
| 2 | 8–11 | Class OUI | **ignored** (set real Flex OUI 00-1C-2D for realism) |
| 3 | 12–15 | …\|**PCC (low16)** | **routing key** |
| 4–6 | 16–27 | timestamps | ignored |

PCC constants `[EXPLICIT PanadapterStream.h:135-141]`: **`0x8003`=FFT/pan**, **`0x8004`=waterfall**, **`0x8002`=meter**, 0x8005=Opus, 0x03E3/0x0123=audio. AE masks word3 `& 0xFFFF` — only PCC matters. Packet-size field & type nibble are **not** validated (UDP length used).

**FFT payload (PCC 0x8003)** — 12-byte sub-header @ byte28, then bins:
`uint16 startBin · uint16 numBins · uint16 binSize(=2) · uint16 totalBins · uint32 frameIndex`, then `numBins × uint16` BE.
- **Each bin value = a pixel Y coordinate, NOT dBm.** 0 = top (=maxDbm), `yPixels-1` = bottom (=minDbm).
- Decode: `dbm = maxDbm − (pixel/(yPix−1))·(maxDbm−minDbm)`. **To paint level `D`:** `pixel = (maxDbm−D)/(maxDbm−minDbm)·(yPixels−1)`.
- Defaults if unset: **minDbm/maxDbm = −130/−40, yPixels = 700.** Single-packet frame: `startBin=0, numBins=totalBins=N, binSize=2`.

**Waterfall payload (PCC 0x8004)** — 36-byte sub-header @ byte28, then bins:
`int64 FrameLowFreq · int64 BinBandwidth · uint32 LineDurationMS(@+16) · uint16 Width(@+20) · uint16 Height(@+22) · uint32 Timecode(@+24) · uint32 AutoBlackLevel(@+28) · uint16 TotalBinsInFrame(@+32) · uint16 FirstBinIndex(@+34)`, then `Width × uint16` BE (AE reads row 0 only).
- Intensity: read as **signed int16 ÷ 128.0** (noise ~96–106, peaks ~110–115 → raw ≈ 12300–14700).
- New frame on `Timecode` **or** `TotalBinsInFrame` change. Single-packet: `FirstBinIndex=0, Width=TotalBinsInFrame=N, Height=1`.
- **Frequency axis (VitaTileFrequency.h):** start-edge + per-bin BW (NOT center/span). Auto-detect by `|FrameLowFreq|`:
  `≥ 1e11` → **VitaFrequency = Hz×2^20**; else **plain Hz**. *Same scale applied to BinBandwidth.* `highMhz = lowMhz + binBwMhz·Width`. (This is the #3457 area — old code had a 1 GHz ceiling, now removed.)

**Meter payload (PCC 0x8002)** — `N × {uint16 meter_id, int16 raw_value}` BE. Routed by **PCC alone** — no stream-id registration (`PanadapterStream.cpp:586,728`), any stream id works. Each `meter_id` must first be **defined via a status line** so AE knows its source/name/unit:
`S<handle>|meter <id>.src=SLC#<id>.num=<sliceIdx>#<id>.nam=LEVEL#<id>.unit=dBm#<id>.low=-150.0#<id>.hi=20.0` — **`#`-separated `index.key=value` tokens, NOT spaces, and no leading `meter <id>` index** (`RadioModel::handleMeterStatus`, `RadioModel.cpp:5131`; the `MeterModel.h:11` header comment showing spaces is misleading — space-separated is silently dropped). The **slice S-meter** = `src=SLC nam=LEVEL`; AE maps it to `sLevel` for that slice `[MeterModel.cpp:88]`. **Raw→value (`MeterModel::convertRaw`):** dBm/dB/dBFS/SWR → `raw/128`; Volts/Amps → `raw/256`; degF/degC → `raw/64`; else `raw` `[MeterModel.cpp:207-218]`. So an S9 (−73 dBm) S-meter = `raw = −73×128 = −9344`.

## 6. Minimum "first waterfall" checklist
1. Broadcast discovery (serial set) every 1 s →
2. Accept TCP, send `V…`+`H…` →
3. OK the sub/client/register commands; reply a UUID to `client gui` →
4. Capture the `0x00` prime source as VITA dest; OK `client udpport` →
5. Empty `slice list`; reply `0x40000000` to `display panafall create`; `0` to `slice create` →
6. Emit `display pan` + `display waterfall` status with **client_handle + waterfall=** →
7. Stream VITA-49 FFT (PCC 0x8003, stream 0x40000000) + waterfall tiles (PCC 0x8004, stream 0x42000000) to the captured dest. AE warns if no UDP within 10 s.

## 7. Remaining unknowns
**✓ CLOSED from source (2026-06-15):**
1. ~~`display panafall create` reply body~~ → **`<panId>,<waterfallId>`** comma form (pan first), or keyed `pan=0x… waterfall=0x…`. `[RadioStatusOwnership.h:48 + test:266-274]`
2. ~~Waterfall frequency encoding~~ → AE accepts `Hz×2^20` (when `|raw| ≥ 1e11`) or plain Hz. `[VitaTileFrequency.h:37-52]` **Use plain Hz** — see §8.5 (Hz×2^20 misdecodes at low/negative centers).

**Still `[CAPTURE]` — but none block a Phase-0 spike (realism/tuning/optional only):**
3. **Exact non-load-bearing VITA header bits** (OUI, InfoClassCode) — match a real Flex packet for realism (AE ignores them).
4. ~~Meter id→unit scaling table~~ → **CLOSED from `MeterModel.*` + live-validated** (see §5 meter payload + §8.6): define via `meter <id> …` status, then dBm = `raw/128`.
5. **Typical bins/frame, fps, MTU fragmentation** — real radios fragment to stay <~1450 B; mirror via startBin/firstBinIndex. AE itself doesn't cap.

> Capture plan: when home, run AE against the real Flex 6300 with a packet capture (Wireshark on :4992 TCP + the VITA UDP), or read `RadioStatusOwnership.cpp` + `VitaTileFrequency.h` + `MeterModel.*` to close items 1/2/4 from source.

## 8. Live-validation findings — real AE v26.6.3, 2026-06-15
Validated **end-to-end against real AetherSDR** (sim in WSL2, AE on the Windows host): discovery → handshake → **panadapter + waterfall + VFO** all render. Four corrections to the above, each confirmed from AE source + its diagnostic logs:

1. **Slice status frequency key is `RF_frequency=`, NOT `freq=`.** The slice *create command* uses `freq=`, but the *status* must report `RF_frequency=<MHz>` or `SliceModel::applyStatus` ignores it and the **VFO stays 0**. `[SliceModel.cpp:574-576]`
2. **Frequency must be coherent: slice ↔ pan ↔ waterfall.** Follow AE's `slice create … freq=F` → echo that freq as `RF_frequency` in the slice status, set pan center = F, and re-emit pan/waterfall status centered on F. If they disagree AE pushes bogus `display pan set center=…` overrides and the VFO collapses.
3. **Waterfall renders ONLY if the tile freq is within ±0.25 MHz of the pan center.** Else AE assumes a transverter, finds none (`WaterfallXVTR … reason=no_xvtr_evidence`), and blanks the row. So the tile `FrameLowFreq`/`BinBandwidth` must track AE's *current* center (parse `display pan set center=/bandwidth=`).
4. **Waterfall colormap threshold — the "rendering but black" trap.** `SpectrumWidget::intensityToRgb`: manual `blackThresh = 160 − black_level` (=145 at black_level 15; auto-black instead anchors to the measured noise floor). `rangeWidth = 120 − color_gain·0.91`. `t = clamp((intensity − blackThresh)/rangeWidth, 0, 1)`. The int16/128 intensity must **exceed blackThresh** to show colour — the "realistic" 96–117 range clamps to black at black_level 15. The spike uses **floor ≈ 140 / peak ≈ 220** so the test card is visible at default settings. (Perf counters `wfFps`/`wfVisibleRows`/`gpuWfRows` confirm tiles are decoded+drawn even when they look black — so a black waterfall = colour mapping, not a decode failure.)
5. **Waterfall freq encoding: use plain Hz** (`low_raw = round(low_hz)`), not Hz×2^20 — plain Hz is < 1e11 for any HF/VHF freq so AE decodes it unambiguously; Hz×2^20 misdecodes at low/negative centers.
6. **S-meter / meter plane (PCC 0x8002) — implemented + validated.** Define the slice meter once on `slice create` (`meter 1.src=SLC#1.num=0#1.nam=LEVEL#1.unit=dBm#1.low=-150.0#1.hi=20.0` — **`#`-separated**, see §5), then stream one meter packet per frame carrying the dBm **at the VFO bin** (`raw = dBm×128`). AE's S-meter then reads the injected level (S9 = −73 dBm, 6 dB/S-unit; calibrated test card). Routed by PCC only, so any stream id works (sim uses `0x46000000`). Reading the VFO bin (not just "the signal level") makes it physically correct per pattern: `carrier`/`staircase` read the carrier, `cal_tones` sit at the floor, `swept_carrier` blips as the tone crosses the VFO.
7. **TX power / SWR meters + transmit state.** Keying (AE's `transmit set mox=1`/`tune=1`, or the sim's own panel toggle) triggers two TCP status lines: `interlock state=TRANSMITTING source=SW tx_client_handle=<ourHandle>` (drives `m_radioTransmitting`; `tx_client_handle == ourHandle || 0` ⇒ AE sees us as TX owner) and `transmit mox=1 tune=… freq=… rfpower=… tunepower=…` (drives the MOX/TUNE UI). De-key → `state=READY` + `mox=0`. Define the TX meters like any meter (§5): `src=TX nam=FWDPWR unit=dBm` and `src=TX nam=SWR unit=SWR`. **FWDPWR is dBm; AE converts `watts = 10^(dBm/10)/1000`** (100 W = 50 dBm → raw 6400), so send `raw = (10·log10(W)+30)·128`. SWR uses `raw/128`. Stream the TX meters **continuously** — real W/SWR while keyed, ~0 W / 1.0 SWR otherwise — because AE only decays its (smoothed) power readout on *new* lower samples; stop sending on de-key and the meter holds the last value. `[MeterModel.cpp:458-476 (convertRaw + smoothing); RadioModel.cpp:4539 (transmit), 4656 (interlock)]`
8. **CW & CWX keying (CW-TX repro) — findings (2026-06-15).**
   - **CWX protocol** — AE→radio: `cwx wpm <n>`, `cwx send "<text>" <block>` (spaces encoded as byte `0x7f`), `cwx macro send <i>`, `cwx clear`, `cwx erase <n>`, `cwx qsk_enabled <0/1>`, `cwx delay <n>`. radio→AE status `cwx <kvs>`: `sent=<idx>` (per-char progress / `charSent`), `wpm=<n>`, `queue=0`/empty (buffer drained → AE releases MOX, #2450). `[CwxModel.cpp; RadioModel.cpp:3299/4637]` Status uses spaces here (NOT the meter `#` form). The sim decodes the text and keys TX per Morse (PARIS: dit = 1.2/wpm s; dah=3, intra-gap=1, char-gap=3, word-gap=7).
   - **Keying needs sub-dit time resolution** — at 20 wpm a dit is 60 ms; a 20 fps (50 ms) loop under-samples and drops/merges elements. The sim runs the keying loop at **50 fps during CW/CWX**, decoupled from the waterfall row rate (`tc = (now-start)·fps` = wall-clock row index, loop-rate independent; pan/wf throttled to one frame per row so a fast loop doesn't flood AE).
   - **End-of-over tail** — hold TX ~0.6 s after the last element so AE's local keyer (which starts a beat later, command latency) drains before unkey; otherwise the final character clips.
   - **Concurrency bug (ours — FIXED)** — the stream thread (status) and the command thread (replies) both wrote the same TCP socket → interleaved, corrupted status lines → AE mis-parsed → spurious TX drops. Serialize **all** socket writes with a lock. (Rare with only start/end status; frequent + clicky once per-char `sent=` was added — which is why `sent=` is now omitted.)
   - **The CW *audio* is AE-local and NOT a sim concern** — the sim emits no audio (no IQ/Opus). AE synthesises the CW sidetone with a **local keyer (`CwxLocalKeyer`)**. Intermittent sidetone breaks/stutter persist even when the sim drives a verified-clean keyed TX (one TX-on, held 37 s, one TX-off, zero mid-stream events — confirmed in the sim log). This is a **known-fragile AE area**: PRs [#3202](https://github.com/aethersdr/AetherSDR/pull/3202) (drift-correct keyer, closes [#2980](https://github.com/aethersdr/AetherSDR/issues/2980)), [#2754](https://github.com/aethersdr/AetherSDR/pull/2754) (CharGap), [#2181](https://github.com/aethersdr/AetherSDR/pull/2181) (keep audio gate open during CWX), [#3271](https://github.com/aethersdr/AetherSDR/pull/3271) (regression test); issues #2980 (macOS stutter), #2694 (Windows sidetone latency/distortion) — all closed pre-v26.6.3, yet stutter still reproduces on **Windows v26.6.3** → likely a residual Windows case the macOS-focused drift fix didn't cover. `flex-sim` is a hardware-free, deterministic repro for it.
   - **Waterfall during TX (#2126 / #1916)** — with real TX state + wall-clock timecode, AE v26.6.3 handles the TX gap **robustly**: only a small pause / thin 1-2 px marker, no blank or disappearance. **Not reproduced** — a *passing* regression baseline, not a failure.
