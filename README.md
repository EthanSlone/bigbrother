# BigBrother

A self-hosted NVR built to replace Frigate: proven plumbing (go2rtc, ffmpeg) with custom
detection, verification and alerting on top. The design and decisions live in the
"NVR Tool" project (`design/architecture.md`).

**Status: Phase 1.** Camera probe, go2rtc stream hub, 24/7 recording with retention, and an
MQTT event bus. Detection, alerts and the web app come next.

```
Cameras ──RTSP──> go2rtc ──restream──> recorder ──> /recordings/<camera>/<date>/<hour>/<MM.SS>.mp4
                    │                     │                      + SQLite index (/data)
                    │                     └──> MQTT: bigbrother/recorder/<camera>/state
                    └──> (later) detector, live view
```

## Test setup on Windows

1. Install **Docker Desktop** with the WSL2 backend and start it.
2. Open PowerShell in this folder.
3. Create your local config files (both are git-ignored):
   ```powershell
   Copy-Item .env.example .env
   Copy-Item config\cameras.example.yml config\cameras.yml
   ```
   Put the camera username and password in `.env`. In `cameras.yml`, list your 5 cameras with
   their IP addresses. Camera passwords only ever go in `.env`.
4. Build, then validate the config:
   ```powershell
   docker compose build
   docker compose run --rm tools configure
   ```
5. **Phase 0: probe the cameras.** This reports codec, resolution, real frame rate,
   keyframe interval and bitrate for every stream, plus a storage estimate:
   ```powershell
   docker compose run --rm tools probe
   ```
   The report is saved to `config\generated\probe-report.md`. Share it in our chat.
6. Start the stack:
   ```powershell
   docker compose up -d
   docker compose logs -f recorder
   ```
   - go2rtc's web UI is at http://localhost:1984, so you can check that every stream plays.
   - Recordings appear about a minute after start:
     `docker compose exec recorder ls -R /recordings`
   - Copy a clip out to watch it:
     `docker compose cp recorder:/recordings/front_door/2026-11-25/18/15.00.mp4 .`

### Testing without cameras

The `fakecam` service loops video clips as password-protected RTSP cameras, each with a
main stream and a 640px substream like a real Annke. The rest of the stack can't tell
them apart from real cameras, and replaying the same clips gives repeatable detection
results.

1. Put clips (`.mp4`, `.mkv`, `.mov`) in the `testdata` folder. Each clip becomes one
   camera named after the file. The best clips, in order:
   - **Your own footage, exported from Frigate**: real people, cars, and the false alarms
     that bother you (shadows, bugs at night, rain). These become our test set.
   - **Free stock CCTV clips** (e.g. Mixkit, Coverr). Pick high, fixed-angle shots with
     people or cars in them.
2. Generate the fake cameras. If you don't have a `config\cameras.yml` yet, this
   writes one for you:
   ```powershell
   docker compose run --rm tools fakecam
   docker compose run --rm tools configure
   docker compose --profile fakecam up -d
   ```
3. The fake cameras' UI is at http://localhost:1985, and the main stack's go2rtc is at
   http://localhost:1984.

To switch to real cameras later, replace the `cameras:` section in `cameras.yml`,
re-run `configure`, then run `docker compose --profile fakecam down` and
`docker compose up -d`.

**Public live feeds** (HLS `.m3u8`), such as state DOT traffic cameras, can be added as a
camera with `main: ffmpeg:https://.../playlist.m3u8#video=copy`. They're useful for
multi-day soak tests of recording. They're less useful for detection: they show distant
highway traffic at low resolution. See the example in `cameras.example.yml`.

### Keep video out of OneDrive

This folder syncs to OneDrive. Recordings and the database live in Docker volumes by
default (`RECORDINGS_PATH=recordings` in `.env`), so they never land in this folder. Leave it
that way on this PC. On the Proxmox server, set `RECORDINGS_PATH` to a path on the big array.

## Commands

| Command | What it does |
|---|---|
| `docker compose run --rm tools configure` | Validates `cameras.yml`, checks `.env`, writes `config/generated/go2rtc.yaml` (holds no secrets; go2rtc reads them from the environment) |
| `docker compose run --rm tools probe` | Phase 0 camera report |
| `docker compose run --rm tools fakecam` | Turns clips in `testdata/` into fake cameras |
| `docker compose --profile fakecam up -d` | Starts the stack plus the fake cameras |
| `docker compose up -d` | Starts go2rtc, mosquitto and the recorder |
| `docker compose restart go2rtc recorder` | Applies camera changes after re-running `configure` |

## How recording works

- **Single camera connection.** Each camera is opened once, by go2rtc. Everything else reads
  go2rtc's restream.
- **No re-encoding.** ffmpeg copies video straight into 60-second MP4 segments aligned to the
  clock. H.265 segments get tagged `hvc1` so they play on iPhone. G.711 audio is converted to
  AAC, only if `audio: true`.
- **UTC everywhere.** File names and the database use UTC, so DST changes never create
  duplicate or missing hours. The app will convert to local time.
- **Supervised ffmpeg.** If it crashes, stalls (no data for 90 s) or a camera is unreachable,
  the recorder retries with backoff (2 s up to 60 s). Other cameras are unaffected.
- **Retention.** Every 10 minutes, it deletes segments older than `retention_days` (per camera
  or global), then removes the oldest segments if the disk passes `max_usage_percent`.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```
Tests include a real ffmpeg segmenting run (skipped if ffmpeg isn't installed).

## Security

Only the future web app/API will ever be exposed through the reverse proxy. go2rtc
(1984/8554) is bound to localhost, and MQTT isn't published at all. Keep cameras on an
isolated VLAN with no internet access.
