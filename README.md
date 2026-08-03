# aiosecspy

Async Python client for the [SecuritySpy](https://www.bensoftware.com/securityspy/) web API (v5 and v6).

Supports `++systemInfo`, the long-lived `++eventStream`, arm/disarm, schedules, PTZ, snapshots, and recording download.

Used by the [hass-securityspy](https://github.com/TwitchCaptain/hass-securityspy) Home Assistant integration.

## Install

```bash
pip install aiosecspy
```

Requires Python 3.12+ and `aiohttp`.

## Quick start

```python
import asyncio

from aiosecspy import SecSpyClient


async def main() -> None:
    async with SecSpyClient("192.168.1.10", 8000, "user", "pass") as client:
        info = await client.refresh()
        print(info.name, info.version, len(client.cameras))

        for camera in client.cameras.values():
            print(camera.number, camera.name, camera.armed_motion)

        jpeg = await client.get_image(3, width=1280)
        print(len(jpeg), "bytes of JPEG")


asyncio.run(main())
```

`async with` creates and owns an `aiohttp.ClientSession`. Pass `session=` if you
already have one (Home Assistant does); the client will then leave it open when
it closes.

## Events

`++eventStream` is a long-lived HTTP response of CR-delimited lines. `EventStream`
reads it in a background task, reconnects with exponential backoff and jitter,
and re-reads `++systemInfo` when the server reports a configuration change.

```python
from aiosecspy import Event, EventType, SecSpyClient


def on_event(event: Event) -> None:  # sync or async callbacks both work
    if event.event_type is EventType.MOTION:
        print(event.when, "motion on camera", event.camera_number)


async def watch() -> None:
    async with SecSpyClient("192.168.1.10", 8000, "user", "pass") as client:
        unsubscribe = client.events.add_listener(on_event)
        client.events.start()
        try:
            await asyncio.sleep(3600)
        finally:
            unsubscribe()
            await client.events.stop()
```

`Event.when` is timezone-aware, using the server's UTC offset from `++systemInfo`.

Alongside the wire event types, the stream emits a few of its own so you can
drive UI state without polling:

| Event type | Meaning |
| --- | --- |
| `CONNECTED` | The stream attached successfully. |
| `DISCONNECTED` | The stream dropped; a reconnect is scheduled. |
| `AUTHFAIL` | Credentials were rejected. The watcher stops instead of retrying, so this is the signal to start a reauth flow. |
| `REFRESH` / `REFRESHFAIL` | Result of the automatic `++systemInfo` reload after `CONFIGCHANGE`. |

A listener that raises is logged and skipped; it never takes down the stream.

## API surface

| Area | Methods |
| --- | --- |
| Lifecycle | `open()`, `close()`, `refresh()`, `camera(number)`, `cameras`, `info` |
| Arming | `toggle_motion()`, `toggle_actions()`, `toggle_continuous()`, `trigger_motion()` |
| Schedules | `set_schedule()`, `set_schedule_override()`, `set_schedule_preset()` |
| PTZ | `ptz_left/right/up/down/zoom/home/stop()`, `ptz_preset(1-8)`, `ptz_command()` |
| Video | `get_image()`, `image_url()`, `mjpeg_url()`, `hls_url()`, `rtsp_url()` |
| Recordings | `list_motion_files()`, `download_file()`, `download_latest_motion_recording()` |

`refresh()` replaces the `Camera` objects but carries over the runtime state the
event stream maintains (motion, classification scores, trigger reasons), so a
refresh does not blank out live state.

### Errors

Everything derives from `SecSpyError`:

| Exception | Raised when |
| --- | --- |
| `AuthenticationError` | HTTP 401/403 — bad username or password. |
| `UnsupportedError` | The server does not implement the endpoint (HTTP 404 on a command). |
| `RequestError` | Transport failure, timeout, or an HTTP/body error. |
| `UntrustedHostError` | A server-supplied link pointed off the configured server. |
| `ResponseTooLargeError` | The response exceeded the size ceiling. |
| `InvalidResponseError` | The body could not be parsed or carried a DTD. |

The last three are subclasses of `RequestError`, so catching `RequestError` is
enough for a simple "cannot talk to the server" path.

### Version differences

`++ssControlContinuous` is missing from many builds. `toggle_continuous()` falls
back to the schedule API when the server answers 404, matching the
"Armed 24/7" / "Disarmed 24/7" schedule by name (v5 calls the latter
"Unarmed 24/7"). Real failures are not swallowed.

Trigger reason bit 512 means Animal on v5 and HomeKit on v6; the parser uses the
server's major version to decide.

## Security

Read [SECURITY.md](SECURITY.md) before logging anything this library produces.
The short version: the `auth` query parameter is equivalent to the password, so
`image_url()`, `mjpeg_url()`, `hls_url()` and `rtsp_url()` all return secrets.

## Development

```bash
pip install -e ".[dev]"
pytest -q --cov
ruff check . && ruff format --check .
mypy aiosecspy
```

## Releasing to PyPI

Publishing uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (no API token).

1. Bump `__version__` in [`aiosecspy/__init__.py`](aiosecspy/__init__.py); the
   packaging metadata reads it from there.
2. Commit, merge to `main`, and create a GitHub Release / tag (for example `v0.1.0`).
3. [`publish.yml`](.github/workflows/publish.yml) checks the tag against the
   package version, builds the sdist/wheel, and uploads to PyPI with
   attestations via the `pypi` GitHub Environment.
