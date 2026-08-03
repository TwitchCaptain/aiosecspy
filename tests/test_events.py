"""Event line parsing and the long-lived event stream watcher."""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta, timezone

import pytest
from aiohttp import web

from aiosecspy import Event, EventType, SecSpyClient, TriggerReason, parse_event_line
from aiosecspy.const import EVENT_MAX_LINE_BYTES
from aiosecspy.exceptions import RequestError


async def collect(stream, count, deadline=5.0):
    """Wait for ``count`` events from a stream and return them."""
    received: list[Event] = []
    done = asyncio.Event()

    def listener(event: Event) -> None:
        received.append(event)
        if len(received) >= count:
            done.set()

    unsub = stream.add_listener(listener)
    try:
        async with asyncio.timeout(deadline):
            await done.wait()
    finally:
        unsub()
    return received


def stream_handler(chunks, *, status=200, delay=0.0):
    """Build a handler that writes raw bytes to the event stream."""

    async def handler(request: web.Request) -> web.StreamResponse:
        if status != 200:
            return web.Response(status=status, text="nope")
        resp = web.StreamResponse()
        await resp.prepare(request)
        for chunk in chunks:
            await resp.write(chunk)
            if delay:
                await asyncio.sleep(delay)
        await asyncio.sleep(delay or 0.05)
        return resp

    return handler


class TestParseEventLine:
    @pytest.mark.parametrize(
        ("line", "etype", "cam"),
        [
            ("20190927092026 3 3 CLASSIFY HUMAN 99", EventType.CLASSIFY, 3),
            ("20190927092026 4 3 TRIGGER_M 9", EventType.TRIGGER_M, 3),
            ("20190927092040 5 X NULL", EventType.NULL, None),
            ("20190927092055 7 3 DISARM_M", EventType.DISARM_M, 3),
            ("20190927092056 8 3 OFFLINE", EventType.OFFLINE, 3),
            ("20190927092036 5 3 MOTION_END", EventType.MOTION_END, 3),
            ("20190927092036 5 CAM3 MOTION", EventType.MOTION, 3),
        ],
    )
    def test_recognized_lines(self, line, etype, cam):
        event = parse_event_line(line)
        assert event.event_type == etype
        assert event.camera_number == cam
        assert not event.errors

    def test_classify_scores(self):
        event = parse_event_line("20190927092036 5 3 CLASSIFY HUMAN 5 VEHICLE 95 ANIMAL 12")
        assert event.classify_human == 5
        assert event.classify_vehicle == 95
        assert event.classify_animal == 12

    def test_trigger_reasons(self):
        # bitmask 9 = motion(1) + camera_event(8)
        event = parse_event_line("20190927092026 4 3 TRIGGER_M 9")
        assert event.reasons == [TriggerReason.MOTION, TriggerReason.CAMERA_EVENT]
        assert "Motion Detected" in event.reason_names

    def test_v5_reads_bit_512_as_animal(self):
        event = parse_event_line("20190927092026 4 3 TRIGGER_M 512", major_version=5)
        assert event.reasons == [TriggerReason.ANIMAL]
        assert event.reason_names == ["Animal Detected"]

    def test_v6_reads_bit_512_as_homekit(self):
        event = parse_event_line("20190927092026 4 3 TRIGGER_M 512", major_version=6)
        assert event.reasons == [TriggerReason.HOMEKIT]

    def test_v5_never_reports_animal_twice(self):
        event = parse_event_line("20190927092026 4 3 TRIGGER_M 1536", major_version=5)
        assert event.reasons == [TriggerReason.ANIMAL]

    def test_timestamp_is_aware_and_uses_the_server_offset(self):
        event = parse_event_line("20190927092026 3 3 MOTION", gmt_offset_hours=-7.0)
        assert event.when is not None
        assert event.when.tzinfo == timezone(timedelta(hours=-7))
        assert event.when.hour == 9

    def test_timestamp_defaults_to_utc(self):
        event = parse_event_line("20190927092026 3 3 MOTION")
        assert event.when is not None
        assert event.when.utcoffset() == UTC.utcoffset(None)

    def test_short_line_is_flagged_not_raised(self):
        event = parse_event_line("garbage")
        assert event.event_type == EventType.UNKNOWN
        assert event.errors == ["unknown_event"]

    def test_unparsable_fields_are_reported(self):
        event = parse_event_line("notadate notanid notacam WAT")
        assert event.event_type == EventType.UNKNOWN
        assert set(event.errors) == {
            "date_parse_fail",
            "id_parse_fail",
            "cam_parse_fail",
            "unknown_event",
        }
        assert event.when is not None

    def test_unparsable_trigger_bitmask_is_reported(self):
        event = parse_event_line("20190927092026 4 3 TRIGGER_M xyz")
        assert event.reasons == []
        assert event.errors == ["reason_parse_fail"]

    def test_synthetic_types_are_not_accepted_from_the_wire(self):
        event = parse_event_line("20190927092026 4 3 AUTHFAIL")
        assert event.event_type == EventType.UNKNOWN


