# BigBrother

A self-hosted NVR built to replace Frigate: proven plumbing (go2rtc, ffmpeg) with custom
detection, verification and alerting on top. The design and decisions live in the
"NVR Tool" project (`design/architecture.md`).

**Status: Phase 2.** Camera probe, go2rtc stream hub, 24/7 recording with retention, object
detection with tracking and a debug viewer, and an MQTT event bus. Next come alerts
(Phase 3), then the app (Phase 4).

```
Cameras ──RTSP──> go2rtc ─┬─ main ──> recorder ──> /recordings/<camera>/<date>/<hour>/<MM.SS>.mp4
                          │
                          └─ sub ───> detector: motion → YOLOX → tracker → events
                                         ├─> /data: events DB + snapshots
                                         ├─> MQTT: bigbrother/events, bigbrother/<camera>/<label>
                                         └─> debug viewer http://localhost:5050
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

### Detection debug viewer

Open **http://localhost:5050**. It shows each camera live with boxes and the recent events
with snapshots. Click a snapshot to enlarge it.

- **Green box:** a confirmed event.
- **Yellow box:** being tracked, not confirmed yet.
- **Grey box:** stationary (e.g. a parked car).
- **Thin red outline:** raw motion.
- **Per-camera numbers:**
  - **preview / analysed / detect fps:** frames shown in your browser / frames checked for
    motion / frames the model actually ran on. The model skips frames with no motion.
  - **model ms:** time per analysis.
  - **filtered:** objects that never became events (brief or low-confidence detections).
  - **skipped:** frames dropped because the model was busy.

The detection model (~36 MB) downloads automatically on first start. It's verified by
checksum and cached in a Docker volume.

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
| `docker compose up -d` | Starts go2rtc, mosquitto, the recorder and the detector |
| `docker compose logs -f detector` | Detector log; per-camera stats print every minute |
| `docker compose run --rm tools evaluate /testdata/parking_lot.mp4 --fps 5 2` | Runs a clip through detection offline and counts events, to compare settings |
| `docker compose restart go2rtc recorder` | Applies camera changes after re-running `configure` |

## How detection works

1. **Decode the substream.** ffmpeg decodes each camera's substream at `live_fps` (default
   15) frames per second, scaled to at most 640 px wide. The preview shows every frame.
   Every third frame (`fps`, default 5) is analysed, and between analyses the boxes move
   along with each object's velocity.
2. **Check for motion.** A cheap motion check runs on every frame. The model only runs
   when something moves, while a moving object is being tracked, or every 30 s to
   re-confirm parked objects. Whole-frame changes (IR switching on, exposure jumps) count
   as lighting changes, not motion.
3. **Find objects.** YOLOX-s (Apache-2.0, COCO-trained) finds people, vehicles, bikes and
   pets. It runs on OpenVINO on Intel CPUs. Moving to a GPU later is a config change:
   `detector.backend: onnxruntime`, `device: cuda`.
   All cameras share one model through an inference pool, by default one inference at a
   time per 8 CPUs. That way cameras queue for the CPU instead of fighting over it.
4. **Track objects.** The tracker gives each object an identity across frames, so one
   person walking by is one event. It matches by box overlap first, then by distance in
   the direction of travel. That keeps identities stable even when the model only runs
   once or twice a second. Weak detections (below 0.5) can keep an existing track alive
   but never start one. Car/truck/bus confusions count as the same object, and objects
   that stop moving are marked stationary.
5. **Confirm events.** An object becomes an event only once it has been seen for as long
   as `min_frames` frames take at the configured fps, and either its median score
   reaches `threshold` or one sighting is very confident (`threshold` + 0.2). One
   confident frame of a shadow isn't enough.
6. **Save and publish.** Each event keeps its best snapshot: confident, large, not cut off
   by the frame edge. Events go to SQLite and MQTT (`start` / `update` / `end`), plus a
   retained per-label count (`bigbrother/<camera>/person`) for Home Assistant later.

**Model choice (benchmarked 2026-11-25 on the test clips):**
- **yolox_tiny:** missed most cars seen from above.
- **yolox_s:** found every person, cyclist and car, at ~40 ms per frame per 2 cores.
- **yolox_m:** ~2x slower for a small gain. It's reserved for the Phase 3 "second opinion"
  check before alerts.

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

## Privacy

Nothing phones home. OpenVINO normally sends a usage event to Google Analytics whenever
it's imported. The Docker image opts out at build time (`opt_in_out --opt_out`), and the
detector writes the same opt-out file before loading OpenVINO. The only outbound
connection is the one-time model download from GitHub, which is checksum-verified.

## Security

Only the future web app/API will ever be exposed through the reverse proxy. go2rtc
(1984/8554) and the debug viewer (5050) are bound to localhost, and MQTT isn't published at
all. Keep cameras on an
isolated VLAN with no internet access.
