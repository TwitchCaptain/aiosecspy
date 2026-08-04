"""Constants for SecuritySpy API."""

from enum import IntEnum, StrEnum


class CameraMode(StrEnum):
    """Schedule / arm mode letters used by SecuritySpy."""

    CONTINUOUS = "C"
    MOTION = "M"
    ACTIONS = "A"
    ALL = "X"


class EventType(StrEnum):
    """Event stream event types (wire values)."""

    ARM_C = "ARM_C"
    DISARM_C = "DISARM_C"
    ARM_M = "ARM_M"
    DISARM_M = "DISARM_M"
    ARM_A = "ARM_A"
    DISARM_A = "DISARM_A"
    ERROR = "ERROR"
    CONFIGCHANGE = "CONFIGCHANGE"
    MOTION = "MOTION"
    MOTION_END = "MOTION_END"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    CLASSIFY = "CLASSIFY"
    TRIGGER_M = "TRIGGER_M"
    TRIGGER_A = "TRIGGER_A"
    FILE = "FILE"
    NULL = "NULL"
    # Library-only
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    AUTHFAIL = "AUTHFAIL"
    UNKNOWN = "UNKNOWN"
    REFRESH = "REFRESH"
    REFRESHFAIL = "REFRESHFAIL"


KNOWN_EVENT_TYPES = frozenset(
    {
        EventType.ARM_C,
        EventType.DISARM_C,
        EventType.ARM_M,
        EventType.DISARM_M,
        EventType.ARM_A,
        EventType.DISARM_A,
        EventType.ERROR,
        EventType.CONFIGCHANGE,
        EventType.MOTION,
        EventType.MOTION_END,
        EventType.ONLINE,
        EventType.OFFLINE,
        EventType.CLASSIFY,
        EventType.TRIGGER_M,
        EventType.TRIGGER_A,
        EventType.FILE,
        EventType.NULL,
    }
)

EVENT_TIME_FORMAT = "%Y%m%d%H%M%S"
CLASSIFY_ABSENT = -99


class TriggerReason(IntEnum):
    """Trigger reason bitmasks (v6 layout).

    On SecuritySpy v5, bit 512 means Animal (not HomeKit).
    """

    MOTION = 1
    AUDIO = 2
    SCRIPT = 4
    CAMERA_EVENT = 8
    WEB_SERVER = 16
    OTHER_CAMERA = 32
    MANUAL = 64
    HUMAN = 128
    VEHICLE = 256
    HOMEKIT = 512
    ANIMAL = 1024
    HUMAN_ARRIVAL = 2048
    HUMAN_DEPARTURE = 4096
    VEHICLE_ARRIVAL = 8192
    VEHICLE_DEPARTURE = 16384
    ANIMAL_ARRIVAL = 32768
    ANIMAL_DEPARTURE = 65536


TRIGGER_REASON_NAMES: dict[TriggerReason, str] = {
    TriggerReason.MOTION: "Motion Detected",
    TriggerReason.AUDIO: "Audio Detected",
    TriggerReason.SCRIPT: "AppleScript",
    TriggerReason.CAMERA_EVENT: "Camera Event",
    TriggerReason.WEB_SERVER: "Web Server",
    TriggerReason.OTHER_CAMERA: "Other Camera",
    TriggerReason.MANUAL: "Manual",
    TriggerReason.HUMAN: "Human Detected",
    TriggerReason.VEHICLE: "Vehicle Detected",
    TriggerReason.HOMEKIT: "HomeKit Event",
    TriggerReason.ANIMAL: "Animal Detected",
    TriggerReason.HUMAN_ARRIVAL: "Human Arrival",
    TriggerReason.HUMAN_DEPARTURE: "Human Departure",
    TriggerReason.VEHICLE_ARRIVAL: "Vehicle Arrival",
    TriggerReason.VEHICLE_DEPARTURE: "Vehicle Departure",
    TriggerReason.ANIMAL_ARRIVAL: "Animal Arrival",
    TriggerReason.ANIMAL_DEPARTURE: "Animal Departure",
}

# PTZ capability bits
PTZ_PAN_TILT = 1
PTZ_HOME = 2
PTZ_ZOOM = 4
PTZ_PRESETS = 8
PTZ_SPEED = 16
PTZ_CONTINUOUS = 32

# PTZ commands (SecuritySpy API)
PTZ_LEFT = 1
PTZ_RIGHT = 2
PTZ_UP = 3
PTZ_DOWN = 4
PTZ_ZOOM_IN = 5
PTZ_ZOOM_OUT = 6
PTZ_HOME_CMD = 7
PTZ_UP_LEFT = 8
PTZ_UP_RIGHT = 9
PTZ_DOWN_LEFT = 10
PTZ_DOWN_RIGHT = 11
PTZ_PRESET_BASE = 12  # goto preset N -> command 12 + (N - 1)
PTZ_STOP = 99
PTZ_PRESET_SAVE_BASE = 112  # save preset N -> command 112 + (N - 1)

# SecuritySpy's wire protocol addresses presets 1-8 (commands 12-19 / 112-119).
# ++systemInfo can carry preset-name-9/10 entries, but there is no command to
# recall them, so the library ignores them.
PTZ_PRESET_MIN = 1
PTZ_PRESET_MAX = 8

DEFAULT_TIMEOUT = 30.0

# Event stream tuning.
EVENT_RECONNECT_DELAY = 5.0
EVENT_RECONNECT_MAX_DELAY = 300.0
# SecuritySpy emits NULL keepalives on the event stream, so a long silence means
# the socket is dead even though TCP never noticed.
EVENT_READ_TIMEOUT = 300.0
# A single event line is a timestamp, id, camera and a short message. Anything
# larger is a malformed or hostile stream, so the reassembly buffer is capped.
EVENT_MAX_LINE_BYTES = 64 * 1024
# Events waiting for listeners queue up here. When the queue fills, the reader
# briefly waits for the dispatcher to catch up (ordinary burst); if it cannot
# within the timeout, a listener is stuck and the oldest events are dropped so
# stream reads never stall behind a hung callback.
EVENT_QUEUE_MAXSIZE = 512
EVENT_QUEUE_PUT_TIMEOUT = 5.0

# Response size ceilings. A compromised or malfunctioning server should not be
# able to exhaust memory on the client.
MAX_TEXT_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

MIN_PORT = 1
MAX_PORT = 65535

MIN_QUALITY = 1
MAX_QUALITY = 100
MAX_IMAGE_DIMENSION = 16384

DOWNLOAD_DATE_FORMAT = "%Y-%m-%d"
