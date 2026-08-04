"""Event stream parsing and watcher."""

from __future__ import annotations

import asyncio
import contextlib
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
    EVENT_QUEUE_MAXSIZE,
    EVENT_QUEUE_PUT_TIMEOUT,
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
    from .models import Camera

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


# Camera mode fields flipped by arm/disarm wire events.
_ARM_EVENT_FIELDS: dict[EventType, tuple[str, str]] = {
    EventType.ARM_C: ("mode_c", "armed"),
    EventType.DISARM_C: ("mode_c", "disarmed"),
    EventType.ARM_M: ("mode_m", "armed"),
    EventType.DISARM_M: ("mode_m", "disarmed"),
    EventType.ARM_A: ("mode_a", "armed"),
    EventType.DISARM_A: ("mode_a", "disarmed"),
}


class EventStream:
    """Long-lived ++eventStream reader with reconnect.

    The reader task applies every event to the client's :class:`~.models.Camera`
    objects the moment it is parsed, then hands it to a dispatcher task that
    runs the registered listeners. A slow listener delays other listeners but
    never the stream reads or the camera state.
    """

    def __init__(self, client: SecSpyClient) -> None:
        """Bind a stream reader to a client; nothing runs until :meth:`start`."""
        self._client = client
        self._callbacks: list[EventCallback] = []
        self._task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
        self._queue_degraded = False
        self._resp: aiohttp.ClientResponse | None = None
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
        """Start the background watcher and dispatcher tasks.

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
        self._queue = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
        self._queue_degraded = False
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="aiosecspy-dispatch")
        self._task = asyncio.create_task(
            self._watch_loop(reconnect_delay, read_timeout),
            name="aiosecspy-eventstream",
        )

    async def stop(self) -> None:
        """Stop the watcher and dispatcher and wait for them to exit.

        Safe to call from inside a listener: the dispatcher (which runs the
        listeners) is signalled instead of cancelled, and the in-flight stream
        read is aborted by closing the response rather than waiting out the
        next keepalive or read timeout.
        """
        self._stop.set()
        self.running = False
        if self._resp is not None:
            self._resp.close()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        current = asyncio.current_task()
        tasks: list[asyncio.Task[None]] = []
        for attr in ("_task", "_dispatch_task"):
            task: asyncio.Task[None] | None = getattr(self, attr)
            setattr(self, attr, None)
            if task is None or task is current:
                continue
            task.cancel()
            tasks.append(task)
        # return_exceptions keeps the expected CancelledError, and anything the
        # tasks died of, from escaping what is a cleanup call.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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

    async def _enqueue(self, event: Event | None) -> None:
        """Queue an event for the dispatcher without ever stalling reads.

        A full queue normally just means a burst outran the listeners, so the
        reader waits briefly for the dispatcher to catch up. If it cannot, a
        listener is stuck: switch to dropping the oldest events (degraded mode)
        until the dispatcher makes the queue healthy again.
        """
        if not self._queue_degraded:
            try:
                await asyncio.wait_for(self._queue.put(event), timeout=EVENT_QUEUE_PUT_TIMEOUT)
            except TimeoutError:
                self._queue_degraded = True
                _LOGGER.warning("Event listeners are not keeping up; dropping oldest events")
            else:
                return
        elif not self._queue.full():
            self._queue_degraded = False
            _LOGGER.info("Event listeners caught up; resuming normal delivery")
            self._queue.put_nowait(event)
            return
        with contextlib.suppress(asyncio.QueueEmpty):
            self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(event)

    async def _queue_synthetic(self, event_type: EventType, msg: str, event_id: int) -> None:
        await self._enqueue(Event(event_type=event_type, msg=redact(msg), event_id=event_id))

    async def _dispatch_loop(self) -> None:
        """Run listeners for queued events; also owns CONFIGCHANGE refreshes."""
        while True:
            event = await self._queue.get()
            if event is None or self._stop.is_set():
                return
            await self._emit(event)
            if self._stop.is_set():
                return
            if event.event_type is EventType.CONFIGCHANGE and self._refresh_on_config_change:
                await self._refresh_client()

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
                await self._queue_synthetic(EventType.AUTHFAIL, str(err), EVENT_ID_AUTH_FAIL)
                await self._queue_synthetic(EventType.DISCONNECTED, str(err), EVENT_ID_DISCONNECTED)
                # Sentinel ends the dispatcher once it has delivered the events
                # above; without it the task would wait on this queue forever.
                await self._enqueue(None)
                self.running = False
                return
            except Exception as err:  # noqa: BLE001 - the watcher must never die
                _LOGGER.warning("Event stream error: %s", redact(str(err)))
                await self._queue_synthetic(EventType.DISCONNECTED, str(err), EVENT_ID_DISCONNECTED)
            else:
                delay = reconnect_delay
                if not self._stop.is_set():
                    # A clean EOF still means the stream is down until the
                    # reconnect succeeds; consumers tracking connectivity need
                    # to hear about the gap.
                    await self._queue_synthetic(
                        EventType.DISCONNECTED, "Event Stream Ended", EVENT_ID_DISCONNECTED
                    )
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
            # Held so stop() can abort a read blocked between keepalives.
            self._resp = resp
            try:
                if resp.status in {401, 403}:
                    msg = f"event stream auth failed: HTTP {resp.status}"
                    raise AuthenticationError(msg)
                resp.raise_for_status()
                await self._queue_synthetic(
                    EventType.CONNECTED, "Event Stream Connected", EVENT_ID_CONNECTED
                )
                await self._read_stream(resp)
            finally:
                self._resp = None

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
        self._apply_event(event)
        await self._enqueue(event)

    def _apply_event(self, event: Event) -> None:
        """Fold a wire event into the client's Camera runtime state.

        ++systemInfo does not report motion or classification, so the stream is
        the only source for those fields. Applying them here, before listeners
        run, keeps ``client.cameras`` current even with no listener registered.
        """
        cam = (
            self._client.cameras.get(event.camera_number)
            if event.camera_number is not None
            else None
        )
        if cam is None:
            return
        et = event.event_type
        if et in _ARM_EVENT_FIELDS:
            attr, value = _ARM_EVENT_FIELDS[et]
            setattr(cam, attr, value)
        elif et in {EventType.MOTION, EventType.TRIGGER_M}:
            cam.motion_active = True
            if event.when:
                cam.last_motion_time = event.when
            cam.trigger_reasons = list(event.reason_names)
        elif et is EventType.MOTION_END:
            cam.motion_active = False
        elif et in {EventType.ONLINE, EventType.OFFLINE}:
            cam.connected = et is EventType.ONLINE
        elif et is EventType.CLASSIFY:
            self._apply_classify(cam, event)

    @staticmethod
    def _apply_classify(cam: Camera, event: Event) -> None:
        # Every CLASSIFY overwrites all three scores (absent classes arrive as
        # CLASSIFY_ABSENT), so a stale detection never lingers. event_object is
        # the top class actually present in this event, or None.
        cam.score_human = event.classify_human
        cam.score_vehicle = event.classify_vehicle
        cam.score_animal = event.classify_animal
        present = [
            (label, value)
            for label, value in (
                ("human", event.classify_human),
                ("vehicle", event.classify_vehicle),
                ("animal", event.classify_animal),
            )
            if value >= 0
        ]
        cam.event_object = max(present, key=lambda item: item[1])[0] if present else None
        if event.when:
            cam.last_motion_time = event.when

    async def _refresh_client(self) -> None:
        try:
            await self._client.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - reported to listeners instead
            await self._emit(
                Event(
                    event_type=EventType.REFRESHFAIL,
                    msg=redact(str(err)),
                    event_id=EVENT_ID_REFRESH_FAIL,
                )
            )
        else:
            await self._emit(
                Event(
                    event_type=EventType.REFRESH,
                    msg="SystemInfo Refresh Success",
                    event_id=EVENT_ID_REFRESH,
                )
            )


def _jitter(delay: float) -> float:
    """Spread reconnects so many clients do not retry in lockstep."""
    return delay * random.uniform(0.5, 1.0)  # noqa: S311 - not a security decision
