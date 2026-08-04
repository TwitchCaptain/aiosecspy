"""Data models for SecuritySpy systemInfo."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .const import (
    CLASSIFY_ABSENT,
    PTZ_CONTINUOUS,
    PTZ_HOME,
    PTZ_PAN_TILT,
    PTZ_PRESETS,
    PTZ_SPEED,
    PTZ_ZOOM,
)

if TYPE_CHECKING:
    from datetime import datetime


@dataclass
class PTZCapabilities:
    """Decoded PTZ capability bitmask."""

    raw: int = 0
    has_pan_tilt: bool = False
    has_home: bool = False
    has_zoom: bool = False
    has_presets: bool = False
    has_speed: bool = False
    continuous: bool = False

    @classmethod
    def from_raw(cls, raw: int) -> PTZCapabilities:
        """Build from SecuritySpy capability integer."""
        return cls(
            raw=raw,
            has_pan_tilt=bool(raw & PTZ_PAN_TILT),
            has_home=bool(raw & PTZ_HOME),
            has_zoom=bool(raw & PTZ_ZOOM),
            has_presets=bool(raw & PTZ_PRESETS),
            has_speed=bool(raw & PTZ_SPEED),
            continuous=bool(raw & PTZ_CONTINUOUS),
        )


@dataclass
class Camera:
    """A SecuritySpy camera from ++systemInfo."""

    number: int
    name: str
    connected: bool = False
    width: int = 0
    height: int = 0
    mode_c: str = "disarmed"
    mode_m: str = "disarmed"
    mode_a: str = "disarmed"
    schedule_id_cc: int = 0
    schedule_id_mc: int = 0
    schedule_id_a: int = 0
    schedule_override_cc: int = 0
    schedule_override_mc: int = 0
    schedule_override_a: int = 0
    ptz: PTZCapabilities = field(default_factory=PTZCapabilities)
    preset_names: dict[int, str] = field(default_factory=dict)
    has_audio: bool = False
    md_enabled: bool = True
    # Runtime event state, maintained by EventStream while it is running.
    # Scores default to CLASSIFY_ABSENT (-99) so "never classified" is
    # distinguishable from a real 0 score.
    motion_active: bool = False
    event_object: str | None = None
    score_human: int = CLASSIFY_ABSENT
    score_vehicle: int = CLASSIFY_ABSENT
    score_animal: int = CLASSIFY_ABSENT
    last_motion_time: datetime | None = None
    trigger_reasons: list[str] = field(default_factory=list)

    @property
    def armed_continuous(self) -> bool:
        """True if continuous capture is armed."""
        return self.mode_c.lower() == "armed"

    @property
    def armed_motion(self) -> bool:
        """True if motion capture is armed."""
        return self.mode_m.lower() == "armed"

    @property
    def armed_actions(self) -> bool:
        """True if actions are armed."""
        return self.mode_a.lower() == "armed"

    def copy_runtime_state(self, other: Camera) -> None:
        """Adopt the event-stream state of a previous instance of this camera.

        ++systemInfo does not report motion or classification state, so a
        refresh would otherwise reset everything the event stream has observed.
        """
        self.motion_active = other.motion_active
        self.event_object = other.event_object
        self.score_human = other.score_human
        self.score_vehicle = other.score_vehicle
        self.score_animal = other.score_animal
        self.last_motion_time = other.last_motion_time
        self.trigger_reasons = list(other.trigger_reasons)


@dataclass(frozen=True)
class RecordingFile:
    """One recording advertised by the ++download feed."""

    title: str
    href: str


@dataclass
class ServerInfo:
    """SecuritySpy server metadata from ++systemInfo."""

    name: str = "SecuritySpy"
    version: str = ""
    uuid: str = ""
    ip1: str = ""
    ip2: str = ""
    http_port: int = 8000
    https_port: int = 8001
    http_enabled: bool = True
    https_enabled: bool = False
    gmt_offset_seconds: int = 0
    camera_count: int = 0
    schedules: dict[int, str] = field(default_factory=dict)
    overrides: dict[int, str] = field(default_factory=dict)
    presets: dict[int, str] = field(default_factory=dict)
    cameras: dict[int, Camera] = field(default_factory=dict)

    @property
    def major_version(self) -> int:
        """Major version number (5 or 6, etc.)."""
        try:
            return int(self.version.split(".", maxsplit=1)[0])
        except (ValueError, IndexError):
            return 0
