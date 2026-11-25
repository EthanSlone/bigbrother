FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bigbrother ./bigbrother

# tini forwards SIGTERM so ffmpeg can close its last segment cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "bigbrother"]
CMD ["--help"]
