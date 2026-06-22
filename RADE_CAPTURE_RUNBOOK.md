# RADE golden-clip capture runbook

How to mint the golden RADE clip once AetherSDR is built with `RADE_WAV_TAP=ON`.
The clip is then replayed by flex-sim as RX so AE's RADE decoder recovers the
known sentence (the decode fixture).

## Prerequisites
- [x] `rade_speech_gen.py` — deterministic source speech (done; `fixtures/rade_src_s0_david_r0_16k.wav`)
- [ ] AE built with `-DRADE_WAV_TAP=ON` — Tap E writes to `build/rade_taps/`
- [ ] A virtual audio cable (VB-CABLE or Voicemeeter) to feed the WAV into AE as mic
- [ ] A Flex (real or flex-sim) for AE to connect to — RADE TX needs a radio target

## The chain
```
SAPI WAV (known sentence)
  -> virtual audio cable (set as AE's mic / TX input)
  -> AE in RADE TX mode (DIGU/DIGL, transmit dax=1)
  -> AE RADE-encodes on PTT
  -> Tap E writes build/rade_taps/  ← THE GOLDEN CLIP (24k stereo, modem waveform)
```

## Steps (capture)
1. **Install a virtual cable** (VB-CABLE is the lightweight default). It creates a
   "CABLE Input" playback device and a "CABLE Output" recording device.
2. **Set AE's TX audio source** to the virtual cable's recording side (CABLE Output),
   or set Windows default mic to it (simplest). AE reads "mic" from there.
