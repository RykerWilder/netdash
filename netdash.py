#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netdash - animated terminal dashboard for macOS and Linux.

Three pages (switch with 1 / 2 / 3 or Tab):
  1 Dashboard  latency to your router, the Internet and a far host with an automatic
               "where is the problem?" verdict, live traffic, speed test, network
               interfaces and paired Bluetooth devices
  2 Devices    who is on your local network: IP, MAC address, name, vendor
  3 Nearby     Bluetooth LE devices around you (stronger than -80 dBm) with signal strength

Install:   python3 -m pip install rich psutil bleak     (bleak is only needed for page 3)
Run:       python3 netdash.py
Keys:      1/2/3 or Tab = pages   s = speed test   n = scan the network (page 2)
           b = scan Bluetooth (page 3)   a = all interfaces (page 1)   q = quit
           Arrows or j/k scroll the lists. Nothing is measured or scanned until you ask.
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import importlib.util
import ipaddress
import json
import math
import os
import re
import select
import shutil
import socket
import ssl
import statistics
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import termios
    import tty
except ImportError:  # Windows
    sys.exit("netdash only works on macOS and Linux.")

try:
    import psutil
    from rich import box
    from rich.align import Align
    from rich.cells import cell_len
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"Missing dependency ({exc.name}). Install it with:\n"
        "    python3 -m pip install rich psutil"
    )

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# --------------------------------------------------------------------------- #
# Palette: colors chosen to stay distinguishable even without telling red from green.
# Meaning is NEVER carried by color alone: numbers, symbols (●/○/✖) or labels
# always sit next to it.
# --------------------------------------------------------------------------- #
C_GOOD = "#3fb950"
C_WARN = "#d29922"
C_BAD = "#f85149"
C_DOWN = "#39c5cf"   # download / receive
C_UP = "#bc8cff"     # upload / send
C_ACCENT = "#58a6ff"
C_DIM = "grey50"
C_BORDER = "grey35"
C_TRACK = "grey27"

WAVE = " ▁▂▃▄▅▆▇█"
BLOCKS = "▁▂▃▄▅▆▇█"
EIGHTHS = " ▏▎▍▌▋▊▉█"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SCALES = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)  # Mbit/s


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #
def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def run(cmd: List[str], timeout: float = 4.0) -> str:
    """Runs a command and returns its stdout ('' on failure). Output is not localized."""
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return res.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def nice_ceil(x: float) -> float:
    """Rounds up to 1, 2, 2.5, 5 x 10^k ('clean' axis scales)."""
    if x <= 0:
        return 1.0
    base = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * base:
            return m * base
    return 10 * base


def fmt_rate(bytes_per_s: float, short: bool = False) -> str:
    bits = bytes_per_s * 8
    if bits >= 1e9:
        s = f"{bits / 1e9:.2f} Gbit/s"
    elif bits >= 1e6:
        s = f"{bits / 1e6:.1f} Mbit/s"
    elif bits >= 1e3:
        s = f"{bits / 1e3:.0f} kbit/s"
    else:
        s = f"{bits:.0f} bit/s"
    return s.replace("bit/s", "b/s") if short else s


def fmt_axis_rate(bytes_per_s: float) -> str:
    bits = bytes_per_s * 8
    if bits >= 1e9:
        return f"{bits / 1e9:.3g}G"
    if bits >= 1e6:
        return f"{bits / 1e6:.3g}M"
    if bits >= 1e3:
        return f"{bits / 1e3:.3g}k"
    return f"{bits:.0f}"


def fmt_ms(v: float) -> str:
    return f"{v:.0f}"


def lat_color(ms: float) -> str:
    return C_GOOD if ms < 60 else C_WARN if ms < 150 else C_BAD


def pick_scale(mbps: float) -> float:
    for s in SCALES:
        if mbps <= s * 0.95:
            return float(s)
    return float(SCALES[-1])


# --------------------------------------------------------------------------- #
# Character graphics: braille chart, gauge, sparkline
# --------------------------------------------------------------------------- #
_DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][column]
_PAD = object()  # "no data yet" (different from None = lost sample)


def braille_chart(samples, width: int, height: int, vmax: float, color_fn,
                  vmin: float = 0.0, fill: bool = False,
                  loss_style: Optional[str] = None) -> List[Text]:
    """Line chart drawn with braille characters: every cell holds 2x4 dots.

    samples: values from oldest to newest; None = lost sample.
    Returns `height` Text objects, one per row.
    """
    px_w, px_h = width * 2, height * 4
    data = list(samples)[-px_w:]
    cols = [_PAD] * (px_w - len(data)) + data
    bits = [[0] * width for _ in range(height)]
    style: List[List[Optional[str]]] = [[None] * width for _ in range(height)]
    span = (vmax - vmin) or 1.0

    def put(x: int, y: int, st: Optional[str]) -> None:
        cy, dy = divmod(y, 4)
        cx, dx = divmod(x, 2)
        bits[cy][cx] |= _DOTS[dy][dx]
        style[cy][cx] = st

    def column(x: int, y0: int, y1: int, st: Optional[str]) -> None:
        lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
        for y in range(lo, hi + 1):
            put(x, y, st)

    prev_y: Optional[int] = None
    prev_st: Optional[str] = None
    for x, v in enumerate(cols):
        if v is _PAD:
            continue
        if v is None:  # lost sample: marker at the bottom, line interrupted
            prev_y = None
            if loss_style:
                column(x, px_h - 3, px_h - 1, loss_style)
            continue
        frac = (clamp(v, vmin, vmax) - vmin) / span
        y = int(round((1 - frac) * (px_h - 1)))
        st = color_fn(v)
        if prev_y is None:
            column(x, y, y, st)
        else:  # join halfway so the line stays continuous
            mid = (prev_y + y) // 2
            column(x - 1, prev_y, mid, prev_st)
            column(x, mid, y, st)
        if fill:
            column(x, y, px_h - 1, st)
        prev_y, prev_st = y, st

    lines: List[Text] = []
    for r in range(height):
        t = Text(no_wrap=True)
        run_txt, run_style = "", None
        for c in range(width):
            b = bits[r][c]
            ch = chr(0x2800 + b) if b else " "
            st = style[r][c] if b else None
            if st == run_style:
                run_txt += ch
            else:
                if run_txt:
                    t.append(run_txt, run_style)
                run_txt, run_style = ch, st
        if run_txt:
            t.append(run_txt, run_style)
        lines.append(t)
    return lines


AXIS_W = 6


def chart_block(samples, width: int, height: int, vmax: float, color_fn, fmt,
                vmin: float = 0.0, fill: bool = False,
                loss_style: Optional[str] = None) -> List[Text]:
    """Braille chart with a Y axis (min/max) on the left."""
    inner = max(4, width - AXIS_W - 1)
    rows = braille_chart(samples, inner, height, vmax, color_fn, vmin, fill, loss_style)
    out = []
    for i, row in enumerate(rows):
        label = fmt(vmax) if i == 0 else fmt(vmin) if i == height - 1 else ""
        line = Text(label.rjust(AXIS_W) + "│", style=C_DIM, no_wrap=True)
        line.append_text(row)
        out.append(line)
    return out


def visible_samples(width: int) -> int:
    """How many samples fit in a chart `width` columns wide."""
    return max(4, width - AXIS_W - 1) * 2


def gauge(frac: float, width: int, color: str) -> Text:
    """Progress bar with 1/8-cell resolution."""
    frac = clamp(frac, 0.0, 1.0)
    cells = frac * width
    full = int(cells)
    part = int((cells - full) * 8)
    t = Text(no_wrap=True)
    t.append("█" * full, style=color)
    used = full
    if full < width and part:
        t.append(EIGHTHS[part], style=color)
        used += 1
    t.append("░" * (width - used), style=C_TRACK)
    return t


def sparkline(values: List[float], width: int, vmax: float, color: str) -> Text:
    vals = values[-width:]
    chars = "".join(BLOCKS[int(clamp(v / vmax, 0, 1) * (len(BLOCKS) - 1))] for v in vals)
    return Text(chars.rjust(width), style=color, no_wrap=True)


def two_col(left: Text, right: Text, width: int, keep: str = "left") -> Text:
    """`left` flush left and `right` flush right on one line.

    When both do not fit, `keep` decides which one survives: "left" drops the right
    part, "right" shortens the left part with an ellipsis.
    """
    t = left.copy()
    t.no_wrap = True
    t.overflow = "ellipsis"
    room = width - cell_len(right.plain)
    if keep == "right" and cell_len(t.plain) > room - 2:
        t.truncate(max(1, room - 2), overflow="ellipsis")
    pad = width - cell_len(t.plain) - cell_len(right.plain)
    if pad >= 2:
        t.append(" " * pad)
        t.append_text(right)
    return t


class Ease:
    """A value that smoothly approaches its target (used to animate the gauges)."""

    def __init__(self, v: float = 0.0):
        self.v = v

    def step(self, target: float, k: float) -> float:
        self.v += (target - self.v) * k
        if abs(target - self.v) < 1e-3:
            self.v = target
        return self.v


class Dynamic:
    """Renderable that receives the real (width, height) only at render time."""

    def __init__(self, fn):
        self.fn = fn

    def __rich_console__(self, console, options):
        h = options.height if options.height is not None else 12
        yield self.fn(options.max_width, h)


# --------------------------------------------------------------------------- #
# State shared between threads
# --------------------------------------------------------------------------- #
class Series:
    """Thread-safe time series with a bounded length."""

    def __init__(self, maxlen: int = 1200):
        self._d: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, v) -> None:
        with self._lock:
            self._d.append(v)

    def snapshot(self) -> list:
        with self._lock:
            return list(self._d)


@dataclass
class Iface:
    name: str
    kind: str            # wifi | ethernet | loopback | virtual | other
    label: str
    up: bool
    ipv4: str = ""
    netmask: str = ""
    mac: str = ""
    speed: int = 0       # Mbit/s reported by the driver (0 = unknown)
    ssid: Optional[str] = None
    signal: Optional[int] = None  # percentage
    is_default: bool = False


@dataclass
class BtDevice:
    name: str
    address: str = ""
    connected: bool = False
    kind: str = ""
    battery: Optional[int] = None


@dataclass
class BtInfo:
    available: bool
    powered: bool = False
    name: str = ""
    address: str = ""
    note: str = ""
    devices: List[BtDevice] = field(default_factory=list)


class SpeedState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.phase = "idle"  # idle | ping | download | upload | done | error
        self.ping_ms: Optional[float] = None
        self.jitter_ms: Optional[float] = None
        self.down_mbps: Optional[float] = None
        self.up_mbps: Optional[float] = None
        self.live_mbps = 0.0
        self.progress = 0.0
        self.down_samples: List[float] = []
        self.up_samples: List[float] = []
        self.error: Optional[str] = None
        self.finished_at: Optional[float] = None
        self.server = ""

    @property
    def running(self) -> bool:
        return self.phase in ("ping", "download", "upload")


@dataclass
class Target:
    """One latency probe target: the router, the Internet or a far host."""
    key: str                      # router | internet | far
    name: str
    host: str = ""
    label: str = ""               # friendly extra, e.g. the city of the far host
    ports: Tuple[int, ...] = (443,)
    timeout: float = 2.0
    accept_refused: bool = False  # count a refused connection (RST) as a reply (router only)
    series: Series = field(default_factory=Series)
    last_ts: float = 0.0          # when the last sample was taken
    ever_ok: bool = False         # has this target ever answered?


