#!/usr/bin/env python3
"""Mi A3 display: short power presses cycle views; a 3-second hold powers off cleanly."""
import argparse
import ctypes
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import logging
import mmap
import os
import pwd
from pathlib import Path
import select
import signal
import socket
import re
import struct
import subprocess
import textwrap
import time
import urllib.error
import urllib.request
import datetime as dt

from PIL import Image, ImageDraw, ImageFont

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
R = 4 / 3  # px per layout unit
K = 1.2  # vertical stretch: lays the 540x960 design out on the taller 720x1560 panel
F = 1.1  # font scale

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


def font(size, bold=False, mono=False):
    key = (size, bold, mono)
    if key not in FONTS:
        path = '/usr/share/fonts/droid/DroidSansMono.ttf' if mono else FONT / ('OpenSans-Bold.ttf' if bold else 'OpenSans-Regular.ttf')
        FONTS[key] = ImageFont.truetype(str(path), size)
    return FONTS[key]


def fmt(value, digits=0, suffix=''):
    return '—' if value is None else f'{value:.{digits}f}{suffix}'


def gib(value):
    return '—' if value is None else f'{value / 2**30:.1f} GiB'


def render(data, stale=False):
    image = Image.new('RGB', IMG, BG)
    d = ImageDraw.Draw(image)

    def text(x, y, value, size=13, color=TEXT, bold=False, max_width=None):
        value = str(value if value is not None else '—')
        f = font(round(size * R * F), bold)
        if max_width:
            while len(value) > 1 and d.textlength(value, font=f) > max_width * R:
                value = value[:-2] + '…'
        d.text((x * R, y * R * K), value, fill=color, font=f)

    def card(x, y, w, h):
        d.rounded_rectangle((x*R, y*R*K, (x+w)*R, (y+h)*R*K), 16, fill=PANEL, outline=LINE, width=1)

    def bar(x, y, w, percent):
        d.rounded_rectangle((x*R, y*R*K, (x+w)*R, (y+5)*R*K), 4, fill=LINE)
        if percent is not None and percent > 0:
            d.rounded_rectangle((x*R, y*R*K, (x+w*min(100,max(0,percent))/100)*R, (y+5)*R*K), 4, fill=CYAN)

    text(20, 15, 'Mi A3 dashboard', 24, bold=True)
    stamp = data.get('collected_at')
    try:
        stamp = dt.datetime.fromisoformat(stamp).astimezone().strftime('%H:%M:%S')
    except (TypeError, ValueError):
        stamp = stamp or 'waiting'
    text(345, 26, stamp, 11, MUTED)
    text(441, 24, 'Offline' if stale else 'Live', 13, AMBER if stale else GREEN)
    battery = data.get('battery', {})
    uptime = data.get('uptime_seconds')
    card(16, 64, 508, 179)
    text(30, 77, 'Battery & uptime', 15, bold=True)
    text(30, 110, fmt(battery.get('percent'), suffix='%'), 32, bold=True)
    if uptime is None:
        up = '—'
    else:
        minutes = int(uptime)//60
        days, remainder = divmod(minutes, 1440)
        hours, mins = divmod(remainder, 60)
        up = f'{days}d {hours}h' if days else f'{hours}h {mins}m'
    text(291, 110, up, 30, bold=True)
    text(30, 156, battery.get('status') or 'No battery gauge', 12, MUTED)
    text(291, 156, 'Since boot', 12, MUTED)
    d.line((60, 368*K, 1020, 368*K), fill=LINE, width=1)
    for x, label, value in [(30, 'Temperature', fmt(battery.get('temperature_c'),1,' °C')),
                             (201, 'Voltage', fmt(battery.get('voltage_v'),3,' V')),
                             (365, 'Current', fmt(battery.get('current_ma'),0,' mA'))]:
        text(x, 191, label, 10, MUTED)
        text(x, 208, value, 14)

    cpu, memory = data.get('cpu', {}), data.get('memory', {})
    storage, gpu = data.get('storage', {}), data.get('gpu', {})
    state = gpu.get('power_state')
    load = gpu.get('utilization_percent')
    gpu_value = 'Unavailable' if not gpu.get('available') else 'Sleeping' if state == 'suspended' else fmt(load,0,'%') if load is not None else 'n/a'
    metrics = [(16, 367, 'CPU · whole device', fmt(cpu.get('percent'),1,'%'), f"{cpu.get('logical_cores', '—')} logical cores", cpu.get('percent')),
               (275, 367, 'Memory', gib(memory.get('used_bytes')), f"of {gib(memory.get('total_bytes'))}", memory.get('used_percent')),
               (16, 680, 'Storage', gib(storage.get('used_bytes')), f"{gib(storage.get('free_bytes'))} free", storage.get('used_percent')),
               (275, 680, gpu.get('model') or 'GPU', gpu_value, 'GPU load' if load is not None else 'No load counter', load)]
    for x,y,label,value,sub,percent in metrics:
        card(x,y,249,112)
        text(x+14,y+12,label,11,MUTED)
        text(x+14,y+33,value,25,bold=True,max_width=221)
        bar(x+14,y+77,221,percent)
        text(x+14,y+87,sub,11,MUTED,max_width=221)

    wifi = data.get('wifi', {})
    card(16,253,508,47)
    text(30,260,wifi.get('ssid') or ('Disconnected' if wifi.get('connected') is False else 'Wi-Fi unknown'),13,bold=True,max_width=180)
    text(220,260,wifi.get('ip_address'),12,MUTED,max_width=165)
    text(428,260,fmt(wifi.get('signal_percent'),suffix='%'),12,CYAN)
    text(30,279,'Wi-Fi',9,MUTED)
    text(220,279,wifi.get('rate_mbps'),9,MUTED,max_width=260)

    card(16,310,508,47)
    services = data.get('services', {})
    for x,label,key in [(30,'SSH','ssh'), (190,'Tailscale','tailscale'),
                        (390,'USB','usb')]:
        up = services.get(key, {}).get('up')
        status = 'Up' if up is True else 'Down' if up is False else 'Unknown'
        text(x,322,f'{label}  {status}',13,GREEN if up is True else AMBER if up is False else MUTED,bold=True)

    card(16,489,508,181)
    text(30,501,'Top processes',14,bold=True)
    text(382,503,'CPU',11,MUTED)
    text(467,503,'RAM',11,MUTED)
    processes = sorted(data.get('processes', []), key=lambda p:(p.get('cpu_percent') or 0,p.get('rss_bytes') or 0), reverse=True)[:5]
    if not processes:
        text(30,544,'No process data',12,MUTED)
    for i,p in enumerate(processes):
        y=537+i*21
        text(30,y,f"{p.get('name','?')} · {p.get('pid','?')}",12,max_width=330)
        text(377,y,fmt(p.get('cpu_percent'),1,'%'),11,MUTED)
        text(457,y,fmt((p['rss_bytes']/2**20) if p.get('rss_bytes') is not None else None,0,' M'),11,MUTED)
    text(30,650,'CPU per core · resident memory',9,MUTED)

    card(16,802,508,109)
    containers = data.get('containers', {})
    items = containers.get('containers', [])
    running = sum(c.get('state') == 'running' for c in items)
    text(30,814,'Containers',14,bold=True)
    text(371,817,f'{running} / {len(items)} running' if containers.get('available') else 'Unavailable',10,MUTED)
    for i,c in enumerate(items[:2]):
        y=846+i*26
        text(30,y,c.get('name') or 'Unnamed',12,max_width=325)
        text(392,y,c.get('state') or 'Unknown',11,GREEN if c.get('state')=='running' else AMBER,max_width=113)
    if not items:
        text(30,850,'No containers' if containers.get('available') else 'Status unavailable',12,MUTED)
    return image


