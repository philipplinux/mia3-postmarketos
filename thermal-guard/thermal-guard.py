#!/usr/bin/env python3
"""Power the phone off when a sensor stays too hot. Brief spikes are ignored.

Battery-class sensors (fuel gauge temp, or the Mi A3's charger/board/XO stand-ins) shut down at BATT_MAX;
everything else (CPU/GPU/PMIC zones) at SOC_MAX. "Sustained" = every sample over the limit for HOLD_S seconds.
Status for dashboards: /run/thermal-guard.status; events survive reboots in /var/log/thermal-guard.log
"""
import glob, json, os, subprocess, sys, time

BATT_MAX = float(os.environ.get("BATT_MAX", 55))   # °C; Li-ion should never sit above ~60
SOC_MAX = float(os.environ.get("SOC_MAX", 100))    # °C; kernel's own CPU critical trip is 110
HOLD_S = float(os.environ.get("HOLD_S", 60))
LIMITS = "/var/lib/thermal-guard/limits.json"  # {"batt_max","soc_max","hold_s"}, set on the phone's Power page; overrides env
RANGES = {"BATT_MAX": ("batt_max", 40, 65), "SOC_MAX": ("soc_max", 70, 110), "HOLD_S": ("hold_s", 10, 300)}
EVERY = 5
NOTIFY = os.environ.get("NOTIFY", "")  # user@desktop over ssh (key there may only run a notify command); empty = off
BATT_HINTS = ("bq27", "batt", "bat_therm", "chg_temp", "quiet_therm", "xo_therm")


def read_c(path, scale):
    try:
        c = int(open(path).read()) / scale
    except (OSError, ValueError):
        return None
    return c if -20 < c < 150 else None  # -40 / huge = unconnected or no battery


def sensors():
    """{name: °C} for every readable temperature."""
    out = {}
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        c = read_c(z + "/temp", 1000)
        if c is not None:
            out["zone:" + open(z + "/type").read().strip()] = c
    for f in glob.glob("/sys/class/power_supply/*/temp"):
        c = read_c(f, 10)
        if c is not None:
            out["psy:" + f.split("/")[-2]] = c
    for f in glob.glob("/sys/bus/iio/devices/iio:device*/in_temp_*_input"):
        name = f.rsplit("in_temp_", 1)[1][:-6]
        if any(h in name for h in BATT_HINTS):  # other IIO temps duplicate the zones
            c = read_c(f, 1000)
            if c is not None:
                out["iio:" + name] = c
    return out


def load_limits(path=LIMITS):
    """Apply limits.json (clamped) over the env defaults; a missing or broken file changes nothing."""
    try:
        c = json.load(open(path))
    except (OSError, ValueError):
        return
    for var, (key, lo, hi) in RANGES.items():
        v = c.get(key) if isinstance(c, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            globals()[var] = float(max(lo, min(hi, v)))


def limit(name):
    return BATT_MAX if any(h in name for h in BATT_HINTS) else SOC_MAX


def update(since, temps, now):
    """Track when each sensor went over its limit; return (since, [(name, °C, limit)] past HOLD_S)."""
    since = {n: since.get(n, now) for n, c in temps.items() if c >= limit(n)}
    return since, [(n, temps[n], limit(n)) for n, t0 in since.items() if now - t0 >= HOLD_S]


LOG = "/var/log/thermal-guard.log"


def note(msg):
    print(msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
            f.flush()
            os.fsync(f.fileno())  # must hit disk before the power goes
    except OSError:
        pass


def chime():
    try:  # audio/mia3-chime, best effort, never blocks the guard
        subprocess.Popen(["mia3-chime", "alert"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def shutdown(why):
    note("powering off: " + why)
    chime()
    subprocess.run(["wall", f"thermal-guard: powering off, {why}"], check=False)
    k = "/var/lib/thermal-guard/"
    try:  # best effort, never delays the poweroff by more than 15 s
        if NOTIFY:
            subprocess.run(["ssh", "-i", k + "notify_key", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                            "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=" + k + "known_hosts",
                            NOTIFY, f"{os.uname().nodename}: powered off, {why}"], timeout=15, check=False)
    except subprocess.TimeoutExpired:
        note("desktop notify timed out")
    subprocess.run(["systemctl", "poweroff"], check=False)
    time.sleep(90)  # orderly shutdown stuck: force it
    open("/proc/sysrq-trigger", "w").write("o")


def main():
    since = {}
    load_limits()
    print(f"watching {len(sensors())} sensors: battery {BATT_MAX} °C, soc {SOC_MAX} °C, sustained {HOLD_S:.0f} s", flush=True)
    while True:
        load_limits()
        temps = sensors()
        before = set(since)
        since, over = update(since, temps, time.monotonic())
        for n in set(since) - before:
            note(f"over limit: {n} {temps[n]:.1f} °C (limit {limit(n)} °C)")
        if set(since) - before:
            chime()
        hottest = max(temps.items(), key=lambda kv: kv[1] - limit(kv[0]), default=("none", 0))
        try:
            open("/run/thermal-guard.status", "w").write(f"{hottest[0]} {hottest[1]:.1f} over={len(since)}\n")
        except OSError:
            pass
        if over:
            shutdown(", ".join(f"{n} {c:.1f} °C >= {lim} °C for {HOLD_S:.0f} s" for n, c, lim in over))
            return
        time.sleep(EVERY)


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        s, over = update({}, {"psy:bq27541-0": 56, "zone:cpu0-thermal": 70}, 0)
        assert list(s) == ["psy:bq27541-0"] and not over
        s, over = update(s, {"psy:bq27541-0": 57}, 59)
        assert not over
        s, over = update(s, {"psy:bq27541-0": 57}, 60)
        assert over == [("psy:bq27541-0", 57, BATT_MAX)]
        s, _ = update(s, {"psy:bq27541-0": 50}, 61)  # dip below resets the clock
        s, over = update(s, {"psy:bq27541-0": 57}, 100)
        assert s == {"psy:bq27541-0": 100} and not over
        assert limit("iio:quiet_therm") == BATT_MAX and limit("zone:gpu-top-thermal") == SOC_MAX
        import tempfile
        f = tempfile.mktemp()
        open(f, "w").write('{"batt_max": 99, "soc_max": "x", "hold_s": 30}')
        load_limits(f)
        assert (BATT_MAX, SOC_MAX, HOLD_S) == (65.0, 100, 30.0)
        print("ok", sensors() if os.path.exists("/sys/class/thermal") else "")
    else:
        main()
