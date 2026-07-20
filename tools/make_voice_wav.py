#!/usr/bin/env python3
"""Turn text into a spoken WAV for the flex-sim noise bench 'voice' channel.

The noise bench needs a *wanted* speech signal that AE's noise reduction should
preserve while it strips the noise mixed around it. This helper speaks whatever
lines you give it (via Windows SAPI TTS) and writes a WAV the mixer can loop.

USAGE
    python tools/make_voice_wav.py --text-file voice_lines.txt --out fixtures/voice.wav
    python tools/make_voice_wav.py --text "The quick brown fox." --out fixtures/voice.wav
    python tools/make_voice_wav.py --list-voices

Then point the bench at it:
    /set?noise_voice=1&noise_voice_wav=<abs path to the wav>

NOTES
- Windows-only (uses System.Speech via PowerShell). On other platforms, supply
  your own WAV and skip this tool.
- COPYRIGHT: whatever text you feed in stays in YOUR local text file and the
  generated WAV stays on YOUR machine. Do NOT commit copyrighted passages or
  their WAVs to this public repo. `voice_lines.txt` and `fixtures/*.wav` are
  gitignored for exactly this reason. Public-domain text or your own words are
  always fine to commit.
- Output is 24 kHz mono 16-bit PCM by default — the sim's native audio rate, so
  WavPlayer needs no resample.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile


def list_voices() -> int:
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer)."
        "GetInstalledVoices() | ForEach-Object "
        "{ $_.VoiceInfo.Name + '  [' + $_.VoiceInfo.Culture + ']' }"
    )
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode:
        sys.stderr.write(r.stderr)
    return r.returncode


def synth(text: str, out_path: str, voice: str | None, rate: int) -> int:
    """Speak `text` to a 24 kHz mono 16-bit PCM WAV at out_path via SAPI.

    Passes the text through a temp file (not the command line) so quotes,
    newlines and apostrophes in the passage can't break PowerShell parsing.
    """
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    # Write the text to a temp UTF-8 file; PowerShell reads it back. This avoids
    # any quoting/escaping hazard with the passage content itself.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(text)
        text_file = tf.name

    voice_line = (
        f"$s.SelectVoice('{voice}');" if voice else ""
    )
    # SetOutputToWaveFile writes PCM; we request an explicit 24 kHz mono 16-bit
    # format so the sim never has to resample. SpeechAudioFormatInfo signature:
    # (samplesPerSecond, AudioBitsPerSample, AudioChannel).
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"{voice_line}"
        f"$s.Rate = {rate};"
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "24000, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{out_path}', $fmt);"
        f"$txt = [System.IO.File]::ReadAllText('{text_file}');"
        "$s.Speak($txt);"
        "$s.Dispose();"
    )
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True)
    finally:
        try:
            os.unlink(text_file)
        except OSError:
            pass

    if r.returncode or not os.path.exists(out_path):
        sys.stderr.write(r.stderr or "SAPI produced no file\n")
        return r.returncode or 1
    size = os.path.getsize(out_path)
    print(f"wrote {out_path}  ({size // 1024} KB, 24 kHz mono 16-bit)")
    print("point the bench at it:")
    print(f"  /set?noise_voice=1&noise_voice_wav={out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="text -> spoken WAV for the noise bench")
    ap.add_argument("--text", help="text to speak (use --text-file for long passages)")
    ap.add_argument("--text-file", help="UTF-8 file whose contents are spoken")
    ap.add_argument("--out", default="fixtures/voice.wav", help="output WAV path")
    ap.add_argument("--voice", help="SAPI voice name (see --list-voices)")
    ap.add_argument("--rate", type=int, default=-1,
                    help="SAPI rate -10..10 (default -1, a touch slow for clarity)")
    ap.add_argument("--list-voices", action="store_true")
    args = ap.parse_args()

    if args.list_voices:
        return list_voices()

    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read().strip()
    elif args.text:
        text = args.text
    else:
        ap.error("supply --text or --text-file (or --list-voices)")

    if not text:
        ap.error("no text to speak")
    return synth(text, args.out, args.voice, args.rate)


if __name__ == "__main__":
    raise SystemExit(main())
