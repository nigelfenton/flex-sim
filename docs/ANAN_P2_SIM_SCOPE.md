# Scoping: an ANAN / openHPSDR Protocol 2 personality for flex-sim

Status: **scoping note, nothing built.** 2026-08-09, aurora13.

Written after a friend in New York moved from a Flex to an ANAN and asked whether
AetherSDR could drive it. This is what that would actually take, and where a sim
fits.

---

## 0. ANSWERED 2026-08-09: it is an ANAN-G2 → Protocol 2, no cheap path

**He has an ANAN-G2.** That is the Saturn FPGA board, and TAPR publishes **no
Protocol 1 firmware for it** — P2 is the only wire it speaks. So §1's cheap path
(reuse the Metis wire AE already has) **does not apply**, and this is the
months-long sibling-backend project in §2.

§1 is kept below because it is still true of the 7000/8000DLE family and would
apply to any *other* ANAN owner who asks — but it is not the path for this radio.

The G2 detail that matters for the sim: it is a Saturn FPGA **plus an onboard
Raspberry Pi**, so the radio is a small computer. Worth knowing when reading a
pcap — some traffic may be the Pi rather than the FPGA.

### Original scoping question (now resolved)

| Model | Board | Protocol 1? | Protocol 2? |
|---|---|---|---|
| ANAN-7000DLE / 8000DLE (Orion MkII) | Cyclone IV / V | **YES** — P1 firmware published by TAPR | yes, with the 8000DLE firmware |
| ANAN-7000DLE MkII / 8000DLE MkII | Cyclone V | **YES** | yes |
| **ANAN-G2 / G2 MkII (Saturn)** | Saturn FPGA + RPi | **NO** | **P2 only** |