@dataclass
class LanDevice:
    ip: str
    mac: str
    name: str = ""
    vendor: str = ""
    random_mac: bool = False      # locally administered (randomized/private) address
    first_seen: float = 0.0
    last_seen: float = 0.0
    online: bool = True
    is_self: bool = False
    is_gateway: bool = False
    new: bool = False             # appeared after the first scan of this session


class LanState:
    def __init__(self) -> None:
        self.devices: List[LanDevice] = []
        self.scanning = False
        self.progress = 0.0
        self.network = ""
        self.iface = ""
        self.last_scan: Optional[float] = None
        self.error: Optional[str] = None
        self.oui_loaded = False


@dataclass
class BleDevice:
    address: str
    name: str = ""
    manufacturer: str = ""
    rssi: float = -100.0          # smoothed signal strength in dBm
    history: List[int] = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0
    adv_count: int = 0


class BleState:
    def __init__(self) -> None:
        self.devices: Dict[str, BleDevice] = {}
        self.lock = threading.Lock()
        self.installed = importlib.util.find_spec("bleak") is not None
        self.scanning = False
        self.requested = False        # the user started a scan (key b) and has not stopped it
        self.ever_scanned = False
        self.error: Optional[str] = None


class State:
    def __init__(self, host: str, port: int, show_all: bool = False,
                 far_host: Optional[str] = None, far_port: int = 443,
                 gateway: Optional[str] = None) -> None:
        self.targets: List[Target] = [
            Target("router", "Router", ports=(80, 443, 53), timeout=0.4, accept_refused=True),
            Target("internet", "Internet", host=host, ports=(port,)),
            Target("far", "Far", host=far_host or "", ports=(far_port,)),
        ]
        self.gateway: Optional[str] = gateway
        self.rx = Series()
        self.tx = Series()
        self.rates: Dict[str, Tuple[float, float]] = {}
        self.ifaces: List[Iface] = []
        self.main_iface: Optional[str] = None
        self.bt: Optional[BtInfo] = None
        self.speed = SpeedState()
        self.lan = LanState()
        self.ble = BleState()
        self.show_all = show_all
        self.errors: Dict[str, str] = {}

    def target(self, key: str) -> Target:
        return next(t for t in self.targets if t.key == key)

    def online(self) -> Optional[bool]:
        last = self.target("internet").series.snapshot()[-4:]
        if not last:
            return None
        return any(v is not None for v in last)


# --------------------------------------------------------------------------- #
# Background monitors: latency and traffic
# --------------------------------------------------------------------------- #
def tcp_probe(addr: str, port: int, timeout: float, accept_refused: bool = False) -> Optional[float]:
    """TCP connect time in ms, or None when there is no answer.

    With accept_refused, a refused connection (RST) also counts as a reply: it proves
    the host is reachable and gives a valid round trip, which is what makes a router
    with closed ports measurable. For remote hosts it must stay off, because a RST
    there usually comes from a firewall or captive portal, not from the host.
    """
    t0 = time.perf_counter()
    try:
        with socket.create_connection((addr, port), timeout=timeout):
            pass
    except ConnectionRefusedError:
        if not accept_refused:
            return None
    except OSError:
        return None
    return (time.perf_counter() - t0) * 1000.0


# Public ping hosts, each in a different city/continent: the farthest one that answers
# becomes the "far" target (override it with --far-host).
FAR_CANDIDATES = (
    ("Sydney", "syd-au-ping.vultr.com"),
    ("Tokyo", "hnd-jp-ping.vultr.com"),
    ("Sao Paulo", "sao-br-ping.vultr.com"),
    ("Johannesburg", "jnb-za-ping.vultr.com"),
    ("Los Angeles", "lax-ca-us-ping.vultr.com"),
    ("Frankfurt", "fra-de-ping.vultr.com"),
)


def pick_far_host(candidates=FAR_CANDIDATES, probe=tcp_probe, cap: float = 450.0,
                  rounds: int = 3) -> Optional[Tuple[str, str]]:
    """Returns (label, host) of the farthest reachable candidate, or None when offline.

    "Farthest" = highest median connect time below `cap` ms (beyond that the path is
    too poor to be a useful reference).
    """
    medians: Dict[Tuple[str, str], float] = {}

    def work(label: str, host: str) -> None:
        try:
            addr = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)[0][4][0]
        except OSError:
            return
        times = [ms for ms in (probe(addr, 443, 2.0) for _ in range(rounds)) if ms is not None]
        if len(times) >= 2:
            medians[(label, host)] = statistics.median(times)

    threads = [threading.Thread(target=work, args=c, daemon=True) for c in candidates]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 8.0
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    if not medians:
        return None
    usable = {k: v for k, v in medians.items() if v <= cap}
    if usable:
        return max(usable.items(), key=lambda kv: kv[1])[0]
    return min(medians.items(), key=lambda kv: kv[1])[0]  # all very slow: take the closest


class TargetMonitor(threading.Thread):
    """Samples one latency target at a fixed interval (TCP connect time)."""

    def __init__(self, state: State, target: Target, interval: float, stop: threading.Event):
        super().__init__(daemon=True)
        self.state, self.target, self.interval, self.stop_ev = state, target, interval, stop
        self._resolved = ("", "")   # (host, address)
        self._port_idx = 0

    def _host(self) -> str:
        return (self.state.gateway or "") if self.target.key == "router" else self.target.host

    def _resolve(self, host: str) -> Optional[str]:
        if self._resolved[0] == host and self._resolved[1]:
            return self._resolved[1]
        try:  # resolved once: DNS time stays out of the measurement
            addr = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)[0][4][0]
        except OSError:
            return None
        self._resolved = (host, addr)
        return addr

    def _probe(self, host: str) -> Optional[float]:
        addr = self._resolve(host)
        if addr is None:
            return None
        ports = self.target.ports
        for k in range(len(ports)):  # start from the port that worked last time
            idx = (self._port_idx + k) % len(ports)
            ms = tcp_probe(addr, ports[idx], self.target.timeout, self.target.accept_refused)
            if ms is not None:
                self._port_idx = idx
                return ms
        self._resolved = ("", "")    # force a fresh DNS lookup next time
        return None

    def run(self) -> None:
        t = self.target
        while not self.stop_ev.is_set():
            if t.key == "far" and not t.host:
                picked = pick_far_host()
                if picked is None:
                    self.stop_ev.wait(5.0)
                    continue
                t.label, t.host = picked
            host = self._host()
            if not host:  # no default gateway known (yet): nothing to measure
                self.stop_ev.wait(0.5)
                continue
            t0 = time.perf_counter()
            ms = self._probe(host)
            t.series.add(ms)
            t.last_ts = time.time()
            if ms is not None:
                t.ever_ok = True
            self.stop_ev.wait(max(0.05, self.interval - (time.perf_counter() - t0)))


def window_stats(samples: list, n: int = 20) -> Optional[Tuple[float, Optional[float]]]:
    """(loss %, median ms) over the last n samples; None when there is too little data."""
    recent = samples[-n:]
    if len(recent) < 5:
        return None
    ok = [v for v in recent if v is not None]
    loss = 100.0 * (len(recent) - len(ok)) / len(recent)
    return loss, (statistics.median(ok) if ok else None)


