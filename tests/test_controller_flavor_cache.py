"""Flavor display prewarming, refresh-ahead, stale reads, backoff and admission."""

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
        self.addCleanup(self.cache.close)

    def wait_for_refresh(self, key, *, catalog=False):
        cache = self.cache.catalog if catalog else self.cache.observations
        with cache.lock:
            result = cache.entries[key].result
        return result.result(5)

    def test_refresh_ahead_is_measured_from_completion_without_a_display_read(self) -> None:
        def observe(*_args):
            self.now += 30
            return SMALL

        self.observe.side_effect = observe
        self.assertEqual(self.cache.flavor(SMALL.name), SMALL)
        self.assertEqual(self.cache.flavors(), (SMALL, LARGE))
        self.now += 539
        self.cache.maintain([SMALL.name])
        self.assertEqual(self.observe.call_count, 1)
        self.assertEqual(self.catalog.call_count, 1)
        self.now += 1
        self.cache.maintain([SMALL.name])
        self.wait_for_refresh(SMALL.name)
        self.wait_for_refresh("flavors", catalog=True)
        self.assertEqual(self.observe.call_count, 2)
        self.assertEqual(self.catalog.call_count, 2)

    def test_expired_values_return_immediately_while_one_refresh_runs(self) -> None:
        self.cache.flavor(SMALL.name)
        self.cache.flavors()
        self.now += 601
        entered, release = threading.Event(), threading.Event()

        def slow(*_args):
            entered.set()
            self.assertTrue(release.wait(5))
            return LARGE

        self.observe.side_effect = slow
        self.catalog.side_effect = lambda *_args: (slow(),)
        with ThreadPoolExecutor(max_workers=10) as pool:
            try:
                reads = [pool.submit(self.cache.flavor, SMALL.name) for _ in range(10)]
                self.assertEqual([read.result(1) for read in reads], [SMALL] * 10)
                self.assertTrue(entered.wait(1))
                catalogs = [pool.submit(self.cache.flavors) for _ in range(10)]
                self.assertEqual([read.result(1) for read in catalogs], [(SMALL, LARGE)] * 10)
                self.assertEqual(self.observe.call_count, 2)
                self.assertEqual(self.catalog.call_count, 2)
            finally:
                release.set()
        self.assertEqual(self.wait_for_refresh(SMALL.name), LARGE)
        self.assertEqual(self.wait_for_refresh("flavors", catalog=True), (LARGE,))
        self.assertEqual(self.cache.flavor(SMALL.name), LARGE)

    def test_start_prewarms_catalog_and_in_use_references_without_blocking(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def slow(*_args):
            entered.set()
            self.assertTrue(release.wait(5))
            return SMALL

        self.observe.side_effect = slow
        try:
            self.cache.start(lambda: [SMALL.name, LARGE.name, SMALL.name])
            self.assertTrue(entered.wait(1))
            release.set()
            self.cache.flavor(SMALL.name)
            self.cache.flavor(LARGE.name)
            self.cache.flavors()
        finally:
            release.set()
            self.cache.close()
        self.assertEqual(self.catalog.call_count, 1)
        self.assertCountEqual(
            [call.args[1] for call in self.observe.call_args_list], [SMALL.name, LARGE.name]
        )

    def test_background_pool_and_pending_work_are_bounded(self) -> None:
        release, entered = threading.Event(), threading.Event()
        active = 0
        lock = threading.Lock()

        def slow(*_args):
            nonlocal active
            with lock:
                active += 1
                if active == 2:
                    entered.set()
            self.assertTrue(release.wait(5))
            return SMALL

        self.observe.side_effect = slow
        self.catalog.side_effect = lambda *_args: (slow(),)
        try:
            self.cache.maintain([f"size-{index}" for index in range(100)])
            self.assertTrue(entered.wait(1))
            self.assertEqual(active, 2)
            with self.cache.observations.lock, self.cache.catalog.lock:
                pending = sum(entry.pending for entry in self.cache.observations.entries.values())
                pending += sum(entry.pending for entry in self.cache.catalog.entries.values())
            self.assertEqual(pending, 32)
        finally:
            release.set()
            self.cache.close()

    def test_stale_failure_preserves_value_and_exponential_backoff_resets(self) -> None:
        self.cache.flavor(SMALL.name)
        self.now += 601
        self.observe.side_effect = openstack.OpenStackError("provider unavailable")
        for count, delay in ((2, 5), (3, 10), (4, 20)):
            self.assertEqual(self.cache.flavor(SMALL.name), SMALL)
            with self.assertRaises(openstack.OpenStackError):
                self.wait_for_refresh(SMALL.name)
            self.assertEqual(self.observe.call_count, count)
            self.now += delay - 1
            for _ in range(10):
                self.assertEqual(self.cache.flavor(SMALL.name), SMALL)
            self.assertEqual(self.observe.call_count, count)
            self.now += 1
        self.observe.side_effect = None
        self.observe.return_value = LARGE
        self.assertEqual(self.cache.flavor(SMALL.name), SMALL)
        self.wait_for_refresh(SMALL.name)
        self.assertEqual(self.cache.flavor(SMALL.name), LARGE)
        self.assertEqual(self.cache.observations.entries[SMALL.name].failures, 0)

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
