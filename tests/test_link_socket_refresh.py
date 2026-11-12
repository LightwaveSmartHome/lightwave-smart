import asyncio
import unittest

import lightwave_smart.link as link_mod
from lightwave_smart.device import LWRFFeature
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