def far_baseline(samples: list) -> Optional[float]:
    """Typical (20th percentile) latency of a target, learned from its own history."""
    ok = sorted(v for v in samples if v is not None)
    return ok[len(ok) // 5] if len(ok) >= 40 else None


def target_is_bad(key: str, stats: Tuple[float, Optional[float]], baseline: Optional[float] = None) -> bool:
    loss, med = stats
    if loss >= 25 or med is None:
        return True
    if key == "router":
        return med > 50
    if key == "internet":
        return med > 150
    return baseline is not None and med > baseline * 2 + 30


def latency_color_for(key: str, ms: float, baseline: Optional[float] = None) -> str:
    if key == "router":
        good, warn = 15.0, 50.0
    elif key == "internet":
        good, warn = 60.0, 150.0
    elif baseline is None:
        return C_ACCENT
    else:
        good, warn = baseline * 1.5 + 15, baseline * 2 + 30
    return C_GOOD if ms < good else C_WARN if ms < warn else C_BAD


def diagnose(state: State, now: Optional[float] = None) -> Tuple[str, str, str]:
    """Where is the problem? Returns (level, short message, long message).

    level is one of: ok | warn | bad | wait. The logic walks outwards hop by hop:
    router (Wi-Fi / LAN) -> nearby Internet -> far host.
    """
    now = time.time() if now is None else now
    stats: Dict[str, Optional[Tuple[float, Optional[float]]]] = {}
    base: Optional[float] = None
    for t in state.targets:
        samples = t.series.snapshot()
        fresh = bool(t.last_ts) and now - t.last_ts < 6.0
        stats[t.key] = window_stats(samples) if fresh else None
        if t.key == "far":
            base = far_baseline(samples)
    router = state.target("router")
    router_blind = not router.ever_ok and len(router.series.snapshot()) >= 10
    r, i, f = stats["router"], stats["internet"], stats["far"]
    if router_blind:
        r = None   # the router never answers TCP probes: leave it out of the diagnosis
    if i is None:
        return "wait", "Collecting data…", "Collecting data…"
    if r is not None and target_is_bad("router", r):
        return ("bad", "Local network issue (Wi-Fi/router)",
                "Local network issue: the router answers poorly (Wi-Fi, cable or the router itself)")
    if target_is_bad("internet", i):
        if r is not None:
            return ("bad", "ISP/Internet issue (router OK)",
                    "Provider or Internet issue: the router is fine, the Internet is not")
        if state.gateway is None:
            return "bad", "No network route", "No network route: connect to Wi-Fi or Ethernet"
        return ("bad", "Internet issue (router n/a)",
                "Internet unreachable or slow (the router cannot be measured)")
    if f is not None and target_is_bad("far", f, base):
        return ("warn", "Far host degraded (nearby OK)",
                "Far host degraded: nearby servers are fine (upstream issue or the host itself)")
    return "ok", "Connection healthy", "Connection healthy: router, Internet and far host all respond well"


class TrafficMonitor(threading.Thread):
    """Samples the network counters every 0.5 s and derives bit/s per interface."""

    PERIOD = 0.5

    def __init__(self, state: State, stop: threading.Event):
        super().__init__(daemon=True)
        self.state, self.stop_ev = state, stop

    def run(self) -> None:
        last = psutil.net_io_counters(pernic=True)
        last_t = time.monotonic()
        while not self.stop_ev.wait(self.PERIOD):
            cur = psutil.net_io_counters(pernic=True)
            now = time.monotonic()
            dt = max(now - last_t, 1e-3)
            rates: Dict[str, Tuple[float, float]] = {}
            for name, c in cur.items():
                p = last.get(name)
                if p is not None:
                    rates[name] = (max(0.0, (c.bytes_recv - p.bytes_recv) / dt),
                                   max(0.0, (c.bytes_sent - p.bytes_sent) / dt))
            self.state.rates = rates
            rx, tx = rates.get(self.state.main_iface or "", (0.0, 0.0))
            self.state.rx.add(rx)
            self.state.tx.add(tx)
            last, last_t = cur, now


class Periodic(threading.Thread):
    """Runs fn() every `period` seconds."""

    def __init__(self, name: str, fn, period: float, state: State, stop: threading.Event):
        super().__init__(daemon=True)
        self.label, self.fn, self.period = name, fn, period
        self.state, self.stop_ev = state, stop

    def run(self) -> None:
        while not self.stop_ev.is_set():
            try:
                self.fn()
                self.state.errors.pop(self.label, None)
            except Exception as exc:  # the dashboard must never crash because of a command
                self.state.errors[self.label] = f"{type(exc).__name__}: {exc}"
            self.stop_ev.wait(self.period)


# --------------------------------------------------------------------------- #
# Network interfaces (macOS + Linux)
# --------------------------------------------------------------------------- #
def parse_default_route(text: str) -> Optional[Tuple[str, str]]:
    """(interface, gateway) of the default route in /proc/net/route (lowest metric)."""
    best: Optional[Tuple[int, str, str]] = None
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8 or f[1] != "00000000":
            continue
        try:
            if not int(f[3], 16) & 2:  # RTF_GATEWAY
                continue
            metric = int(f[6])
            gateway = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
        except (ValueError, struct.error):
            continue
        if best is None or metric < best[0]:
            best = (metric, f[0], gateway)
    return (best[1], best[2]) if best else None


def parse_proc_route(text: str) -> Optional[str]:
    route = parse_default_route(text)
    return route[0] if route else None


def parse_route_get(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """`route -n get default` (macOS) -> (interface, gateway or None)."""
    m = re.search(r"interface:\s*(\S+)", text)
    if not m:
        return None
    g = re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", text)
    return m.group(1), (g.group(1) if g else None)


def parse_route_get_default(text: str) -> Optional[str]:
    route = parse_route_get(text)
    return route[0] if route else None


def default_route() -> Tuple[Optional[str], Optional[str]]:
    """(interface, gateway IP) of the default route; (None, None) when there is none."""
    try:
        if IS_LINUX:
            with open("/proc/net/route") as fh:
                return parse_default_route(fh.read()) or (None, None)
        if IS_MAC:
            return parse_route_get(run(["route", "-n", "get", "default"], 3)) or (None, None)
    except OSError:
        pass
    return None, None


def default_interface() -> Optional[str]:
    return default_route()[0]


def parse_hardware_ports(text: str) -> Dict[str, str]:
    """Parses `networksetup -listallhardwareports` -> {device: port name}."""
    ports: Dict[str, str] = {}
    for block in text.split("\n\n"):
        port = re.search(r"Hardware Port:\s*(.+)", block)
        dev = re.search(r"Device:\s*(\S+)", block)
        if port and dev:
            ports[dev.group(1)] = port.group(1).strip()
    return ports


def classify_interface(name: str, ports: Dict[str, str]) -> Tuple[str, str]:
    """Returns (kind, human-readable label)."""
    if name == "lo" or name.startswith("lo") and name[2:].isdigit():
        return "loopback", "Loopback"
    if IS_MAC:
        port = ports.get(name)
        if not port:
            return "virtual", "Virtual"
        low = port.lower()
        if "wi-fi" in low or "airport" in low:
            return "wifi", "Wi-Fi"
        if "bluetooth" in low or "bridge" in low:
            return "virtual", port
        return "ethernet", port
    base = f"/sys/class/net/{name}"
    if os.path.isdir(f"{base}/wireless") or os.path.isdir(f"{base}/phy80211"):
        return "wifi", "Wi-Fi"
    if os.path.realpath(base).startswith("/sys/devices/virtual/"):
        return "virtual", "Virtual"
    if os.path.exists(f"{base}/device"):
        return "ethernet", "Ethernet"
    return "other", "Other"


def parse_nmcli_wifi(text: str, dev: str) -> Tuple[Optional[str], Optional[int]]:
    """`nmcli -t -f ACTIVE,DEVICE,SIGNAL,SSID dev wifi` -> (ssid, signal%)."""
    for line in text.splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4 and parts[0] == "yes" and parts[1] == dev:
            ssid = parts[3].replace("\\:", ":").replace("\\\\", "\\")
            try:
                sig: Optional[int] = int(parts[2])
            except ValueError:
                sig = None
            return ssid or None, sig
    return None, None


def parse_iw_link(text: str) -> Tuple[Optional[str], Optional[int]]:
    ssid = re.search(r"SSID:\s*(.+)", text)
    sig = re.search(r"signal:\s*(-?\d+)\s*dBm", text)
    pct = int(clamp(2 * (int(sig.group(1)) + 100), 0, 100)) if sig else None
    return (ssid.group(1).strip() if ssid else None), pct


def wifi_details(name: str) -> Tuple[Optional[str], Optional[int]]:
    """SSID and signal strength, as far as the system lets us read them."""
    if IS_MAC:
        out = run(["networksetup", "-getairportnetwork", name], 3)
        m = re.search(r"Current (?:Wi-Fi|AirPort) Network:\s*(.+)", out)
        return (m.group(1).strip() if m else None), None
    if shutil.which("nmcli"):
        ssid, sig = parse_nmcli_wifi(
            run(["nmcli", "-t", "-f", "ACTIVE,DEVICE,SIGNAL,SSID", "dev", "wifi"], 3), name)
        if ssid:
            return ssid, sig
    if shutil.which("iw"):
        return parse_iw_link(run(["iw", "dev", name, "link"], 3))
    return None, None


def collect_interfaces(default: Optional[str] = None) -> List[Iface]:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    ports = parse_hardware_ports(run(["networksetup", "-listallhardwareports"], 3)) if IS_MAC else {}
    result: List[Iface] = []
    for name, entries in addrs.items():
        kind, label = classify_interface(name, ports)
        st = stats.get(name)
        v4 = next((a for a in entries if a.family == socket.AF_INET), None)
        iface = Iface(
            name=name, kind=kind, label=label,
            up=bool(st and st.isup),
            ipv4=v4.address if v4 else "",
            netmask=(v4.netmask or "") if v4 else "",
            mac=next((a.address for a in entries if a.family == psutil.AF_LINK), ""),
            speed=int(st.speed) if st and st.speed else 0,
            is_default=(name == default),
        )
        if kind == "wifi" and iface.up:
            iface.ssid, iface.signal = wifi_details(name)
        result.append(iface)
    return result


# --------------------------------------------------------------------------- #
# Bluetooth (macOS: system_profiler, Linux: bluetoothctl)
# --------------------------------------------------------------------------- #
_BT_KINDS = {
    "audio-headset": "Headset", "audio-headphones": "Headphones", "audio-card": "Audio",
    "input-keyboard": "Keyboard", "input-mouse": "Mouse", "input-gaming": "Gamepad",
    "input-tablet": "Tablet", "phone": "Phone", "computer": "Computer",
    "printer": "Printer", "camera-video": "Camera", "video-display": "Display",
}


def _pct(value) -> Optional[int]:
    m = re.search(r"(\d+)", str(value)) if value is not None else None
    return int(m.group(1)) if m else None


def parse_macos_bluetooth(data: dict) -> BtInfo:
    props = data.get("controller_properties") or {}
    powered = props.get("controller_state") == "attrib_on"
    if not props and isinstance(data.get("local_device_title"), dict):  # macOS <= 11
        powered = data["local_device_title"].get("general_power") == "attrib_on"
        props = data["local_device_title"]
    info = BtInfo(True, powered=powered,
                  name=props.get("controller_chipset") or props.get("general_name") or "Controller",
                  address=props.get("controller_address") or props.get("general_address") or "")

    def add(dname: str, d: dict, connected: bool) -> None:
        bats = [_pct(d.get(k)) for k in
                ("device_batteryLevelMain", "device_batteryLevelLeft", "device_batteryLevelRight")]
        bats = [b for b in bats if b is not None]
        info.devices.append(BtDevice(
            name=dname, address=d.get("device_address", ""), connected=connected,
            kind=d.get("device_minorType") or d.get("device_majorType") or "",
            battery=min(bats) if bats else None))

    for key, conn in (("device_connected", True), ("device_not_connected", False)):
        for entry in data.get(key) or []:
            for dname, d in entry.items():
                add(dname, d if isinstance(d, dict) else {}, conn)
    for entry in data.get("device_title") or []:  # macOS <= 11
        for dname, d in entry.items():
            d = d if isinstance(d, dict) else {}
            add(dname, d, str(d.get("device_isconnected", "")).lower().endswith("yes"))
    return info


def bluetooth_macos() -> BtInfo:
    out = run(["system_profiler", "SPBluetoothDataType", "-json"], timeout=15)
    try:
        data = json.loads(out)["SPBluetoothDataType"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return BtInfo(False, note="Bluetooth not available")
    return parse_macos_bluetooth(data)


def parse_bluetoothctl_show(text: str) -> Optional[BtInfo]:
    if not text.strip() or "No default controller" in text:
        return None
    first = re.match(r"\s*Controller\s+([0-9A-Fa-f:]{17})", text)
    kv: Dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            kv.setdefault(k.strip(), v.strip())
    return BtInfo(True, powered=kv.get("Powered", "").lower() == "yes",
                  name=kv.get("Alias") or kv.get("Name") or "Controller",
                  address=first.group(1) if first else "")


def parse_bluetoothctl_info(text: str, address: str, fallback_name: str) -> BtDevice:
    kv: Dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            kv.setdefault(k.strip(), v.strip())
    batt = re.search(r"\((\d+)\)", kv.get("Battery Percentage", ""))
    icon = kv.get("Icon", "")
    return BtDevice(name=kv.get("Alias") or kv.get("Name") or fallback_name, address=address,
                    connected=kv.get("Connected", "").lower() == "yes",
                    kind=_BT_KINDS.get(icon, icon), battery=int(batt.group(1)) if batt else None)


def bluetooth_linux() -> BtInfo:
    if not shutil.which("bluetoothctl"):
        return BtInfo(False, note="bluetoothctl not found (install bluez)")
    info = parse_bluetoothctl_show(run(["bluetoothctl", "show"], 4))
    if info is None:
        return BtInfo(False, note="no Bluetooth adapter available")
    for line in run(["bluetoothctl", "devices"], 4).splitlines()[:25]:
        m = re.match(r"\s*Device\s+([0-9A-Fa-f:]{17})\s*(.*)", line)
        if m:
            addr, name = m.group(1), m.group(2).strip() or m.group(1)
            info.devices.append(parse_bluetoothctl_info(run(["bluetoothctl", "info", addr], 3), addr, name))
    return info


def collect_bluetooth() -> BtInfo:
    if IS_MAC:
        return bluetooth_macos()
    if IS_LINUX:
        return bluetooth_linux()
    return BtInfo(False, note="unsupported platform")


# --------------------------------------------------------------------------- #
# Devices on the local network (ARP sweep + mDNS / reverse-DNS names)
# --------------------------------------------------------------------------- #
class LanScanError(Exception):
    pass


def fmt_age(seconds: float) -> str:
    sec = int(max(0, seconds))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def normalize_mac(mac: str) -> str:
    """'A4:2B:B:1:2:3' -> 'a4:2b:0b:01:02:03' (macOS prints octets without leading zeros)."""
    return ":".join(part.zfill(2) for part in mac.lower().replace("-", ":").split(":"))


def is_random_mac(mac: str) -> bool:
    """Locally administered bit set: a randomized/private address (modern phones and laptops)."""
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except ValueError:
        return False


def _usable_mac(mac: str) -> bool:
    try:
        first = int(mac[:2], 16)
    except ValueError:
        return False
    return mac != "00:00:00:00:00:00" and not first & 0x01  # skip broadcast / multicast


def parse_proc_arp(text: str, iface: Optional[str] = None) -> Dict[str, str]:
    """/proc/net/arp (Linux) -> {ip: mac} for complete entries."""
    out: Dict[str, str] = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 6:
            continue
        ip, flags, mac, dev = f[0], f[2], normalize_mac(f[3]), f[5]
        try:
            complete = int(flags, 16) & 0x2
        except ValueError:
            continue
        if complete and _usable_mac(mac) and (iface is None or dev == iface):
            out[ip] = mac
    return out


_ARP_AN_RE = re.compile(
    r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})\s+on\s+(\S+)")


def parse_arp_an(text: str, iface: Optional[str] = None) -> Dict[str, str]:
    """`arp -an` (macOS) -> {ip: mac}. Incomplete entries do not match the pattern."""
    out: Dict[str, str] = {}
    for m in _ARP_AN_RE.finditer(text):
        ip, mac, dev = m.group(1), normalize_mac(m.group(2)), m.group(3)
        if _usable_mac(mac) and (iface is None or dev == iface):
            out[ip] = mac
    return out


def read_arp_table(iface: str) -> Dict[str, str]:
    """IP -> MAC for the neighbours the OS has already resolved on `iface`."""
    try:
        if IS_LINUX:
            with open("/proc/net/arp") as fh:
                return parse_proc_arp(fh.read(), iface)
        if IS_MAC:
            return parse_arp_an(run(["arp", "-an"], 5), iface)
    except OSError:
        pass
    return {}


def lan_scan_plan(ip: str, netmask: str) -> Tuple[ipaddress.IPv4Network, List[str]]:
    """Which addresses to probe. Refuses anything that is not a private local network."""
    try:
        net = ipaddress.ip_interface(f"{ip}/{netmask}").network
    except ValueError as exc:
        raise LanScanError(f"cannot read the network of {ip}: {exc}")
    if not (net.is_private or net.is_link_local):
        raise LanScanError(f"refusing to scan {net}: it is not a private local network "
                           "(netdash only scans your own LAN)")
    if net.prefixlen < 22:  # very large LAN: only scan the /24 around this machine
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    if net.prefixlen > 30:
        raise LanScanError(f"network {net} is too small to scan")
    return net, [str(h) for h in net.hosts() if str(h) != ip]


def parse_oui(text: str) -> Dict[str, str]:
    """Vendor prefixes from an IEEE oui.txt or an nmap-mac-prefixes file."""
    table: Dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9A-Fa-f]{6})\s+\(base 16\)\s+(\S.*)$", line)
        if not m and not line.startswith("#"):
            m = re.match(r"^([0-9A-Fa-f]{6})\s+(\S.*)$", line)
        if m:
            table[m.group(1).upper()] = m.group(2).strip()
    return table


