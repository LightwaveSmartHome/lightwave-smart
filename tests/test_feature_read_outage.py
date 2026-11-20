import unittest

from lightwave_smart.device import LWRFFeature
from lightwave_smart.link import LWLink2

LINK_A = "99"
LINK_B = "100"
FEATURE_A = "s1-10-99+2"
FEATURE_A_OTHER = "s1-11-99+2"
FEATURE_B = "s1-10-100+1"
FEATURE_CLOSED = "s1-12-101+1"


def _feature(link, feature_id):
    return LWRFFeature(
        feature_id,
        {"attributes": {"type": "switch"}},
        link,
    )


def _channel_down(message):
    return {"success": False, "error": {"message": message}}


def _warnings(output):
    return [
        line for line in output
        if "Feature reads failing while the Lightwave channel is down" in line
    ]


def _outage_debugs(output):
    return [
        line for line in output
        if "Feature read failed while the Lightwave channel is down" in line
    ]


class FeatureReadOutageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.link = LWLink2()
        self.feature_a = _feature(self.link, FEATURE_A)
        self.feature_a_other = _feature(self.link, FEATURE_A_OTHER)
        self.feature_b = _feature(self.link, FEATURE_B)

    async def asyncTearDown(self):
        await self.link.async_deactivate(source="test")

    def test_transient_failure_warns_once_per_link_then_debugs(self):
        destination_down = _channel_down("dispatcher: destination not connected")
        channel_closed = _channel_down("websocket: Channel was closed")
        with self.assertLogs("lightwave_smart", level="DEBUG") as captured:
            self.feature_a.process_feature_read(destination_down)
            self.feature_a.process_feature_read(destination_down)
            self.feature_a_other.process_feature_read(channel_closed)

        warnings = _warnings(captured.output)
        self.assertEqual(len(warnings), 1)
        self.assertIn("WARNING", warnings[0])
        self.assertIn(f"link {LINK_A}", warnings[0])
        self.assertIn(FEATURE_A, warnings[0])
        self.assertEqual(
            sum("failed to read feature" in line for line in captured.output),
            0,
        )
        debugs = _outage_debugs(captured.output)
        self.assertEqual(len(debugs), 2)
        self.assertTrue(all("DEBUG" in line for line in debugs))
        self.assertIn(FEATURE_A, debugs[0])
        self.assertIn(FEATURE_A_OTHER, debugs[1])

    def test_another_link_still_warns(self):
        down = _channel_down("dispatcher: destination not connected")
        with self.assertLogs("lightwave_smart.link", level="WARNING") as captured:
            self.feature_a.process_feature_read(down)
            self.feature_b.process_feature_read(down)

        warnings = _warnings(captured.output)
        self.assertEqual(len(warnings), 2)
        self.assertIn(f"link {LINK_A}", warnings[0])
        self.assertIn(FEATURE_A, warnings[0])
        self.assertIn(f"link {LINK_B}", warnings[1])
        self.assertIn(FEATURE_B, warnings[1])

    def test_channel_was_closed_is_a_transient_outage(self):
        feature = _feature(self.link, FEATURE_CLOSED)
        down = _channel_down("websocket: Channel was closed")
        with self.assertLogs("lightwave_smart", level="DEBUG") as captured:
            feature.process_feature_read(down)
            feature.process_feature_read(down)

        warnings = _warnings(captured.output)
        self.assertEqual(len(warnings), 1)
        self.assertIn("link 101", warnings[0])
        self.assertIn(FEATURE_CLOSED, warnings[0])
        self.assertEqual(
            sum("failed to read feature" in line for line in captured.output),
            0,
        )
        debugs = _outage_debugs(captured.output)
        self.assertEqual(len(debugs), 1)

    def test_other_failures_still_warn_each_time(self):
        failed = _channel_down("feature missing")
        with self.assertLogs("lightwave_smart.device", level="WARNING") as captured:
            self.feature_a.process_feature_read(failed)
            self.feature_a.process_feature_read(failed)

        self.assertEqual(len(captured.output), 2)
        self.assertTrue(all("failed to read feature" in line for line in captured.output))

    async def test_connect_clears_every_link(self):
        down = _channel_down("hookManager: Disconnected before a reply")
        self.feature_a.process_feature_read(down)
        self.feature_b.process_feature_read(down)

        self.link._ws._authenticated = True
        await self.link._ws._notify_connect()

        with self.assertLogs("lightwave_smart.link", level="WARNING") as captured:
            self.feature_a.process_feature_read(down)
            self.feature_b.process_feature_read(down)

        warnings = _warnings(captured.output)
        self.assertEqual(len(warnings), 2)
        self.assertIn(f"link {LINK_A}", warnings[0])
        self.assertIn(f"link {LINK_B}", warnings[1])

    async def test_link_online_clears_only_that_link(self):
        down = _channel_down("dispatcher: destination not connected")
        self.feature_a.process_feature_read(down)
        self.feature_b.process_feature_read(down)

        await self.link._link_socket_event_handler({
            "items": [{
                "payload": {"online": True, "linkId": LINK_A, "connectedSince": 1},
            }],
        })

        with self.assertLogs("lightwave_smart", level="DEBUG") as captured:
            self.feature_a.process_feature_read(down)
            self.feature_b.process_feature_read(down)

        warnings = _warnings(captured.output)
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"link {LINK_A}", warnings[0])
        self.assertIn(FEATURE_A, warnings[0])
        debugs = _outage_debugs(captured.output)
        self.assertEqual(len(debugs), 1)
        self.assertIn(f"link {LINK_B}", debugs[0])
        self.assertIn(FEATURE_B, debugs[0])

        await self.link._link_socket_event_handler({
            "items": [{
                "payload": {"online": True, "linkId": LINK_A, "connectedSince": 1},
            }],
        })

        with self.assertLogs("lightwave_smart", level="DEBUG") as captured:
            self.feature_a.process_feature_read(down)

        self.assertEqual(_warnings(captured.output), [])
        debugs = _outage_debugs(captured.output)
        self.assertEqual(len(debugs), 1)
        self.assertIn(f"link {LINK_A}", debugs[0])
