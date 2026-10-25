# Scoping: an ANAN / openHPSDR Protocol 2 personality for flex-sim

> **Status: historical design record (written 2026-08-09, revised 2026-09-16).**
> The v1 simulator this note scoped has since been **built** — see the
> [README](../README.md#anan--openhpsdr-protocol-2-simulator). Where the original
> text turned out to be wrong it is **corrected in place and marked**, rather than
> silently rewritten, so the reasoning stays readable.
>
> Upstream, AetherSDR now has an **ANAN-G2 Protocol 2 receive backend**:
> [aethersdr/AetherSDR#5143](https://github.com/aethersdr/AetherSDR/pull/5143), merged
> 2026-08-31, designed in the approved
> [RFC #4970](https://github.com/aethersdr/AetherSDR/issues/4970).

Written when an ANAN owner who had moved from a Flex asked whether AetherSDR could
drive the radio. This is what that would take, and where a simulator fits.

---

## 0. ANSWERED: it is an ANAN-G2 → Protocol 2, no cheap path

**The radio in question is an ANAN-G2.** That is the Saturn FPGA board, and TAPR
publishes **no Protocol 1 firmware for it** — P2 is the only wire it speaks. So §1's
cheap path (reuse the Metis wire AE already has) **does not apply** to it, and this
is the sibling-backend project in §2.

§1 is kept because it is still true of the 7000/8000DLE family and would apply to any
*other* ANAN owner who asks.

The G2 detail that matters for a sim: it is a Saturn FPGA **plus an onboard Raspberry
Pi**, so the radio is a small computer. Worth knowing when reading a capture — some
traffic may be the Pi rather than the FPGA.

### Original scoping question (resolved)

| Model | Board | Protocol 1? | Protocol 2? |
|---|---|---|---|
| ANAN-7000DLE / 8000DLE (Orion MkII) | Cyclone IV / V | **YES** — P1 firmware published by TAPR | yes, with the 8000DLE firmware |
| ANAN-7000DLE MkII / 8000DLE MkII | Cyclone V | **YES** | yes |
| **ANAN-G2 / G2 MkII (Saturn)** | Saturn FPGA + RPi | **NO** | **P2 only** |

TAPR publishes [Protocol 1 firmware for the ANAN-7000DLE/8000DLE](https://github.com/TAPR/OpenHPSDR-Firmware/tree/master/Protocol%201/ANAN-7000DLE_ANAN-8000DLE-Andromeda).
Those radios run the **same Metis/P1 wire AetherSDR already speaks** — the wire
`Hl2Backend` + `MetisClient` were built for.

So there are two different projects hiding behind "support the ANAN":

- **A 7000/8000** → possibly a *firmware-and-discovery* problem, not a protocol
  problem. Load P1 firmware, and the existing backend may largely work. The gap is
  discovery/board-ID gating and per-model capability, not a new wire.
- **A G2/Saturn** → P2 is mandatory and this is a months-long backend.

The cheap path and the expensive path differ by an order of magnitude, and only the
owner knows which applies — so the model has to be established before scoping.

## 1. If it is P1 (the cheap path)

Almost all the work is already merged upstream. What would need checking:

- **Board ID.** `MetisProtocol.h`'s `isHermesLite2()` gates on `boardId == 0x06`.
  An Orion MkII reports something else, so discovery would reject it even though the
  wire is identical. Board IDs are **not** documented in the firmware repo README —
  they need reading out of the P1 spec or off a real radio.
- **Capabilities.** Receiver count, sample rates, band edges and TX power scaling all
  differ from an HL2. `Hl2Bands.h` / `Hl2DbReference.h` are HL2-shaped.
- **Naming.** A backend called `Hl2Backend` driving an ANAN is a maintainability
  smell, but that is a rename conversation, not an engineering one.

**A P1 ANAN personality in flex-sim is nearly free** — the existing HPSDR wire with a
different discovery reply. *(Status 2026-09-16: not built. The P2 work below took
priority because the radio that prompted this is a G2.)*

## 2. If it is P2 (the expensive path)

P2 is not "P1 plus features". It is a different architecture.

| | Protocol 1 (Metis) | Protocol 2 |
|---|---|---|
| Ports | one (`:1024`), endpoint-multiplexed | **~90**, function-per-port |
| | | 1024 CR/MEM · 1025 DDC cmd · 1026 DUC cmd · 1027 high-priority · 1028 DDC audio · 1029–1036 DUC IQ |
| | | inbound: 1025 status · 1026 mic · 1027–1034 wideband · **1035–1114 DDC IQ** |
| Framing | one 1032-byte shape | **eleven distinct datagram formats** |
| Transport | 100 Mb fine | **Gigabit required** |

`Hl2Backend` is bound to `MetisClient` throughout — constructor ordering, EP2 pacing,
the gateware watchdog, telemetry. P2 wants a **sibling backend** reusing the
`IRadioBackend` seam and much of `Hl2RxDsp`, not a mode flag. Comparable in size to
the entire HL2 backend effort.

## 3. Why the sim is the right first deliverable

1. **Nobody can review P2 work without it.** AetherSDR #4815 is the cautionary tale:
   a test gated on hardware nobody had passed silently on every machine for months.
   A sim makes P2 work reviewable by people without an expensive radio on the bench.
2. **flex-sim already proves the pattern** — emulating a Flex well enough that AE
   cannot tell, plus the accessory personas. A P2 personality is the same approach
   against a different wire, and it needs no upstream approval to start.
3. **The protocol is dissectable.** A [Wireshark dissector](https://github.com/matthew-wolf-n4mtt/openhpsdr-e)
   covers all eleven datagram formats.
   ⚠ **Its licence is unstated — reading reference only, never copy.** Same clean-room
   posture as the HL2 work: protocol *facts* from documentation, expressed in original
   code.

## 4. Proposed sim scope

**v0 — P1 ANAN personality**
- Answer Metis discovery with an Orion-MkII-shaped reply (board ID TBC)
- Everything else is the existing HPSDR path
- *Status: not built.*

**v1 — P2 minimum viable receiver** — *Status: **built** as `anan_sim.py`.*
- Discovery on `:1024`, General/high-priority command intake
- **One** DDC IQ stream outbound
- Enough to make a radio appear in a client and paint a waterfall

**v0.2 / v0.3 — multi-DDC, then real-G2 behaviour** — *Status: **built** (2026-10-24 / 2026-10-25).*
Every enabled DDC streams; pre-rendered signal for high packet rates. v0.3 replaced the
v0.2 guesses with what a real G2 was measured to do (diversity, DDC count, timestamps,
sessions, the mic stream and the status packet). Built once a consumer appeared:
AetherSDR's G2 backend is moving to a second receiver (RFC #4970 phase 2) and diversity
(RFC #6725). See §7.

**v3+ — as needed**
- DUC/transmit, wideband, mic/line, PureSignal feedback, a web control page
- Only worth building once something consumes v0.2 and a real-G2 capture says what the
  radio does

## 4b. Port map and packet sizes (extracted 2026-08-09, corrected since)

From **laurencebarker/Saturn** (GPL-3.0), `project_documentation/` — written by the
G2's own firmware author, so it describes what *this* radio does.

### Ports (PC → SDR unless noted)

| Purpose | Port | Thread idx |
|---|---|---|
| Discovery **and** General Packet to SDR | **1024** | 0 |
| DDC Specific | 1025 | 1 |
| DUC Specific | 1026 | 2 |
| High Priority **from** PC | 1027 | 3 |
| DDC Audio (speaker) | 1028 | 4 |
| DUC0 I/Q | 1029 | 5 |
| **High Priority → PC** | 1025 | 6 |
| **Mic samples → PC** | 1026 | 7 |
| **DDC0–9 I/Q → PC** | **1035–1044** | 8–17 |
| Wideband ADC0 / ADC1 → PC | 1027 / 1028 | 18 / 19 |
| Memory-mapped either way | not supported | — |

⭐ **The discovery reply goes to the *source port* of the discovery request**, not a
fixed port.

> **Corrected 2026-08-21** (found by NereusSDR, [#5](https://github.com/nigelfenton/flex-sim/issues/5)):
> data streams go to the client's **session** port, not necessarily the port it
> probed discovery from. In a real-G2 capture the PC probes discovery from one
> ephemeral port and runs the session from another; the radio streams to the
> session port exclusively. Clients that use one socket for both cannot tell the
> difference, which is how the original reading survived.

> **Corrected 2026-08-21** (also #5): stream as the DDC(s) the client **enables** —
> the DDC-Specific packet's byte 7 is an enable bitmask — from radio port 1035+n.
> Thetis-family clients run RX1 on **DDC2** (DDC0/1 are reserved for PureSignal
> feedback), so their I/Q comes from port 1037. The per-DDC sample-rate record
> (byte 18+6n) must be read for the *enabled* DDC.

⭐ **The General Packet can RE-ASSIGN every port above.** Zero means "use the
default". Thetis sends the defaults, but a conforming radio has to honour a
non-default set.

⚠ **Byte order is network (big-endian) throughout.** Saturn does not implement the
protocol's byte-order-change mechanism.

### Startup sequence

1. P1 discovery **and** P2 discovery both arrive at **1024** — a P2 radio has to
   tolerate a P1 probe on the same port.
2. P2 discovery → **P2 reply** (to the requester's source port).
3. **General Packet** → assigns ports; re-bind if non-default.
4. DDC Specific → DUC Specific → **High Priority with run bit = 1**.
5. Only then do outgoing data threads start. **There is no separate run/stop
   packet** — the run bit is embedded in the High Priority packet.
6. High Priority → PC is sent **every 50 ms in RX, every 1 ms in TX**.

### Packet sizes

> ⚠ **CORRECTED 2026-08-17 against real ANAN-G2 captures** ([#3](https://github.com/nigelfenton/flex-sim/pull/3)).
> The original note below read the Saturn *transfer sizes* spreadsheet's
> "1440 bytes / 240 samples" as the **DDC (RX)** layout. **It is the DUC (TX)
> layout.** On a real G2, RX frames carry their own header declaring 238 samples:
>
> | Stream | Header | IQ payload | Samples/frame | Total |
> |---|---|---|---|---|
> | **DDC (RX)**, radio → PC | **16 B**: seq(4) timestamp(8) bits/sample(2) samples/frame(2) | **1428 B** | **238** | 1444 B |
> | **DUC (TX)**, PC → radio | **4 B**: seq(4) only | 1440 B | 240 | 1444 B |
>
> Both payloads are 1444 B and both divide by 6, so both look self-consistent —
> but applying 240 samples to RX **overruns every frame by 12 bytes**. Samples are
> 24-bit I then 24-bit Q, big-endian. `p2verify.py` is the tool that established
> this, and `anan_sim.py` carries the corrected values.

> ⚠ **CORRECTED 2026-09-16 — I/Q handedness.** The wire I/Q is the **conjugate** of
> the analytic convention: a signal above the DDC centre arrives at a **negative**
> frequency. Until v0.3.1 the sim emitted `I = cos, Q = sin` for its "+1 kHz" tone,
> which a correct client draws 1 kHz *below* the dial and demodulates only in LSB.
> AetherSDR's ANAN backend exposed it: its polarity was measured on a real G2
> against WWV and cross-checked with an RSP1B + SDR++ sharing no code with it
> (AetherSDR `docs/HERMES.md` §16). The packet-geometry checks above could never
> catch this — every frame was the right size.

High Priority centre frequencies (4 bytes per DDC at byte 9 + 4n) are a **phase word**
by default, not Hz: Hz = word × 122.88 MHz / 2³². The General Packet's byte 37 bit 3
selects it and discovery reply byte 21 advertises it; 10.000 MHz is 349 525 333.

The sample rate changes the **cadence**, not the frame size. With 238 samples per RX
frame:

| Rate | RX frame period |
|---|---|
| 48 k | 4.96 ms |
| 96 k | 2.48 ms |
| 192 k | 1.24 ms |
| 384 k | 0.62 ms |
| 768 k | 0.31 ms |
| 1536 k | 0.155 ms |

The sample rate is **commanded by the client**, not configured on the radio: the DDC
Specific packet (port 1025) carries 6-byte per-DDC records from byte 17 — ADC at
17+6n, rate at 18+6n as a big-endian uint16 in kHz.

Interleaved DDC halves the samples per packet and so halves the duration. Speaker
audio 256 B / 64 samples @ 48 k; mic 128 B / 64 samples @ 48 k.

## 5. Open questions

1. ~~**Which ANAN?**~~ **ANSWERED:** G2 (Saturn) → Protocol 2 only.
2. **Board IDs for the Orion MkII family** — needed for a v0 P1 discovery reply, and
   not in the firmware repo README. *Still open; v0 is not built.*
3. ~~**A capture from a real ANAN.**~~ **ANSWERED 2026-08-17:** captures from a real
   ANAN-G2 were obtained and turned v1 from "built to a spec" into "built to observed
   bytes" — they are what corrected the RX geometry above.
4. ~~**Does AE want a P2 backend at all?**~~ **ANSWERED 2026-08-31: yes.** AetherSDR
   merged an ANAN-G2 P2 receive backend
   ([#5143](https://github.com/aethersdr/AetherSDR/pull/5143)) under the approved
   [RFC #4970](https://github.com/aethersdr/AetherSDR/issues/4970).

## 6. Outcome

With a G2 confirmed there was **no shortcut**: driving that radio means a Protocol 2
backend, comparable in size to the whole HL2 effort. The plan was therefore:

1. **Get a real capture** — without an ANAN on the bench, a capture is the only ground
   truth for what a sim must emit. **Done**, and it corrected a documented figure that
   everything else had agreed on.
2. **Build the v1 P2 sim** — discovery plus DDC I/Q. **Done** (`anan_sim.py`), then
   exercised by an independent client (NereusSDR) that found four defects the
   documentation-derived checks could not.
3. **Judge a backend on its merits, with a sim to develop against.** The backend was
   built upstream under its own RFC. The sim was first run against it on
   **2026-09-16**: it interoperated end to end, and the backend — already validated on
   real hardware — showed the sim's I/Q handedness was mirrored (fixed in v0.3.1).

## 7. What a real G2 has told us — and what is still open

On 2026-10-25 **Sotiris KO9B** (the author of AetherSDR's G2 backend) measured the seven
questions this section used to pose, on his own radio, with a receive-only probe. His three
run logs are the source of every *measured* statement in the README. Results:

| # | Question | Answer from a real G2 | v0.2 guess |
|---|---|---|---|
| 1 | Diversity: how is it selected, and what is the cadence? | **DDC Specific byte 1363 == 0x02** (equality, not a bit; **not** High Priority). Byte 7 enables DDC0 only and p2app enables DDC1 itself; the pair's rate is DDC1's record (bytes 24–25). Header still 238 samples = 119 pairs; **packet rate doubles** (201.5 → 407.3 pkt/s at 48 k, 811.8 → 1619.3 at 192 k). | cadence ✔ (doubles) — trigger ✘ (High Priority bit) |
| 1b | Pair order | DDC0 first in the first session after power-up; **swapped in 4 of 10 trials** after DDC0 had run alone (p2app sets up its buffers and FIFO once, so a leftover odd sample count shifts every pair). Never moved in 15 trials when DDC0 only ever carries pairs (RX1 on DDC2). A client can detect the order itself. | DDC0 first ✔ (not guaranteed) |
| 2 | Does DDC1 also stream while diversity is on? | **No** — nothing from :1036. | ✔ |
| 3 | Two DDCs at once | Own port, own sequence counter, **starting at 0 at run=1**; timestamp bytes 4–11 **always zero**; same ADC and frequency ⇒ packet *k* of each covers the same instants (lag 0, peak 1.00) a constant NCO phase apart; ~0.1 ms between them; mixed rates run at their own rates. | ports and counters ✔ — timestamps ✘ (wall clock) |
| 4 | Discovery reply | byte 13 = **27**, byte 20 = **4**, board 10, protocol **4.3**, byte 23 (p2app build) = **46**. | 27 ✔, 4 ✔, protocol 3.9 ✘ |
| 5 | A DDC above the advertised count | **Streams normally** (DDC4 from :1039); p2app reads enables for 10 DDCs and the 4 is a literal in `p2app.c`. | ✘ (ignored it) |
| 6 | Status packet | About every **232 ms**; byte map in the README (supply rail is bytes **57–58**, not 49–50). | 60 zero bytes every 50 ms ✘ |
| 7 | TX with real audio | **Not yet** — AetherSDR does not transmit on the G2 yet; he will capture it when it does. | — |

Two things he volunteered: every session starts with a **stale burst** (up to ~76 packets,
observed 76, 49 and 12, in the previous layout, numbered from 0, some from ports no longer
enabled), and the **mic stream** runs at ~750 packets/s from :1026 whenever the radio runs.

### Still open

- **TX with real audio** (and a second sample rate) — the framing is confirmed, modulation is not.
- Is the **first session after power-up** clean of the stale burst? (Expected; unchecked.)
- A **diversity pair whose two rate records differ** — he always sets them equal; the sim
  follows DDC1's record, as p2app's source says, but nobody has run a mismatch.
- **Where DDC8–9 live** in the DDC Specific packet (p2app reads 10 enables; the sim stops at 8).
- The **DDC FIFO depth** (status bytes 31–32) varied 63–4165 in his run; the sim reports the
  median (1522) as a constant.
- A **side-by-side**: he will run `anan_sim.py` and the same probe against both the sim and his G2.

`p2verify.py` in this repository reads a pcapng directly (no tshark). Captures of a private
station's traffic stay private; only the findings need to come back.