OUI_FILES = (
    "/usr/share/ieee-data/oui.txt", "/var/lib/ieee-data/oui.txt",
    "/usr/share/nmap/nmap-mac-prefixes", "/usr/local/share/nmap/nmap-mac-prefixes",
    "/opt/homebrew/share/nmap/nmap-mac-prefixes",
)


def load_oui() -> Dict[str, str]:
    """Vendor table from the first file found (Linux: package ieee-data or nmap; macOS: nmap)."""
    for path in OUI_FILES:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                table = parse_oui(fh.read())
        except OSError:
            continue
        if table:
            return table
    return {}


def short_name(host: str) -> str:
    """'Fabios-iPhone.local.' -> 'Fabios-iPhone'; bare IP addresses give ''."""
    host = host.strip().rstrip(".")
    if not host or re.fullmatch(r"[\d.]+", host):
        return ""
    return host.split(".")[0]


def build_mdns_query(ip: str, qid: int = 0) -> bytes:
    """mDNS/DNS PTR question for the reverse name of `ip` (x.x.x.x.in-addr.arpa)."""
    labels = ip.split(".")[::-1] + ["in-addr", "arpa"]
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"
    return struct.pack(">HHHHHH", qid, 0, 1, 0, 0, 0) + qname + struct.pack(">HH", 12, 1)


def _dns_name(data: bytes, pos: int) -> Tuple[str, int]:
    """Decodes a (possibly compressed) DNS name; returns (name, offset after the name)."""
    labels: List[str] = []
    end, jumped, hops = pos, False, 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated name")
        n = data[pos]
        if n == 0:
            if not jumped:
                end = pos + 1
            break
        if (n & 0xC0) == 0xC0:
            if pos + 1 >= len(data):
                raise ValueError("truncated pointer")
            if not jumped:
                end = pos + 2
            jumped = True
            pos = ((n & 0x3F) << 8) | data[pos + 1]
            hops += 1
            if hops > 16:
                raise ValueError("compression loop")
            continue
        labels.append(data[pos + 1:pos + 1 + n].decode("utf-8", "replace"))
        pos += 1 + n
    return ".".join(labels), end


def parse_mdns_ptr(data: bytes) -> Dict[str, str]:
    """Reverse-lookup answers in a DNS/mDNS packet -> {ip: hostname}."""
    out: Dict[str, str] = {}
    try:
        qdcount, ancount = struct.unpack(">HH", data[4:8])
        pos = 12
        for _ in range(qdcount):
            _, pos = _dns_name(data, pos)
            pos += 4
        for _ in range(ancount):
            owner, pos = _dns_name(data, pos)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
            pos += 10
            if rtype == 12:
                target, _ = _dns_name(data, pos)
                m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\.(\d+)\.in-addr\.arpa\.?", owner, re.I)
                if m:
                    out[".".join(reversed(m.groups()))] = target
            pos += rdlen
    except (ValueError, struct.error):
        pass
    return out


def mdns_names(ips: List[str], own_ip: str, wait: float = 1.5) -> Dict[str, str]:
    """Asks the devices for their own mDNS (Bonjour/Avahi) name; {ip: hostname}."""
    names: Dict[str, str] = {}
    if not ips:
        return names
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(own_ip))
        sock.bind((own_ip, 0))  # source port != 5353: devices answer us directly (unicast)
        sock.settimeout(0.2)
        for n, ip in enumerate(ips, 1):
            sock.sendto(build_mdns_query(ip, n), ("224.0.0.251", 5353))
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            for owner_ip, host in parse_mdns_ptr(data).items():
                if owner_ip == addr[0]:  # a device may only name itself
                    names[owner_ip] = host
    except OSError:
        pass
    finally:
        if sock is not None:
            sock.close()
    return names


def reverse_dns(ips: List[str], timeout: float = 3.0) -> Dict[str, str]:
    """Reverse DNS for many addresses in parallel, never waiting longer than `timeout`."""
    found: Dict[str, str] = {}

    def work(ip: str) -> None:
        try:
            host = socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):
            return
        if host and host != ip:
            found[ip] = host

    threads = [threading.Thread(target=work, args=(ip,), daemon=True) for ip in ips[:64]]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return dict(found)


