""" tests / unit / test_output_mqtt.py """
import json
import shutil
import tempfile
import unittest

from mppsolar.outputs.hass_mqtt import hass_mqtt
from mppsolar.outputs.hassd_mqtt import hassd_mqtt
from mppsolar.outputs.mqtt import mqtt


class TestMqttOutput(unittest.TestCase):
    """ test the mqtt output module """
    maxDiff = 9999

    def test_mqtt_msg(self):
        """ test the mqtt msg build """
        result = []
        # Get a mqtt output processor
        # op = get_outputs("mqtt")[0]
        tag = "test"
        data = {
            "raw_response": [
                "(1 92931701100510 B  0141 005 51.4 Ì#\r",
                "",
            ],
            "_command": "QPGS0",
            "_command_description": "Parallel Information inquiry",
            "Battery voltage": [51.4, "V"],
        }

        result = mqtt().build_msgs(
            data=data, tag=tag, keep_case=False, filter=None, excl_filter=None
        )

        # needed to initialise variables
        expected = [
            {"topic": f"{tag}/status/battery_voltage/value", "payload": 51.4},
            {"topic": f"{tag}/status/battery_voltage/unit", "payload": "V"},
        ]

        # print(result)
        self.assertEqual(result, expected)

    def test_hassd_mqtt_force_update_is_boolean(self):
        """Home Assistant discovery payloads must use JSON booleans, not strings."""
        data = {"Battery voltage": [51.4, "V"]}

        config_msgs, _ = hassd_mqtt().build_msgs(
            data=data,
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
            config={"remove_spaces": True, "keep_case": False},
            fullconfig={"device": {"name": "mppsolar", "id": "mppsolar"}},
        )

        payload = json.loads(next(msg["payload"] for msg in config_msgs if msg["topic"].startswith("homeassistant/device/")))
        component = next(iter(payload["components"].values()))
        self.assertIsInstance(component["force_update"], bool)
        self.assertTrue(component["force_update"])

        msgs = hass_mqtt().build_msgs(
            data={"Battery voltage": [51.4, "V"]},
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
        )
        payload = json.loads(msgs[0]["payload"])
        self.assertIsInstance(payload["force_update"], bool)
        self.assertTrue(payload["force_update"])

    def test_hassd_mqtt_device_discovery_has_components(self):
        """Home Assistant prefers device discovery with a shared device and components map."""
        data = {"Battery voltage": [51.4, "V"]}

        config_msgs, _ = hassd_mqtt().build_msgs(
            data=data,
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
            config={"remove_spaces": True, "keep_case": False},
            fullconfig={"device": {"name": "solar", "id": "solar"}},
        )

        device_topic = next(
            msg["topic"] for msg in config_msgs if msg["topic"].startswith("homeassistant/device/")
        )
        self.assertEqual(device_topic, "homeassistant/device/solar/config")

        payload = json.loads(next(msg["payload"] for msg in config_msgs if msg["topic"] == device_topic))
        self.assertIn("device", payload)
        self.assertIn("components", payload)
        self.assertTrue(any(key.endswith("battery_voltage") for key in payload["components"]))
        battery_component = next(
            component for key, component in payload["components"].items() if key.endswith("battery_voltage")
        )
        self.assertEqual(battery_component["name"], "Battery voltage")
        # A successful cycle also carries an always-present "ERROR" -> "OK" status
        # sensor (see build_msgs), so its own config tombstone is expected here too.
        tombstones = [msg for msg in config_msgs if msg["payload"] == ""]
        self.assertEqual(len(tombstones), 2)
        battery_tombstone = next(t for t in tombstones if t["topic"].endswith("battery_voltage/config"))
        self.assertTrue(battery_tombstone["topic"].startswith("homeassistant/sensor/mpp_"))
        self.assertTrue(battery_tombstone["retain"])

    def test_hassd_mqtt_response_validity_failure_sets_unavailable(self):
        """A bad protocol response should mark known state topics unavailable until valid readings return."""
        processor = hassd_mqtt()
        processor.build_msgs(
            data={"Battery voltage": [51.4, "V"]},
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
            config={"remove_spaces": True, "keep_case": False, "tag": "test"},
            fullconfig={"device": {"name": "mppsolar", "id": "mppsolar"}},
        )

        config_msgs, value_msgs = processor.build_msgs(
            data={"validity check": ["Error: Response to short", ""]},
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
            config={"remove_spaces": True, "keep_case": False, "tag": "test"},
            fullconfig={"device": {"name": "mppsolar", "id": "mppsolar"}},
        )

        self.assertEqual(config_msgs, [])
        self.assertIn(
            {"topic": "homeassistant/sensor/mpp_test_battery_voltage/state", "payload": "unavailable", "retain": False},
            value_msgs,
        )

    def test_hassd_mqtt_error_sensor_clears_once_device_responds_again(self):
        """device.py reports a failed command as a single {"ERROR": [msg, ""]} entry
        (not the "validity check" key), which becomes a normal text sensor. Once the
        device responds again that sensor must be refreshed to "OK" rather than left
        showing the old error message forever - it's never in a later cycle's data,
        so nothing would otherwise republish its topic."""
        processor = hassd_mqtt()
        config = {"remove_spaces": True, "keep_case": False, "tag": "test"}
        fullconfig = {"device": {"name": "mppsolar", "id": "mppsolar"}}

        _, value_msgs = processor.build_msgs(
            data={"ERROR": ["Command QPIGS failed after 3 attempts. Last error: timeout", ""]},
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
            config=config,
            fullconfig=fullconfig,
        )
        self.assertIn(
            {
                "topic": "homeassistant/sensor/mpp_test_error/state",
                "payload": "Command QPIGS failed after 3 attempts. Last error: timeout",
                "retain": False,
            },
            value_msgs,
        )

        _, value_msgs = processor.build_msgs(
            data={"Battery voltage": [51.4, "V"]},
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
            config=config,
            fullconfig=fullconfig,
        )
        self.assertIn(
            {"topic": "homeassistant/sensor/mpp_test_error/state", "payload": "OK", "retain": False},
            value_msgs,
        )

    def test_hassd_mqtt_state_dir_kwarg_used_when_no_config_dict(self):
        """The daemon's ini-file main loop calls output() with a plain state_dir=
        kwarg (no config dict) - make sure that path is honoured too."""
        state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state_dir, ignore_errors=True)

        first_run = hassd_mqtt()
        first_run.build_msgs(
            data={"Battery voltage": [51.4, "V"]},
            tag="test",
            name="mppsolar",
            keep_case=False,
            filter=None,
            excl_filter=None,
            state_dir=state_dir,
        )

        second_run = hassd_mqtt()
        config_msgs, value_msgs = second_run.build_msgs(
            data={"validity check": ["Error: Unable to connect to device", ""]},
            tag="test",
            name="mppsolar",
            keep_case=False,
            filter=None,
            excl_filter=None,
            state_dir=state_dir,
        )

        self.assertEqual(config_msgs, [])
        self.assertIn(
            {"topic": "homeassistant/sensor/mpp_test_battery_voltage/state", "payload": "unavailable", "retain": False},
            value_msgs,
        )

    def test_hassd_mqtt_restores_known_topics_after_restart(self):
        """A fresh process (eg after a daemon restart) that immediately fails to connect
        should still be able to mark the topics from its *previous* run unavailable,
        by recovering them from the on-disk state cache rather than starting blank."""
        state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state_dir, ignore_errors=True)
        config = {"remove_spaces": True, "keep_case": False, "tag": "test", "state_dir": state_dir}
        fullconfig = {"device": {"name": "mppsolar", "id": "mppsolar"}}

        # First process: gets one good reading, persisting its known topics to disk.
        first_run = hassd_mqtt()
        first_run.build_msgs(
            data={"Battery voltage": [51.4, "V"]},
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
            config=config,
            fullconfig=fullconfig,
        )

        # Second process (simulating a restart): brand new instance, no in-memory history,
        # and the very first thing it sees is a validity check failure.
        second_run = hassd_mqtt()
        config_msgs, value_msgs = second_run.build_msgs(
            data={"validity check": ["Error: Unable to connect to device", ""]},
            tag="test",
            keep_case=False,
            filter=None,
            excl_filter=None,
            config=config,
            fullconfig=fullconfig,
        )

        self.assertEqual(config_msgs, [])
        self.assertIn(
            {"topic": "homeassistant/sensor/mpp_test_battery_voltage/state", "payload": "unavailable", "retain": False},
            value_msgs,
        )
