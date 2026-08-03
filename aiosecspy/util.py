"""Shared helpers: credential redaction and hardened XML parsing."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .exceptions import InvalidResponseError

__all__ = ["parse_xml", "redact"]

_AUTH_PARAM_RE = re.compile(r"(?i)\b(auth|password|pass)=[^&\s\"']*")
_USERINFO_RE = re.compile(r"(?i)(?<=://)[^/@\s]+:[^/@\s]*@")
_REDACTED = "<redacted>"

# A DOCTYPE is the only way to declare the internal entities that drive
# "billion laughs" style expansion attacks, and SecuritySpy never sends one.
_DOCTYPE_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def redact(text: str) -> str:
    """Strip credentials out of a string before it reaches a log or exception.

    Covers the ``auth=`` query parameter (base64 of ``user:password``) and
    ``user:password@`` URL userinfo.
    """
    text = _AUTH_PARAM_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)
    return _USERINFO_RE.sub(f"{_REDACTED}@", text)


def parse_xml(xml_text: str | bytes, *, label: str = "response") -> ET.Element:
    """Parse untrusted XML from the server.

    ``xml.etree`` expands internal entities, so a hostile ++systemInfo reply
    could blow up memory. Documents carrying a DTD are rejected outright rather
    than parsed, which keeps the stdlib parser and avoids a defusedxml
    dependency for a document shape that never legitimately uses one.
    """
    data = xml_text.encode("utf-8", errors="replace") if isinstance(xml_text, str) else xml_text
    if _DOCTYPE_RE.search(data):
        msg = f"{label}: XML doctype and entity declarations are rejected"
        raise InvalidResponseError(msg)
    try:
        return ET.fromstring(data)  # noqa: S314 - DTDs rejected above
    except ET.ParseError as err:
        msg = f"{label}: malformed XML: {err}"
        raise InvalidResponseError(msg) from err
