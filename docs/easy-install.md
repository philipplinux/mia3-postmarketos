# Easy install: the ready image, step by step

This turns an old Xiaomi Mi A3 into a small Linux server with a dashboard on its screen.
No building, no coding. About 30 minutes, most of it waiting.

> **Everything on the phone gets erased.** Copy your photos and files off it first.

## What you need

- A **Xiaomi Mi A3** (model M1906F9SH) that still runs Android
- A **USB cable** that does data, not just charging (the one from a phone box usually works)
- A **computer**: Windows, Mac or Linux
- Optional: a **microSD card**, used as extra storage

## Step 1: Get the fastboot tool on your computer

Fastboot is a small program that lets the computer write to the phone.

- **Windows:** download [SDK Platform-Tools for Windows](https://developer.android.com/tools/releases/platform-tools)
  and unzip it to `C:\platform-tools`. Also install [Git for Windows](https://git-scm.com/download/win), which
  gives you "Git Bash", a terminal that can run the flash script. Open Git Bash and type
  `export PATH="$PATH:/c/platform-tools"` each time before you use fastboot.
- **Mac:** install [Homebrew](https://brew.sh), then in Terminal: `brew install --cask android-platform-tools`
- **Linux:** Fedora/Arch `sudo dnf install android-tools` or `sudo pacman -S android-tools`, Debian/Ubuntu `sudo apt install fastboot`

Check it worked: type `fastboot --version` in the terminal. You should see a version number.

## Step 2: Unlock the phone (one time, erases it)

The phone only accepts a new system after you unlock it.

1. On the phone, open **Settings → About phone** and tap **Build number** 7 times. It says "You are now a developer".
2. Go to **Settings → System → Developer options** and turn on **OEM unlocking**.
3. Turn the phone off. Hold **Volume down + Power** until you see a rabbit with a hat. This is **fastboot mode**.
4. Plug the phone into the computer. In the terminal type `fastboot devices`. You should see one line ending in `fastboot`.
5. Type `fastboot flashing unlock`. On the phone, use the volume keys to pick **Unlock** and press Power.
6. The phone wipes itself and starts Android again. Skip the setup screens.

## Step 3: Download the image

1. Go to the [Releases page](https://github.com/philipplinux/mia3-postmarketos/releases) and download
   `mia3-postmarketos-generic-<date>.tar.xz` (about 400 MB).
2. Unpack it. Windows: right-click → Extract all (or use [7-Zip](https://www.7-zip.org) if that fails). Mac/Linux: double-click.
   You get a folder with `boot.img`, `rootfs.img`, `vbmeta.img` and `flash.sh`.

## Step 4: Put it on the phone

1. Turn the phone off, then hold **Volume down + Power** for fastboot mode again. Plug it in.
2. In the terminal, go into the unpacked folder: type `cd ` (with a space), drag the folder onto the terminal window, press Enter.
3. Type `sh flash.sh` and press Enter. Type `yes` when it asks.
4. Wait about 5 minutes. It ends with `flashed`, and the phone restarts by itself.

After about a minute the phone shows a dashboard on its screen. Android is still on the phone, hidden on the
other "slot", in case you ever want it back.

## Step 5: Make it yours, on the phone's screen

No computer needed for this. Tap the **Network** tab (the icons at the bottom of the screen) and:

1. **Change the password:** tap **SSH**, type a new password on the on-screen keyboard (at least 6 characters),
   tap **Set password**. Everyone knows the default `changeme`, so do this first.
2. **Connect to Wi-Fi:** tap the big **Wi-Fi** card at the top, then **Change network**, pick your network,
   type its password, tap **Connect**.

That's it. You can unplug the cable now.

Logging in from a computer works too: `ssh user@172.16.42.1` over the USB cable, or `ssh user@<phone address>` over
Wi-Fi (the address is on the Network tab). The password is the one you just set.

## Using it

- **Terminal:** on the dashboard, open the Misc tab and tap **Terminal** (shell with an on-screen keyboard).
- **The screen:** press the power key to switch between the dashboard, the text console and screen off.
  Swipe to change dashboard tabs.
- **Web dashboard:** turn it on from the phone screen (Network tab), then open `http://<phone address>:8090` in a browser.
- **Battery:** it stops charging at 50 % on purpose, so the battery lasts years while plugged in all the time.
- **microSD card:** see "microSD card" in the [README](../README.md#microsd-card).
- **Reach it from anywhere (optional):** log in from a computer, type `sudo tailscale up` and open the link it prints.

## If something goes wrong

| Problem | Fix |
|---|---|
| `fastboot devices` shows nothing | Try another cable or USB port. Windows: install the [Google USB driver](https://developer.android.com/studio/run/win-usb) |
| `flash.sh` says the bootloader is locked | Do Step 2 again |
| `ssh` says "no route" or times out | Wait one more minute, or unplug and replug the cable |
| The screen stays black | Press the power key once. Still black after 3 minutes: hold Power 10 s to restart |
| You want Android back | In fastboot mode type `fastboot set_active a` and then `fastboot reboot` |
