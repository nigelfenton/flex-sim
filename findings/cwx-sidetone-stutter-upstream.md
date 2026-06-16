# DRAFT upstream issue — CWX local sidetone stutters under spectrum load on Windows

> Draft for review (2026-06-16). Verify against a fresh build before posting, and decide
> whether to reference `flex-sim` publicly (the repro works with a real radio too — see below).

---

**Title:** CWX local sidetone drops individual elements under panadapter/VITA-49 load on Windows (v26.6.3)

**Type:** bug · audio · CW · Windows

## Summary
On Windows (AetherSDR **v26.6.3**, current release), sending a CWX message produces an
intermittent **stutter** — individual Morse elements are dropped or stretched roughly every
few character groups, with audible clicks. Overall message timing is correct (so the #3202
drift-correction is working), but *per-element* edges slip under load.

This is the residual Windows case after [#2980](https://github.com/aethersdr/AetherSDR/issues/2980)
(macOS stutter, fixed by [#3202](https://github.com/aethersdr/AetherSDR/pull/3202)) and
[#2694](https://github.com/aethersdr/AetherSDR/issues/2694) (Windows sidetone latency/distortion).

## Environment
- AetherSDR v26.6.3 (latest release, 2026-06-14), Windows 11
- CWX send (typed buffer / contest macro), 20 wpm, PC-audio sidetone
- An active panadapter + waterfall running (this matters — see root cause)

## Symptom
**Expected:** continuous local sidetone matching the sent text. **Actual:** intermittent dropped/clipped elements under spectrum load.

- CW sidetone plays the message but **occasionally drops/clips an element** (e.g. a dit or the
  gap inside a character), heard as a break or click — roughly every 4–5 character groups on a
  long message.
- **Load-dependent:** more severe with a busy waterfall / higher VITA-49 packet rate.
- Cumulative timing stays correct — the message doesn't slow down or desync overall.

## Root cause (from source)
`src/core/CwxLocalKeyer.cpp` gates the sidetone by toggling key state on a **`QTimer` running on
the GUI event loop**, emitting `keyStateChanged(bool)` at each element edge:

```cpp
if (keyDownNext != m_currentlyDown) {
    m_currentlyDown = keyDownNext;
    emit keyStateChanged(keyDownNext);        // tone on/off, delivered via the event loop
}
// Drift-correct against an absolute clock: the next edge should land
// at m_nextEdgeMs from the start of the run, not durationMs from now.
// Without this, GUI-thread event-loop coalescing on macOS (panadapter
// paint, VITA-49 burst handling) pushes each successive edge later
// and the slip accumulates — audible as stuttering ... (#2980).
m_nextEdgeMs += durationMs;
const qint64 wait = qMax<qint64>(1, m_nextEdgeMs - elapsedMs());
armTimer(static_cast<int>(wait));
```

The drift-correction (#3202) fixes the **cumulative** slip — each edge is re-aimed at an absolute
time, so the message stays in sync. But it does **not** fix **per-edge jitter**: when the event
loop is busy (panadapter paint + VITA-49 burst handling, both named in the comment), a given
`keyStateChanged` is delivered late, so *that* element is gapped/stretched before the next edge
catches up. At 20 wpm a dit is 60 ms; Windows timer granularity and event-loop coalescing under
spectrum load are easily enough to chop a 60 ms element. The #3202 comment is explicitly about
**macOS** coalescing; Windows hits the same per-edge path and it's still audible here.

In short: **real-time CW audio is gated by GUI-event-loop timer delivery**, which jitters under
load. Drift-correction addresses average rate, not instantaneous edge accuracy.

## Reproduction
1. Connect a client with a panadapter + waterfall active (load on the GUI/VITA path).
2. Send a long CWX message at 20 wpm (e.g. a contest macro, or `CQ CQ CQ TST` repeated).
3. Listen to the PC-audio sidetone → intermittent dropped/clipped elements; worse with a busier
   waterfall.

Reproduced **deterministically and hardware-free** with a synthetic VITA-49 spectrum source
driving AE's input while AE's own CWX keyer sends — removing the radio from the loop and
confirming the stutter is purely client-side timing, correlated with VITA-49/GUI load.

## Suggested fix
- **Robust:** render the sidetone envelope **sample-accurately in the audio path** — derive key
  on/off from the element schedule against the audio sample clock (in/near the audio callback),
  instead of toggling tone state from event-loop timer events. This decouples the audio from GUI
  scheduling entirely; jitter can no longer gap an element.
- **Partial mitigations** (reduce, won't fully cure): run the keyer on a dedicated high-priority
  thread off the GUI loop; add audio look-ahead / pre-buffering so a late edge doesn't underrun.

## Related
- [#2980](https://github.com/aethersdr/AetherSDR/issues/2980) / [#3202](https://github.com/aethersdr/AetherSDR/pull/3202) — macOS stutter + drift-correction (cumulative slip)
- [#2694](https://github.com/aethersdr/AetherSDR/issues/2694) — Windows sidetone latency/distortion
- [#2754](https://github.com/aethersdr/AetherSDR/pull/2754) — CharGap between live-mode entries in `CwxLocalKeyer`
- [#2181](https://github.com/aethersdr/AetherSDR/pull/2181) — keep audio gate open during CWX send
- [#3271](https://github.com/aethersdr/AetherSDR/pull/3271) — `CwxLocalKeyer` drift-correction regression test (existing harness to validate a fix)
