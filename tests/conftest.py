"""Shared fixtures: a fake SecuritySpy web server backed by aiohttp."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from aiosecspy import SecSpyClient

FIXTURES = Path(__file__).parent / "fixtures"

Responder = Callable[[web.Request], web.StreamResponse | Awaitable[web.StreamResponse]]


def read_fixture(name: str) -> str:
    """Return the text of a file in tests/fixtures."""
    return (FIXTURES / name).read_text()


@dataclass
class Call:
    """One request the fake server received."""

    path: str
    query: dict[str, str]


@dataclass
class FakeSecSpy:
    """A stand-in SecuritySpy server with scripted responses."""

    routes: dict[str, Responder] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    server: TestServer | None = None

    @property
    def host(self) -> str:
        assert self.server is not None
        return str(self.server.host)

    @property
    def port(self) -> int:
        assert self.server is not None
        return int(self.server.port or 0)

    def route(self, path: str, responder: Responder) -> None:
        """Attach a handler to an absolute path such as ``/++image``."""
        self.routes[path] = responder

    def text(self, path: str, body: str, *, status: int = 200) -> None:
        """Reply to ``path`` with a fixed text body."""
        self.route(path, lambda _request: web.Response(status=status, text=body))

    def bytes(self, path: str, body: bytes, *, status: int = 200) -> None:
        """Reply to ``path`` with a fixed binary body."""
        self.route(
            path,
            lambda _request: web.Response(
                status=status, body=body, content_type="application/octet-stream"
            ),
        )

    def ok(self, path: str) -> None:
        """Reply to ``path`` the way SecuritySpy acknowledges a command."""
        self.text(path, "OK")

    def query_for(self, path: str) -> dict[str, str]:
        """Return the query of the most recent request to ``path``."""
        for call in reversed(self.calls):
            if call.path == path:
                return call.query
        msg = f"no request was made to {path}; saw {[c.path for c in self.calls]}"
        raise AssertionError(msg)

    def paths(self) -> list[str]:
        """Every path requested so far, in order."""
        return [call.path for call in self.calls]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.calls.append(Call(path=request.path, query=dict(request.query)))
        responder = self.routes.get(request.path)
        if responder is None:
            return web.Response(status=404, text="Not Found")
        result = responder(request)
        if isinstance(result, Awaitable):
            return await result
        return result


@pytest.fixture
async def fake_server():
    """Run a fake SecuritySpy server for the duration of one test."""
    fake = FakeSecSpy()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake._handle)
    server = TestServer(app)
    await server.start_server()
    fake.server = server
    try:
        yield fake
    finally:
        await server.close()


@pytest.fixture
async def client(fake_server):
    """A SecSpyClient pointed at the fake server, using a caller-owned session."""
    async with aiohttp.ClientSession() as session:
        secspy = SecSpyClient(
            fake_server.host,
            fake_server.port,
            "user",
            "s3cret",
            session=session,
        )
        try:
            yield secspy
        finally:
            await secspy.close()


@pytest.fixture
async def open_client(client, fake_server):
    """A client that has already loaded the v6 ++systemInfo fixture."""
    fake_server.text("/++systemInfo", read_fixture("systemInfo-v6.xml"))
    await client.refresh()
    return client
