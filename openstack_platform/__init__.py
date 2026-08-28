"""OpenStack application platform implementation.

The top-level package contains shared configuration, provider, runtime, and
operator modules. Product lifecycle logic lives under :mod:`controller`, while
the constrained privileged action boundary lives under :mod:`helper`.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1
