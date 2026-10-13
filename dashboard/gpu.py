"""Adreno telemetry plus the devfreq governor / max clock setters. A suspended GPU has no active clock reading."""
import os
import re
from pathlib import Path


def gpu_info(sys_root: Path) -> dict:
    def read(path):
        try:
            return path.read_text().strip()
        except (OSError, UnicodeError):
            return None

    def mhz(path):
        try:
            value = int(read(path))
            return round(value / 1_000_000, 1) if value >= 0 else None
        except (ValueError, TypeError):
            return None

    path = sys_root / 'class/devfreq/5900000.gpu'
    result = dict(available=path.is_dir(), model=None, clock_mhz=None,
                  reported_clock_mhz=None, max_clock_mhz=None,
                  power_state=None, utilization_percent=None)
    if not result['available']:
        return result
    match = re.search(r'qcom,adreno-(\d+)', read(path / 'device/uevent') or '')
    state = read(path / 'device/power/runtime_status')
    reported = mhz(path / 'cur_freq')
    result.update(model=f'Adreno {match.group(1)}' if match else 'Adreno GPU',
                  power_state=state, reported_clock_mhz=reported,
                  clock_mhz=reported if state == 'active' else None,  # devfreq scales 320-950 MHz with load
                  max_clock_mhz=mhz(path / 'max_freq'),
                  governor=read(path / 'governor'), governors=(read(path / 'available_governors') or '').split(),
                  freqs_mhz=[int(f) // 1_000_000 for f in (read(path / 'available_frequencies') or '').split()],
                  writable=os.access(path / 'max_freq', os.W_OK))  # audio/60-mia3-dashboard.rules gives group video write
    return result


def set_gpu(governor=None, max_mhz=None, sys_root=Path('/sys')):
    """Writes devfreq governor / max_freq (resets at reboot)."""
    path = sys_root / 'class/devfreq/5900000.gpu'
    try:
        if governor is not None:
            if governor not in (path / 'available_governors').read_text().split():
                return False, 'bad governor'
            (path / 'governor').write_text(governor)
            return True, f'GPU governor {governor}'
        freqs = [int(f) for f in (path / 'available_frequencies').read_text().split()]
        if not isinstance(max_mhz, int) or isinstance(max_mhz, bool) or max_mhz * 1_000_000 not in freqs:
            return False, 'bad clock'
        (path / 'max_freq').write_text(str(max_mhz * 1_000_000))
        return True, f'GPU max clock {max_mhz} MHz'
    except OSError as e:
        return False, f'GPU: {e.strerror or e}'
