# netdash
![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white)

<img src="https://github.com/RykerWilder/static_files/blob/main/netdash.webp" alt="Netdash">

Animated terminal dashboard for **macOS and Linux**, in a single Python file: it shows the quality of your
connection, who is on your network and which Bluetooth devices are nearby.

## Installation

```bash
python3 -m pip install -r requirements.txt
python3 netdash.py
```

## Tabs

Switch tabs with `1` `2` `3` or `Tab` / `Shift-Tab`. Quit with `q`.
Nothing is measured or scanned until you press the key for it.

### 1 · Dashboard

Latency to your router, the Internet and a far host, with a one-line verdict that says where the problem is
(Wi-Fi/router, your provider, or far away). Also live traffic graphs, the speed test, your network interfaces
and your paired Bluetooth devices.

| Key | Action |
|-----|--------|
| `s` | run the speed test (works on every tab) |
| `a` | show/hide virtual and loopback interfaces |

### 2 · Devices

Who is on your own local network: IP address, MAC address, name, vendor and online/offline state. Devices that
appear after the first scan are tagged `new`. Only private networks are scanned: use it only on networks you own
or are allowed to inspect.

| Key | Action |
|-----|--------|
| `n` | scan the network |
| `↑` `↓` / `j` `k`, `PgUp` `PgDn`, `Home` `End` | scroll the list |

### 3 · Nearby

Bluetooth Low Energy devices around you, stronger than −80 dBm: name, maker, signal strength (RSSI), a rough
distance and a short signal trend. On macOS, allow your terminal under System Settings > Privacy & Security > Bluetooth.

| Key | Action |
|-----|--------|
| `b` | start / stop the scan (leaving the tab stops it) |
| `↑` `↓` / `j` `k`, `PgUp` `PgDn`, `Home` `End` | scroll the list |
