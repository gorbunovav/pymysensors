"""Handle sensor classes."""
import logging
from collections import deque
from os import chown

import voluptuous as vol
from voluptuous.humanize import humanize_error

from .const import get_const
from .message import Message
from .validation import is_battery_level, is_heartbeat, safe_is_version

_LOGGER = logging.getLogger(__name__)


class Sensor:
    """Represent a sensor."""

    # pylint: disable=too-many-instance-attributes

    def __init__(self, sensor_id):
        """Set up sensor."""
        self.sensor_id = sensor_id
        self.children = {}
        self.type = None
        self.sketch_name = None
        self.sketch_version = None
        self._battery_level = 0
        self._protocol_version = "1.4"
        self._heartbeat = 0
        self.new_state = {}
        self.queue = deque()
        self.reboot = False

    def __getstate__(self):
        """Get state to save as pickle."""
        state = self.__dict__.copy()
        for attr in ("_battery_level", "_heartbeat", "_protocol_version"):
            value = state.pop(attr, None)
            prop = attr
            if prop.startswith("_"):
                prop = prop[1:]
            if value is not None:
                state[prop] = value

        return state

    def __setstate__(self, state):
        """Set state when loading pickle."""
        # Restore instance attributes
        for key, val in state.items():
            setattr(self, key, val)
        # Reset some attributes
        self.new_state = {}
        self.queue = deque()
        self.reboot = False
        if "_heartbeat" not in self.__dict__:
            self.heartbeat = 0

    def __repr__(self):
        """Return the representation."""
        return f"<Sensor sensor_id={self.sensor_id}, children: {self.children}>"

    @property
    def battery_level(self):
        """Return battery level."""
        return self._battery_level

    @battery_level.setter
    def battery_level(self, value):
        """Set valid battery level."""
        self._battery_level = is_battery_level(value)

    @property
    def heartbeat(self):
        """Return heartbeat value."""
        return self._heartbeat

    @heartbeat.setter
    def heartbeat(self, value):
        """Set valid heartbeat value."""
        self._heartbeat = is_heartbeat(value)

    @property
    def is_smart_sleep_node(self):
        """Return True if the node uses smart sleep mode."""
        return bool(self.new_state)

    @property
    def protocol_version(self):
        """Return protocol version."""
        return self._protocol_version

    @protocol_version.setter
    def protocol_version(self, value):
        """Set valid protocol version."""
        self._protocol_version = safe_is_version(value)

    def add_child_sensor(self, child_id, child_type, description=""):
        """Create and add a child sensor."""
        if child_id in self.children:
            _LOGGER.warning(
                "child_id %s already exists in children of node %s, "
                "cannot add child",
                child_id,
                self.sensor_id,
            )
            return None
        self.children[child_id] = ChildSensor(child_id, child_type, description)
        return child_id

    def get_desired_value(self, child_id, value_type):
        """Return sensor state value taking into account node type."""
        if self.children[child_id] is None:
            return None

        value = None

        if self.is_smart_sleep_node:
            child = self.new_state[child_id]
            value = child.values.get(value_type) if child else None

        if value:
            return value

        child = self.children[child_id]

        return child.values.get(value_type)

    def init_smart_sleep_mode(self):
        """Init desired state dict for all known children."""
        for child in self.children.values():
            if child.id in self.new_state:
                continue

            self.new_state[child.id] = ChildSensor(
                child.id, child.type, child.description
            )

    def set_child_desired_state(self, child_id, value_type, value):
        """Set a desired child sensor's value for smart sleep nodes."""
        if child_id not in self.new_state:
            _LOGGER.warning(
                "Warning: attempt to set a desired state value"
                " on non-smart sleep node: node %s, child %s, type %s, value %s",
                self.sensor_id,
                child_id,
                value_type,
                value,
            )
            return

        if not self.validate_child_state(child_id, value_type, value):
            _LOGGER.warning("Unable to set desired child sate")
            return

        child = self.new_state[child_id]
        child.values[value_type] = value

    def update_child_value(self, child_id, value_type, value):
        """Update a child sensor's local state."""
        child = self.children[child_id]
        child.values[value_type] = value

        if child_id not in self.new_state:
            return

        # New sate received from the node -
        # we can clear the desired state value to indicate that no changes are required
        new_state_child = self.new_state[child_id]

        new_state_child.values[value_type] = None

    def validate_child_state(self, child_id, value_type, value):
        """Check if we will be able to generate a set message from these values."""
        const = get_const(self.protocol_version)

        msg_type = const.MessageType.set

        msg = Message(
            node_id=self.sensor_id,
            child_id=child_id,
            type=msg_type,
            sub_type=value_type,
            payload=value,
        )

        msg_string = msg.encode()

        if msg_string is None:
            _LOGGER.error(
                "Not a valid state: node %s, child %s, type %s, "
                "sub_type %s, payload %s",
                self.sensor_id,
                child_id,
                msg_type,
                value_type,
                value,
            )
            return False

        try:
            msg.validate(self.protocol_version)
        except AttributeError as exc:
            _LOGGER.error("Invalid %s: %s", msg, exc)
            return False
        except vol.Invalid as exc:
            _LOGGER.error("Invalid %s: %s", msg, humanize_error(msg.__dict__, exc))
            return False

        return True


class ChildSensor:
    """Represent a child sensor."""

    def __init__(self, child_id, child_type, description=""):
        """Set up child sensor."""
        # pylint: disable=invalid-name
        self.id = child_id
        self.type = child_type
        self.description = description
        self.values = {}

    def __setstate__(self, state):
        """Set state when loading pickle."""
        # Restore instance attributes
        self.__dict__.update(state)
        # Make sure all attributes exist
        if "description" not in self.__dict__:
            self.description = ""

    def __repr__(self):
        """Return the representation."""
        return (
            f"<ChildSensor child_id={self.id}, child_type={self.type}, "
            f"description={self.description}, values: {self.values}>"
        )

    def get_schema(self, protocol_version):
        """Return the child schema for the correct const version."""
        const = get_const(protocol_version)
        custom_schema = vol.Schema(
            {
                typ.value: const.VALID_SETREQ[typ]
                for typ in const.VALID_TYPES[const.Presentation.S_CUSTOM]
            }
        )
        return custom_schema.extend(
            {typ.value: const.VALID_SETREQ[typ] for typ in const.VALID_TYPES[self.type]}
        )

    def validate(self, protocol_version, values=None):
        """Validate child value types and values against protocol_version."""
        if values is None:
            values = self.values
        return self.get_schema(protocol_version)(values)
