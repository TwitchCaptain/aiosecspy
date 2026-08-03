"""Client behaviour: request shape, error mapping, and URL builders."""

from __future__ import annotations

import base64
from datetime import UTC
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import pytest
from aiohttp import web
from conftest import read_fixture

from aiosecspy import (
    AuthenticationError,
    RequestError,
    ResponseTooLargeError,
    SecSpyClient,
    UnsupportedError,
    UntrustedHostError,
)

EXPECTED_AUTH = base64.urlsafe_b64encode(b"user:s3cret").decode()


def make_client(host="cam.example", port=8000, **kwargs):
    return SecSpyClient(host, port, "user", "s3cret", **kwargs)


class TestConstruction:
    def test_builds_base_url(self):
        assert make_client().base_url == "http://cam.example:8000/"

    def test_use_ssl_switches_scheme(self):
        assert make_client(use_ssl=True).base_url == "https://cam.example:8000/"

    def test_ipv6_host_is_bracketed(self):
        assert make_client("::1").base_url == "http://[::1]:8000/"

    def test_bracketed_ipv6_host_is_accepted(self):
        assert make_client("[::1]").base_url == "http://[::1]:8000/"

    def test_accepts_a_pasted_url_as_host(self):
        assert make_client("https://cam.example/").host == "cam.example"

    @pytest.mark.parametrize("host", ["", "   ", "cam.example/path", "cam example"])
    def test_rejects_unusable_hosts(self, host):
        with pytest.raises(ValueError, match="host"):
            make_client(host)

    @pytest.mark.parametrize(
        "host",
        [
            "cam.example:8000",
            "[::1]:8000",
            "https://cam.example:8000/",
            "192.0.2.1:8000",
        ],
    )
    def test_rejects_hosts_that_already_include_a_port(self, host):
        with pytest.raises(ValueError, match="port"):
            make_client(host)

    @pytest.mark.parametrize("port", [0, -1, 70000])
    def test_rejects_out_of_range_ports(self, port):
        with pytest.raises(ValueError, match="port"):
            make_client(port=port)

    def test_repr_hides_the_password(self):
        text = repr(make_client())
        assert "s3cret" not in text
        assert "cam.example:8000" in text

    def test_auth_blob_is_url_safe_base64(self):
        client = SecSpyClient("cam.example", 8000, "user", "p+a/ss")
        blob = client.auth_params()["auth"]
        assert base64.urlsafe_b64decode(blob) == b"user:p+a/ss"


class TestRequests:
    async def test_refresh_sends_auth_and_parses_system_info(self, client, fake_server):
        fake_server.text("/++systemInfo", read_fixture("systemInfo-v6.xml"))
        info = await client.refresh()

        assert info.version == "6.20"
        assert fake_server.query_for("/++systemInfo") == {
            "auth": EXPECTED_AUTH,
            "format": "xml",
        }

    async def test_request_before_open_is_a_clear_error(self):
        client = make_client()
        with pytest.raises(RequestError, match="not open"):
            await client.refresh()

    async def test_unauthorized_maps_to_authentication_error(self, client, fake_server):
        fake_server.text("/++systemInfo", "denied", status=401)
        with pytest.raises(AuthenticationError):
            await client.refresh()

    async def test_server_error_maps_to_request_error(self, client, fake_server):
        fake_server.text("/++systemInfo", "boom", status=500)
        with pytest.raises(RequestError, match="500"):
            await client.refresh()

    async def test_missing_command_maps_to_unsupported(self, client):
        with pytest.raises(UnsupportedError):
            await client.trigger_motion(3)

    async def test_non_ok_body_is_rejected(self, client, fake_server):
        fake_server.text("/++triggermd", "FAILED")
        with pytest.raises(RequestError, match="unexpected response"):
            await client.trigger_motion(3)

    async def test_json_result_ok_is_accepted(self, client, fake_server):
        fake_server.text("/++ssSetPreset", '{"result": "OK"}')
        await client.set_schedule_preset(2)

    @pytest.mark.parametrize("status", [200, 500])
    async def test_errors_never_leak_the_auth_blob(self, client, fake_server, status):
        fake_server.text("/++triggermd", f"failed for auth={EXPECTED_AUTH}", status=status)
        with pytest.raises(RequestError) as excinfo:
            await client.trigger_motion(3)
        assert EXPECTED_AUTH not in str(excinfo.value)
        assert "<redacted>" in str(excinfo.value)

    async def test_oversized_response_is_refused(self, client, fake_server):
        client.max_download_bytes = 64
        fake_server.bytes("/++getfilehb/1/x.m4v", b"x" * 4096)
        with pytest.raises(ResponseTooLargeError):
            await client.download_file("++getfile/1/x.m4v")

    async def test_oversized_streamed_response_is_refused(self, client, fake_server):
        async def chunked(request: web.Request) -> web.StreamResponse:
            resp = web.StreamResponse()
            await resp.prepare(request)
            for _ in range(8):
                await resp.write(b"y" * 1024)
            return resp

        client.max_download_bytes = 64
        fake_server.route("/++getfilehb/1/x.m4v", chunked)
        with pytest.raises(ResponseTooLargeError):
            await client.download_file("++getfile/1/x.m4v")


