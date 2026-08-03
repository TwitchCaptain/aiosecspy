# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/TwitchCaptain/aiosecspy/security/advisories/new)
rather than opening a public issue. Expect an initial response within a week.

## Supported versions

Only the latest published release receives fixes.

## Threat model

This library talks to a SecuritySpy server on your network. Two things follow
from that, and both shape the code:

- **The `auth` parameter is the password.** SecuritySpy authenticates with
  `auth=<url-safe base64 of "username:password">` on the query string, so any
  URL this library builds is a credential. `image_url()`, `mjpeg_url()`,
  `hls_url()` and `rtsp_url()` all embed one. Do not log them, do not put them
  in error reports, and prefer `get_image()` when you only need the bytes.
  Exception messages and log records emitted by this library are passed through
  a redaction filter first.
- **The server's responses are untrusted input.** A SecuritySpy instance can be
  compromised, spoofed on a flat LAN, or simply buggy. The client therefore
  refuses XML with a DTD, caps every response body, caps the event-stream line
  buffer, and refuses to follow a `++download` link that points anywhere other
  than the configured host, port and scheme.

`verify_ssl=False` disables certificate verification and is only appropriate on
a trusted network with a self-signed SecuritySpy certificate.
