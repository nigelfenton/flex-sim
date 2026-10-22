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
  - AE → `slice create pan=0x40000000 freq=14.225000` → reply `R|0|0` (slice index). **Send the new slice's status (`slice 0 … in_use=1`) BEFORE that reply** — see §9.
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
- **Frequency axis (VitaTileFrequency.h):** start-edge + per-bin BW (NOT center/span). **AE ≥ #4412 decodes UNCONDITIONALLY as VitaFrequency = Hz×2^20** — the old auto-detect (`|FrameLowFreq| ≥ 1e11` → ×2^20, else plain Hz) is GONE. *Same scale applied to BinBandwidth.* `highMhz = lowMhz + binBwMhz·Width`. (History: #3457 removed the 1 GHz ceiling; #4412 removed the plain-Hz fallback.)

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
2. ~~Waterfall frequency encoding~~ → **`Hz×2^20` (VitaFrequency), unconditionally** since AE #4412. `[VitaTileFrequency.h]` The pre-#4412 magnitude auto-detect (and the old advice here to use plain Hz) is gone — see §8.5.

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
5. **Waterfall freq encoding: use Hz×2^20 (VitaFrequency)** (`low_raw = round(low_hz·2^20)`). ⚠ REVERSED 2026-08-01: this entry used to say "use plain Hz" (safe under the pre-#4412 magnitude auto-detect). **AE ≥ #4412 decodes Hz×2^20 unconditionally**, so plain Hz lands ~2^20 low → tile fails the ±0.25 MHz pan-proximity check (§8.3) → **waterfall black while the pan stays correct**. Found live via AE's `WaterfallXVTR` log (tile_mhz = pan_center/2^20 to the digit); fix mirrored from Aether-gate a431886; pinned by `tests/test_wf_packet.py`.
6. **S-meter / meter plane (PCC 0x8002) — implemented + validated.** Define the slice meter once on `slice create` (`meter 1.src=SLC#1.num=0#1.nam=LEVEL#1.unit=dBm#1.low=-150.0#1.hi=20.0` — **`#`-separated**, see §5), then stream one meter packet per frame carrying the dBm **at the VFO bin** (`raw = dBm×128`). AE's S-meter then reads the injected level (S9 = −73 dBm, 6 dB/S-unit; calibrated test card). Routed by PCC only, so any stream id works (sim uses `0x46000000`). Reading the VFO bin (not just "the signal level") makes it physically correct per pattern: `carrier`/`staircase` read the carrier, `cal_tones` sit at the floor, `swept_carrier` blips as the tone crosses the VFO.
7. **TX power / SWR meters + transmit state.** Keying (AE's `transmit set mox=1`/`tune=1`, or the sim's own panel toggle) triggers two TCP status lines: `interlock state=TRANSMITTING source=SW tx_client_handle=<ourHandle>` (drives `m_radioTransmitting`; `tx_client_handle == ourHandle || 0` ⇒ AE sees us as TX owner) and `transmit mox=1 tune=… freq=… rfpower=… tunepower=…` (drives the MOX/TUNE UI). De-key → `state=READY` + `mox=0`. Define the TX meters like any meter (§5): `src=TX nam=FWDPWR unit=dBm` and `src=TX nam=SWR unit=SWR`. **FWDPWR is dBm; AE converts `watts = 10^(dBm/10)/1000`** (100 W = 50 dBm → raw 6400), so send `raw = (10·log10(W)+30)·128`. SWR uses `raw/128`. Stream the TX meters **continuously** — real W/SWR while keyed, ~0 W / 1.0 SWR otherwise — because AE only decays its (smoothed) power readout on *new* lower samples; stop sending on de-key and the meter holds the last value. `[MeterModel.cpp:458-476 (convertRaw + smoothing); RadioModel.cpp:4539 (transmit), 4656 (interlock)]`
8. **CW & CWX keying (CW-TX repro) — findings (2026-06-15).**
   - **CWX protocol** — AE→radio: `cwx wpm <n>`, `cwx send "<text>" <block>` (spaces encoded as byte `0x7f`), `cwx macro send <i>`, `cwx clear`, `cwx erase <n>`, `cwx qsk_enabled <0/1>`, `cwx delay <n>`. radio→AE status `cwx <kvs>`: `sent=<idx>` (per-char progress / `charSent`), `wpm=<n>`, `queue=0`/empty (buffer drained → AE releases MOX, #2450). `[CwxModel.cpp; RadioModel.cpp:3299/4637]` Status uses spaces here (NOT the meter `#` form). The sim decodes the text and keys TX per Morse (PARIS: dit = 1.2/wpm s; dah=3, intra-gap=1, char-gap=3, word-gap=7).
   - **Keying needs sub-dit time resolution** — at 20 wpm a dit is 60 ms; a 20 fps (50 ms) loop under-samples and drops/merges elements. The sim runs the keying loop at **50 fps during CW/CWX**, decoupled from the waterfall row rate (`tc = (now-start)·fps` = wall-clock row index, loop-rate independent; pan/wf throttled to one frame per row so a fast loop doesn't flood AE).
   - **End-of-over tail** — hold TX ~0.6 s after the last element so AE's local keyer (which starts a beat later, command latency) drains before unkey; otherwise the final character clips.
   - **Concurrency bug (ours — FIXED)** — the stream thread (status) and the command thread (replies) both wrote the same TCP socket → interleaved, corrupted status lines → AE mis-parsed → spurious TX drops. Serialize **all** socket writes with a lock. (Rare with only start/end status; frequent + clicky once per-char `sent=` was added — which is why `sent=` is now omitted.)
   - **The CW *audio* is AE-local and NOT a sim concern** — the sim emits no audio (no IQ/Opus). AE synthesises the CW sidetone with a **local keyer (`CwxLocalKeyer`)**. Intermittent sidetone breaks/stutter persist even when the sim drives a verified-clean keyed TX (one TX-on, held 37 s, one TX-off, zero mid-stream events — confirmed in the sim log). This is a **known-fragile AE area**: PRs [#3202](https://github.com/aethersdr/AetherSDR/pull/3202) (drift-correct keyer, closes [#2980](https://github.com/aethersdr/AetherSDR/issues/2980)), [#2754](https://github.com/aethersdr/AetherSDR/pull/2754) (CharGap), [#2181](https://github.com/aethersdr/AetherSDR/pull/2181) (keep audio gate open during CWX), [#3271](https://github.com/aethersdr/AetherSDR/pull/3271) (regression test); issues #2980 (macOS stutter), #2694 (Windows sidetone latency/distortion) — all closed pre-v26.6.3, yet stutter still reproduces on **Windows v26.6.3** → likely a residual Windows case the macOS-focused drift fix didn't cover. `flex-sim` is a hardware-free, deterministic repro for it.
   - **Waterfall during TX (#2126 / #1916)** — with real TX state + wall-clock timecode, AE v26.6.3 handles the TX gap **robustly**: only a small pause / thin 1-2 px marker, no blank or disappearance. **Not reproduced** — a *passing* regression baseline, not a failure.

## 9. TX routing for TCI / API clients — 2026-10-10

Found driving AE 26.10.1's TCI server from CAT4OM (a third-party CAT hub). Each gap made a correct client look broken against the sim; all three are pinned by `tests/test_tx_routing.py`.

1. **`xmit 1` / `xmit 0` is a PTT verb.** AE's TCI server turns `trx:N,true;` into `xmit 1`, and API clients such as CAT4OM's native Flex driver send it directly — not `transmit set mox=`. It keys like MOX and answers with the same `interlock` / `transmit` status pair (§8.7). Without it AE logged `TCI PTT confirmation timeout: radio never reported keyed` and unkeyed after ~1.25 s (correct fail-closed behaviour on AE's part). A malformed `xmit` keys nothing and is answered `0x5000002D` (Bad field).
2. **`slice create` announces the slice before replying.** AE's TCI VFO-B route (`TciServer`, `vfo:N,1,<hz>;` with no second slice yet) looks the new slice up by the index in the reply. Reply first and the slice does not exist yet: AE logs `VFO-B create reply had no settled slice: <n>`, abandons it, and creates *another* slice on the next VFO-B or split request — slices leak (A, B, C …).
3. **`slice set N tx=1` moves the radio's single TX slice.** It is how AE routes TX for TCI split / VFO B. The previous TX slice loses the flag and both slices are re-announced (`tx=1` / `tx=0`) so the client's TX-slice view follows. Diversity children never take TX; `tx=0` is ignored (a radio always has exactly one TX slice).

## 10. TX audio state for the client-side voice keyer — 2026-10-11

Found testing aethersdr/AetherSDR#6292 (client-side voice keyer, RFC #4214) against the sim. Pinned by `tests/test_tx_audio_state.py`.

1. **Mic source.** `mic list` answers `MIC,BAL,LINE,ACC,PC` like a real 6000. AE selects the source with `mic input <src>` and reads it back from the `transmit` status, which now carries `mic_selection=<src>` and `dax=0|1`. #6292 refuses to record unless the radio reports `PC`.
2. **Licence.** `sub license all` is answered with `license feature name=digital_voice_keyer enabled=1`, or `enabled=0` with `--no-dvk-license`. AE fails *open* when it hears nothing, so before this every sim looked DVK-licensed and the unlicensed path could not be exercised.
3. **`remote_audio_tx` id.** It gets its own id (`0x88000000` + radio). It used to share `stream create netcw`'s `0x48000000`.
4. **`sub tx all`** is answered with the current `interlock` / `transmit` status, so a client that reconnects while the radio is keyed learns it.
5. **Client drop unkeys.** A client that disconnects while keyed (MOX, `xmit` or TUNE) is unkeyed, as its TX slice goes with it. The sim used to stay keyed, so the next client inherited a carrier nobody owned.

## 11. Licensing options — 2026-10-12

Asked for by K5PTB, to test AE against different licences. Pinned by `tests/test_licensing.py`; checked end to end against the #6292 build.

1. **What the radio sends.** `sub license all` is answered with the three FlexLib licence objects:
   - `license radio_id=<serial> highest_major_version=4 region=USA`
   - `license subscription name=smartsdr+|smartsdr+_early_access expiration=<YYYY-MM-DD>T00:00:00Z` (left out for `none`)
   - `license feature name=<feature> enabled=0|1 reason=<word>`, one per feature

   Default: an active SmartSDR+ subscription (expires a year from today) and `digital_voice_keyer enabled=1 reason=plus`.
2. **Startup flags.** `--subscription none|smartsdr+|early-access`, `--subscription-expires YYYY-MM-DD` (a past date is an expired subscription), `--license-feature NAME=0|1[:REASON]` (repeatable; reason defaults to `plus`/`none`), `--no-dvk-license` (kept; same as `--license-feature digital_voice_keyer=0`), `--dvk-refuses`. A malformed value stops the sim at startup.
3. **Live changes.** `GET /license` on the control port reads the licence; with a query it changes it and pushes the new status lines to the connected client, no reconnect:
   - `?feature=NAME&enabled=0|1[&reason=WORD]`
   - `?subscription=none|smartsdr%2B|early-access[&expires=YYYY-MM-DD]`
   - `?dvk_refuses=0|1`
   - rack mode adds `&radio=N`

   Everything is validated before anything changes; bad input gets `{"ok": false, "error": ...}` and changes nothing. `/state` carries the licence too.
4. **DVK refusal.** With `dvk_refuses` on, every `dvk ...` command is answered `0x50004001`, as a radio that won't store recordings does, sometimes while it still reports the feature enabled. AE treats that refusal as the reliable "not licensed" signal.
5. **What AE does with it (#6292 build, 2026-10-12).** With the keyer source on *Automatic*, AE follows a live change both ways: licence off → title `Digital Voice Keyer (Local)` and the *Radio DVK* item is removed from the menu; licence on → back to `(Radio)`. An explicit *Local* or *Radio* choice is saved in AE's settings and overrides the licence, so start from Automatic when testing. AE only upgrades its subscription label within a session, so test a downgrade with a fresh connection.

## 12. SmartSDR v4.3 firmware (`--firmware 4.3`) — 2026-10-14

SmartSDR v4.3.16 (released 2026-10-13, FlexLib 4.3.16.42034) changed parts of the wire protocol. aethersdr/AetherSDR#6437 maps every change from FlexLib 4.1.5. `--firmware 4.3` turns on the ones below, so AE's 4.3 work can be tested without upgrading a radio. The default (`3.3.28.0`) is unchanged. Other presets are `3.3`, `4.1` and `4.2`, or give a full version string; anything 4.3 or later gets these behaviours. Pinned by `tests/test_firmware_43.py`, which checks each one against both an old-firmware sim and a 4.3 sim.

1. **Version.** Discovery `version=`, the `V` line and `info` `software_ver=` all report the chosen version, e.g. `4.3.16.42034`.
2. **PTT Override** (#6442). The transmit status gains `ptt_mic_override=0|1` (default 0). Older firmware never reports the key, which is how a client tells the two apart. `transmit set ptt_mic_override=0|1` sets it, and any other value gets `0x5000002D` (Bad field). On older firmware the key is ignored.
3. **Interlock source list.** `interlock source=` lists **every** latched source, hardware first, comma-joined: `MIC,VOX`, `MIC,VOX,SW`. VOX is reported as `VOX`. Older firmware names one source (hardware wins) and reports VOX as `SW`.
4. **What a hardware PTT transmits.** Before 4.3 a hardware PTT (MIC, ACC or RCA) always forced the analog mic. On 4.3 it transmits the **selected** source, DAX included, unless PTT Override is on. MOX, TUNE, `xmit` and VOX always use the selected source. `/state` and `/txchain` report the result as `tx_audio_source`.
   - **Simulate a footswitch** from the control port: `GET /hwptt?src=MIC|ACC|RCA|VOX&on=0|1` (rack: `&radio=N`). It keys the radio and emits interlock and transmit status like a real PTT.
5. **Slice DSP levels.** The client sends `lms_nr_level`, `lms_anf_level` and `speex_nr_level`, and v4.3 reports them as `nrl_level`, `anfl_level` and `nrs_level`. NRS is clamped to 0..50 (SMART-12540; was 0..100). `nrf_level` is gone (NRF is tuned by `nrf_wlen`), so the sim ignores it and doesn't echo it. *Assumption: whether real 4.3 firmware ignores `nrf_level` or refuses it is unverified (#6437).* Older firmware echoes the command names.
6. **Per-slice audio rate.** `slice set N sample_rate=24000|48000|96000`, reported as `sample_rate=` in slice status; any other rate gets Bad field. RX audio for a 48 or 96 kHz active slice uses the new class codes:

   | Rate | Full BW (float32 stereo) | Reduced BW (int16 mono) |
   |---|---|---|
   | 24 kHz | `0x03E3` | `0x0123` |
   | 48 kHz | `0x03E4` | `0x0124` |
   | 96 kHz | `0x03E5` | `0x0125` |

   Packets stay 128 frames, so 48 kHz sends twice as many and 96 kHz four times as many. The sim's sources make 24 kHz audio, which is upsampled by linear interpolation; that's fine for testing the routing, not for judging audio quality. Older firmware has no `sample_rate` and streams 24 kHz only.
7. **APD (SmartSignal).** `sub apd all` and `apd enable=0|1` are answered.
   - **v4.3** adds `state=OFF|IDLE|ACTIVE` (OFF when disabled, ACTIVE while keyed), plus `apd equalizer_active=<0|1> ant=ANT1 freq=<MHz> rfpower=<W>`.
   - **Older firmware** sends `equalizer_active` on the enable line.
   - TRAINING, HOLD and FAULT aren't simulated.
8. **UDP keepalives.**
   - FlexLib 4.2.20+ sends `client udp_register handle=..` every 50 ms until the first VITA packet arrives, then every 5 s. Older clients sent `client ping handle=..`.
   - AE sends them to the radio's port 4991, the DAX TX port. The sim counts both kinds (`/state` → `udp_keepalive`).
   - **Fixed:** they used to be counted as DAX TX audio, so a `client ping` every 5 s could make txchain stage 3 flicker true.
9. **DAX TX rate.** v4.3 firmware rejects TX audio whose rate differs from the TX slice's ("DAX TX source rate != slice rate"). On 4.3 the txchain gains stage **3b: TX audio rate matches the TX slice**, judged from the class code of the incoming packets (`/txchain` → `audio_pcc`).
10. **CWX quote stand-in.** Byte `0x1E` inside the quoted text of `cwx send` is read as `"`, so a quote no longer ends the argument early.

**Not covered:** SmartLink WAN sessions, and anything 8000/Aurora-only (AutoDX, NTP, Noise Floor Range).

**Checked against a real AE:** the #6292 build connected to a `--firmware 4.3` sim, showed version 4.3.16.42034, and took `ptt_mic_override` in the transmit status without complaint (2026-10-14).

## 13. Multiple clients (MultiFlex + non-GUI controllers) — 2026-10-21

A real radio serves several API clients at once. The case that drove this is a front-panel controller (a CTR2-Max running custom firmware) reaching the radio through AetherSDR's CTR2 USB relay: AE opens a **second, dedicated TCP connection** to the radio and relays the controller's Flex API bytes unchanged. Single-client, the sim never answered it. Pinned by `tests/test_multiclient.py`.

1. **One thread per TCP connection.** Each gets `V<version>` and its **own** `H<handle>`, and has its own send lock. The first connection keeps the long-standing handle (`0x1A2B3C4D` + radio id), so a lone client sees exactly what it always did; later ones step in bit 20 (`0x1A3B3C4D`, …). Status lines are sent as `S<recipient's handle>|…`.
2. **Shared radio state stays single** (slices, pans, TX, TNFs), serialised by one lock around command handling. Each slice and pan records its **owner**, and its status always carries `client_handle=<owner>`, whichever client caused the change.
3. **Who gets a status line.** Status for a shared object goes to the client whose command caused it, the primary GUI client, and every client that sent `sub <topic> …` for it (`slice`, `pan`, `tx` for transmit and interlock, `meter`, `client`, `cwx`, `apd`, `tnf`, `amplifier`, `license`). Answers to a `sub` go only to the subscriber. `sub slice all` and `sub client all` report what already exists; `sub meter all` sends meter definitions once a slice exists.
4. **GUI and non-GUI clients.** A client that sends `client gui [<id>]` is a GUI client. The reply is its `client_id`, which is the id it offered (AE persists it) or a new UUID. It is then reported to `client` subscribers as `client 0x<handle> connected local_ptt=<0|1> client_id=<uuid> program=<program> station=<station>`, again after `client station`/`client program`, and as `client 0x<handle> disconnected forced=0 wan_validation_failed=0 duplicate_client_id=0` when it leaves. Spaces in program/station are sent as `0x7F`. A client that never sends `client gui` is non-GUI and is never reported.
5. **`client bind client_id=<uuid>`** (FlexLib `Radio.BindGUIClient`) binds a non-GUI client to that GUI client. Objects it creates (`slice create`, pans) then belong to the GUI client, and `slice list` lists the GUI client's slices. It can drive any slice: `slice tune N <MHz>`, `slice set N mode=…`, `slice set N audio_level=|audio_pan=|agc_mode=|agc_threshold=`, `filt N <lo> <hi>` (reported as `filter_lo=`/`filter_hi=`), and `xmit 0|1` / `transmit set mox=`. A `mode=`, `audio_level=` (etc.) or `filt` change is echoed as slice status to everyone, so the GUI client follows the panel.
6. **The primary client** gets the spectrum, waterfall, meter and audio streams (`vita_dest`). It is the first connection until a client sends `client gui`; if the first connection was not a GUI client, the GUI client takes over. When the primary leaves, the next GUI client, if any, becomes primary. Its streams restart when it creates a panadapter.
7. **TX ownership.** `tx_client_handle=` in the interlock status is the owner of the TX slice. With no slices it is the bound GUI client of whoever keyed, or the primary client.
8. **Disconnect is per client.** Only the leaving client's own slices (`slice N in_use=0` to the others) and pans are removed. TX is unkeyed only if that client keyed it or owns the TX slice. A lone client therefore gets the old behaviour: everything is cleared and TX is unkeyed. A controller that leaves while keyed unkeys; another client coming and going does not.
9. **Meters for a non-GUI client.** `client udpport <port>` from a client other than the primary registers `(its IP, port)`, and the meter packets (PCC `0x8002`) the primary gets are copied there. The spectrum and audio stay with the primary. Meters only flow while the primary is streaming. A controller behind AE's USB relay can't receive UDP anyway, so it reads state from status lines instead.

**What changed for a lone AE** (checked by diffing the wire output of AE's connect sequence before and after): the `client gui` reply echoes the id AE offered; AE now gets its own `client … connected` status; and `slice set N mode=` (and the new set-only keys) is echoed as slice status. All three are what a real radio sends. AE ignores its own `client connected` line apart from noting it (`RadioModel::onStatusReceived` only announces other handles). Nothing else changed.

**Guesses, not checked against a real radio:**
- The error code for `client bind` with an unknown `client_id` (the sim uses `0x5000002D`).
- Whether a real radio stops a non-GUI client from driving a slice that isn't its bound GUI client's. The sim doesn't enforce ownership.
- Who a real radio reports as `tx_client_handle` when a bound controller keys (the sim reports the TX slice's owner).
- Whether a real radio sends `client connected` for non-GUI clients without a `client_id` (the sim sends none).
- Whether `local_ptt` starts at 1 for the first GUI client.

**Not covered:** duplicate `client_id` (a real radio drops the older session with `duplicate_client_id=1`), `client disconnect <handle>`, per-client panadapter streams for a second GUI client, a second GUI client's remote audio, and discovery's `gui_client_*` fields (still advertised as none: see §1).

## 14. flex-sim extension: `sim` verbs — 2026-10-21

Not part of the Flex API. They let a client with only the API connection (a front-panel controller behind AE's CTR2 relay, with no HTTP access to the control panel) read and set the simulated signal. A real radio answers them with an error, so a client should send them only after `info` reports `name=flex-sim`. Any client may use them, bound controllers included. Pinned by `tests/test_multiclient.py`.

| Command | Reply |
|---|---|
| `sim get` | `R<seq>\|0\|level=<dBm> floor=<dBm> pattern=<name>` |
| `sim set level=<dBm>` | `R<seq>\|0\|`. Sets what the control panel's signal-level slider sets, clamped to its range (-130 to -10 dBm). |
| anything else, or a non-numeric level | `0x5000002D` |

The change shows on the next spectrum frame. No status line is sent: the level isn't a radio object.

**Autopan fix (same date).** `slice tune N <MHz> autopan=1` (and `slice set … autopan=1`) re-centres the slice's panadapter. It now re-announces `display pan … center=` (and the waterfall line) to every client that gets that pan's status, the way `display pan set center=` already did. Before, the client kept the old centre while the sim framed the FFT on the new one, so the `carrier` pattern (drawn at the VFO bin) appeared away from the slice flag.
