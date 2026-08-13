""" tests / unit / test_output_mqtt.py """
import json
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
        tombstones = [msg for msg in config_msgs if msg["payload"] == ""]
        self.assertEqual(len(tombstones), 1)
        self.assertTrue(tombstones[0]["topic"].startswith("homeassistant/sensor/mpp_"))
        self.assertTrue(tombstones[0]["topic"].endswith("battery_voltage/config"))
        self.assertTrue(tombstones[0]["retain"])
