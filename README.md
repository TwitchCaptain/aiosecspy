# aiosecspy

Async Python client for [SecuritySpy](https://www.bensoftware.com/securityspy/) web API (v5 and v6).

Supports `++systemInfo`, long-lived `++eventStream`, arm/disarm, schedules, PTZ, snapshots, and file download.

Used by the [hass-securityspy](https://github.com/TwitchCaptain/hass-securityspy) Home Assistant integration.

## Install

```bash
pip install aiosecspy
```

## Quick start

```python
import asyncio
from aiosecspy import SecSpyClient

async def main() -> None:
    async with SecSpyClient("192.168.1.10", 8000, "user", "pass") as client:
        print(client.info.version, len(client.cameras))
        client.events.start()
        # ...

asyncio.run(main())
```

## Releasing to PyPI

Publishing uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (no API token).

1. Bump `version` in [`pyproject.toml`](pyproject.toml).
2. Commit, merge to `main`, and create a GitHub Release / tag (for example `v0.1.0`).
3. The [`publish.yml`](.github/workflows/publish.yml) workflow builds the sdist/wheel and uploads to PyPI using the `pypi` GitHub Environment.

First successful publish activates the pending publisher you configured for `TwitchCaptain/aiosecspy` → `publish.yml` → environment `pypi`.
