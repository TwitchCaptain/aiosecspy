"""Async HTTP client for SecuritySpy."""

from __future__ import annotations

import base64
import contextlib
import ipaddress
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import aiohttp

from .const import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    DEFAULT_TIMEOUT,
    DOWNLOAD_DATE_FORMAT,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIMENSION,
    MAX_PORT,
    MAX_QUALITY,
    MAX_TEXT_RESPONSE_BYTES,
    MIN_PORT,
    MIN_QUALITY,
    PTZ_DOWN,
    PTZ_HOME_CMD,
    PTZ_LEFT,
    PTZ_PRESET_BASE,
    PTZ_PRESET_MAX,
    PTZ_PRESET_MIN,
    PTZ_RIGHT,
    PTZ_STOP,
    PTZ_UP,
    PTZ_ZOOM_IN,
    PTZ_ZOOM_OUT,
    CameraMode,
)
from .events import EventStream
from .exceptions import (
    AuthenticationError,
    RequestError,
    ResponseTooLargeError,
    UnsupportedError,
    UntrustedHostError,
)
from .systeminfo import parse_system_info
from .util import parse_xml, redact

if TYPE_CHECKING:
    from .models import Camera, ServerInfo

# Sequences that must never appear in a request path we build from a
# server-supplied href, because they can retarget the request.
_UNSAFE_PATH = ("://", "\\", "\n", "\r", "\t")


def _normalize_host(host: str) -> str:
    """Return a host usable in a URL authority, bracketing IPv6 literals."""
    host = host.strip()
    if not host:
        msg = "host must not be empty"
        raise ValueError(msg)
    # Tolerate users pasting a full URL into a "host" field.
    if "://" in host:
        host = urlsplit(host).hostname or ""
    host = host.strip("[]").rstrip("/")
    if not host or any(c in host for c in "/?#@ \t\r\n"):
        msg = f"invalid host: {host!r}"
        raise ValueError(msg)
    # Only IPv6 literals need brackets; a hostname is not an IP and raises here.
    with contextlib.suppress(ValueError):
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]"
    return host


