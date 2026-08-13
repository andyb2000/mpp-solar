from enum import Enum

class DaemonType(Enum):
    """ Daemon types implemented """
    DISABLED = "disabled"
    SYSTEMD = "systemd"
    OPENRC = "openrc"

def get_daemon(daemontype):
    match daemontype:
        case DaemonType.DISABLED:
            from .daemon_disabled import DaemonDummy as daemon
            return daemon()
        case DaemonType.SYSTEMD:
            from .daemon_systemd import DaemonSystemd as daemon
            return daemon()
        case DaemonType.OPENRC:
            from .daemon_openrc import DaemonOpenRC as daemon
            return daemon()
        case _:
            raise Exception(f"unknown daemontype {daemontype}")

def detect_daemon_type():
    """ Auto-detect the appropriate daemon type for the system """
    import os
    import shutil
    
    # Only use systemd notifications when running inside a systemd service
    # context where NOTIFY_SOCKET is provided.
    if shutil.which('systemctl') and os.path.exists('/run/systemd/system') and os.environ.get('NOTIFY_SOCKET'):
        return DaemonType.SYSTEMD
    
    # Check if OpenRC is available
    if shutil.which('rc-service') or os.path.exists('/sbin/openrc'):
        return DaemonType.OPENRC
    
    # Default to OpenRC-compatible daemon handling (pid/signal/watchdog files)
    return DaemonType.OPENRC  # Use OpenRC implementation as fallback since it handles signals