# ---- touch UI: gestures, pages, docker helpers -------------------------------------------------
W, H = 1080, 2340            # layout/touch space; frames are drawn at P x this (the 720x1560 panel)
P = 2 / 3
IMG = (720, 1560)
TAB_Y = 2190
TABS = ('Overview', 'Containers', 'Processes', 'Network', 'Power')
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
WIFI_BTN = (30, 1340, 1050, 1480)       # Network page: open the picker
NET_ROWS = (('ssh', 'SSH'), ('tailscale', 'Tailscale'), ('usb', 'USB'), ('bluetooth', 'Bluetooth'))
NET_TOP, NET_ROW = 720, 150
SSH_TOGGLE = (700, 185, 1035, 305)
USER_BTN = (30, 350, 1050, 490)
BT_POWER = (30, 170, 1050, 290)
BT_TOP = 320
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


def back_btn(text, card):
    card(30, 30, BACK_BTN[2] - BACK_BTN[0], BACK_BTN[3] - BACK_BTN[1])
    text(140, 80, '‹ Back', 44, anchor='mm')


def btn(text, card, box, label, color=CYAN, size=48, bold=True):
    card(box[0], box[1], box[2] - box[0], box[3] - box[1], outline=color)
    text((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, label, size, color, bold=bold, anchor='mm')


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


def page_bt(w, data):
    image = Image.new('RGB', IMG, BG)
    d, text, card, bar = tools(image)
    back_btn(text, card)
    text(290, 48, 'Bluetooth', 56, bold=True)
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
        tag = 'Connected · tap to disconnect' if dev['connected'] else 'Paired · tap to connect' if dev['paired'] \
            else 'Tap to pair'
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
        if self.confirm:
            if kind == 'tap':
                act, arg = self.confirm[:2]
                if hit(CANCEL_BTN, g[1], g[2]):
                    self.confirm = None
                elif hit(RESTART_BTN, g[1], g[2]):
                    self.confirm = None
                    return [('act', act, arg)]
            return []
        if self.wifi is not None:
            return self.on_wifi(g, now)
        if self.sub is not None:
            return self.on_sub(g, data, now)
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
            elif self.page == 0 and 802 * 2.4 <= y <= 911 * 2.4:
                self.page = 1
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
                else:
                    self.sub = {'kind': 'bt', 'devs': None, 'on': up}
                    return [('wifi', bt_scan)]
            elif self.page == 3 and hit(WIFI_BTN, x, y):
                self.wifi = {'nets': None, 'sel': None, 'pw': '', 'mode': 'abc', 'show': False}
                return [('wifi', wifi_scan)]
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

    def on_sub(self, g, data, now):
        """SSH / USB / Bluetooth sub-pages of the Network tab."""
        w, kind = self.sub, g[0]
        if (kind == 'tap' and hit(BACK_BTN, g[1], g[2])) or (kind == 'swipe' and g[1] == 'right'):
            self.sub = None
            return []
        if kind != 'tap':
            return []
        x, y = g[1], g[2]
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
        elif w['kind'] == 'bt':
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
        if self.wifi is not None:
            image = page_wifi(self.wifi)
        elif self.sub is not None:
            image = {'ssh': page_ssh, 'usb': page_usb, 'bt': page_bt}[self.sub['kind']](self.sub, data)
        elif self.detail:
            image = page_detail(self.detail, self.info, data)
        elif self.pdetail:
            image = page_proc_detail(self, data, now)
        elif self.page == 0:
            image = render(data, stale)
        else:
            image = (page_containers, page_processes, page_network, page_battery)[self.page - 1](
                data, stale, *((self.scroll,) if self.page in (1, 2) else ()))
        d, text, card, bar = tools(image)
        if not (self.detail or self.pdetail or self.wifi is not None or self.sub is not None):
            d.rectangle((0, TAB_Y * P, IMG[0], IMG[1]), fill=PANEL)
            d.line((0, TAB_Y * P, IMG[0], TAB_Y * P), fill=LINE, width=1)
            for i, name in enumerate(TABS):
                text(i * TAB_W + TAB_W // 2, (TAB_Y + H) // 2, name, 36, CYAN if i == self.page else MUTED,
                     bold=i == self.page, anchor='mm')
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
        return image


def tools(image):
    d = ImageDraw.Draw(image)

    def text(x, y, value, size, color=TEXT, bold=False, max_width=None, anchor='la', mono=False):
        value = str(value if value is not None else '—')
        f = font(max(8, round(size * P)), bold, mono)
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
        text(980, y + 35, state, 48, GREEN if up is True else AMBER if up is False else MUTED, bold=True, anchor='ra')
        text(1030, y + 33, '›', 52, MUTED, anchor='ra')
    card(WIFI_BTN[0], WIFI_BTN[1], WIFI_BTN[2] - WIFI_BTN[0], WIFI_BTN[3] - WIFI_BTN[1], outline=CYAN)
    text(540, (WIFI_BTN[1] + WIFI_BTN[3]) // 2, 'Change Wi-Fi network', 48, CYAN, bold=True, anchor='mm')
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
    assert ui.on_gesture(('tap', 500, 1350), data, 0) == [('wifi', wifi_scan)] and ui.wifi['nets'] is None
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
                if isinstance(res, list):
                    if ui.wifi is not None:
                        ui.wifi['nets'] = res
                    elif ui.sub is not None:
                        ui.sub['devs'] = res
                else:
                    logging.info('Job result: %s', res[1])
                    ui.toast = (res[1], now + 6)
                    if len(res) > 2 and ui.sub is not None:
                        ui.sub['devs'] = res[2]
                    if res[0]:
                        ui.wifi = None
                wifi_future, ui_dirty = None, True
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
