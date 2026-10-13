#!/usr/bin/env python3
"""Mi A3 display: short power presses cycle views; a 3-second hold powers off cleanly."""
import argparse
import asyncio
import ctypes
from concurrent.futures import ThreadPoolExecutor
import fcntl
import ipaddress
import json
import logging
import mmap
import os
import pwd
from pathlib import Path
import select
import shutil
import signal
import socket
import re
import struct
import subprocess
import textwrap
import threading
import time
import urllib.error
import urllib.request
import datetime as dt

from PIL import Image, ImageDraw, ImageFont
try:
    from dbus_fast import BusType, DBusError, Message, Variant
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method
except ImportError:         # pairing popup off; apk add py3-dbus-fast
    MessageBus = None

BG = '#090c11'
PANEL = '#111720'
LINE = '#263241'
TEXT = '#e8eef5'
MUTED = '#91a0b1'
CYAN = '#65e0da'
GREEN = '#7ce6a6'
AMBER = '#ffcb70'
FONT = Path('/usr/share/fonts/opensans')
FONTS = {}

HOLD_ACTION_S = 3.0
SAMPLE_S = 1.0
KILL_GRACE_S = 10.0
NOTICE_S = 5.0
SYSRQ_STEP_S = 3.0
MIN_RUNAWAY_PCT = 50.0
STILL_BURNING_PCT = 20.0
PROTECTED_COMMS = frozenset({
    'mia3-dashboard', 'mia3-display', 'systemd', '(sd-pam)', 'sshd',
    'sshd.pam', 'sshd-session', 'sshd-session.pa',
})
EVIOCGKEY_96 = 0x80604518


ICON_FONT = Path('/usr/share/fonts/nerd-fonts/SymbolsNerdFont-Regular.ttf')   # apk add font-nerd-fonts-symbols


def font(size, bold=False, mono=False, icon=False):
    key = (size, bold, mono, icon)
    if key not in FONTS:
        path = ICON_FONT if icon else '/usr/share/fonts/droid/DroidSansMono.ttf' if mono else \
            FONT / ('OpenSans-Bold.ttf' if bold else 'OpenSans-Regular.ttf')
        FONTS[key] = ImageFont.truetype(str(path), size)
    return FONTS[key]


def fmt(value, digits=0, suffix=''):
    return '—' if value is None else f'{value:.{digits}f}{suffix}'


def gib(value):
    return '—' if value is None else f'{value / 2**30:.1f} GiB'


def uptime_text(up):
    return '—' if up is None else f'{int(up) // 86400}d {int(up) % 86400 // 3600}h' if up >= 86400 else \
        f'{int(up) // 3600}h {int(up) % 3600 // 60}m'


OV_BATT, OV_NET = (30, 160, 1050, 500), (30, 1190, 1050, 1560)
OV_CONT, OV_PROC = (30, 1590, 525, 2160), (555, 1590, 1050, 2160)


def render(data, stale=False):
    """Overview: battery, 2x2 gauges, connectivity, then container/process summaries (tap a card for its tab)."""
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, 'Mi A3', stale)
    try:
        stamp = dt.datetime.fromisoformat(data.get('collected_at')).astimezone().strftime('%H:%M:%S')
    except (TypeError, ValueError):
        stamp = 'waiting'
    text(900, 50, stamp, 34, MUTED, anchor='ra')

    b, th = data.get('battery') or {}, data.get('thermal') or {}
    x0, y0, x1, y1 = OV_BATT
    card(x0, y0, x1 - x0, y1 - y0)
    text(60, 175, fmt(b.get('percent'), suffix='%'), 130, bold=True)
    text(60, 350, b.get('status') or 'No battery gauge', 36, MUTED, max_width=560)
    bar(60, 420, 560, b.get('percent'), h=24, color=GREEN)
    temp = th.get('temp_c') if th.get('temp_c') is not None else b.get('temperature_c')
    for i, (label, value) in enumerate((('Uptime', uptime_text(data.get('uptime_seconds'))),
                                        ('Temperature', fmt(temp, 1, ' °C')))):
        text(700, 195 + i * 150, label, 30, MUTED)
        text(700, 235 + i * 150, value, 52, AMBER if label == 'Temperature' and th.get('over') else TEXT, bold=True)

    cpu, mem, st, gpu = (data.get(k) or {} for k in ('cpu', 'memory', 'storage', 'gpu'))
    load = gpu.get('utilization_percent')
    gpu_value = 'Off' if not gpu.get('available') else 'Asleep' if gpu.get('power_state') == 'suspended' \
        else fmt(load, 0, '%') if load is not None else 'n/a'
    tiles = (('CPU', fmt(cpu.get('percent'), 0, '%'), f"{cpu.get('logical_cores', '—')} cores", cpu.get('percent')),
             ('Memory', gib(mem.get('used_bytes')), f"of {gib(mem.get('total_bytes'))}", mem.get('used_percent')),
             ('Storage', gib(st.get('used_bytes')), f"{gib(st.get('free_bytes'))} free" + sd_free(), st.get('used_percent')),
             (gpu.get('model') or 'GPU', gpu_value, 'GPU load', load))
    for i, (label, value, sub, pct) in enumerate(tiles):
        x, y = 30 + i % 2 * 525, 530 + i // 2 * 330
        card(x, y, 495, 300)
        text(x + 30, y + 25, label, 34, MUTED, max_width=435)
        text(x + 30, y + 70, value, 72, bold=True, max_width=435)
        bar(x + 30, y + 190, 435, pct)
        text(x + 30, y + 230, sub, 30, MUTED, max_width=435)

    wifi, services = data.get('wifi') or {}, data.get('services') or {}
    x0, y0, x1, y1 = OV_NET
    card(x0, y0, x1 - x0, y1 - y0)
    text(60, 1215, 'Wi-Fi', 30, MUTED)
    text(60, 1255, wifi.get('ssid') or ('Disconnected' if wifi.get('connected') is False else 'unknown'), 56,
         bold=True, max_width=700)
    text(60, 1340, wifi.get('ip_address'), 38, MUTED, max_width=700)
    text(1020, 1230, fmt(wifi.get('signal_percent'), suffix='%'), 64, CYAN, bold=True, anchor='ra')
    text(1020, 1325, f"{wifi.get('rate_mbps') or ''}", 30, MUTED, anchor='ra', max_width=260)
    for i, (key, label) in enumerate((('ssh', 'SSH'), ('tailscale', 'Tailscale'), ('bluetooth', 'Bluetooth'),
                                      ('usb', 'USB'))):
        up = (services.get(key) or {}).get('up')
        color = GREEN if up is True else AMBER if up is False else MUTED
        x = 60 + i * 245
        card(x, 1430, 225, 100, fill=BG, outline=color)
        text(x + 112, 1480, label, 34, color, bold=True, anchor='mm')

    cs = data.get('containers') or {}
    items = cs.get('containers') or []
    x0, y0, x1, y1 = OV_CONT
    card(x0, y0, x1 - x0, y1 - y0)
    text(60, 1615, 'Containers', 34, MUTED)
    text(60, 1660, f"{sum(c.get('state') == 'running' for c in items)}/{len(items)}" if cs.get('available') else '—',
         72, bold=True)
    text(60, 1760, 'running' if cs.get('available') else 'Docker unavailable', 30, MUTED)
    for i, c in enumerate(sorted(items, key=lambda c: c.get('state') != 'running')[:4]):
        y = 1840 + i * 75
        d.ellipse((70 * P, (y + 14) * P, 92 * P, (y + 36) * P), fill=GREEN if c.get('state') == 'running' else AMBER)
        text(110, y, c.get('name') or '?', 34, max_width=390)

    procs = sorted(data.get('processes') or [], key=lambda p: p.get('cpu_percent') or 0, reverse=True)
    x0, y0, x1, y1 = OV_PROC
    card(x0, y0, x1 - x0, y1 - y0)
    text(585, 1615, 'Processes', 34, MUTED)
    text(585, 1660, str(len(procs)) if procs else '—', 72, bold=True)
    text(585, 1760, 'top by CPU', 30, MUTED)
    for i, pr in enumerate(procs[:4]):
        y = 1840 + i * 75
        text(585, y, pr.get('name') or '?', 34, max_width=300)
        text(1020, y + 2, fmt(pr.get('cpu_percent'), 0, '%'), 32, MUTED, anchor='ra')
    return image


# ---- touch UI: gestures, pages, docker helpers -------------------------------------------------
W, H = 1080, 2340            # layout/touch space; frames are drawn at P x this (the 720x1560 panel)
P = 2 / 3
IMG = (720, 1560)
TAB_Y = 2190
TABS = ('Overview', 'Containers', 'Processes', 'Network', 'Files', 'Power')
TAB_ICONS = ('\U000F056E', '\U000F0868', '\U000F0EE0', '\U000F05A9', '\U000F024B', '\U000F0425')  # Material Design (Nerd Font)
FILES_PAGE = TABS.index('Files')
TAB_W = W // len(TABS)
ROW_HS = {1: 170, 2: 120}   # list row height per page (containers, processes)
ROW_H = ROW_HS[1]
TOP_PROCS = 30
GRACE_S = 5.0               # SIGKILL is offered this long after a SIGTERM
ACT_L, ACT_R, ACT_W = (30, 2150, 520, 2290), (560, 2150, 1050, 2290), (30, 2150, 1050, 2290)
DASH_URL = 'http://127.0.0.1:8090'
CONFIRM_TEXT = {'restart': ('Restart', 'The container will be restarted'), 'pause': ('Pause', 'Freezes every process in it'),
                'unpause': ('Unpause', 'Resumes the container'), 'stop': ('Stop', 'The container will be stopped'),
                'start': ('Start', 'The container will be started'), 'term': ('Terminate', 'Sends SIGTERM'),
                'kill': ('Force kill', 'Sends SIGKILL; unsaved data is lost'),
                'ssh_off': ('Turn off', 'Remote logins over SSH stop'),
                'ts_off': ('Turn off', 'Remote access over Tailscale stops'),
                'forget_wifi': ('Forget', 'Deletes the saved password'),
                'forget_wifi_active': ('Forget', 'Wi-Fi disconnects; the phone may go offline'),
                'forget_bt': ('Forget', 'Unpairs it; pair again to reconnect'),
                'delete': ('Delete', 'The file is gone for good'),
                'delete_dir': ('Delete', 'The folder and everything in it'),
                'reboot': ('Reboot', 'The phone restarts'), 'poweroff': ('Shut down', 'Turn it back on with the power button')}
POWER_BTNS = (('reboot', (30, 1620, 520, 1820)), ('poweroff', (560, 1620, 1050, 1820)))
LIST_TOP = 150
LIST_BOTTOM = TAB_Y - 10
DRAG_PX = 40
SWIPE_PX = 250
LONG_S = 0.8
CONFIRM_S = 10.0
CANCEL_BTN = (130, 1130, 510, 1260)
RESTART_BTN = (570, 1130, 950, 1260)
BACK_BTN = (30, 30, 250, 130)
WIFI_CARD = (30, 170, 1050, 690)        # Network page: Wi-Fi card opens the details page
NET_ROWS = (('ssh', 'SSH'), ('tailscale', 'Tailscale'), ('usb', 'USB'), ('bluetooth', 'Bluetooth'), ('ir', 'IR remote'))
IPCFG_BTN = (30, 1070, 1050, 1190)      # Wi-Fi details: IP & DNS settings
SAVED_BTN = (30, 1210, 1050, 1330)      # Wi-Fi details: saved networks (forget)
CHANGE_BTN = (30, 1350, 1050, 1470)     # Wi-Fi details: open the network picker
HOME_DIR = '/home/philipp'
WRITABLE = ('/home/philipp', '/mnt/sd')    # Files may create/delete only below these (the service runs as root)
MKDIR_BTN, DEL_BTN = (690, 30, 860, 130), (880, 30, 1050, 130)
FILE_TOP, FILE_ROW, FILE_LINE = 210, 120, 44
FILES_MAX, FILE_PEEK = 500, 65536
NO_PREVIEW = ('/proc', '/sys', '/dev')  # reads there can block or hang the kernel (debugfs regmaps)
METHOD_BTN = (30, 170, 1050, 290)
IP_FIELDS = (('addr', 'Address / prefix', 'e.g. 10.0.0.50/24'), ('gw', 'Gateway', 'e.g. 10.0.0.1'),
             ('dns', 'DNS servers', 'empty = from router; comma between servers'))
IP_BOXES = tuple((f, (30, 320 + i * 190, 1050, 480 + i * 190)) for i, (f, _, _) in enumerate(IP_FIELDS))
IR = '/dev/lirc0'
# Common TV codes (ir-ctl protocol, {button: scancode or (protocol, scancode)}); LG and Sony power verified on real TVs.
# A key a brand lacks is drawn dimmed. Sony Back/Play/Pause are SIRC-15 device 0x97 (Bravia).
IR_BRANDS = (('LG', 'nec', {'Power': 0x0408, 'Input': 0x040b, 'Mute': 0x0409, 'Vol +': 0x0402, 'Vol -': 0x0403,
                            'Ch +': 0x0400, 'Ch -': 0x0401, 'Home': 0x047c, 'Up': 0x0440, 'Down': 0x0441,
                            'Left': 0x0407, 'Right': 0x0406, 'OK': 0x0444, 'Back': 0x0428, 'Menu': 0x0443,
                            'Play': 0x04b0, 'Pause': 0x04ba}),
             ('Samsung', 'necx', {'Power': 0x070702, 'Input': 0x070701, 'Mute': 0x07070f, 'Vol +': 0x070707,
                                  'Vol -': 0x07070b, 'Ch +': 0x070712, 'Ch -': 0x070710, 'Home': 0x070779,
                                  'Up': 0x070760, 'Down': 0x070761, 'Left': 0x070765, 'Right': 0x070762,
                                  'OK': 0x070768, 'Back': 0x070758, 'Menu': 0x07071a, 'Play': 0x070747,
                                  'Pause': 0x07074a}),
             ('Sony', 'sony12', {'Power': 0x010015, 'Input': 0x010025, 'Mute': 0x010014, 'Vol +': 0x010012,
                                 'Vol -': 0x010013, 'Ch +': 0x010010, 'Ch -': 0x010011, 'Home': 0x010060,
                                 'Up': 0x010074, 'Down': 0x010075, 'Left': 0x010034, 'Right': 0x010033,
                                 'OK': 0x010065, 'Back': ('sony15', 0x970023), 'Menu': ('sony15', 0x970036),
                                 'Play': ('sony15', 0x97001a), 'Pause': ('sony15', 0x970019)}),
             ('Philips', 'rc5', {'Power': 0x000c, 'Input': 0x0038, 'Mute': 0x000d, 'Vol +': 0x0010,
                                 'Vol -': 0x0011, 'Ch +': 0x0020, 'Ch -': 0x0021}))
IR_BRAND_BTN = (30, 170, 1050, 290)
_C4, _C3 = (30, 290, 550, 810), (30, 375, 720)       # 4-wide rows (240 px keys), 3-wide pad (330 px keys)
IR_BTNS = tuple([(k, (_C4[i], 320, _C4[i] + 240, 450)) for i, k in enumerate(('Power', 'Input', 'Mute', 'Home'))]
                + [(k, (_C3[i % 3], 480 + i // 3 * 165, _C3[i % 3] + 330, 630 + i // 3 * 165)) for i, k in enumerate(
                    ('Back', 'Up', 'Menu', 'Left', 'OK', 'Right', 'Play', 'Down', 'Pause'))]
                + [(k, (_C4[i], 990, _C4[i] + 240, 1120)) for i, k in enumerate(('Vol -', 'Vol +', 'Ch -', 'Ch +'))])
IR_ARROWS = {'Up': ((0, -1), (-1, 1), (1, 1)), 'Down': ((0, 1), (-1, -1), (1, -1)),
             'Left': ((-1, 0), (1, -1), (1, 1)), 'Right': ((1, 0), (-1, -1), (-1, 1))}   # no arrow glyphs in Open Sans
IR_ALL_BTN = (30, 1150, 1050, 1280)
BT_VIS = (30, 310, 530, 430)
BT_NAME = (550, 310, 1050, 430)
NET_TOP, NET_ROW = 720, 150
SSH_TOGGLE = (700, 185, 1035, 305)
USER_BTN = (30, 350, 1050, 490)
BT_POWER = (30, 170, 1050, 290)
BT_TOP = 460
USB_MODES = (('network', 'Network', 'USB Ethernet, phone at 172.16.42.1'),
             ('serial', 'Network + serial', 'Adds a login console (ttyACM on the PC)'),
             ('charge', 'Charge only', 'No data, just power'))
USB_BTNS = tuple((m, (30, 200 + i * 240, 1050, 410 + i * 240)) for i, (m, _, _) in enumerate(USB_MODES))
GADGET = Path('/sys/kernel/config/usb_gadget/g1')
RESCAN_BTN = (780, 30, 1050, 130)
CONNECT_BTN = (30, 1330, 1050, 1480)
WIFI_TOP, WIFI_ROW = 170, 150
KB_TOP, KEY_W, KEY_H = 1560, 108, 150
KB_ROWS = {'abc': ('1234567890', 'qwertyuiop', 'asdfghjkl', 'zxcvbnm'),
           'ABC': ('1234567890', 'QWERTYUIOP', 'ASDFGHJKL', 'ZXCVBNM'),
           'sym': ('1234567890', '!@#$%^&*()', '-_=+[]{};:', '\'",.?/\\'),
           'sym2': ('1234567890', '!@#$%^&*()', '-_=+[]{};:', '<>|~`€£')}
KB_SHIFT = {'abc': 'ABC', 'ABC': 'abc', 'sym': 'sym2', 'sym2': 'sym'}
DOCKER = '/usr/sbin/docker'
DOCKER_ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'DOCKER_CONFIG': '/nonexistent'}
SECRET_RE = re.compile(r'(?i)((?:key|token|secret|passw\w*|auth\w*|credential\w*)["\']?\s*[=:]\s*)\S+')
ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]')


