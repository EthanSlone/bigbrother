# testdata

Put video clips here (.mp4, .mkv, .mov). Each one becomes a fake camera.
Then run `docker compose run --rm tools fakecam` (see the main README).

Clips are git-ignored and never committed.

## Starter clips

| File | Shows | Codec |
|---|---|---|
| `parking_lot.mp4` | Overhead parking lot: people walking, a cyclist, cars | H.265 |
| `driveway.mp4` | Cars driving through a wet lot, seen from above | H.264 |
| `hallway.mp4` | Indoor hallway through a wide-angle CCTV lens, people walking | H.264 |

These are upscaled to 2560x1440 with a keyframe every ~2 s so the system treats them like
the real Annke cameras. The detail is still that of the 768x432 originals.

**Credits:** the source footage is from Intel's [sample-videos](https://github.com/intel-iot-devkit/sample-videos)
repository (`person-bicycle-car-detection.mp4`, `car-detection.mp4`, `people-detection.mp4`),
licensed under [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/).
The clips were re-encoded and upscaled.
