FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app
COPY requirements.txt .
# OpenVINO sends usage telemetry on import unless opted out, so opt out at build time.
RUN pip install --no-cache-dir -r requirements.txt \
 && opt_in_out --opt_out
COPY bigbrother ./bigbrother

# tini forwards SIGTERM so ffmpeg can close its last segment cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "bigbrother"]
CMD ["--help"]
