""" daemon.py """
import logging
import os
from enum import Enum, auto
from time import time

try:
    from cysystemd import journal
    from cysystemd.daemon import Notification, notify
except ModuleNotFoundError as exception:
    print(f"error: {exception}, try 'pip install cysystemd' (which may need 'apt install build-essential libsystemd-dev'), see https://pypi.org/project/cysystemd/ for further info")
    exit(1)

from mppsolar.daemon import DaemonType
from mppsolar.daemon.daemon import Daemon
# Set-up logger
log = logging.getLogger("daemon")

class DaemonSystemd(Daemon):
    """ abstraction to support different daemon approaches / solutions """
    def __str__(self):
        return f"Daemon name: {self.type}"

    def __init__(self):
        self.type = DaemonType.SYSTEMD
        self.keepalive = 60
        self._warned_notify_unavailable = False
        log.debug(f"got daemon type: {self.type}, keepalive: {self.keepalive}")

        self._notify = self._safe_notify
        self._journal = journal.write
        self._Notification = Notification

        self.notify(f"got daemon type: {self.type}, keepalive: {self.keepalive}")

    def _safe_notify(self, notification, message=None):
        """Send systemd notification only when socket is available."""
        if not os.environ.get("NOTIFY_SOCKET"):
            if not self._warned_notify_unavailable:
                log.info("NOTIFY_SOCKET is not set; skipping systemd notifications")
                self._warned_notify_unavailable = True
            return False

        try:
            if message is None:
                return notify(notification)
            return notify(notification, message)
        except Exception as e:
            # Keep daemon loop running even if sd_notify transport fails.
            log.warning(f"systemd notify failed: {e}")
            return False