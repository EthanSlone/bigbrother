# BigBrother

A self-hosted NVR built to replace Frigate: proven plumbing (go2rtc, ffmpeg) with custom
detection, verification and alerting on top. The design and decisions live in the
"NVR Tool" project (`design/architecture.md`).

**Status: Phase 4.** Camera probe, go2rtc stream hub, 24/7 recording with retention, object
detection with tracking, zones and masks, alerts to your phone via Pushover with a
second-opinion check, and **the app**: login, live video, an event feed with clips,
recordings with a timeline, and system status. It installs to a phone's home screen.

```
Cameras ──RTSP──> go2rtc ─┬─ main ──> recorder ──> /recordings/<camera>/<date>/<hour>/<MM.SS>.mp4
                          │
                          └─ sub ───> detector: motion → YOLOX → tracker → events
                                         ├─> /data: events DB + snapshots
                                         ├─> MQTT: bigbrother/events, bigbrother/<camera>/<label>
                                         └─> debug viewer http://localhost:5050
web app :8090 ── login ──> live video (go2rtc MSE, proxied), events, recordings, settings
```

## The app

Open **http://<server>:8090** (set `WEB_PORT` in `.env` to change it).

- **First start:** the app asks for a setup code to create the first account. It's printed
  in the web service's log, so only someone who can read the server's logs can claim the
  install:
  ```bash
  docker compose logs web | grep "setup code"
  ```
- **People:** an admin adds everyone else under **Settings → People**. **Members** watch,
  review events, keep events and turn alerts on and off; **admins** can also change
  settings, delete events and manage people. If you're locked out, use the server:
  ```bash
  docker compose exec -it web python -m bigbrother user add NAME      # first one is an admin
  docker compose exec -it web python -m bigbrother user passwd NAME
  docker compose exec web python -m bigbrother user role NAME admin|member
  docker compose exec web python -m bigbrother user reset-2fa NAME    # lost phone
  docker compose exec web python -m bigbrother user list
  ```
- **Two-factor sign-in:** required for admins by default (**Settings → People** can make it
  required for everyone, or optional). Anyone required who hasn't set it up is taken
  through setup right after their password: an authenticator app (Google or Microsoft
  Authenticator, 1Password, Authy…), then 8 one-time recovery codes for a lost phone.
  Codes are only asked for when signing in on a new device. **Move to a new phone** under
  Your account needs the password. The same page lists where you're signed in and can
  sign out every other device.
- **Staying signed in:** with "Keep me signed in", a device stays signed in as long as it
  opens BigBrother at least once every 30 days. Use the same address at home and away
  (each address signs in separately).
- **Live:** video comes straight from go2rtc (MSE, with HLS for older iPhones) through the
  app's own connection, so it needs no extra ports and shares the login. The grid uses the
  substreams; opening a camera plays its main stream. Tiles that are off screen or in a
  background tab stop streaming.
- **Events:** filter by camera, object, alert outcome and day. Each event has its snapshot,
  a clip with 5 s before and after, and why an alert was or wasn't sent. The Events button
  counts what's new since you last looked (per person), and new events are marked.
  **Keep** (the star) holds an event past `storage.event_retention_days` until you unstar
  it. Admins can delete an event; its snapshot and saved clip go with it, the recording
  stays.
- **Recordings:** a day timeline per camera (24 h, 6 h or 1 h). Click or drag to play from
  any moment; playback continues across the 1-minute files and skips gaps. "Save 5 minutes"
  downloads an MP4. **All cameras** plays every camera side by side on one clock, at 1×
  to 8×; select a camera to watch it alone from the same moment.
- **Phone:** install it as an app. Android: the app offers **Install BigBrother** (or Chrome's
  ⋮ menu → Install app). iPhone: Share → Add to Home Screen, in Safari or Chrome (then sign in once inside the app: it has its own sign-in). It opens full screen
  from its own icon, with shortcuts to Events and all-camera Recordings.

**Behind your reverse proxy:** point it at port 8090, with WebSocket upgrades allowed
(needed for live video). Put the proxy's IP in `TRUSTED_PROXIES` in `.env` so login rate
limiting sees real client addresses and cookies are marked secure under HTTPS.

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
  - **model ms:** how long the model takes per frame.
  - **wait ms:** time a frame spent queued behind other cameras. If this climbs, the CPU
    can't keep up: give the VM more cores or raise `detector.workers`.
  - **filtered:** objects that never became events (brief or low-confidence detections).
  - **skipped:** frames dropped because the model was busy.

