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
