# Agent rules for this repo

Xiaomi Mi A3 on postmarketOS (kernel 7.2.3-sm61x5). README.md has the full picture; this file is what an agent must not get wrong.

## Reaching the phone

| Host | Address | Use |
|---|---|---|
| `mia3` | Tailscale `100.x.y.z` | default |
| `mia3-wifi` | `10.0.0.50` | when Tailscale is down |
| USB | `172.16.42.1` | last resort |

- The OnePlus OT3 (`ot3-usb`) also sits at `172.16.42.1` on another USB interface. If the host key mismatches, you reached the wrong phone. Never override the host key.
- USB link dead: on the PC, `echo 0 | sudo tee /sys/bus/usb/devices/3-1/authorized`, then `echo 1`.
- Root commands: write the script locally, `scp` it to the phone's `/tmp`, run `sudo sh /tmp/x.sh` over `ssh -tt`.
- Never ask the owner to type on the phone.

## Deploying

- Everything at once: `sh deploy.sh` from the PC (see README "Install (one shot)").
- `display.py` alone: copy it to the phone and run `python3 display.py --selftest` there as root (the PC lacks the fonts). Then `install -m644` it to `/usr/local/lib/mia3-display/` and `systemctl restart mia3-display`.
- Web dashboard: files go in `~/projects/mia3-dashboard/dashboard/`, then `systemctl --user restart mia3-dashboard`. The phone has no curl: use python `urllib` against `http://127.0.0.1:8090/api/status`.
- Kernel modules: build on the PC against `~/projects/mia3-kernel/linux-7.2.3-r0` in `docker run alpine:edge` (`deploy.sh` does this). `.ko` files are gitignored.
- Runtime DT overlays apply once per boot. `mia3_dtbo` loads one overlay per insmod, and a second apply of the same overlay fails with `-EINVAL`.

## Don'ts

- **Secrets:** never print Wi-Fi passwords, API keys, tokens, private keys (`/var/lib/thermal-guard/notify_key`, `/etc/hawser/token`) or `.credentials.md`/`.fallback.env`.
- **debugfs:** no broad regmap reads. Read single registers only, when needed.
- **Docker on the phone:** never a bare `docker compose up -d`. Lidarr stays off.
- **Audio tests:** quiet. Use `mia3-chime test` or low-gain tones.
- **Out of scope:** earpiece, microphones and GPS are deliberately left disabled.
- **Proprietary files:** firmware, stock images and vendor sources belong in the private repo `mia3-postmarketos-private`, never here.

## Logging

Record work in the Vault: `~/Documents/Vault/log.md` and the hub `wiki/concepts/Hardware/Mi A3 - postmarketOS Setup.md`. Read the Vault `AGENTS.md` first.
