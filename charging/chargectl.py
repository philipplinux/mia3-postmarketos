#!/usr/bin/env python3
"""Charge to a battery-voltage limit, then hold until USB is unplugged or the limit is raised.

No fuel-gauge driver is enabled, so the selected percentage is estimated from battery voltage.
USB input remains enabled while holding; no controller mode deliberately drains the battery.
Unreadable temperatures count as hot, and a low-battery failsafe resumes charging.
"""
import glob, json, signal, subprocess, sys, time

P = "/sys/module/pmi632_chg/parameters/"
STATE = "/run/chargectl.mode"
CONF = "/var/lib/chargectl/chargectl.json"
TARGET_STATE = "/run/chargectl.target"
USB_STATE = "/run/chargectl.usb"
HOT_STATE = "/run/chargectl.hot"
THERMAL_RESUME_STATE = "/run/chargectl.thermal_resume"
# OCV (V) -> percent: stock Sunwoda 4030 mAh profile at 25 °C (dtbo_a_16, same table as battery/mia3-qg.dts)
OCV = [(3.000, 0), (3.493, 2), (3.629, 4), (3.697, 6), (3.708, 10), (3.733, 14), (3.773, 20), (3.797, 26),
       (3.805, 30), (3.818, 36), (3.831, 40), (3.846, 44), (3.865, 48), (3.874, 50), (3.899, 54), (3.950, 58),
       (3.967, 60), (4.014, 66), (4.053, 70), (4.094, 74), (4.156, 80), (4.223, 86), (4.270, 90), (4.340, 96),
       (4.386, 100)]
# Voltage under charge reads ~0.05-0.1 V above rest. Once held, battery voltage sag does not restart charging.
STOP_ABOVE, FAILSAFE = 0.05, 3.60
USB_ATTACH, USB_DROP = 4.4, 3.5
LOW, LOW_REARM = 10, 20  # %: audio/mia3-chime "low" once below LOW, again only after back above LOW_REARM
HOT, COOL = 45.0, 40.0  # °C: stop charging at HOT, allow it again below COOL
TEMPS = ("chg_temp", "quiet_therm", "xo_therm")
SWITCH = {"charge": ("1", "0"), "hold": ("0", "0")}  # charging_enabled, usb_suspend; input is never suspended
STOP = False


def volts_for(pct):
    for (v0, p0), (v1, p1) in zip(OCV, OCV[1:]):
        if pct <= p1:
            return v0 + (v1 - v0) * (pct - p0) / (p1 - p0)
    return OCV[-1][0]


def load_conf():
    try:
        c = json.load(open(CONF))
        return max(20, min(100, int(c.get("target", 50))))
    except (OSError, ValueError, TypeError, AttributeError):
        return 50


def step(mode, v, t=25.0, hot=False, target=50, usb=True, raised=False, resume_charge=False):
    """Next (mode, hot) for voltage v and temperature t."""
    hot = t >= HOT or (hot and t >= COOL)
    if hot:
        return "hold", True
    if not usb:
        return "charge", False
    if resume_charge and mode == "hold":
        mode = "charge"
    return _step(mode, v, target, raised), False


def _step(mode, v, target=50, raised=False):
    if v < FAILSAFE or target >= 100:
        return "charge"
    stop = volts_for(target) + STOP_ABOVE
    if mode == "discharge":  # migrate the old drain mode into normal policy
        mode = "charge"
    if mode == "charge" and v >= stop:
        return "hold"
    if mode == "hold" and raised and v < stop:
        return "charge"
    return mode if mode in ("charge", "hold") else "charge"


def low_chime(armed, v):
    """Returns (armed, chime now): fire once when the battery drops below LOW %."""
    if armed and 0 < v < volts_for(LOW):
        return False, True
    return armed or v > volts_for(LOW_REARM), False


def vbat():
    return int(open(glob.glob("/sys/bus/iio/devices/iio:device*/in_voltage_vbat_sns_input")[0]).read()) / 1e6


def usb_voltage():
    try:
        path = glob.glob("/sys/bus/iio/devices/iio:device*/in_voltage_usb_in_v_div_16_input")[0]
        value = int(open(path).read()) / 1e6
        return value if 0 <= value <= 20 else None
    except (OSError, ValueError, IndexError):
        return None


def usb_role():
    for path in glob.glob("/sys/class/typec/port*/power_role"):
        try:
            value = open(path).read().strip()
        except OSError:
            continue
        if "[sink]" in value or value == "sink":
            return "sink"
        if "[source]" in value or value == "source":
            return "source"
    return None


def usb_present(volts, role=None, previous=None):
    if role == "source":
        return False
    if volts is not None and volts <= USB_DROP:
        return False
    if role == "sink":
        return True
    if volts is None:
        return previous
    if volts >= USB_ATTACH:
        return True
    if volts <= USB_DROP:
        return False
    return previous if previous is not None else volts >= (USB_ATTACH + USB_DROP) / 2


def temp():
    vals = []
    for name in TEMPS:
        for f in glob.glob(f"/sys/bus/iio/devices/iio:device*/in_temp_{name}_input"):
            c = int(open(f).read()) / 1000
            if -20 < c < 120:  # -40 is the ADC's "not connected" value
                vals.append(c)
    if not vals:
        raise OSError("no temperature sensor readable")
    return max(vals)


def apply(mode):
    en, susp = SWITCH[mode]
    open(P + "usb_suspend", "w").write(susp)  # resume input before enabling charge, suspend before disabling
    open(P + "charging_enabled", "w").write(en)
    open(STATE, "w").write(mode + "\n")


