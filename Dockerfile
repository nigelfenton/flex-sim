# flex-sim — synthetic FlexRadio-6000 emulator / AetherSDR test bench, plus the
# standalone accessory simulators that ship alongside it.
# Pure-stdlib Python, so the image is just the interpreter + the sims.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="flex-sim" \
      org.opencontainers.image.description="Synthetic FlexRadio-6000 emulator / test bench for AetherSDR" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

WORKDIR /app

# Every sim, not just the radio. One image serves the whole bench and the
# container chooses which sim runs -- so an accessory can never drift to a
# different build than the radio it is being tested next to.
#
# A glob rather than a list on purpose: station.py imports its siblings at
# module scope, so a sim missing from the image is an ImportError at launch,
# not a degraded run. A glob also means a new sim is containerised the moment
# it lands, with no second edit here to forget.
COPY *.py ./

# Radio:       :4992 discovery + FlexLib control + VITA-49, :8731 control panel
# Accessories: :4531 SPE Expert amplifier, :9600 ACOM 600S
EXPOSE 4992/tcp 4992/udp 8731/tcp 4531/tcp 9600/tcp

# The radio stays the default, so existing invocations such as
#   docker run --rm --network host flex-sim --ae 192.168.1.50
# are unchanged. To run an accessory instead, override the ENTRYPOINT --
#   entrypoint: ["python3", "spe_sim.py"]
# not `command`, because each sim parses its own flags and a bare command
# would be appended to flex_sim.py's argv and rejected.
#
# The accessory sims are interactive by default and their prompt is the point:
# give the container `stdin_open: true` + `tty: true` and `docker attach` lands
# on it (detach with Ctrl-P Ctrl-Q, which leaves the sim running). Pass
# `--no-cli` for a headless/staged run instead.
ENTRYPOINT ["python3", "flex_sim.py"]
