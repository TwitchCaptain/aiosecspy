# Changelog

All notable changes to `aiosecspy` are documented here. The project follows
[Semantic Versioning](https://semver.org/) and this file follows
[Keep a Changelog](https://keepachangelog.com/).

## [0.2.1] - 2026-08-04

### Fixed

- Relaxed the `aiohttp` dependency from `>=3.14.3` to `>=3.9.0` so Home
  Assistant Core can install `aiosecspy` without fighting HA's own aiohttp pin
  (HA 2026.2.3 ships `aiohttp==3.13.3`).

## [0.2.0] - 2026-08-04

### Added

- `EventStream` now folds wire events into `client.cameras` while running:
  MOTION/TRIGGER_M/MOTION_END, CLASSIFY, ARM/DISARM, and ONLINE/OFFLINE update
  the matching `Camera` fields before listeners fire, so consumers can read
  state instead of re-implementing the event machine.
- A dedicated dispatcher task runs listeners from a bounded queue, so a slow
  or hung listener can no longer stall stream reads. Sustained overflow drops
  the oldest events (with a warning) instead of blocking.
- Diagonal PTZ helpers (`ptz_up_left`, `ptz_up_right`, `ptz_down_left`,
  `ptz_down_right`) and `ptz_save_preset()`.
- `RecordingFile` dataclass for `list_motion_files()` results.
- `CLASSIFY_ABSENT` (-99) exported so "never classified" is distinguishable
  from a real 0 score.
- The publish workflow runs the test suite and, for manual dispatches,
  requires the checked-out commit to carry the matching `v<version>` tag.

### Changed (breaking)

- `Camera.score_human` / `score_vehicle` / `score_animal` default to
  `CLASSIFY_ABSENT` instead of `0`, and `Camera.event_object` defaults to
  `None` instead of `"none"`.
- `Camera.last_motion_time` is now a timezone-aware `datetime | None` instead
  of a string.
- Boolean toggles are keyword-only: `toggle_motion(3, arm=True)`,
  `toggle_actions(...)`, `toggle_continuous(...)`, and
  `ptz_zoom(3, zoom_in=True)`.
- `list_motion_files()` returns `list[RecordingFile]` instead of dicts, and
  when no camera filter is given it asks the server for every known camera
  (a bare `++download` returns an empty feed).
- Removed the unused `EventType.ALL` and `EventType.CUSTOM` members.
- PTZ presets are limited to 1-8 to match SecuritySpy's wire protocol;
  preset-name-9/10 entries in `++systemInfo` are ignored.
- `SecSpyClient` rejects usernames containing `:` (ambiguous in the auth blob
  and RTSP userinfo), and `repr(client)` no longer includes the username.

### Fixed

- Clean stream EOF now emits `DISCONNECTED` before reconnecting, so
  connectivity tracking is truthful.
- `stop()` closes the in-flight HTTP response, so stopping the stream (even
  from inside a listener) no longer waits out the read timeout.
- Capped response reads accumulate the full body instead of returning after
  the first network chunk, which truncated large `++systemInfo` replies.
- `set_schedule()` / `set_schedule_override()` update the local `Camera`
  schedule fields on success.

## [0.1.0] - 2026-08-03

Initial release: async client for the SecuritySpy HTTP API with `++systemInfo`
parsing, arm/disarm and schedule control, PTZ, snapshots, RTSP/MJPEG URL
helpers, recording downloads, and a reconnecting `++eventStream` watcher.

[0.2.1]: https://github.com/TwitchCaptain/aiosecspy/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TwitchCaptain/aiosecspy/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/TwitchCaptain/aiosecspy/releases/tag/v0.1.0
