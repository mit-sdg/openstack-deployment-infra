"""TLS helpers for the platform's retained internal certificate authority."""

from __future__ import annotations

import ssl


def internal_ca_context(cafile: str) -> ssl.SSLContext:
    """Return a verified context compatible with the existing internal CA.

    Python 3.14 enables OpenSSL's strict X.509 verification by default. The
    platform CA predates that default and has no keyUsage extension. Clearing
    only VERIFY_X509_STRICT retains certificate-chain and hostname validation.
    """

    context = ssl.create_default_context(cafile=cafile)
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return context