The detection model (~36 MB) downloads automatically on first start. It's verified by
checksum and cached in a Docker volume.

### Playback

- **Event clips:** every event in the viewer has a **▶ Clip** button. It plays the event
  from 5 s before to 5 s after.
- **Timeline:** shows each camera over the last 1, 6 or 24 hours. Grey is recorded, gaps
  are missing footage, and ticks are events (green for people, blue for vehicles). Click
  anywhere to play from that moment; playback continues across recordings. Click a tick
  to play that event's clip.

How clips are made:
- They're stitched from the 1-minute recordings without re-encoding, so they're quick to
  build and lossless. Cuts land on keyframes, so a clip can start or end a second early.
- Recordings are written in 1-minute pieces, so the newest minute isn't playable until
  that piece closes. A clip of something happening right now may stop early.
- Clips are cached for 2 days and rebuilt on demand.
- Events, their snapshots and alert pictures are kept as long as recordings
  (`storage.retention_days`).

### Zones and masks

In the app, open **Settings → Zones and masks**, pick a camera, then click on the picture
to place corners (drag corners to adjust):
- **Zone:** a named area like `porch` or `driveway`. You can limit a zone to certain
  objects. An object enters a zone when its ground point (bottom-centre of its box) is
  inside the zone on 2 checks in a row. Events record every zone they entered, and alert
  rules can require one.
- **Motion mask:** movement here is ignored. Use it for trees, flags and the camera's
  timestamp overlay.
- **Object mask:** detections whose ground point falls here are dropped, e.g. a statue
  that looks like a person.

**Save** writes them to `config/cameras.yml` and restarts detection to apply them.
Coordinates are fractions of the frame, so they fit any resolution. (The debug viewer's
editor still works too: it generates YAML to paste in by hand.)

### Settings in the app

**Settings** edits `config/cameras.yml` for you: cameras (Hikvision/Annke, UniFi Protect,
or any RTSP address), zones and masks, alert rules (objects, zones, cooldown, hours), and
how long to keep recordings.
- Every save is checked with the same rules the services use; a bad value is refused
  with the reason and nothing is written.
- Your comments in the file are kept, and the previous version is saved in
  `config/backups/` (last 20).
- Only the services a change affects restart, about 20 seconds: a zone change restarts
  detection and alerts but never interrupts recording.
- Passwords stay in `.env`. Cameras refer to them by variable name, e.g. `${CAM_PASSWORD}`;
  a new variable needs adding to `.env` and `docker compose up -d`.

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
| `docker compose run --rm tools test-alert` | Sends one test notification |
| `docker compose logs -f alerter` | Alert decisions as they happen |
| `docker compose --profile fakecam up -d` | Starts the stack plus the fake cameras |
| `docker compose up -d` | Starts go2rtc, mosquitto, the recorder, the detector and the alerter |
| `docker compose logs -f detector` | Detector log; per-camera stats print every minute |
| `docker compose run --rm tools evaluate /testdata/parking_lot.mp4 --fps 5 2` | Runs a clip through detection offline and counts events, to compare settings |
| `docker compose run --rm -v "$PWD:/w" tools evaluate-testset /w/testset.zip` | Scores detection settings on a test set exported from the app (see below) |
| `docker compose restart go2rtc recorder` | Applies camera changes after re-running `configure` |

## How detection works

1. **Decode the substream.** ffmpeg decodes each camera's substream at `fps` (default 5)
   frames per second, at up to 1280 px wide. The model looks at the whole frame at 640 px;
   the extra resolution is for zoom (step 3). Live video in the app comes straight from
   go2rtc, so nothing is decoded just for viewing.
2. **Check for motion.** A cheap motion check runs on every frame. The model only runs
   when something moves, while a moving object is being tracked, or every 30 s to
   re-confirm parked objects. Whole-frame changes (IR switching on, exposure jumps) count
   as lighting changes, not motion.
3. **Find objects.** YOLOX-s (Apache-2.0, COCO-trained) finds people, vehicles, bikes and
   pets. It runs on OpenVINO on Intel CPUs. Moving to a GPU later is a config change:
   `detector.backend: onnxruntime`, `device: cuda`.
   All cameras share one model through an inference pool, by default one inference at a
   time per 8 CPUs. That way cameras queue for the CPU instead of fighting over it.
   The model runs at 640x384 rather than its native 640x640: camera frames are 16:9, so
   a square input is ~40% grey padding. Same detections, ~40% less work
   (`detector.input_size`).
   **Zoom:** at 640 px a person at the far end of the yard is a few pixels tall. Each
   small moving area that nothing confident explains gets a second look: a crop at the
   substream's full resolution (2x closer on a 1280x720 substream). Small objects being
   tracked stay zoomed on when the whole-frame pass loses them. Spots where a zoom found
   nothing (trees, bugs, rain) are left alone for 10 s. Costs one extra inference, only
   while something small moves. On a test clip with far-away people it found 7 people and
   a car where the whole-frame pass found 4 people.