def kb_keys(mode):
    """[(key, box)] for the on-screen keyboard; drawing and hit testing share it."""
    keys = []
    for r, row in enumerate(KB_ROWS[mode]):
        y, x0 = KB_TOP + r * KEY_H, 162 if r == 3 else (1080 - KEY_W * len(row)) // 2
        keys += [(c, (x0 + i * KEY_W, y, x0 + (i + 1) * KEY_W, y + KEY_H)) for i, c in enumerate(row)]
    y = KB_TOP + 3 * KEY_H
    keys += [('shift', (0, y, 162, y + KEY_H)), ('back', (918, y, 1080, y + KEY_H))]
    y += KEY_H
    return keys + [('mode', (0, y, 216, y + KEY_H)), ('space', (216, y, 864, y + KEY_H)), ('show', (864, y, 1080, y + KEY_H))]


def parse_wifi(out, known):
    """nmcli -t -e no -f IN-USE,SIGNAL,SECURITY,SSID rows -> networks, strongest per SSID, active first."""
    nets = {}
    for line in out.splitlines():
        use, sig, sec, ssid = (line.split(':', 3) + ['', '', '', ''])[:4]
        if ssid and (ssid not in nets or int(sig or 0) > nets[ssid]['signal']):
            nets[ssid] = {'ssid': ssid, 'signal': int(sig or 0), 'secure': sec not in ('', '--'),
                          'active': use == '*', 'known': ssid in known}
    return sorted(nets.values(), key=lambda n: (not n['active'], not n['known'], -n['signal']))


