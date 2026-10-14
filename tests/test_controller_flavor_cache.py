"""Flavor display cache hit, expiry, bounded failures and concurrent reads."""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from openstack_platform import openstack
from openstack_platform.controller.flavor_cache import FlavorCache

SMALL = openstack.Flavor("50", "builder-small", 1, 1024, 20)
LARGE = openstack.Flavor("200", "worker-large", 4, 16384, 64)


class FlavorCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.cache = FlavorCache(mock.sentinel.platform, clock=lambda: self.now)
        self.observe = self.enterContext(
            mock.patch.object(
                openstack,
                "observe_flavor_capacity",
                return_value=SMALL,
            )
        )
        self.catalog = self.enterContext(
            mock.patch.object(
                openstack,
                "observe_flavors",
                return_value=(SMALL, LARGE),
            )
        )

    def test_references_and_catalog_cache_hits_and_expire_from_completion(self) -> None:
        def observe(*_args):
            self.now += 30
            return SMALL

        self.observe.side_effect = observe
        self.assertEqual(self.cache.flavor(SMALL.name), SMALL)
        self.now += 599
        self.assertEqual(self.cache.flavor(SMALL.name), SMALL)
        self.assertEqual(self.observe.call_count, 1)
        self.now += 1
        self.cache.flavor(SMALL.name)
        self.assertEqual(self.observe.call_count, 2)
        self.observe.side_effect = None
        self.observe.return_value = LARGE
        self.assertEqual(self.cache.flavor(LARGE.name), LARGE)
        self.assertEqual(self.observe.call_count, 3)
        self.assertEqual(self.cache.flavors(), (SMALL, LARGE))
        self.now += 599
        self.assertEqual(self.cache.flavors(), (SMALL, LARGE))
        self.assertEqual(self.catalog.call_count, 1)
        self.now += 1
        self.cache.flavors()
        self.assertEqual(self.catalog.call_count, 2)

    def test_failures_share_a_short_cooldown_then_recover_for_both_reads(self) -> None:
        for provider, read in (
            (self.observe, lambda: self.cache.flavor(SMALL.name)),
            (self.catalog, self.cache.flavors),
        ):
            with self.subTest(read=read):
                provider.side_effect = openstack.OpenStackError("provider unavailable")
                for _attempt in range(2):
                    with self.assertRaises(openstack.OpenStackError):
                        read()
                self.assertEqual(provider.call_count, 1)
                self.now += 5
                provider.side_effect = None
                read()
                self.assertEqual(provider.call_count, 2)

    def test_single_flight_for_same_reference_without_blocking_other_references(self) -> None:
        started, finish = threading.Event(), threading.Event()

        def observe(_platform, reference):
            if reference == SMALL.name:
                started.set()
                if not finish.wait(5):
                    raise AssertionError("test did not release provider read")
                return SMALL
            return LARGE

        self.observe.side_effect = observe
        with ThreadPoolExecutor(max_workers=12) as pool:
            try:
                readers = [pool.submit(self.cache.flavor, SMALL.name) for _ in range(10)]
                self.assertTrue(started.wait(5))
                self.assertEqual(pool.submit(self.cache.flavor, LARGE.name).result(5), LARGE)
                self.assertEqual(self.observe.call_count, 2)
            finally:
                finish.set()
            self.assertEqual([future.result(5) for future in readers], [SMALL] * 10)
        self.assertEqual(self.observe.call_count, 2)

    def test_catalog_single_flight(self) -> None:
        started, finish = threading.Event(), threading.Event()

        def observe(_platform):
            started.set()
            if not finish.wait(5):
                raise AssertionError("test did not release catalog read")
            return (SMALL, LARGE)

        self.catalog.side_effect = observe
        with ThreadPoolExecutor(max_workers=10) as pool:
            try:
                readers = [pool.submit(self.cache.flavors) for _ in range(10)]
                self.assertTrue(started.wait(5))
                self.assertEqual(self.catalog.call_count, 1)
            finally:
                finish.set()
            self.assertEqual([future.result(5) for future in readers], [(SMALL, LARGE)] * 10)
        self.assertEqual(self.catalog.call_count, 1)

    def test_reference_cache_is_bounded_and_evicts_least_recently_used(self) -> None:
        for index in range(128):
            self.cache.flavor(f"size-{index}")
        self.cache.flavor("size-0")
        self.cache.flavor("size-128")
        self.cache.flavor("size-0")
        self.assertEqual(self.observe.call_count, 129)
        self.cache.flavor("size-1")
        self.assertEqual(self.observe.call_count, 130)