def read_state(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None


def write_state(path, value):
    value = str(value)
    if read_state(path) != value:
        open(path, "w").write(value + "\n")


def as_bool(value):
    return value == "1"


def charging_wanted(mode, v, target, usb, raised):
    """Whether thermal protection is interrupting a charge cycle that should resume when cool."""
    if not usb:
        return False
    if v < FAILSAFE or target >= 100:
        return True
    stop = volts_for(target) + STOP_ABOVE
    return (mode in (None, "charge", "discharge") or raised) and v < stop


def stop_service(_signum, _frame):
    global STOP
    STOP = True


def main():
    global STOP
    signal.signal(signal.SIGTERM, stop_service)
    mode = read_state(STATE)
    hot = as_bool(read_state(HOT_STATE))
    thermal_resume = as_bool(read_state(THERMAL_RESUME_STATE))
    previous_usb = read_state(USB_STATE)
    previous_usb = None if previous_usb is None else as_bool(previous_usb)
    previous_target = read_state(TARGET_STATE)
    previous_target = int(previous_target) if previous_target and previous_target.isdigit() else None
    armed = True
    first = True
    while not STOP:
        target = load_conf()
        try:
            v = vbat()
        except Exception as ex:  # can't read the battery: charge, never strand it
            print("vbat read failed, charging:", ex, flush=True)
            v = 0.0
        try:
            t = temp()
        except Exception as ex:  # can't see heat: don't push current into the battery
            print("temp read failed, holding:", ex, flush=True)
            t = HOT
        usb = usb_present(usb_voltage(), usb_role(), previous_usb if previous_usb is not None else mode == "hold")
        if usb is None:
            usb = mode == "hold"
        raised = previous_target is not None and target > previous_target
        step_mode = mode
        if mode == "discharge":
            step_mode = "charge"
        new_usb = mode == "hold" and previous_usb is False and usb
        if new_usb:
            step_mode = "charge"  # a new USB connection starts a new charge cycle
        armed, ring = low_chime(armed, v)
        if ring:
            print(f"{v:.3f} V: battery below {LOW}%", flush=True)
            try:
                subprocess.Popen(["mia3-chime", "low"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        resume_after_thermal = hot and not (t >= HOT) and thermal_resume
        new, now_hot = step(step_mode or "charge", v, t, hot, target, usb, raised, resume_after_thermal)
        if now_hot:
            stop = volts_for(target) + STOP_ABOVE
            if not usb or v >= stop:
                thermal_resume = False
            else:
                thermal_resume = thermal_resume or charging_wanted(step_mode, v, target, usb, raised)
        elif not now_hot and hot:
            thermal_resume = False
        if now_hot != hot:
            print(f"{t:.1f} °C: {'too hot, charging stopped' if now_hot else 'cooled down'}", flush=True)
            hot = now_hot
        if first or new != mode:
            print(f"{v:.3f} V {t:.1f} °C target {target}% USB {'in' if usb else 'out'}: {mode} -> {new}", flush=True)
            apply(new)
            mode = new
        write_state(TARGET_STATE, target)
        write_state(USB_STATE, int(usb))
        write_state(HOT_STATE, int(hot))
        write_state(THERMAL_RESUME_STATE, int(thermal_resume))
        previous_target, previous_usb = target, usb
        first = False
        if not STOP:
            time.sleep(2)  # detect USB unplug/reconnect promptly; also observes limit changes


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        assert _step("hold", 3.80) == "hold" and _step("hold", 3.60) == "hold"  # no sag-triggered top-up
        assert _step("charge", 3.85) == "charge" and _step("charge", 3.93) == "hold"
        assert _step("hold", 3.95, 80, raised=True) == "charge"
        assert _step("hold", 3.95, 50, raised=False) == "hold"  # lowering the limit does not drain
        assert _step("hold", 3.95, 50, raised=True) == "hold"  # already above a newly raised threshold
        assert _step("discharge", 3.93) == "hold"  # old drain mode migrates to hold
        assert _step("hold", 3.5) == "charge"  # low-battery failsafe
        assert step("charge", 3.70, 46) == ("hold", True)        # heat beats low voltage
        assert step("hold", 3.70, 42, hot=True) == ("hold", True)  # hysteresis: still hot until < 40
        assert step("hold", 3.70, 39, hot=True, resume_charge=True) == ("charge", False)
        assert step("hold", 3.70, 39, hot=True) == ("hold", False)  # limit hold stays latched after cooling
        assert step("charge", 3.85, 44) == ("charge", False)
        assert abs(volts_for(50) - 3.874) < 0.001 and volts_for(100) == 4.386 and volts_for(10) == 3.708
        assert _step("hold", 3.70, 100) == "charge" and _step("charge", 4.3, 100) == "charge"
        assert _step("hold", 4.00, 80) == "hold"  # voltage sag does not trigger a recharge
        assert step("hold", 3.70, usb=False) == ("charge", False)
        assert usb_present(5.0) is True and usb_present(0.0, previous=True) is False
        assert usb_present(0.0, role="sink", previous=True) is False  # VBUS drop wins over stale sink role
        assert usb_present(5.0, role="source", previous=True) is False  # OTG role wins over voltage
        assert usb_present(None, role="sink") is True and usb_present(None, role="source") is False
        assert usb_present(None, previous=True) is True and usb_present(None, previous=False) is False
        assert usb_present(4.0, previous=True) is True and usb_present(4.0, previous=False) is False
        assert charging_wanted("hold", 3.70, 80, True, True)  # raised limit while already hot resumes when cool
        assert low_chime(True, 3.55) == (False, True) and low_chime(False, 3.55) == (False, False)
        assert low_chime(False, 3.65) == (False, False) and low_chime(False, 3.78) == (True, False)
        assert low_chime(True, 0.0) == (True, False)  # unreadable vbat never chimes
        print("ok")
    else:
        main()
