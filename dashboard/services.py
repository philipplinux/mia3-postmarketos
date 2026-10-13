"""Read-only SSH, Tailscale, USB, and Bluetooth status."""
import json
import os
import subprocess
import time
from pathlib import Path

_cached = None
_cached_at = 0
_cached_key = None

_USB_NOT_CONFIGURED = {
    'not attached', 'not-attached', 'attached', 'powered', 'reconnecting',
    'unauthenticated', 'default', 'addressed', 'address', 'suspended',
}


def _usb_info(sys_root):
    """Report USB data readiness from the read-only UDC state sysfs node."""
    udc_root = Path(sys_root) / 'class' / 'udc'
    try:
        controllers = sorted(udc_root.iterdir())
    except OSError:
        return {'up': None}
    if not controllers:
        return {'up': None}

    states = []
    for controller in controllers:
        try:
            state = (controller / 'state').read_text().strip().lower()
        except OSError:
            states.append(None)
        else:
            states.append(state)

    if 'configured' in states:
        return {'up': True}
    if all(state in _USB_NOT_CONFIGURED for state in states):
        return {'up': False}
    return {'up': None}


def services_info(run=subprocess.run, sys_root='/sys'):
    global _cached, _cached_at, _cached_key
    cache_key = (os.fspath(sys_root), run)
    now = time.monotonic()
    if _cached is not None and _cached_key == cache_key and now - _cached_at < 1:
        return _cached
    result = {'ssh': {'up': None}, 'tailscale': {'up': None},
              'usb': _usb_info(sys_root), 'bluetooth': {'up': None}}
    try:
        show = run(['bluetoothctl', 'show'], capture_output=True, text=True, timeout=1)
        if show.returncode == 0 and 'Powered:' in show.stdout:
            result['bluetooth']['up'] = 'Powered: yes' in show.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        status = run(['systemctl', 'is-active', 'sshd.service'],
                     capture_output=True, text=True, timeout=1).stdout.strip()
        if status in ('active', 'inactive', 'failed', 'activating', 'deactivating'):
            result['ssh']['up'] = status == 'active'
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        response = run(['tailscale', 'status', '--json'],
                       capture_output=True, text=True, timeout=1)
        if response.returncode == 0:
            status = json.loads(response.stdout)
            result['tailscale']['up'] = (status.get('BackendState') == 'Running'
                                         and status.get('Self', {}).get('Online') is True)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        pass
    _cached, _cached_at, _cached_key = result, time.monotonic(), cache_key
    return result