4. **Track objects.** The tracker gives each object an identity across frames, so one
   person walking by is one event. It matches by box overlap first, then by distance in
   the direction of travel. That keeps identities stable even when the model only runs
   once or twice a second. Weak detections (below 0.5) can keep an existing track alive
   but never start one. Car/truck/bus confusions count as the same object, and objects
   that stop moving are marked stationary.
   - **Motion gate:** a new object must appear where something moved in the last 2 s
     (or be very confident, 0.85+). A coat on a chair or a bush that looks like a person
     never starts an event. Tiny movements that are too small to wake the model still
     count here, so far-away people aren't blocked.
   - **Label voting:** the model often reports one animal as both "person" and "dog".
     Overlapping person/animal detections become votes on one object, which takes the
     majority label.
5. **Confirm events.** An object becomes an event only once it has been seen for as long
   as `min_frames` frames take at the configured fps, and either its median score
   reaches `threshold` (or the object's own entry in `thresholds`) or one sighting is very
   confident (`threshold` + 0.2). One confident frame of a shadow isn't enough.
   - **Night:** infrared footage is detected per camera (it has no colour at all). There,
     the score needed rises by `night_boost` (0.1), one more sighting is needed, and the
     second opinion before alerting must be 0.1 surer too. Events remember they were at
     night.
   - **Label mix-ups wait:** if the votes are split (say 55% dog, 45% person), the event
     waits for more sightings until one label has 60%.
6. **Save and publish.** Each event keeps its best snapshot: confident, large, not cut off
   by the frame edge. Events go to SQLite and MQTT (`start` / `update` / `end`), plus a
   retained per-label count (`bigbrother/<camera>/person`) for Home Assistant later.

**Model choice (benchmarked 2026-11-25 on the test clips):**
- **yolox_tiny:** missed most cars seen from above.
- **yolox_s:** found every person, cyclist and car, at ~40 ms per frame per 2 cores.
- **yolox_m:** ~2x slower for a small gain. It's reserved for the Phase 3 "second opinion"
  check before alerts.

## Alerts

The **alerter** service listens for detector events and, for each one, decides, verifies
and notifies:

1. **Rules** (per camera, under `alert:`):
   - **Object:** is it an object that alerts on this camera (`labels`)?
   - **Armed:** are alerts armed?
   - **Schedule:** is it inside the alert schedule?
   - **Moving:** is it moving? Parked cars never alert.
   - **Zone:** has it entered a required zone (`zones`)?
   - **Cooldown:** has no alert for the same camera and object type gone out within
     `cooldown_seconds`?

   Rules are re-checked while the object stays in view, so a person who walks onto the
   porch a few seconds later still triggers the alert.
2. **Second opinion:**
   - The alerter grabs a full-resolution frame from go2rtc and runs the bigger YOLOX-m
     model on the area around the object.
   - If the object has already moved on, it checks the detector's own snapshot instead,
     which comes from the exact moment of detection.
   - The alert is only sent if the bigger model also sees the same kind of object. It
     tries up to 3 times.
3. **Notification:**
   - Pushover delivers it to iPhone and Android, with the full-res picture and the
     object boxed, to everyone who follows that camera.
   - It's sent once per event, and **once per visit**: the same kind of object on another
     camera within `alerts.group_seconds` (90 s) is recorded as "Same visit" instead of a
     second notification. This is tracked per person, so someone who only follows the
     front door still hears about the visitor who came up the driveway.
   - A 4xx error is never retried, as Pushover asks. Server errors are retried after 5 s.

**Every event gets an outcome**, shown in the viewer's *Alert* column:
- **sent**, **logged** (Pushover not set up yet), **failed** or **same visit**,
- or **not sent**, with the reason. For example: "cooldown: a person alert was sent 14s ago",
  "hasn't entered porch", "second opinion found no person", or "disarmed".

Click an alert badge to see the exact picture that was sent.

**Arm or disarm** with the lamp at the top of the app, or over MQTT: publish `on` or
`off` to `bigbrother/alerts/armed/set`. The state is published to `bigbrother/alerts/armed`.

**Automatically:** under **Settings → Alerts**, set the hours alerts should be on
(`alerts.arm_schedule`, e.g. overnight and weekdays 9 to 5). Alerts turn on at each
window's start and off at its end. Only those moments act, so the lamp still works in
between: turn alerts off by hand and they stay off until the next start.

**Set up Pushover:**
1. Admin: create an Application named BigBrother on pushover.net and paste its API token
   under **Settings → Alerts**.
2. Everyone who wants alerts: install the Pushover app (one-time purchase after a 30-day
   trial), then under **Settings → Your account → Phone alerts** paste your user key from
   pushover.net, pick your cameras, and press **Send me a test**. Keys are checked with
   Pushover before they're saved.
3. Optional: set **Address of this app from outside** under Settings → Alerts so each
   alert has a "View clip" link.

`PUSHOVER_TOKEN` and `PUSHOVER_USER` in `.env` still work: the token is used when none is
set in the app, and that user key gets every alert. `docker compose run --rm tools
test-alert` sends a test to everyone set up.

The verifier model, YOLOX-m (~100 MB), downloads on first start.

## Health alerts

The alerter also watches the NVR itself and tells you, through the same notifier, when:
- a camera stops recording (no new recording saved for a few minutes; the recorder's last
  error is included),
- detection stops on a camera,
- the recorder or detector service goes offline, or go2rtc stops answering,
- the recordings disk gets fuller than retention should allow.

A problem must last 5 minutes (`alerts.health.after_minutes`) before it's reported, so
restarts don't alert. Each problem is reported once, with a reminder every 12 hours, and
"back to normal" follows when it's fixed. Health alerts go out even when intrusion alerts
are switched off. Current problems show under **Settings → System status**, with a red dot
on Settings.

## Detection feedback and test sets

Detection is tuned on your own cameras, from your verdicts:
- **On each event** ("Was this right?"): *Correct*, *False alarm*, or *Wrong object* and
  what it really was. The Events page can filter to events you haven't reviewed, or to
  false alarms.
- **Missed something?** On Recordings, go to the moment and press **Report a missed
  detection**. 10 seconds either side are kept, even after the 24/7 footage is deleted.
- **Export:** Settings → Detection feedback → **Build export** makes a zip of every reviewed
  event and reported miss (optionally plus 50 unreviewed events): snapshots, alert pictures
  and clips shrunk to 960 px at 5 fps, about 0.2 MB per event. No passwords or camera
  addresses are included.

`evaluate-testset` replays the zip through the same detection code the detector runs,
with the old settings ("before"), the current ones ("now") and any variants you name, and
scores each against your verdicts: false alarms removed, correct events kept, wrong objects
fixed, misses found:

```bash
docker compose run --rm -v "$PWD:/w" tools evaluate-testset /w/bigbrother-testset-20261126-0900.zip \
    --variant "strict:threshold=0.7" --variant "nozoom:zoom=false"
```

## How recording works

- **Single camera connection.** Each camera is opened once, by go2rtc. Everything else reads
  go2rtc's restream.
- **Event clips are kept separately.** Once an event's recording is on disk, its clip (5 s
  before to 5 s after, at most the first 5 minutes) is cut out without re-encoding and kept
  for `storage.event_retention_days` (default 30), while 24/7 footage follows
  `retention_days`. Snapshots and alert pictures follow the events.
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

Only the web app (8090) is meant to be exposed, through the reverse proxy. go2rtc
(1984/8554) and the debug viewer (5050) are bound to localhost, and MQTT isn't published at
all.

The app:
- Passwords are hashed with scrypt. Sessions are random tokens, stored only as hashes,
  in HttpOnly SameSite cookies (Secure behind HTTPS).
- Failed logins are limited to 10 per client and 10 per username per 15 minutes. Signed-in
  devices stay signed in during a lockout. Changing a password signs out every other device.
- Two-factor sign-in uses standard 6-digit codes (TOTP, RFC 6238); a code can't be used
  twice, and recovery codes are stored hashed and work once.
- Members can't reach settings, accounts, event deletion or test-set exports; the server
  enforces this, not just the screens.
- Every request except the login page and static files needs a session; that includes
  video, snapshots, clips and the live stream. Requests that change something need a custom
  header, which blocks cross-site request forgery. Live-stream WebSockets must come from
  the app's own origin.
- Only configured cameras' streams can be opened, and only for viewing: go2rtc's admin API
  and WebRTC aren't reachable through the app.
- A strict Content-Security-Policy allows only the app's own scripts. Keep cameras on an
isolated VLAN with no internet access.