class TestCommands:
    async def test_toggle_motion_sends_arm_flag_and_updates_state(self, open_client, fake_server):
        fake_server.ok("/++ssControlMotionCapture")
        await open_client.toggle_motion(3, arm=False)

        assert fake_server.query_for("/++ssControlMotionCapture") == {
            "auth": EXPECTED_AUTH,
            "cameraNum": "3",
            "arm": "0",
        }
        assert open_client.camera(3).armed_motion is False

    async def test_continuous_falls_back_to_schedule_when_unsupported(
        self, open_client, fake_server
    ):
        # ++ssControlContinuous is absent, so the fake server 404s it.
        fake_server.ok("/++ssSetSchedule")
        await open_client.toggle_continuous(3, arm=True)

        assert fake_server.query_for("/++ssSetSchedule") == {
            "auth": EXPECTED_AUTH,
            "cameraNum": "3",
            "mode": "C",
            "id": "1",
        }

    async def test_continuous_fallback_picks_the_disarmed_schedule(self, open_client, fake_server):
        fake_server.ok("/++ssSetSchedule")
        await open_client.toggle_continuous(3, arm=False)
        assert fake_server.query_for("/++ssSetSchedule")["id"] == "0"

    async def test_continuous_does_not_touch_schedules_on_a_real_failure(
        self, open_client, fake_server
    ):
        fake_server.text("/++ssControlContinuous", "server exploded", status=500)
        fake_server.ok("/++ssSetSchedule")

        with pytest.raises(RequestError):
            await open_client.toggle_continuous(3, arm=True)
        assert "/++ssSetSchedule" not in fake_server.paths()

    async def test_set_schedule_normalizes_the_mode(self, open_client, fake_server):
        fake_server.ok("/++ssSetSchedule")
        await open_client.set_schedule(3, "M", 1)
        assert fake_server.query_for("/++ssSetSchedule")["mode"] == "M"

    async def test_set_schedule_rejects_an_unknown_mode(self, open_client):
        with pytest.raises(ValueError, match="Z"):
            await open_client.set_schedule(3, "Z", 1)

    @pytest.mark.parametrize("preset", [0, 9, -1])
    async def test_ptz_preset_range_is_enforced(self, open_client, preset):
        with pytest.raises(ValueError, match="preset"):
            await open_client.ptz_preset(3, preset)

    async def test_ptz_preset_maps_to_the_command_number(self, open_client, fake_server):
        fake_server.ok("/++ptz/command")
        await open_client.ptz_preset(3, 2)
        assert fake_server.query_for("/++ptz/command")["command"] == "13"

    async def test_get_image_passes_sizing_through(self, open_client, fake_server):
        fake_server.bytes("/++image", b"\xff\xd8jpeg")
        data = await open_client.get_image(3, width=640, height=480, quality=70)

        assert data == b"\xff\xd8jpeg"
        assert fake_server.query_for("/++image") == {
            "auth": EXPECTED_AUTH,
            "cameraNum": "3",
            "width": "640",
            "height": "480",
            "quality": "70",
        }

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"quality": 0}, "quality"),
            ({"quality": 101}, "quality"),
            ({"width": 0}, "width"),
            ({"height": -5}, "height"),
        ],
    )
    async def test_get_image_validates_its_arguments(self, open_client, kwargs, match):
        with pytest.raises(ValueError, match=match):
            await open_client.get_image(3, **kwargs)


