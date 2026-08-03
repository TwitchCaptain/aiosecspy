"""Exceptions for aiosecspy."""

from __future__ import annotations


class SecSpyError(Exception):
    """Base error for SecuritySpy client failures."""


class AuthenticationError(SecSpyError):
    """Invalid credentials or unauthorized response."""


class UnsupportedError(SecSpyError):
    """Server does not support the requested operation."""


class RequestError(SecSpyError):
    """HTTP or transport failure talking to SecuritySpy."""


class UntrustedHostError(RequestError):
    """A URL supplied by the server points somewhere other than the server.

    Raised instead of following the redirect-like link, because doing so would
    send the SecuritySpy credentials to a third party.
    """


class ResponseTooLargeError(RequestError):
    """The server sent more data than the configured ceiling allows."""


class InvalidResponseError(RequestError):
    """The server replied with a body this client cannot parse or trust."""
