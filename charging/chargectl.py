#!/usr/bin/env python3
"""Hold the Mi A3 battery around 50% using the pmi632_chg module switches.

No fuel gauge on this PMIC, so it works from battery voltage (dashboard OCV table: 3.80 V ~45%, 3.90 V ~60%).
Modes: charge (USB charges), hold (USB powers the phone, battery idle), discharge (USB ignored, run on battery).
Heat: no battery thermistor is wired up, so the hottest of charger/board/XO sensors stands in for it.
Too hot -> hold (no charge current, and USB still carries the load); unreadable temps count as hot.
"""
import glob, sys, time

P = "/sys/module/pmi632_chg/parameters/"
STATE = "/run/chargectl.mode"
# tuning knobs (volts); voltage under charge reads ~0.05-0.1 V above rest
CHARGE_STOP, HOLD_AT, RECHARGE, FAILSAFE = 3.90, 3.83, 3.75, 3.60
HOT, COOL = 45.0, 40.0  # °C: stop charging at HOT, allow it again below COOL
TEMPS = ("chg_temp", "quiet_therm", "xo_therm")
SWITCH = {"charge": ("1", "0"), "hold": ("0", "0"), "discharge": ("0", "1")}  # charging_enabled, usb_suspend


def step(mode, v, t=25.0, hot=False):
    """Next (mode, hot) for voltage v and temperature t."""
    hot = t >= HOT or (hot and t >= COOL)
    if hot:
        return "hold", True
    return _step(mode, v), False


def _step(mode, v):
    if v < FAILSAFE:
        return "charge"
    if mode == "discharge" and v <= HOLD_AT:
        return "hold"
    if mode == "charge" and v >= CHARGE_STOP:
        return "hold"
    if mode == "hold" and v >= CHARGE_STOP:
        return "discharge"
    if mode == "hold" and v <= RECHARGE:
        return "charge"
    return mode


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


def main():
    mode, hot = None, False
    while True:
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
        new, now_hot = step(mode or ("discharge" if v >= CHARGE_STOP else "hold"), v, t, hot)
        if v == 0.0 and not now_hot:
            new = "charge"
        if now_hot != hot:
            print(f"{t:.1f} °C: {'too hot, charging stopped' if now_hot else 'cooled down'}", flush=True)
            hot = now_hot
        if new != mode:
            print(f"{v:.3f} V {t:.1f} °C: {mode} -> {new}", flush=True)
            apply(new)
            mode = new
        time.sleep(20 if hot else 60)


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
        print("ok")
    else:
        main()
