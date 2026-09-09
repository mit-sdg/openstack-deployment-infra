"""Allowlisted deployment snapshots, never mutable application source or secret state."""

from __future__ import annotations

import json
import sqlite3

from ..validation import ValidationError, repository_url
from . import database as db


def source_repository(connection: sqlite3.Connection, attempt: db.DeploymentAttempt) -> str | None:
    """Read the exact deployment intent, not the application's latest repository.

    Missing/imported or inconsistent journal evidence is unknown, not permission
    to guess a historical source from current application state.
    """
    operation = db.get_operation(connection, attempt.deployment_id)
    if (
        operation is None
        or operation.kind != "app.deploy"
        or operation.scope != f"app-{attempt.application_id}"
        or operation.refs.get("application_id") != attempt.application_id
        or operation.refs.get("source_commit") != attempt.source_commit
        or operation.refs.get("configuration_sha256") != attempt.configuration_sha256
        or operation.refs.get("configuration_revision") != attempt.configuration_revision
        or operation.refs.get("requested_ref") != attempt.requested_ref
    ):
        return None
    try:
        return repository_url(operation.refs.get("repository"))
    except ValidationError:
        return None


def configuration_snapshot(attempt: db.DeploymentAttempt) -> object:
    # The strict configuration type contains build/runtime settings and storage
    # resource IDs/output key names only. Never project operation refs, Nomad jobs
    # or environment/secret values into a management response.
    return (
        None
        if attempt.configuration is None
        else json.loads(attempt.configuration.canonical_json())
    )
