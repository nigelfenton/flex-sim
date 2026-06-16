# DRAFT PR — fix CWX sidetone per-edge stutter under load

> Draft against source read via the GitHub API (not compiled here). Apply,
> compile and test on the main machine before opening. Pairs with the issue
> draft (`cwx-sidetone-stutter-upstream.md`), filed upstream as
> **aethersdr/AetherSDR#3623** (2026-06-16).

## Root cause (recap)
The CW key *gate* is timed on the **GUI thread**, even though the sidetone
*samples* are produced on the audio thread:

- `CwxLocalKeyer` (parented to `MainWindow`, so GUI thread) schedules each
  element edge with a `QTimer` and does `emit keyStateChanged(down)`
  (`src/core/CwxLocalKeyer.cpp`).
- `MainWindow_Session.cpp:658` routes that through a **GUI-thread lambda** →
  `CwSidetoneGenerator::setKeyDown(down)`.
- `CwSidetoneGenerator::process()` (audio thread) reads `m_keyDown` once per
  block and runs the envelope (`src/core/CwSidetoneGenerator.cpp`).

So the gate depends on GUI-thread timer delivery **and** a GUI-thread slot. Under
panadapter paint + VITA-49 burst load the event loop coalesces both, the gate
moves late, and an element is gapped. #3202's drift-correction fixes cumulative
slip but not this per-edge jitter (and only reasoned about macOS).

---

## Patch 1 — move the keyer off the GUI thread  *(recommended first PR: small, low-risk)*
Run `CwxLocalKeyer` on a dedicated high-priority worker thread and set the gate
**directly** (no GUI-thread hop). Removes the named contention source; remaining
jitter is OS-timer + one 2 ms audio block — inaudible at 60 ms dits.

`MainWindow.h` — add members:
```cpp
class CwxLocalKeyer* m_cwxLocalKeyer{nullptr};
QThread*             m_cwxKeyerThread{nullptr};   // NEW: keep the keyer's QTimer off the GUI loop
```

`MainWindow_Session.cpp` — replace the keyer setup (was lines ~653-662):
```cpp
// Keyer on its own high-priority thread so its QTimer isn't coalesced by
// GUI paint / VITA-49 handling (the cause of CWX sidetone stutter under load).
m_cwxKeyerThread = new QThread(this);
m_cwxKeyerThread->setObjectName("CwxLocalKeyer");
m_cwxLocalKeyer  = new CwxLocalKeyer();          // NO parent — required for moveToThread
m_cwxLocalKeyer->moveToThread(m_cwxKeyerThread);
connect(m_cwxKeyerThread, &QThread::finished, m_cwxLocalKeyer, &QObject::deleteLater);
m_cwxKeyerThread->start(QThread::TimeCriticalPriority);

// start()/stop() now auto-queue onto the keyer thread (cross-thread connection),
// so QTimer is armed there, not on the GUI loop.
connect(&m_radioModel.cwxModel(), &CwxModel::transmissionRequested,
        m_cwxLocalKeyer, &CwxLocalKeyer::start);
connect(&m_radioModel.cwxModel(), &CwxModel::transmissionCancelled,
        m_cwxLocalKeyer, &CwxLocalKeyer::stop);

// Set the gate DIRECTLY on the emitting (keyer) thread — setKeyDown is atomic.
// Qt::DirectConnection avoids bouncing the edge back through the GUI event loop.
connect(m_cwxLocalKeyer, &CwxLocalKeyer::keyStateChanged, this,
        [this](bool down) {
            if (m_audio && m_audio->cwSidetone())
                m_audio->cwSidetone()->setKeyDown(down);
        }, Qt::DirectConnection);
```

Teardown (MainWindow dtor / disconnect path):
```cpp
if (m_cwxKeyerThread) { m_cwxKeyerThread->quit(); m_cwxKeyerThread->wait(); }
```

Notes/risks: the keyer is a clean worker (no UI calls), so it's safe off-thread.
`setKeyDown` is already `std::atomic`. The DirectConnection lambda touches
`m_audio`/`cwSidetone()` from the keyer thread — both are session-stable and
null-checked; if teardown races are a concern, guard `m_audio` or use a captured
generator pointer cleared under the audio-stop path.

