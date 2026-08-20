"""Shared implementation for the staff platform CLI.

Feature modules intentionally import the small concrete helpers in this package
directly.  The package does not define provider or repository abstractions.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1
