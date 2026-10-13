#!/usr/bin/env python3
"""swaybar status_command: tty1-tty4 tabs across the top. tty1 = the stock fbcon console on VT 1
(kernel messages; mia3-display notices the switch and takes over there), tty2-4 = sway workspaces with foot shells.
A tap on a tab arrives as an i3bar click event on stdin."""
import fcntl, json, os, subprocess, sys, threading

TABS = '1234'
FBCON_TAB = '1'
WIDTH = 84          # logical px per tab: 720 px panel / scale 2 / 4 tabs, minus 6 px of borders
lock = threading.Lock()


def focused():
    try:
        ws = json.loads(subprocess.run(['swaymsg', '-t', 'get_workspaces', '-r'], capture_output=True, text=True).stdout)
        return next((w['name'] for w in ws if w['focused']), '')
    except (OSError, ValueError):
        return ''


def blocks(cur):
    return [dict(name=n, full_text=f'tty{n}', min_width=WIDTH, align='center', separator=False, separator_block_width=0,
                 color='#ffffff', background='#3a6ea5' if n == cur else '#1e1e1e', border='#1e1e1e',
                 border_top=4, border_bottom=4, border_left=3, border_right=3) for n in TABS]


def draw():
    with lock:
        print(json.dumps(blocks(focused())) + ',', flush=True)


def watch():
    p = subprocess.Popen(['swaymsg', '-t', 'subscribe', '-m', '["workspace"]'], stdout=subprocess.PIPE, text=True)
    for _ in p.stdout:
        draw()


def click(line):
    try:
        n = json.loads(line.strip().lstrip(',[')).get('name')
    except ValueError:
        return
    if n == FBCON_TAB:
        fd = os.open('/dev/tty', os.O_RDWR)    # sway's controlling tty (tty2) may switch VTs
        try:
            fcntl.ioctl(fd, 0x5606, 1)          # VT_ACTIVATE
        finally:
            os.close(fd)
    elif n in TABS:
        subprocess.run(['swaymsg', 'workspace', n], capture_output=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        b = blocks('3')
        assert [x['full_text'] for x in b] == ['tty1', 'tty2', 'tty3', 'tty4'] and b[2]['background'] == '#3a6ea5'
        print('ok')
        sys.exit()
    print('{"version": 1, "click_events": true}\n[', flush=True)
    draw()
    threading.Thread(target=watch, daemon=True).start()
    for line in sys.stdin:
        click(line)
