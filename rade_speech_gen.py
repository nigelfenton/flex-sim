#!/usr/bin/env python3
"""Deterministic speech generator for RADE golden-clip fixtures (Windows / SAPI).

A RADE golden clip can't be synthesised from a formula — RADE is a learned voice
modem, so the only way to mint a reference is to feed KNOWN speech through AE's
RADE encoder and capture the result (Tap E). The *speech* going in, though, should
be deterministic, or the "golden" clip isn't reproducible.

This emits a fixed Harvard sentence (the phonetically-balanced audio-testing
convention) spoken by a pinned SAPI voice at a pinned rate, to a WAV with an
EXPLICIT format (16 kHz / 16-bit / mono = RADE's internal speech rate, so no
resampling artefact is introduced before the encoder). Same inputs -> same bytes.

The output WAV is the *source* for the capture step (route it into AE as TX audio
via a virtual cable, AE in RADE TX, grab Tap E). It is NOT itself the golden clip.

Windows only (uses System.Speech via PowerShell). Pure stdlib otherwise.

Usage:
    python rade_speech_gen.py                       # default sentence/voice -> ./fixtures/
    python rade_speech_gen.py --list                # list installed SAPI voices
    python rade_speech_gen.py --sentence 2 --voice Zira
    python rade_speech_gen.py --out DIR --rate 0
"""
import argparse
import hashlib
import os
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))

# RADE's internal speech sample rate (third_party/radae rade_api.h:
# RADE_SPEECH_SAMPLE_RATE = 16000). Emitting at 16k mono means the source enters
# the encoder chain with no pre-resample; AE upsamples to 24k at the device edge.
SPEECH_RATE_HZ = 16000
BITS = 16
CHANNELS = 1

# Harvard sentences (IEEE Recommended Practice for Speech Quality Measurements):
# phonetically balanced, the standard intelligibility set. A fixed pick = a fixed
# fixture; the index is recorded in the filename + manifest for traceability.
HARVARD = [
    "The birch canoe slid on the smooth planks.",            # 0 — list 1, s1 (classic)
    "Glue the sheet to the dark blue background.",           # 1 — list 1, s2
    "It's easy to tell the depth of a well.",                # 2 — list 1, s3
    "These days a chicken leg is a rare dish.",              # 3 — list 1, s4
    "The juice of lemons makes fine punch.",                 # 4 — list 1, s5
]

DEFAULT_VOICE = "David"      # Microsoft David Desktop (en-US male) — universal default
DEFAULT_RATE = 0             # SAPI rate -10..+10; 0 = natural pace


def list_voices():
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.GetInstalledVoices()|%{$i=$_.VoiceInfo;"
        "Write-Output ($i.Name+'  ['+$i.Culture+'  '+$i.Gender+']')}"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
    return r.returncode


def synth(sentence_idx, voice_substr, rate, out_path):
    text = HARVARD[sentence_idx]
    # Pin an explicit wave format so the WAV is identical across machines/runs.
    # SpeechAudioFormatInfo(samplesPerSecond, AudioBitsPerSample.Sixteen, AudioChannel.Mono)
    # SelectVoiceByHints would be fuzzy; match the installed name by substring instead.
    ps = f"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $s.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Name -like '*{voice_substr}*' }} | Select-Object -First 1
if (-not $voice) {{ throw "no SAPI voice matching '{voice_substr}'" }}
$s.SelectVoice($voice.VoiceInfo.Name)
$s.Rate = {rate}
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({SPEECH_RATE_HZ}, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{out_path}', $fmt)
$s.Speak('{text}')
$s.SetOutputToNull()
$s.Dispose()
Write-Output 'OK'
"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0 or "OK" not in r.stdout:
        raise RuntimeError(f"SAPI synth failed: {r.stderr.strip() or r.stdout.strip()}")
    return text


def wav_info(path):
    with wave.open(path, "rb") as w:
        nch, sw, fr, nf = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    dur = nf / fr if fr else 0
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
    return {"channels": nch, "sample_width_bytes": sw, "rate_hz": fr,
            "frames": nf, "duration_s": round(dur, 3), "sha256_16": digest}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list installed SAPI voices and exit")
    ap.add_argument("--sentence", type=int, default=0,
                    help=f"Harvard sentence index 0..{len(HARVARD)-1} (default 0)")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="voice name substring (default David)")
    ap.add_argument("--rate", type=int, default=DEFAULT_RATE, help="SAPI rate -10..+10 (default 0)")
    ap.add_argument("--out", default=os.path.join(HERE, "fixtures"))
    args = ap.parse_args()

    if args.list:
        return list_voices()
    if not (0 <= args.sentence < len(HARVARD)):
        sys.exit(f"sentence index must be 0..{len(HARVARD)-1}")

    os.makedirs(args.out, exist_ok=True)
    safe_voice = args.voice.lower().replace(" ", "")
    name = f"rade_src_s{args.sentence}_{safe_voice}_r{args.rate}_16k"
    wav_path = os.path.join(args.out, name + ".wav")

    print(f"Synthesising Harvard sentence #{args.sentence}: \"{HARVARD[args.sentence]}\"")
    print(f"  voice ~ '{args.voice}', rate {args.rate}, format {SPEECH_RATE_HZ} Hz/{BITS}-bit/mono")
    text = synth(args.sentence, args.voice, args.rate, wav_path)
    info = wav_info(wav_path)

    # Manifest beside the WAV so a captured Tap E can be traced to its source speech.
    manifest = {
        "purpose": "RADE golden-clip SOURCE speech (feed into AE RADE TX, capture Tap E)",
        "sentence_index": args.sentence, "sentence_text": text,
        "voice_substr": args.voice, "sapi_rate": args.rate,
        "wav": os.path.basename(wav_path), **info,
    }
    import json
    with open(os.path.join(args.out, name + ".json"), "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, indent=2)

    print(f"  -> {wav_path}")
    print(f"     {info['duration_s']}s, {info['rate_hz']} Hz, {info['channels']} ch, "
          f"sha256[:16]={info['sha256_16']}")
    print(f"  -> manifest: {name}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
