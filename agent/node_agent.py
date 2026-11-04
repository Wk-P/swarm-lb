"""swarm-lb 节点监控 agent —— 只用标准库，每台节点（manager + worker）各跑一个。

GET /metrics.json  → 当前快照（CPU/内存/温度/降频/磁盘/网络/IO/容器）
GET /healthz       → ok

每 SAMPLE_S 秒采样一次，速率类指标按两次采样差值计算。只读，不做任何写操作。
部署：deploy/deploy-agents.sh（systemd: swarm-lb-agent.service，端口 9100）
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("AGENT_PORT", "9100"))
SAMPLE_S = float(os.environ.get("AGENT_SAMPLE_S", "2"))
DOCKER_SOCK = "/var/run/docker.sock"
CGROUP = Path("/sys/fs/cgroup/system.slice")
DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+)$")
NCPU = os.cpu_count() or 1


def read(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text()
    except Exception:
        return default


# ------------------------------------------------------------------ raw readers

def cpu_times() -> list[tuple[int, int]]:
    """[(busy, total)]，下标 0 为总计，其后为各核。"""
    out = []
    for line in read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        v = [int(x) for x in line.split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        total = sum(v[:8])
        out.append((total - idle, total))
    return out


def net_bytes() -> tuple[str, int, int]:
    best = ("", 0, 0)
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, data = line.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("veth", "docker", "br-", "vx", "wg")):
            continue
        f = data.split()
        rx, tx = int(f[0]), int(f[8])
        if rx + tx >= best[1] + best[2]:
            best = (name, rx, tx)
    return best


def disk_bytes() -> tuple[int, int]:
    r = w = 0
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) > 9 and DISK_RE.match(f[2]):
            r += int(f[5]) * 512
            w += int(f[9]) * 512
    return r, w


def meminfo() -> dict[str, int]:
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    return m


def throttled() -> dict:
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout
        v = int(out.strip().split("=")[1], 16)
    except Exception:
        return {"raw": None, "now": [], "since_boot": []}
    names = {0: "欠压", 1: "限频", 2: "降频", 3: "温度软限"}
    return {
        "raw": hex(v),
        "now": [n for b, n in names.items() if v & (1 << b)],
        "since_boot": [n for b, n in names.items() if v & (1 << (b + 16))],
    }


def disks() -> list[dict]:
    out, seen = [], set()
    for line in read("/proc/mounts").splitlines():
        dev, mnt, fs = line.split()[:3]
        if not dev.startswith("/dev/") or fs in ("squashfs", "iso9660") or dev in seen:
            continue
        if mnt.startswith(("/boot", "/snap", "/var/lib/docker")):
            continue
        seen.add(dev)
        try:
            u = shutil.disk_usage(mnt)
        except OSError:
            continue
        out.append({"mount": mnt, "dev": dev.removeprefix("/dev/"), "total": u.total, "used": u.used})
    return out


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=3)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(self._path)
        self.sock = s


def docker_containers() -> list[dict]:
    if not os.path.exists(DOCKER_SOCK):
        return []
    c = UnixHTTPConnection(DOCKER_SOCK)
    try:
        c.request("GET", "/containers/json")
        r = c.getresponse()
        if r.status != 200:
            return []
        data = json.loads(r.read())
    except Exception:
        return []
    finally:
        c.close()
    out = []
    for d in data:
        labels = d.get("Labels") or {}
        out.append({
            "id": d["Id"],
            "name": (d.get("Names") or ["?"])[0].lstrip("/"),
            "service": labels.get("com.docker.swarm.service.name", ""),
            "image": d.get("Image", "").split("@")[0],
            "state": d.get("State"),
            "status": d.get("Status"),
        })
    return out


def cg_usage(cid: str) -> tuple[int | None, int | None, int | None]:
    base = CGROUP / f"docker-{cid}.scope"
    usec = None
    for line in read(base / "cpu.stat").splitlines():
        if line.startswith("usage_usec"):
            usec = int(line.split()[1])
            break
    cur = read(base / "memory.current").strip()
    mx = read(base / "memory.max").strip()
    return usec, int(cur) if cur.isdigit() else None, int(mx) if mx.isdigit() else None


# ------------------------------------------------------------------ sampler

class Sampler:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot: dict = {"ready": False}
        self._prev: dict = {}
        self._containers: list[dict] = []
        self._containers_ts = 0.0
        self._thr: dict = {}
        self._thr_ts = 0.0
        self.model = read("/proc/device-tree/model").strip("\x00\n ") or "linux"

    def sample(self):
        now = time.monotonic()
        prev = self._prev
        dt = now - prev["t"] if prev else None

        cpus = cpu_times()
        iface, rx, tx = net_bytes()
        dr, dw = disk_bytes()

        def pct(i):
            if not prev or i >= len(prev["cpu"]):
                return None
            b0, t0 = prev["cpu"][i]
            b1, t1 = cpus[i]
            return round(100 * (b1 - b0) / (t1 - t0), 1) if t1 > t0 else 0.0

        def rate(a, b):
            return round(max(0, a - b) / dt) if dt else None

        if now - self._containers_ts > 10:
            self._containers = docker_containers()
            self._containers_ts = now
        if now - self._thr_ts > 10:
            self._thr = throttled()
            self._thr_ts = now

        cg_prev = prev.get("cg", {}) if prev else {}
        cg_now, containers = {}, []
        for c in self._containers:
            usec, mem, mmax = cg_usage(c["id"])
            cg_now[c["id"]] = usec
            cpu = None
            if dt and usec is not None and cg_prev.get(c["id"]) is not None:
                cpu = round(100 * (usec - cg_prev[c["id"]]) / (dt * 1e6) / NCPU, 1)
            containers.append({**{k: v for k, v in c.items() if k != "id"},
                               "id": c["id"][:12], "cpu_pct": cpu, "mem": mem, "mem_max": mmax})

        m = meminfo()
        temp = read("/sys/class/thermal/thermal_zone0/temp").strip()
        freq = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").strip()
        snap = {
            "ready": prev != {},
            "ts": time.time(),
            "hostname": socket.gethostname(),
            "model": self.model,
            "uptime_s": float(read("/proc/uptime", "0 0").split()[0]),
            "load": list(os.getloadavg()),
            "cpus": NCPU,
            "cpu_pct": pct(0),
            "cpu_cores": [pct(i) for i in range(1, len(cpus))],
            "freq_mhz": int(freq) // 1000 if freq.isdigit() else None,
            "temp_c": int(temp) / 1000 if temp.lstrip("-").isdigit() else None,
            "throttled": self._thr,
            "mem": {"total": m.get("MemTotal"), "avail": m.get("MemAvailable")},
            "swap": {"total": m.get("SwapTotal"), "free": m.get("SwapFree")},
            "disks": disks(),
            "net": {"iface": iface, "rx_bps": rate(rx, prev.get("rx", 0)) if prev else None,
                    "tx_bps": rate(tx, prev.get("tx", 0)) if prev else None},
            "io": {"read_bps": rate(dr, prev.get("dr", 0)) if prev else None,
                   "write_bps": rate(dw, prev.get("dw", 0)) if prev else None},
            "containers": sorted(containers, key=lambda x: x["name"]),
        }
        self._prev = {"t": now, "cpu": cpus, "rx": rx, "tx": tx, "dr": dr, "dw": dw, "cg": cg_now}
        with self.lock:
            self.snapshot = snap

    def loop(self):
        while True:
            try:
                self.sample()
            except Exception as e:  # noqa: BLE001  采样失败不退出
                with self.lock:
                    self.snapshot = {**self.snapshot, "error": f"{type(e).__name__}: {e}"}
            time.sleep(SAMPLE_S)


SAMPLER = Sampler()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/metrics.json"):
            with SAMPLER.lock:
                body = json.dumps(SAMPLER.snapshot).encode()
            self._send(200, body, "application/json")
        elif self.path.startswith("/healthz"):
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # 不刷访问日志
        pass


def main():
    threading.Thread(target=SAMPLER.loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
