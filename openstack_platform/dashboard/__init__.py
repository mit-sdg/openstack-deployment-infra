"""Read-only operator dashboard for platform roles and hosted applications.

The dashboard runs beside the operator CLI and has no mutation routes. It reads
database-backed administrator routes from the privileged controller socket
through the pinned admin SSH alias, probes public routes without credentials,
and serves a static browser view over a private Unix socket.
"""
