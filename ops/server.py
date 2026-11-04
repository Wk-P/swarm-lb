"""swarm-lb 运维管理界面（Manager-Node :5000）

只读：节点、swarm 服务、manager 上所有登记的 systemd 服务、主机资源。
可写：仅 inventory.toml 中 owner = "ours" 的 unit 允许 start / stop / restart。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import time
import tomllib
from pathlib import Path

import aiohttp
import docker  # type: ignore
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "inventory.toml"
STATIC = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("OPS_PORT", "5000"))

ACTIONS = {"start", "stop", "restart"}


def load_inventory() -> dict:
    with open(INVENTORY, "rb") as f:
        return tomllib.load(f)


async def run(*cmd: str, timeout: float = 15) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode(errors="replace")


# ---------------------------------------------------------------- host

def host_stats() -> dict:
    load1, load5, load15 = os.getloadavg()
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    disk = shutil.disk_usage("/")
    temp = None
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except Exception:
        pass
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    return {
        "hostname": socket.gethostname(),
        "load": [load1, load5, load15],
        "cpus": os.cpu_count(),
        "mem_total": mem.get("MemTotal"),
        "mem_avail": mem.get("MemAvailable"),
        "disk_total": disk.total,
        "disk_used": disk.used,
        "temp_c": temp,
        "uptime_s": uptime,
    }


# ---------------------------------------------------------------- systemd

async def systemd_states(units: list[str]) -> dict[str, dict]:
    props = "Id,LoadState,ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestamp,ExecMainStartTimestamp"
    _, out = await run("systemctl", "show", "--no-pager", "-p", props, *units)
    states: dict[str, dict] = {}
    for block in out.strip().split("\n\n"):
        d = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in d:
            states[d["Id"]] = d
    return states


async def listening_ports() -> set[int]:
    _, out = await run("ss", "-tlnH")
    ports = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            try:
                ports.add(int(parts[3].rsplit(":", 1)[1]))
            except ValueError:
                pass
    return ports


async def queue_status(session: aiohttp.ClientSession, port: int, path: str):
    try:
        async with session.get(f"http://127.0.0.1:{port}{path}",
                               timeout=aiohttp.ClientTimeout(total=2)) as r:
            return await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}


# ---------------------------------------------------------------- docker

def docker_state() -> dict:
    cli = docker.from_env(timeout=10)
    try:
        nodes = []
        id2host = {}
        for n in cli.nodes.list():
            a = n.attrs
            host = a["Description"]["Hostname"]
            id2host[a["ID"]] = host
            res = a["Description"].get("Resources", {})
            nodes.append({
                "hostname": host,
                "addr": a["Status"].get("Addr"),
                "role": a["Spec"].get("Role"),
                "labels": a["Spec"].get("Labels", {}),
                "state": a["Status"].get("State"),
                "availability": a["Spec"].get("Availability"),
                "leader": a.get("ManagerStatus", {}).get("Leader", False),
                "engine": a["Description"].get("Engine", {}).get("EngineVersion"),
                "cpus": res.get("NanoCPUs", 0) / 1e9,
                "mem": res.get("MemoryBytes", 0),
            })
        services = []
        for s in cli.services.list():
            spec = s.attrs["Spec"]
            tasks = []
            for t in s.tasks():
                if t.get("DesiredState") != "running" and t["Status"]["State"] == "shutdown":
                    continue
                tasks.append({
                    "node": id2host.get(t.get("NodeID"), t.get("NodeID", "")[:10]),
                    "state": t["Status"]["State"],
                    "desired": t.get("DesiredState"),
                    "since": t["Status"].get("Timestamp"),
                    "err": t["Status"].get("Err", ""),
                })
            tasks.sort(key=lambda x: x["since"] or "", reverse=True)
            limits = spec.get("TaskTemplate", {}).get("Resources", {}).get("Limits", {})
            services.append({
                "name": spec["Name"],
                "image": spec["TaskTemplate"]["ContainerSpec"]["Image"].split("@")[0],
                "replicas": spec.get("Mode", {}).get("Replicated", {}).get("Replicas"),
                "running": sum(1 for t in tasks if t["state"] == "running"),
                "constraints": spec["TaskTemplate"].get("Placement", {}).get("Constraints", []),
                "cpu_limit": limits.get("NanoCPUs", 0) / 1e9,
                "mem_limit": limits.get("MemoryBytes", 0),
                "tasks": tasks[:5],
            })
        services.sort(key=lambda x: x["name"])
        return {"nodes": nodes, "services": services}
    finally:
        cli.close()


async def tcp_ok(host: str, port: int) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 1.5)
        w.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- handlers

async def api_overview(request: web.Request) -> web.Response:
    inv = load_inventory()
    svcs = inv.get("service", [])
    loop = asyncio.get_running_loop()

    async def dock():
        try:
            return await loop.run_in_executor(None, docker_state)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}", "nodes": [], "services": []}

    session: aiohttp.ClientSession = request.app["session"]
    states, ports, dstate = await asyncio.gather(
        systemd_states([s["unit"] for s in svcs]), listening_ports(), dock()
    )
    queues = await asyncio.gather(*[
        queue_status(session, s["port"], s["status_path"]) if s.get("status_path") and s["port"] in ports
        else asyncio.sleep(0, None)
        for s in svcs
    ])
    out_svcs = []
    for s, q in zip(svcs, queues):
        st = states.get(s["unit"], {})
        out_svcs.append({**s, "systemd": st, "listening": s["port"] in ports, "queue": q})

    workers = [n for n in inv.get("node", []) if n["role"] == "worker"]
    app_ok = await asyncio.gather(*[tcp_ok(n["ip"], 8080) for n in workers])

    return web.json_response({
        "time": time.time(),
        "cluster": inv.get("cluster", {}),
        "inventory_nodes": inv.get("node", []),
        "worker_app_8080": {n["name"]: ok for n, ok in zip(workers, app_ok)},
        "host": host_stats(),
        "services": out_svcs,
        "docker": dstate,
    })


def find_service(unit: str) -> dict | None:
    for s in load_inventory().get("service", []):
        if s["unit"] == unit:
            return s
    return None


async def api_action(request: web.Request) -> web.Response:
    unit, action = request.match_info["unit"], request.match_info["action"]
    svc = find_service(unit)
    if svc is None:
        return web.json_response({"ok": False, "msg": "unit 未在 inventory.toml 登记"}, status=404)
    if svc.get("owner") != "ours":
        return web.json_response({"ok": False, "msg": f"owner={svc.get('owner')}，不允许操作"}, status=403)
    if action not in ACTIONS or (svc.get("self") and action == "stop"):
        return web.json_response({"ok": False, "msg": "不支持的操作"}, status=400)
    if svc.get("self") and action == "restart":
        # 先回应再重启自己
        asyncio.get_running_loop().call_later(
            0.5, lambda: asyncio.ensure_future(run("sudo", "-n", "systemctl", "restart", unit)))
        return web.json_response({"ok": True, "msg": "界面即将重启"})
    code, out = await run("sudo", "-n", "systemctl", action, unit, timeout=60)
    return web.json_response({"ok": code == 0, "msg": out.strip() or "ok"}, status=200 if code == 0 else 500)


async def api_logs(request: web.Request) -> web.Response:
    unit = request.match_info["unit"]
    if find_service(unit) is None:
        return web.json_response({"ok": False, "msg": "unit 未登记"}, status=404)
    n = max(10, min(int(request.query.get("n", "300")), 3000))
    _, out = await run("journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso")
    return web.json_response({"ok": True, "text": out})


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def on_startup(app: web.Application):
    app["session"] = aiohttp.ClientSession()


async def on_cleanup(app: web.Application):
    await app["session"].close()


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_post("/api/unit/{unit}/{action}", api_action)
    app.router.add_get("/api/unit/{unit}/logs", api_logs)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
