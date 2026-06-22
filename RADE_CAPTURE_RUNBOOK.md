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