def send_sweep(hosts: List[str], own_ip: str, delay: float = 0.002, on_progress=None) -> None:
    """Sends one tiny UDP datagram to each host so the OS resolves its MAC address via ARP.

    Nothing else is sent: this is not a port scan, and the payload is a single zero byte.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((own_ip, 0))
        sock.settimeout(0.2)
        for n, ip in enumerate(hosts, 1):
            try:
                sock.sendto(b"\x00", (ip, 9))  # UDP "discard" port
            except OSError:
                pass
            if delay:
                time.sleep(delay)
            if on_progress and n % 8 == 0:
                on_progress(n / len(hosts))
    finally:
        sock.close()


class LanScanner:
    """Finds the devices on the local network.

    Only the machine's own private subnet is scanned, by making the OS resolve each
    address with ARP and then reading its neighbour table. There is no port scanning.
    """

    def __init__(self, state: State) -> None:
        self.state = state
        self._oui: Optional[Dict[str, str]] = None
        self._names: Dict[str, Tuple[str, float]] = {}   # ip -> (name, when it was resolved)

    def start(self) -> None:
        lan = self.state.lan
        if lan.scanning:
            return
        lan.scanning, lan.progress, lan.error = True, 0.0, None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        lan = self.state.lan
        try:
            self._scan(lan)
        except LanScanError as exc:
            lan.error = str(exc)
        except Exception as exc:  # never let a scan crash the dashboard
            lan.error = f"{type(exc).__name__}: {exc}"
        finally:
            lan.last_scan, lan.progress, lan.scanning = time.time(), 1.0, False

    def _pick_interface(self) -> Iface:
        s = self.state
        cands = [i for i in s.ifaces if i.kind in ("wifi", "ethernet", "other") and i.up and i.ipv4]
        main = next((i for i in cands if i.name == s.main_iface), None)
        if main is not None:
            return main
        if cands:
            return cands[0]
        raise LanScanError("no active Wi-Fi/Ethernet interface with an IPv4 address")

    def _wait(self, lan: LanState, seconds: float, lo: float, hi: float) -> None:
        steps = max(1, int(seconds / 0.1))
        for n in range(steps):
            lan.progress = lo + (hi - lo) * (n + 1) / steps
            time.sleep(seconds / steps)

    def _scan(self, lan: LanState) -> None:
        iface = self._pick_interface()
        net, hosts = lan_scan_plan(iface.ipv4, iface.netmask or "255.255.255.0")
        own_ip = iface.ipv4
        lan.network, lan.iface = str(net), iface.name

        found: Dict[str, str] = {}

        def collect() -> None:
            for ip, mac in read_arp_table(iface.name).items():
                if ip != own_ip and ipaddress.ip_address(ip) in net:
                    found[ip] = mac

        # Two passes: the first ARP burst can miss devices that are dozing (phones, IoT).
        for lo, hi in ((0.0, 0.3), (0.45, 0.75)):
            send_sweep(hosts, own_ip, 0.002, lambda f, lo=lo, hi=hi: setattr(lan, "progress", lo + (hi - lo) * f))
            self._wait(lan, 1.2, hi, hi + 0.12)
            collect()

        lan.progress = 0.88
        now = time.time()
        wanted = [ip for ip in found if self._needs_name(ip, now)]
        resolved: Dict[str, str] = {}
        if wanted:
            rdns: Dict[str, str] = {}
            worker = threading.Thread(target=lambda: rdns.update(reverse_dns(wanted, 3.0)), daemon=True)
            worker.start()
            mdns = mdns_names(wanted, own_ip, 1.5)
            worker.join(3.5)
            resolved = {**rdns, **mdns}   # the device's own mDNS name wins
            for ip in wanted:
                self._names[ip] = (short_name(resolved.get(ip, "")), now)

        if self._oui is None:
            self._oui = load_oui()
        lan.oui_loaded = bool(self._oui)
        self._merge(lan, iface, found, now)

    def _needs_name(self, ip: str, now: float) -> bool:
        cached = self._names.get(ip)
        return cached is None or (not cached[0] and now - cached[1] > 60)

    def _merge(self, lan: LanState, iface: Iface, found: Dict[str, str], now: float) -> None:
        oui = self._oui or {}
        prev = {(d.mac or d.ip): d for d in lan.devices}
        first_scan = not lan.devices          # on the first scan nothing counts as "new"
        own_mac = normalize_mac(iface.mac) if iface.mac else ""
        entries = [(iface.ipv4, own_mac, True)] + [(ip, mac, False) for ip, mac in found.items()]
        devices: List[LanDevice] = []
        seen = set()
        for ip, mac, is_self in entries:
            key = mac or ip
            seen.add(key)
            d = prev.get(key) or LanDevice(ip=ip, mac=mac, first_seen=now, new=not first_scan)
            d.ip, d.last_seen, d.online, d.is_self = ip, now, True, is_self
            d.is_gateway = ip == self.state.gateway
            d.random_mac = is_random_mac(mac) if mac else False
            d.vendor = oui.get(mac.replace(":", "")[:6].upper(), "") if mac else ""
            name = short_name(socket.gethostname()) if is_self else self._names.get(ip, ("", 0.0))[0]
            if name:
                d.name = name
            devices.append(d)
        for key, d in prev.items():           # devices that did not answer this time
            if key not in seen:
                d.online = False
                devices.append(d)
        devices.sort(key=lambda d: ipaddress.ip_address(d.ip))
        lan.devices = devices


# --------------------------------------------------------------------------- #
# Nearby Bluetooth Low Energy devices (via the optional `bleak` package)
# --------------------------------------------------------------------------- #
# Bluetooth SIG company identifiers (only the ones that are certain).
COMPANY_IDS = {
    0x0006: "Microsoft", 0x004C: "Apple", 0x0075: "Samsung", 0x0087: "Garmin",
    0x009E: "Bose", 0x00E0: "Google", 0x012D: "Sony", 0x0171: "Amazon",
    0x01DA: "Logitech", 0x027D: "Huawei", 0x038F: "Xiaomi",
}


BLE_MIN_RSSI = -80   # dBm: weaker devices are not listed (they are too far to be "nearby")


def signal_level(rssi: float) -> int:
    """RSSI (dBm) -> 0..4 bars."""
    if rssi >= -55:
        return 4
    if rssi >= -67:
        return 3
    if rssi >= -78:
        return 2
    if rssi >= -90:
        return 1
    return 0


def proximity_label(rssi: float) -> str:
    """Coarse hint only: RSSI depends on the transmit power, walls, bodies, antennas..."""
    return ("very far", "far", "nearby", "close", "very close")[signal_level(rssi)]


def looks_like_address(name: str) -> bool:
    return bool(re.fullmatch(r"([0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}", name))


def describe_ble_error(exc: BaseException) -> str:
    msg = str(exc) or type(exc).__name__
    low = msg.lower()
    if "not authorized" in low or "permission" in low or "denied" in low:
        return msg + " (macOS: allow your terminal under System Settings > Privacy & Security > Bluetooth)"
    if "adapter" in low and ("no " in low or "not found" in low):
        return "No Bluetooth adapter found"
    if "turned off" in low or "powered off" in low or "not powered" in low:
        return "Bluetooth is turned off"
    return msg


class BleScanner(threading.Thread):
    """Scans for BLE advertisements, and only while the user has asked for it.

    The scan is started and stopped with start_scan() / stop_scan() (key b); the UI also
    stops it when you leave the page. If it fails it stops: there are no silent retries.
    """

    STALE_AFTER = 90.0  # while scanning, forget devices that have not been heard for this long

    def __init__(self, state: State, stop: threading.Event):
        super().__init__(daemon=True)
        self.state, self.stop_ev = state, stop
        self.active = threading.Event()
        self.bleak = None  # the bleak module (replaceable in tests)

    # -- controls (called from the UI thread) ---------------------------------------- #
    def start_scan(self) -> None:
        ble = self.state.ble
        with ble.lock:
            ble.devices.clear()      # every scan starts from an empty list
        if self.is_alive():          # keep the "bleak is missing" message of a dead thread
            ble.error = None
        ble.ever_scanned, ble.requested = True, True
        self.active.set()

    def stop_scan(self) -> None:
        self.state.ble.requested = False
        self.active.clear()

    def toggle(self) -> None:
        if self.active.is_set():
            self.stop_scan()
        else:
            self.start_scan()

    # -- scanner thread ------------------------------------------------------------------ #
    def run(self) -> None:
        ble = self.state.ble
        if self.bleak is None:
            try:
                import bleak  # type: ignore
            except ImportError:
                ble.installed = False
                ble.error = "Bluetooth scanning needs the 'bleak' package:  python3 -m pip install bleak"
                return
            self.bleak = bleak
        asyncio.run(self._main())

    def _on_adv(self, device, adv) -> None:
        now = time.time()
        ble = self.state.ble
        with ble.lock:
            d = ble.devices.get(device.address)
            if d is None:
                d = ble.devices[device.address] = BleDevice(address=device.address, first_seen=now)
            name = (getattr(adv, "local_name", None) or getattr(device, "name", None) or "").strip()
            if name and not looks_like_address(name):
                d.name = name
            if adv.manufacturer_data and not d.manufacturer:
                cid = next(iter(adv.manufacturer_data))
                d.manufacturer = COMPANY_IDS.get(cid, f"0x{cid:04X}")
            rssi = float(adv.rssi)
            d.rssi = rssi if d.adv_count == 0 else d.rssi * 0.6 + rssi * 0.4   # smooth the jitter
            d.history.append(int(rssi))
            del d.history[:-40]
            d.last_seen = now
            d.adv_count += 1

    def _prune(self) -> None:
        now = time.time()
        ble = self.state.ble
        with ble.lock:
            for addr in [a for a, d in ble.devices.items() if now - d.last_seen > self.STALE_AFTER]:
                del ble.devices[addr]

    async def _main(self) -> None:
        ble = self.state.ble
        scanner = None
        try:
            while not self.stop_ev.is_set():
                want = self.active.is_set()
                try:
                    if want and scanner is None:
                        scanner = self.bleak.BleakScanner(detection_callback=self._on_adv)
                        await scanner.start()
                        ble.scanning, ble.error = True, None
                    elif not want and scanner is not None:
                        await scanner.stop()
                        scanner, ble.scanning = None, False
                except Exception as exc:
                    scanner, ble.scanning = None, False
                    ble.error = describe_ble_error(exc)
                    self.stop_scan()   # no silent retries: press b to try again
                if scanner is not None:  # a stopped scan keeps its results on screen
                    self._prune()
                await asyncio.sleep(0.2)
        finally:
            ble.scanning = False
            if scanner is not None:
                try:
                    await scanner.stop()
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# Speed test (HTTP against Cloudflare's public endpoints, standard library only)
# --------------------------------------------------------------------------- #
class SpeedTestError(Exception):
    pass


def make_ssl_context() -> ssl.SSLContext:
    try:  # certifi, if installed, avoids the certificate problems of python.org builds on macOS
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return ("SSL certificate could not be verified (macOS: run 'Install Certificates.command' "
                "or 'pip install certifi')")
    if isinstance(exc, socket.gaierror):
        return "server unreachable (DNS lookup failed): are you offline?"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "connection timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    return str(exc) or type(exc).__name__


class _Counter:
    def __init__(self) -> None:
        self.value = 0
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        with self._lock:
            self.value += n


class SpeedTest:
    HEADERS = {"User-Agent": "Mozilla/5.0 (netdash)", "Accept": "*/*"}
    DOWN_CHUNK = 25_000_000   # bytes requested per GET
    UP_MAX_REQ = 50_000_000   # max bytes per single upload POST
    UP_SNDBUF = 256 * 1024    # small send buffer: counted bytes are the ones actually in flight
    WARMUP = 1.0              # seconds discarded from the final calculation (TCP slow start)

    def __init__(self, state: SpeedState, url: str, duration: float, streams: int, max_mb: int):
        u = urlparse(url if "://" in url else "https://" + url)
        self.state = state
        self.scheme = u.scheme
        self.host = u.hostname or ""
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.prefix = u.path.rstrip("/")
        self.duration = max(2.0, duration)
        self.streams = max(1, streams)
        self.max_bytes = max(1, max_mb) * 1_000_000
        # One long POST per connection: no end-of-request pauses that would skew the measurement.
        self.up_req_bytes = min(self.UP_MAX_REQ, max(1_000_000, self.max_bytes // self.streams))
        self._ctx: Optional[ssl.SSLContext] = None

    # -- public API ----------------------------------------------------- #
    def start(self) -> None:
        if self.state.running:
            return
        self.state.reset()
        self.state.phase = "ping"
        self.state.server = self.host
        threading.Thread(target=self._run, daemon=True).start()

    # -- internals ------------------------------------------------------ #
    def _conn(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            if self._ctx is None:
                self._ctx = make_ssl_context()
            return http.client.HTTPSConnection(self.host, self.port, timeout=10, context=self._ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=10)

    def _run(self) -> None:
        s = self.state
        try:
            self._latency()
            self._transfer("download")
            self._transfer("upload")
            s.phase, s.finished_at = "done", time.time()
        except Exception as exc:
            s.phase, s.error = "error", describe_error(exc)

    def _latency(self, rounds: int = 8) -> None:
        s = self.state
        conn = self._conn()
        times: List[float] = []
        try:
            for i in range(rounds + 1):
                t0 = time.perf_counter()
                conn.request("GET", f"{self.prefix}/__down?bytes=0", headers=self.HEADERS)
                resp = conn.getresponse()
                resp.read()
                if resp.status != 200:
                    raise SpeedTestError(f"server replied HTTP {resp.status}")
                if i:  # the first request includes the TCP/TLS handshake: discard it
                    times.append((time.perf_counter() - t0) * 1000.0)
                s.progress = (i + 1) / (rounds + 1)
        finally:
            conn.close()
        s.ping_ms = statistics.median(times)
        s.jitter_ms = (sum(abs(a - b) for a, b in zip(times, times[1:])) / (len(times) - 1)
                       if len(times) > 1 else 0.0)

    def _download_worker(self, counter: _Counter, stop: threading.Event, errors: list) -> None:
        conn = None
        try:
            conn = self._conn()
            while not stop.is_set():
                conn.request("GET", f"{self.prefix}/__down?bytes={self.DOWN_CHUNK}", headers=self.HEADERS)
                resp = conn.getresponse()
                if resp.status != 200:
                    raise SpeedTestError(f"server replied HTTP {resp.status}")
                while True:
                    if stop.is_set():
                        return
                    data = resp.read(1 << 17)
                    if not data:
                        break
                    counter.add(len(data))
        except Exception as exc:
            if not stop.is_set():
                errors.append(exc)
        finally:
            if conn is not None:
                conn.close()

    def _upload_worker(self, counter: _Counter, stop: threading.Event, errors: list) -> None:
        conn = None
        chunk = os.urandom(1 << 16)
        try:
            conn = self._conn()
            conn.connect()
            try:
                conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.UP_SNDBUF)
            except OSError:
                pass
            while not stop.is_set():
                conn.putrequest("POST", f"{self.prefix}/__up")
                conn.putheader("Content-Length", str(self.up_req_bytes))
                conn.putheader("Content-Type", "application/octet-stream")
                for k, v in self.HEADERS.items():
                    conn.putheader(k, v)
                conn.endheaders()
                sent = 0
                while sent < self.up_req_bytes:
                    if stop.is_set():
                        return
                    n = min(len(chunk), self.up_req_bytes - sent)
                    conn.send(chunk[:n])
                    counter.add(n)
                    sent += n
                resp = conn.getresponse()
                resp.read()
                if resp.status >= 400:
                    raise SpeedTestError(f"server replied HTTP {resp.status}")
        except Exception as exc:
            if not stop.is_set():
                errors.append(exc)
        finally:
            if conn is not None:
                conn.close()

    def _transfer(self, direction: str) -> None:
        s = self.state
        down = direction == "download"
        samples: List[float] = []
        if down:
            s.down_samples = samples
        else:
            s.up_samples = samples
        s.phase, s.progress, s.live_mbps = direction, 0.0, 0.0

        counter, stop, errors = _Counter(), threading.Event(), []
        target = self._download_worker if down else self._upload_worker
        threads = [threading.Thread(target=target, args=(counter, stop, errors), daemon=True)
                   for _ in range(self.streams)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()

        window: deque = deque()
        mark: Optional[Tuple[float, int]] = None
        last_sample = 0.0
        while True:
            el = time.perf_counter() - t0
            total = counter.value
            window.append((el, total))
            while window and el - window[0][0] > 0.6:
                window.popleft()
            if mark is None and el >= self.WARMUP:
                mark = (el, total)
            if len(window) > 1 and window[-1][0] > window[0][0]:
                dt = window[-1][0] - window[0][0]
                s.live_mbps = (window[-1][1] - window[0][1]) * 8 / dt / 1e6
            if el - last_sample >= 0.2:
                samples.append(s.live_mbps)
                last_sample = el
            s.progress = min(1.0, max(el / self.duration, total / self.max_bytes))
            if el >= self.duration or total >= self.max_bytes:
                break
            if not any(t.is_alive() for t in threads):
                break
            time.sleep(0.05)

        stop.set()
        el = time.perf_counter() - t0
        total = counter.value
        if total == 0:
            raise errors[0] if errors else SpeedTestError("no data transferred")
        if mark is not None and el - mark[0] > 0.5:
            mbps = (total - mark[1]) * 8 / (el - mark[0]) / 1e6
        else:
            mbps = total * 8 / max(el, 1e-6) / 1e6
        s.progress = 1.0
        s.live_mbps = 0.0
        if down:
            s.down_mbps = mbps
        else:
            s.up_mbps = mbps


# --------------------------------------------------------------------------- #
# User interface
# --------------------------------------------------------------------------- #
PAGES = (("dash", "1", "Dashboard"), ("lan", "2", "Devices"), ("ble", "3", "Nearby"))
KIND_ORDER = {"wifi": 0, "ethernet": 1, "other": 2, "virtual": 3, "loopback": 4}
KIND_SYMBOL = {"wifi": "≋", "ethernet": "⇄", "other": "◈", "virtual": "◇", "loopback": "∞"}
LAT_FLOORS = {"router": 10.0, "internet": 50.0, "far": 100.0}   # minimum chart ceiling, ms


class UI:
    def __init__(self, state: State):
        self.s = state
        self.page = "dash"
        self.scroll = {"lan": 0, "ble": 0}
        self.t = time.time()
        self._last = time.monotonic()
        self.scale = Ease(50.0)
        self.down = Ease()
        self.up = Ease()

    # -- navigation ------------------------------------------------------ #
    def set_page(self, page: str) -> None:
        if page in {p[0] for p in PAGES}:
            self.page = page

    def next_page(self, step: int = 1) -> None:
        keys = [p[0] for p in PAGES]
        self.page = keys[(keys.index(self.page) + step) % len(keys)]

    def scroll_by(self, delta: int) -> None:
        if self.page in self.scroll:
            self.scroll[self.page] = max(0, self.scroll[self.page] + delta)

    def scroll_to(self, pos: int) -> None:
        if self.page in self.scroll:
            self.scroll[self.page] = pos

    def _clamp_scroll(self, page: str, total: int, rows: int) -> int:
        """Keeps the scroll offset inside the list and remembers the corrected value."""
        top = clamp(self.scroll[page], 0, max(0, total - rows))
        self.scroll[page] = int(top)
        return int(top)

    # -- animation ------------------------------------------------------- #
    def tick(self) -> None:
        now = time.monotonic()
        dt = min(0.25, now - self._last)
        self._last = now
        self.t = time.time()
        k = 1 - math.exp(-dt * 9)
        sp = self.s.speed
        down_t = sp.live_mbps if sp.phase == "download" else (sp.down_mbps or 0.0)
        up_t = sp.live_mbps if sp.phase == "upload" else (sp.up_mbps or 0.0)
        self.down.step(down_t, k)
        self.up.step(up_t, k)
        self.scale.step(pick_scale(max(down_t, up_t)), k)

    def spin(self) -> str:
        return SPINNER[int(self.t * 12) % len(SPINNER)]

    # -- layout ---------------------------------------------------------- #
    def render(self, console: Console):
        self.tick()
        size = console.size
        if size.width < 80 or size.height < 24:
            return Align.center(
                Text(f"Terminal too small ({size.width}x{size.height}): at least 80x24 is required.",
                     style=C_WARN), vertical="middle")
        root = Layout()
        root.split_column(Layout(self.header(), size=1), Layout(name="body"), Layout(self.footer(), size=1))
        body = root["body"]
        if self.page == "lan":
            body.update(self.lan_panel())
        elif self.page == "ble":
            body.update(self.ble_panel())
        else:
            body.split_column(Layout(name="top", ratio=6), Layout(self.speed_panel(), size=7),
                              Layout(name="bottom", ratio=4))
            body["top"].split_row(Layout(self.latency_panel()), Layout(self.traffic_panel()))
            body["bottom"].split_row(Layout(self.iface_panel(), ratio=3), Layout(self.bt_panel(), ratio=2))
        return root

    def panel(self, body, title: str, extra: str = "") -> Panel:
        t = Text(f" {title} ", style="bold")
        if extra:
            t.append(f"{extra} ", style=C_DIM)
        return Panel(body, title=t, title_align="left", border_style=C_BORDER,
                     box=box.ROUNDED, padding=(0, 1))

    def header(self):
        wave = "".join(WAVE[int((math.sin(self.t * 3 + i * 0.7) * 0.5 + 0.5) * 8)] for i in range(6))
        left = Text.assemble((" ◉ NETDASH ", f"bold {C_ACCENT}"), (wave, C_ACCENT), "  ")
        for key, num, label in PAGES:
            style = f"bold black on {C_ACCENT}" if key == self.page else C_DIM
            left.append(f" {num} {label} ", style=style)
        online = self.s.online()
        if online is None:
            status = Text(f"{self.spin()} connecting… ", style=C_DIM)
        elif online:
            status = Text.assemble(("● online", C_GOOD))
        else:
            status = Text.assemble(("✖ offline", f"bold {C_BAD}"))
        right = Text.assemble(status, (f"   {time.strftime('%H:%M:%S')} ", C_DIM))
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", no_wrap=True)
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(left, right)
        return grid

    def footer(self):
        keys = {
            "dash": (("s", "speed test"), ("a", "all interfaces"), ("Tab", "pages"), ("q", "quit")),
            "lan": (("n", "scan network"), ("↑↓", "scroll"), ("s", "speed test"), ("Tab", "pages"), ("q", "quit")),
            "ble": (("b", "scan Bluetooth"), ("↑↓", "scroll"), ("Tab", "pages"), ("q", "quit")),
        }[self.page]
        t = Text(no_wrap=True)
        for key, label in keys:
            t.append(f" {key} ", style=f"bold black on {C_ACCENT}")
            t.append(f" {label}  ", style=C_DIM)
        return t

    # -- latency (router / internet / far) ------------------------------- #
    def latency_panel(self):
        s = self.s

        def block(t: Target, w: int, lines: int) -> List[Text]:
            samples = t.series.snapshot()
            win = samples[-visible_samples(w):]
            ok = [v for v in win if v is not None]
            cur = samples[-1] if samples else None
            base = far_baseline(samples) if t.key == "far" else None
            ident = (s.gateway or "") if t.key == "router" else (t.label or t.host)
            left = Text(no_wrap=True)
            left.append(t.name, style="bold")
            if ident:
                left.append(f" {ident}", style=C_DIM)
            if ok and w >= 62:
                jitter = (sum(abs(a - b) for a, b in zip(ok, ok[1:])) / (len(ok) - 1)) if len(ok) > 1 else 0.0
                loss = 100.0 * (len(win) - len(ok)) / len(win)
                left.append(f"   avg {sum(ok) / len(ok):.0f} · jitter {jitter:.1f} · loss {loss:.0f}%", style=C_DIM)
            right = Text(no_wrap=True)
            if t.key == "far" and not t.host:
                right.append("choosing a host…", style=C_DIM)
            elif t.key == "router" and not s.gateway:
                right.append("no gateway", style=C_WARN)
            elif not samples:
                right.append(self.spin(), style=C_DIM)
            elif t.key == "router" and not t.ever_ok and len(samples) >= 10:
                right.append("no TCP reply", style=C_WARN)
            elif cur is None:
                right.append("✖ no reply", style=f"bold {C_BAD}")
            else:
                right.append(f"{cur:.1f} ms", style=f"bold {latency_color_for(t.key, cur, base)}")
            out = [two_col(left, right, w, keep="right")]
            if lines - 1 >= 1:
                floor = LAT_FLOORS[t.key]
                vmax = max(floor, nice_ceil(max(ok) * 1.15)) if ok else floor
                out.extend(chart_block(samples, w, lines - 1, vmax,
                                       lambda v, k=t.key, b=base: latency_color_for(k, v, b),
                                       fmt_ms, loss_style=C_BAD))
            return out

        def body(w: int, h: int):
            level, short, long = diagnose(s)
            icon, col = {"ok": ("✔", C_GOOD), "warn": ("▲", C_WARN), "bad": ("✖", C_BAD),
                         "wait": (self.spin(), C_DIM)}[level]
            verdict = Text(no_wrap=True, overflow="ellipsis")
            verdict.append(f"{icon} ", style=f"bold {col}")
            verdict.append(long if cell_len(long) + 2 <= w else short, style="" if level == "ok" else "bold")
            per = max(1, (h - 1) // len(s.targets))
            rows: List = [verdict]
            for t in s.targets:
                rows.extend(block(t, w, per))
            return Group(*rows)

        return self.panel(Dynamic(body), "Latency", "ms · ✖ lost")

    # -- traffic --------------------------------------------------------- #
    def traffic_panel(self):
        s = self.s

        def block(arrow: str, series: list, color: str, w: int, rows: int):
            cur = series[-1] if series else 0.0
            win = series[-visible_samples(w):]
            peak = max(win) if win else 0.0
            vmax = nice_ceil(max(peak * 8 * 1.1, 1e6)) / 8
            head = Text(no_wrap=True)
            head.append(f"{arrow} {fmt_rate(cur)}", style=f"bold {color}")
            head.append(f"   peak {fmt_rate(peak)}", style=C_DIM)
            return [head, *chart_block(series, w, max(1, rows), vmax, lambda _v: color, fmt_axis_rate, fill=True)]

        def body(w: int, h: int):
            top = max(2, h // 2)
            return Group(*block("↓", s.rx.snapshot(), C_DOWN, w, top - 1),
                         *block("↑", s.tx.snapshot(), C_UP, w, h - top - 1))

        return self.panel(Dynamic(body), "Traffic", f"{s.main_iface or 'no interface'} · bit/s")

    # -- speed test ------------------------------------------------------ #
    def speed_panel(self):
        sp = self.s.speed

        def row(label: str, value: str, ease: Ease, color: str, samples: List[float], w: int):
            spark_w = 22 if w >= 90 else 0
            fixed = 10 + 14 + 2 + (spark_w + 2 if spark_w else 0)
            bar_w = max(8, w - fixed)
            t = Text(no_wrap=True)
            t.append(label.ljust(10), style="bold")
            t.append(value.rjust(14), style=f"bold {color}")
            t.append("  ")
            t.append_text(gauge(ease.v / self.scale.v, bar_w, color))
            if spark_w:
                t.append("  ")
                t.append_text(sparkline(list(samples), spark_w, self.scale.v, color))
            return t

        def mbps_text(phase: str, final: Optional[float], ease: Ease) -> str:
            if sp.phase == phase:
                return f"{ease.v:7.1f} Mbit/s"
            return f"{final:7.1f} Mbit/s" if final is not None else "—"

        def body(w: int, h: int):
            st = Text(no_wrap=True)
            right = Text(style=C_DIM)
            if sp.phase == "idle":
                st.append("Press ", style=C_DIM)
                st.append(" s ", style=f"bold black on {C_ACCENT}")
                st.append(" to start the speed test", style=C_DIM)
            elif sp.running:
                names = {"ping": "Measuring latency", "download": "Downloading",
                         "upload": "Uploading"}
                st.append(f"{self.spin()} ", style=C_ACCENT)
                st.append(f"{names[sp.phase]}… {sp.progress * 100:3.0f}%", style="bold")
                right.append(f"server: {sp.server}")
            elif sp.phase == "done":
                st.append("✔ Test complete", style=f"bold {C_GOOD}")
                right.append(f"{sp.server} · {time.strftime('%H:%M:%S', time.localtime(sp.finished_at))}")
            else:
                st.append("✖ Error: ", style=f"bold {C_BAD}")
                st.append(sp.error or "unknown")
                right.append("press s to retry")

            ping = Text(no_wrap=True)
            ping.append("PING".ljust(10), style="bold")
            if sp.ping_ms is not None:
                ping.append(f"{sp.ping_ms:7.1f} ms".rjust(14), style=f"bold {lat_color(sp.ping_ms)}")
                ping.append(f"  jitter {sp.jitter_ms:.1f} ms", style=C_DIM)
            elif sp.phase == "ping":
                ping.append("measuring…".rjust(14), style=C_DIM)
            else:
                ping.append("—".rjust(14), style=C_DIM)

            return Group(
                two_col(st, right, w), Text(""), ping,
                row("DOWNLOAD", mbps_text("download", sp.down_mbps, self.down), self.down, C_DOWN,
                    sp.down_samples, w),
                row("UPLOAD", mbps_text("upload", sp.up_mbps, self.up), self.up, C_UP,
                    sp.up_samples, w))

        return self.panel(Dynamic(body), "Speed test", f"scale 0–{self.scale.v:.0f} Mbit/s")

    # -- interfaces ------------------------------------------------------ #
    def iface_panel(self):
        s = self.s

        def body(w: int, h: int):
            if not s.ifaces:
                return Text(f"{self.spin()} detecting interfaces…", style=C_DIM)
            vis = list(s.ifaces) if s.show_all else [i for i in s.ifaces if i.kind in ("wifi", "ethernet", "other")]
            hidden = len(s.ifaces) - len(vis)
            vis.sort(key=lambda i: (not i.is_default, KIND_ORDER.get(i.kind, 9), not i.up, i.name))
            compact = w < 52  # narrow terminals (80 columns): no details, download only
            tbl = Table(box=None, expand=True, pad_edge=False, show_edge=False,
                        header_style=C_DIM, padding=(0, 1), collapse_padding=True)
            tbl.add_column("Interface", no_wrap=True, overflow="ellipsis", min_width=12)
            tbl.add_column("IPv4", no_wrap=True, min_width=15)
            if not compact:
                tbl.add_column("Details", no_wrap=True, overflow="ellipsis", ratio=1)
            tbl.add_column("↓" if compact else "↓ / ↑", no_wrap=True, justify="right",
                           min_width=6 if compact else 13)

            notes = []
            avail = max(1, h - 1)  # one row is the header
            if hidden and not s.show_all:
                notes.append(f"+{hidden} hidden (press a)")
            if len(vis) + (1 if notes else 0) > avail:
                cut = max(1, avail - 1 - (1 if notes else 0))
                notes.insert(0, f"+{len(vis) - cut} more")
                vis = vis[:cut]
            for i in vis:
                if not i.up:
                    col, state_txt = C_DIM, "down"
                elif not i.ipv4:
                    col, state_txt = C_WARN, "no IP"
                else:
                    col, state_txt = C_GOOD, ""
                parts = [i.label]
                if i.kind == "wifi" and i.ssid:
                    parts = [i.ssid + (f" {i.signal}%" if i.signal is not None else "")]
                elif i.kind == "ethernet" and i.speed and i.up:
                    parts.append(f"{i.speed} Mb/s")
                if state_txt:
                    parts.insert(0, state_txt)
                rx, tx = s.rates.get(i.name, (0.0, 0.0))
                name = Text.assemble(("● " if i.up else "○ ", col),
                                     (f"{KIND_SYMBOL.get(i.kind, '')} ", C_DIM),
                                     (i.name, "bold" if i.up else C_DIM))
                if i.is_default:
                    name.append(" ★", style=C_WARN)
                cells = [name, Text(i.ipv4 or "—", style="" if i.ipv4 else C_DIM)]
                if not compact:
                    cells.append(Text(" · ".join(parts), style=C_DIM))
                rates = Text.assemble((f"↓{fmt_axis_rate(rx)}", C_DOWN if rx else C_DIM))
                if not compact:
                    rates.append(" ")
                    rates.append(f"↑{fmt_axis_rate(tx)}", style=C_UP if tx else C_DIM)
                cells.append(rates)
                tbl.add_row(*cells)
            out: List = [tbl]
            if notes:
                out.append(Text("  ".join(notes), style=C_DIM))
            return Group(*out)

        return self.panel(Dynamic(body), "Network interfaces", "★ default route · bit/s")

    # -- bluetooth (paired / connected devices) ----------------------------------- #
    def bt_panel(self):
        s = self.s

        def body(w: int, h: int):
            bt = s.bt
            if bt is None:
                return Text(f"{self.spin()} looking for adapters…", style=C_DIM)
            head = Text(no_wrap=True, overflow="ellipsis")
            if not bt.available:
                head.append("✖ ", style=C_BAD)
                head.append(bt.note or "unavailable", style=C_DIM)
                return head
            if bt.powered:
                head.append("● on", style=C_GOOD)
            else:
                head.append("○ off", style=C_WARN)
            head.append(f"  {bt.name} {bt.address}".rstrip(), style=C_DIM)
            if not bt.devices:
                return Group(head, Text("no known devices", style=C_DIM))
            devs = sorted(bt.devices, key=lambda d: (not d.connected, d.name.lower()))
            tbl = Table(box=None, expand=True, pad_edge=False, show_edge=False,
                        header_style=C_DIM, padding=(0, 1))
            tbl.add_column("Device", no_wrap=True, overflow="ellipsis", ratio=4)
            tbl.add_column("Type", no_wrap=True, overflow="ellipsis", ratio=2)
            tbl.add_column("Batt.", no_wrap=True, justify="right", min_width=5)
            avail = max(1, h - 2)
            extra = max(0, len(devs) - avail)
            if extra:
                devs = devs[:max(1, avail - 1)]
                extra = len(bt.devices) - len(devs)
            pulse = int(self.t * 2) % 2 == 0
            for d in devs:
                if d.connected:
                    dot = Text("●", style=f"bold {C_GOOD}" if pulse else C_GOOD)
                else:
                    dot = Text("○", style=C_DIM)
                bat = Text("—", style=C_DIM)
                if d.battery is not None:
                    col = C_GOOD if d.battery > 40 else C_WARN if d.battery > 15 else C_BAD
                    bat = Text(f"{d.battery}%", style=col)
                tbl.add_row(Text.assemble(dot, " ", (d.name, "" if d.connected else C_DIM)),
                            Text(d.kind or "—", style=C_DIM), bat)
            out: List = [head, tbl]
            if extra:
                out.append(Text(f"+{extra} more", style=C_DIM))
            return Group(*out)

        n = len([d for d in (s.bt.devices if s.bt else []) if d.connected])
        return self.panel(Dynamic(body), "Bluetooth", f"{n} connected" if s.bt and s.bt.available else "")

    # -- page 2: devices on the local network ------------------------------------------ #
    def lan_panel(self):
        s = self.s

        def body(w: int, h: int):
            lan = s.lan
            now = time.time()
            status = Text(no_wrap=True, overflow="ellipsis")
            right = Text(no_wrap=True, style=C_DIM)
            if lan.error:
                status.append("✖ ", style=f"bold {C_BAD}")
                status.append(lan.error)
                right.append("press n to retry")
            elif lan.scanning:
                status.append(f"{self.spin()} ", style=C_ACCENT)
                status.append(f"Scanning {lan.network or 'your network'}…", style="bold")
                right = Text.assemble(gauge(lan.progress, 20, C_ACCENT), (f" {lan.progress * 100:3.0f}%", C_DIM))
            elif lan.last_scan:
                status.append("✔ ", style=f"bold {C_GOOD}")
                status.append(f"Scan complete at {time.strftime('%H:%M:%S', time.localtime(lan.last_scan))}", style="bold")
                right.append("press n to rescan")
            else:
                status.append("Press ", style=C_DIM)
                status.append(" n ", style=f"bold black on {C_ACCENT}")
                status.append(" to scan your network", style=C_DIM)

            devices = list(lan.devices)
            rows_avail = max(1, h - 3)          # status line + table header + note line
            top = self._clamp_scroll("lan", len(devices), rows_avail)
            wide = w >= 90
            tbl = Table(box=None, expand=True, pad_edge=False, show_edge=False,
                        header_style=C_DIM, padding=(0, 1), collapse_padding=True)
            tbl.add_column("", no_wrap=True, width=1)
            tbl.add_column("IP address", no_wrap=True, min_width=15)
            tbl.add_column("MAC address", no_wrap=True, min_width=17)
            tbl.add_column("Name", no_wrap=True, overflow="ellipsis", ratio=3)
            if wide:
                tbl.add_column("Vendor", no_wrap=True, overflow="ellipsis", ratio=3)
            tbl.add_column("Seen", no_wrap=True, justify="right", min_width=8)
            for d in devices[top:top + rows_avail]:
                dot = Text("●", style=C_GOOD) if d.online else Text("○", style=C_DIM)
                name = Text(no_wrap=True)
                name.append(d.name or "—", style="" if d.name and d.online else C_DIM)
                if d.is_self:
                    name.append(" (this device)", style=C_ACCENT)
                if d.is_gateway:
                    name.append(" (router)", style=C_WARN)
                if d.new:
                    name.append(" new", style=f"bold {C_WARN}")
                cells = [dot, Text(d.ip, style="" if d.online else C_DIM),
                         Text(d.mac or "—", style="" if d.online else C_DIM), name]
                if wide:
                    cells.append(Text(d.vendor or ("private MAC" if d.random_mac else "—"), style=C_DIM))
                cells.append(Text("now" if d.online else f"{fmt_age(now - d.last_seen)} ago", style=C_DIM))
                tbl.add_row(*cells)

            note = Text(no_wrap=True, overflow="ellipsis", style=C_DIM)
            note.append("Only your own private network is scanned (ARP, no port scans).")
            if devices and not lan.oui_loaded:
                note.append(" Vendor names need the ieee-data or nmap package.")
            if len(devices) > rows_avail:
                tail = Text(f"↑↓ {top + 1}–{min(len(devices), top + rows_avail)} of {len(devices)}", style=C_DIM)
                note = two_col(note, tail, w, keep="right")
            parts: List = [two_col(status, right, w, keep="left")]
            if devices:
                parts.append(tbl)
            parts.append(note)
            return Group(*parts)

        s_lan = s.lan
        online = sum(1 for d in s_lan.devices if d.online)
        extra = f"{s_lan.network} · {online} online" + (
            f" · {len(s_lan.devices) - online} offline" if len(s_lan.devices) > online else "") if s_lan.network else ""
        return self.panel(Dynamic(body), "Devices on your network", extra)

    # -- page 3: nearby Bluetooth LE devices ---------------------------------------------- #
    def ble_panel(self):
        s = self.s

        def body(w: int, h: int):
            ble = s.ble
            now = time.time()
            with ble.lock:
                devs = [{"addr": d.address, "name": d.name, "maker": d.manufacturer, "rssi": d.rssi,
                         "hist": list(d.history), "seen": d.last_seen} for d in ble.devices.values()]
            nearby = [d for d in devs if d["rssi"] >= BLE_MIN_RSSI]   # weaker ones are too far away
            hidden = len(devs) - len(nearby)
            status = Text(no_wrap=True, overflow="ellipsis")
            right = Text(no_wrap=True, style=C_DIM)
            if hidden:
                right.append(f"{hidden} weaker than {BLE_MIN_RSSI} dBm hidden")
            hint = ""
            if ble.error:
                status.append("✖ ", style=f"bold {C_BAD}")
                status.append(ble.error)
                hint = "press b to retry"
            elif not ble.installed:
                status.append("✖ ", style=f"bold {C_BAD}")
                status.append("Bluetooth scanning needs the 'bleak' package:  python3 -m pip install bleak")
            elif ble.scanning:
                status.append(f"{self.spin()} ", style=C_ACCENT)
                status.append(f"Scanning… {len(nearby)} nearby", style="bold")
                hint = "press b to stop"
            elif ble.requested:
                status.append("Starting the scanner…", style=C_DIM)
            elif ble.ever_scanned:
                status.append("■ Scan stopped", style="bold")
                hint = "press b to scan again"
            else:
                status.append("Press ", style=C_DIM)
                status.append(" b ", style=f"bold black on {C_ACCENT}")
                status.append(" to scan for nearby Bluetooth devices", style=C_DIM)
            if hint:
                right.append(("  ·  " if hidden else "") + hint)

            # While scanning, devices not heard for a while are dimmed; a stopped scan is a snapshot.
            nearby.sort(key=lambda d: (ble.scanning and now - d["seen"] > 15, -d["rssi"]))
            rows_avail = max(1, h - 3)
            top = self._clamp_scroll("ble", len(nearby), rows_avail)
            tbl = Table(box=None, expand=True, pad_edge=False, show_edge=False,
                        header_style=C_DIM, padding=(0, 1), collapse_padding=True)
            tbl.add_column("Signal", no_wrap=True, min_width=13)
            tbl.add_column("Name", no_wrap=True, overflow="ellipsis", ratio=3)
            tbl.add_column("Maker", no_wrap=True, overflow="ellipsis", ratio=2, min_width=8)
            if w >= 78:
                tbl.add_column("Distance", no_wrap=True, min_width=10)
            if w >= 96:
                tbl.add_column("Trend", no_wrap=True, min_width=12)
            if w >= 110:
                tbl.add_column("Address", no_wrap=True, min_width=17)
            tbl.add_column("Seen", no_wrap=True, justify="right", min_width=5)
            for d in nearby[top:top + rows_avail]:
                age = now - d["seen"]
                stale = ble.scanning and age > 15
                level = signal_level(d["rssi"])
                lit = C_GOOD if level >= 3 else C_WARN if level == 2 else C_BAD
                bars = Text(no_wrap=True)
                for n, ch in enumerate("▂▄▆█"):  # unlit bars shrink to a thin line: shape, not just color
                    on = n < level and not stale
                    bars.append(ch if on else "▁", style=lit if on else C_TRACK)
                bars.append(f" {d['rssi']:4.0f} dBm", style=C_DIM if stale else "")
                dim = C_DIM if stale else ""
                cells = [bars,
                         Text(d["name"] or "(unknown)", style=C_DIM if stale or not d["name"] else ""),
                         Text(d["maker"] or "—", style=C_DIM)]
                if w >= 78:
                    cells.append(Text(proximity_label(d["rssi"]), style=dim))
                if w >= 96:
                    cells.append(sparkline([h + 100 for h in d["hist"]], 12, 70, C_DIM if stale else C_ACCENT))
                if w >= 110:
                    addr = d["addr"]
                    cells.append(Text(addr if len(addr) <= 17 else addr[:8] + "…", style=C_DIM))
                cells.append(Text("now" if age < 2 else fmt_age(age), style=C_DIM))
                tbl.add_row(*cells)

            note = Text(no_wrap=True, overflow="ellipsis", style=C_DIM)
            note.append("BLE advertisements only. Distance is a rough hint from signal strength; walls and bodies change it.")
            if len(nearby) > rows_avail:
                tail = Text(f"↑↓ {top + 1}–{min(len(nearby), top + rows_avail)} of {len(nearby)}", style=C_DIM)
                note = two_col(note, tail, w, keep="right")
            parts: List = [two_col(status, right, w, keep="left")]
            if nearby:
                parts.append(tbl)
            elif ble.scanning:
                parts.append(Text(f"No device stronger than {BLE_MIN_RSSI} dBm yet.", style=C_DIM))
            parts.append(note)
            return Group(*parts)

        return self.panel(Dynamic(body), "Nearby Bluetooth devices", f"stronger than {BLE_MIN_RSSI} dBm")


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="netdash",
        description="Animated terminal dashboard: latency, traffic, speed test, network interfaces, "
                    "devices on your network and nearby Bluetooth.")
    p.add_argument("--host", default="1.1.1.1", help="Internet host used to measure latency (default: 1.1.1.1)")
    p.add_argument("--port", type=int, default=443, help="TCP port used to measure latency (default: 443)")
    p.add_argument("--far-host", metavar="HOST[:PORT]",
                   help="far host for the third latency probe (default: the farthest of a few public "
                        "ping servers around the world)")
    p.add_argument("--gateway", metavar="IP", help="router address, when it cannot be detected automatically")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between two latency samples (default: 1)")
    p.add_argument("--speed-url", default="https://speed.cloudflare.com",
                   help="speed test server exposing the /__down and /__up endpoints")
    p.add_argument("--duration", type=float, default=8.0, help="duration of the download and upload phases in seconds (default: 8)")
    p.add_argument("--streams", type=int, default=4, help="parallel connections (default: 4)")
    p.add_argument("--max-mb", type=int, default=250,
                   help="data cap per phase in MB, to spare metered connections (default: 250)")
    p.add_argument("--no-bluetooth", action="store_true", help="turn the Bluetooth panel off")
    p.add_argument("-a", "--all-interfaces", action="store_true",
                   help="also show virtual and loopback interfaces")
    p.add_argument("--fps", type=int, default=20, help="frames per second (default: 20)")
    return p.parse_args(argv)


def split_host_port(value: str, default_port: int) -> Tuple[str, int]:
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host, int(port)
    return value, default_port


_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]|\x1bO[A-Za-z]")
_KEY_SEQUENCES = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1bOA": "up", "\x1bOB": "down",
    "\x1b[5~": "pgup", "\x1b[6~": "pgdn", "\x1b[H": "home", "\x1b[F": "end",
    "\x1b[1~": "home", "\x1b[4~": "end", "\x1b[Z": "backtab",
}


def parse_keys(data: str) -> List[str]:
    """Raw terminal input -> key names ('up', 'tab', 'q', ...). Unknown sequences are ignored."""
    keys: List[str] = []
    i = 0
    while i < len(data):
        ch = data[i]
        if ch == "\x1b":
            name = next((n for seq, n in _KEY_SEQUENCES.items() if data.startswith(seq, i)), None)
            if name:
                keys.append(name)
                i += next(len(seq) for seq in _KEY_SEQUENCES if data.startswith(seq, i))
            else:
                m = _ESC_RE.match(data, i)
                i = m.end() if m else i + 1
            continue
        keys.append("tab" if ch == "\t" else "enter" if ch in "\r\n" else ch.lower())
        i += 1
    return keys


def main(argv=None) -> int:
    args = parse_args(argv)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("netdash needs an interactive terminal.")

    far_host, far_port = split_host_port(args.far_host, 443) if args.far_host else (None, 443)
    state = State(args.host, args.port, args.all_interfaces, far_host, far_port, args.gateway)
    stop = threading.Event()
    speed = SpeedTest(state.speed, args.speed_url, args.duration, args.streams, args.max_mb)
    lan_scanner = LanScanner(state)
    ble_scanner = BleScanner(state, stop)

    def refresh_ifaces() -> None:
        default_if, gateway = default_route()
        ifaces = collect_interfaces(default_if)
        main_if = next((i.name for i in ifaces if i.is_default), None)
        if main_if is None:
            main_if = next((i.name for i in ifaces if i.kind in ("wifi", "ethernet") and i.up and i.ipv4), None)
        state.ifaces, state.main_iface = ifaces, main_if
        state.gateway = args.gateway or gateway

    def refresh_bt() -> None:
        state.bt = collect_bluetooth()

    interval = max(0.2, args.interval)
    workers: List[threading.Thread] = [TargetMonitor(state, t, interval, stop) for t in state.targets]
    workers.append(TrafficMonitor(state, stop))
    workers.append(Periodic("interfaces", refresh_ifaces, 4.0, state, stop))
    if not args.no_bluetooth:
        workers.append(Periodic("bluetooth", refresh_bt, 15.0, state, stop))
    else:
        state.bt = BtInfo(False, note="disabled (--no-bluetooth)")
    workers.append(ble_scanner)
    for w in workers:
        w.start()

    console = Console()
    ui = UI(state)
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    frame = 1.0 / max(1, args.fps)
    try:
        tty.setcbreak(fd)
        with Live(ui.render(console), console=console, screen=True, auto_refresh=False) as live:
            running = True
            while running:
                t0 = time.monotonic()
                if ui.page != "ble" and ble_scanner.active.is_set():
                    ble_scanner.stop_scan()   # leaving the Bluetooth page ends its scan
                live.update(ui.render(console), refresh=True)
                wait = max(0.0, frame - (time.monotonic() - t0))
                if select.select([fd], [], [], wait)[0]:
                    for key in parse_keys(os.read(fd, 64).decode(errors="ignore")):
                        if key in ("q", "\x03", "\x04"):
                            running = False
                        elif key in ("1", "2", "3"):
                            ui.set_page(PAGES[int(key) - 1][0])
                        elif key == "tab":
                            ui.next_page()
                        elif key == "backtab":
                            ui.next_page(-1)
                        elif key == "s":
                            speed.start()
                        elif key == "n" and ui.page == "lan":
                            lan_scanner.start()
                        elif key == "b" and ui.page == "ble":
                            ble_scanner.toggle()
                        elif key == "a" and ui.page == "dash":
                            state.show_all = not state.show_all
                        elif key in ("up", "k"):
                            ui.scroll_by(-1)
                        elif key in ("down", "j"):
                            ui.scroll_by(1)
                        elif key == "pgup":
                            ui.scroll_by(-10)
                        elif key == "pgdn":
                            ui.scroll_by(10)
                        elif key == "home":
                            ui.scroll_to(0)
                        elif key == "end":
                            ui.scroll_to(10 ** 6)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        ble_scanner.join(timeout=2.0)

    sp = state.speed
    if sp.phase == "done":
        print(f"Last speed test: ping {sp.ping_ms:.1f} ms · ↓ {sp.down_mbps:.1f} Mbit/s · ↑ {sp.up_mbps:.1f} Mbit/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
