#!/usr/bin/env python3
"""Hold the Mi A3 battery at a set level (default 50%) using the pmi632_chg module switches.

No fuel gauge on this PMIC, so it works from battery voltage (dashboard OCV table: 3.80 V ~45%, 3.90 V ~60%).
Modes: charge (USB charges), hold (USB powers the phone, battery idle), discharge (USB ignored, run on battery).
Heat: no battery thermistor is wired up, so the hottest of charger/board/XO sensors stands in for it.
Too hot -> hold (no charge current, and USB still carries the load); unreadable temps count as hot.
/var/lib/chargectl/chargectl.json {"target": 20-100, "drain": bool} sets the level (100 = no limit) and whether a battery above it
is run down to it (drain) or just left alone; edits apply within a few seconds.
"""
import glob, json, os, subprocess, sys, time

P = "/sys/module/pmi632_chg/parameters/"
STATE = "/run/chargectl.mode"
CONF = "/var/lib/chargectl/chargectl.json"
# same rough OCV table as the dashboard's battery.py (V -> %)
OCV = [(3.40, 0), (3.60, 10), (3.70, 25), (3.80, 45), (3.90, 60), (4.00, 72), (4.10, 85), (4.25, 95), (4.40, 100)]
# tuning knobs (volts) around the target's rest voltage; voltage under charge reads ~0.05-0.1 V above rest
STOP_ABOVE, RECHARGE_BELOW, FAILSAFE = 0.07, 0.08, 3.60
LOW, LOW_REARM = 10, 20  # %: audio/mia3-chime "low" once below LOW, again only after back above LOW_REARM
HOT, COOL = 45.0, 40.0  # °C: stop charging at HOT, allow it again below COOL
TEMPS = ("chg_temp", "quiet_therm", "xo_therm")
SWITCH = {"charge": ("1", "0"), "hold": ("0", "0"), "discharge": ("0", "1")}  # charging_enabled, usb_suspend


def volts_for(pct):
    for (v0, p0), (v1, p1) in zip(OCV, OCV[1:]):
        if pct <= p1:
            return v0 + (v1 - v0) * (pct - p0) / (p1 - p0)
    return OCV[-1][0]


def load_conf():
    try:
        c = json.load(open(CONF))
        return max(20, min(100, int(c.get("target", 50)))), bool(c.get("drain", True))
    except (OSError, ValueError, TypeError, AttributeError):
        return 50, True


def step(mode, v, t=25.0, hot=False, target=50, drain=True):
    """Next (mode, hot) for voltage v and temperature t."""
    hot = t >= HOT or (hot and t >= COOL)
    if hot:
        return "hold", True
    return _step(mode, v, target, drain), False


def _step(mode, v, target=50, drain=True):
    if v < FAILSAFE or target >= 100:
        return "charge"
    hold_at = volts_for(target)
    stop = hold_at + STOP_ABOVE
    if mode == "discharge" and (v <= hold_at or not drain):
        return "hold"
    if mode == "charge" and v >= stop:
        return "hold"
    if mode == "hold" and v >= stop and drain:
        return "discharge"
    if mode == "hold" and v <= hold_at - RECHARGE_BELOW:
        return "charge"
    return mode


def low_chime(armed, v):
    """Returns (armed, chime now): fire once when the battery drops below LOW %."""
    if armed and 0 < v < volts_for(LOW):
        return False, True
    return armed or v > volts_for(LOW_REARM), False


def vbat():
    return int(open(glob.glob("/sys/bus/iio/devices/iio:device*/in_voltage_vbat_sns_input")[0]).read()) / 1e6


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


def conf_mtime():
    try:
        return os.stat(CONF).st_mtime
    except OSError:
        return None


def main():
    mode, hot, armed = None, False, True
    while True:
        seen = conf_mtime()
        target, drain = load_conf()
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
        armed, ring = low_chime(armed, v)
        if ring:
            print(f"{v:.3f} V: battery below {LOW}%", flush=True)
            try:
                subprocess.Popen(["mia3-chime", "low"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        new, now_hot = step(mode or "hold", v, t, hot, target, drain)
        if v == 0.0 and not now_hot:
            new = "charge"
        if now_hot != hot:
            print(f"{t:.1f} °C: {'too hot, charging stopped' if now_hot else 'cooled down'}", flush=True)
            hot = now_hot
        if new != mode:
            print(f"{v:.3f} V {t:.1f} °C target {target}%{'' if drain else ' (no drain)'}: {mode} -> {new}", flush=True)
            apply(new)
            mode = new
        for _ in range(10 if hot else 30):  # wake early when the display changes the target
            time.sleep(2)
            if conf_mtime() != seen:
                break


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        assert _step("discharge", 4.3) == "discharge" and _step("discharge", 3.82) == "hold"
        assert _step("hold", 3.80) == "hold" and _step("hold", 3.74) == "charge"
        assert _step("charge", 3.85) == "charge" and _step("charge", 3.91) == "hold"
        assert _step("discharge", 3.5) == "charge" and _step("hold", 3.95) == "discharge"
        assert step("charge", 3.70, 46) == ("hold", True)        # heat beats low voltage
        assert step("hold", 3.70, 42, hot=True) == ("hold", True)  # hysteresis: still hot until < 40
        assert step("hold", 3.70, 39, hot=True) == ("charge", False)
        assert step("charge", 3.85, 44) == ("charge", False)
        assert abs(volts_for(50) - 3.833) < 0.001 and volts_for(100) == 4.40 and volts_for(10) == 3.60
        assert _step("hold", 3.70, 100) == "charge" and _step("charge", 4.3, 100) == "charge"
        assert _step("hold", 3.95, 50, drain=False) == "hold" and _step("discharge", 3.95, 50, drain=False) == "hold"
        assert _step("hold", 4.00, 80) == "hold" and _step("hold", 3.97, 80) == "charge"   # 80% rests ~4.06 V
        assert low_chime(True, 3.55) == (False, True) and low_chime(False, 3.55) == (False, False)
        assert low_chime(False, 3.65) == (False, False) and low_chime(False, 3.75) == (True, False)
        assert low_chime(True, 0.0) == (True, False)  # unreadable vbat never chimes
        print("ok")
    else:
        main()