class TestUrlBuilders:
    def test_image_url_carries_auth(self):
        url = make_client().image_url(3, width=100)
        assert url.startswith("http://cam.example:8000/++image?")
        assert dict(parse_qsl(urlsplit(url).query))["auth"] == EXPECTED_AUTH
        assert "width=100" in url

    def test_rtsp_url_uses_the_configured_port_and_quotes_credentials(self):
        client = SecSpyClient("cam.example", 8001, "us er", "p@ss/word", use_ssl=True)
        assert client.rtsp_url(3) == (
            "rtsps://us%20er:p%40ss%2Fword@cam.example:8001/stream?cameraNum=3"
        )

    def test_rtsp_url_scheme_can_be_forced(self):
        assert make_client().rtsp_url(3, use_ssl=True).startswith("rtsps://")

    def test_mjpeg_and_hls_urls(self):
        client = make_client()
        assert client.mjpeg_url(3).startswith("http://cam.example:8000/++video?")
        assert client.hls_url(3).startswith("http://cam.example:8000/++hls?")


class TestUntrustedInput:
    """A malicious or compromised server must not be able to steer the client."""

    async def test_download_refuses_a_foreign_host(self, client, fake_server):
        with pytest.raises(UntrustedHostError):
            await client.download_file("http://evil.example/++getfile/1/x.m4v")
        assert fake_server.calls == []

    async def test_download_refuses_a_different_port(self, client, fake_server):
        href = f"http://{client.host}:{client.port + 1}/++getfile/1/x.m4v"
        with pytest.raises(UntrustedHostError):
            await client.download_file(href)
        assert fake_server.calls == []

    async def test_download_refuses_a_scheme_change(self, client, fake_server):
        href = f"https://{client.host}:{client.port}/++getfile/1/x.m4v"
        with pytest.raises(UntrustedHostError):
            await client.download_file(href)
        assert fake_server.calls == []

    async def test_download_refuses_protocol_relative_hrefs(self, client, fake_server):
        with pytest.raises(UntrustedHostError):
            await client.download_file("//evil.example/++getfile/1/x.m4v")
        assert fake_server.calls == []

    async def test_download_refuses_path_traversal(self, client, fake_server):
        with pytest.raises(UntrustedHostError):
            await client.download_file("++getfile/../../etc/passwd")
        assert fake_server.calls == []

    @pytest.mark.parametrize(
        "href",
        [
            "++getfile/../../etc/passwd?auth=SUPERSECRET",
            "++get\\file/x?auth=SUPERSECRET",
        ],
    )
    async def test_refusal_messages_do_not_echo_credentials(self, client, href):
        with pytest.raises(UntrustedHostError) as excinfo:
            await client.download_file(href)
        assert "SUPERSECRET" not in str(excinfo.value)

    async def test_download_refuses_an_empty_href(self, client):
        with pytest.raises(UntrustedHostError):
            await client.download_file("  ")

    @pytest.mark.parametrize(
        "api",
        ["++a\\b", "++a\nb", "/absolute", "++a://b"],
    )
    def test_url_builder_refuses_hostile_paths(self, client, api):
        with pytest.raises(UntrustedHostError):
            client._url(api)

    async def test_href_without_the_plusplus_prefix_is_normalized(self, client, fake_server):
        fake_server.bytes("/++getfilehb/4/x.m4v", b"movie")
        assert await client.download_file("getfile/4/x.m4v") == b"movie"

    async def test_download_rewrites_to_the_high_bandwidth_endpoint(self, client, fake_server):
        fake_server.bytes("/++getfilehb/4/2018-10-17/10-17-2018+M+Gate.m4v", b"movie")
        data = await client.download_file("++getfile/4/2018-10-17/10-17-2018+M+Gate.m4v")
        assert data == b"movie"

    async def test_download_href_auth_cannot_override_client_credentials(self, client, fake_server):
        fake_server.bytes("/++getfilehb/4/x.m4v", b"movie")
        await client.download_file("++getfile/4/x.m4v?auth=attackerblob&cameraNum=4")
        assert fake_server.query_for("/++getfilehb/4/x.m4v") == {
            "auth": EXPECTED_AUTH,
            "cameraNum": "4",
        }

    async def test_download_can_request_the_low_bandwidth_copy(self, client, fake_server):
        fake_server.bytes("/++getfilelb/4/x.m4v", b"small")
        data = await client.download_file("++getfile/4/x.m4v", high_bandwidth=False)
        assert data == b"small"

    async def test_absolute_url_on_the_same_server_is_allowed(self, client, fake_server):
        fake_server.bytes("/++getfilehb/4/x.m4v", b"movie")
        href = f"http://{client.host}:{client.port}/++getfile/4/x.m4v"
        assert await client.download_file(href) == b"movie"


