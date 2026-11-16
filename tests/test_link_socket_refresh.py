import asyncio
from datetime import datetime, timezone
import json
import unittest

import aiohttp
import lightwave_smart.link as link_mod
from lightwave_smart.device import LWRFDevice, LWRFFeature, LWRFFeatureSet
from lightwave_smart.link import LWLink2, link_id_from_combined_id


def _feature(feature_id):
    return LWRFFeature(feature_id, {"attributes": {"type": "switch"}}, None)


class LinkIdTests(unittest.TestCase):
    def test_reads_link_id_before_factory_resets(self):
        self.assertEqual(link_id_from_combined_id("struct-10-99+2"), "99")
        self.assertIsNone(link_id_from_combined_id("struct-10"))
        self.assertIsNone(link_id_from_combined_id(None))


class LinkSocketRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._debounce = link_mod._LINK_REFRESH_DEBOUNCE_SECS
        self._retry = link_mod._LINK_REFRESH_RETRY_SECS
        self._attempts = link_mod._LINK_REFRESH_ATTEMPTS
        self._grace = link_mod._LINK_OFFLINE_GRACE_SECS
        link_mod._LINK_REFRESH_DEBOUNCE_SECS = 0
        link_mod._LINK_REFRESH_RETRY_SECS = 0
        link_mod._LINK_REFRESH_ATTEMPTS = 3
        link_mod._LINK_OFFLINE_GRACE_SECS = 0

    def tearDown(self):
        link_mod._LINK_REFRESH_DEBOUNCE_SECS = self._debounce
        link_mod._LINK_REFRESH_RETRY_SECS = self._retry
        link_mod._LINK_REFRESH_ATTEMPTS = self._attempts
        link_mod._LINK_OFFLINE_GRACE_SECS = self._grace

    def _link(self):
        link = LWLink2()
        own = _feature("struct-10-99+2")
        other = _feature("struct-11-50+0")
        link.features = {own.id: own, other.id: other}
        return link, own, other

    async def _finish(self, link):
        task = next(iter(link._link_refresh_tasks.values()))
        await task

    async def test_online_event_reads_only_that_link_without_cache(self):
        link, own, other = self._link()
        sent = []

        async def send(message, redact=False, immediate=False):
            sent.append(message.get_items())
            return [{
                "itemId": item["itemId"],
                "success": True,
                "payload": {"value": 0},
            } for item in message.get_items()]

        link._ws.async_sendmessage = send
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "deviceId": "struct-1-99+2", "online": True}}]
        })
        await self._finish(link)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0]["payload"]["featureId"], own.id)
        self.assertIs(sent[0][0]["payload"]["noCache"], True)
        self.assertEqual(own.state, 0)
        self.assertIsNone(other.state)

    async def test_offline_event_does_not_read(self):
        link, _own, _other = self._link()
        link._ws.async_sendmessage = self.fail
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": False}}]
        })
        self.assertEqual(link._link_refresh_tasks, {})
        await next(iter(link._link_grace_tasks.values()))

    async def test_failed_read_is_retried(self):
        link, own, _other = self._link()
        calls = {"n": 0}

        async def send(message, redact=False, immediate=False):
            calls["n"] += 1
            success = calls["n"] > 1
            return [{
                "itemId": item["itemId"],
                "success": success,
                "error": {"code": "TIMEOUT", "message": "Request timeout"} if not success else None,
                "payload": {"value": 7} if success else {},
            } for item in message.get_items()]

        link._ws.async_sendmessage = send
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True}}]
        })
        await self._finish(link)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(own.state, 7)

    async def test_second_online_event_shares_the_refresh_task(self):
        link, _own, _other = self._link()

        async def send(message, redact=False, immediate=False):
            return [{
                "itemId": item["itemId"],
                "success": True,
                "payload": {"value": 1},
            } for item in message.get_items()]

        link._ws.async_sendmessage = send
        link._arm_link_feature_refresh(99)
        link._arm_link_feature_refresh(99)
        self.assertEqual(len(link._link_refresh_tasks), 1)
        await self._finish(link)

    async def test_second_online_event_does_not_read_again(self):
        link, _own, _other = self._link()
        calls = {"n": 0}

        async def send(message, redact=False, immediate=False):
            calls["n"] += 1
            return [{
                "itemId": item["itemId"],
                "success": True,
                "payload": {"value": 1},
            } for item in message.get_items()]

        link._ws.async_sendmessage = send
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True}}]
        })
        await self._finish(link)
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": True}}]
        })

        self.assertEqual(calls["n"], 1)
        self.assertEqual(link._link_socket_state.get("99"), "online")

    async def test_partial_failure_retries_only_the_failed_feature(self):
        link, failed_feature, other = self._link()
        other_on_link = _feature("struct-10-99+3")
        link.features[other_on_link.id] = other_on_link
        calls = []

        async def send(message, redact=False, immediate=False):
            ids = [item["payload"]["featureId"] for item in message.get_items()]
            calls.append(ids)
            return [{
                "itemId": item["itemId"],
                "success": item["payload"]["featureId"] != failed_feature.id or len(calls) > 1,
                "payload": {"value": 4},
            } for item in message.get_items()]

        link._ws.async_sendmessage = send
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True}}]
        })
        await self._finish(link)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], [failed_feature.id])
        self.assertEqual(failed_feature.state, 4)
        self.assertEqual(other_on_link.state, 4)
        self.assertIsNone(other.state)

    async def test_empty_cycle_stays_online_until_features_load(self):
        link = LWLink2()
        sent = []

        async def send(message, redact=False, immediate=False):
            sent.append([item["payload"]["featureId"] for item in message.get_items()])
            return [{
                "itemId": item["itemId"],
                "success": True,
                "payload": {"value": 3},
            } for item in message.get_items()]

        link._ws.async_sendmessage = send
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True}}]
        })
        await self._finish(link)
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True}}]
        })

        self.assertEqual(sent, [])
        self.assertEqual(link._link_socket_state.get("99"), "online")

        own = _feature("struct-10-99+2")
        link.features = {own.id: own}
        await link._kick_online_link_feature_refreshes()
        await self._finish(link)
        await link._kick_online_link_feature_refreshes()

        self.assertEqual(sent, [[own.id]])
        self.assertEqual(own.state, 3)

    async def test_missing_online_field_is_ignored(self):
        link, _own, _other = self._link()
        link._ws.async_sendmessage = self.fail
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "deviceId": "struct-1-99+2"}}]
        })
        self.assertEqual(link._link_refresh_tasks, {})
        self.assertEqual(link._link_grace_tasks, {})
        self.assertNotIn("99", link._link_socket_state)

    async def test_deactivate_during_debounce_does_not_refresh(self):
        link, _own, _other = self._link()
        link_mod._LINK_REFRESH_DEBOUNCE_SECS = 30
        link._ws.async_sendmessage = self.fail
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True}}]
        })
        self.assertEqual(len(link._link_refresh_tasks), 1)

        await link.async_deactivate()
        await asyncio.sleep(0)

        self.assertEqual(link._link_refresh_tasks, {})
        self.assertTrue(link._closing)

    async def test_older_offline_does_not_cancel_the_refresh(self):
        link, _own, _other = self._link()
        link_mod._LINK_REFRESH_DEBOUNCE_SECS = 30
        link._ws.async_sendmessage = self.fail
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True, "connectedSince": 5000}}]
        })
        self.assertEqual(len(link._link_refresh_tasks), 1)

        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": False, "connectedSince": 1000}}]
        })

        self.assertEqual(link._link_socket_state.get("99"), "online")
        self.assertEqual(link._link_grace_tasks, {})
        task = next(iter(link._link_refresh_tasks.values()))
        self.assertFalse(task.done())
        await link.async_deactivate()


class LinkLastEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_utc_now = link_mod._utc_now
        self._debounce = link_mod._LINK_REFRESH_DEBOUNCE_SECS
        self._grace = link_mod._LINK_OFFLINE_GRACE_SECS
        link_mod._LINK_REFRESH_DEBOUNCE_SECS = 0
        link_mod._LINK_OFFLINE_GRACE_SECS = 0

    def tearDown(self):
        link_mod._utc_now = self._orig_utc_now
        link_mod._LINK_REFRESH_DEBOUNCE_SECS = self._debounce
        link_mod._LINK_OFFLINE_GRACE_SECS = self._grace

    def _two_hub_link(self):
        link = LWLink2()

        hub1_dev = LWRFDevice()
        hub1_dev.link = link
        hub1_dev.device_id = "s1-1-99+0"
        hub1_dev.product_code = "L2"
        hub1_fs = LWRFFeatureSet()
        hub1_fs.link = link
        hub1_fs.featureset_id = "s1-hubgrp"
        hub1_fs.device = hub1_dev
        hub1_dev.add_featureset(hub1_fs)

        hub2_dev = LWRFDevice()
        hub2_dev.link = link
        hub2_dev.device_id = "s2-1-50+0"
        hub2_dev.product_code = "L2"
        hub2_fs = LWRFFeatureSet()
        hub2_fs.link = link
        hub2_fs.featureset_id = "s2-hubgrp"
        hub2_fs.device = hub2_dev
        hub2_dev.add_featureset(hub2_fs)

        link.devices = {hub1_dev.device_id: hub1_dev, hub2_dev.device_id: hub2_dev}
        link.featuresets = {hub1_fs.featureset_id: hub1_fs, hub2_fs.featureset_id: hub2_fs}
        link.set_structure_data(hub1_dev.device_id, hub1_fs.featureset_id, "Home")
        link.set_structure_data(hub2_dev.device_id, hub2_fs.featureset_id, "Outbuilding")

        feat1 = _feature("s1-10-99+2")
        feat2 = _feature("s2-11-50+0")
        link.features = {feat1.id: feat1, feat2.id: feat2}
        return link, hub1_fs.featureset_id, hub2_fs.featureset_id

    async def test_feature_and_device_status_events_stamp_only_matching_hub(self):
        link, hub1_fs_id, hub2_fs_id = self._two_hub_link()
        self.assertIsNone(link.get_link_last_event(hub1_fs_id))
        self.assertIsNone(link.get_link_last_event(hub2_fs_id))

        t1 = datetime(2026, 11, 14, 18, 0, 0, tzinfo=timezone.utc)
        link_mod._utc_now = lambda: t1
        await link._feature_event_handler({
            "items": [{"payload": {"featureId": "s1-10-99+2", "value": 1}}]
        })
        self.assertEqual(link.get_link_last_event(hub1_fs_id), t1)
        self.assertIsNone(link.get_link_last_event(hub2_fs_id))

        t2 = datetime(2026, 11, 14, 18, 1, 0, tzinfo=timezone.utc)
        link_mod._utc_now = lambda: t2
        await link._device_status_event_handler({
            "items": [{"payload": {"deviceId": "s2-4-50+0", "status": "connected"}}]
        })
        self.assertEqual(link.get_link_last_event(hub1_fs_id), t1)
        self.assertEqual(link.get_link_last_event(hub2_fs_id), t2)

    async def test_repeat_online_heartbeat_advances_time_while_stale_and_offline_do_not(self):
        link, hub1_fs_id, _hub2_fs_id = self._two_hub_link()

        async def send(message, redact=False, immediate=False):
            return [{
                "itemId": item["itemId"],
                "success": True,
                "payload": {"value": 0},
            } for item in message.get_items()]

        link._ws.async_sendmessage = send

        t1 = datetime(2026, 11, 14, 18, 10, 0, tzinfo=timezone.utc)
        link_mod._utc_now = lambda: t1
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": True, "connectedSince": 5000}}]
        })
        await next(iter(link._link_refresh_tasks.values()))
        self.assertEqual(link.get_link_last_event(hub1_fs_id), t1)

        # Repeat online heartbeat while already online moves last_event.
        t2 = datetime(2026, 11, 14, 18, 15, 0, tzinfo=timezone.utc)
        link_mod._utc_now = lambda: t2
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": True, "connectedSince": 5000}}]
        })
        self.assertEqual(link.get_link_last_event(hub1_fs_id), t2)

        # Older connectedSince generation does not move last_event.
        t3 = datetime(2026, 11, 14, 18, 20, 0, tzinfo=timezone.utc)
        link_mod._utc_now = lambda: t3
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": True, "connectedSince": 4000}}]
        })
        self.assertEqual(link.get_link_last_event(hub1_fs_id), t2)

        # Offline notice from server does not move last_event.
        await link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": False, "connectedSince": 6000}}]
        })
        await next(iter(link._link_grace_tasks.values()))
        self.assertEqual(link.get_link_last_event(hub1_fs_id), t2)

        # Reconnect socket-state reset keeps last_event until a fresh notification arrives.
        link._clear_link_socket_state()
        self.assertEqual(link.get_link_last_event(hub1_fs_id), t2)

    async def test_responses_do_not_set_last_event_in_consumer(self):
        link, hub1_fs_id, _hub2_fs_id = self._two_hub_link()

        class FakeSocket:
            def __init__(self, frames):
                self._frames = list(frames)

            async def receive(self):
                if self._frames:
                    return self._frames.pop(0)
                link._ws._active = False
                return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps({
                    "version": 1,
                    "transactionId": 999,
                    "direction": "response",
                    "class": "feature",
                    "operation": "write",
                    "items": [],
                }), None)

        response_frame = aiohttp.WSMessage(
            aiohttp.WSMsgType.TEXT,
            json.dumps({
                "version": 1,
                "transactionId": 1,
                "direction": "response",
                "class": "feature",
                "operation": "write",
                "items": [{
                    "itemId": 77,
                    "success": True,
                    "payload": {"featureId": "s1-10-99+2", "value": 1},
                }],
            }),
            None,
        )

        link._ws._active = True
        link._ws._websocket = FakeSocket([response_frame])
        await link._ws._consumer_handler()

        self.assertIsNone(link.get_link_last_event(hub1_fs_id))

