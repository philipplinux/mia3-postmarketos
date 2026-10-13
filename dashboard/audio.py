"""Speaker volume (MAX98937 register 0x0036 over i2c-dev) and a short test tone. Needs the Mi-A3 card
(audio/bringup.sh), i2c-tools, and the audio group on /dev/snd + /dev/i2c-1 (audio/60-mia3-i2c.rules)."""
import io
import math
import re
import struct
import subprocess
import wave

MAX = 127  # AMP_VOL 0..127 = -16..+15.75 dB
VOL_FILE = '/var/lib/mia3-audio/volume'  # read by audio/max98937-init.sh at boot
ROUTE = 'SEC_MI2S_RX Audio Mixer MultiMedia3'
_player = None


def _card():
    try:
        m = re.search(r'^\s*(\d+) \[.*\]: .*Mi-A3', open('/proc/asound/cards').read(), re.M)
    except OSError:
        return None
    return m and m.group(1)


def _amixer(card, *args):
    return subprocess.run(['amixer', '-c', card, *args], capture_output=True, text=True, timeout=5)


def _amp(*data, read=False):
    """i2ctransfer to the amp (bus 1, 0x3a): register hi, lo[, value]; returns the read byte or None."""
    cmd = ['i2ctransfer', '-y', '1', f'w{len(data)}@0x3a', *(hex(b) for b in data)] + (['r1'] if read else [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(p.stdout, 16) if read and p.returncode == 0 else (0 if p.returncode == 0 else None)


def audio_info():
    card = _card()
    if card is None:
        return {'available': False}
    v = _amp(0x00, 0x36, read=True)
    return {'available': True, 'volume': round((v & MAX) * 100 / MAX) if v is not None else None,
            'playing': _player is not None and _player.poll() is None}


def set_volume(percent):
    card = _card()
    if card is None:
        return False, 'sound card is not up (run audio/bringup.sh)'
    if not isinstance(percent, int) or isinstance(percent, bool) or not 0 <= percent <= 100:
        return False, 'bad volume'
    v = round(percent * MAX / 100)
    if _amp(0x00, 0x36, v) is None:
        return False, 'amp write failed (i2c-1 access?)'
    try:
        with open(VOL_FILE, 'w') as f:
            f.write(f'{v}\n')
    except OSError:
        pass  # ponytail: volume then resets to the boot default on reboot
    return True, f'volume {percent}%'


def tone(db=-20, secs=1.0, hz=440, rate=48000):
    """WAV bytes: sine at db dBFS with 50 ms fades."""
    a, n, f = 10 ** (db / 20) * 32767, int(rate * secs), rate // 20
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b''.join(struct.pack('<hh', s, s) for s in (
            int(a * min(1, i / f, (n - i) / f) * math.sin(2 * math.pi * hz * i / rate)) for i in range(n))))
    return buf.getvalue()


def play_tone():
    global _player
    card = _card()
    if card is None:
        return False, 'sound card is not up (run audio/bringup.sh)'
    if _player is not None and _player.poll() is None:
        return False, 'already playing'
    _amixer(card, '-q', 'cset', f'name={ROUTE}', 'on')
    _player = subprocess.Popen(['timeout', '-s', 'KILL', '10', 'aplay', '-q', '-D', f'hw:{card},0', '-'],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _player.stdin.write(tone(db=-20))
    _player.stdin.close()
    return True, 'test tone: 440 Hz, 1 s'


if __name__ == '__main__':
    w = tone(secs=0.1)
    assert w[:4] == b'RIFF' and len(w) == 44 + 4800 * 4
    s = struct.unpack_from('<h', w, 44 + 4 * 2400)[0]  # mid-signal sample within -20 dBFS
    assert abs(s) <= 3277
    print('ok')