class TestFileListing:
    FEED = """<?xml version="1.0"?>
    <feed>
      <entry>
        <title>Gate</title>
        <link href="++getfile/4/2018-10-17/10-17-2018+M+Gate.m4v"/>
      </entry>
      <entry>
        <title>Door</title>
        <link href="++getfile/3/2018-10-17/10-17-2018+M+Door.m4v"/>
      </entry>
    </feed>
    """

    async def test_lists_motion_files_with_a_date_window(self, open_client, fake_server):
        fake_server.text("/++download", self.FEED)
        files = await open_client.list_motion_files(3, days=2, limit=10)

        assert [f["title"] for f in files] == ["Gate", "Door"]
        query = fake_server.query_for("/++download")
        assert query["mcFilesCheck"] == "1"
        assert query["results"] == "10"
        assert query["cameraNum"] == "3"
        assert query["date1"] <= query["date2"]
        assert len(query["date1"]) == len("2018-10-17")

    async def test_limit_is_applied_to_the_parsed_feed(self, open_client, fake_server):
        fake_server.text("/++download", self.FEED)
        assert len(await open_client.list_motion_files(3, limit=1)) == 1

    @pytest.mark.parametrize(("days", "limit"), [(0, 5), (1, 0)])
    async def test_rejects_nonsense_windows(self, open_client, days, limit):
        with pytest.raises(ValueError, match="at least"):
            await open_client.list_motion_files(3, days=days, limit=limit)

    async def test_download_latest_reports_when_there_is_nothing(self, open_client, fake_server):
        fake_server.text("/++download", '<?xml version="1.0"?><feed/>')
        with pytest.raises(RequestError, match="no motion recordings"):
            await open_client.download_latest_motion_recording(3)

    async def test_download_latest_fetches_the_first_entry(self, open_client, fake_server):
        fake_server.text("/++download", self.FEED)
        fake_server.bytes("/++getfilehb/4/2018-10-17/10-17-2018+M+Gate.m4v", b"clip")
        assert await open_client.download_latest_motion_recording(3) == b"clip"

    async def test_reads_href_from_an_item_child_element(self, open_client, fake_server):
        fake_server.text(
            "/++download",
            '<?xml version="1.0"?><feed><item>'
            "<name>Gate</name><href>++getfile/4/x.m4v</href>"
            "</item></feed>",
        )
        files = await open_client.list_motion_files()
        assert files == [{"title": "Gate", "href": "++getfile/4/x.m4v"}]

    async def test_falls_back_to_scanning_for_getfile_attributes(self, open_client, fake_server):
        fake_server.text(
            "/++download",
            '<?xml version="1.0"?><feed>'
            '<row href="++getfile/4/x.m4v"><title>Gate</title></row>'
            "</feed>",
        )
        files = await open_client.list_motion_files()
        assert files == [{"title": "Gate", "href": "++getfile/4/x.m4v"}]


