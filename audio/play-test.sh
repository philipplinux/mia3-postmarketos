#!/bin/sh
# Quiet speaker test (as root): amp as set by max98937-init.sh, -40 dBFS 440 Hz for 1.5 s (DB=-30 for louder). SILENT=1 plays zeros.
# The q6asm "MultiMedia1" PCM drives DSP stream MM3 (overlay order), hence the MultiMedia3 route.
set -e
C=$(grep -m1 Mi-A3 /proc/asound/cards | awk '{print $1}')
amixer -c$C -q cset name='SEC_MI2S_RX Audio Mixer MultiMedia3' on
python3 - "${SILENT:-0}" "${DB:--40}" <<'PY'
import math, struct, sys, wave
r, a = 48000, 0 if sys.argv[1] == '1' else 10 ** (float(sys.argv[2]) / 20) * 32767
n = int(r * 1.5)
with wave.open('/tmp/mia3-test.wav', 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(r)
    for i in range(n):
        s = int(a * min(1, i / 2400, (n - i) / 2400) * math.sin(2 * math.pi * 440 * i / r))
        w.writeframes(struct.pack('<hh', s, s))
PY
timeout -s KILL 10 aplay -D hw:$C,0 /tmp/mia3-test.wav
