# flex-sim — synthetic FlexRadio-6000 emulator / AetherSDR test bench.
# Pure-stdlib Python, so the image is just the interpreter + one file.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="flex-sim" \
      org.opencontainers.image.description="Synthetic FlexRadio-6000 emulator / test bench for AetherSDR" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

WORKDIR /app
COPY flex_sim.py .

# :4992 = SmartSDR discovery + FlexLib control + VITA-49 data; :8731 = control panel
EXPOSE 4992/tcp 4992/udp 8731/tcp

# Args pass straight through, e.g.:  docker run --rm --network host flex-sim --ae 192.168.1.50
ENTRYPOINT ["python3", "flex_sim.py"]