class TestCoverageOfTheThinWrappers:
    """The one-line command wrappers still need to send the right thing."""

    @pytest.mark.parametrize(
        ("method", "args", "command"),
        [
            ("ptz_left", (), "1"),
            ("ptz_right", (), "2"),
            ("ptz_up", (), "3"),
            ("ptz_down", (), "4"),
            ("ptz_zoom", (True,), "5"),
            ("ptz_zoom", (False,), "6"),
            ("ptz_home", (), "7"),
            ("ptz_stop", (), "99"),
        ],
    )
    async def test_ptz_wrappers(self, open_client, fake_server, method, args, command):
        fake_server.ok("/++ptz/command")
        await getattr(open_client, method)(3, *args)
        assert fake_server.query_for("/++ptz/command")["command"] == command

    async def test_toggle_actions(self, open_client, fake_server):
        fake_server.ok("/++ssControlActions")
        await open_client.toggle_actions(3, arm=True)
        assert fake_server.query_for("/++ssControlActions")["arm"] == "1"
        assert open_client.camera(3).armed_actions is True

    async def test_set_schedule_override(self, open_client, fake_server):
        fake_server.ok("/++ssSetOverride")
        await open_client.set_schedule_override(3, "A", 4)
        assert fake_server.query_for("/++ssSetOverride") == {
            "auth": EXPECTED_AUTH,
            "cameraNum": "3",
            "mode": "A",
            "id": "4",
        }

    async def test_camera_modes_returns_raw_text(self, open_client, fake_server):
        fake_server.text("/++cameramodes", "C:armed M:armed A:disarmed")
        assert await open_client.camera_modes(3) == "C:armed M:armed A:disarmed"

    async def test_camera_lookup_raises_for_unknown_numbers(self, open_client):
        with pytest.raises(KeyError, match="99"):
            open_client.camera(99)

    def test_cameras_is_empty_before_the_first_refresh(self, client):
        assert client.cameras == {}

    async def test_server_timezone_follows_system_info(self, client, open_client):
        assert client.server_timezone.utcoffset(None).total_seconds() == -25200

    def test_rejects_a_non_positive_timeout(self):
        with pytest.raises(ValueError, match="timeout"):
            make_client(timeout=0)

    async def test_continuous_without_system_info_stays_unsupported(self, client):
        with pytest.raises(UnsupportedError, match="no schedule list"):
            await client.toggle_continuous(3, arm=True)

    async def test_continuous_fallback_uses_default_ids_when_names_are_odd(
        self, open_client, fake_server
    ):
        open_client.info.schedules = {7: "Weekdays Only"}
        fake_server.ok("/++ssSetSchedule")
        await open_client.toggle_continuous(3, arm=True)
        assert fake_server.query_for("/++ssSetSchedule")["id"] == "1"

    def test_server_timezone_is_utc_before_the_first_refresh(self, client):
        assert client.server_timezone is UTC

    async def test_a_dead_server_becomes_a_request_error(self, fake_server):
        port = fake_server.port
        await fake_server.server.close()
        async with aiohttp.ClientSession() as session:
            client = SecSpyClient("127.0.0.1", port, "user", "s3cret", session=session, timeout=2)
            with pytest.raises(RequestError, match="transport error"):
                await client.refresh()


class TestSessionLifecycle:
    async def test_close_leaves_a_caller_owned_session_open(self, client):
        session = client.session
        await client.close()
        assert session is not None
        assert not session.closed

    async def test_open_creates_and_closes_its_own_session(self, fake_server):
        fake_server.text("/++systemInfo", read_fixture("systemInfo-v6.xml"))
        client = SecSpyClient(fake_server.host, fake_server.port, "user", "s3cret")
        async with client:
            assert client.session is not None
            assert client.info is not None
        assert client.session is None
