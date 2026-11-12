import asyncio
import unittest

import lightwave_smart.link as link_mod
from lightwave_smart.device import LWRFDevice
from lightwave_smart.link import LWLink2


class OpenSocket:
    closed = False


def _link_up(link):
    link._ws._authenticated = True
    link._ws._websocket = OpenSocket()


def _switch(link, device_id="structure-1-1+1"):
    device = LWRFDevice()
    device.link = link
    device.device_id = device_id
    device.product_code = "L21"
    device.manufacturer_code = "LightwaveRF"
    device.latest_firmware_version = "2.0.0"
    device._latest_firmware_release_id = "release-1"
    link.devices[device_id] = device
    return device


def _hub(link, device_id="structure-1-1+hub"):
    device = _switch(link, device_id)
    device.product_code = "L2"
    return device


class DeviceConnectionStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.link = LWLink2()
        _link_up(self.link)
        self.sent = []

        async def send(message, redact=False, immediate=False):
            self.sent.append(message)
            return [{"success": True, "itemId": message.get_items()[0]["itemId"]}]

        self.link._ws.async_sendmessage = send
        self._grace = link_mod._LINK_OFFLINE_GRACE_SECS

    async def asyncTearDown(self):
        link_mod._LINK_OFFLINE_GRACE_SECS = self._grace

    async def test_offline_switch_does_not_send_firmware_update(self):
        device = _switch(self.link)
        device.set_connection_status("disconnected")

        started = await device.update_firmware("2.0.0")

        self.assertFalse(started)
        self.assertEqual(self.sent, [])
        self.assertFalse(device.firmware_update_allowed())

    async def test_unknown_and_unset_status_do_not_allow_update(self):
        device = _switch(self.link)

        self.assertIsNone(device.connection_status)
        self.assertFalse(device.firmware_update_allowed())

        device.set_connection_status("unknown")
        self.assertFalse(device.firmware_update_allowed())

        device.set_connection_status("not-a-status")
        self.assertIsNone(device.connection_status)

    async def test_connected_switch_sends_firmware_update(self):
        device = _switch(self.link)
        device.set_connection_status("connected")

        started = await device.update_firmware("2.0.0")

        self.assertTrue(started)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].opclass, "firmware")
        self.assertEqual(self.sent[0].operation, "update")

    async def test_hub_uses_websocket_and_skips_device_status(self):
        device = _hub(self.link)

        started = await device.update_firmware("2.0.0")

        self.assertTrue(started)
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(device.connection_status)

    async def test_hub_does_not_send_when_websocket_is_down(self):
        device = _hub(self.link)
        self.link._ws._authenticated = False

        started = await device.update_firmware("2.0.0")

        self.assertFalse(started)
        self.assertEqual(self.sent, [])

    async def test_status_event_updates_device_and_notifies_once(self):
        device = _switch(self.link)
        seen = []
        device.register_connection_status_callback(lambda **kwargs: seen.append(kwargs["device_id"]))

        await self.link._device_status_event_handler({
            "items": [{"payload": {"deviceId": device.device_id, "status": "Connected"}}]
        })
        await self.link._device_status_event_handler({
            "items": [{"payload": {"deviceId": device.device_id, "status": "connected"}}]
        })

        self.assertEqual(device.connection_status, "connected")
        self.assertEqual(seen, [device.device_id])
        self.assertTrue(device.firmware_update_allowed())

    async def test_status_event_ignores_the_hub(self):
        device = _hub(self.link)

        await self.link._device_status_event_handler({
            "items": [{"payload": {"deviceId": device.device_id, "status": "disconnected"}}]
        })

        self.assertIsNone(device.connection_status)
        self.assertTrue(device.firmware_update_allowed())

    async def test_failed_read_clears_connected_status(self):
        device = _switch(self.link)
        device.set_connection_status("connected")

        async def send(message, redact=False, immediate=False):
            return [{"success": False, "itemId": message.get_items()[0]["itemId"], "error": {"message": "no"}}]

        self.link._ws.async_sendmessage = send

        status = await device.async_refresh_connection_status()

        self.assertIsNone(status)
        self.assertIsNone(device.connection_status)
        self.assertFalse(device.firmware_update_allowed())

    async def test_batched_read_stores_status_for_gen2_switches_only(self):
        switch = _switch(self.link, "structure-1-1+switch")
        hub = _hub(self.link, "structure-1-1+hub")
        gen1 = _switch(self.link, "structure-1-1+gen1")
        gen1.product_code = "LW100"

        async def send(message, redact=False, immediate=False):
            self.assertEqual(message.opclass, "deviceStatus")
            self.assertEqual(message.operation, "read")
            items = message.get_items()
            self.assertEqual([item["payload"]["deviceId"] for item in items], [switch.device_id])
            return [{
                "success": True,
                "itemId": items[0]["itemId"],
                "payload": {"deviceId": switch.device_id, "status": "disconnected"},
            }]

        self.link._ws.async_sendmessage = send

        await self.link._async_read_device_statuses()

        self.assertEqual(switch.connection_status, "disconnected")
        self.assertIsNone(hub.connection_status)
        self.assertIsNone(gen1.connection_status)

    async def test_missing_batch_response_clears_status(self):
        device = _switch(self.link)
        device.set_connection_status("connected")

        async def send(message, redact=False, immediate=False):
            return []

        self.link._ws.async_sendmessage = send

        await self.link._async_read_device_statuses()

        self.assertIsNone(device.connection_status)

    async def test_grace_then_disconnects_only_that_links_gen2_devices(self):
        own = _switch(self.link, "struct-1-99+1")
        own.set_connection_status("connected")
        other = _switch(self.link, "struct-1-50+1")
        other.set_connection_status("connected")
        hub = _hub(self.link, "struct-1-99+hub")
        link_mod._LINK_OFFLINE_GRACE_SECS = 0

        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": False}}]
        })
        self.assertEqual(own.connection_status, "connected")

        await next(iter(self.link._link_grace_tasks.values()))

        self.assertEqual(own.connection_status, "disconnected")
        self.assertEqual(other.connection_status, "connected")
        self.assertIsNone(hub.connection_status)
        self.assertEqual(self.link._link_socket_state.get("99"), "offline")

    async def test_online_during_grace_does_not_disconnect(self):
        device = _switch(self.link, "struct-1-99+1")
        device.set_connection_status("connected")
        link_mod._LINK_OFFLINE_GRACE_SECS = 30

        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": False}}]
        })
        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True}}]
        })
        await asyncio.sleep(0)

        self.assertEqual(device.connection_status, "connected")
        self.assertEqual(self.link._link_socket_state.get("99"), "online")
        await self.link.async_deactivate()

    async def test_in_flight_status_read_does_not_undo_the_latch(self):
        own = _switch(self.link, "struct-1-99+1")
        own.set_connection_status("connected")
        other = _switch(self.link, "struct-1-50+1")
        release = asyncio.Event()
        started = asyncio.Event()
        link_mod._LINK_OFFLINE_GRACE_SECS = 0

        async def send(message, redact=False, immediate=False):
            self.assertEqual(message.opclass, "deviceStatus")
            started.set()
            await release.wait()
            return [{
                "success": True,
                "itemId": item["itemId"],
                "payload": {"status": "connected"},
            } for item in message.get_items()]

        self.link._ws.async_sendmessage = send
        read = asyncio.create_task(self.link._async_read_device_statuses())
        await started.wait()
        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": False}}]
        })
        await next(iter(self.link._link_grace_tasks.values()))
        self.assertEqual(own.connection_status, "disconnected")

        release.set()
        await read

        self.assertEqual(own.connection_status, "disconnected")
        self.assertEqual(other.connection_status, "connected")

    async def test_activate_clears_a_latch_so_the_next_read_applies(self):
        self.link._closing = True
        self.link._link_socket_state["99"] = "offline"
        self.link._link_status_epoch["99"] = 4
        self.link._link_connected_since["99"] = 50
        self.link._link_restore_owed.add("99")
        self.link._link_feature_cycle_seen.add("99")
        self.link._ws.activate = lambda source=None: None

        async def connect(*args, **kwargs):
            return False

        self.link.async_connect = connect
        await self.link.async_activate()

        async def send(message, redact=False, immediate=False):
            return [{
                "success": True,
                "itemId": message.get_items()[0]["itemId"],
                "payload": {"status": "connected"},
            }]

        self.link._ws.async_sendmessage = send

        self.assertFalse(self.link._closing)
        self.assertEqual(self.link._link_socket_state, {})
        self.assertEqual(self.link._link_status_epoch, {})
        self.assertEqual(self.link._link_connected_since, {})
        self.assertEqual(self.link._link_restore_owed, set())
        self.assertEqual(self.link._link_feature_cycle_seen, set())

        device = _switch(self.link, "struct-1-99+1")
        device.set_connection_status("disconnected")
        await self.link._async_read_device_statuses()

        self.assertEqual(device.connection_status, "connected")

    async def test_cancelled_grace_does_not_disconnect(self):
        device = _switch(self.link, "struct-1-99+1")
        device.set_connection_status("connected")
        link_mod._LINK_OFFLINE_GRACE_SECS = 30

        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": False}}]
        })
        await self.link.async_deactivate()

        self.assertEqual(device.connection_status, "connected")
        self.assertNotEqual(self.link._link_socket_state.get("99"), "offline")

    async def test_hierarchy_returns_while_status_read_is_waiting(self):
        _switch(self.link, "struct-1-99+1")
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(message, redact=False, immediate=False):
            if message.opclass == "user":
                return [{"success": True, "payload": {"groupIds": []}}]
            if message.opclass == "deviceStatus":
                started.set()
                await release.wait()
                return []
            return []

        self.link._ws.async_sendmessage = send
        hierarchy = asyncio.create_task(self.link.async_get_hierarchy())
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(hierarchy, timeout=1)
        self.assertTrue(hierarchy.done())

        release.set()
        pending = [task for task in list(self.link._background_tasks) if not task.done()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=1)

    async def test_older_socket_offline_does_not_latch(self):
        device = _switch(self.link, "struct-1-99+1")
        device.set_connection_status("connected")
        link_mod._LINK_OFFLINE_GRACE_SECS = 0

        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True, "connectedSince": "2000"}}]
        })
        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": 99, "online": False, "connectedSince": 1000}}]
        })

        self.assertEqual(self.link._link_socket_state.get("99"), "online")
        self.assertEqual(self.link._link_grace_tasks, {})
        self.assertEqual(device.connection_status, "connected")
        await self.link.async_deactivate()

    async def test_same_generation_offline_still_disconnects(self):
        device = _switch(self.link, "struct-1-99+1")
        device.set_connection_status("connected")
        link_mod._LINK_OFFLINE_GRACE_SECS = 0

        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True, "connectedSince": 2000}}]
        })
        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": False, "connectedSince": 2000}}]
        })
        await next(iter(self.link._link_grace_tasks.values()))

        self.assertEqual(device.connection_status, "disconnected")
        self.assertEqual(self.link._link_socket_state.get("99"), "offline")
        await self.link.async_deactivate()

    async def test_offline_without_a_generation_still_disconnects(self):
        device = _switch(self.link, "struct-1-99+1")
        device.set_connection_status("connected")
        link_mod._LINK_OFFLINE_GRACE_SECS = 0

        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True, "connectedSince": 2000}}]
        })
        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": False}}]
        })
        await next(iter(self.link._link_grace_tasks.values()))

        self.assertEqual(device.connection_status, "disconnected")
        await self.link.async_deactivate()

    async def test_status_is_frozen_during_grace_and_reread_when_it_ends(self):
        device = _switch(self.link, "struct-1-99+1")
        device.set_connection_status("connected")
        link_mod._LINK_OFFLINE_GRACE_SECS = 30
        link_mod._LINK_REFRESH_DEBOUNCE_SECS = 0
        link_mod._LINK_REFRESH_RETRY_SECS = 0
        release = asyncio.Event()
        started = asyncio.Event()
        calls = {"n": 0}

        async def send(message, redact=False, immediate=False):
            if message.opclass != "deviceStatus":
                return []
            calls["n"] += 1
            if calls["n"] == 1:
                started.set()
                await release.wait()
                return [{
                    "success": False,
                    "itemId": item["itemId"],
                    "error": {"message": "down"},
                } for item in message.get_items()]
            return [{
                "success": True,
                "itemId": item["itemId"],
                "payload": {"status": "connected"},
            } for item in message.get_items()]

        self.link._ws.async_sendmessage = send
        try:
            read = asyncio.create_task(self.link._async_read_device_statuses())
            await started.wait()
            await self.link._link_socket_event_handler({
                "items": [{"payload": {"linkId": "99", "online": False, "connectedSince": 1000}}]
            })
            await self.link._device_status_event_handler({
                "items": [{"payload": {"deviceId": device.device_id, "status": "disconnected"}}]
            })
            self.assertEqual(device.connection_status, "connected")

            skipped = await self.link._async_read_device_statuses()
            self.assertTrue(skipped)
            self.assertEqual(calls["n"], 1)

            await self.link._link_socket_event_handler({
                "items": [{"payload": {"linkId": "99", "online": True, "connectedSince": 2000}}]
            })
            await next(iter(self.link._link_refresh_tasks.values()))
            release.set()
            await read

            self.assertEqual(device.connection_status, "connected")
            self.assertGreaterEqual(calls["n"], 2)
            self.assertEqual(self.link._link_socket_state.get("99"), "online")
        finally:
            link_mod._LINK_REFRESH_DEBOUNCE_SECS = 2
            link_mod._LINK_REFRESH_RETRY_SECS = 5
            await self.link.async_deactivate()

    async def test_hub_firmware_follows_the_link_latch(self):
        hub = _hub(self.link, "struct-1-99+hub")
        seen = []
        hub.register_connection_status_callback(lambda **kwargs: seen.append(kwargs["device_id"]))
        link_mod._LINK_OFFLINE_GRACE_SECS = 0

        self.assertTrue(hub.firmware_update_allowed())
        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": False}}]
        })
        self.assertEqual(self.link._link_socket_state.get("99"), "grace")
        self.assertTrue(hub.firmware_update_allowed())
        self.assertEqual(seen, [])

        await next(iter(self.link._link_grace_tasks.values()))

        self.assertFalse(hub.firmware_update_allowed())
        self.assertIsNone(hub.connection_status)
        self.assertEqual(seen, [hub.device_id])
        self.assertFalse(await hub.update_firmware("2.0.0"))
        self.assertEqual(self.sent, [])

        await self.link._link_socket_event_handler({
            "items": [{"payload": {"linkId": "99", "online": True, "connectedSince": 5}}]
        })

        self.assertTrue(hub.firmware_update_allowed())
        self.assertEqual(seen, [hub.device_id, hub.device_id])
        await self.link.async_deactivate()


if __name__ == "__main__":
    unittest.main()