class SecSpyClient:
    """Async SecuritySpy web API client.

    Credentials are sent as the SecuritySpy ``auth`` query parameter (URL-safe
    base64 of ``username:password``). That parameter is equivalent to the
    password, so URLs built by this client are secrets: never log them, and
    prefer :meth:`get_image` over :meth:`image_url` when you do not need to hand
    a URL to an external player.
    """

    def __init__(  # noqa: PLR0913 - connection settings, all optional but host/port/user/pass
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_ssl: bool = False,
        verify_ssl: bool = True,
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    ) -> None:
        """Build a client for one SecuritySpy server.

        Nothing is sent until :meth:`open` or :meth:`refresh` is awaited. When
        ``session`` is supplied the caller keeps ownership of it and
        :meth:`close` will not close it.
        """
        port = int(port)
        if not MIN_PORT <= port <= MAX_PORT:
            msg = f"port must be {MIN_PORT}-{MAX_PORT}, got {port}"
            raise ValueError(msg)
        if timeout <= 0:
            msg = f"timeout must be positive, got {timeout}"
            raise ValueError(msg)

        self.host = _normalize_host(host)
        self.port = port
        self.scheme = "https" if use_ssl else "http"
        self.base_url = f"{self.scheme}://{self.host}:{port}/"
        self._base_netloc = urlsplit(self.base_url).netloc.lower()
        self._username = username
        self._password = password
        self._auth = base64.urlsafe_b64encode(f"{username}:{password}".encode()).decode()
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.max_download_bytes = max_download_bytes
        self._session = session
        self._owns_session = session is None
        self.info: ServerInfo | None = None
        self.events = EventStream(self)

    def __repr__(self) -> str:
        """Describe the client without exposing credentials."""
        return f"<SecSpyClient {self.scheme}://{self.host}:{self.port} user={self._username!r}>"

    @property
    def session(self) -> aiohttp.ClientSession | None:
        """Active aiohttp session, or None before :meth:`open`."""
        return self._session

    @property
    def cameras(self) -> dict[int, Camera]:
        """Camera map from the last refresh, keyed by camera number."""
        if not self.info:
            return {}
        return self.info.cameras

    @property
    def server_timezone(self) -> timezone:
        """Server's UTC offset, from ++systemInfo (UTC until first refresh)."""
        if self.info is None:
            return UTC
        return timezone(timedelta(seconds=self.info.gmt_offset_seconds))

    def _url(self, api: str) -> str:
        """Turn an API name or server-supplied path into an absolute URL.

        The result is pinned to the configured server: anything that would
        change host, port or scheme is rejected so the ``auth`` parameter is
        never handed to a third party.
        """
        if api.startswith("/") or any(bad in api for bad in _UNSAFE_PATH):
            msg = f"refusing to request unsafe path: {redact(api)!r}"
            raise UntrustedHostError(msg)
        path = (api if api.startswith("++") else f"++{api}").replace(" ", "%20")
        if any(seg == ".." for seg in path.split("?", 1)[0].split("/")):
            msg = f"refusing to request traversing path: {redact(path)!r}"
            raise UntrustedHostError(msg)
        url = urljoin(self.base_url, path)
        if urlsplit(url).netloc.lower() != self._base_netloc:
            msg = f"refusing to request off-server URL for {redact(api)!r}"
            raise UntrustedHostError(msg)
        return url

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"auth": self._auth}
        if extra:
            params.update(extra)
        return params

    def event_stream_url(self) -> str:
        """Absolute ++eventStream URL, without credentials.

        Pair it with :meth:`auth_params`; used by :class:`~.events.EventStream`.
        """
        return self._url("++eventStream")

    def auth_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Query parameters carrying the auth blob. The result is a secret."""
        return self._params(extra)

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self.timeout)

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            msg = "client session is not open; await open() first"
            raise RequestError(msg)
        return self._session

    async def open(self) -> None:
        """Create a session if needed and refresh ++systemInfo."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self.verify_ssl)
            self._session = aiohttp.ClientSession(connector=connector)
            self._owns_session = True
        await self.refresh()

    async def close(self) -> None:
        """Stop the event stream and close the session if we created it."""
        await self.events.stop()
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> Self:
        """Open the client for use in ``async with``."""
        await self.open()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Close the client on ``async with`` exit."""
        await self.close()

    async def _request(
        self,
        api: str,
        params: dict[str, Any] | None = None,
        *,
        expect_ok: bool = False,
        max_bytes: int = MAX_TEXT_RESPONSE_BYTES,
    ) -> bytes:
        session = self._require_session()
        url = self._url(api)
        try:
            async with session.get(
                url,
                params=self._params(params),
                timeout=self._timeout(),
                ssl=self.verify_ssl,
            ) as resp:
                return await self._read_response(
                    api, resp, expect_ok=expect_ok, max_bytes=max_bytes
                )
        except (AuthenticationError, UnsupportedError, RequestError):
            raise
        except TimeoutError as err:
            msg = f"{api} timed out after {self.timeout}s"
            raise RequestError(msg) from err
        except aiohttp.ClientError as err:
            msg = f"{api} transport error: {redact(str(err))}"
            raise RequestError(msg) from err

    async def _read_response(
        self,
        label: str,
        resp: aiohttp.ClientResponse,
        *,
        expect_ok: bool = False,
        max_bytes: int = MAX_TEXT_RESPONSE_BYTES,
    ) -> bytes:
        label = redact(label)
        body = await self._read_capped(label, resp, max_bytes)
        if resp.status in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            msg = f"authentication failed for {label}: HTTP {resp.status}"
            raise AuthenticationError(msg)
        if resp.status == HTTPStatus.NOT_FOUND and expect_ok:
            msg = f"{label} returned 404"
            raise UnsupportedError(msg)
        if resp.status >= HTTPStatus.BAD_REQUEST:
            # Error pages often echo the request, credentials included.
            preview = redact(body[:200].decode("utf-8", errors="replace"))
            msg = f"{label} failed: HTTP {resp.status} {preview!r}"
            raise RequestError(msg)
        if expect_ok:
            text = body.decode("utf-8", errors="replace").strip()
            compact = "".join(text.split())
            if not (text.endswith("OK") or '"result":"OK"' in compact):
                msg = f"{label} unexpected response: {redact(text[:200])!r}"
                raise RequestError(msg)
        return body

    @staticmethod
    async def _read_capped(label: str, resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
        declared = resp.content_length
        if declared is not None and declared > max_bytes:
            msg = f"{label} response is {declared} bytes, over the {max_bytes} byte limit"
            raise ResponseTooLargeError(msg)
        body = await resp.content.read(max_bytes + 1)
        if len(body) > max_bytes:
            msg = f"{label} response exceeded the {max_bytes} byte limit"
            raise ResponseTooLargeError(msg)
        return body

    async def _request_text(self, api: str, params: dict[str, Any] | None = None) -> str:
        return (await self._request(api, params)).decode("utf-8", errors="replace")

    async def _request_ok(self, api: str, params: dict[str, Any] | None = None) -> None:
        await self._request(api, params, expect_ok=True)

    async def refresh(self) -> ServerInfo:
        """Fetch and parse ++systemInfo.

        This replaces :attr:`info` and every :class:`~.models.Camera` in
        :attr:`cameras`, so references taken before the call go stale. Runtime
        state that only the event stream knows about (motion, object
        classification scores) is copied onto the new camera objects rather
        than reset, so re-reading :attr:`cameras` after a refresh still shows
        what the stream has observed.
        """
        xml_text = await self._request_text("++systemInfo", {"format": "xml"})
        info = parse_system_info(xml_text)
        if self.info is not None:
            for number, camera in info.cameras.items():
                previous = self.info.cameras.get(number)
                if previous is not None:
                    camera.copy_runtime_state(previous)
        self.info = info
        return info

    def camera(self, number: int) -> Camera:
        """Return camera by number or raise KeyError."""
        cams = self.cameras
        if number not in cams:
            msg = f"camera {number} not found"
            raise KeyError(msg)
        return cams[number]

    async def toggle_motion(self, camera_num: int, arm: bool) -> None:  # noqa: FBT001 - mirrors the arm=0/1 wire API
        """Arm/disarm motion capture."""
        await self._request_ok(
            "++ssControlMotionCapture",
            {"cameraNum": camera_num, "arm": "1" if arm else "0"},
        )
        if camera_num in self.cameras:
            self.cameras[camera_num].mode_m = "armed" if arm else "disarmed"

    async def toggle_actions(self, camera_num: int, arm: bool) -> None:  # noqa: FBT001 - mirrors the arm=0/1 wire API
        """Arm/disarm actions."""
        await self._request_ok(
            "++ssControlActions",
            {"cameraNum": camera_num, "arm": "1" if arm else "0"},
        )
        if camera_num in self.cameras:
            self.cameras[camera_num].mode_a = "armed" if arm else "disarmed"

    async def toggle_continuous(self, camera_num: int, arm: bool) -> None:  # noqa: FBT001 - mirrors the arm=0/1 wire API
        """Arm/disarm continuous capture.

        Many SecuritySpy builds do not ship ``++ssControlContinuous`` and answer
        404. Only that case falls back to the schedule API; transport failures
        and rejected commands propagate so a broken network never silently
        rewrites a camera's schedule.
        """
        try:
            await self._request_ok(
                "++ssControlContinuous",
                {"cameraNum": camera_num, "arm": "1" if arm else "0"},
            )
        except UnsupportedError as err:
            if self.info is None:
                msg = "continuous toggle unsupported and no schedule list is loaded"
                raise UnsupportedError(msg) from err
            await self.set_schedule(
                camera_num, CameraMode.CONTINUOUS, self._always_schedule_id(arm=arm)
            )
        if camera_num in self.cameras:
            self.cameras[camera_num].mode_c = "armed" if arm else "disarmed"

    def _always_schedule_id(self, *, arm: bool) -> int:
        """Find the "Armed 24/7" or "Disarmed 24/7" schedule id.

        v5 names the disarmed schedule "Unarmed 24/7" and v6 calls it
        "Disarmed 24/7"; both conventionally use ids 1 and 0.
        """
        schedules = self.info.schedules if self.info else {}
        wanted: tuple[str, ...] = ("armed 24",) if arm else ("disarmed 24", "unarmed 24")
        fallback = 1 if arm else 0
        for sid, name in sorted(schedules.items()):
            lowered = name.lower()
            if any(lowered.startswith(prefix) for prefix in wanted):
                return sid
        return fallback

    async def trigger_motion(self, camera_num: int) -> None:
        """Manually trigger motion detection."""
        await self._request_ok("++triggermd", {"cameraNum": camera_num})

    async def set_schedule(self, camera_num: int, mode: CameraMode | str, schedule_id: int) -> None:
        """Set camera schedule for mode C/M/A/X."""
        await self._request_ok(
            "++ssSetSchedule",
            {
                "cameraNum": camera_num,
                "mode": str(CameraMode(mode)),
                "id": schedule_id,
            },
        )

    async def set_schedule_override(
        self, camera_num: int, mode: CameraMode | str, override_id: int
    ) -> None:
        """Set camera schedule override for mode C/M/A/X."""
        await self._request_ok(
            "++ssSetOverride",
            {
                "cameraNum": camera_num,
                "mode": str(CameraMode(mode)),
                "id": override_id,
            },
        )

    async def set_schedule_preset(self, preset_id: int) -> None:
        """Activate a server-wide schedule preset."""
        await self._request_ok("++ssSetPreset", {"id": preset_id})

    async def camera_modes(self, camera_num: int) -> str:
        """Return raw ++cameramodes text."""
        return await self._request_text("++cameramodes", {"cameraNum": camera_num})

    def _image_params(
        self, camera_num: int, width: int | None, height: int | None, quality: int | None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"cameraNum": camera_num}
        for name, value in (("width", width), ("height", height)):
            if value is None:
                continue
            if not 0 < value <= MAX_IMAGE_DIMENSION:
                msg = f"{name} must be 1-{MAX_IMAGE_DIMENSION}, got {value}"
                raise ValueError(msg)
            params[name] = value
        if quality is not None:
            if not MIN_QUALITY <= quality <= MAX_QUALITY:
                msg = f"quality must be {MIN_QUALITY}-{MAX_QUALITY}, got {quality}"
                raise ValueError(msg)
            params["quality"] = quality
        return params

    async def get_image(
        self,
        camera_num: int,
        *,
        width: int | None = None,
        height: int | None = None,
        quality: int | None = None,
    ) -> bytes:
        """Download a JPEG snapshot."""
        return await self._request(
            "++image",
            self._image_params(camera_num, width, height, quality),
            max_bytes=MAX_IMAGE_BYTES,
        )

    def image_url(
        self,
        camera_num: int,
        *,
        width: int | None = None,
        height: int | None = None,
        quality: int | None = 80,
    ) -> str:
        """Build an authenticated ++image URL.

        The returned URL embeds the ``auth`` credential blob. Treat it as a
        secret and prefer :meth:`get_image` unless an external consumer needs
        the URL itself.
        """
        params = self._image_params(camera_num, width, height, quality)
        params["auth"] = self._auth
        return f"{self._url('++image')}?{urlencode(params)}"

    def rtsp_url(self, camera_num: int, *, use_ssl: bool | None = None) -> str:
        """Build an RTSP stream URL with userinfo auth.

        SecuritySpy rejects the ``auth`` query parameter on RTSP, so the
        credentials go in the URL userinfo in cleartext. Callers that must avoid
        embedding a password should use :meth:`mjpeg_url` or :meth:`hls_url`.

        RTSP is served by the same web server on the same port used for HTTP.
        """
        secure = self.scheme == "https" if use_ssl is None else use_ssl
        scheme = "rtsps" if secure else "rtsp"
        user = quote(self._username, safe="")
        password = quote(self._password, safe="")
        return f"{scheme}://{user}:{password}@{self.host}:{self.port}/stream?cameraNum={camera_num}"

    def mjpeg_url(self, camera_num: int) -> str:
        """Build a ++video MJPEG URL. Embeds credentials; treat as a secret."""
        params = {"cameraNum": camera_num, "auth": self._auth}
        return f"{self._url('++video')}?{urlencode(params)}"

    def hls_url(self, camera_num: int) -> str:
        """Build a ++hls URL (v6+). Embeds credentials; treat as a secret."""
        params = {"cameraNum": camera_num, "auth": self._auth}
        return f"{self._url('++hls')}?{urlencode(params)}"

    async def ptz_command(self, camera_num: int, command: int) -> None:
        """Send a PTZ command integer."""
        await self._request_ok(
            "++ptz/command",
            {"cameraNum": camera_num, "command": command},
        )

    async def ptz_left(self, camera_num: int) -> None:
        """Pan left."""
        await self.ptz_command(camera_num, PTZ_LEFT)

    async def ptz_right(self, camera_num: int) -> None:
        """Pan right."""
        await self.ptz_command(camera_num, PTZ_RIGHT)

    async def ptz_up(self, camera_num: int) -> None:
        """Tilt up."""
        await self.ptz_command(camera_num, PTZ_UP)

    async def ptz_down(self, camera_num: int) -> None:
        """Tilt down."""
        await self.ptz_command(camera_num, PTZ_DOWN)

    async def ptz_zoom(self, camera_num: int, zoom_in: bool = True) -> None:  # noqa: FBT001, FBT002
        """Zoom in or out."""
        await self.ptz_command(camera_num, PTZ_ZOOM_IN if zoom_in else PTZ_ZOOM_OUT)

    async def ptz_home(self, camera_num: int) -> None:
        """Move to the home position."""
        await self.ptz_command(camera_num, PTZ_HOME_CMD)

    async def ptz_stop(self, camera_num: int) -> None:
        """Stop continuous PTZ movement."""
        await self.ptz_command(camera_num, PTZ_STOP)

    async def ptz_preset(self, camera_num: int, preset: int) -> None:
        """Move to preset 1-8."""
        if not PTZ_PRESET_MIN <= preset <= PTZ_PRESET_MAX:
            msg = f"PTZ preset must be {PTZ_PRESET_MIN}-{PTZ_PRESET_MAX}, got {preset}"
            raise ValueError(msg)
        await self.ptz_command(camera_num, PTZ_PRESET_BASE + (preset - PTZ_PRESET_MIN))

    async def list_motion_files(
        self,
        camera_num: int | None = None,
        *,
        days: int = 1,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """List recent motion capture files via ++download.

        ``days`` counts back from today in the server's timezone, which is what
        the ``date1``/``date2`` window is expressed in.
        """
        if days < 1:
            msg = f"days must be at least 1, got {days}"
            raise ValueError(msg)
        if limit < 1:
            msg = f"limit must be at least 1, got {limit}"
            raise ValueError(msg)

        today = datetime.now(self.server_timezone).date()
        params: dict[str, Any] = {
            "format": "xml",
            "date1": (today - timedelta(days=days - 1)).strftime(DOWNLOAD_DATE_FORMAT),
            "date2": today.strftime(DOWNLOAD_DATE_FORMAT),
            "results": str(limit),
            "mcFilesCheck": "1",
        }
        if camera_num is not None:
            params["cameraNum"] = camera_num
        text = await self._request_text("++download", params)
        return _parse_file_feed(text, limit)

    async def download_file(
        self,
        href: str,
        *,
        high_bandwidth: bool = True,
        max_bytes: int | None = None,
    ) -> bytes:
        """Download a recording by the ``href`` returned from ++download.

        SecuritySpy serves recordings from ``++getfilehb/`` (original) and
        ``++getfilelb/`` (downscaled); the feed advertises the neutral
        ``++getfile/`` prefix, which this rewrites.

        The href comes from the server, so it is validated to stay on the
        configured host before the credentials are attached to it.
        """
        path = self._safe_download_path(href)
        if path.startswith("++getfile/"):
            prefix = "++getfilehb/" if high_bandwidth else "++getfilelb/"
            path = prefix + path.removeprefix("++getfile/")
        api, _, query = path.partition("?")
        params = dict(parse_qsl(query)) if query else None
        return await self._request(api, params, max_bytes=max_bytes or self.max_download_bytes)

    def _safe_download_path(self, href: str) -> str:
        """Reduce a feed href to a path on this server, or refuse it."""
        href = href.strip()
        if not href:
            msg = "empty download href"
            raise UntrustedHostError(msg)
        if href.startswith("//"):
            msg = f"refusing protocol-relative download href: {href!r}"
            raise UntrustedHostError(msg)
        if "://" in href:
            parts = urlsplit(href)
            if parts.netloc.lower() != self._base_netloc or parts.scheme != self.scheme:
                msg = (
                    f"refusing to download from {parts.scheme}://{parts.netloc}: "
                    f"not the configured server {self.base_url}"
                )
                raise UntrustedHostError(msg)
            href = parts.path + (f"?{parts.query}" if parts.query else "")
        path = href.lstrip("/")
        if not path.startswith("++"):
            path = "++" + path
        # _url() re-validates the result stays on this server.
        self._url(path)
        return path

    async def download_latest_motion_recording(
        self, camera_num: int, *, high_bandwidth: bool = True
    ) -> bytes:
        """Download the newest motion recording for a camera."""
        files = await self.list_motion_files(camera_num, days=7, limit=5)
        if not files:
            msg = f"no motion recordings found for camera {camera_num}"
            raise RequestError(msg)
        return await self.download_file(files[0]["href"], high_bandwidth=high_bandwidth)


def _parse_file_feed(xml_text: str, limit: int) -> list[dict[str, str]]:
    """Pull ``{title, href}`` pairs out of a ++download feed."""
    root = parse_xml(xml_text, label="++download")
    items = root.findall(".//item") or root.findall(".//entry") or root.findall(".//file")
    results: list[dict[str, str]] = []
    for item in items:
        title = item.findtext("title") or item.findtext("name") or ""
        link = item.find("link")
        href = ""
        if link is not None:
            href = link.attrib.get("href") or (link.text or "")
        if not href:
            href = item.findtext("href") or item.attrib.get("href") or ""
        if href:
            results.append({"title": title, "href": href})
    if not results:
        for el in root.iter():
            href = el.attrib.get("href") or ""
            if href and ("getfile" in href or href.startswith("++")):
                title = el.findtext("title") or el.findtext("name") or el.tag
                results.append({"title": title, "href": href})
    return results[:limit]
