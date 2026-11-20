import asyncio
import unittest

from lightwave_smart.message import LW_WebsocketMessage
from lightwave_smart.websocket import LWWebsocket


class CompleteAndCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_twice_does_not_raise_and_sets_wait_flag(self):
        message = LW_WebsocketMessage("feature", "write")
        message.add_item({"value": 1})

        message.complete()
        self.assertTrue(message.future.done())
        self.assertTrue(message._waitflag.is_set())
        result = message.future.result()

        message._waitflag.clear()
        message.complete()

        self.assertTrue(message._waitflag.is_set())
        self.assertIs(message.future.result(), result)

    async def test_clean_up_clears_duplicate_item_with_finished_future(self):
        websocket = LWWebsocket(None)
        message = LW_WebsocketMessage("feature", "write")
        message.add_item({"value": 1})
        message.complete()

        websocket._pending_items_manager.add_item(11, message)
        websocket._pending_items_manager.add_item(12, message)

        await websocket.clean_up()

        self.assertEqual(websocket._pending_items_manager.count, 0)
        self.assertTrue(message._waitflag.is_set())
        self.assertTrue(websocket._transaction_queue.empty())


class ReconnectCallbackPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_callbacks_survive_deactivate_and_reactivate_without_duplication(self):
        websocket = LWWebsocket(None)
        events = []

        async def hierarchy_cb():
            events.append("hierarchy")

        def cloud_link_connect():
            events.append("connect")

        def cloud_link_disconnect():
            events.append("disconnect")

        websocket.register_connect_callback(hierarchy_cb)
        websocket.register_connect_callback(cloud_link_connect)
        websocket.register_disconnect_callback(cloud_link_disconnect)

        websocket.activate(source="initial")
        websocket._authenticated = True
        await websocket._notify_connect()
        self.assertEqual(events, ["hierarchy", "connect"])
        events.clear()

        # Intentional deactivate suppresses disconnect notifications and preserves callbacks.
        await websocket.async_deactivate(source="service_handle_reconnect")
        await websocket._notify_disconnect()
        self.assertEqual(events, [])

        # Re-activating and passing connect_callback again must not duplicate hierarchy_cb.
        websocket.activate(source="service_handle_reconnect")
        websocket.register_connect_callback(hierarchy_cb)
        websocket._authenticated = True
        await websocket._notify_connect()
        self.assertEqual(events, ["hierarchy", "connect"])
        events.clear()

        # Subsequent unintentional drop notifies disconnect listeners.
        await websocket._notify_disconnect()
        self.assertEqual(events, ["disconnect"])
        events.clear()

        websocket.unregister_connect_callback(cloud_link_connect)
        websocket.unregister_disconnect_callback(cloud_link_disconnect)
        websocket._authenticated = True
        await websocket._notify_connect()
        await websocket._notify_disconnect()
        self.assertEqual(events, ["hierarchy"])

        await websocket.async_deactivate(source="cleanup")


class TransactionQueueCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_deactivate_finishes_the_transaction_queue(self):
        websocket = LWWebsocket(None)
        websocket.activate(source="test")
        await websocket.async_deactivate(source="test")
        await asyncio.sleep(0)
        self.assertFalse(websocket._active)
        self.assertEqual(websocket.background_tasks, set())

    async def test_cancel_while_active_restarts_the_transaction_queue(self):
        websocket = LWWebsocket(None)
        websocket.activate(source="test")
        task = next(
            item for item in websocket.background_tasks
            if item.get_name() == "transaction_queue"
        )
        sent = []

        async def fake_send(message):
            sent.append(message)

        websocket._send_message_to_websocket = fake_send
        task.cancel()
        _, pending = await asyncio.wait({task}, timeout=1)
        self.assertEqual(pending, set())
        self.assertTrue(task.cancelled())

        await asyncio.sleep(0)
        replacement = next(
            item for item in websocket.background_tasks
            if item.get_name() == "transaction_queue"
        )
        self.assertIsNot(replacement, task)
        self.assertFalse(replacement.done())

        message = LW_WebsocketMessage("feature", "read")
        await websocket._transaction_queue.put(message)
        for _ in range(50):
            if sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(sent, [message])
        await websocket.async_deactivate(source="cleanup")

