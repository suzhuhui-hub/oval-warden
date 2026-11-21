FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

WORKDIR /bot
COPY arena.py bot.py combat.py mechfight.py ./
USER 65534:65534
ENTRYPOINT ["python3", "-B", "-u", "/bot/bot.py"]
