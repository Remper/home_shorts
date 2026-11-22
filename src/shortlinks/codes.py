"""Short codes: normalisation, validation and generation.

Codes are case-insensitive and stored in upper case. Upper-case letters,
digits and '-' are all in the QR code alphanumeric set, so a printed link such
as HTTPS://S.EXAMPLE.NET/A001 encodes in alphanumeric mode, which needs a
smaller symbol than byte mode. Lower-case input (someone typing a code) is
folded to upper case before lookup.
"""

from __future__ import annotations

import re
import secrets

# Generated codes avoid characters that are easily confused when read off a
# label or typed: 0/O and 1/I. 32 symbols, so 5 characters give 2**25
# (about 33 million) codes.
GENERATED_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

MAX_CODE_LENGTH = 32

# A custom code: letters, digits and inner hyphens. No leading or trailing
# hyphen, no doubled hyphen ("A--1" reads like a typo).
_CODE_RE = re.compile(r"[A-Z0-9](?:-?[A-Z0-9])*")

# Words that are paths of the service itself, or would look like one.
RESERVED = frozenset({"API", "MCP", "HEALTHZ", "ROBOTS", "FAVICON", "STATIC", "ADMIN"})


class InvalidCode(ValueError):
    pass


def normalise(raw: str) -> str | None:
    """Return the canonical form of a code from a request path, or None.

    Only the exact code is accepted: no slashes, dots, percent-escapes or
    other characters. That makes /A001/, /A001.html and /A%30%30%31 unknown
    rather than aliases, so each code has exactly one spelling up to case.
    """
    if not raw or len(raw) > MAX_CODE_LENGTH or not raw.isascii():
        return None
    code = raw.upper()
    if not _CODE_RE.fullmatch(code):
        return None
    return code


def validate_custom(raw: str) -> str:
    """Validate a code chosen by an API client; return its canonical form."""
    code = normalise(raw)
    if code is None:
        raise InvalidCode(
            f"code must be 1-{MAX_CODE_LENGTH} letters, digits and inner single hyphens"
        )
    if code in RESERVED:
        raise InvalidCode(f"code {code} is reserved")
    return code


def generate(length: int) -> str:
    return "".join(secrets.choice(GENERATED_ALPHABET) for _ in range(length))
