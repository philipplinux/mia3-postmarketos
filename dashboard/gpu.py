"""Read-only Adreno telemetry. A suspended GPU has no active clock reading."""
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
                  max_clock_mhz=mhz(path / 'max_freq'))
    return result
