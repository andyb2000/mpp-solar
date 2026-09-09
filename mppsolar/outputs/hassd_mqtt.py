import json as js
import logging
import os
import re
from datetime import datetime

from ..helpers import get_kwargs, key_wanted
from .mqtt import mqtt

log = logging.getLogger("hassd_mqtt")

# Where the last-known set of state topics is cached between daemon restarts,
# so a validity check failure right after startup can still mark previously
# published entities unavailable, instead of leaving their last-good reading
# (from before the restart) showing forever. Overridable via config: state_dir.
DEFAULT_STATE_DIR = "/var/tmp/mppsolar"


class hassd_mqtt(mqtt):
    def __str__(self):
        return """outputs the to the supplied mqtt broker in hass format: eg "homeassistant/sensor/mpp_{tag}_{key}/state" """

    def __init__(self, *args, **kwargs) -> None:
        log.debug(f"__init__: kwargs {kwargs}")
        self._known_state_topics = []
        self._state_file = None
        self._state_loaded = False

    def _resolve_state_file(self, state_dir, device_id, tag):
        """Path used to persist known state topics across process restarts, unique per device+tag."""
        state_dir = state_dir or DEFAULT_STATE_DIR
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{device_id}_{tag}").strip("_") or "mppsolar"
        return os.path.join(state_dir, f"hassd_mqtt_{safe_name}.json")

    def _load_known_state_topics(self):
        """Recover the topic list saved by a previous run of this process, once per instance."""
        if self._state_loaded:
            return
        self._state_loaded = True
        try:
            with open(self._state_file, "r") as f:
                topics = js.load(f)
            if isinstance(topics, list) and not self._known_state_topics:
                self._known_state_topics = topics
                log.debug(f"Restored {len(topics)} known state topics from {self._state_file}")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning(f"Could not read hassd_mqtt state file {self._state_file}: {e}")

    def _save_known_state_topics(self):
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "w") as f:
                js.dump(self._known_state_topics, f)
        except OSError as e:
            log.warning(f"Could not persist hassd_mqtt state file {self._state_file}: {e}")

    def build_msgs(self, *args, **kwargs):
        log.debug(f"kwargs {kwargs}")
        data = get_kwargs(kwargs, "data")
        if data is None:
            return [], []

        # Identify device/tag up front (independent of success/failure) so we can
        # locate this instance's on-disk state cache before deciding what to do.
        config = get_kwargs(kwargs, "config")
        if config is not None:
            fullconfig = get_kwargs(kwargs, "fullconfig")
            early_tag = config.get("tag", None) or "mppsolar"
            early_device_id = (fullconfig or {}).get("device", {}).get("id", "mppsolar")
        else:
            early_tag = get_kwargs(kwargs, "tag") or "mppsolar"
            early_device_id = get_kwargs(kwargs, "name", "mppsolar")
        # state_dir may arrive as its own kwarg (set via [SETUP] state_dir= in the
        # mpp-solar.conf config file, see docs/configfile.md) or, for the config-dict
        # style callers, inside the per-output config mapping.
        state_dir = get_kwargs(kwargs, "state_dir")
        if state_dir is None and config is not None:
            state_dir = config.get("state_dir")
        if self._state_file is None:
            self._state_file = self._resolve_state_file(state_dir, early_device_id, early_tag)
        self._load_known_state_topics()

        # Capture this before it's popped/mutated below - it decides which branch
        # (failure vs success) the rest of this method takes.
        validity_msg = data.get("validity check")

        # Clean data
        command = data.pop("_command", None)
        data.pop("_command_description", None)
        data.pop("raw_response", None)

        # check if config supplied
        config = get_kwargs(kwargs, "config")
        if config is not None:
            log.debug(f"config: {config}")
            # try for fullconfig
            fullconfig = get_kwargs(kwargs, "fullconfig")
            # get results topic
            # results_topic = config.get("results_topic", None)
            # get formatting info
            remove_spaces = config.get("remove_spaces", True)
            keep_case = config.get("keep_case", False)
            filter = config.get("filter", None)
            excl_filter = config.get("excl_filter", None)
            tag = config.get("tag", None)
            device = fullconfig.get("device", {})
            device_name = device.get("name", "mppsolar")
            device_id = device.get("id", "mppsolar")
            device_model = device.get("model", "mppsolar")
            device_manufacturer = device.get("manufacturer", "mppsolar")
        else:
            # results_topic = None
            # get formatting info
            remove_spaces = True
            keep_case = get_kwargs(kwargs, "keep_case")
            filter = get_kwargs(kwargs, "filter")
            excl_filter = get_kwargs(kwargs, "excl_filter")
            tag = get_kwargs(kwargs, "tag")
            device_name = get_kwargs(kwargs, "name", "mppsolar")
            device_id = device_name
            device_model = device_name
            device_manufacturer = "MPP-Solar"

        if filter is not None:
            filter = re.compile(filter)
        if excl_filter is not None:
            excl_filter = re.compile(excl_filter)
        if tag is None:
            if command:
                tag = command
            else:
                tag = "mppsolar"

        def _status_key(raw_key):
            """Apply the same key transforms the main loop below uses, so a
            status topic computed here matches the one that loop would build."""
            key = raw_key
            if remove_spaces:
                key = key.replace(" ", "_")
            if not keep_case:
                key = key.lower()
            return key

        if validity_msg is not None:
            # Protocol-level decode failure (bad CRC, short/empty response, ...).
            # Refresh the "validity_check" status sensor with the failure text
            # itself (mirroring the "ERROR" status sensor below) instead of just
            # marking everything else unavailable and leaving this sensor
            # showing whatever it last said - otherwise, once the device
            # recovers, nothing would ever touch this topic again either, and
            # it's left stuck on a historic fault forever.
            log.warning(f"validity check failed ({validity_msg}), marking known state topics unavailable")
            validity_topic = f"homeassistant/sensor/mpp_{tag}_{_status_key('validity check')}/state"
            error_topic = f"homeassistant/sensor/mpp_{tag}_{_status_key('ERROR')}/state"
            error_payload = data.get("ERROR", ["OK", ""])[0]

            # Previously known *real* topics (excludes the two status topics
            # themselves, which get their own fresh value below) still need
            # marking unavailable - see the module-level comment on why.
            previously_known = [t for t in self._known_state_topics if t not in (validity_topic, error_topic)]

            value_msgs = [
                {"topic": validity_topic, "payload": validity_msg[0], "retain": False},
                {"topic": error_topic, "payload": error_payload, "retain": False},
            ]
            value_msgs.extend(
                {"topic": topic, "payload": "unavailable", "retain": False} for topic in previously_known
            )
            for topic in (validity_topic, error_topic, *previously_known):
                if topic not in self._known_state_topics:
                    self._known_state_topics.append(topic)
            self._save_known_state_topics()
            return [], value_msgs

        # device.py reports a failed command (timeout, decode error, retries
        # exhausted, ...) as a single {"ERROR": [msg, ""]} entry, which becomes
        # a normal text sensor below (eg sensor.mpp_{tag}_error) - that part is
        # wanted. But once the inverter starts responding again, a successful
        # response has no "ERROR" key at all, so that sensor (and the
        # "validity_check" one handled above) would never be republished and
        # would keep showing the old error text forever. Explicitly mark both
        # "OK" on every cycle that isn't itself a failure, so they behave like
        # any other sensor - always refreshed.
        if "ERROR" not in data:
            data["ERROR"] = ["OK", ""]
        data["validity check"] = ["OK", ""]

        # Build array of mqtt messages with hass update format
        config_msgs = []
        value_msgs = []
        self._known_state_topics = []
        device_components = {}
        safe_device_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(device_id)).strip("_") or "mppsolar"

        # Loop through responses
        for key, values in data.items():
            orig_key = key
            value = values[0]
            unit = values[1]
            if len(values) > 2 and values[2] and "unit" in values[2]:
                unit = values[2]["unit"]
                
            icon = None
            if len(values) > 2 and values[2] and "icon" in values[2]:
                icon = values[2]["icon"]
            device_class = None

            if len(values) > 2 and values[2] and "device-class" in values[2]:
                device_class = values[2]["device-class"]
            state_class = None
            if len(values) > 2 and values[2] and "state_class" in values[2]:
                state_class = values[2]["state_class"]

            # remove spaces
            if remove_spaces:
                key = key.replace(" ", "_")
            if not keep_case:
                # make lowercase
                key = key.lower()
            if key_wanted(key, filter, excl_filter):
                #
                # CONFIG / AUTODISCOVER
                #
                # <discovery_prefix>/<component>/[<node_id>/]<object_id>/config
                # topic "homeassistant/binary_sensor/garden/config"
                # msg '{"name": "garden", "device_class": "motion", "state_topic": "homeassistant/binary_sensor/garden/state", "unit_of_measurement": "°C", "icon": "power-plug"}'

                # For binary sensors
                if unit == "bool" or value == "enabled" or value == "disabled":
                    sensor = "binary_sensor"
                    if value == 0 or value == "0" or value == "disabled":
                        # for QPIWS one can add [or tag == "myQPIWStag"], if there's a QPIWS section in mpp-solar.conf
                        value = "OFF"
                    elif value == 1 or value == "1" or value == "enabled":
                        value = "ON"
                else:
                    sensor = "sensor"
                component_id = f"mpp_{tag}_{key}"
                component_cfg = {
                    "p": sensor,
                    "name": f"{orig_key}",
                    "state_topic": f"homeassistant/{sensor}/mpp_{tag}_{key}/state",
                    "unique_id": component_id,
                    "force_update": True,
                }
                if unit and unit != "bool":
                    component_cfg["unit_of_measurement"] = f"{unit}"
                if device_class:
                    component_cfg["device_class"] = device_class
                if state_class:
                    component_cfg["state_class"] = state_class
                if icon:
                    component_cfg["icon"] = icon
                if unit == "Hz":
                    component_cfg["device_class"] = "frequency"
                if unit in ["A", "mA"]:
                    component_cfg["device_class"] = "current"
                if unit in ["V", "mV"]:
                    component_cfg["device_class"] = "voltage"
                if unit in ["W", "kW"]:
                    component_cfg["device_class"] = "power"
                if unit == "Wh" or unit == "kWh":
                    component_cfg.update(
                        {
                            "icon": "mdi:counter",
                            "device_class": "energy",
                            "state_class": "total_increasing",
                            "last_reset": str(datetime.now()),
                        }
                    )
                device_components[component_id] = component_cfg
                config_msgs.append(
                    {
                        "topic": f"homeassistant/{sensor}/mpp_{tag}_{key}/config".replace(" ", "_"),
                        "payload": "",
                        "retain": True,
                    }
                )
                # VALUE SETTING
                topic = f"homeassistant/{sensor}/mpp_{tag}_{key}/state"
                if topic not in self._known_state_topics:
                    self._known_state_topics.append(topic)
                # State messages are time-sensitive — do not retain stale values
                msg = {"topic": topic, "payload": value, "retain": False}
                value_msgs.append(msg)

        if device_components:
            device_payload = {
                "device": {
                    "name": device_name,
                    "identifiers": [device_id],
                    "model": device_model,
                    "manufacturer": device_manufacturer,
                },
                "origin": {"name": "MPP-Solar", "sw": "dev"},
                "components": device_components,
            }
            config_msgs.append(
                {
                    "topic": f"homeassistant/device/{safe_device_id}/config",
                    "payload": js.dumps(device_payload),
                    "retain": True,
                }
            )
        if self._known_state_topics:
            self._save_known_state_topics()
        return config_msgs, value_msgs

    def output(self, *args, **kwargs):
        """Over write mqtt output as we want to send config msgs first...."""
        log.info("Using output processor: hassd_mqtt")
        log.debug(f"kwargs {kwargs}")
        data = get_kwargs(kwargs, "data")
        # exit if no data
        if data is None:
            return

        # Get device name for MQTT routing
        device_name = get_kwargs(kwargs, "name", "mppsolar")

        # get the broker instance
        mqtt_broker = get_kwargs(kwargs, "mqtt_broker")
        # exit if no broker
        if mqtt_broker is None:
            return

        # build the messages...
        config_msgs, value_msgs = self.build_msgs(**kwargs)
        log.debug(f"hassd_mqtt.output config_msgs {config_msgs}")
        log.debug(f"hassd_mqtt.output value_msgs {value_msgs}")

        # publish config msgs first, then value msgs — queue preserves order
        mqtt_broker.publishMultiple(config_msgs, device_name=device_name)
        mqtt_broker.publishMultiple(value_msgs, device_name=device_name)