3. **Connect AE to a radio** — flex-sim is fine (it's a Flex as far as AE knows);
   `python flex_sim.py --ae <AE-host-ip>`. RADE needs a TX target even if no RF.
4. **Activate RADE on the TX slice** — right-click the slice → RADE. AE forces
   DIGU/DIGL and sets `transmit dax=1` (see [[ae-rade-pipeline]]).
5. **Arm capture + play the speech into the cable + key PTT**, in this order:
   - Start PTT (MOX) on AE so RADE begins encoding
   - Play `fixtures/rade_src_s0_david_r0_16k.wav` to the **CABLE Input** playback device
     (e.g. `powershell` Media.SoundPlayer, or any player with output device = CABLE Input)
   - Let it play the full ~3 s sentence, then release PTT (triggers the EOO frame)
6. **Collect Tap E** from `build/rade_taps/` — the file with `_E_` / "full_session"
   in its name = the golden clip. Copy it to `flex-sim/fixtures/rade_golden_<sentence>.wav`.

## Steps (verify the loop) — later
7. Replay the golden clip via flex-sim as RX audio (flex-sim already streams 24k
   stereo; the clip IS 24k stereo, so it drops in).
8. On a second AE (or the same one in RX), with RADE active, the decoder should
   recover "The birch canoe slid on the smooth planks." Intelligible = PASS.

## Notes / gotchas
- Tap E is **24 kHz stereo float32** (the modem waveform after upsample) — matches
  flex-sim's audio path exactly, no resample needed on replay.
- RADE TX is **mic-only** by default; the virtual cable is what lets a *file* drive it.
- Determinism: the SAPI source is byte-identical run-to-run, but the Tap E capture
  also depends on RADE encoder version — pin the AE git SHA in the golden clip's name.
- If no virtual cable is available, an alternative is the in-tree test-tone path, but
  that gives a tone not speech — speech is needed for a real intelligibility fixture.
```

## Capture session 1 (2026-06-22 ~01:18) — pipeline PROVEN, audio injection BLOCKED

**What worked (end-to-end pipeline confirmed):**
- RADE-TAP build runs, RADE engages on MOX, encodes, writes all 5 taps (A/B/D/E/F).
  Proven by AE log `01:18:36 RADE_WAV_TAP: wrote ...rade_tap_E_24k_stereo_full_session.wav`.
- Gotcha found: `writeWavFloat` does NOT create the tap dir — **`build/rade_taps/` must
  exist first** or every write silently `qWarning`s and fails. Created it; taps then wrote.
- Radio: Flex 6300, ANT2 -> dummy load, 20 W, DIGU. Safe TX. Good setup.

**What's BLOCKED — audio not reaching AE's mic:**
- Symptom: no AE mic/Level meter movement on TX; Tap E only **0.204 s** (RMS 0.35 — real
  modem signal, but just the EOO frame + ~nothing, because `m_tapVoiceAccum` got almost no
  voice frames = encoder received ~no input audio).
- Root cause: the WAV isn't flowing through VB-CABLE into AE. **Independent cable loopback
  test returned a 44-byte (header-only, silent) recording** — nothing traverses CABLE
  Input -> CABLE Output when played via `System.Media.SoundPlayer` / MCI.
- The real issue: `SoundPlayer`/MCI don't honour the CABLE device routing (they hit the
  primary endpoint, not CABLE Input) despite CABLE being the Windows default. NAudio 2.2.1
  net472 single-DLL won't load in PS 5.1 (ReflectionTypeLoadException — needs its split
  assemblies).

**FIX FOR NEXT SESSION (10 min, clear head):**
- Don't script the playback. Open the fixture WAV in a REAL media player (VLC / Films & TV /
  Windows Media Player) whose output is set to **CABLE Input** (or default-to-CABLE + speakers
  silent). Confirm AE's mic meter moves when it plays.
- Timing: key MOX, wait ~2 s for RADE to engage, THEN play the full 3.1 s WAV, hold ~1 s,
  unkey (EOO writes Tap E). Tap E should then be ~3+ s of speech-derived modem signal.
- Verify Tap E with: parse float WAV (fmt tag 3), check dur ~3 s and RMS > 0.05.
- `fixtures/rade_src_s0_david_r0_16k.wav` is the deterministic source (3.13 s, sha-stable).
- Partial 0.2 s proof-of-concept archived at `AetherSDR/build/rade_taps/attempt2_partial/`.
- ALSO: VB-CABLE is currently the Windows DEFAULT playback -> system audio is going to the
  cable (speakers silent). Flip default back to Realtek when done capturing.

## Capture session 2 (2026-06-22 ~01:33) — ✅ SOLVED, golden clip captured

**THE FIX: set AE's mic source to PC (not MIC).** That was the entire blocker. With
MIC selected, AE listens to a hardware mic — the cable audio never reaches the encoder.
With **PC** selected, AE takes TX audio from the PC/DAX path = CABLE Output = our WAV.

Observations that confirmed it:
- MIC source -> MOX keys radio, real RF. PC source -> AE shows TX on screen, radio does
  NOT key, no RF — and that is FINE: RADE encodes host-side, Tap E is written before the
  radio, so **no real RF is needed to capture the golden clip.**
- Result: Tap E = **430,892 bytes / 2.24 s**, 12/12 windows sustained modem signal,
  RMS 0.59. A complete RADE encoding of the birch-canoe sentence.

Working playback path (session 2): **Audacity** with Playback Device explicitly set to
**CABLE Input (VB-Audio)** -> CABLE Output -> AE PC mic. (Audacity respects the device
choice where SoundPlayer/MCI did not — that was session 1's red herring.)

Golden clip committed: `fixtures/rade_golden_s0_birchcanoe_ae2ade8d4c.wav` (+ .json manifest).

**Remaining (next): step 4 — replay it via flex-sim as RX, confirm AE RADE-decodes the
sentence (the actual decode fixture / intelligibility check).**

## Replay attempt (2026-06-22 ~01:48) — float-WAV path WORKS, blocked by same-host UDP prime

**Built + verified (committed):** WavPlayer now reads IEEE-float WAVs (RADE Tap-E is
float32 — `wave.open` rejects it). Golden clip reads at 24k/2ch/float, RMS 0.61,
ratio 1.0 (no resample). Replay *machinery* is done.

**Live decode BLOCKED — flex-sim same-host UDP prime gap (NOT a RADE/clip issue):**
- AE connects to flex-sim fine, sets up RADE (DIGU, slice tx=1), and DOES request the
  audio: `stream create type=remote_audio_rx compression=none`, announces `client udpport
  58724`. Control path 100% OK.
- BUT: flex-sim logs `no UDP prime in 12s — AE never opened the data path; cannot stream`.
  REPRODUCIBLE across reconnects.
- Network state confirmed: flex-sim prime listener bound `UDP 127.0.0.1:5992` (ok), AE bound
  `UDP 127.0.0.1:58724` (its announced rx port, ok), AND **AE holds `UDP 0.0.0.0:4992`**.
- Root cause (strong hypothesis): on ONE Windows host, AE's prime/data plane assumes the
  data port == discovery port (4992) — which AE itself owns — so the prime never reaches
  flex-sim's advertised :5992. flex-sim was validated in WSL2 where AE+flex-sim had SEPARATE
  network stacks (both could use 4992 on different IPs); single-host loopback breaks that.
- Code: `prime_loop` (flex_sim.py ~L731) binds `(self.ip, self.port)` = 127.0.0.1:5992 and
  only accepts prime from `ae_peer_ip` (127.0.0.1 — matches, not the filter's fault).

**FIXES TO TRY NEXT SESSION (pick one, needs iteration):**
1. Run flex-sim on a genuinely SEPARATE IP from AE so both can use 4992 (e.g. flex-sim on a
   second host / VM / the LAN IP, AE stays on this box) — matches the validated WSL2 topology.
2. Make flex-sim ALSO listen for the prime on AE's announced `client udpport` value, or bind
   the prime on 0.0.0.0 and/or additionally on :4992 if free.
3. Inspect AE source for where it sends the remote_audio_rx prime (which dest port) to confirm
   the 4992-assumption before coding.

**Everything else is GREEN:** golden clip captured + committed, replay reader works. Only the
flex-sim<->AE audio transport on one host remains.

## Replay session 2 (2026-06-22 ~02:02) — FULL CHAIN PROVEN, one scoped gap left

**FIXED tonight (committed):**
1. **Same-host UDP prime** — flex-sim now seeds `vita_dest` directly from AE's announced
   `client udpport` (flex_sim.py ~L886) instead of relying on a prime packet that never
   arrives when AE owns :4992 on the same host. Spectrum + audio data path now establish
   on one Windows box. `[udp] VITA dest from client udpport: ...` confirms.
2. **Live audio hot-swap** — `audio_loop` now re-reads `self.audio_source` each iteration
   (~L1198) so the control panel can switch tone->wav without recreating the stream.
   GOTCHA: the control-panel param is **`audio_src`** (NOT `audio_source`) — sending the
   wrong name sets only wav_path and the source stays on tone.

**The chain is PROVEN end-to-end:** golden clip -> flex-sim float-WAV replay -> vita_dest ->
AE receives it -> **the RADE modem waveform ARRIVES at AE intact (audible as a "warble")**.

**THE ONE REMAINING GAP — flex-sim sends audio on the wrong stream for RADE:**
- RADE RX decodes audio **only from the DAX RX path**: `PanadapterStream::daxAudioReady` ->
  `feedRxAudio(channel, pcm)` filtered to the slice's `daxChannel()`
  (AetherSDR/src/gui/MainWindow_DigitalModes.cpp:403-412). Comment there: RADE RX "requires
  DAX audio to be flowing first."
- flex-sim streams **`remote_audio_rx`** (general speaker audio) and has **ZERO DAX handling**
  (no dax_rx, no daxAudio in flex_sim.py). AE creates `stream create type=dax_rx dax_channel=1`
  but flex-sim never sends audio on it -> RADE decoder gets nothing -> we hear the raw warble,
  no decode.

**FIX (scoped flex-sim feature, ~1-2h, next session):** add a **DAX RX audio VITA-49 stream**
to flex-sim — distinct stream ID + AE's DAX-RX audio format — and stream the golden clip on
THAT path on the channel AE assigned (dax_channel=1), not remote_audio_rx. Check AE's DAX RX
decoder for the exact PCC/format. Then RADE will decode -> "the birch canoe slid on the smooth
planks" out the speaker.
