"""App binding defaults must not change the provider or stored secret contract."""

from __future__ import annotations

import unittest

from openstack_platform.controller.storage_contract import (
    DEFAULT_BINDINGS,
    canonicalize_environment,
    provider_environment,
)
from openstack_platform.helper.storage import s3_environment


class StorageContractTests(unittest.TestCase):
    def test_s3_defaults_use_app_names_while_provider_round_trip_keeps_aws_names(self) -> None:
        self.assertEqual(
            dict(DEFAULT_BINDINGS["s3"]),
            {
                "endpoint": "S3_ENDPOINT",
                "public_endpoint": "S3_PUBLIC_ENDPOINT",
                "region": "S3_REGION",
                "access_key_id": "S3_ACCESS_KEY_ID",
                "secret_access_key": "S3_SECRET_ACCESS_KEY",
                "bucket": "S3_BUCKET",
            },
        )
        provider = dict(s3_environment("https://storage:9000", "bucket", "key", "secret"))
        self.assertEqual(
            provider,
            {
                "AWS_ENDPOINT_URL_S3": "https://storage:9000",
                "S3_ENDPOINT": "https://storage:9000",
                "AWS_REGION": "garage",
                "AWS_ACCESS_KEY_ID": "key",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "S3_BUCKET": "bucket",
            },
        )
        stored = canonicalize_environment("s3", "default", provider)
        self.assertEqual(
            stored,
            {
                "STORAGE__S3__DEFAULT__ENDPOINT": "https://storage:9000",
                "STORAGE__S3__DEFAULT__REGION": "garage",
                "STORAGE__S3__DEFAULT__ACCESS_KEY_ID": "key",
                "STORAGE__S3__DEFAULT__SECRET_ACCESS_KEY": "secret",
                "STORAGE__S3__DEFAULT__BUCKET": "bucket",
            },
        )
        self.assertEqual(provider_environment("s3", "default", stored), provider)
