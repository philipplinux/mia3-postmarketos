#!/bin/sh
# MAX98937 speaker amp setup over i2c-dev (bus 1, 0x3a), as root, after the card is up (bringup.sh).
# Register values: Xiaomi's GPL max98937.c (max98937_reg_channel_map[0] + max98937_reg_common_map), except
# speaker gain 0x51 (+3 dB; vendor 0x55 = +15 dB with their DSM limiter). Then I2S slave, 16-bit, 32 BCLK/LRCLK,
# 48 kHz, amp + global enable. Global enable only latches while BCLK runs, so it is set during 1 s of silence;
# it then stays set across streams.
# Volume: register 0x0036, 0..127 = -16..+15.75 dB; kept in /var/lib/mia3-audio/volume.
set -e
modprobe i2c-dev
V=$(cat /var/lib/mia3-audio/volume 2>/dev/null || echo 48)
w() { i2ctransfer -y 1 w3@0x3a 0x${1%??} 0x${1#??} 0x$2; }
w 0500 00
while read -r a v; do w "$a" "$v"; done <<'REGS'
0040 01
001e 01
0018 03
001a 03
001c fc
0028 80
0029 01
0011 03
0053 00
0055 00
005e 16
005f 00
0060 0c
0061 0c
0062 12
0063 00
0064 0c
0065 0c
0066 0e
0067 03
0068 18
0069 18
006a 0a
006b 00
006c 80
006d 00
005a 00
005b 00
005c 1f
005d 00
0042 07
0014 75
0015 8c
0017 55
003e f7
0023 40
003a 00
001b 00
0019 00
001f 03
001d ff
003d 03
0035 00
0037 0f
003b 51
003c 85
003f 1c
0041 40
0043 00
0044 ff
0016 08
0073 0f
0077 01
0052 02
0020 00
0021 00
0022 00
004e 10
0079 45
007a 2b
007b 04
0024 00
0023 40
0025 02
0026 08
0027 80
0039 01
REGS
w 0036 "$(printf %02x "$V")"
C=$(grep -m1 Mi-A3 /proc/asound/cards | awk '{print $1}')
amixer -c"$C" -q cset name='SEC_MI2S_RX Audio Mixer MultiMedia3' on
timeout 2 aplay -q -D hw:"$C",0 -t raw -f S16_LE -r 48000 -c 2 -d 1 /dev/zero &
sleep 0.4
w 0500 01
wait
[ "$(i2ctransfer -y 1 w2@0x3a 0x05 0x00 r1)" = 0x01 ] || { echo "max98937: global enable did not latch" >&2; exit 1; }
