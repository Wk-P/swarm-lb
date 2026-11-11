"""ops/local/ 各脚本的公共部分（开发机执行，Linux / macOS / Windows 通用，只用标准库）。

运行方式：在仓库根目录执行  uv run ops/local/<脚本>.py <环境名> [参数...]
环境名也可以用环境变量 SWARM_ENV 指定。

依赖本机的 ssh 客户端：Linux/macOS 自带；Windows 用系统自带的 OpenSSH（PowerShell 里的 ssh）
或 Git Bash 里的 ssh 都可以，它们都读 ~/.ssh/config（即 C:\\Users\\<你>\\.ssh\\config）。
"""
from __future__ import annotations

import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENVS_DIR = REPO_ROOT / "envs"

# Windows 控制台默认不是 UTF-8，中文输出会乱码或报错
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


def list_envs() -> str:
    names = sorted(p.stem for p in ENVS_DIR.glob("*.env") if p.name != "example.env")
    return " ".join(names) or "（无，先 cp envs/example.env envs/<环境名>.env）"


class Env(dict):
    """envs/<环境名>.env 的内容。name = 环境名，file = 文件路径。"""

    name: str
    file: Path

    def require(self, *keys: str) -> None:
        miss = [k for k in keys if not self.get(k)]
        if miss:
            die(f"{self.file} 缺少：{' '.join(miss)}")

    def render(self, template: Path) -> str:
        """把模板里的 @变量名@ 替换成环境中的值；缺值则退出。"""
        text = template.read_text(encoding="utf-8")
        keys = sorted(set(re.findall(r"@([A-Z0-9_]+)@", text)))
        self.require(*keys)
        for k in keys:
            text = text.replace(f"@{k}@", self[k])
        return text

    def ssh_hosts(self) -> list[str]:
        return [self[k] for k in ("MANAGER_SSH", "WORKER1_SSH", "WORKER2_SSH", "WORKER3_SSH")]


def load_env(name: str | None) -> Env:
    name = name or os.environ.get("SWARM_ENV")
    if not name:
        die(f"未指定环境。用法：uv run ops/local/<脚本>.py <环境名> …    可用环境：{list_envs()}")
    f = ENVS_DIR / f"{name}.env"
    if not f.is_file():
        die(f"找不到 envs/{name}.env。可用环境：{list_envs()}")
    env = Env()
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    env.name, env.file = name, f
    return env


def run(cmd: list[str], *, input: bytes | str | None = None, capture: bool = False,
        check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    if isinstance(input, str):
        input = input.encode("utf-8")
    r = subprocess.run(cmd, input=input, cwd=cwd or REPO_ROOT,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None)
    if check and r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip() if capture else ""
        die(f"命令失败（{r.returncode}）：{' '.join(cmd[:3])} …{(' ' + err) if err else ''}")
    return r


def out(cmd: list[str], **kw) -> str:
    """执行命令并返回 stdout（去掉首尾空白）。"""
    r = run(cmd, capture=True, **kw)
    return r.stdout.decode("utf-8", "replace").strip()


def ssh(host: str, remote_cmd: str, *, input: bytes | str | None = None, capture: bool = False,
        check: bool = True, tty: bool = False, batch: bool = True) -> subprocess.CompletedProcess:
    opts = ["-o", "BatchMode=yes"] if batch else []
    if tty:
        opts.append("-t")
    elif input is None:
        opts.append("-n")
    return run(["ssh", *opts, host, remote_cmd], input=input, capture=capture, check=check)


def ssh_out(host: str, remote_cmd: str, *, check: bool = True) -> str:
    r = ssh(host, remote_cmd, capture=True, check=check)
    return r.stdout.decode("utf-8", "replace").strip()


def ssh_bash(host: str, script: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """在远程用 bash 执行一段脚本（脚本走 stdin，换行统一为 LF）。"""
    return ssh(host, "bash -s", input=script.replace("\r\n", "\n"), check=check)


def tar_bytes(files: dict[str, bytes]) -> bytes:
    """把 {相对路径: 内容} 打成 tar。*.sh 给执行权限，其余 644；换行保持原样。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path, data in files.items():
            ti = tarfile.TarInfo(path)
            ti.size = len(data)
            ti.mode = 0o755 if path.endswith(".sh") else 0o644
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def q(s: str) -> str:
    """远程 shell 参数转义。"""
    return shlex.quote(s)
