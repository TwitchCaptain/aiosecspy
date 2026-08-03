"""Event stream parsing and watcher."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp

from .const import (
    CLASSIFY_ABSENT,
    EVENT_MAX_LINE_BYTES,
    EVENT_READ_TIMEOUT,
    EVENT_RECONNECT_DELAY,
    EVENT_RECONNECT_MAX_DELAY,
    EVENT_TIME_FORMAT,
    KNOWN_EVENT_TYPES,
    TRIGGER_REASON_NAMES,
    EventType,
    TriggerReason,
)
from .exceptions import AuthenticationError, RequestError
from .util import redact

if TYPE_CHECKING:
    from .client import SecSpyClient

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[["Event"], Awaitable[None] | None]

# Header is "<timestamp> <id> <camera> <message...>".
_EVENT_HEADER_FIELDS = 4
_TRIGGER_TOKENS = 2

# Synthetic event ids, so consumers can tell library events from server events.
EVENT_ID_CONNECTED = -9999
EVENT_ID_REFRESH = -9998
EVENT_ID_REFRESH_FAIL = -9997
EVENT_ID_DISCONNECTED = -10000
EVENT_ID_AUTH_FAIL = -10001


@dataclass
class Event:
    """A parsed SecuritySpy event-stream line."""

    event_type: EventType
    event_id: int = -1
    camera_number: int | None = None
    when: datetime | None = None
    msg: str = ""
    raw: str = ""
    reasons: list[TriggerReason] = field(default_factory=list)
    reason_names: list[str] = field(default_factory=list)
    classify_human: int = CLASSIFY_ABSENT
    classify_vehicle: int = CLASSIFY_ABSENT
    classify_animal: int = CLASSIFY_ABSENT
    errors: list[str] = field(default_factory=list)


def parse_event_line(
    text: str,
    gmt_offset_hours: float = 0.0,
    *,
    major_version: int = 6,
) -> Event:
    """Parse one CR-delimited event stream line.

    SecuritySpy stamps events in its own local time. ``gmt_offset_hours`` (from
    ``++systemInfo``) is used to return an aware datetime, so consumers never
    have to guess which clock ``when`` belongs to.
    """
    tzinfo = timezone(timedelta(hours=gmt_offset_hours))
    text = text.strip()
    parts = text.split(" ", _EVENT_HEADER_FIELDS - 1)
    if len(parts) < _EVENT_HEADER_FIELDS:
        return Event(
            event_type=EventType.UNKNOWN,
            raw=text,
            msg=text,
            errors=["unknown_event"],
        )

    stamp, eid_s, cam_s, msg = parts
    event = Event(event_type=EventType.UNKNOWN, raw=text, msg=msg)

    try:
        event.when = datetime.strptime(stamp, EVENT_TIME_FORMAT).replace(tzinfo=tzinfo)
    except ValueError:
        event.when = datetime.now(tzinfo)
        event.errors.append("date_parse_fail")

    try:
        event.event_id = int(eid_s)
    except ValueError:
        event.event_id = -2
        event.errors.append("id_parse_fail")

    cam_s = cam_s.removeprefix("CAM")
    # "X" is the server-wide camera placeholder used by keepalives.
    if cam_s != "X":
        try:
            event.camera_number = int(cam_s)
        except ValueError:
            event.errors.append("cam_parse_fail")

    tokens = msg.split()
    event.event_type = _wire_event_type(tokens[0] if tokens else "", event)

    if event.event_type is EventType.CLASSIFY and len(tokens) > 1:
        _parse_classify(tokens[1:], event)

    if (
        event.event_type in {EventType.TRIGGER_M, EventType.TRIGGER_A}
        and len(tokens) == _TRIGGER_TOKENS
    ):
        _parse_trigger_reasons(tokens[1], event, major_version)

    return event


def _wire_event_type(type_s: str, event: Event) -> EventType:
    """Map a wire event-name token to a known EventType, else UNKNOWN."""
    try:
        et = EventType(type_s)
    except ValueError:
        event.errors.append("unknown_event")
        return EventType.UNKNOWN
    # Reject library-only values (CONNECTED, AUTHFAIL, …) that happen to
    # parse as EventType but are not on the wire.
    if et not in KNOWN_EVENT_TYPES:
        event.errors.append("unknown_event")
        return EventType.UNKNOWN
    return et


def _parse_classify(parts: list[str], event: Event) -> None:
    for idx in range(0, len(parts) - 1, 2):
        label = parts[idx].upper()
        try:
            value = int(parts[idx + 1])
        except ValueError:
            continue
        if label == "HUMAN":
            event.classify_human = value
        elif label == "VEHICLE":
            event.classify_vehicle = value
        elif label == "ANIMAL":
            event.classify_animal = value


def _parse_trigger_reasons(token: str, event: Event, major_version: int) -> None:
    try:
        bitmask = int(token)
    except ValueError:
        event.errors.append("reason_parse_fail")
        return

    # v5 stops the table at Animal, so its bit 512 means Animal. v6 inserted
    # HomeKit at 512 and moved Animal to 1024.
    legacy = major_version < 6  # noqa: PLR2004 - version 6 renumbered the bitmask
    for flag in TriggerReason:
        if not bitmask & int(flag):
            continue
        if legacy and flag is TriggerReason.ANIMAL:
            continue
        if legacy and flag is TriggerReason.HOMEKIT:
            flag = TriggerReason.ANIMAL  # noqa: PLW2901
        event.reasons.append(flag)
        event.reason_names.append(TRIGGER_REASON_NAMES.get(flag, flag.name))


class EventStream:
    """Long-lived ++eventStream reader with reconnect."""

    def __init__(self, client: SecSpyClient) -> None:
        """Bind a stream reader to a client; nothing runs until :meth:`start`."""
        self._client = client
        self._callbacks: list[EventCallback] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._refresh_on_config_change = True
        self.running = False

    def add_listener(self, callback: EventCallback) -> Callable[[], None]:
        """Register an async/sync callback; returns an unsubscribe callable."""
        self._callbacks.append(callback)

        def _unsub() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return _unsub

    def start(
        self,
        *,
        reconnect_delay: float = EVENT_RECONNECT_DELAY,
        refresh_on_config_change: bool = True,
        read_timeout: float | None = EVENT_READ_TIMEOUT,
    ) -> None:
        """Start the background watcher task.

        ``read_timeout`` bounds the silence between event lines. SecuritySpy
        sends NULL keepalives, so a longer gap means the connection is dead even
        if TCP has not noticed yet.
        """
        if self._task and not self._task.done():
            return
        if self._client.session is None:
            msg = "client session is not open; await open() before events.start()"
            raise RequestError(msg)
        self._refresh_on_config_change = refresh_on_config_change
        self._stop.clear()
        self.running = True
        self._task = asyncio.create_task(
            self._watch_loop(reconnect_delay, read_timeout),
            name="aiosecspy-eventstream",
        )

    async def stop(self) -> None:
        """Stop the watcher and wait for it to exit."""
        self._stop.set()
        self.running = False
        task, self._task = self._task, None
        if task is None or task is asyncio.current_task():
            # Called from inside a listener: the flag above is enough, and
            # cancelling ourselves here would just raise into the callback.
            return
        task.cancel()
        # return_exceptions keeps the expected CancelledError, and anything the
        # watcher died of, from escaping what is a cleanup call.
        await asyncio.gather(task, return_exceptions=True)

    async def _emit(self, event: Event) -> None:
        for callback in list(self._callbacks):
            try:
                result = callback(event)
                if isinstance(result, Awaitable):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Event listener failed for %s", event.event_type)

    async def _emit_synthetic(self, event_type: EventType, msg: str, event_id: int) -> None:
        await self._emit(Event(event_type=event_type, msg=redact(msg), event_id=event_id))

    async def _watch_loop(self, reconnect_delay: float, read_timeout: float | None) -> None:
        delay = reconnect_delay
        while not self._stop.is_set():
            try:
                await self._run_once(read_timeout)
            except asyncio.CancelledError:
                raise
            except AuthenticationError as err:
                # Credentials will not fix themselves; stop instead of hammering
                # the server with requests that can trip lockout protections.
                _LOGGER.error("Event stream authentication failed: %s", err)  # noqa: TRY400 - a traceback adds nothing here
                await self._emit_synthetic(EventType.AUTHFAIL, str(err), EVENT_ID_AUTH_FAIL)
                await self._emit_synthetic(EventType.DISCONNECTED, str(err), EVENT_ID_DISCONNECTED)
                self.running = False
                return
            except Exception as err:  # noqa: BLE001 - the watcher must never die
                _LOGGER.warning("Event stream error: %s", redact(str(err)))
                await self._emit_synthetic(EventType.DISCONNECTED, str(err), EVENT_ID_DISCONNECTED)
            else:
                delay = reconnect_delay
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_jitter(delay))
            except TimeoutError:
                delay = min(delay * 2, EVENT_RECONNECT_MAX_DELAY)
        self.running = False

    async def _run_once(self, read_timeout: float | None) -> None:
        client = self._client
        session = client.session
        if session is None or session.closed:
            msg = "client session is not open"
            raise RequestError(msg)

        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=client.timeout, sock_read=read_timeout
        )
        async with session.get(
            client.event_stream_url(),
            params=client.auth_params({"version": "3"}),
            timeout=timeout,
            ssl=client.verify_ssl,
        ) as resp:
            if resp.status in {401, 403}:
                msg = f"event stream auth failed: HTTP {resp.status}"
                raise AuthenticationError(msg)
            resp.raise_for_status()
            await self._emit_synthetic(
                EventType.CONNECTED, "Event Stream Connected", EVENT_ID_CONNECTED
            )
            await self._read_stream(resp)

    async def _read_stream(self, resp: aiohttp.ClientResponse) -> None:
        buffer = bytearray()
        async for chunk in resp.content.iter_any():
            if self._stop.is_set():
                return
            buffer.extend(chunk)
            # Drain complete lines before applying the size cap so a large
            # chunk that contains many CR-terminated events is not mistaken
            # for a single unbounded line.
            while (idx := buffer.find(b"\r")) >= 0:
                line = bytes(buffer[:idx]).decode("utf-8", errors="replace")
                del buffer[: idx + 1]
                await self._handle_line(line)
                if self._stop.is_set():
                    return
            if len(buffer) > EVENT_MAX_LINE_BYTES:
                msg = (
                    f"event stream sent {len(buffer)} bytes without a line break; "
                    "dropping the connection"
                )
                raise RequestError(msg)

    async def _handle_line(self, line: str) -> None:
        # Header alone is three spaces; anything shorter is a partial line.
        if line.count(" ") < _EVENT_HEADER_FIELDS - 1:
            return
        info = self._client.info
        gmt_hours = info.gmt_offset_seconds / 3600.0 if info else 0.0
        major = info.major_version if info else 6
        event = parse_event_line(line, gmt_hours, major_version=major)
        await self._emit(event)
        if event.event_type is EventType.CONFIGCHANGE and self._refresh_on_config_change:
            await self._refresh_client()

    async def _refresh_client(self) -> None:
        try:
            await self._client.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - reported to listeners instead
            await self._emit_synthetic(EventType.REFRESHFAIL, str(err), EVENT_ID_REFRESH_FAIL)
        else:
            await self._emit_synthetic(
                EventType.REFRESH, "SystemInfo Refresh Success", EVENT_ID_REFRESH
            )


def _jitter(delay: float) -> float:
    """Spread reconnects so many clients do not retry in lockstep."""
    return delay * random.uniform(0.5, 1.0)  # noqa: S311 - not a security decision
