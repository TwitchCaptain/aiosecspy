"""Async SecuritySpy client library."""

from .client import SecSpyClient
from .const import CLASSIFY_ABSENT, CameraMode, EventType, TriggerReason
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
from .models import Camera, PTZCapabilities, RecordingFile, ServerInfo
from .systeminfo import parse_system_info

__all__ = [
    "CLASSIFY_ABSENT",
    "AuthenticationError",
    "Camera",
    "CameraMode",
    "Event",
    "EventStream",
    "EventType",
    "InvalidResponseError",
    "PTZCapabilities",
    "RecordingFile",
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

__version__ = "0.2.0"
