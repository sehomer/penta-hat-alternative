#!/usr/bin/env python3
"""Radxa Penta SATA HAT top board on drive.local: OLED status, fan control, button.

Hardware (Pi 5, header GPIO on chip "pinctrl-rp1"):
  OLED  SSD1306 128x32, I2C bus 1 @ 0x3C, reset on GPIO23 (must be held high)
  Fan   GPIO27, high = on, software PWM
  Button GPIO17, active low (input with pull-up)

Button: click = next page (holds it for MANUAL_HOLD s), long press = display on/off.
Runs as root (smartctl/zpool) via penta-hat.service.
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import gpiod
from PIL import ImageFont
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306

# ---- settings ---------------------------------------------------------------
GPIO_CHIP = "pinctrl-rp1"
PIN_RESET, PIN_FAN, PIN_BUTTON = 23, 27, 17

POOL = "store"
EXT_MOUNT = "/srv/dev-disk-by-uuid-641A-B12F"
EXT_NAME = "TB HDD"
NET_IF = "end0"

PAGE_SECONDS = 6          # auto-advance interval
MANUAL_HOLD = 30          # after a button click, stay on that page this long
LONG_PRESS = 1.5          # seconds
NIGHT_HOURS = (23, 7)     # dim display from 23:00 to 07:00
CONTRAST_DAY, CONTRAST_NIGHT = 0xCF, 0x01
ROTATE = 2                # display rotation in 90° steps (2 = upside down)

# fan curves: (temperature °C, duty 0..1); the highest step reached wins,
# and the fan runs at the max of the CPU and hottest-SSD result.
CPU_CURVE = [(50, 0.25), (60, 0.50), (65, 0.75), (70, 1.0)]
SSD_CURVE = [(35, 0.25), (40, 0.50), (45, 0.75), (50, 1.0)]
FAN_PERIOD = 0.025        # software PWM period (s), same as Radxa's driver

UPDATES_RECHECK = 6 * 3600  # re-run the apt check at least this often (s);
                            # it also re-runs whenever apt lists or dpkg change

SSD_TEMP_ALERT = 60
CPU_TEMP_ALERT = 80

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


def log(*a):
    print(*a, flush=True)


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as e:
        log("cmd failed:", cmd, e)
        return ""


def hbytes(n):
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1000 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K") or n >= 100 else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def curve(temp, steps):
    duty = 0.0
    for t, d in steps:
        if temp is not None and temp >= t:
            duty = d
    return duty


# ---- hardware ---------------------------------------------------------------
class Fan:
    """Software PWM on a GPIO line. Starts at 100% until the first reading."""

    def __init__(self, line):
        self.line = line
        self.duty = 1.0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            d = self.duty
            if d >= 1.0 or d <= 0.0:
                self.line.set_value(1 if d >= 1.0 else 0)
                time.sleep(0.5)
            else:
                self.line.set_value(1)
                time.sleep(FAN_PERIOD * d)
                self.line.set_value(0)
                time.sleep(FAN_PERIOD * (1 - d))


class Button(threading.Thread):
    def __init__(self, line, events):
        super().__init__(daemon=True)
        self.line, self.events = line, events

    def run(self):
        pressed_at = None
        while True:
            down = self.line.get_value() == 0
            now = time.monotonic()
            if down and pressed_at is None:
                pressed_at = now
            elif down and pressed_at is not None and now - pressed_at >= LONG_PRESS:
                self.events.append("long")
                while self.line.get_value() == 0:   # wait for release
                    time.sleep(0.02)
                pressed_at = None
            elif not down and pressed_at is not None:
                if now - pressed_at >= 0.03:        # debounce
                    self.events.append("click")
                pressed_at = None
            time.sleep(0.02)


def _kver(v):
    """'6.12.109+rpt-rpi-2712' -> ((6, 12, 109), 'rpt-rpi-2712')"""
    num, _, flavour = v.partition("+")
    return tuple(int(x) for x in re.findall(r"\d+", num)), flavour


def reboot_reason():
    """Why a reboot is needed, or None.

    /run/reboot-required is Debian's flag (set by unattended-upgrades'
    kernel hook; OMV's daily cron checks it too). As a fallback, a newer
    installed kernel of the running flavour also counts.
    """
    if os.path.exists("/run/reboot-required"):
        try:
            with open("/run/reboot-required.pkgs") as f:
                pkgs = sorted({l.strip() for l in f if l.strip()})
        except OSError:
            pkgs = []
        return ", ".join(pkgs) or "updates"
    run_num, run_flav = _kver(os.uname().release)
    try:
        newer = [v for v in os.listdir("/lib/modules")
                 if _kver(v)[1] == run_flav and _kver(v)[0] > run_num]
    except OSError:
        newer = []
    return "new kernel" if newer else None


# ---- data collection ----------------------------------------------------------
class Stats(threading.Thread):
    def __init__(self, fan):
        super().__init__(daemon=True)
        self.fan = fan
        self.d = {"disks": {}, "alerts": [], "pool": {}, "ext": None,
                  "rx": 0.0, "tx": 0.0, "cpu": None, "fan": 1.0,
                  "updates": None, "reboot": None}
        self._net_prev = None
        self._upd_key = None

    def run(self):
        last_pool = last_smart = -1e9
        while True:
            now = time.monotonic()
            for name, fn, every, last in (("fast", self.fast, 0, None),
                                          ("pool", self.pool, 30, last_pool),
                                          ("smart", self.smart, 120, last_smart),
                                          ("updates", self.updates, 30, last_pool)):
                if last is not None and now - last < every:
                    continue
                try:
                    fn()
                except Exception as e:
                    log(f"{name} update failed:", repr(e))
            if now - last_pool >= 30:
                last_pool = now
            if now - last_smart >= 120:
                last_smart = now
            self.update_fan()
            self.update_alerts()
            time.sleep(5)

    def fast(self):
        d = self.d
        reboot = reboot_reason()
        if reboot != d["reboot"]:
            log("reboot required:", reboot or "no")
        d["reboot"] = reboot
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            d["cpu"] = int(f.read()) / 1000
        d["load"] = os.getloadavg()[0]
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k] = int(v.split()[0])
        d["mem"] = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])
        with open("/proc/uptime") as f:
            up = int(float(f.read().split()[0]))
        dd, hh, mm = up // 86400, up % 86400 // 3600, up % 3600 // 60
        d["uptime"] = f"{dd}d{hh}h" if dd else f"{hh}h{mm:02d}m"
        ips = sh(["hostname", "-I"]).split()
        d["ip"] = next((i for i in ips if "." in i), "no IP")

        base = f"/sys/class/net/{NET_IF}/statistics/"
        with open(base + "rx_bytes") as f:
            rx = int(f.read())
        with open(base + "tx_bytes") as f:
            tx = int(f.read())
        now = time.monotonic()
        if self._net_prev:
            prx, ptx, pt = self._net_prev
            dt = max(now - pt, 0.1)
            d["rx"], d["tx"] = (rx - prx) / dt, (tx - ptx) / dt
        self._net_prev = (rx, tx, now)

        if os.path.ismount(EXT_MOUNT):
            st = os.statvfs(EXT_MOUNT)
            total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            d["ext"] = (100 * (1 - free / total), free)
        else:
            d["ext"] = None

    def pool(self):
        p = {}
        out = sh(["zpool", "list", "-H", "-o", "health", POOL])
        p["health"] = out or "MISSING"
        out = sh(["zfs", "list", "-Hp", "-o", "used,avail", POOL]).split()
        if len(out) == 2:
            p["used"], p["avail"] = int(out[0]), int(out[1])
        status = sh(["zpool", "status", POOL])
        p["errors"] = "No known data errors" not in status if status else False
        m = re.search(r"([\d.]+)% done", status)
        if "in progress" in status and m:
            p["scrub"] = f"scrub {float(m.group(1)):.0f}% running"
        else:
            m = re.search(r"scrub repaired \S+ in \S+ with (\d+) errors on \w+ (\w+) (\d+)", status)
            if m:
                errs, mon, day = m.groups()
                mon = datetime.strptime(mon, "%b").month
                p["scrub"] = f"scrub {day}.{mon}. " + ("ok" if errs == "0" else f"{errs} err")
                p["errors"] = p["errors"] or errs != "0"
            else:
                p["scrub"] = "no scrub yet"
        self.d["pool"] = p

    def smart(self):
        disks = {}
        out = sh(["lsblk", "-J", "-d", "-o", "NAME,TRAN"])
        names = [b["name"] for b in json.loads(out)["blockdevices"] if b.get("tran") == "sata"] if out else []
        for n in sorted(names):
            j = sh(["/usr/sbin/smartctl", "-j", "-A", "-H", f"/dev/{n}"])
            try:
                j = json.loads(j)
            except ValueError:
                disks[n] = (None, None)
                continue
            temp = j.get("temperature", {}).get("current")
            ok = j.get("smart_status", {}).get("passed")
            disks[n] = (temp, ok)
        self.d["disks"] = disks

    def updates(self):
        """Pending apt upgrades as (total, security, [names]); None until known.

        Uses apt's simulated dist-upgrade on the lists apt-daily already
        refreshes, so it never downloads or changes anything. Only re-runs
        when the apt lists or dpkg status change, or every UPDATES_RECHECK s.
        """
        try:
            key = (os.path.getmtime("/var/lib/apt/lists"),
                   os.path.getmtime("/var/lib/dpkg/status"),
                   int(time.time() // UPDATES_RECHECK))
        except OSError:
            key = None
        if key is not None and key == self._upd_key:
            return
        out = sh(["apt-get", "-s", "-o", "Debug::NoLocking=1", "dist-upgrade"], timeout=60)
        if not out:
            return          # apt busy or failed: keep the last result, retry later
        inst = [l for l in out.splitlines() if l.startswith("Inst ")]
        names = [l.split()[1] for l in inst]
        sec = sum(1 for l in inst if re.search(r"[-/ ]security", l.split("(", 1)[-1], re.I))
        upd = (len(inst), sec, names)
        if upd[:2] != (self.d["updates"] or (None, None))[:2]:
            log(f"updates: {upd[0]} pending, {upd[1]} security")
        self.d["updates"] = upd
        self._upd_key = key

    def update_fan(self):
        d = self.d
        temps = [t for t, _ in d["disks"].values() if t is not None]
        if d["cpu"] is None or not temps:
            duty = 1.0                       # no data yet: be safe
        else:
            duty = max(curve(d["cpu"], CPU_CURVE), curve(max(temps), SSD_CURVE))
        if duty != d["fan"]:
            log(f"fan {d['fan']:.0%} -> {duty:.0%} (cpu {d['cpu']}, ssd max {max(temps) if temps else '?'})")
        d["fan"] = self.fan.duty = duty

    def update_alerts(self):
        d, a = self.d, []
        p = d["pool"]
        if p and p.get("health") != "ONLINE":
            a.append(f"POOL {p.get('health')}")
        if p.get("errors"):
            a.append("POOL ERRORS")
        for n, (t, ok) in d["disks"].items():
            if ok is False:
                a.append(f"{n} SMART FAIL")
            if t is not None and t >= SSD_TEMP_ALERT:
                a.append(f"{n} HOT {t}C")
        if d["cpu"] is not None and d["cpu"] >= CPU_TEMP_ALERT:
            a.append(f"CPU HOT {d['cpu']:.0f}C")
        if a != d["alerts"]:
            log("alerts:", a or "none")
        d["alerts"] = a


# ---- display ----------------------------------------------------------------
def page_system(d):
    return [d.get("ip", "..."),
            f"CPU {d['cpu'] or 0:.0f}C  load {d.get('load', 0):.2f}",
            f"Mem {d.get('mem', 0):.0f}%  up {d.get('uptime', '?')}"]


def page_pool(d):
    p = d["pool"]
    if not p:
        return [f"ZFS {POOL}", "reading..."]
    lines = [f"ZFS {POOL}  {p['health']}"]
    if "used" in p:
        lines.append(f"{hbytes(p['used'])} used {hbytes(p['avail'])} free")
    lines.append(p.get("scrub", ""))
    return lines


def page_disks(d):
    items = [f"{n} {t if t is not None else '?'}C" for n, (t, _) in d["disks"].items()]
    items.append(f"fan {d['fan']:.0%}")
    return [" ".join(f"{x:<8}" for x in items[i:i + 2]).rstrip() for i in range(0, len(items), 2)][:3]


def page_ext_net(d):
    ext = d["ext"]
    l1 = f"{EXT_NAME} {ext[0]:.0f}% {hbytes(ext[1])} free" if ext else f"{EXT_NAME} not mounted"
    return [l1,
            f"↓{hbytes(d['rx'])}/s ↑{hbytes(d['tx'])}/s",
            datetime.now().strftime("%d.%m. %H:%M")]


def page_updates(d):
    total, sec, names = d["updates"] or (0, 0, [])
    lines = [f"Updates: {total} pending"] if total else []
    if sec:
        lines.append(f"!! {sec} SECURITY !!")
    if d["reboot"]:
        lines.append(">> REBOOT ME <<")
        if not total:
            lines.append(f"for {d['reboot']}")
    if total:
        lines.append(", ".join(names))
    return lines[:3]


PAGES = [page_system, page_pool, page_disks, page_ext_net]


def pages(d):
    """The page list; the updates page only appears while updates are
    pending or a reboot is required."""
    if (d["updates"] and d["updates"][0]) or d["reboot"]:
        return PAGES + [page_updates]
    return PAGES


def draw(dev, font, lines, invert=False):
    with canvas(dev) as c:
        if invert:
            c.rectangle(dev.bounding_box, fill="white")
        for i, text in enumerate(lines[:3]):
            c.text((0, i * 11 - 1), text, font=font, fill="black" if invert else "white")


def is_night():
    h, (start, end) = datetime.now().hour, NIGHT_HOURS
    return h >= start or h < end


def main():
    chip = gpiod.Chip(GPIO_CHIP)
    reset = chip.get_line(PIN_RESET)
    reset.request(consumer="penta-oled-reset", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[1])
    reset.set_value(0)
    time.sleep(0.05)
    reset.set_value(1)
    time.sleep(0.05)

    fan_line = chip.get_line(PIN_FAN)
    fan_line.request(consumer="penta-fan", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[1])
    btn_line = chip.get_line(PIN_BUTTON)
    btn_line.request(consumer="penta-button", type=gpiod.LINE_REQ_DIR_IN,
                     flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP)

    dev = ssd1306(i2c(port=1, address=0x3C), width=128, height=32, rotate=ROTATE)
    font = ImageFont.truetype(FONT_PATH, 10)
    draw(dev, font, ["drive", "starting..."])

    fan = Fan(fan_line)
    stats = Stats(fan)
    stats.start()
    events = []
    Button(btn_line, events).start()

    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)

    page, next_switch, manual_until = 0, time.monotonic() + PAGE_SECONDS, 0.0
    display_on, contrast = True, None
    log("penta-hat started")
    try:
        while True:
            now = time.monotonic()
            while events:
                ev = events.pop(0)
                if ev == "long":
                    display_on = not display_on
                    dev.show() if display_on else dev.hide()
                    log("display", "on" if display_on else "off")
                elif not display_on:
                    display_on = True
                    dev.show()
                else:
                    page += 1
                    manual_until = now + MANUAL_HOLD
                    next_switch = now + PAGE_SECONDS

            if now >= next_switch and now >= manual_until:
                page += 1
                next_switch = now + PAGE_SECONDS

            d = stats.d
            alerts = d["alerts"]
            want = CONTRAST_NIGHT if is_night() and not alerts else CONTRAST_DAY
            if want != contrast:
                dev.contrast(want)
                contrast = want

            if display_on or alerts:
                if alerts and not display_on:
                    display_on = True
                    dev.show()
                # with alerts, every other page is the (inverted) alert page
                if alerts and page % 2:
                    draw(dev, font, ["!! ALERT !!"] + alerts[:2], invert=True)
                else:
                    plist = pages(d)
                    idx = (page // 2 if alerts else page) % len(plist)
                    draw(dev, font, plist[idx](d))
            time.sleep(0.5)
    finally:
        fan_line.set_value(1)   # leave the fan on if we stop
        draw(dev, font, ["penta-hat", "stopped"])
        dev.persist = True


if __name__ == "__main__":
    sys.exit(main())
