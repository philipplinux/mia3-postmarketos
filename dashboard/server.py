#!/usr/bin/env python3
"""Small Mi A3 status dashboard (read-only, except same-origin POSTs to kill own processes and pause/stop/start containers). Standard library only."""

from __future__ import annotations

import argparse
import ipaddress
import socket
import datetime as dt
import json
import os
import pwd
import re
import signal
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, UnicodeError):
        return None


def _int_value(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def zone_max(sys_root: Path, prefixes: tuple[str, ...]) -> float | None:
    """Hottest thermal zone whose type starts with one of prefixes (TSENS zones from mia3-tsens), °C."""
    temps = []
    for z in Path(sys_root, "class/thermal").glob("thermal_zone*"):
        try:
            if z.joinpath("type").read_text().strip().startswith(prefixes):
                temps.append(int(z.joinpath("temp").read_text()) / 1000)
        except (OSError, ValueError):
            pass
    return round(max(temps), 1) if temps else None


def thermal_info() -> dict[str, Any]:
    """thermal-guard status (+ last line of /var/log/thermal-guard.log) and charge mode."""
    def rd(p):
        try:
            return Path(p).read_text().strip()
        except OSError:
            return None
    def last_line(p):
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                lines = f.read().decode(errors="replace").splitlines()
            return next((l.strip() for l in reversed(lines) if l.strip()), None)
        except OSError:
            return None
    out: dict[str, Any] = {"sensor": None, "temp_c": None, "over": 0, "last_event": last_line("/var/log/thermal-guard.log"),
                           "charge_mode": rd("/run/chargectl.mode")}
    parts = (rd("/run/thermal-guard.status") or "").split()
    try:
        out["sensor"], out["temp_c"] = parts[0], float(parts[1])
        out["over"] = int(parts[2].partition("=")[2])
    except (IndexError, ValueError):
        pass
    return out


def battery_info(sys_root: Path) -> dict[str, Any]:
    supplies = sys_root / "class" / "power_supply"
    try:
        candidates = sorted(p for p in supplies.iterdir() if p.is_dir())
    except OSError:
        candidates = []
    for path in candidates:
        uevent: dict[str, str] = {}
        raw = _read_text(path / "uevent") or ""
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                uevent[key.removeprefix("POWER_SUPPLY_")] = value
        kind = _read_text(path / "type") or uevent.get("TYPE")
        if (kind or "").lower() != "battery":
            continue

        def value(key: str) -> str | None:
            return _read_text(path / key.lower()) or uevent.get(key)

        capacity = _int_value(value("CAPACITY"))
        temp = _int_value(value("TEMP"))
        voltage = _int_value(value("VOLTAGE_NOW"))
        current = _int_value(value("CURRENT_NOW"))
        present_raw = value("PRESENT")
        return {
            "available": True,
            "name": uevent.get("NAME", path.name),
            "percent": capacity,
            "status": value("STATUS"),
            "temperature_c": round(temp / 10, 1) if temp is not None else None,
            "voltage_v": round(voltage / 1_000_000, 3) if voltage is not None else None,
            "current_ma": round(current / 1_000, 1) if current is not None else None,
            "present": (present_raw not in {"0", "no", "false"}) if present_raw is not None else None,
        }
    estimate = iio_battery(sys_root)
    if estimate:
        return estimate
    return {"available": False, "name": None, "percent": None, "status": None,
            "temperature_c": None, "voltage_v": None, "current_ma": None, "present": None}


from battery import charge_conf, iio_battery, set_charge_conf, usb_input_current_ma

SD_PATH = "/mnt/sd"
LISTEN_CONF = "/var/lib/mia3-dashboard/listen.json"  # {"wifi","usb","tailscale": bool}, set on the phone's Network page
NETS = (("local", "127.0.0.0/8"), ("usb", "172.16.42.0/24"), ("tailscale", "100.64.0.0/10"))


def listen_conf() -> dict[str, bool]:
    try:
        c = json.load(open(LISTEN_CONF))
        return {k: c.get(k) is True for k in ("wifi", "usb", "tailscale")}
    except (OSError, ValueError, AttributeError):
        return {"wifi": False, "usb": False, "tailscale": False}


def net_of(local_ip: str, wifi_ip: str | None = None) -> str:
    """Which interface class a connection arrived on, from the phone-side address."""
    try:
        ip = ipaddress.ip_address(local_ip.removeprefix("::ffff:"))
    except ValueError:
        return "other"
    if ip.version == 6:
        return "local" if ip.is_loopback else "other"  # ponytail: no IPv6 exposure
    if wifi_ip and str(ip) == wifi_ip:
        return "wifi"
    return next((name for name, net in NETS if ip in ipaddress.ip_network(net)), "other")  # other = docker bridges etc.


def access_info(wifi: dict[str, Any], port: int) -> dict[str, Any]:
    conf, ip = listen_conf(), (wifi.get("ip_address") or "").split("/")[0] or None
    urls = {"wifi": ip and f"http://{ip}:{port}", "usb": f"http://172.16.42.1:{port}",
            "tailscale": f"http://{socket.gethostname()}:{port}"}
    return {k: {"on": conf[k], "url": urls[k]} for k in conf}
from audio import audio_info, play_tone, set_gain, set_volume
from gpu import gpu_info, set_gpu
from services import services_info


def uptime_seconds(proc_root: Path) -> float | None:
    raw = _read_text(proc_root / "uptime")
    if not raw:
        return None
    try:
        return round(float(raw.split()[0]), 1)
    except (ValueError, IndexError):
        return None


def memory_info(proc_root: Path) -> dict[str, Any]:
    raw = _read_text(proc_root / "meminfo") or ""
    values: dict[str, int] = {}
    for line in raw.splitlines():
        match = re.match(r"^(MemTotal|MemAvailable|MemFree):\s+(\d+)\s+kB", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable", values.get("MemFree"))
    used = max(0, total - available) if total is not None and available is not None else None
    return {"total_bytes": total, "available_bytes": available, "used_bytes": used,
            "used_percent": round(used * 100 / total, 1) if used is not None and total else None}


def _cpu_totals(proc_root: Path) -> tuple[int, int] | None:
    raw = _read_text(proc_root / "stat")
    if not raw:
        return None
    for line in raw.splitlines():
        fields = line.split()
        if fields and fields[0] == "cpu":
            try:
                ticks = [int(field) for field in fields[1:]]
                if len(ticks) < 4:
                    return None
                # Linux reports guest time inside user/nice too, so exclude the final
                # guest and guest_nice counters to avoid counting those ticks twice.
                total = sum(ticks[:8])
                idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
                return total, total - idle
            except ValueError:
                return None
    return None


def _core_totals(proc_root: Path) -> list[tuple[int, int]]:
    """Per-core (total, busy) ticks from the cpuN lines of /proc/stat."""
    cores = []
    for line in (_read_text(proc_root / "stat") or "").splitlines():
        fields = line.split()
        if fields and fields[0][:3] == "cpu" and fields[0][3:].isdigit():
            ticks = [int(field) for field in fields[1:9]]
            cores.append((sum(ticks), sum(ticks) - ticks[3] - ticks[4]))
    return cores


def _disk(path: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {"path": path, "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
                "used_percent": round(usage.used * 100 / usage.total, 1) if usage.total else None}
    except OSError:
        return {"path": path, "total_bytes": None, "used_bytes": None, "free_bytes": None, "used_percent": None}


def _process_snapshot(proc_root: Path) -> dict[int, tuple[int, int, str, int]]:
    """Return pid -> (starttime, cpu ticks, comm, RSS bytes), never command lines."""
    snapshot: dict[int, tuple[int, int, str, int]] = {}
    try:
        paths = list(proc_root.iterdir())
    except OSError:
        return snapshot
    for path in paths:
        if path.name.isdigit():
            one = _process_snapshot_pid(proc_root, int(path.name))
            if one:
                snapshot[int(path.name)] = one
    return snapshot


def _process_snapshot_pid(proc_root: Path, pid: int) -> tuple[int, int, str, int] | None:
    stat = _read_text(proc_root / str(pid) / "stat")
    if not stat:
        return None
    # comm may contain spaces and parentheses; the kernel terminates it at the final ')'.
    close = stat.rfind(")")
    if close < 0:
        return None
    try:
        fields = stat[close + 1:].split()  # starts at field 3 (state)
        cpu_ticks = int(fields[11]) + int(fields[12])  # utime + stime
        starttime = int(fields[19])
        rss_pages = int(fields[21]) if len(fields) > 21 else 0  # field 24 of /proc/PID/stat
    except (ValueError, IndexError):
        return None
    comm = stat[stat.find("(") + 1:close].strip()
    try:
        rss_bytes = rss_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, AttributeError):
        rss_bytes = 0
    return starttime, cpu_ticks, comm, rss_bytes


def _split_escaped(value: str, separator: str = ":") -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == separator:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def wifi_info(proc_root: Path, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    wireless: dict[str, dict[str, Any]] = {}
    raw = _read_text(proc_root / "net" / "wireless") or ""
    for line in raw.splitlines()[2:]:
        match = re.match(r"^\s*([^:]+):\s+\S+\s+([\d.]+)\s+(-?[\d.]+)", line)
        if match:
            wireless[match.group(1).strip()] = {
                "quality": float(match.group(2)), "signal_dbm": float(match.group(3))
            }
    result: dict[str, Any] = {"available": False, "connected": False, "interface": None,
                              "ssid": None, "signal_percent": None, "rate_mbps": None,
                              "ip_address": None, "quality": None, "signal_dbm": None}
    try:
        proc = run(["nmcli", "-t", "--escape", "yes", "-f", "ACTIVE,SSID,SIGNAL,RATE,DEVICE",
                    "device", "wifi", "list", "--rescan", "no"], capture_output=True,
                   text=True, timeout=2, check=True)
        active: list[str] | None = None
        for line in proc.stdout.splitlines():
            fields = _split_escaped(line)
            if len(fields) >= 5 and fields[0].lower() in {"yes", "*"}:
                active = fields
                break
        if active:
            result.update({"available": True, "connected": True, "ssid": active[1] or None,
                           "signal_percent": _int_value(active[2]), "rate_mbps": active[3] or None,
                           "interface": active[4] or None})
            iface = active[4]
            if iface:
                ip_proc = run(["nmcli", "-t", "--escape", "yes", "-f", "IP4.ADDRESS",
                               "device", "show", iface], capture_output=True, text=True,
                              timeout=2, check=True)
                for line in ip_proc.stdout.splitlines():
                    if line.startswith("IP4.ADDRESS") and ":" in line:
                        result["ip_address"] = line.split(":", 1)[1] or None
                        break
                result.update(wireless.get(iface, {}))
        else:
            result["available"] = True
    except (OSError, subprocess.SubprocessError):
        # NetworkManager may be absent; kernel wireless statistics remain a useful fallback.
        if wireless:
            iface, metrics = next(iter(wireless.items()))
            result.update({"available": True, "connected": None, "interface": iface,
                           "quality": metrics.get("quality"), "signal_dbm": metrics.get("signal_dbm")})
    return result


def docker_info(run: Callable[..., Any] = subprocess.run, docker_cmd: str = "docker") -> dict[str, Any]:
    try:
        proc = run([docker_cmd, "ps", "-a", "--format", "{{json .}}"], capture_output=True, text=True,
                   timeout=3, check=True)
        containers = []
        for line in proc.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            containers.append({"name": item.get("Names"), "image": item.get("Image"),
                               "state": item.get("State"), "status": item.get("Status"),
                               "ports": item.get("Ports")})
        return {"available": True, "containers": containers, "error": None}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "containers": [], "error": str(exc)[:160]}


PROTECTED_COMMS = {"mia3-dashboard", "mia3-display"}
SECRET_RE = re.compile(r"(?i)((?:key|token|secret|passw\w*|auth\w*|credential\w*)[\"']?\s*[=:]\s*)\S+")
KILL_GRACE_S = 4  # SIGKILL only if a SIGTERM to the same process is at least this old
_users: dict[int, str] = {}


def _user_name(uid: int) -> str:
    if uid not in _users:
        try:
            _users[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _users[uid] = str(uid)
    return _users[uid]


def _cmdline(proc_root: Path, pid: int) -> str | None:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()[:400]
    except OSError:
        return None
    text = raw.replace(b"\0", b" ").decode(errors="replace").strip()
    return SECRET_RE.sub(r"\1***", text)[:80] or None


def process_protected(pid: int, comm: str) -> bool:
    return pid in (1, os.getpid()) or comm in PROTECTED_COMMS


class Collector:
    FEATURE_CACHE_SECONDS = 30

    def __init__(self, proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys"),
                 storage_path: str = "/", cpu_count: int | None = None,
                 run: Callable[..., Any] = subprocess.run, docker_cmd: str = "docker") -> None:
        self.proc_root = Path(proc_root)
        self.sys_root = Path(sys_root)
        self.storage_path = storage_path
        self.cpu_count = cpu_count or os.cpu_count() or 1
        self.run = run
        self.docker_cmd = docker_cmd
        self.port = 8090
        self._previous_cpu: tuple[int, int] | None = None
        self._previous_cores: list[tuple[int, int]] = []
        self._previous_processes: dict[int, tuple[int, int, str, int]] = {}
        self._feature_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._termed: dict[tuple[int, int], float] = {}

    def _feature(self, key: str, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._feature_cache.get(key)
        if cached and now - cached[0] < self.FEATURE_CACHE_SECONDS:
            return cached[1]
        value = loader()
        self._feature_cache[key] = (time.monotonic(), value)
        return value

    def collect(self) -> dict[str, Any]:
        with self._lock:
            current_cpu = _cpu_totals(self.proc_root)
            current_processes = _process_snapshot(self.proc_root)
            cpu_percent: float | None = None
            process_rows: list[dict[str, Any]] = []
            total_delta = 0
            if current_cpu and self._previous_cpu:
                total_delta = current_cpu[0] - self._previous_cpu[0]
                busy_delta = current_cpu[1] - self._previous_cpu[1]
                if total_delta > 0:
                    cpu_percent = round(max(0, min(100, busy_delta * 100 / total_delta)), 1)
            memory_total = memory_info(self.proc_root).get("total_bytes")
            for pid, (start, ticks, comm, rss) in current_processes.items():
                previous = self._previous_processes.get(pid)
                process_cpu = None
                if total_delta > 0 and previous and previous[0] == start:
                    process_cpu = round(max(0, ticks - previous[1]) * self.cpu_count * 100 / total_delta, 1)
                process_rows.append({"pid": pid, "name": comm, "cpu_percent": process_cpu,
                                     "rss_bytes": rss,
                                     "memory_percent": round(rss * 100 / memory_total, 1)
                                     if memory_total else None})
            cores = _core_totals(self.proc_root)
            core_percent = [round(max(0, min(100, (b - pb) * 100 / (t - pt))), 1) if t > pt else None
                            for (t, b), (pt, pb) in zip(cores, self._previous_cores)]
            self._previous_cores = cores
            self._previous_cpu = current_cpu
            self._previous_processes = current_processes
            process_rows.sort(key=lambda row: (row["cpu_percent"] is not None,
                                               row["cpu_percent"] or 0,
                                               row["rss_bytes"]), reverse=True)
            me = os.getuid()
            for row in process_rows:
                try:
                    uid = os.stat(self.proc_root / str(row["pid"])).st_uid
                except OSError:
                    uid = -1
                row["start"] = current_processes[row["pid"]][0]
                row["user"] = _user_name(uid) if uid >= 0 else "?"
                row["killable"] = uid == me and not process_protected(row["pid"], row["name"])
            for row in process_rows[:30] + sorted(process_rows, key=lambda r: -r["rss_bytes"])[:30]:
                row["cmd"] = _cmdline(self.proc_root, row["pid"])
            storage = _disk(self.storage_path)
            return {
                "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "battery": {**battery_info(self.sys_root), "charge": charge_conf(),
                            "usb_current_ma": usb_input_current_ma(self.sys_root)},
                "gpu": {**gpu_info(self.sys_root), "temp_c": zone_max(self.sys_root, ("gpu-",))},
                "thermal": thermal_info(),
                "audio": audio_info(),
                "services": services_info(self.run),
                "uptime_seconds": uptime_seconds(self.proc_root),
                "cpu": {"percent": cpu_percent, "logical_cores": self.cpu_count,
                        "process_percent_basis": "one core = 100%", "cores": core_percent,
                        "temp_c": zone_max(self.sys_root, ("cpu",))},
                "memory": memory_info(self.proc_root),
                "storage": storage,
                "sd": _disk(SD_PATH) if os.path.ismount(SD_PATH) else None,
                "wifi": self._feature("wifi", lambda: wifi_info(self.proc_root, self.run)),
                "access": access_info(self._feature("wifi", lambda: wifi_info(self.proc_root, self.run)), self.port),
                "processes": process_rows,
                "containers": self._feature("docker", lambda: docker_info(self.run, self.docker_cmd)),
            }


    def kill(self, pid: Any, start: Any, sig: Any) -> tuple[bool, str]:
        """SIGTERM, or SIGKILL once a SIGTERM to the same process is old enough. Own processes only."""
        if not (isinstance(pid, int) and not isinstance(pid, bool) and isinstance(start, int)) or sig not in ("TERM", "KILL"):
            return False, "bad request"
        proc = _process_snapshot_one(self.proc_root, pid)
        if proc is None or proc[0] != start:
            return False, "process is gone"
        if process_protected(pid, proc[1]):
            return False, "protected process"
        if os.stat(self.proc_root / str(pid)).st_uid != os.getuid():
            return False, "not your process"
        now = time.monotonic()
        if sig == "KILL" and now - self._termed.get((pid, start), now) < KILL_GRACE_S:
            return False, "send SIGTERM first and wait"
        try:
            os.kill(pid, signal.SIGTERM if sig == "TERM" else signal.SIGKILL)
        except OSError as exc:
            return False, str(exc)[:80]
        if sig == "TERM":
            self._termed[(pid, start)] = now
        return True, f"SIG{sig} sent to {proc[1]} ({pid})"

    DOCKER_ACTIONS = {"pause": ("running",), "unpause": ("paused",), "stop": ("running", "paused"),
                      "start": ("exited", "created")}

    def docker_action(self, action: str, name: Any) -> tuple[bool, str]:
        states = self.DOCKER_ACTIONS.get(action)
        if states is None or not isinstance(name, str):
            return False, "bad request"
        listing = docker_info(self.run, self.docker_cmd)
        match = next((c for c in listing["containers"] if c["name"] == name), None)
        if match is None:
            return False, "unknown container"
        if (match["state"] or "").lower() not in states:
            return False, f"cannot {action} a {match['state']} container"
        try:
            proc = self.run([self.docker_cmd, action, name], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)[:80]
        self._feature_cache.pop("docker", None)
        return (True, f"{action} {name}: ok") if proc.returncode == 0 else (False, (proc.stderr or "failed").strip()[:80])


def _process_snapshot_one(proc_root: Path, pid: int) -> tuple[int, str] | None:
    one = _process_snapshot_pid(proc_root, pid)
    return (one[0], one[2]) if one else None


class DashboardHandler(BaseHTTPRequestHandler):
    collector: Collector
    html_path: Path

    def _blocked(self) -> bool:
        wifi = self.collector._feature("wifi", lambda: wifi_info(self.collector.proc_root, self.collector.run))
        net = net_of(self.connection.getsockname()[0], (wifi.get("ip_address") or "").split("/")[0] or None)
        if net == "local" or listen_conf().get(net):
            return False
        self._send(403, f"Dashboard is off on {net} (turn it on in the phone's Network page)".encode(),
                   "text/plain; charset=utf-8")
        return True

    def do_GET(self) -> None:
        if self._blocked():
            return
        if self.path == "/api/status":
            body = json.dumps(self.collector.collect(), separators=(",", ":")).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif self.path == "/" or self.path == "/index.html":
            try:
                body = self.html_path.read_bytes()
            except OSError:
                self._send(500, b"Dashboard page is unavailable", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self._blocked():
            return
        # CSRF guard: same-origin JSON only (a cross-site page can't send application/json without a preflight).
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin or urlsplit(origin).netloc != self.headers.get("Host", "\0"):
            return self._json(403, False, "bad origin")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json(415, False, "json only")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else None
        except (ValueError, OSError):
            body = None
        if not isinstance(body, dict):
            return self._json(400, False, "bad request")
        if self.path == "/api/process/kill":
            ok, msg = self.collector.kill(body.get("pid"), body.get("start"), body.get("signal"))
        elif self.path == "/api/charge":
            ok, msg = set_charge_conf(body.get("target"), body.get("drain"))
        elif self.path == "/api/audio/volume":
            ok, msg = set_volume(body.get("volume"))
        elif self.path == "/api/audio/gain":
            ok, msg = set_gain(body.get("gain"))
        elif self.path == "/api/gpu":
            ok, msg = set_gpu(body.get("governor"), body.get("max_mhz"))
        elif self.path == "/api/audio/tone":
            ok, msg = play_tone()
        elif self.path.startswith("/api/docker/"):
            ok, msg = self.collector.docker_action(self.path[len("/api/docker/"):], body.get("name"))
        else:
            return self._json(404, False, "not found")
        self._json(200 if ok else 409, ok, msg)

    def _json(self, status: int, ok: bool, msg: str) -> None:
        self._send(status, json.dumps({"ok": ok, "message": msg}).encode(), "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> None:
    try:
        Path("/proc/self/comm").write_text("mia3-dashboard")
    except OSError:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    handler = type("Mi A3DashboardHandler", (DashboardHandler,), {
        "collector": Collector(), "html_path": Path(__file__).with_name("index.html")
    })
    handler.collector.port = args.port
    # Bound everywhere; _blocked() refuses interfaces that are off, nftables opens 8090 on wlan*/tailscale0.
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    server.daemon_threads = True
    print(f"Mi A3 dashboard listening on :{args.port} (local always; {listen_conf()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
