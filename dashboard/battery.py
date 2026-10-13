"""Battery estimate from PMIC IIO ADCs (no fuel gauge driver on the Mi A3)."""
import json
import os
import statistics
import time
from collections import deque
from pathlib import Path

# OCV (V) -> percent: stock Sunwoda 4030 mAh profile at 25 °C (dtbo_a_16, same table as battery/mia3-qg.dts)
OCV = [(3.000, 0), (3.493, 2), (3.629, 4), (3.697, 6), (3.708, 10), (3.733, 14), (3.773, 20), (3.797, 26),
       (3.805, 30), (3.818, 36), (3.831, 40), (3.846, 44), (3.865, 48), (3.874, 50), (3.899, 54), (3.950, 58),
       (3.967, 60), (4.014, 66), (4.053, 70), (4.094, 74), (4.156, 80), (4.223, 86), (4.270, 90), (4.340, 96),
       (4.386, 100)]


def percent_from_vbat(v):
    if v <= OCV[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(OCV, OCV[1:]):
        if v <= v1:
            return round(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return 100


VBAT = deque()   # (monotonic s, V): % comes from the last 60 s average, a single read jumps with load
PCT = deque()    # (monotonic s, %): mia3-qg capacity, median of the last 60 s; load spikes read 10-15 % low


def _window(q, v, now, window):
    now = time.monotonic() if now is None else now
    q.append((now, v))
    while q[0][0] < now - window:
        q.popleft()
    return [x for _, x in q]


def smooth_vbat(v, now=None, window=60):
    xs = _window(VBAT, v, now, window)
    return sum(xs) / len(xs)


def smooth_pct(p, now=None, window=60):
    return round(statistics.median(_window(PCT, p, now, window)))


def usb_input_current_ma(sys_root):
    """PMI632 USB sense voltage: 0.4 V/A in sink/buck mode; source/OTG uses a different scale."""
    # Xiaomi laurel-r-oss d3766fc, smb5-lib.c smblib_get_prop_usb_current_now: uA = uV * 100 / 40.
    root = Path(sys_root)
    try:
        roles = [(p.read_text().strip()) for p in (root / 'class/typec').glob('port*/power_role')]
        if not any('[sink]' in r or r == 'sink' for r in roles):
            return None
        sense = next((root / 'bus/iio/devices').glob('iio:device*/in_voltage_usb_in_i_uv_input'))
        uv = int(sense.read_text())
        return round(uv / 400, 1) if uv >= 0 else None
    except (OSError, ValueError, StopIteration):
        return None


def iio_battery(sys_root):
    def read(dev, key):
        try:
            return int((dev / key).read_text()) / 1000  # raw uV -> mV, raw m°C -> °C
        except (OSError, ValueError):
            return None
    devs = {}
    for dev in Path(sys_root, 'bus/iio/devices').glob('iio:device*'):
        try:
            devs[(dev / 'name').read_text().strip()] = dev
        except OSError:
            pass
    pmi = next((d for n, d in devs.items() if 'pmic@2:adc' in n), None)
    if pmi is None:
        return None
    vbat = read(pmi, 'in_voltage_vbat_sns_input')
    if vbat is None:
        return None
    usb = read(pmi, 'in_voltage_usb_in_v_div_16_input')
    vph = read(pmi, 'in_voltage_vph_pwr_input')
    plugged = usb is not None and usb > 4000
    temp = read(pmi, 'in_temp_chg_temp_input')
    return {"available": True, "name": "PMI632 ADC (est.)", "percent": percent_from_vbat(smooth_vbat(vbat / 1000)),
            "status": status(plugged),
            "temperature_c": round(temp, 1) if temp is not None else None,
            "voltage_v": round(vbat / 1000, 3),
            "usb_voltage_v": round(usb / 1000, 3) if usb is not None else None,
            "system_voltage_v": round(vph / 1000, 3) if vph is not None else None,
            "current_ma": None, "present": True}


MODES = {"charge": "Charging to ~{}%", "hold": "USB power, holding ~{}%"}


CONF = "/var/lib/chargectl/chargectl.json"  # read by chargectl.service; dir owned by philipp so this service can write


def charge_conf():
    try:
        c = json.load(open(CONF))
        return {"target": max(20, min(100, int(c.get("target", 50))))}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"target": 50}


def set_charge_conf(target=None, drain=None):
    c = charge_conf()
    if target is None:
        return True, "Drain mode is disabled; charge limit unchanged" if drain is not None else "No charge setting changed"
    if not isinstance(target, int) or isinstance(target, bool):
        return False, "bad target"
    c = {"target": max(20, min(100, target))}
    try:
        with open(CONF + ".tmp", "w") as f:
            json.dump(c, f)
        os.replace(CONF + ".tmp", CONF)
    except OSError as e:
        return False, f"could not save: {e.strerror}"
    return True, f"Charge limit {c['target']}%"


def charge_target():
    return charge_conf()["target"]


def status(plugged):
    try:  # chargectl overrides what the USB voltage suggests
        mode, t = open("/run/chargectl.mode").read().strip(), charge_target()
        if plugged and mode == "charge" and t >= 100:
            return "Charging (no limit)"
        return MODES[mode].format(t) if plugged and mode in MODES else "On battery (est.)" if not plugged else "USB power, charge state unknown"
    except (OSError, KeyError):
        return "USB power / charging (est.)" if plugged else "On battery (est.)"


if __name__ == '__main__':
    assert [smooth_pct(p, now=t) for t, p in enumerate([44, 44, 28, 44, 40])][-1] == 44  # dips don't move it
    assert smooth_pct(30, now=100) == 30  # old samples age out
    assert percent_from_vbat(2.9) == 0 and percent_from_vbat(4.5) == 100
    assert percent_from_vbat(3.00) == 0 and percent_from_vbat(4.386) == 100
    assert percent_from_vbat(3.874) == 50 and percent_from_vbat(4.053) == 70
    vals = [percent_from_vbat(3.4 + i / 100) for i in range(101)]
    assert vals == sorted(vals)
    assert smooth_vbat(3.8, 0) == 3.8 and abs(smooth_vbat(4.0, 30) - 3.9) < 1e-9
    assert smooth_vbat(4.2, 100) == 4.2 and len(VBAT) == 1   # older than 60 s dropped
    VBAT.clear()
    import tempfile
    CONF = tempfile.mkdtemp() + '/c.json'
    assert charge_conf() == {"target": 50}
    assert set_charge_conf(85, False)[0] and charge_conf() == {"target": 85}
    assert set_charge_conf(5)[0] and charge_target() == 20
    assert not set_charge_conf("x")[0] and set_charge_conf(drain=True)[0]
    print(iio_battery('/sys'))
    print('ok')
