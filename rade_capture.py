#!/usr/bin/env python3
"""
rade_capture.py — record AetherSDR TCI RX audio to a WAV file.

Connect to AetherSDR's TCI WebSocket, subscribe to the RX audio stream
on a given receiver, decode binary TCI audio frames, and write the result
to a WAV file.  The WAV can then be fed back into flex-sim (audio_src=wav)
so AE decodes it without a real radio — useful for RADE loopback testing.

Loopback procedure:
  1. RF cable: Flex-6300 TX → inline attenuator → Flex-6300 RX (same receiver)
  2. AetherSDR: set mode to RADE, speak a few words at low TX power
  3. Run this script to capture the decoded RX audio
  4. Point flex-sim at the resulting WAV — AE will try to decode it as RADE

Usage:
    python rade_capture.py [--host HOST] [--port PORT] [--rx N]
                           [--duration SECS] [--out FILE]

Defaults: host=10.0.0.107 port=40001 rx=0 duration=10 out=rade_capture.wav

Dependency: pip install websocket-client   (or apt install python3-websocket)
"""

import argparse, struct, sys, threading, time, wave

try:
    import websocket
except ImportError:
    sys.exit("websocket-client not found — apt install python3-websocket")

HEADER_SIZE    = 64
TYPE_RX_AUDIO  = 1
FMT_INT16      = 0
FMT_FLOAT32    = 3


def parse_header(data: bytes):
    if len(data) < HEADER_SIZE:
        return None
    r, sr, fmt, codec, crc, length, typ, ch = struct.unpack_from('<8I', data, 0)
    return dict(receiver=r, sample_rate=sr, format=fmt,
                length=length, type=typ, channels=ch)


def decode_samples(data: bytes, hdr: dict) -> list:
    payload = data[HEADER_SIZE:]
    n = hdr['length'] * hdr['channels']
    if hdr['format'] == FMT_FLOAT32:
        if len(payload) < n * 4:
            return []
        return [max(-32768, min(32767, int(v * 32767)))
                for v in struct.unpack_from(f'<{n}f', payload)]
    if hdr['format'] == FMT_INT16:
        if len(payload) < n * 2:
            return []
        return list(struct.unpack_from(f'<{n}h', payload))
    return []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--host',     default='10.0.0.107', help='AetherSDR host')
    ap.add_argument('--port',     type=int, default=40001, help='TCI port')
    ap.add_argument('--rx',       type=int, default=0, dest='receiver',
                    help='TCI receiver / TRX number (default 0)')
    ap.add_argument('--duration', type=float, default=10.0,
                    help='seconds to record (default 10)')
    ap.add_argument('--out',      default='rade_capture.wav',
                    help='output WAV path')
    args = ap.parse_args()

    url = f"ws://{args.host}:{args.port}"
    print(f"Connecting → {url}  (receiver {args.receiver}, {args.duration}s)")

    all_samples  = []
    sample_rate  = None
    n_channels   = None
    done         = threading.Event()
    t_start      = [None]

    def on_open(ws):
        t_start[0] = time.monotonic()
        ws.send(f"audio_start:{args.receiver};")
        print(f"→ audio_start:{args.receiver};")

    def on_message(ws, msg):
        nonlocal sample_rate, n_channels
        if not isinstance(msg, bytes):
            return   # text command echo — ignore
        hdr = parse_header(msg)
        if (hdr is None
                or hdr['type'] != TYPE_RX_AUDIO
                or hdr['receiver'] != args.receiver):
            return
        if sample_rate is None:
            sample_rate = hdr['sample_rate']
            n_channels  = hdr['channels']
            fmt_name    = {FMT_INT16: 'int16', FMT_FLOAT32: 'float32'}.get(hdr['format'], '?')
            print(f"  stream: {sample_rate} Hz  {n_channels}ch  {fmt_name}")
        all_samples.extend(decode_samples(msg, hdr))
        elapsed = time.monotonic() - t_start[0]
        print(f"\r  {elapsed:.1f} / {args.duration}s  "
              f"({len(all_samples)} samples)", end='', flush=True)
        if elapsed >= args.duration:
            ws.send(f"audio_stop:{args.receiver};")
            done.set()
            ws.close()

    def on_error(ws, err):
        print(f"\nWS error: {err}")

    def on_close(ws, code, reason):
        done.set()

    ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                on_error=on_error, on_close=on_close)
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()

    try:
        done.wait(timeout=args.duration + 10)
    except KeyboardInterrupt:
        ws.close(); done.wait(timeout=2)

    print()
    if not all_samples:
        print("No audio captured — check AE is connected and audio_start was accepted.")
        sys.exit(1)

    sr = sample_rate or 48000
    ch = n_channels  or 1
    out = args.out
    with wave.open(out, 'wb') as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)    # always int16
        wf.setframerate(sr)
        wf.writeframes(struct.pack(f'<{len(all_samples)}h', *all_samples))

    dur = len(all_samples) / (sr * ch)
    print(f"Saved: {out}  ({dur:.1f}s, {sr}Hz, {ch}ch, {len(all_samples)} samples)")
    print()
    print("Next step: copy to hub and point flex-sim at it:")
    print(f"  scp {out} nigel@10.0.0.51:~/shack-experiments/flex-sim/audio/")
    print(f"  # then in flex-sim control panel: Audio source → WAV file")
    print(f"  #   path: /home/nigel/shack-experiments/flex-sim/audio/{out}")


if __name__ == '__main__':
    main()