TAPR publishes [Protocol 1 firmware for the ANAN-7000DLE/8000DLE](https://github.com/TAPR/OpenHPSDR-Firmware/tree/master/Protocol%201/ANAN-7000DLE_ANAN-8000DLE-Andromeda).
Those radios run the **same Metis/P1 wire AetherSDR already speaks** — the wire
`Hl2Backend` + `MetisClient` were built for and that our Radioberry work
independently validated.

So there are two completely different projects hiding behind "support the ANAN":

- **He has a 7000/8000** → possibly a *firmware-and-discovery* problem, not a
  protocol problem. Load P1 firmware, and the existing backend may largely work.
  The gap is discovery/board-ID gating and per-model capability, not a new wire.
- **He has a G2/Saturn** → P2 is mandatory and this is a months-long backend.

⚠ **Do not scope, promise, or build until this is answered.** The cheap path and
the expensive path differ by an order of magnitude and only the owner knows which
applies.

## 1. If it turns out to be P1 (the cheap path)

Almost all the work is already merged. What would need checking:

- **Board ID.** `MetisProtocol.h`'s `isHermesLite2()` gates on `boardId == 0x06`.
  An Orion MkII reports something else, so discovery would reject it even though
  the wire is identical. Board IDs are **not** documented in the firmware repo
  README — they need reading out of the P1 spec or off a real radio.
- **Capabilities.** Receiver count, sample rates, band edges, TX power scaling
  all differ from an HL2. `Hl2Bands.h` / `Hl2DbReference.h` are HL2-shaped.
- **Naming.** A backend called `Hl2Backend` driving an ANAN is a maintainability
  smell, but that is a rename conversation, not an engineering one.

**A P1 ANAN personality in flex-sim is nearly free** — it is the existing HPSDR
wire with a different discovery reply. That is the *first* thing to build either
way, because it tests the discovery/board-ID gating that both paths need.

## 2. If it is P2 (the expensive path)

P2 is not "P1 plus features". It is a different architecture.

| | Protocol 1 (Metis) | Protocol 2 |
|---|---|---|
| Ports | one (`:1024`), endpoint-multiplexed | **~90**, function-per-port |
| | | 1024 CR/MEM · 1025 DDC cmd · 1026 DUC cmd · 1027 high-priority · 1028 DDC audio · 1029–1036 DUC IQ |
| | | inbound: 1025 status · 1026 mic · 1027–1034 wideband · **1035–1114 DDC IQ** |
| Framing | one 1032-byte shape | **eleven distinct datagram formats** |
| Transport | 100 Mb fine | **Gigabit required** |

`Hl2Backend` is bound to `MetisClient` throughout — constructor ordering, EP2
pacing, the gateware watchdog, telemetry. P2 wants a **sibling backend** reusing
the `IRadioBackend` seam and much of `Hl2RxDsp`, not a mode flag. Comparable in
size to the entire HL2 backend effort.

## 3. Why the sim is the right first deliverable

1. **Nobody can review P2 work without it.** #4815 is the cautionary tale: a test
   gated on hardware nobody has passed silently on every machine for months.
   A sim makes P2 reviewable by people without a $3k radio.
2. **flex-sim already proves the pattern** — 62 functions emulating a Flex well
   enough that AE cannot tell, plus the amp personas. A P2 personality is the
   same trick against a different wire, and it is our repo, so no upstream
   approval is needed to start.
3. **The protocol is dissectable.** A [Wireshark dissector](https://github.com/matthew-wolf-n4mtt/openhpsdr-e)
   covers all eleven datagram formats.
   ⚠ **Its licence is unstated — reading reference only, never copy.** Same
   clean-room posture as the HL2 work: protocol *facts* from documentation,
   expressed in original code.

## 4. Proposed sim scope

**v0 — P1 ANAN personality (do this first, regardless)**
- Answer Metis discovery with an Orion-MkII-shaped reply (board ID TBC)
- Everything else is the existing HPSDR path
- Proves AE's discovery/board-ID gating against a non-HL2 P1 radio
- Cost: small. This is a discovery-reply variant.

**v1 — P2 minimum viable receiver**
- Discovery on `:1024`, General/high-priority command intake
- **One** DDC IQ stream outbound (`:1035`)
- Enough to make a radio appear in AE and paint a waterfall
- Cost: days, not months — bounded, and the natural first proof.

**v2+ — as needed**
- Multi-DDC, DUC/transmit, wideband, mic/line
- Only worth building once something consumes v1

## 5. Open questions

1. ~~**Which ANAN?**~~ **ANSWERED: G2 (Saturn) → Protocol 2 only.**
2. **Board IDs for the Orion MkII family** — needed for v0's discovery reply, and
   not in the firmware repo README.
3. **A pcap from a real ANAN** would turn v1 from "built to a spec" into "built
   to observed bytes". Worth asking the friend for a Wireshark capture of
   Thetis/piHPSDR connecting — that is a five-minute favour for him and the
   single most valuable input we could get.
4. Does AE want a P2 backend at all? Worth an RFC before writing backend code —
   the sim can proceed regardless, since it lives in our repo.

## 6. The honest framing (revised, model known)

The user-facing prize is real — he is tired of Thetis and would rather run AE —
but with a G2 confirmed, **there is no shortcut**. Driving that radio means a
Protocol 2 backend: ~90 function-per-port sockets, eleven datagram formats, a new
client class, and a sibling to `Hl2Backend`. That is comparable in size to the
entire HL2 effort, which ran to months and many PRs.

What I would NOT do is promise him a timeline. What I would do:

1. **Get the pcap anyway** — it is now the foundation rather than a nice-to-have,
   because without an ANAN on the bench a capture is the *only* ground truth for
   what the sim must emit.
2. **Build the v1 P2 sim** (discovery + one DDC IQ stream). Days of work, and it
   is the thing that makes any future P2 backend reviewable by people without a
   $3k radio — the #4815 lesson applied before the fact rather than after.
3. **Then judge the backend on its merits**, with a sim to develop against and a
   real radio (his) available for the final proof. That is a much better position
   to start from than a cold months-long build against hardware nobody has.

The sim is worth building. The backend is a decision for the maintainer and
whoever has the appetite, not something to commit to on a friend's behalf.
