# flex-sim

A **synthetic FlexRadio-6000 emulator** for testing [AetherSDR](https://github.com/aethersdr/AetherSDR) — a hardware-free spectrum / waterfall / S-meter / CW test "radio" you drive from your browser.

flex-sim looks like a real FlexRadio 6000 on your network: AetherSDR discovers it, connects, and renders a live panadapter, waterfall, S-meter, TX meters and CW from a programmable signal engine. **No radio required.**

> Pure **Python 3.8+ standard library** — zero dependencies. **GPL-3.0**.

---

## Quick start — no Python, no command line

1. **Download the binary** for your computer from the **[Releases page](https://github.com/nigelfenton/flex-sim/releases/latest)**:
   - **Windows:** `flex-sim-windows-x64.exe`
   - **Linux:** `flex-sim-linux-x64`
   - **macOS:** `flex-sim-macos-arm64`
2. **Run it on a computer that is *not* running AetherSDR** — a spare PC, a Raspberry Pi, a NUC, a VM… anything on the same network. *(Why not the same computer? It's one simple rule — see [Networking](#networking--the-one-rule). You can run it on the same machine, it just needs a couple of extra steps.)*
   - **Windows:** double-click `flex-sim-windows-x64.exe`. Windows may warn *"unrecognized app"* — the binary isn't code-signed — choose **More info → Run anyway**.
   - **Linux / macOS:** `chmod +x flex-sim-linux-x64 && ./flex-sim-linux-x64`
3. **Open AetherSDR.** It should list a radio — model **FLEX-6600**, serial **FLEXSIM00**. Select it and connect.
4. **Open the control panel** at the address flex-sim prints on startup — `http://<that-computer-ip>:8731/` — and pick a test pattern. You should see a live waterfall and S-meter.

**If AetherSDR doesn't find it**, tell flex-sim where AetherSDR is so it can announce itself directly:
```
flex-sim-windows-x64.exe --ae 192.168.1.50      # <- the IP of the PC running AetherSDR
```

---

## Networking — the one rule

flex-sim is a **pretend radio on your network**, so **it needs its own IP address, separate from the computer running AetherSDR.** (They both use network port 4992 — on a single IP they'd collide.)

- **Easiest — run it on a different computer** on the same network (spare PC, Raspberry Pi, NUC, VM). It gets its own IP automatically and AetherSDR finds it just like a real radio. Nothing else to set up. **Most people should do this.**
- **Want it on the *same* computer as AetherSDR?** It still needs its own IP, which Windows won't hand a second program directly. Two ways:
  - **WSL (recommended on Windows):** run flex-sim inside Windows Subsystem for Linux — WSL gives it its own IP. See [Same-machine setup](#same-machine-setup-wsl) below. *(This is the proven path.)*
  - **`--port`:** `flex-sim --port 5992 --ae <AE-ip>` keeps it on the same IP but moves it off AetherSDR's port 4992.

Adding **`--ae <AetherSDR-IP>`** is always worth it — it makes flex-sim announce itself straight to AetherSDR (helps when network broadcast doesn't reach, or across subnets).

---

## Run from Python (any OS, no install)
```
python3 flex_sim.py --ae <AetherSDR-IP>
```
On Windows use `python` if that's how Python is installed. Handy flags: `--pattern carrier` · `--ctl-port 8731` · `--version` · `-h`.

## Many radios at once (rack mode)
```
python3 flex_sim.py --radios 3 --models FLEX-6300,FLEX-6600,FLEX-6700 --ae <AE-IP>
```
Runs N virtual radios that AetherSDR sees as separate rigs — a hardware-free multi-radio bench. Each gets its **own IP** (the `--ip` base, then +1, +2 …), serial (`FLEXSIM00…`), and model from `--models` (cycled). Models differ in capacity — **6300/6400 = 2 slices / 1 SCU, 6600 = 4 / 2, 6700/8600 = 8 / 2** — so a mixed rack tests single- vs multi-MCU side by side, with up to the model's slice count of stacked receivers each. On **one host** the extra IPs must exist on the interface first (real rigs each have their own) — see [Same-machine setup](#same-machine-setup-wsl).

## Control panel
`http://<flex-sim-ip>:8731/` — pick a pattern (the hint box says what it exercises in AetherSDR), set the noise floor / signal level in **dBm (with S-units)**, signal width and noise colour; key **TX** (forward-power + SWR meters); send **CW** (normal / full break-in, driven from AetherSDR's own CWX keyer).

## Patterns
`noise_floor` · `ramp` · `cal_tones` · `carrier` · `cw` · `swept_carrier` · `comb` · `step` · `impulse` · `staircase` · `noise` · `tx_blank`. The panel's hint box explains what each one exercises.

## Offline self-test
```
python3 loopback_test.py carrier      # mock-AetherSDR: handshake + VITA + meter checks, no real AE
```

---

## Same-machine setup (WSL)

Running flex-sim on the **same Windows PC** as AetherSDR, via WSL2 (which gives it its own IP, so no port clash):

1. Put this in `%USERPROFILE%\.wslconfig` so WSL's `:4992` doesn't relay onto Windows:
   ```ini
   [wsl2]
   localhostForwarding=false
   ```
2. In WSL: `python3 flex_sim.py --ae <Windows-host-IP-as-seen-from-WSL>` (usually the default gateway, e.g. `172.x.x.1`).
3. AetherSDR (on Windows) discovers flex-sim at WSL's own IP (e.g. `172.x.x.x`).

For **rack mode on one host**, add the extra IPs to the interface first (one per extra radio):
```bash
sudo ip addr add 172.17.189.199/20 dev eth0   # radio 2
sudo ip addr add 172.17.189.200/20 dev eth0   # radio 3
```
(These are cleared when WSL restarts — re-add them after a reboot.)

## Docker
On **Linux**, a `macvlan` network gives the container its own LAN IP (clean — see `docker-compose.yml`). On **Docker Desktop for Windows/Mac**, containers aren't reachable at their own IP from the host, so Docker does **not** solve the same-machine case there — use WSL. Docker is for a **separate Linux box**.

## Build the binary yourself
The Releases binaries are built by GitHub Actions ([`.github/workflows/build.yml`](.github/workflows/build.yml)). To build locally:
```
pip install pyinstaller
pyinstaller --onefile --name flex-sim flex_sim.py    # -> dist/flex-sim(.exe)
```

---

## License
**GPL-3.0-or-later** — see [`LICENSE`](LICENSE) (matches AetherSDR's license).

## Credits
Created by **Nigel Fenton (G0JKN)** — design, direction, and testing against live AetherSDR. Code generated by **Claude (Anthropic)** via Claude Code under Nigel's direction — the same AI-assisted, human-reviewed workflow AetherSDR itself uses.

> Status: **v0.1 (beta).** Wire format reverse-engineered from AetherSDR's own decoder — see [`PROTOCOL.md`](PROTOCOL.md). Design notes in [`DESIGN.md`](DESIGN.md).
