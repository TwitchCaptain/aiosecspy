"""++systemInfo parsing across SecuritySpy v5 and v6 schemas."""

from __future__ import annotations

import pytest
from conftest import read_fixture

from aiosecspy import InvalidResponseError, parse_system_info
from aiosecspy.models import Camera, PTZCapabilities


def test_parse_system_info_v6():
    info = parse_system_info(read_fixture("systemInfo-v6.xml"))

    assert info.version == "6.20"
    assert info.major_version == 6
    assert info.uuid == "EXAMPLEUUID000000001"
    assert info.name == "Example Server"
    assert info.gmt_offset_seconds == -25200
    assert info.https_enabled is True
    assert info.http_port == 8000

    door = info.cameras[3]
    assert door.name == "Door"
    assert door.connected is True
    assert door.armed_motion is True
    assert door.armed_actions is True
    assert door.armed_continuous is False
    assert door.ptz.has_pan_tilt is True
    assert door.has_audio is True
    assert (door.width, door.height) == (3072, 2048)

    assert info.schedules[1] == "Armed 24/7"
    assert info.overrides[0] == "No Override"


def test_parse_system_info_v5():
    info = parse_system_info(read_fixture("systemInfo-v5.xml"))

    assert info.version.startswith("5.")
    assert info.major_version == 5
    porch = info.cameras[1]
    assert porch.name == "Porch"
    assert porch.armed_continuous is True
    assert porch.width == 2304


def test_camera_count_falls_back_to_the_camera_list():
    xml = """<?xml version="1.0"?><system>
      <server><version>6.20</version></server>
      <camera-list><camera><number>1</number><name>One</name></camera></camera-list>
    </system>"""
    assert parse_system_info(xml).camera_count == 1


def test_unknown_version_reports_major_zero():
    info = parse_system_info('<?xml version="1.0"?><system><server/></system>')
    assert info.major_version == 0
    assert info.name == "SecuritySpy"


def test_malformed_xml_is_an_invalid_response():
    with pytest.raises(InvalidResponseError, match="malformed"):
        parse_system_info("<system><server>")


def test_entity_expansion_is_refused():
    """A hostile server must not be able to blow up memory with a DTD."""
    bomb = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
      <!ENTITY lol "lol">
      <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
    ]>
    <system><server><name>&lol2;</name></server></system>"""
    with pytest.raises(InvalidResponseError, match="doctype"):
        parse_system_info(bomb)


def test_external_entity_is_refused():
    xxe = """<?xml version="1.0"?>
    <!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <system><server><name>&xxe;</name></server></system>"""
    with pytest.raises(InvalidResponseError):
        parse_system_info(xxe)


class TestPTZCapabilities:
    def test_decodes_every_bit(self):
        caps = PTZCapabilities.from_raw(63)
        assert caps.has_pan_tilt
        assert caps.has_home
        assert caps.has_zoom
        assert caps.has_presets
        assert caps.has_speed
        assert caps.continuous

    def test_no_capabilities(self):
        caps = PTZCapabilities.from_raw(0)
        assert not any(
            [
                caps.has_pan_tilt,
                caps.has_home,
                caps.has_zoom,
                caps.has_presets,
                caps.has_speed,
                caps.continuous,
            ]
        )


class TestRuntimeState:
    def test_copy_runtime_state_carries_event_fields(self):
        old = Camera(number=1, name="Door")
        old.motion_active = True
        old.event_object = "human"
        old.score_human = 97
        old.last_motion_time = "2026-08-03T01:00:00"
        old.trigger_reasons = ["Motion Detected"]

        new = Camera(number=1, name="Door")
        new.copy_runtime_state(old)

        assert new.motion_active is True
        assert new.event_object == "human"
        assert new.score_human == 97
        assert new.last_motion_time == "2026-08-03T01:00:00"
        assert new.trigger_reasons == ["Motion Detected"]
        # The list is copied, not shared.
        new.trigger_reasons.append("Human Detected")
        assert old.trigger_reasons == ["Motion Detected"]

    async def test_refresh_preserves_runtime_state(self, open_client):
        camera = open_client.camera(3)
        camera.motion_active = True
        camera.score_human = 88

        await open_client.refresh()

        refreshed = open_client.camera(3)
        assert refreshed is not camera
        assert refreshed.motion_active is True
        assert refreshed.score_human == 88

    async def test_refresh_still_updates_configuration(self, open_client, fake_server):
        assert open_client.camera(3).armed_motion is True
        fake_server.text(
            "/++systemInfo",
            read_fixture("systemInfo-v6.xml").replace(
                "<mc-mode>armed</mc-mode>", "<mc-mode>disarmed</mc-mode>"
            ),
        )
        await open_client.refresh()
        assert open_client.camera(3).armed_motion is False
