"""Battery estimate from PMIC IIO ADCs (no fuel gauge driver on the Mi A3)."""
import json
import os
import time
from collections import deque
from pathlib import Path

# Li-ion open-circuit voltage (V) -> percent; rough, and reads high while charging.
OCV = [(3.40, 0), (3.60, 10), (3.70, 25), (3.80, 45), (3.90, 60), (4.00, 72),
       (4.10, 85), (4.25, 95), (4.40, 100)]


def percent_from_vbat(v):
    if v <= OCV[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(OCV, OCV[1:]):
        if v <= v1:
            return round(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return 100


VBAT = deque()   # (monotonic s, V): % comes from the last 60 s average, a single read jumps with load


def smooth_vbat(v, now=None, window=60):
    now = time.monotonic() if now is None else now
    VBAT.append((now, v))
    while VBAT[0][0] < now - window:
        VBAT.popleft()
    return sum(x for _, x in VBAT) / len(VBAT)


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
    plugged = usb is not None and usb > 4000
    temp = read(pmi, 'in_temp_chg_temp_input')
    return {"available": True, "name": "PMI632 ADC (est.)", "percent": percent_from_vbat(smooth_vbat(vbat / 1000)),
            "status": status(plugged),
            "temperature_c": round(temp, 1) if temp is not None else None,
            "voltage_v": round(vbat / 1000, 3), "current_ma": None, "present": True}


MODES = {"charge": "Charging to ~{}%", "hold": "USB power, holding ~{}%", "discharge": "On battery, draining to ~{}%"}


CONF = "/var/lib/chargectl/chargectl.json"  # read by chargectl.service; dir owned by philipp so this service can write


def charge_conf():
    try:
        c = json.load(open(CONF))
        return {"target": max(20, min(100, int(c.get("target", 50)))), "drain": bool(c.get("drain", True))}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"target": 50, "drain": True}


def set_charge_conf(target=None, drain=None):
    c = charge_conf()
    if target is not None:
        if not isinstance(target, int) or isinstance(target, bool):
            return False, "bad target"
        c["target"] = max(20, min(100, target))
    if drain is not None:
        if not isinstance(drain, bool):
            return False, "bad drain"
        c["drain"] = drain
    try:
        with open(CONF + ".tmp", "w") as f:
            json.dump(c, f)
        os.replace(CONF + ".tmp", CONF)
    except OSError as e:
        return False, f"could not save: {e.strerror}"
    return True, f"Charge limit {c['target']}%" + (", drain on" if c["drain"] else "")


def charge_target():
    return charge_conf()["target"]


def status(plugged):
    try:  # chargectl overrides what the USB voltage suggests
        mode, t = open("/run/chargectl.mode").read().strip(), charge_target()
        if plugged and mode == "charge" and t >= 100:
            return "Charging (no limit)"
        return MODES[mode].format(t) if plugged else "On battery (est.)"
    except (OSError, KeyError):
        return "USB power / charging (est.)" if plugged else "On battery (est.)"


if __name__ == '__main__':
    assert percent_from_vbat(3.3) == 0 and percent_from_vbat(4.5) == 100
    assert percent_from_vbat(3.40) == 0 and percent_from_vbat(4.40) == 100
    assert percent_from_vbat(3.75) == 35 and percent_from_vbat(4.0) == 72
    vals = [percent_from_vbat(3.4 + i / 100) for i in range(101)]
    assert vals == sorted(vals)
    assert smooth_vbat(3.8, 0) == 3.8 and abs(smooth_vbat(4.0, 30) - 3.9) < 1e-9
    assert smooth_vbat(4.2, 100) == 4.2 and len(VBAT) == 1   # older than 60 s dropped
    VBAT.clear()
    import tempfile
    CONF = tempfile.mkdtemp() + '/c.json'
    assert charge_conf() == {"target": 50, "drain": True}
    assert set_charge_conf(85, False)[0] and charge_conf() == {"target": 85, "drain": False}
    assert set_charge_conf(5)[0] and charge_target() == 20
    assert not set_charge_conf("x")[0] and not set_charge_conf(drain=1)[0]
    print(iio_battery('/sys'))
    print('ok')
