# Penta HAT Alternative

A lightweight replacement for Radxa's `rockpi-penta` software, for the
**Radxa Penta SATA HAT** top board on a **Raspberry Pi 5** NAS
(OpenMediaVault + ZFS). The whole thing is one Python script that drives the
top board's OLED, fan and button and shows the status of the NAS at a glance.

## Features

**OLED status pages** (SSD1306 128×32, rotated 180°, next page every 6 s)

| Page | Shows |
|---|---|
| System | IP address, CPU temperature and load, memory usage, uptime |
| ZFS pool | Pool health, used and free space, last scrub result or scrub progress |
| Disks | Temperature of every SATA SSD (via SMART) and the current fan speed |
| External disk and network | Fill level and free space of the external disk, network throughput ↓/↑ |
| Updates | Pending apt updates, a `!! SECURITY !!` warning, and **REBOOT ME** with the reason. Only shown when there's something to do. |

**Alerts:** when something is wrong, the display switches to an inverted
`!! ALERT !!` screen. That happens if the pool isn't ONLINE or has errors, a
disk fails SMART or goes above 60 °C, or the CPU goes above 80 °C.

**Fan control:** software PWM with two temperature curves. The fan runs at
whichever is higher: the CPU curve or the hottest SSD's curve.

| CPU °C | 50 | 60 | 65 | 70 |
|---|---|---|---|---|
| SSD °C | 35 | 40 | 45 | 50 |
| Fan | 25 % | 50 % | 75 % | 100 % |

The fan starts at 100 % until the first temperature reading.

**Button:** a click shows the next page and holds it for 30 s. A long press
(1.5 s) switches the display on or off.

**Night mode:** the display is dimmed from 23:00 to 07:00.

**Update check:** `apt-get -s dist-upgrade` is simulated without locking, so it
never gets in the way of OMV or apt. It runs again whenever the apt lists or
dpkg change, and at least every 6 hours. A reboot is flagged when
`/run/reboot-required` exists or a newer kernel is installed than the one
running.

## Hardware notes

These are the things that made the stock setup fail on a Pi 5:

- The header GPIOs are on the chip **`pinctrl-rp1`** (gpiochip15). Radxa's
  `rpi5.env` wrongly says 4.
- The OLED's **reset pin, GPIO23, must be held high**. Otherwise the display
  shows dots or noise.
- I2C has to be enabled: `dtparam=i2c_arm=on` in `/boot/firmware/config.txt`.

| Part | Connection |
|---|---|
| OLED SSD1306 128×32 | I2C bus 1, address `0x3C`, reset GPIO23 |
| Fan | GPIO27, high = on |
| Button | GPIO17, active low (pull-up) |

## Configuration

All settings are at the top of `penta_hat.py`. They include the pool name
(`POOL`), the external disk mount and label (`EXT_MOUNT`, `EXT_NAME`), the network
interface (`NET_IF`), page timing, night hours, display rotation, fan curves
and alert thresholds.

## Installation (on the NAS)

The script needs root, because it calls `smartctl` and `zpool`.

```bash
sudo apt install python3-libgpiod smartmontools fonts-dejavu-core
sudo mkdir -p /opt/penta-hat
sudo python3 -m venv --system-site-packages /opt/penta-hat/venv   # system gpiod
sudo /opt/penta-hat/venv/bin/pip install luma.oled
sudo install -m 755 penta_hat.py /opt/penta-hat/penta_hat.py
```

`/etc/systemd/system/penta-hat.service`:

```ini
[Unit]
Description=Radxa Penta SATA HAT top board (OLED, fan, button)
After=local-fs.target zfs-mount.service network-online.target folder2ram_startup.service
Wants=network-online.target

[Service]
ExecStart=/opt/penta-hat/venv/bin/python /opt/penta-hat/penta_hat.py
WorkingDirectory=/opt/penta-hat
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now penta-hat
journalctl -u penta-hat -f
```

Disable Radxa's `rockpi-penta` service first if it's installed, because both
would fight over the same GPIOs.

## Deploying changes

Edit the script here and copy it to the NAS:

```bash
scp penta_hat.py drive.local:/tmp/
ssh drive.local 'sudo install -m 755 /tmp/penta_hat.py /opt/penta-hat/penta_hat.py && sudo systemctl restart penta-hat'
```