def nmcli(*args, timeout=45):
    r = subprocess.run(['nmcli', *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def wifi_scan():
    known = set(nmcli('-t', '-e', 'no', '-f', 'NAME', 'con', 'show')[1].splitlines())
    return parse_wifi(nmcli('-t', '-e', 'no', '-f', 'IN-USE,SIGNAL,SECURITY,SSID', 'dev', 'wifi', 'list',
                            '--rescan', 'yes')[1], known)


def wifi_connect(ssid, password, known):
    # ponytail: psk goes via argv, visible in ps for a few seconds; D-Bus secret agent if other users get shells here
    if known and password:
        ok, msg = nmcli('con', 'modify', 'id', ssid, 'wifi-sec.psk', password)
        if not ok:
            return False, msg[-80:]
    if known:
        ok, msg = nmcli('--wait', '30', 'con', 'up', 'id', ssid)
    else:
        ok, msg = nmcli('--wait', '30', 'dev', 'wifi', 'connect', ssid, *(('password', password) if password else ()))
        if not ok:
            nmcli('con', 'delete', 'id', ssid)        # don't keep a profile with a wrong password
    if not ok:
        nmcli('dev', 'connect', 'wlan0')               # fall back to whatever autoconnects
        return False, (msg.splitlines() or ['failed'])[-1].replace('Error: ', '')[:80]
    return True, f'Connected to {ssid}'


def kb_type(w, key, field='pw'):
    """Apply one on-screen key to w[field]."""
    if key == 'shift':
        w['mode'] = KB_SHIFT[w['mode']]
    elif key == 'mode':
        w['mode'] = 'abc' if w['mode'].startswith('sym') else 'sym'
    elif key == 'back':
        w[field] = w[field][:-1]
    elif key == 'show':
        w['show'] = not w['show']
    elif key:
        w[field] += ' ' if key == 'space' else key
        if w['mode'] == 'ABC':
            w['mode'] = 'abc'       # one-shot shift


def draw_kb(d, text, w):
    labels = {'shift': 'Shift', 'back': 'Del', 'mode': 'abc' if w['mode'].startswith('sym') else '?123',
              'space': 'space', 'show': 'Hide' if w['show'] else 'Show'}
    d.rectangle((0, (KB_TOP - 10) * P, IMG[0], IMG[1]), fill=PANEL)
    for key, (x0, y0, x1, y1) in kb_keys(w['mode']):
        d.rounded_rectangle((x0 * P + 3, y0 * P + 3, x1 * P - 3, y1 * P - 3), 8, fill=LINE)
        text((x0 + x1) // 2, (y0 + y1) // 2, labels.get(key, key), 34 if key in labels else 50, anchor='mm')


def sh(*cmd, timeout=30, stdin=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:80]
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def svc(unit, on):
    ok, msg = sh('systemctl', 'enable' if on else 'disable', '--now', unit)
    return ok, f"{unit} {'started' if on else 'stopped'}" if ok else (msg.splitlines() or ['failed'])[-1][:80]


def login_users():
    return [u.pw_name for u in pwd.getpwall() if u.pw_uid >= 1000 and u.pw_uid < 65534
            and not u.pw_shell.endswith(('nologin', 'false'))] or ['root']


def set_password(user, password):
    ok, msg = sh('chpasswd', stdin=f'{user}:{password}\n')     # never logged
    return ok, f'Password set for {user}' if ok else (msg.splitlines() or ['failed'])[-1][:80]


def usb_mode():
    try:
        if not (GADGET / 'UDC').read_text().strip():
            return 'charge'
    except OSError:
        return None
    return 'serial' if (GADGET / 'configs/c.1/acm.GS0').exists() else 'network'


def usb_set(mode):
    """Rebind the configfs gadget with or without a CDC-ACM console; 'charge' leaves it unbound."""
    udcs = sorted(os.listdir('/sys/class/udc'))
    link, fn = GADGET / 'configs/c.1/acm.GS0', GADGET / 'functions/acm.GS0'
    try:
        (GADGET / 'UDC').write_text('\n')
    except OSError:
        pass                                    # already unbound
    try:
        if mode == 'serial':
            fn.mkdir(exist_ok=True)
            if not link.is_symlink():
                link.symlink_to(fn)
        elif link.is_symlink():
            sh('systemctl', 'stop', 'serial-getty@ttyGS0.service')
            link.unlink()
        if mode != 'charge' and udcs:
            (GADGET / 'UDC').write_text(udcs[0])
    except OSError as exc:
        return False, f'USB: {exc.strerror}'
    if mode == 'serial':
        sh('systemctl', 'start', 'serial-getty@ttyGS0.service')
    return True, 'USB: ' + dict((m, t) for m, t, _ in USB_MODES)[mode]


def parse_bt(devices, paired, connected):
    """bluetoothctl `devices` listings -> rows, connected then paired then named, unnamed (bare address) last."""
    def addrs(out):
        return {line.split(' ', 2)[1] for line in out.splitlines() if line.startswith('Device ')}
    pa, co, rows = addrs(paired), addrs(connected), []
    for line in devices.splitlines():
        parts = line.split(' ', 2)
        if len(parts) >= 2 and parts[0] == 'Device':
            name = parts[2] if len(parts) > 2 else parts[1]
            rows.append({'addr': parts[1], 'name': name, 'paired': parts[1] in pa, 'connected': parts[1] in co,
                         'named': name.replace('-', ':') != parts[1]})
    return sorted(rows, key=lambda r: (not r['connected'], not r['paired'], not r['named'], r['name'].lower()))


def bt_list():
    return parse_bt(*(sh('bluetoothctl', 'devices', *f, timeout=10)[1] for f in ((), ('Paired',), ('Connected',))))


def bt_powered():
    return 'Powered: yes' in sh('bluetoothctl', 'show', timeout=5)[1]


def bt_scan():
    sh('bluetoothctl', 'power', 'on', timeout=10)
    sh('bluetoothctl', '--timeout', '8', 'scan', 'on', timeout=15)
    return bt_list()


def bt_power(on):
    ok, msg = sh('bluetoothctl', 'power', 'on' if on else 'off', timeout=10)
    return ok, f"Bluetooth {'on' if on else 'off'}" if ok else msg[-80:], bt_list()


def bt_act(addr, name, how):
    """how: connect | disconnect | pair (pair + trust + connect; Just Works devices only, no PIN entry)."""
    if how == 'pair':
        ok, msg = sh('bluetoothctl', '--timeout', '25', 'pair', addr, timeout=30)
        ok = ok and 'Failed' not in msg
        if ok:
            sh('bluetoothctl', 'trust', addr, timeout=10)
            how = 'connect'
        else:
            return False, f'Pairing {name} failed', bt_list()
    ok, msg = sh('bluetoothctl', '--timeout', '15', how, addr, timeout=20)
    ok = ok and 'Failed' not in msg and 'not available' not in msg
    verb = 'Connected to' if how == 'connect' else 'Disconnected'
    return ok, f'{verb} {name}' if ok else f'{how.capitalize()} {name} failed', bt_list()


def nm_fields(out):
    """`nmcli -t -e no -f ...` key:value lines -> {key: [values]}; 'IP4.DNS[2]' folds into 'IP4.DNS'."""
    res = {}
    for line in out.splitlines():
        k, _, v = line.partition(':')
        if v:
            res.setdefault(k.split('[')[0], []).append(v)
    return res


def wifi_dev():
    out = nmcli('-t', '-f', 'DEVICE,TYPE', 'dev', timeout=10)[1]
    return next((l.split(':')[0] for l in out.splitlines() if l.endswith(':wifi')), 'wlan0')


def wifi_info():
    dev = wifi_dev()
    info = nm_fields(nmcli('-t', '-e', 'no', '-f', 'GENERAL.CONNECTION,GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,'
                           'IP6.ADDRESS', 'dev', 'show', dev, timeout=10)[1])
    con = (info.get('GENERAL.CONNECTION') or [''])[0]
    if con:
        info.update(nm_fields(nmcli('-t', '-e', 'no', '-f', 'ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,'
                                    'ipv4.ignore-auto-dns', 'con', 'show', 'id', con, timeout=10)[1]))
    for line in nmcli('-t', '-e', 'no', '-f', 'IN-USE,FREQ,CHAN,RATE,SECURITY', 'dev', 'wifi', 'list', 'ifname', dev,
                      '--rescan', 'no', timeout=10)[1].splitlines():
        if line.startswith('*:'):
            info['link'] = line.split(':')[1:]
    info['dev'], info['con'] = dev, con
    return info


def ip_args(method, addr, gw, dns):
    """Validated `nmcli con modify` args for the IP & DNS page; raises ValueError with a short message."""
    servers = [x for x in dns.replace(' ', ',').split(',') if x]
    for x in servers:
        try:
            ipaddress.ip_address(x)
        except ValueError:
            raise ValueError(f'Bad DNS server: {x}')
    args = ['ipv4.dns', ','.join(servers), 'ipv4.ignore-auto-dns', 'yes' if servers else 'no']
    if method == 'auto':
        return ['ipv4.method', 'auto', 'ipv4.addresses', '', 'ipv4.gateway', ''] + args
    try:
        net = ipaddress.ip_interface(addr if '/' in addr else addr + '/24')
    except ValueError:
        raise ValueError(f'Bad address: {addr or "empty"}')
    if net.version != 4:
        raise ValueError('Address must be IPv4')
    if gw:
        try:
            g = ipaddress.ip_address(gw)
        except ValueError:
            raise ValueError(f'Bad gateway: {gw}')
        if g not in net.network:
            raise ValueError(f'Gateway {gw} not in {net.network}')
    return ['ipv4.method', 'manual', 'ipv4.addresses', str(net), 'ipv4.gateway', gw] + args


def ip_apply(con, method, addr, gw, dns):
    """Apply, reconnect, ping the gateway; on any failure put the old settings back."""
    try:
        args = ip_args(method, addr, gw, dns)
    except ValueError as exc:
        return False, str(exc)
    keys = ('ipv4.method', 'ipv4.addresses', 'ipv4.gateway', 'ipv4.dns', 'ipv4.ignore-auto-dns')
    old = nm_fields(nmcli('-t', '-e', 'no', '-f', ','.join(keys), 'con', 'show', 'id', con, timeout=10)[1])
    ok, msg = nmcli('con', 'modify', 'id', con, *args, timeout=15)
    if ok:
        ok, msg = nmcli('--wait', '30', 'con', 'up', 'id', con)
    if ok:
        info = wifi_info()
        gw_now = (info.get('IP4.GATEWAY') or [''])[0]
        ok = not gw_now or sh('ping', '-c', '1', '-W', '3', gw_now, timeout=6)[0]
        msg = 'Gateway not reachable' if not ok else msg
    if not ok:
        nmcli('con', 'modify', 'id', con, *[x for k in keys for x in (k, (old.get(k) or [''])[0])], timeout=15)
        nmcli('--wait', '30', 'con', 'up', 'id', con)
        return False, f"{(msg.splitlines() or ['failed'])[-1].replace('Error: ', '')[:60]}: reverted", wifi_info()
    return True, 'IP settings saved', info


def ir_send(proto, code, label='button'):
    sony = proto.startswith('sony')
    ok, msg = sh('ir-ctl', '-d', IR, *(('-g', '25000') if sony else ()), *['-S', f'{proto}:{code:#x}'] * (4 if sony else 1),
                 timeout=5)
    return ok, f'Sent {label}' if ok else (msg.splitlines() or ['IR failed'])[-1][:80]


def ir_all_off():
    sent = [b for b, proto, codes in IR_BRANDS if ir_send(proto, codes['Power'])[0]]
    return bool(sent), f"Power sent: {', '.join(sent)}" if sent else 'IR failed'


def parse_saved(out):
    """`nmcli -t -f NAME,TYPE,DEVICE con show` -> saved Wi-Fi profiles, active first."""
    rows = []
    for line in out.splitlines():
        name, _, rest = line.replace('\\:', '\0').partition(':')
        typ, _, dev = rest.partition(':')
        if typ == '802-11-wireless':
            rows.append({'name': name.replace('\0', ':'), 'active': bool(dev)})
    return sorted(rows, key=lambda r: (not r['active'], r['name'].lower()))


def wifi_saved():
    return parse_saved(nmcli('-t', '-f', 'NAME,TYPE,DEVICE', 'con', 'show')[1])


def wifi_forget(name):
    ok, msg = nmcli('con', 'delete', 'id', name)
    return ok, f'Forgot {name}' if ok else (msg.splitlines() or ['failed'])[-1][:80], wifi_saved()


def bt_forget(addr, name):
    ok, msg = sh('bluetoothctl', 'remove', addr, timeout=10)
    ok = ok and 'Failed' not in msg and 'not available' not in msg
    return ok, f'Forgot {name}' if ok else f'Forget {name} failed', bt_list()


def list_dir(path):
    """Folders first, then files; stat follows symlinks. Capped at FILES_MAX entries."""
    ents = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st, isdir = e.stat(), e.is_dir()
            except OSError:
                st, isdir = None, False
            ents.append({'name': e.name, 'dir': isdir, 'size': st and st.st_size, 'mtime': st and st.st_mtime})
            if len(ents) >= FILES_MAX:
                break
    return sorted(ents, key=lambda e: (not e['dir'], e['name'].lower()))


def file_view(path):
    """Read-only preview: {'img': Image} or {'lines': [...]}."""
    if path.startswith(NO_PREVIEW) or not os.path.isfile(path):
        return {'lines': ['No preview for kernel or device files']}
    try:
        with Image.open(path) as im:
            im.thumbnail((1020, 1900))
            return {'img': im.convert('RGB')}
    except Exception:
        pass
    try:
        with open(path, 'rb') as f:
            raw = f.read(FILE_PEEK)
    except OSError as exc:
        return {'lines': [exc.strerror or 'Cannot read']}
    if b'\0' in raw:
        return {'lines': [f'Binary file, {os.path.getsize(path):,} bytes']}
    lines = raw.decode('utf-8', 'replace').expandtabs(4).splitlines() or ['(empty)']
    return {'lines': lines + (['…'] if len(raw) == FILE_PEEK else [])}


def sd_free(path='/mnt/sd'):
    """' · SD x free' when the microSD card is mounted, else ''."""
    if not os.path.ismount(path):
        return ''
    st = os.statvfs(path)
    return f' · SD {st.f_bavail * st.f_frsize / 2**30:.1f} GiB'


def writable(path, strict=False):
    """Below WRITABLE (strict: not a root itself, for deleting)."""
    path = os.path.realpath(path)
    return any(path.startswith(r + '/') or (path == r and not strict) for r in WRITABLE)


def fmt_size(n):
    return '—' if n is None else f'{n} B' if n < 1024 else next(
        f'{n / 1024 ** i:.1f} {u}' for i, u in ((1, 'KiB'), (2, 'MiB'), (3, 'GiB'), (4, 'TiB')) if n < 1024 ** (i + 1) or u == 'TiB')


class PairAgent:
    """BlueZ agent (DisplayYesNo): pairing/connection requests become a yes/no popup; the UI polls .req."""
    PATH = '/mia3/agent'

    def __init__(self):
        self.req, self.loop = None, None
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            asyncio.run(self.serve())
        except Exception:
            logging.exception('Bluetooth agent stopped')

    def answer(self, yes):
        req, self.req = self.req, None
        if req and self.loop:
            self.loop.call_soon_threadsafe(lambda: req['fut'].done() or req['fut'].set_result(yes))

    async def ask(self, bus, device, kind, code=None):
        reply = await bus.call(Message(destination='org.bluez', path=device, member='Get', signature='ss',
                                       interface='org.freedesktop.DBus.Properties', body=['org.bluez.Device1', 'Alias']))
        name = reply.body[0].value if reply.body else device.rsplit('dev_', 1)[-1].replace('_', ':')
        fut = asyncio.get_running_loop().create_future()
        self.req = {'name': name, 'kind': kind, 'code': code, 'fut': fut}
        try:
            yes = await asyncio.wait_for(fut, 30)
        except asyncio.TimeoutError:
            yes = False
        finally:
            if self.req and self.req['fut'] is fut:
                self.req = None
        logging.info('Bluetooth %s %s: %s', kind, name, 'accepted' if yes else 'rejected')
        if not yes:
            raise DBusError('org.bluez.Error.Rejected', 'Rejected on the phone')
        await bus.call(Message(destination='org.bluez', path=device, member='Set', signature='ssv',
                               interface='org.freedesktop.DBus.Properties',
                               body=['org.bluez.Device1', 'Trusted', Variant('b', True)]))

    async def serve(self):
        pa = self

        class Agent(ServiceInterface):
            def __init__(self):
                super().__init__('org.bluez.Agent1')

            @method()
            def Release(self):
                pass

            @method()
            def Cancel(self):
                pa.answer(False)

            @method()
            async def RequestConfirmation(self, device: 'o', passkey: 'u'):
                await pa.ask(bus, device, 'confirm', f'{passkey:06d}')

            @method()
            async def RequestAuthorization(self, device: 'o'):
                await pa.ask(bus, device, 'pair')

            @method()
            async def AuthorizeService(self, device: 'o', uuid: 's'):
                await pa.ask(bus, device, 'connect')

            @method()
            def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'):
                pass

            @method()
            def DisplayPinCode(self, device: 'o', pincode: 's'):
                pass

            @method()
            def RequestPinCode(self, device: 'o') -> 's':
                raise DBusError('org.bluez.Error.Rejected', 'Legacy PIN pairing not supported')

            @method()
            def RequestPasskey(self, device: 'o') -> 'u':
                raise DBusError('org.bluez.Error.Rejected', 'Passkey entry not supported')

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.loop = asyncio.get_running_loop()
        bus.export(self.PATH, Agent())
        while True:     # re-register after bluetoothd restarts; AlreadyExists replies are harmless
            for member, sig, body in (('RegisterAgent', 'os', [self.PATH, 'DisplayYesNo']),
                                      ('RequestDefaultAgent', 'o', [self.PATH])):
                await bus.call(Message(destination='org.bluez', path='/org/bluez', interface='org.bluez.AgentManager1',
                                       member=member, signature=sig, body=body))
            await asyncio.sleep(30)


def bt_vis(on):
    for cmd in (('pairable', 'on' if on else 'off'), ('discoverable', 'on' if on else 'off')):
        ok, msg = sh('bluetoothctl', *cmd, timeout=10)
        if not ok or 'Failed' in msg:
            return False, msg[-80:], bt_list()
    return True, 'Visible for 3 minutes' if on else 'Hidden', bt_list()


def bt_rename(name):
    ok, msg = sh('bluetoothctl', 'system-alias', name, timeout=10)
    return ok and 'Failed' not in msg, f'Bluetooth name: {name}' if ok else msg[-80:], bt_list()


def bt_state():
    out = sh('bluetoothctl', 'show', timeout=5)[1]
    alias = next((l.split(':', 1)[1].strip() for l in out.splitlines() if l.strip().startswith('Alias:')), '')
    return {'on': 'Powered: yes' in out, 'vis': 'Discoverable: yes' in out, 'alias': alias}


def back_btn(text, card):
    card(30, 30, BACK_BTN[2] - BACK_BTN[0], BACK_BTN[3] - BACK_BTN[1])
    text(140, 80, '‹ Back', 44, anchor='mm')


def btn(text, card, box, label, color=CYAN, size=48, bold=True):
    card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=color)
    glyph = len(label) == 1 and ord(label) >= 0xE000
    text((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, label, size, color, bold=bold, anchor='mm', icon=glyph)


def page_ssh(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    back_btn(text, card)
    text(290, 48, 'SSH', 56, bold=True)
    up = ((data.get('services') or {}).get('ssh') or {}).get('up')
    card(30, 170, 1020, 150)
    text(60, 215, 'Server', 34, MUTED)
    text(60, 255, 'Up' if up else 'Down' if up is False else 'Unknown', 48, GREEN if up else AMBER, bold=True)
    btn(text, card, SSH_TOGGLE, 'Turn off' if up else 'Turn on', AMBER if up else GREEN, 44)
    card(USER_BTN[0], USER_BTN[1], USER_BTN[2] - USER_BTN[0], USER_BTN[3] - USER_BTN[1])
    text(60, 375, 'User' + (' · tap to switch' if len(w['users']) > 1 else ''), 32, MUTED)
    text(60, 420, w['user'], 48, bold=True, max_width=960)
    text(40, 540, f"New password for {w['user']}", 36, MUTED, max_width=1000)
    card(30, 600, 1020, 160, outline=CYAN)
    shown = w['pw'] if w['show'] else '•' * len(w['pw'])
    text(60, 650, shown[-34:] or ' ', 52, mono=w['show'])
    text(1020, 800, f"{len(w['pw'])} characters (min 6)", 30, MUTED, anchor='ra')
    btn(text, card, CONNECT_BTN, 'Set password', GREEN if len(w['pw']) >= 6 else MUTED, 56)
    draw_kb(d, text, w)
    return image


def page_usb(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    back_btn(text, card)
    text(290, 48, 'USB mode', 56, bold=True)
    cur = usb_mode()
    for (m, title, hint), (_, box) in zip(USB_MODES, USB_BTNS):
        card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=GREEN if m == cur else LINE)
        text(60, box[1] + 40, title, 52, GREEN if m == cur else TEXT, bold=True)
        text(60, box[1] + 120, hint, 34, MUTED, max_width=960)
        if m == cur:
            text(1020, box[1] + 45, 'Active', 36, GREEN, anchor='ra')
    text(540, 1000, 'Switching drops the USB link for a second', 32, MUTED, anchor='mm')
    return image


def field(text, card, box, label, value, active, color=TEXT, mono=True):
    card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=CYAN if active else LINE)
    text(60, box[1] + 18, label, 30, MUTED)
    text(60, box[1] + 70, value[-36:] or ' ', 46, color, mono=mono)


def page_wifiinfo(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    back_btn(text, card)
    wifi, i = data.get('wifi') or {}, w.get('info')
    text(290, 48, wifi.get('ssid') or (i or {}).get('con') or 'Wi-Fi', 56, bold=True, max_width=760)
    if i is None:
        text(540, 600, 'Loading…', 48, MUTED, anchor='mm')
    else:
        first = lambda k: (i.get(k) or ['—'])[0]
        link = i.get('link') or ['—'] * 4
        rows = (('Signal', f"{fmt(wifi.get('signal_percent'), suffix='%')}"), ('IP address', first('IP4.ADDRESS')),
                ('IP mode', 'Static' if first('ipv4.method') == 'manual' else 'DHCP'), ('Gateway', first('IP4.GATEWAY')),
                ('DNS', ', '.join(i.get('IP4.DNS') or ['—'])), ('Band', f'{link[0]} · ch {link[1]}'),
                ('Link rate', link[2]), ('Security', link[3] or 'Open'), ('MAC', first('GENERAL.HWADDR')),
                ('IPv6', first('IP6.ADDRESS')))
        card(30, 170, 1020, 880)
        for n, (k, v) in enumerate(rows):
            text(60, 196 + n * 84, k, 34, MUTED)
            text(1020, 192 + n * 84, v, 38, anchor='ra', max_width=700)
    btn(text, card, IPCFG_BTN, 'IP & DNS settings', CYAN, 48)
    btn(text, card, SAVED_BTN, 'Saved networks', CYAN, 48)
    btn(text, card, CHANGE_BTN, 'Change network', CYAN, 48)
    return image


def page_saved(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    back_btn(text, card)
    text(290, 48, 'Saved Wi-Fi', 56, bold=True)
    if w['devs'] is None:
        text(540, 600, 'Loading…', 48, MUTED, anchor='mm')
    elif not w['devs']:
        text(540, 600, 'No saved networks', 48, MUTED, anchor='mm')
    for i, n in enumerate((w['devs'] or [])[:(TAB_Y - WIFI_TOP) // WIFI_ROW]):
        y = WIFI_TOP + i * WIFI_ROW
        card(30, y, 1020, WIFI_ROW - 15, outline=GREEN if n['active'] else LINE)
        text(60, y + 25, n['name'], 46, bold=n['active'], max_width=940)
        text(60, y + 85, 'Connected · tap to forget' if n['active'] else 'Tap to forget', 30,
             GREEN if n['active'] else MUTED)
    return image


def page_files(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    v, path = w['view'], w['path']
    if w.get('edit'):
        back_btn(text, card)
        text(290, 48, 'New folder', 56, bold=True)
        field(text, card, (30, 200, 1050, 360), f"in {path[-40:]}", w['pw'], True, mono=False)
        btn(text, card, CONNECT_BTN, 'Create folder', GREEN if w['pw'].strip() else MUTED, 56)
        draw_kb(d, text, w)
        return image
    tx = 40
    if v or w.get('dele') or path != '/':
        back_btn(text, card)
        tx = 290
    text(tx, 48, ('Delete what?' if w.get('dele') else os.path.basename(v['name'] if v else path) or '/'), 56,
         bold=True, max_width=660 - tx, color=AMBER if w.get('dele') else TEXT)
    can = writable(v['name'] if v else path, strict=bool(v))
    if not v:
        btn(text, card, MKDIR_BTN, '\U000F0257', CYAN if can else LINE, 64, False)
    btn(text, card, DEL_BTN, '\U000F0A7A', (AMBER if w.get('dele') or v else MUTED) if can else LINE, 64, False)
    text(40, 150, ('…' + path[-48:]) if len(path) > 49 else path, 30, MUTED, mono=True)
    clip = Image.new('RGB', (IMG[0], round((TAB_Y - FILE_TOP) * P)), BG)
    cd, ctext, ccard, _ = tools(clip)
    off = FILE_TOP + w['scroll']
    if v and 'img' in v:
        clip.paste(v['img'].resize((round(v['img'].width * P), round(v['img'].height * P))), (round(30 * P), -round(w['scroll'] * P)))
    elif v:
        first = w['scroll'] // FILE_LINE
        for n, line in enumerate(v['lines'][first:first + (TAB_Y - FILE_TOP) // FILE_LINE + 2], first):
            ctext(40, n * FILE_LINE - w['scroll'] + 4, line or ' ', 28, mono=True, max_width=1010)
    elif not w['ents']:
        ctext(540, 300, 'Empty folder', 48, MUTED, anchor='mm')
    else:
        first = w['scroll'] // FILE_ROW
        for n, e in enumerate(w['ents'][first:first + (TAB_Y - FILE_TOP) // FILE_ROW + 2], first):
            y = n * FILE_ROW - w['scroll']
            ccard(30, y, 1020, FILE_ROW - 12, outline=AMBER if w.get('dele') else LINE)
            ctext(60, y + 16, e['name'] + ('/' if e['dir'] else ''), 42, CYAN if e['dir'] else TEXT, max_width=700)
            when = dt.datetime.fromtimestamp(e['mtime']).strftime('%Y-%m-%d %H:%M') if e['mtime'] else ''
            ctext(60, y + 70, when, 26, MUTED)
            if w.get('dele'):
                ctext(1010, y + FILE_ROW // 2 - 6, '\U000F0A7A', 56, AMBER, anchor='rm', icon=True)
            else:
                ctext(1020, y + 30, '›' if e['dir'] else fmt_size(e['size']), 36, MUTED, anchor='ra')
    image.paste(clip, (0, round(FILE_TOP * P)))
    return image


def page_ipcfg(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    back_btn(text, card)
    text(290, 48, 'IP & DNS', 56, bold=True)
    auto = w['method'] == 'auto'
    btn(text, card, METHOD_BTN, 'Automatic (DHCP) · tap for static' if auto else 'Static · tap for DHCP',
        GREEN if auto else AMBER, 42)
    for (f, label, hint), (_, box) in zip(IP_FIELDS, IP_BOXES):
        off = auto and f != 'dns'
        field(text, card, box, label, 'automatic' if off else w[f] or hint, w['field'] == f and not off,
              MUTED if off or not w[f] else TEXT, mono=bool(w[f]) and not off)
    text(540, 920, 'Wrong settings are reverted if the gateway stops answering', 30, MUTED, anchor='mm')
    btn(text, card, CONNECT_BTN, 'Save and reconnect', GREEN, 52)
    draw_kb(d, text, w)
    return image


def page_ir(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    back_btn(text, card)
    text(290, 48, 'IR remote', 56, bold=True)
    brand = IR_BRANDS[w['brand']][0]
    btn(text, card, IR_BRAND_BTN, f'{brand} TV · tap to switch', CYAN, 44)
    codes = IR_BRANDS[w['brand']][2]
    for k, box in IR_BTNS:
        color = LINE if k not in codes else AMBER if k == 'Power' else CYAN if k == 'OK' else TEXT
        btn(text, card, box, '' if k in IR_ARROWS else k, color, 44)
        if k in IR_ARROWS:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            d.polygon([((cx + dx * 30) * P, (cy + dy * 30) * P) for dx, dy in IR_ARROWS[k]], fill=color)
    btn(text, card, IR_ALL_BTN, 'All TVs off (every brand)', AMBER, 44)
    text(540, 1340, 'Point the top edge of the phone at the device' if Path(IR).exists() else 'No IR device (/dev/lirc0)',
         32, MUTED if Path(IR).exists() else AMBER, anchor='mm')
    return image


def page_bt(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    back_btn(text, card)
    if w.get('edit'):
        text(290, 48, 'Bluetooth name', 56, bold=True)
        field(text, card, (30, 200, 1050, 360), 'Shown to other devices', w['pw'], True, mono=False)
        btn(text, card, CONNECT_BTN, 'Save name', GREEN if w['pw'].strip() else MUTED, 56)
        draw_kb(d, text, w)
        return image
    text(290, 48, 'Bluetooth', 56, bold=True)
    vis = w.get('vis')
    btn(text, card, BT_VIS, 'Visible' if vis else 'Hidden', GREEN if vis else MUTED, 44)
    btn(text, card, BT_NAME, (w.get('alias') or 'Name') + ' ›', CYAN, 40, False)
    btn(text, card, RESCAN_BTN, 'Rescan', CYAN, 40, False)
    on = w.get('on')
    btn(text, card, BT_POWER, 'On · tap to turn off' if on else 'Off · tap to turn on', GREEN if on else AMBER, 44)
    if w['devs'] is None:
        text(540, 700, 'Scanning…', 48, MUTED, anchor='mm')
    elif not w['devs']:
        text(540, 700, 'No devices found', 48, MUTED, anchor='mm')
    for i, dev in enumerate((w['devs'] or [])[:(TAB_Y - BT_TOP) // WIFI_ROW]):
        y = BT_TOP + i * WIFI_ROW
        card(30, y, 1020, WIFI_ROW - 15, outline=GREEN if dev['connected'] else LINE)
        text(60, y + 25, dev['name'], 46, bold=dev['connected'], max_width=720, color=TEXT if dev['named'] else MUTED)
        tag = 'Connected · tap to disconnect, hold to forget' if dev['connected'] else \
            'Paired · tap to connect, hold to forget' if dev['paired'] else 'Tap to pair'
        text(60, y + 85, tag, 30, GREEN if dev['connected'] else MUTED)
        text(1020, y + 30, dev['addr'], 28, MUTED, anchor='ra', mono=True)
    return image


def page_wifi(w):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    card(30, 30, BACK_BTN[2] - BACK_BTN[0], BACK_BTN[3] - BACK_BTN[1])
    text(140, 80, '‹ Back', 44, anchor='mm')
    n = w['sel']
    if n is None:
        text(290, 48, 'Wi-Fi', 56, bold=True)
        card(RESCAN_BTN[0], 30, RESCAN_BTN[2] - RESCAN_BTN[0], 100)
        text((RESCAN_BTN[0] + RESCAN_BTN[2]) // 2, 80, 'Rescan', 40, anchor='mm')
        if w['nets'] is None:
            text(540, 600, 'Scanning…', 48, MUTED, anchor='mm')
        elif not w['nets']:
            text(540, 600, 'No networks found', 48, MUTED, anchor='mm')
        for i, net in enumerate((w['nets'] or [])[:(2340 - WIFI_TOP) // WIFI_ROW]):
            y = WIFI_TOP + i * WIFI_ROW
            card(30, y, 1020, WIFI_ROW - 15, outline=GREEN if net['active'] else LINE)
            text(60, y + 25, net['ssid'], 46, bold=net['active'], max_width=700)
            tag = 'Connected' if net['active'] else 'Saved' if net['known'] else 'Locked' if net['secure'] else 'Open'
            text(60, y + 85, tag, 30, GREEN if net['active'] else MUTED)
            text(1020, y + 25, f"{net['signal']}%", 40, MUTED, anchor='ra')
            bar(800, y + 95, 220, net['signal'], h=12)
        return image
    text(290, 48, n['ssid'], 56, bold=True, max_width=760)
    text(40, 200, 'Password' + (' (saved one is replaced)' if n['known'] else ''), 36, MUTED)
    card(30, 260, 1020, 160, outline=CYAN)
    shown = w['pw'] if w['show'] else '•' * len(w['pw'])
    text(60, 310, shown[-34:] or ' ', 52, mono=w['show'])
    text(1020, 460, f"{len(w['pw'])} characters", 30, MUTED, anchor='ra')
    ok = len(w['pw']) >= 8 or not n['secure']
    card(CONNECT_BTN[0], CONNECT_BTN[1], CONNECT_BTN[2] - CONNECT_BTN[0], CONNECT_BTN[3] - CONNECT_BTN[1],
         outline=GREEN if ok else LINE)
    text(540, (CONNECT_BTN[1] + CONNECT_BTN[3]) // 2, 'Connect', 56, GREEN if ok else MUTED, bold=True, anchor='mm')
    draw_kb(d, text, w)
    return image


def hit(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


class Touch:
    """Multitouch protocol-B events (slot 0 only) -> tap / long / swipe / scroll gestures."""

    def __init__(self, xmax, ymax):
        self.sx, self.sy = W / (xmax + 1), H / (ymax + 1)
        self.slot = 0
        self.want = self.touching = self.long = False
        self.x = self.y = self.x0 = self.y0 = 0.0
        self.t0 = 0.0
        self.axis = None

    def feed(self, kind, code, value, now):
        if kind == 3:
            if code == 47:
                self.slot = value
            elif self.slot == 0:
                if code == 53:
                    self.x = value * self.sx
                elif code == 54:
                    self.y = value * self.sy
                elif code == 57:
                    self.want = value >= 0
        elif kind == 1 and code == 330:  # BTN_TOUCH
            self.want = bool(value)
        elif kind == 0 and code == 0:
            return self.sync(now)
        return []

    def sync(self, now):
        out = []
        if self.want and not self.touching:
            self.touching, self.long, self.axis = True, False, None
            self.x0, self.y0, self.t0 = self.x, self.y, now
        elif not self.want and self.touching:
            self.touching = False
            dx = self.x - self.x0
            if self.axis == 'h' and abs(dx) >= SWIPE_PX:
                out.append(('swipe', 'left' if dx < 0 else 'right'))
            elif self.axis == 'v':
                out.append(('scrollend',))
            elif self.axis is None and not self.long:
                out.append(('tap', self.x0, self.y0))
        elif self.touching and not self.long:
            dx, dy = self.x - self.x0, self.y - self.y0
            if self.axis is None and max(abs(dx), abs(dy)) > DRAG_PX:
                self.axis = 'h' if abs(dx) > abs(dy) else 'v'
                if self.axis == 'v':
                    out.append(('scrollstart',))
            if self.axis == 'v':
                out.append(('scroll', dy))
        return out

    def tick(self, now):
        if self.touching and self.axis is None and not self.long and now - self.t0 >= LONG_S:
            self.long = True
            return [('long', self.x0, self.y0)]
        return []


def containers_of(data):
    return (data.get('containers') or {}).get('containers') or []


def procs_of(data):
    # server order is already CPU then RSS; the top slice is what the Processes tab lists
    return (data.get('processes') or [])[:TOP_PROCS]


def items_of(data, page):
    return procs_of(data) if page == 2 else containers_of(data)


def row_at(data, y, scroll, page=1):
    if not LIST_TOP <= y < LIST_BOTTOM:
        return None
    idx = int((y - LIST_TOP + scroll) // ROW_HS[page])
    items = items_of(data, page)
    if not 0 <= idx < len(items):
        return None
    return items[idx] if page == 2 else items[idx].get('name')


def max_scroll(data, page=1):
    return max(0, len(items_of(data, page)) * ROW_HS[page] - (LIST_BOTTOM - LIST_TOP))


def container_actions(state):
    return [('pause', 'Pause'), ('stop', 'Stop')] if state == 'running' else \
        [('unpause', 'Unpause')] if state == 'paused' else [('start', 'Start')]


def boxes(n):
    return [ACT_W] if n == 1 else [ACT_L, ACT_R]


class Ui:
    def __init__(self):
        self.page, self.scroll, self.base = 0, 0, 0
        self.detail = self.info = self.confirm = None   # confirm = (act, arg, label, deadline)
        self.pdetail = None    # process detail: (pid, start)
        self.termed = {}       # (pid, start) -> time SIGTERM was accepted
        self.toast = ('', 0.0)
        self.wifi = None       # picker: {'nets', 'sel', 'pw', 'mode', 'show'}
        self.sub = None        # Network sub-page: {'kind': 'ssh' | 'usb' | 'bt', ...}
        self.pair = None       # Bluetooth agent request: {'name', 'kind', 'code'}
        self.files = {'kind': 'files', 'path': '/', 'ents': [], 'scroll': 0, 'view': None}   # Files tab state
        self.files_opened = False

    def tick(self, now):
        changed = False
        if self.confirm and now >= self.confirm[3]:
            self.confirm, changed = None, True
        if self.toast[0] and now >= self.toast[1]:
            self.toast, changed = ('', 0.0), True
        return changed

    def proc_row(self, data):
        pid, start = self.pdetail
        return next((r for r in data.get('processes') or [] if r.get('pid') == pid and r.get('start') == start), None)

    def proc_action(self, data, now):
        """(act, label, enabled) for the process detail button."""
        row = self.proc_row(data)
        if row is None:
            return None
        t = self.termed.get(self.pdetail)
        if t is not None and now - t >= GRACE_S:
            return 'kill', 'Force kill (SIGKILL)', row.get('killable', False)
        return 'term', 'Kill (SIGTERM)', row.get('killable', False)

    def on_gesture(self, g, data, now):
        """Apply one gesture; returns effects for the main loop: ('blank',), ('detail', n), ('act', act, arg)."""
        kind = g[0]
        if self.pair:
            if kind == 'tap' and (hit(CANCEL_BTN, g[1], g[2]) or hit(RESTART_BTN, g[1], g[2])):
                self.pair = None
                return [('pair', hit(RESTART_BTN, g[1], g[2]))]
            return []
        if self.confirm:
            if kind == 'tap':
                act, arg = self.confirm[:2]
                if hit(CANCEL_BTN, g[1], g[2]):
                    self.confirm = None
                elif hit(RESTART_BTN, g[1], g[2]):
                    self.confirm = None
                    if act.startswith('forget'):
                        self.toast = ('Forgetting…', float('inf'))
                        return [('wifi', *arg)]
                    if act.startswith('delete'):
                        self.delete(arg, now)
                        return []
                    return [('act', act, arg)]
            return []
        if self.wifi is not None:
            return self.on_wifi(g, now)
        if self.sub is not None:
            return self.on_sub(g, data, now)
        if self.page == FILES_PAGE and kind != 'swipe' and (self.files.get('edit') or not (kind == 'tap' and g[2] >= TAB_Y)):
            self.sub = self.files            # the Files tab reuses the sub-page handlers
            try:
                return self.on_sub(g, data, now)
            finally:
                self.sub = None
        if self.detail or self.pdetail:
            if (kind == 'tap' and hit(BACK_BTN, g[1], g[2])) or (kind == 'swipe' and g[1] == 'right'):
                self.detail = self.info = self.pdetail = None
            elif kind == 'long' and self.detail:
                self.confirm = ('restart', self.detail, self.detail, now + CONFIRM_S)
            elif kind == 'tap' and self.detail:
                state = (self.info or {}).get('state') or next(
                    (c.get('state') for c in containers_of(data) if c.get('name') == self.detail), None)
                acts = container_actions(state)
                for (act, _), box in zip(acts, boxes(len(acts))):
                    if hit(box, g[1], g[2]):
                        self.confirm = (act, self.detail, self.detail, now + CONFIRM_S)
            elif kind == 'tap' and hit(ACT_W, g[1], g[2]):
                pa, row = self.proc_action(data, now), self.proc_row(data)
                if pa and pa[2]:
                    self.confirm = (pa[0], self.pdetail, f"{row['name']} ({row['pid']})", now + CONFIRM_S)
                elif pa:
                    self.toast = ("Not your process: can't kill", now + 4)
            return []
        if kind == 'scrollstart':
            self.base = self.scroll
        elif kind == 'scroll' and self.page in (1, 2):
            self.scroll = int(min(max_scroll(data, self.page), max(0, self.base - g[1])))
        elif kind == 'swipe':
            self.page = min(len(TABS) - 1, max(0, self.page + (1 if g[1] == 'left' else -1)))
            self.scroll = 0
        elif kind == 'tap':
            x, y = g[1], g[2]
            hitrow = row_at(data, y, self.scroll, self.page) if self.page in (1, 2) else None
            if y >= TAB_Y:
                self.page, self.scroll = min(len(TABS) - 1, int(x // TAB_W)), 0
            elif y < LIST_TOP:
                return [('blank',)]
            elif hitrow and self.page == 1:
                self.detail, self.info = hitrow, None
                return [('detail', self.detail)]
            elif hitrow:
                self.pdetail = (hitrow['pid'], hitrow['start'])
            elif self.page == 0:
                for box, page in ((OV_BATT, len(TABS) - 1), (OV_NET, 3), (OV_CONT, 1), (OV_PROC, 2)):
                    if hit(box, x, y):
                        self.page = page
            elif self.page == 3 and NET_TOP <= y < NET_TOP + len(NET_ROWS) * NET_ROW:
                key = NET_ROWS[int((y - NET_TOP) // NET_ROW)][0]
                up = ((data.get('services') or {}).get(key) or {}).get('up')
                if key == 'tailscale':
                    if up:
                        self.confirm = ('ts_off', None, 'Tailscale', now + CONFIRM_S)
                        return []
                    return [('wifi', svc, 'tailscaled', True)]
                if key == 'ssh':
                    users = login_users()
                    self.sub = {'kind': 'ssh', 'users': users, 'user': users[0], 'pw': '', 'mode': 'abc', 'show': False}
                elif key == 'usb':
                    self.sub = {'kind': 'usb'}
                elif key == 'ir':
                    self.sub = {'kind': 'ir', 'brand': 0}
                else:
                    self.sub = {'kind': 'bt', 'devs': None, 'edit': False, **bt_state()}
                    return [('wifi', bt_scan)]
            elif self.page == 3 and hit(WIFI_CARD, x, y):
                self.sub = {'kind': 'wifi', 'info': None}
                return [('wifi', wifi_info)]
            elif self.page == len(TABS) - 1:
                for act, box in POWER_BTNS:
                    if hit(box, x, y):
                        self.confirm = (act, None, 'the phone', now + CONFIRM_S)
        elif kind == 'long' and self.page == 1 and row_at(data, g[2], self.scroll):
            name = row_at(data, g[2], self.scroll)
            self.confirm = ('restart', name, name, now + CONFIRM_S)
        return []

    def on_wifi(self, g, now):
        w, kind = self.wifi, g[0]
        if (kind == 'tap' and hit(BACK_BTN, g[1], g[2])) or (kind == 'swipe' and g[1] == 'right'):
            if w['sel']:
                w['sel'] = None
            else:
                self.wifi = None
            return []
        if kind not in ('tap', 'long'):
            return []
        x, y = g[1], g[2]
        if w['sel'] is None:
            if kind == 'tap' and hit(RESCAN_BTN, x, y):
                w['nets'] = None
                return [('wifi', wifi_scan)]
            nets, i = w['nets'] or [], int((y - WIFI_TOP) // WIFI_ROW) if y >= WIFI_TOP else -1
            if 0 <= i < len(nets):
                n = nets[i]
                if kind == 'tap' and (n['known'] or not n['secure']):   # long press = enter a new password
                    self.toast = (f"Connecting to {n['ssid']}…", float('inf'))
                    return [('wifi', wifi_connect, n['ssid'], None, n['known'])]
                w.update(sel=n, pw='', mode='abc', show=False)
            return []
        if kind != 'tap':
            return []
        n = w['sel']
        if hit(CONNECT_BTN, x, y):
            if len(w['pw']) >= 8 or not n['secure']:
                self.toast = (f"Connecting to {n['ssid']}…", float('inf'))
                return [('wifi', wifi_connect, n['ssid'], w['pw'], n['known'])]
            return []
        kb_type(w, next((k for k, box in kb_keys(w['mode']) if hit(box, x, y)), None))
        return []

    def open_dir(self, path, keep_scroll=False):
        w = self.sub or self.files
        try:
            w.update(path=path, ents=list_dir(path), scroll=w['scroll'] if keep_scroll else 0, view=None, edit=False,
                     dele=w.get('dele') and keep_scroll)
        except OSError as exc:
            self.toast = (f"{os.path.basename(path) or '/'}: {exc.strerror or 'cannot open'}", time.monotonic() + 4)

    def ask_delete(self, full, isdir, now):
        if writable(full, strict=True):
            self.confirm = ('delete_dir' if isdir else 'delete', full, os.path.basename(full), now + CONFIRM_S)
        else:
            self.toast = ('Read-only here (only /home/philipp and /mnt/sd)', now + 4)

    def delete(self, path, now):
        """Runs on the UI thread. ponytail: a huge tree stalls the screen while rmtree runs; move to dpool if that bites."""
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            msg = f'Deleted {os.path.basename(path)}'
        except OSError as exc:
            msg = f'Delete failed: {exc.strerror or exc}'
        self.toast = (msg, now + 4)
        self.open_dir(self.files['path'], keep_scroll=True)

    def on_files(self, g, x, y, now):
        w = self.sub
        if w.get('edit'):
            if hit(CONNECT_BTN, x, y):
                name, full = w['pw'].strip(), os.path.join(w['path'], w['pw'].strip())
                if not name or '/' in name or name in ('.', '..'):
                    self.toast = ('Invalid folder name', now + 4)
                    return []
                try:
                    os.mkdir(full)
                    st = os.stat(w['path'])
                    os.chown(full, st.st_uid, st.st_gid)      # owned like its parent, not root
                    self.toast = (f'Created {name}', now + 4)
                except OSError as exc:
                    self.toast = (f'mkdir failed: {exc.strerror or exc}', now + 4)
                self.open_dir(w['path'], keep_scroll=True)
            else:
                kb_type(w, next((k for k, box in kb_keys(w['mode']) if hit(box, x, y)), None))
            return []
        if hit(DEL_BTN, x, y):
            if w['view']:
                self.ask_delete(w['view']['name'], False, now)
            elif writable(w['path']):
                w['dele'] = not w.get('dele')
            else:
                self.toast = ('Read-only here (only /home/philipp and /mnt/sd)', now + 4)
            return []
        if hit(MKDIR_BTN, x, y) and not w['view']:
            if writable(w['path']):
                w.update(edit=True, pw='', mode='abc', show=True)
            else:
                self.toast = ('Read-only here (only /home/philipp and /mnt/sd)', now + 4)
            return []
        if w['view'] is None and y >= FILE_TOP:
            i = int((y - FILE_TOP + w['scroll']) // FILE_ROW)
            if 0 <= i < len(w['ents']):
                e = w['ents'][i]
                full = os.path.join(w['path'], e['name'])
                if w.get('dele') or g[0] == 'long':
                    self.ask_delete(full, e['dir'], now)
                elif e['dir']:
                    self.open_dir(full)
                else:
                    w.update(view={'name': full, **file_view(full)}, scroll=0)
        return []

    def on_sub(self, g, data, now):
        """SSH / USB / Bluetooth sub-pages of the Network tab."""
        w, kind = self.sub, g[0]
        if (kind == 'tap' and hit(BACK_BTN, g[1], g[2])) or (kind == 'swipe' and g[1] == 'right'):
            if w['kind'] == 'ipcfg':
                self.sub = {'kind': 'wifi', 'info': w['info']}
            elif w['kind'] == 'saved':                       # a forgotten network can change the details
                self.sub = {'kind': 'wifi', 'info': None}
                return [('wifi', wifi_info)]
            elif w['kind'] == 'files' and (w.get('edit') or w.get('dele')):
                w.update(edit=False, dele=False)
            elif w['kind'] == 'files' and w['view']:
                w.update(view=None, scroll=w.get('dir_scroll', 0))
            elif w['kind'] == 'files' and w['path'] != '/':
                self.open_dir(os.path.dirname(w['path'].rstrip('/')) or '/')
            elif w.get('edit'):
                w['edit'] = False
            else:
                self.sub = None
            return []
        if w['kind'] == 'files' and kind in ('scrollstart', 'scroll') and not w.get('edit'):
            if kind == 'scrollstart':
                w['base'] = w['scroll']
                return []
            v = w['view']
            if v and 'img' in v:
                total = v['img'].height
            else:
                total = len(v['lines']) * FILE_LINE if v else len(w['ents']) * FILE_ROW
            w['scroll'] = int(min(max(0, total - (TAB_Y - FILE_TOP) + 40), max(0, w['base'] - g[1])))
            return []
        if kind == 'long' and w['kind'] == 'files' and not (w['view'] or w.get('edit')):
            return self.on_files(g, g[1], g[2], now)
        if kind == 'long' and w['kind'] == 'bt' and not w.get('edit'):
            devs, i = w['devs'] or [], int((g[2] - BT_TOP) // WIFI_ROW) if g[2] >= BT_TOP else -1
            if 0 <= i < len(devs) and devs[i]['paired']:
                dev = devs[i]
                self.confirm = ('forget_bt', (bt_forget, dev['addr'], dev['name']), dev['name'], now + CONFIRM_S)
            return []
        if kind != 'tap':
            return []
        x, y = g[1], g[2]
        if w['kind'] == 'files':
            if w['view'] is None:
                w['dir_scroll'] = w['scroll']
            return self.on_files(g, x, y, now)
        if w['kind'] == 'saved':
            nets, i = w['devs'] or [], int((y - WIFI_TOP) // WIFI_ROW) if y >= WIFI_TOP else -1
            if 0 <= i < len(nets):
                n = nets[i]
                self.confirm = ('forget_wifi_active' if n['active'] else 'forget_wifi', (wifi_forget, n['name']),
                                n['name'], now + CONFIRM_S)
            return []
        if w['kind'] == 'ssh':
            if hit(SSH_TOGGLE, x, y):
                if ((data.get('services') or {}).get('ssh') or {}).get('up'):
                    self.confirm = ('ssh_off', None, 'SSH', now + CONFIRM_S)
                    return []
                return [('wifi', svc, 'sshd', True)]
            if hit(USER_BTN, x, y):
                u = w['users']
                w['user'] = u[(u.index(w['user']) + 1) % len(u)]
            elif hit(CONNECT_BTN, x, y):
                if len(w['pw']) >= 6:
                    pw, w['pw'] = w['pw'], ''
                    self.toast = (f"Setting password for {w['user']}…", float('inf'))
                    return [('wifi', set_password, w['user'], pw)]
            else:
                kb_type(w, next((k for k, box in kb_keys(w['mode']) if hit(box, x, y)), None))
        elif w['kind'] == 'usb':
            for m, box in USB_BTNS:
                if hit(box, x, y) and m != usb_mode():
                    self.toast = ('Switching USB mode…', float('inf'))
                    return [('wifi', usb_set, m)]
        elif w['kind'] == 'wifi':
            i = w['info']
            if hit(CHANGE_BTN, x, y):
                self.wifi = {'nets': None, 'sel': None, 'pw': '', 'mode': 'abc', 'show': False}
                return [('wifi', wifi_scan)]
            if hit(SAVED_BTN, x, y):
                self.sub = {'kind': 'saved', 'devs': None}
                return [('wifi', wifi_saved)]
            if hit(IPCFG_BTN, x, y) and i and i.get('con'):
                first = lambda *ks: next((i[k][0] for k in ks if i.get(k)), '')
                method = 'manual' if first('ipv4.method') == 'manual' else 'auto'
                self.sub = {'kind': 'ipcfg', 'info': i, 'con': i['con'], 'method': method,
                            'addr': first('ipv4.addresses', 'IP4.ADDRESS'), 'gw': first('ipv4.gateway', 'IP4.GATEWAY'),
                            'dns': first('ipv4.dns'), 'field': 'addr' if method == 'manual' else 'dns',
                            'mode': 'sym', 'show': True}
        elif w['kind'] == 'ipcfg':
            if hit(METHOD_BTN, x, y):
                w['method'] = 'auto' if w['method'] == 'manual' else 'manual'
                w['field'] = 'addr' if w['method'] == 'manual' else 'dns'
            elif hit(CONNECT_BTN, x, y):
                self.toast = ('Applying, Wi-Fi reconnects…', float('inf'))
                return [('wifi', ip_apply, w['con'], w['method'], w['addr'].strip(), w['gw'].strip(), w['dns'].strip())]
            elif any(hit(box, x, y) for _, box in IP_BOXES):
                f = next(f for f, box in IP_BOXES if hit(box, x, y))
                if w['method'] == 'manual' or f == 'dns':
                    w['field'] = f
            else:
                kb_type(w, next((k for k, box in kb_keys(w['mode']) if hit(box, x, y)), None), w['field'])
        elif w['kind'] == 'ir':
            brand, proto, codes = IR_BRANDS[w['brand']]
            if hit(IR_BRAND_BTN, x, y):
                w['brand'] = (w['brand'] + 1) % len(IR_BRANDS)
            elif hit(IR_ALL_BTN, x, y):
                self.toast = ('Sending power to every brand…', float('inf'))
                return [('wifi', ir_all_off)]
            for k, box in IR_BTNS:
                if hit(box, x, y) and k in codes:
                    p, code = codes[k] if isinstance(codes[k], tuple) else (proto, codes[k])
                    return [('wifi', ir_send, p, code, f'{brand} {k}')]
        elif w['kind'] == 'bt' and w.get('edit'):
            if hit(CONNECT_BTN, x, y):
                name = w['pw'].strip()
                if name:
                    w.update(edit=False, alias=name)
                    return [('wifi', bt_rename, name)]
            else:
                kb_type(w, next((k for k, box in kb_keys(w['mode']) if hit(box, x, y)), None))
        elif w['kind'] == 'bt':
            if hit(BT_VIS, x, y):
                w['vis'] = not w.get('vis')
                return [('wifi', bt_vis, w['vis'])]
            if hit(BT_NAME, x, y):
                w.update(edit=True, pw=w.get('alias') or '', mode='abc', show=True)
                return []
            if hit(RESCAN_BTN, x, y):
                w['devs'] = None
                return [('wifi', bt_scan)]
            if hit(BT_POWER, x, y):
                w['on'] = not w.get('on')
                return [('wifi', bt_power, w['on'])]
            devs, i = w['devs'] or [], int((y - BT_TOP) // WIFI_ROW) if y >= BT_TOP else -1
            if 0 <= i < len(devs):
                dev = devs[i]
                how = 'disconnect' if dev['connected'] else 'connect' if dev['paired'] else 'pair'
                self.toast = (f"{how.capitalize()}ing {dev['name']}…", float('inf'))
                return [('wifi', bt_act, dev['addr'], dev['name'], how)]
        return []

    def render(self, data, stale, now):
        if self.page == FILES_PAGE and not self.files_opened:
            self.files_opened = True
            self.open_dir(HOME_DIR if os.path.isdir(HOME_DIR) else '/')
        if self.wifi is not None:
            image = page_wifi(self.wifi)
        elif self.sub is not None:
            image = {'ssh': page_ssh, 'usb': page_usb, 'bt': page_bt, 'wifi': page_wifiinfo, 'ipcfg': page_ipcfg,
                     'ir': page_ir, 'saved': page_saved, 'files': page_files}[self.sub['kind']](self.sub, data)
        elif self.detail:
            image = page_detail(self.detail, self.info, data)
        elif self.pdetail:
            image = page_proc_detail(self, data, now)
        elif self.page == 0:
            image = render(data, stale)
        elif self.page == FILES_PAGE:
            image = page_files(self.files, data)
        else:
            image = {1: page_containers, 2: page_processes, 3: page_network, 5: page_battery}[self.page](
                data, stale, *((self.scroll,) if self.page in (1, 2) else ()))
        d, text, card, bar = tools(image)
        typing = self.page == FILES_PAGE and self.files.get('edit')     # keyboard needs the tab bar's space
        if not (self.detail or self.pdetail or self.wifi is not None or self.sub is not None or typing):
            d.rectangle((0, TAB_Y * P, IMG[0], IMG[1]), fill=PANEL)
            d.line((0, TAB_Y * P, IMG[0], TAB_Y * P), fill=LINE, width=1)
            for i, name in enumerate(TABS):
                cx, on = i * TAB_W + TAB_W // 2, i == self.page
                if on:
                    d.rounded_rectangle(((cx - 50) * P, (TAB_Y + 6) * P, (cx + 50) * P, (TAB_Y + 14) * P), 4, fill=CYAN)
                if ICON_FONT.exists():
                    text(cx, (TAB_Y + H) // 2 + 4, TAB_ICONS[i], 76, CYAN if on else MUTED, anchor='mm', icon=True)
                else:
                    text(cx, (TAB_Y + H) // 2, name[:5], 32, CYAN if on else MUTED, bold=on, anchor='mm')
        if self.toast[0]:
            card(30, TAB_Y - 150, 1020, 110, fill=BG, outline=AMBER)
            text(60, TAB_Y - 122, self.toast[0], 38, AMBER, max_width=960)
        if self.confirm:
            image = Image.blend(image, Image.new('RGB', image.size, 'black'), 0.75)
            d, text, card, bar = tools(image)
            card(90, 800, 900, 520, fill=PANEL, outline=AMBER)
            verb, hint = CONFIRM_TEXT[self.confirm[0]]
            text(540, 900, f'{verb} {self.confirm[2]}?', 52, bold=True, anchor='mm', max_width=820)
            text(540, 1000, hint, 32, MUTED, anchor='mm')
            card(*CANCEL_BTN[:2], CANCEL_BTN[2] - CANCEL_BTN[0], CANCEL_BTN[3] - CANCEL_BTN[1])
            text(320, 1195, 'Cancel', 44, anchor='mm')
            card(*RESTART_BTN[:2], RESTART_BTN[2] - RESTART_BTN[0], RESTART_BTN[3] - RESTART_BTN[1],
                 fill=AMBER, outline=AMBER)
            text(760, 1195, verb, 44, BG, bold=True, anchor='mm')
        if self.pair:
            p = self.pair
            image = Image.blend(image, Image.new('RGB', image.size, 'black'), 0.75)
            d, text, card, bar = tools(image)
            card(90, 760, 900, 560, fill=PANEL, outline=CYAN)
            text(540, 840, 'Allow connection?' if p['kind'] == 'connect' else 'Pair with', 40, MUTED, anchor='mm')
            text(540, 910, p['name'], 52, bold=True, anchor='mm', max_width=820)
            if p['code']:
                text(540, 1010, p['code'], 84, CYAN, bold=True, anchor='mm', mono=True)
                text(540, 1085, 'Same code on the other device?', 30, MUTED, anchor='mm')
            card(*CANCEL_BTN[:2], CANCEL_BTN[2] - CANCEL_BTN[0], CANCEL_BTN[3] - CANCEL_BTN[1])
            text(320, 1195, 'Reject', 44, anchor='mm')
            card(*RESTART_BTN[:2], RESTART_BTN[2] - RESTART_BTN[0], RESTART_BTN[3] - RESTART_BTN[1],
                 fill=GREEN, outline=GREEN)
            text(760, 1195, 'Allow' if p['kind'] == 'connect' else 'Pair', 44, BG, bold=True, anchor='mm')
        return image


def tools(image):
    d = ImageDraw.Draw(image)

    def text(x, y, value, size, color=TEXT, bold=False, max_width=None, anchor='la', mono=False, icon=False):
        value = str(value if value is not None else '—')
        f = font(max(8, round(size * P)), bold, mono, icon)
        if max_width:
            while len(value) > 1 and d.textlength(value, font=f) > max_width * P:
                value = value[:-2] + '…'
        d.text((x * P, y * P), value, fill=color, font=f, anchor=anchor)

    def card(x, y, w, h, fill=PANEL, outline=LINE):
        d.rounded_rectangle((x * P, y * P, (x + w) * P, (y + h) * P), 16, fill=fill, outline=outline, width=1)

    def bar(x, y, w, percent, h=16, color=CYAN):
        d.rounded_rectangle((x * P, y * P, (x + w) * P, (y + h) * P), 5, fill=LINE)
        if percent is not None and percent > 0:
            d.rounded_rectangle((x * P, y * P, (x + w * min(100, max(0, percent)) / 100) * P, (y + h) * P), 5, fill=color)

    return d, text, card, bar


def header(image, title, stale, right=None):
    d, text, card, bar = tools(image)
    d.rectangle((0, 0, IMG[0], (LIST_TOP - 10) * P), fill=BG)
    text(40, 36, title, 60, bold=True)
    text(1040, 50, right or ('Offline' if stale else 'Live'), 34,
         AMBER if stale else MUTED if right else GREEN, anchor='ra')


def page_containers(data, stale, scroll):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    c = data.get('containers') or {}
    items = containers_of(data)
    if not items:
        text(40, 200, 'No containers' if c.get('available') else 'Status unavailable', 40, MUTED)
    for i, it in enumerate(items):
        y = LIST_TOP + i * ROW_H - scroll
        if y + ROW_H < LIST_TOP or y > LIST_BOTTOM:
            continue
        up = it.get('state') == 'running'
        card(30, y, 1020, ROW_H - 16)
        text(60, y + 22, it.get('name') or 'Unnamed', 46, bold=True, max_width=700)
        text(1020, y + 28, it.get('state') or 'Unknown', 38, GREEN if up else AMBER, anchor='ra')
        text(60, y + 92, f"{it.get('image') or ''} · {it.get('status') or ''}", 30, MUTED, max_width=960)
    total = len(items) * ROW_H
    if total > LIST_BOTTOM - LIST_TOP:
        span = LIST_BOTTOM - LIST_TOP
        top = LIST_TOP + span * scroll / total
        d.rounded_rectangle((1066 * P, top * P, 1074 * P, (top + span * span / total) * P), 3, fill=MUTED)
    header(image, 'Containers', stale,
           f"{sum(i.get('state') == 'running' for i in items)} / {len(items)} running" if items else None)
    return image


def page_processes(data, stale, scroll):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    rows = procs_of(data)
    rh = ROW_HS[2]
    if not rows:
        text(40, 200, 'No process data', 40, MUTED)
    for i, r in enumerate(rows):
        y = LIST_TOP + i * rh - scroll
        if y + rh < LIST_TOP or y > LIST_BOTTOM:
            continue
        card(30, y, 1020, rh - 14)
        text(60, y + 12, r.get('name'), 40, bold=True, max_width=520)
        text(600, y + 18, r.get('user'), 30, TEXT if r.get('killable') else MUTED, max_width=180)
        text(1020, y + 12, f"{fmt(r.get('cpu_percent'), 0, '%')}  {fmt_bytes(r.get('rss_bytes'))}", 36, anchor='ra')
        text(60, y + 62, r.get('cmd') or r.get('name'), 26, MUTED, max_width=960)
    total = len(rows) * rh
    if total > LIST_BOTTOM - LIST_TOP:
        span = LIST_BOTTOM - LIST_TOP
        top = LIST_TOP + span * scroll / total
        d.rounded_rectangle((1066 * P, top * P, 1074 * P, (top + span * span / total) * P), 3, fill=MUTED)
    header(image, 'Processes', stale, f'top {len(rows)} by CPU')
    return image


def fmt_bytes(n):
    if n is None:
        return '—'
    return f'{n / 1048576:.0f} MiB' if n < 1 << 30 else f'{n / (1 << 30):.1f} GiB'


def page_proc_detail(ui, data, now):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    card(30, 30, BACK_BTN[2] - BACK_BTN[0], BACK_BTN[3] - BACK_BTN[1])
    text(140, 80, '‹ Back', 44, anchor='mm')
    r = ui.proc_row(data)
    text(290, 48, f"{r['name']} ({r['pid']})" if r else 'Process', 56, bold=True, max_width=760)
    if r is None:
        text(40, 170, 'gone', 64, AMBER, bold=True)
        return image
    text(40, 170, 'running', 64, GREEN, bold=True)
    card(30, 280, 1020, 560)
    for i, (k, v) in enumerate((('User', r.get('user')), ('CPU', fmt(r.get('cpu_percent'), 1, '%')),
                                ('Memory (RSS)', fmt_bytes(r.get('rss_bytes'))),
                                ('Kill', 'allowed' if r.get('killable') else 'not your process'))):
        text(60, 310 + i * 75, k, 34, MUTED)
        text(1020, 305 + i * 75, v, 40, anchor='ra', max_width=700)
    text(60, 640, 'Command', 34, MUTED)
    for j, line in enumerate(textwrap.wrap(r.get('cmd') or r.get('name') or '', 50)[:4]):
        text(60, 690 + j * 40, line, 28, TEXT, mono=True)
    pa = ui.proc_action(data, now)
    if ui.termed.get(ui.pdetail) is not None:
        text(540, 2090, 'SIGTERM sent: still running' if pa and pa[0] == 'kill' else 'SIGTERM sent…', 32, AMBER, anchor='mm')
    ok = pa[2]
    card(*ACT_W[:2], ACT_W[2] - ACT_W[0], ACT_W[3] - ACT_W[1], fill=AMBER if ok else PANEL, outline=AMBER if ok else LINE)
    text(540, 2220, pa[1], 48, BG if ok else MUTED, bold=True, anchor='mm')
    return image


def page_network(data, stale):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, 'Network', stale)
    wifi, services = data.get('wifi') or {}, data.get('services') or {}
    card(30, 170, 1020, 520)
    text(60, 195, 'Wi-Fi', 34, MUTED)
    text(60, 250, wifi.get('ssid') or ('Disconnected' if wifi.get('connected') is False else 'unknown'), 64,
         bold=True, max_width=960)
    text(60, 350, wifi.get('ip_address'), 44, max_width=960)
    sig = wifi.get('signal_percent')
    text(60, 440, f"Signal {fmt(sig, suffix='%')}", 36, MUTED)
    bar(60, 500, 960, sig)
    text(60, 555, f"{wifi.get('interface') or '—'} · {wifi.get('rate_mbps') or '—'}", 34, MUTED, max_width=960)
    for i, (key, label) in enumerate(NET_ROWS):
        up = (services.get(key) or {}).get('up')
        y = NET_TOP + i * NET_ROW
        card(30, y, 1020, NET_ROW - 15)
        text(60, y + 35, label, 52, bold=True)
        state = 'Up' if up is True else 'Down' if up is False else 'Unknown'
        if key == 'usb':
            m = usb_mode()
            state, up = dict((k, t) for k, t, _ in USB_MODES).get(m, state), m != 'charge'
        elif key == 'bluetooth':
            state = 'On' if up else 'Off' if up is False else state
        elif key == 'ir':
            up = Path(IR).exists()
            state = 'Ready' if up else 'Missing'
        text(980, y + 35, state, 48, GREEN if up is True else AMBER if up is False else MUTED, bold=True, anchor='ra')
        text(1030, y + 33, '›', 52, MUTED, anchor='ra')
    text(1030, 190, '›', 52, MUTED, anchor='ra')
    return image


def page_battery(data, stale):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    header(image, 'Battery & power', stale)
    b = data.get('battery') or {}
    card(30, 170, 1020, 520)
    text(60, 200, fmt(b.get('percent'), suffix='%'), 150, bold=True)
    text(60, 400, b.get('status') or 'No battery gauge', 40, MUTED, max_width=960)
    bar(60, 480, 960, b.get('percent'), h=28, color=GREEN)
    text(60, 560, 'Estimated from battery voltage' if b.get('name') else '', 30, MUTED)
    up = data.get('uptime_seconds')
    for i, (label, value) in enumerate((
            ('Voltage', fmt(b.get('voltage_v'), 3, ' V')),
            ('Temperature', fmt(b.get('temperature_c'), 1, ' °C')),
            ('Current', fmt(b.get('current_ma'), 0, ' mA')),
            ('Uptime', '—' if up is None else f'{int(up) // 86400}d {int(up) % 86400 // 3600}h {int(up) % 3600 // 60}m'))):
        y = 730 + i * 150
        card(30, y, 1020, 130)
        text(60, y + 38, label, 40, MUTED)
        text(1020, y + 30, value, 52, bold=True, anchor='ra')
    th = data.get('thermal') or {}
    if th.get('sensor'):
        hot = th.get('over', 0) > 0
        card(30, 1330, 1020, 220, outline=AMBER if hot else None)
        text(60, 1358, 'Thermal guard', 40, MUTED)
        text(1020, 1350, f"{th['sensor'].replace('iio:', '')} {th['temp_c']:.1f} °C", 48, AMBER if hot else TEXT, bold=True, anchor='ra', max_width=700)
        text(60, 1440, th.get('last_event') or 'no thermal events', 30, AMBER if hot else MUTED, max_width=960)
        if th.get('charge_mode'):
            text(60, 1495, f"charge: {th['charge_mode']}", 30, MUTED)
    for act, (x0, y0, x1, y1) in POWER_BTNS:
        card(x0, y0, x1 - x0, y1 - y0, outline=AMBER if act == 'poweroff' else CYAN)
        text((x0 + x1) // 2, (y0 + y1) // 2, CONFIRM_TEXT[act][0], 56, bold=True, anchor='mm')
    return image


def page_detail(name, info, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    card(30, 30, BACK_BTN[2] - BACK_BTN[0], BACK_BTN[3] - BACK_BTN[1])
    text(140, 80, '‹ Back', 44, anchor='mm')
    text(290, 48, name, 56, bold=True, max_width=760)
    base = next((c for c in containers_of(data) if c.get('name') == name), {})
    info = info or {}
    state = info.get('state') or base.get('state') or '…'
    text(40, 170, state, 64, GREEN if state == 'running' else AMBER, bold=True)
    if info.get('health'):
        text(1040, 190, info['health'], 40, MUTED, anchor='ra')
    card(30, 280, 1020, 620)
    rows = (('Uptime', info.get('uptime') or base.get('status') or '…'),
            ('Image', info.get('image') or base.get('image') or '…'),
            ('Restarts', info.get('restarts') if info.get('restarts') is not None else '…'))
    for i, (k, v) in enumerate(rows):
        text(60, 310 + i * 75, k, 34, MUTED)
        text(1020, 305 + i * 75, v, 40, anchor='ra', max_width=760)
    text(60, 545, 'CPU', 34, MUTED)
    text(1020, 540, fmt(info.get('cpu'), 1, '%'), 40, anchor='ra')
    bar(60, 600, 960, info.get('cpu'))
    text(60, 655, 'Memory', 34, MUTED)
    text(1020, 650, info.get('mem') or '—', 40, anchor='ra')
    bar(60, 710, 960, info.get('mem_percent'))
    text(60, 775, 'Updated every 5 s' if info else 'Loading…', 28, MUTED)
    card(30, 930, 1020, 1120)
    text(60, 955, 'Last log lines', 34, MUTED)
    y = 1015
    for line in (info.get('logs') or ['…' if not info else '(no output)'])[-24:]:
        text(60, y, line, 27, TEXT, mono=True)
        y += 42
    text(540, 2100, 'Hold anywhere to restart', 28, MUTED, anchor='mm')
    acts = container_actions(state)
    for (act, label), box in zip(acts, boxes(len(acts))):
        card(box[0], box[1], box[2] - box[0], box[3] - box[1], fill=AMBER, outline=AMBER)
        text((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, label, 48, BG, bold=True, anchor='mm')
    return image


def docker(*args, timeout=6):
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=timeout, env=DOCKER_ENV)


def clean_log(line):
    return SECRET_RE.sub(r'\1***', ANSI_RE.sub('', line))


def container_detail(name):
    out = {'name': name}
    try:
        insp = json.loads(docker('inspect', name).stdout)[0]
        st = insp.get('State') or {}
        out.update(state=st.get('Status'), health=(st.get('Health') or {}).get('Status'),
                   restarts=insp.get('RestartCount'), image=(insp.get('Config') or {}).get('Image'))
        if st.get('Running') and st.get('StartedAt'):
            started = dt.datetime.fromisoformat(st['StartedAt'][:19]).replace(tzinfo=dt.timezone.utc)
            secs = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds())
            out['uptime'] = (f'{secs // 86400}d {secs % 86400 // 3600}h' if secs >= 86400 else
                             f'{secs // 3600}h {secs % 3600 // 60}m' if secs >= 3600 else
                             f'{secs // 60}m {secs % 60}s')
        else:
            out['uptime'] = f"exited ({st.get('ExitCode')})"
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError):
        pass
    try:
        s = json.loads(docker('stats', '--no-stream', '--format', '{{json .}}', name, timeout=8).stdout)
        out.update(cpu=float(s['CPUPerc'].rstrip('%')), mem=s['MemUsage'].split(' / ')[0],
                   mem_percent=float(s['MemPerc'].rstrip('%')))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass
    try:
        r = docker('logs', '--tail', '12', name)
        lines = []
        for raw in (r.stdout + r.stderr).splitlines()[-12:]:
            lines += textwrap.wrap(clean_log(raw), 58) or ['']
        out['logs'] = lines[-24:]
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def docker_restart(name):
    try:
        r = docker('restart', '-t', '10', name, timeout=40)
        return (True, f'Restarted {name}') if r.returncode == 0 else (False, f'Restart failed: {r.stderr.strip()[:80]}')
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f'Restart failed: {str(exc)[:80]}'


def api_action(act, arg):
    """Process kill and docker pause/unpause/stop/start go through the dashboard (user philipp), not this root service."""
    if act in ('term', 'kill'):
        path, body = '/api/process/kill', {'pid': arg[0], 'start': arg[1], 'signal': 'TERM' if act == 'term' else 'KILL'}
    else:
        path, body = f'/api/docker/{act}', {'name': arg}
    req = urllib.request.Request(DASH_URL + path, json.dumps(body).encode(),
                                 {'Content-Type': 'application/json', 'Origin': DASH_URL})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = json.load(r)
            return body.get('ok', False), str(body.get('message', 'done'))[:80]
    except urllib.error.HTTPError as exc:
        try:
            return False, json.load(exc).get('message', 'failed')[:80]
        except ValueError:
            return False, f'HTTP {exc.code}'
    except (OSError, ValueError) as exc:
        return False, str(exc)[:80]


def selftest():
    t = Touch(719, 1559)

    def run(events, now0=0.0, step=0.01):
        out = []
        for i, ev in enumerate(events):
            out += t.feed(*ev, now0 + i * step)
        return out
    down = lambda x, y: [(3, 47, 0), (3, 57, 1), (3, 53, x), (3, 54, y), (0, 0, 0)]
    up = [(3, 57, -1), (0, 0, 0)]
    assert run(down(360, 780) + up) == [('tap', 540.0, 780 * H / 1560)]
    assert run(down(600, 700) + [(3, 53, 300), (0, 0, 0)] + up) == [('swipe', 'left')]
    assert run(down(100, 700) + [(3, 53, 130), (0, 0, 0)] + up) == []          # short horizontal: no swipe
    g = run(down(360, 1000) + [(3, 54, 800), (0, 0, 0)] + up)
    assert g[0] == ('scrollstart',) and g[1][0] == 'scroll' and g[1][1] < 0 and g[-1] == ('scrollend',)
    run(down(360, 500))
    assert t.tick(0.5) == [] and t.tick(1.0) == [('long', 540.0, 500 * H / 1560)]
    assert run(up) == []                                                         # no tap after long
    ui, data = Ui(), {'containers': {'containers': [{'name': f'c{i}'} for i in range(20)]}}
    ui.page = 1
    ui.on_gesture(('scrollstart',), data, 0)
    ui.on_gesture(('scroll', -99999), data, 0)
    assert ui.scroll == max_scroll(data) == 20 * ROW_H - (LIST_BOTTOM - LIST_TOP)
    ui.on_gesture(('scroll', 99999), data, 0)
    assert ui.scroll == 0
    assert ui.on_gesture(('tap', 500, LIST_TOP + 5), data, 0) == [('detail', 'c0')] and ui.detail == 'c0'
    ui.on_gesture(('long', 500, 500), data, 1)
    assert ui.confirm[:2] == ('restart', 'c0')
    assert ui.on_gesture(('tap', 700, 1200), data, 2) == [('act', 'restart', 'c0')] and ui.confirm is None
    ui.info = {'state': 'running'}                       # detail buttons follow state; every action is confirmed
    ui.on_gesture(('tap', 700, 2200), data, 3)
    assert ui.confirm[:2] == ('stop', 'c0')
    ui.on_gesture(('tap', 300, 1200), data, 3)           # Cancel
    assert ui.confirm is None
    assert [a for a, _ in container_actions('paused')] == ['unpause'] and [a for a, _ in container_actions('exited')] == ['start']
    ui = Ui()
    pdata = {'processes': [{'pid': 7, 'start': 5, 'name': 'sleep', 'killable': True},
                           {'pid': 1, 'start': 1, 'name': 'init', 'killable': False}]}
    ui.page = 2
    ui.on_gesture(('tap', 500, LIST_TOP + 5), pdata, 0)
    assert ui.pdetail == (7, 5) and ui.proc_action(pdata, 0)[:2] == ('term', 'Kill (SIGTERM)')
    ui.on_gesture(('tap', 500, 2200), pdata, 1)
    assert ui.confirm[:2] == ('term', (7, 5))
    assert ui.on_gesture(('tap', 760, 1200), pdata, 2) == [('act', 'term', (7, 5))]
    assert ui.proc_action(pdata, 3)[0] == 'term'          # SIGKILL only after the grace period
    ui.termed[(7, 5)] = 3
    assert ui.proc_action(pdata, 3 + GRACE_S)[0] == 'kill'
    ui.pdetail = (1, 1)
    ui.on_gesture(('tap', 500, 2200), pdata, 9)
    assert ui.confirm is None                            # root process: button disabled
    ui = Ui()
    ui.on_gesture(('tap', (len(TABS) - 1) * TAB_W + 5, TAB_Y + 20), data, 0)
    assert ui.page == len(TABS) - 1
    ui.on_gesture(('tap', 275, 1720), data, 0)
    assert ui.confirm[0] == 'reboot'
    ui.on_gesture(('tap', 300, 1200), data, 1)           # Cancel
    ui.on_gesture(('tap', 805, 1720), data, 2)
    assert ui.on_gesture(('tap', 760, 1200), data, 3) == [('act', 'poweroff', None)]
    ui.render(data, False, 4)
    nets = parse_wifi('*:80:WPA2:home\n:40:WPA2:home\n:90:--:cafe\n:70:WPA1 WPA2:a:b\n:10::', {'a:b'})
    assert [n['ssid'] for n in nets] == ['home', 'a:b', 'cafe'] and nets[0]['signal'] == 80 and not nets[2]['secure']
    ui = Ui()
    ui.page = 3
    assert ui.on_gesture(('tap', 500, 400), data, 0) == [('wifi', wifi_info)] and ui.sub['kind'] == 'wifi'
    ui.render(data, False, 0)
    info = nm_fields('GENERAL.CONNECTION:home\nIP4.ADDRESS[1]:10.0.0.50/24\nIP4.GATEWAY:10.0.0.1\nIP4.DNS[1]:1.1.1.1\n'
                     'IP4.DNS[2]:9.9.9.9\nipv4.method:manual\nipv4.addresses:10.0.0.50/24\nipv4.dns:\n')
    assert info['IP4.DNS'] == ['1.1.1.1', '9.9.9.9'] and 'ipv4.dns' not in info
    ui.sub['info'] = {**info, 'con': 'home', 'link': ['5180 MHz', '36', '1170 Mbit/s', 'WPA2']}
    ui.render(data, False, 0)
    ui.on_gesture(('tap', 500, 1130), data, 0)                  # IP & DNS settings
    assert ui.sub['kind'] == 'ipcfg' and ui.sub['addr'] == '10.0.0.50/24' and ui.sub['field'] == 'addr'
    ui.render(data, False, 0)
    ui.on_gesture(('tap', 500, IP_BOXES[2][1][1] + 20), data, 0)  # DNS field
    for k in '1.1.1.1':
        ui.on_gesture(('tap', *next(((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) for kk, b in kb_keys('sym') if kk == k)), data, 0)
    assert ui.on_gesture(('tap', 500, 1400), data, 0) == [('wifi', ip_apply, 'home', 'manual', '10.0.0.50/24', '10.0.0.1',
                                                          '1.1.1.1')]
    assert ip_args('manual', '10.0.0.50', '10.0.0.1', '1.1.1.1, 9.9.9.9') == [
        'ipv4.method', 'manual', 'ipv4.addresses', '10.0.0.50/24', 'ipv4.gateway', '10.0.0.1',
        'ipv4.dns', '1.1.1.1,9.9.9.9', 'ipv4.ignore-auto-dns', 'yes']
    assert ip_args('auto', 'x', 'y', '')[:2] == ['ipv4.method', 'auto'] and ip_args('auto', '', '', '')[-1] == 'no'
    for bad in (('manual', '10.0.0.300/24', '', ''), ('manual', '10.0.0.9/24', '10.0.1.1', ''), ('auto', '', '', '1.1.1')):
        try:
            ip_args(*bad)
            assert False, bad
        except ValueError:
            pass
    ui.on_gesture(('tap', 100, 80), data, 0)                    # back to details
    assert ui.sub['kind'] == 'wifi'
    assert ui.on_gesture(('tap', 500, 1400), data, 0) == [('wifi', wifi_scan)] and ui.wifi['nets'] is None
    ui.render(data, False, 0)
    ui.wifi['nets'] = nets
    assert ui.on_gesture(('tap', 500, WIFI_TOP + WIFI_ROW + 5), data, 0) == [('wifi', wifi_connect, 'a:b', None, True)]
    ui.on_gesture(('tap', 500, WIFI_TOP + 5), data, 0)          # secured, not saved: password page
    assert ui.wifi['sel']['ssid'] == 'home'
    tap = lambda key, mode='abc': ui.on_gesture(('tap', *next(((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
                                                              for k, b in kb_keys(mode) if k == key)), data, 1)
    for k in 'pass':
        tap(k)
    tap('shift'); tap('W', 'ABC'); tap('mode'); tap('!', 'sym'); tap('1', 'sym'); tap('space', 'sym'); tap('back', 'sym')
    assert ui.wifi['pw'] == 'passW!1' and ui.wifi['mode'] == 'sym'
    assert ui.on_gesture(('tap', 500, 1400), data, 2) == []             # < 8 chars
    tap('2', 'sym')
    ui.render(data, False, 2)
    assert ui.on_gesture(('tap', 500, 1400), data, 2) == [('wifi', wifi_connect, 'home', 'passW!12', False)]
    ui.on_gesture(('tap', 100, 80), data, 3); ui.on_gesture(('tap', 100, 80), data, 3)
    assert ui.wifi is None
    assert clean_log('\x1b[31mapi_key=abc123 ok\x1b[0m') == 'api_key=*** ok'
    ndata = {'services': {'ssh': {'up': True}, 'tailscale': {'up': True}, 'bluetooth': {'up': True}}}
    ui = Ui()
    ui.page = 3
    ui.render(ndata, False, 0)
    ui.on_gesture(('tap', 500, NET_TOP + NET_ROW + 5), ndata, 0)            # Tailscale up: confirm first
    assert ui.confirm[0] == 'ts_off'
    assert ui.on_gesture(('tap', 760, 1200), ndata, 1) == [('act', 'ts_off', None)]
    assert ui.on_gesture(('tap', 500, NET_TOP + NET_ROW + 5), {}, 2) == [('wifi', svc, 'tailscaled', True)]
    ui.on_gesture(('tap', 500, NET_TOP + 5), ndata, 3)
    assert ui.sub['kind'] == 'ssh'
    ui.render(ndata, False, 3)
    ui.on_gesture(('tap', 800, 240), ndata, 3)
    assert ui.confirm[0] == 'ssh_off'
    ui.on_gesture(('tap', 300, 1200), ndata, 3)                              # Cancel
    tap = lambda key: ui.on_gesture(('tap', *next(((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
                                                  for k, b in kb_keys('abc') if k == key)), ndata, 4)
    for k in 'secret':
        tap(k)
    user = ui.sub['user']
    assert ui.on_gesture(('tap', 500, 1400), ndata, 5) == [('wifi', set_password, user, 'secret')] and ui.sub['pw'] == ''
    ui.on_gesture(('tap', 100, 80), ndata, 6)
    assert ui.sub is None
    ui.on_gesture(('tap', 500, NET_TOP + 2 * NET_ROW + 5), ndata, 6)
    assert ui.sub == {'kind': 'usb'}
    ui.render(ndata, False, 6)
    ui.sub = None
    assert ui.on_gesture(('tap', 500, NET_TOP + 3 * NET_ROW + 5), ndata, 7) == [('wifi', bt_scan)]
    devs = parse_bt('Device AA:BB:CC:DD:EE:01 Speaker\nDevice 11:22:33:44:55:66 11-22-33-44-55-66\n'
                    'Device AA:BB:CC:DD:EE:02 Keyboard', 'Device AA:BB:CC:DD:EE:02 Keyboard', '')
    assert [d['name'] for d in devs] == ['Keyboard', 'Speaker', '11-22-33-44-55-66'] and not devs[2]['named']
    ui.sub['devs'] = devs
    ui.render(ndata, False, 7)
    assert ui.on_gesture(('tap', 500, BT_TOP + 5), ndata, 8) == [('wifi', bt_act, 'AA:BB:CC:DD:EE:02', 'Keyboard', 'connect')]
    assert ui.on_gesture(('tap', 500, BT_TOP + WIFI_ROW + 5), ndata, 8)[0][3:] == ('Speaker', 'pair')
    ui.sub['vis'] = False
    assert ui.on_gesture(('tap', 200, 370), ndata, 8) == [('wifi', bt_vis, True)]
    ui.on_gesture(('tap', 800, 370), ndata, 8)
    assert ui.sub['edit']
    ui.sub['pw'] = 'mia3'
    ui.render(ndata, False, 8)
    assert ui.on_gesture(('tap', 500, 1400), ndata, 8) == [('wifi', bt_rename, 'mia3')] and not ui.sub['edit']
    ui.sub = None
    ui.on_gesture(('tap', 500, NET_TOP + 4 * NET_ROW + 5), ndata, 9)
    assert ui.sub == {'kind': 'ir', 'brand': 0}
    ui.render(ndata, False, 9)
    assert ui.on_gesture(('tap', 200, 400), ndata, 9) == [('wifi', ir_send, 'nec', 0x0408, 'LG Power')]
    ui.on_gesture(('tap', 500, 230), ndata, 9)
    assert ui.on_gesture(('tap', 650, 1050), ndata, 9) == [('wifi', ir_send, 'necx', 0x070710, 'Samsung Ch -')]
    ui.on_gesture(('tap', 500, 230), ndata, 9)
    assert ui.on_gesture(('tap', 200, 550), ndata, 9) == [('wifi', ir_send, 'sony15', 0x970023, 'Sony Back')]
    assert ui.on_gesture(('tap', 540, 720), ndata, 9) == [('wifi', ir_send, 'sony12', 0x010065, 'Sony OK')]
    ui.on_gesture(('tap', 500, 230), ndata, 9)
    ui.render(ndata, False, 9)
    assert ui.on_gesture(('tap', 900, 380), ndata, 9) == []               # Philips has no Home code
    assert ir_send.__code__.co_consts  # sony gap is in ir_send
    assert parse_saved('home:802-11-wireless:wlan0\ncafe\\:5G:802-11-wireless:\nlo:loopback:lo\n') == [
        {'name': 'home', 'active': True}, {'name': 'cafe:5G', 'active': False}]
    ui = Ui()
    ui.sub = {'kind': 'wifi', 'info': None}
    assert ui.on_gesture(('tap', 500, 1270), data, 0) == [('wifi', wifi_saved)] and ui.sub == {'kind': 'saved', 'devs': None}
    ui.sub['devs'] = [{'name': 'home', 'active': True}, {'name': 'cafe', 'active': False}]
    ui.render(data, False, 0)
    ui.on_gesture(('tap', 500, WIFI_TOP + WIFI_ROW + 5), data, 0)
    assert ui.confirm[:3] == ('forget_wifi', (wifi_forget, 'cafe'), 'cafe')
    ui.render(data, False, 0)
    assert ui.on_gesture(('tap', 760, 1200), data, 0) == [('wifi', wifi_forget, 'cafe')]
    assert ui.on_gesture(('tap', 100, 80), data, 0) == [('wifi', wifi_info)] and ui.sub['kind'] == 'wifi'
    ui.sub = {'kind': 'bt', 'devs': [{'addr': 'AA', 'name': 'PC', 'paired': True, 'connected': False, 'named': True}],
              'edit': False}
    ui.on_gesture(('long', 500, BT_TOP + 5), data, 0)
    assert ui.confirm[:2] == ('forget_bt', (bt_forget, 'AA', 'PC'))
    assert ui.on_gesture(('tap', 760, 1200), data, 0) == [('wifi', bt_forget, 'AA', 'PC')]
    ui.pair = {'name': 'PC', 'kind': 'confirm', 'code': '123456'}
    ui.render(data, False, 0)
    assert ui.on_gesture(('tap', 500, 400), data, 0) == [] and ui.pair
    assert ui.on_gesture(('tap', 760, 1200), data, 0) == [('pair', True)] and ui.pair is None
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.mkdir(f'{tmp}/sub')
        Path(f'{tmp}/a.txt').write_text('hello\tworld\n' * 100)
        Path(f'{tmp}/b.bin').write_bytes(b'\0\1')
        Image.new('RGB', (40, 20), 'red').save(f'{tmp}/c.png')
        assert [e['name'] for e in list_dir(tmp)] == ['sub', 'a.txt', 'b.bin', 'c.png']
        assert file_view(f'{tmp}/a.txt')['lines'][0] == 'hello   world' and 'Binary' in file_view(f'{tmp}/b.bin')['lines'][0]
        assert file_view(f'{tmp}/c.png')['img'].size == (40, 20) and 'No preview' in file_view('/proc/kmsg')['lines'][0]
        ui = Ui()
        ui.on_gesture(('tap', FILES_PAGE * TAB_W + 5, TAB_Y + 20), data, 0)
        assert ui.page == FILES_PAGE and ui.sub is None
        ui.render(data, False, 0)
        f = ui.files
        assert f['path'] in (HOME_DIR, '/')
        ui.sub = f
        ui.open_dir(tmp)
        ui.sub = None
        ui.render(data, False, 0)
        ui.on_gesture(('tap', 500, FILE_TOP + 5), data, 0)               # sub/
        assert f['path'] == f'{tmp}/sub' and ui.sub is None
        ui.on_gesture(('tap', 100, 80), data, 0)                          # back up
        assert f['path'] == tmp
        ui.on_gesture(('tap', 500, FILE_TOP + FILE_ROW + 5), data, 0)    # a.txt
        assert f['view']['lines'][0].startswith('hello')
        ui.on_gesture(('scrollstart',), data, 0)
        ui.on_gesture(('scroll', -300), data, 0)
        assert f['scroll'] == 300
        ui.render(data, False, 0)
        ui.on_gesture(('tap', 100, 80), data, 0)
        assert f['view'] is None
        ui.on_gesture(('tap', 500, FILE_TOP + 3 * FILE_ROW + 5), data, 0)
        ui.render(data, False, 0)
        ui.on_gesture(('tap', 100, 80), data, 0)
        global WRITABLE
        WRITABLE = (tmp,)
        ui.on_gesture(('tap', 770, 80), data, 0)                          # new folder
        assert f['edit']
        ui.render(data, False, 0)
        f['pw'] = 'new'
        ui.on_gesture(('tap', 500, 1400), data, 0)
        assert os.path.isdir(f'{tmp}/new') and not f['edit']
        ui.on_gesture(('tap', 960, 80), data, 0)                          # delete mode
        assert f['dele']
        ui.render(data, False, 0)
        ui.on_gesture(('tap', 500, FILE_TOP + 5), data, 0)                # 'new' sorts first
        assert ui.confirm[:2] == ('delete_dir', f'{tmp}/new')
        ui.render(data, False, 0)
        ui.on_gesture(('tap', 760, 1200), data, 0)
        assert not os.path.exists(f'{tmp}/new') and ui.confirm is None
        ui.on_gesture(('long', 500, FILE_TOP + FILE_ROW + 5), data, 0)    # long-press a.txt
        assert ui.confirm[:2] == ('delete', f'{tmp}/a.txt')
        ui.on_gesture(('tap', 300, 1200), data, 0)
        assert os.path.exists(f'{tmp}/a.txt')
        WRITABLE = ('/home/philipp', '/mnt/sd')
        assert not writable('/etc') and writable('/home/philipp') and not writable('/home/philipp', strict=True)
        ui.on_gesture(('swipe', 'left'), data, 0)
        assert ui.page == FILES_PAGE + 1
    ui = Ui()
    ui.render(data, False, 0)
    ui.on_gesture(('tap', 500, 1700), data, 0)
    assert ui.page == 1
    assert fmt_size(5) == '5 B' and fmt_size(2048) == '2.0 KiB' and fmt_size(3 << 30) == '3.0 GiB'
    print('selftest ok')


def read_proc(pid, proc=Path('/proc')):
    try:
        text = (proc / str(pid) / 'stat').read_text()
        left = text.find('(')
        right = text.rfind(')')
        if left < 0 or right <= left:
            return None
        comm = text[left + 1:right]
        fields = text[right + 2:].split()
        ticks = int(fields[11]) + int(fields[12])
        starttime = int(fields[19])
        uid_line = next(line for line in (proc / str(pid) / 'status').read_text().splitlines()
                        if line.startswith('Uid:'))
        uid = int(uid_line.split()[1])
        return uid, comm, ticks, starttime
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def proc_snapshot(proc=Path('/proc')):
    snapshot = {}
    for entry in proc.iterdir():
        if entry.name.isdigit():
            item = read_proc(entry.name, proc)
            if item is not None:
                snapshot[int(entry.name)] = item
    return snapshot


def pick_target(before, after, elapsed, uid, clk_tck, self_pid):
    if elapsed <= 0 or clk_tck <= 0:
        return None
    candidates = []
    for pid, (owner, comm, ticks, starttime) in after.items():
        prior = before.get(pid)
        if (prior is None or prior[3] != starttime or owner != uid or
                pid == self_pid or comm in PROTECTED_COMMS):
            continue
        pct = (ticks - prior[2]) / clk_tck / elapsed * 100
        candidates.append((pct, pid, comm))
    if not candidates:
        return None
    pct, pid, comm = max(candidates)
    return pid, comm, pct


def still_burning(before, after, elapsed, clk_tck):
    if after is None or after[3] != before[3] or elapsed <= 0 or clk_tck <= 0:
        return False
    pct = (after[2] - before[2]) / clk_tck / elapsed * 100
    return pct >= STILL_BURNING_PCT


def key_held(fd):
    buf = bytearray(96)
    try:
        fcntl.ioctl(fd, EVIOCGKEY_96, buf, True)
    except OSError:
        logging.exception('Power-key state query failed')
        return False
    return bool(buf[116 // 8] & (1 << (116 % 8)))


def render_notice(lines):
    image = Image.new('RGB', (1080, 1920), BG)
    draw = ImageDraw.Draw(image)
    title_font = font(52, True)
    draw.text((60, 300), 'Mi A3 kill switch', fill=AMBER, font=title_font)
    line_font = font(40)
    for index, line in enumerate(lines):
        line = str(line)
        while len(line) > 1 and draw.textlength(line, font=line_font) > 960:
            line = line[:-2] + '…'
        draw.text((60, 420 + index * 80), line, fill=TEXT, font=line_font)
    return image


class DRMResources(ctypes.Structure):
    _fields_ = [('count_fbs',ctypes.c_int),('fbs',ctypes.POINTER(ctypes.c_uint32)),
                ('count_crtcs',ctypes.c_int),('crtcs',ctypes.POINTER(ctypes.c_uint32)),
                ('count_connectors',ctypes.c_int),('connectors',ctypes.POINTER(ctypes.c_uint32)),
                ('count_encoders',ctypes.c_int),('encoders',ctypes.POINTER(ctypes.c_uint32)),
                ('min_width',ctypes.c_uint32),('max_width',ctypes.c_uint32),
                ('min_height',ctypes.c_uint32),('max_height',ctypes.c_uint32)]


class DRMHead(ctypes.Structure):
    # Only the initial fields are read; the remainder is owned by libdrm.
    _fields_ = [('crtc_id',ctypes.c_uint32),('buffer_id',ctypes.c_uint32),
                ('x',ctypes.c_uint32),('y',ctypes.c_uint32),
                ('width',ctypes.c_uint32),('height',ctypes.c_uint32)]


class DRMProperties(ctypes.Structure):
    _fields_ = [('count',ctypes.c_uint32),('ids',ctypes.POINTER(ctypes.c_uint32)),
                ('values',ctypes.POINTER(ctypes.c_uint64))]


class DRMProperty(ctypes.Structure):
    _fields_ = [('id',ctypes.c_uint32),('flags',ctypes.c_uint32),('name',ctypes.c_char*32)]


def disable_console_blanking():
    interval_path = Path('/sys/module/kernel/parameters/consoleblank')
    interval = int(interval_path.read_text())
    if interval:
        console = os.open('/dev/tty1', os.O_WRONLY | os.O_NOCTTY)
        try:
            os.write(console, b'\x1b[9;0]')
        finally:
            os.close(console)
        interval = int(interval_path.read_text())
    if interval:
        raise RuntimeError(f'Console blanking remains enabled: {interval}s')
    logging.info('Console blanking disabled: verified interval 0s')


class Display:
    def __init__(self):
        self.fd = os.open('/dev/fb0', os.O_RDWR)
        var = bytearray(160)
        fcntl.ioctl(self.fd, 0x4600, var, True)  # FBIOGET_VSCREENINFO
        width,height,_,_,self.xoffset,self.yoffset,bpp = struct.unpack_from('7I',var)
        channels = struct.unpack_from('9I',var,32)
        self.width, self.height = width, height
        if (bpp,channels) != (32,(16,8,0,8,8,0,0,8,0)):
            raise RuntimeError('Unsupported framebuffer layout; refusing to write')
        self.stride = int(Path('/sys/class/graphics/fb0/stride').read_text())
        self.memory = mmap.mmap(self.fd, self.stride * (height+self.yoffset), mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        self.previous_tty = int(Path('/sys/class/tty/tty0/active').read_text().strip().removeprefix('tty'))
        self.tty = os.open('/dev/tty8',os.O_RDWR | os.O_NOCTTY)
        self.backlight = Path('/sys/class/backlight/5e94000.dsi.0')
        self.brightness = (self.backlight/'brightness').read_text().strip()
        self.power = (self.backlight/'bl_power').read_text().strip()
        self.drm_fd = os.open('/dev/dri/card1',os.O_RDWR)
        self.drm = ctypes.CDLL('libdrm.so.2',use_errno=True)
        self.drm.drmModeGetResources.restype = ctypes.POINTER(DRMResources)
        self.drm.drmModeGetCrtc.restype = ctypes.POINTER(DRMHead)
        self.drm.drmModeObjectGetProperties.restype = ctypes.POINTER(DRMProperties)
        self.drm.drmModeGetProperty.restype = ctypes.POINTER(DRMProperty)
        self.drm.drmModeConnectorSetProperty.argtypes = [ctypes.c_int,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_uint64]
        self.connector, self.dpms_property, self.initial_dpms = self.find_dpms()
        self.shown = False
        self.touched = False
        self.view = 'terminal'
        self.kb = None

    def keyboard(self, on):
        if on and self.kb is None:
            try:
                # delay: the console repaints over anything drawn right after the VT switch
                self.kb = subprocess.Popen(['sh', '-c', 'sleep 1; exec buffyboard'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                logging.exception('buffyboard failed to start')
        elif not on and self.kb is not None:
            self.kb.terminate()
            try:
                self.kb.wait(5)
            except subprocess.TimeoutExpired:
                self.kb.kill()
            self.kb = None

    def find_dpms(self):
        resources = self.drm.drmModeGetResources(self.drm_fd)
        if not resources:
            raise OSError(ctypes.get_errno(),'DRM resources unavailable')
        try:
            for cid in resources.contents.connectors[:resources.contents.count_connectors]:
                props = self.drm.drmModeObjectGetProperties(self.drm_fd,cid,ctypes.c_uint32(0xc0c0c0c0))
                if not props:
                    continue
                try:
                    for i,pid in enumerate(props.contents.ids[:props.contents.count]):
                        prop = self.drm.drmModeGetProperty(self.drm_fd,pid)
                        if not prop:
                            continue
                        try:
                            if prop.contents.name == b'DPMS':
                                return cid,pid,props.contents.values[i]
                        finally:
                            self.drm.drmModeFreeProperty(prop)
                finally:
                    self.drm.drmModeFreeObjectProperties(props)
            raise RuntimeError('No connector DPMS control')
        finally:
            self.drm.drmModeFreeResources(resources)

    def set_power(self, value):
        if self.drm.drmSetMaster(self.drm_fd):
            raise OSError(ctypes.get_errno(),'DRM master unavailable')
        try:
            if self.drm.drmModeConnectorSetProperty(self.drm_fd,self.connector,self.dpms_property,value):
                raise OSError(ctypes.get_errno(),'DRM panel power failed')
        finally:
            self.drm.drmDropMaster(self.drm_fd)

    def cycle_power(self):
        # Keep DRM ownership for the complete off/on transaction, as in
        # the standalone recovery that the owner confirmed visually.
        if self.drm.drmSetMaster(self.drm_fd):
            raise OSError(ctypes.get_errno(),'DRM master unavailable for wake')
        try:
            for value in (3,0):
                if self.drm.drmModeConnectorSetProperty(self.drm_fd,self.connector,self.dpms_property,value):
                    raise OSError(ctypes.get_errno(),'DRM wake cycle failed')
                if value == 3:
                    time.sleep(0.15)
        finally:
            self.drm.drmDropMaster(self.drm_fd)

    def refresh_panel(self):
        if self.drm.drmSetMaster(self.drm_fd):
            raise OSError(ctypes.get_errno(),'DRM master unavailable for refresh')
        resources = self.drm.drmModeGetResources(self.drm_fd)
        if not resources:
            self.drm.drmDropMaster(self.drm_fd)
            raise OSError(ctypes.get_errno(),'DRM resources unavailable')
        try:
            refreshed = False
            for cid in resources.contents.crtcs[:resources.contents.count_crtcs]:
                head = self.drm.drmModeGetCrtc(self.drm_fd,cid)
                if not head:
                    continue
                try:
                    if head.contents.buffer_id and (head.contents.width,head.contents.height)==(self.width,self.height):
                        rc = self.drm.drmModeDirtyFB(self.drm_fd,head.contents.buffer_id,None,0)
                        if rc:
                            raise OSError(ctypes.get_errno(),'DRM framebuffer refresh failed')
                        refreshed = True
                finally:
                    self.drm.drmModeFreeCrtc(head)
            if not refreshed:
                raise RuntimeError('No active phone scanout to refresh')
        finally:
            self.drm.drmModeFreeResources(resources)
            self.drm.drmDropMaster(self.drm_fd)

    def draw(self,image):
        # Frames are laid out for 1080x1920; scale to panel width, pad below.
        if image.size == (self.width,self.height):
            canvas = image
        else:
            canvas = Image.new('RGB',(self.width,self.height),image.getpixel((0,0)))
            canvas.paste(image.resize((self.width,self.width*image.height//image.width),Image.BILINEAR))
        raw = canvas.tobytes('raw','BGRX')
        rowbytes = self.width*4
        for row in range(self.height):
            offset=(row+self.yoffset)*self.stride+self.xoffset*4
            self.memory[offset:offset+rowbytes]=raw[row*rowbytes:(row+1)*rowbytes]
        self.refresh_panel()

    def activate_graphics(self):
        # Preserve the readable panel initialized at boot. A recovery DPMS
        # cycle here can turn a working console into an unwakeable panel.
        if not self.touched or self.view == 'terminal':
            self.touched = True
            fcntl.ioctl(self.tty,0x4B3A,0)  # KD_TEXT while switching VTs
            fcntl.ioctl(self.tty,0x5606,8)
            fcntl.ioctl(self.tty,0x5607,8)
            fcntl.ioctl(self.tty,0x4B3A,1)  # KD_GRAPHICS after activation

    def show(self,image):
        self.keyboard(False)
        self.activate_graphics()
        self.draw(image)
        self.shown = True
        self.view = 'dashboard'
        logging.info('Dashboard frame submitted; panel power preserved')

    def terminal(self):
        # Release graphics mode so the kernel can switch to the existing
        # login console and repaint its stored text. No shell/session reset.
        disable_console_blanking()
        fcntl.ioctl(self.tty,0x4B3A,0)  # KD_TEXT
        fcntl.ioctl(self.tty,0x5606,1)  # VT_ACTIVATE tty1
        fcntl.ioctl(self.tty,0x5607,1)  # VT_WAITACTIVE
        self.shown = False
        self.view = 'terminal'
        self.keyboard(True)

    def hide(self):
        # Black pixels on the AMOLED panel, without its unreliable power cycle.
        self.keyboard(False)
        self.activate_graphics()
        self.draw(Image.new('RGB',(1080,1920),'black'))
        self.shown = False
        self.view = 'blank'

    def cycle(self,image):
        if not self.touched or self.view == 'blank':
            self.show(image)
        elif self.view == 'dashboard':
            self.terminal()
        else:
            self.hide()
        logging.info('Display view: %s',self.view)

    def close(self):
        self.keyboard(False)
        if self.touched:
            try:
                fcntl.ioctl(self.tty,0x4B3A,0)  # KD_TEXT
                fcntl.ioctl(self.tty,0x5606,self.previous_tty)
            except (OSError, RuntimeError):
                logging.exception('Console restore failed')
        os.close(self.tty)
        os.close(self.drm_fd)
        self.memory.close()
        os.close(self.fd)


def fetch():
    with urllib.request.urlopen('http://127.0.0.1:8090/api/status',timeout=4) as response:
        return json.load(response)


def main():
    try:
        Path("/proc/self/comm").write_text("mia3-display")
    except OSError:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview',type=Path)
    parser.add_argument('--timeout',type=float,default=0,
                        help='Seconds before hiding; 0 disables automatic hiding (default)')
    parser.add_argument('--exclusive-power-key',action='store_true',
                        help='Grab power input so other Linux input listeners cannot receive it')
    parser.add_argument('--touch-name',default='generic ft5x06 (48)',help='evdev name of the touchscreen')
    parser.add_argument('--page',type=int,default=0,help='with --preview: touch UI page (0-3)')
    parser.add_argument('--selftest',action='store_true')
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return
    if args.timeout < 0:
        parser.error('--timeout must be zero or positive')
    if args.preview:
        ui = Ui()
        ui.page = args.page
        ui.render(fetch(), False, 0).save(args.preview)
        return
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    disable_console_blanking()
    display = Display()
    devices = [p for p in Path('/sys/class/input').glob('event*') if (p/'device/name').read_text().strip()=='pm8941_pwrkey']
    if len(devices)!=1:
        display.close()
        raise RuntimeError('Expected one pm8941 power-key device')
    event_fd = os.open('/dev/input/'+devices[0].name,os.O_RDONLY | os.O_NONBLOCK)
    if args.exclusive_power_key:
        try:
            fcntl.ioctl(event_fd,0x40044590,1)  # EVIOCGRAB; released on fd close
        except OSError:
            os.close(event_fd)
            display.close()
            raise
        logging.info('Exclusive power-key input acquired')
    touch_fd = touch = None
    for p in Path('/sys/class/input').glob('event*'):
        if (p/'device/name').read_text().strip() == args.touch_name:
            touch_fd = os.open('/dev/input/'+p.name, os.O_RDONLY | os.O_NONBLOCK)
            xmax = struct.unpack('6i', fcntl.ioctl(touch_fd, 0x80184540+53, bytes(24)))[2]  # EVIOCGABS(MT_X)
            ymax = struct.unpack('6i', fcntl.ioctl(touch_fd, 0x80184540+54, bytes(24)))[2]
            touch = Touch(xmax, ymax)
            logging.info('Touchscreen %s opened: /dev/input/%s (%dx%d)', args.touch_name, p.name, xmax+1, ymax+1)
    if touch is None:
        logging.warning('Touchscreen %r not found; touch input disabled', args.touch_name)
    event = struct.Struct('@llHHi')
    stopped = False
    toggle = False
    action_requested = False
    action = None
    notice_until = 0.0
    pressed_at = None
    hold_fired = False
    last_release = -1.0
    try:
        user_uid = pwd.getpwnam('philipp').pw_uid
    except KeyError:
        user_uid = None
    clk = os.sysconf('SC_CLK_TCK')

    def stop(*_):
        nonlocal stopped
        stopped = True

    def request_toggle(*_):
        nonlocal toggle
        toggle = True

    def request_action(*_):
        nonlocal action_requested
        action_requested = True

    def notice(lines, until):
        nonlocal notice_until
        display.show(render_notice(lines))
        notice_until = until
        logging.info('Notice: %s', ' / '.join(lines))

    def start_action(now):
        # Hold = clean shutdown; the PMIC's own ~12 s hold reset is a hard hot-restart.
        nonlocal action
        if action is not None:
            return
        action = {'stage': 'poweroff', 'deadline': float('inf')}
        notice(['Powering off…', "Screen goes dark when done"], float('inf'))
        subprocess.Popen(['systemctl', 'poweroff'])

    def sysrq(command):
        try:
            Path('/proc/sysrq-trigger').write_text(command)
        except OSError:
            logging.exception('Kill switch sysrq %s failed', command)

    def finish_action(lines, now):
        nonlocal action
        notice(lines, now + NOTICE_S)
        action = None

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGUSR1, request_toggle)
    signal.signal(signal.SIGUSR2, request_action)
    latest = {}
    ui = Ui()
    agent = PairAgent() if MessageBus else None
    shown_req = None
    is_stale = True
    ui_dirty = False
    last_draw = 0.0
    detail_future = restart_future = pending = wifi_future = None
    next_detail = 0.0
    dpool = ThreadPoolExecutor(max_workers=2)

    def compose():
        return ui.render(latest, is_stale, time.monotonic())
    deadline = 0
    next_refresh = 0
    future = None
    changed = True
    pool = ThreadPoolExecutor(max_workers=1)
    notify = os.environ.get('NOTIFY_SOCKET')
    if notify:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect('\0' + notify[1:] if notify.startswith('@') else notify)
            sock.sendall(b'READY=1')
    logging.info('Ready: short power presses cycle views; hold ignored (Power tab reboots)')
    try:
        while not stopped:
            ready = select.select([event_fd] + ([touch_fd] if touch else []), [], [], 0.03 if ui_dirty else 0.1)[0]
            if agent and agent.req is not shown_req:                 # pairing request: pop up, wake the screen
                shown_req = ui.pair = agent.req
                ui_dirty = True
                if ui.pair and display.view != 'dashboard' and action is None:
                    display.show(compose())
            if touch_fd in ready:
                try:
                    raw = os.read(touch_fd, event.size * 64)
                except BlockingIOError:
                    raw = b''
                gestures = []
                for offset in range(0, len(raw) - event.size + 1, event.size):
                    _, _, kind, code, value = event.unpack_from(raw, offset)
                    gestures += touch.feed(kind, code, value, time.monotonic())
                if display.view == 'blank':
                    if any(g[0] == 'tap' for g in gestures):
                        display.show(compose())
                        ui_dirty = False
                elif display.shown and action is None and time.monotonic() >= notice_until:
                    for g in gestures:
                        for effect in ui.on_gesture(g, latest, time.monotonic()):
                            if effect[0] == 'blank':
                                display.hide()
                            elif effect[0] == 'detail':
                                next_detail = 0.0
                            elif effect[0] == 'pair' and agent:
                                agent.answer(effect[1])
                            elif effect[0] == 'wifi' and wifi_future is None:
                                logging.info('Touch: job %s', effect[1].__name__)    # never log args (passwords)
                                wifi_future = dpool.submit(*effect[1:])
                            elif effect[0] == 'act' and restart_future is None:
                                act, arg = effect[1:]
                                if act in ('ssh_off', 'ts_off'):
                                    if wifi_future is None:
                                        wifi_future = dpool.submit(svc, 'sshd' if act == 'ssh_off' else 'tailscaled', False)
                                        ui.toast = ('Turning off…', float('inf'))
                                    continue
                                if act in ('reboot', 'poweroff'):
                                    notice([f'{CONFIRM_TEXT[act][0]}…', 'Screen goes dark when done'], float('inf'))
                                    subprocess.Popen(['systemctl', act])
                                    continue
                                pending = effect[1:]
                                restart_future = dpool.submit(docker_restart, arg) if act == 'restart' \
                                    else dpool.submit(api_action, act, arg)
                                ui.toast = (f'{CONFIRM_TEXT[act][0]}…', float('inf'))
                                logging.info('Touch: %s %s', act, arg)
                        ui_dirty = True
            if event_fd in ready:
                raw = os.read(event_fd, event.size * 32)
                for offset in range(0, len(raw) - event.size + 1, event.size):
                    _, _, kind, code, value = event.unpack_from(raw, offset)
                    now = time.monotonic()
                    if kind != 1 or code != 116:
                        continue
                    if value == 1 and pressed_at is None:
                        pressed_at = now
                        hold_fired = False
                    elif value == 0:
                        if pressed_at is not None and not hold_fired and now - last_release > 0.3:
                            if action is not None:
                                logging.info('Press ignored during kill action')
                            else:
                                logging.info('Physical power-button press')
                                toggle = True
                                notice_until = 0
                        last_release = now
                        pressed_at = None

            now = time.monotonic()
            if touch and display.shown and action is None:
                for g in touch.tick(now):
                    for effect in ui.on_gesture(g, latest, now):
                        pass  # long-press only opens the confirm dialog
                    ui_dirty = True
            if ui.tick(now):
                ui_dirty = True
            if wifi_future is not None and wifi_future.done():
                try:
                    res = wifi_future.result()
                except Exception as exc:
                    logging.exception('Wi-Fi action failed')
                    res = (False, str(exc)[:80])
                refresh = False
                if isinstance(res, list):
                    if ui.wifi is not None:
                        ui.wifi['nets'] = res
                    elif ui.sub is not None:
                        ui.sub['devs'] = res
                elif isinstance(res, dict):
                    if ui.sub is not None and ui.sub['kind'] == 'wifi':
                        ui.sub['info'] = res
                else:
                    logging.info('Job result: %s', res[1])
                    ui.toast = (res[1], now + 6)
                    extra = res[2] if len(res) > 2 else None
                    if isinstance(extra, dict) and ui.sub is not None:     # ip_apply: back to details on success
                        ui.sub = {'kind': 'wifi', 'info': extra} if res[0] else {**ui.sub, 'info': extra}
                    elif extra is not None and ui.sub is not None:
                        ui.sub['devs'] = extra
                    if res[0]:
                        refresh = ui.wifi is not None and ui.sub is not None and ui.sub['kind'] == 'wifi'
                        ui.wifi = None
                wifi_future, ui_dirty = None, True
                if refresh:                                                # new network: reload the details page
                    ui.sub['info'] = None
                    wifi_future = dpool.submit(wifi_info)
            if restart_future is not None and restart_future.done():
                ok, msg = restart_future.result()
                logging.info('Touch action result: %s', msg)
                if ok and pending[0] == 'term':
                    ui.termed[pending[1]] = now
                ui.toast = (msg, now + 6)
                restart_future, next_detail, ui_dirty = None, 0.0, True
            if ui.detail and display.shown and detail_future is None and now >= next_detail:
                detail_future = dpool.submit(container_detail, ui.detail)
                next_detail = now + 5
            if detail_future is not None and detail_future.done():
                try:
                    info = detail_future.result()
                    if info.get('name') == ui.detail:
                        ui.info, ui_dirty = info, True
                except Exception:
                    logging.exception('Container detail failed')
                detail_future = None
            if pressed_at is not None and not hold_fired and now - pressed_at >= HOLD_ACTION_S:
                hold_fired = True
                logging.info('Power hold ignored')  # reboot/shut down live on the Power tab
                pressed_at = None
            action_requested = False

            if toggle:
                toggle = False
                if action is not None:
                    logging.info('Press ignored during kill action')
                else:
                    display.cycle(compose())
                    if display.shown:
                        deadline = now + args.timeout
                        next_refresh = 0

            if action is not None and now >= action['deadline']:
                stage = action['stage']
                if stage == 'sample':
                    after = proc_snapshot()
                    top = pick_target(
                        action['s0'], after, now - action['t0'],
                        user_uid, clk, os.getpid())
                    if top is None or top[2] < MIN_RUNAWAY_PCT:
                        detail = f'top: {top[1]} {top[2]:.0f}%' if top else 'top: none'
                        finish_action(['No runaway process', detail], now)
                    else:
                        pid, comm, pct = top
                        identity = read_proc(pid)
                        if identity is None or identity[0] != user_uid or identity[3] != after[pid][3]:
                            finish_action(['Runaway process ended', f'PID {pid}'], now)
                        else:
                            try:
                                os.kill(pid, signal.SIGKILL)
                                notice([f'Killing {comm}', f'PID {pid} · {pct:.0f}% CPU'], float('inf'))
                                action = {
                                    'stage': 'grace', 'deadline': now + KILL_GRACE_S,
                                    'pid': pid, 'comm': comm, 'start': identity[3],
                                }
                            except ProcessLookupError:
                                finish_action(['Process already exited', f'PID {pid}'], now)
                            except OSError:
                                logging.exception('Kill switch could not signal PID %d', pid)
                                finish_action(['Could not kill process', f'PID {pid}'], now)
                elif stage == 'grace':
                    base = read_proc(action['pid'])
                    if base is None or base[3] != action['start']:
                        finish_action([f"Killed {action['comm']}", f"PID {action['pid']}"], now)
                    else:
                        action.update(stage='recheck', base=base, t=now,
                                      deadline=now + SAMPLE_S)
                elif stage == 'recheck':
                    current = read_proc(action['pid'])
                    if still_burning(action['base'], current, now - action['t'], clk):
                        logging.error(
                            'Kill switch: PID %d still burning after SIGKILL; sysrq reboot',
                            action['pid'])
                        notice(['Process stuck in kernel', 'Rebooting: sync, remount ro, reboot'],
                               float('inf'))
                        sysrq('s')
                        action.update(stage='sysrq_u', deadline=now + SYSRQ_STEP_S)
                    else:
                        finish_action([f"Killed {action['comm']}", f"PID {action['pid']}"], now)
                elif stage == 'sysrq_u':
                    sysrq('u')
                    action.update(stage='sysrq_b', deadline=now + SYSRQ_STEP_S)
                elif stage == 'sysrq_b':
                    sysrq('b')

            if args.timeout > 0 and display.shown and now >= deadline:
                display.hide()
                logging.info('Display off: timeout')
            if future is not None and future.done():
                try:
                    new = future.result()
                    changed = {k: v for k, v in new.items() if k != 'collected_at'} != \
                        {k: v for k, v in latest.items() if k != 'collected_at'}
                    latest, is_stale = new, False
                except Exception:
                    logging.exception('Dashboard refresh failed; retaining last sample')
                    is_stale = changed = True
                future = None
                ui_dirty = ui_dirty or changed
            if ui_dirty and display.shown and now >= notice_until and now - last_draw >= 0.066:
                display.draw(compose())
                ui_dirty, last_draw = False, now
            if notice_until and action is None and now >= notice_until:
                notice_until = 0
                display.show(compose())
            if display.shown and future is None and now >= next_refresh:
                future = pool.submit(fetch)
                next_refresh = now + 5
    finally:
        pool.shutdown(wait=True,cancel_futures=True)
        dpool.shutdown(wait=False,cancel_futures=True)
        display.close()
        os.close(event_fd)


if __name__=='__main__':
    main()