class TestEventStream:
    async def test_start_requires_an_open_session(self):
        client = SecSpyClient("cam.example", 8000, "user", "s3cret")
        with pytest.raises(RequestError, match="not open"):
            client.events.start()

    async def test_emits_connected_then_parsed_events(self, open_client, fake_server):
        fake_server.route(
            "/++eventStream",
            stream_handler([b"20190927092026 3 3 MOTION\r20190927092036 4 3 MOTION_END\r"]),
        )
        open_client.events.start()
        events = await collect(open_client.events, 3)
        await open_client.events.stop()

        assert [e.event_type for e in events] == [
            EventType.CONNECTED,
            EventType.MOTION,
            EventType.MOTION_END,
        ]
        assert fake_server.query_for("/++eventStream")["version"] == "3"

    async def test_reassembles_lines_split_across_chunks(self, open_client, fake_server):
        fake_server.route(
            "/++eventStream",
            stream_handler([b"20190927092026 3 3 MO", b"TION\r"], delay=0.01),
        )
        open_client.events.start()
        events = await collect(open_client.events, 2)
        await open_client.events.stop()

        assert events[1].event_type == EventType.MOTION

    async def test_partial_line_without_a_full_header_is_ignored(self, open_client, fake_server):
        fake_server.route(
            "/++eventStream",
            stream_handler([b"junk\r20190927092026 3 3 MOTION\r"]),
        )
        open_client.events.start()
        events = await collect(open_client.events, 2)
        await open_client.events.stop()

        assert [e.event_type for e in events] == [EventType.CONNECTED, EventType.MOTION]

    async def test_auth_failure_stops_the_stream_and_reports_it(self, open_client, fake_server):
        fake_server.route("/++eventStream", stream_handler([], status=401))
        open_client.events.start()
        events = await collect(open_client.events, 2)
        await open_client.events.stop()

        assert [e.event_type for e in events] == [
            EventType.AUTHFAIL,
            EventType.DISCONNECTED,
        ]
        assert open_client.events.running is False

    async def test_a_flood_without_line_breaks_drops_the_connection(self, open_client, fake_server):
        fake_server.route(
            "/++eventStream",
            stream_handler([b"x" * (EVENT_MAX_LINE_BYTES + 1024)]),
        )
        open_client.events.start(reconnect_delay=30.0)
        events = await collect(open_client.events, 2)
        await open_client.events.stop()

        assert events[-1].event_type == EventType.DISCONNECTED
        assert "without a line break" in events[-1].msg

    async def test_config_change_triggers_a_refresh(self, open_client, fake_server):
        fake_server.route(
            "/++eventStream",
            stream_handler([b"20190927092026 3 X CONFIGCHANGE\r"]),
        )
        open_client.events.start()
        events = await collect(open_client.events, 3)
        await open_client.events.stop()

        assert [e.event_type for e in events] == [
            EventType.CONNECTED,
            EventType.CONFIGCHANGE,
            EventType.REFRESH,
        ]

    async def test_a_failing_listener_does_not_stop_the_others(self, open_client, fake_server):
        fake_server.route(
            "/++eventStream",
            stream_handler([b"20190927092026 3 3 MOTION\r"]),
        )
        seen: list[EventType] = []

        def boom(_event):
            msg = "listener is broken"
            raise RuntimeError(msg)

        open_client.events.add_listener(boom)
        open_client.events.add_listener(lambda event: seen.append(event.event_type))
        open_client.events.start()
        await collect(open_client.events, 2)
        await open_client.events.stop()

        assert EventType.MOTION in seen

    async def test_unsubscribing_stops_delivery(self, open_client):
        received: list[Event] = []
        unsub = open_client.events.add_listener(received.append)
        unsub()
        await open_client.events._emit(Event(event_type=EventType.MOTION))
        assert received == []

    async def test_stop_is_safe_when_never_started(self, open_client):
        await open_client.events.stop()
        assert open_client.events.running is False

    async def test_stop_from_inside_a_listener_does_not_deadlock(self, open_client, fake_server):
        fake_server.route(
            "/++eventStream",
            stream_handler([b"20190927092026 3 3 MOTION\r"] * 3, delay=0.01),
        )
        stopped = asyncio.Event()

        async def stop_on_motion(event: Event) -> None:
            if event.event_type is EventType.MOTION:
                await open_client.events.stop()
                stopped.set()

        open_client.events.add_listener(stop_on_motion)
        open_client.events.start()
        async with asyncio.timeout(5):
            await stopped.wait()
        assert open_client.events.running is False

    async def test_refresh_failure_is_reported_to_listeners(self, open_client, fake_server):
        fake_server.route(
            "/++eventStream",
            stream_handler([b"20190927092026 3 X CONFIGCHANGE\r"]),
        )
        fake_server.text("/++systemInfo", "not xml at all")
        open_client.events.start()
        events = await collect(open_client.events, 3)
        await open_client.events.stop()

        assert events[-1].event_type == EventType.REFRESHFAIL
