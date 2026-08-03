"""Async SecuritySpy client library."""

from .client import SecSpyClient
from .const import CameraMode, EventType, TriggerReason
from .events import Event, EventStream, parse_event_line
from .exceptions import (
    AuthenticationError,
    InvalidResponseError,
    RequestError,
    ResponseTooLargeError,
    SecSpyError,
    UnsupportedError,
    UntrustedHostError,
)
from .models import Camera, PTZCapabilities, ServerInfo
from .systeminfo import parse_system_info

__all__ = [
    "AuthenticationError",
    "Camera",
    "CameraMode",
    "Event",
    "EventStream",
    "EventType",
    "InvalidResponseError",
    "PTZCapabilities",
    "RequestError",
    "ResponseTooLargeError",
    "SecSpyClient",
    "SecSpyError",
    "ServerInfo",
    "TriggerReason",
    "UnsupportedError",
    "UntrustedHostError",
    "parse_event_line",
    "parse_system_info",
]

__version__ = "0.1.0"