---

## Patch 2 — sample-accurate key schedule  *(robust follow-up)*
Make the gate independent of *any* thread timing: feed the element schedule to
the generator and advance it by **samples consumed in `process()`**.

`CwSidetoneGenerator` (header) — add an SPSC key-event ring (GUI produces, audio
consumes) and a per-sample cursor:
```cpp
struct KeyEvent { bool down; uint32_t frames; };
bool pushKeyEvent(bool down, uint32_t frames) noexcept;   // producer (keyer thread)
void clearKeySchedule() noexcept;                         // producer: stop()
// ...
static constexpr int kKeyCap = 1024;             // power of two
KeyEvent              m_keyRing[kKeyCap];
std::atomic<uint32_t> m_keyHead{0};              // consumer (audio)
std::atomic<uint32_t> m_keyTail{0};              // producer (gui/keyer)
bool                  m_useSchedule{false};
bool                  m_schedDown{false};
uint32_t              m_schedRemain{0};
```

In `process()`, evaluate the gate **per sample** from the schedule instead of the
block-level `m_keyDown` (move the existing edge-transition switch into the
per-sample loop, keyed off `schedDown`):
```cpp
// inside the per-frame loop, before computing env:
if (m_useSchedule) {
    while (m_schedRemain == 0) {
        const uint32_t h = m_keyHead.load(std::memory_order_relaxed);
        if (h == m_keyTail.load(std::memory_order_acquire)) break;  // empty: hold
        m_schedDown   = m_keyRing[h & (kKeyCap-1)].down;
        m_schedRemain = m_keyRing[h & (kKeyCap-1)].frames;
        m_keyHead.store(h + 1, std::memory_order_release);
    }
    keyDownNow = m_schedDown;
    if (m_schedRemain) --m_schedRemain;
}
// run the Idle/RampUp/Sustain/RampDown transitions on keyDownNow (per sample)
```

`CwxLocalKeyer` — instead of (or alongside) the `QTimer`, encode each element to
`pushKeyEvent(down, durationMs * sampleRate / 1000)`. The drift-correction and
GUI timer are then no longer load-bearing for the sidetone.

Testing: update the `CwxLocalKeyer` drift test (#3271) to assert the schedule's
sample boundaries; add a process()-level test that injects a stall and verifies
element boundaries stay sample-exact.

Risk: larger (touches the audio-thread hot path + adds a lock-free ring), but the
correct end state — gate timing is fully decoupled from event-loop scheduling.

---

## Recommendation
Ship **Patch 1** first — it's a handful of lines, directly removes the
GUI-coalescing the code comment itself blames, and almost certainly cures the
audible stutter. Keep **Patch 2** as the durable follow-up if any residual
block-granularity jitter remains. Both validate against the existing #3271 harness
and the deterministic flex-sim repro.

---

## Patch 1 — branch + commit (copy-paste on the main machine)
```
git checkout -b fix/cwx-sidetone-keyer-offthread
# apply Patch 1, build, test with the flex-sim repro
git commit -am "$(cat <<'MSG'
fix(cwx): run local sidetone keyer off the GUI thread to stop per-edge stutter

CwxLocalKeyer was parented to MainWindow, so its QTimer ran on the GUI event
loop and keyStateChanged was delivered through a GUI-thread lambda. Under
panadapter paint / VITA-49 burst load the loop coalesces both, the key gate
lands late, and individual CW elements drop or clip -- audible stutter on
Windows that #3202's drift-correction (cumulative slip, macOS) does not cover.

Move the keyer to a dedicated TimeCriticalPriority QThread and set the sidetone
gate via Qt::DirectConnection so the edge is not bounced back through the GUI
loop. setKeyDown is atomic; CwxLocalKeyer is a UI-free worker.

Fixes #3623.
MSG
)"
```
PR title: `fix(cwx): run local sidetone keyer off the GUI thread to stop per-edge stutter`
