# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# ↑ 只用标准库：uv run 时不安装项目依赖，也不要求 Python 3.11；没有 uv 时直接 python 运行也可以
"""【开发机执行】把仓库部署到集群宿主机（manager）的 $REMOTE_DIR，并安装/更新 systemd unit。

    uv run ops/local/deploy_host.py <环境名>              # 同步代码 + .env + uv sync + 安装 unit，重启运维界面
    uv run ops/local/deploy_host.py <环境名> --restart    # 另外重启正在运行的采集器
    uv run ops/local/deploy_host.py <环境名> --force      # 跳过部署前检查（会提示）

部署前检查（多人协作，防止互相覆盖）：
  - 工作区必须干净，HEAD 必须已推送到 origin    —— 远程跑的代码一定能在 git 里找到
  - manager 上已部署的提交必须在 origin 上，且是本地 HEAD 的祖先 —— 否则说明别人部署了你没拉取的代码
部署记录：$REMOTE_DIR/.deployed-rev（提交）、.deployed-by（谁、何时、从哪台机器）、.deployed-files（文件清单）

安全约束：
  - 只安装 ops/cluster/systemd/ 里、且在 inventory.toml 登记为 managed=true 的 unit（不看 owner）
  - 被替换的旧 unit 先备份到 ~/swarm-bak/systemd/<时间戳>/
  - 只删除上次由本脚本部署、这次已不存在的文件（按 .deployed-files 清单）；.venv/.env 等不动
  - 不碰 ~/appv8、~/apps 及其 venv（旧代码目录，已不再被任何 unit 使用，保留备查）
  - 不改变采集器的开机自启状态（需要时在 manager 上 systemctl enable/disable）
"""
from __future__ import annotations

import argparse
import re
import socket
from datetime import datetime

from common import REPO_ROOT, die, load_env, out, q, run, ssh, ssh_bash, ssh_out, tar_bytes

# 不部署到集群的路径：clients/ 在集群外运行，ops/local/ 只在开发机用，其余是存档或私有数据
SYNC_EXCLUDE = re.compile(r"^(clients|ops/local|envs|data|legacy|host/archive|image/versions)/")
UV_GROUPS = "--no-default-groups --group host --group algo --group ui"


def main() -> None:
    ap = argparse.ArgumentParser(description="部署到 manager")
    ap.add_argument("env", nargs="?", help="环境名（或设置 SWARM_ENV）")
    ap.add_argument("--restart", action="store_true", help="重启正在运行的采集器")
    ap.add_argument("--force", action="store_true", help="跳过部署前检查")
    a = ap.parse_args()

    env = load_env(a.env)
    env.require("MANAGER_SSH", "MANAGER_IP", "REMOTE_DIR", "OPS_PORT")
    host, dest = env["MANAGER_SSH"], env["REMOTE_DIR"]

    def fail_or_force(msg: str) -> None:
        if a.force:
            print(f"  ⚠️  {msg}（--force，继续）")
        else:
            die(f"{msg}   （确认无误可加 --force）")

    def on_origin(rev: str) -> bool:
        return bool(out(["git", "branch", "-r", "--contains", rev], check=False))

    print(f">>> [0/5] 部署前检查  环境={env.name}  目标={host}:{dest}")
    rev = out(["git", "rev-parse", "HEAD"])
    dirty = bool(out(["git", "status", "--porcelain"]))
    if dirty:
        fail_or_force("工作区有未提交的改动")
    if run(["git", "fetch", "-q", "origin"], check=False).returncode != 0:
        print("  ⚠️  git fetch 失败，按本地已知的 origin 判断")
    if not on_origin(rev):
        fail_or_force(f"HEAD {rev[:7]} 还没推送到 origin")
    old_rev = ssh_out(host, f"cat {dest}/.deployed-rev 2>/dev/null || true", check=False)
    if old_rev:
        old_by = ssh_out(host, f"cat {dest}/.deployed-by 2>/dev/null || true", check=False)
        print(f"  manager 上当前：{old_rev[:7]}  {old_by}")
        known = run(["git", "cat-file", "-e", f"{old_rev}^{{commit}}"], capture=True, check=False).returncode == 0
        if not known or not on_origin(old_rev):
            fail_or_force(f"manager 上部署的 {old_rev[:7]} 不在 origin 上（别人部署了未推送的代码，或 git 历史被重写过）")
        elif run(["git", "merge-base", "--is-ancestor", old_rev, rev], check=False).returncode != 0:
            fail_or_force(f"manager 上的 {old_rev[:7]} 不是 HEAD 的祖先（别人部署了你没合并的提交，先 git pull）")

    print(f">>> [1/5] 同步代码 + .env  ({rev[:7]}{'+dirty' if dirty else ''})")
    listed = out(["git", "ls-files", "-co", "--exclude-standard", "-z"]).split("\0")
    files = sorted(f for f in listed if f and not SYNC_EXCLUDE.match(f) and (REPO_ROOT / f).is_file())
    payload = {f: (REPO_ROOT / f).read_bytes() for f in files}
    ssh(host, f"mkdir -p {q(dest)} && tar -xf - -C {q(dest)}", input=tar_bytes(payload))
    ssh(host, f"umask 077 && cat > {q(dest)}/.env", input=env.file.read_bytes())

    print(">>> [2/5] 清理上次部署、这次已不存在的文件")
    ssh(host, f"cat > {q(dest)}/.deployed-files.new", input="\n".join(files) + "\n")
    ssh_bash(host, CLEANUP.replace("@DEST@", dest))

    # 本机渲染 unit 模板（agent 的 unit 由 deploy_agents.py 负责）
    units = {p.name: env.render(p).encode("utf-8")
             for p in sorted((REPO_ROOT / "ops/cluster/systemd").glob("*.service"))
             if p.name != "swarm-lb-agent.service"}
    rd = f"{dest}/.rendered/systemd"
    ssh(host, f"rm -rf {q(rd)} && mkdir -p {q(rd)} && tar -xf - -C {q(rd)}", input=tar_bytes(units))

    print(f">>> [3/5] uv sync ({UV_GROUPS})")
    ssh(host, f"cd {q(dest)} && ~/.local/bin/uv sync --frozen {UV_GROUPS} 2>&1 | tail -3")

    print(">>> [4/5] 安装 systemd unit")
    ssh_bash(host, INSTALL_UNITS.replace("@DEST@", dest).replace("@RESTART@", "1" if a.restart else "0"))

    print(">>> [5/5] 记录部署")
    who = out(["git", "config", "user.name"], check=False) or "unknown"
    by = f"{who} {datetime.now():%Y-%m-%d %H:%M:%S} from {socket.gethostname()}"
    ssh(host, f"echo {q(rev)} > {q(dest)}/.deployed-rev && echo {q(by)} > {q(dest)}/.deployed-by")
    print(f"  {rev[:7]}  {by}")

    ssh(host, "systemctl --no-pager --plain list-units 'collect_*' swarm-lb-ops.service | head -n -5")
    print(f">>> 运维界面: http://{env['MANAGER_IP']}:{env['OPS_PORT']}/")


# 在 manager 上执行：按 .deployed-files 清单删除这次已不存在的文件
CLEANUP = r"""
set -euo pipefail
cd @DEST@
if [ -f .deployed-files ]; then
  n=0
  while IFS= read -r f; do
    case "$f" in ""|/*|*..*) continue;; esac
    [ -f "$f" ] && { rm -f -- "$f"; n=$((n+1)); }
  done < <(comm -23 <(sort .deployed-files) <(sort .deployed-files.new))
  echo "  删除 $n 个旧文件"
  # 代码已经删光、只剩 __pycache__ 的目录（改名后的旧包），连缓存一起删
  find . -mindepth 2 -type d -name __pycache__ -not -path './.venv/*' | while read -r c; do
    p=$(dirname "$c"); ls "$p"/*.py >/dev/null 2>&1 || { rm -rf -- "$c"; echo "  删除旧缓存 $c"; }
  done
  find . -mindepth 1 -type d -empty -not -path './.venv/*' -not -path './.git/*' -print -delete | sed 's/^/  删除空目录 /'
else
  echo "  首次记录文件清单，不删除任何文件"
fi
mv .deployed-files.new .deployed-files
"""

# 在 manager 上执行：安装 managed 的 unit，必要时重启
INSTALL_UNITS = r"""
set -euo pipefail
cd @DEST@
RESTART=@RESTART@
ALLOWED=$(.venv/bin/python -c '
import tomllib
inv = tomllib.load(open("inventory.toml", "rb"))
print(" ".join(s["unit"] for s in inv["service"] if s.get("managed")))
')
TS=$(date +%Y%m%d-%H%M%S)
CHANGED=""
for f in .rendered/systemd/*.service; do
  u=$(basename "$f")
  case " $ALLOWED " in *" $u "*) ;; *) echo "  跳过 $u（inventory 未登记为 managed）"; continue;; esac
  dst=/etc/systemd/system/$u
  if [ -f "$dst" ] && cmp -s "$f" "$dst"; then echo "  $u 未变化"; continue; fi
  if [ -f "$dst" ]; then mkdir -p ~/swarm-bak/systemd/$TS; cp "$dst" ~/swarm-bak/systemd/$TS/; fi
  sudo -n install -m 644 "$f" "$dst"; echo "  已安装 $u"; CHANGED="$CHANGED $u"
done
[ -n "$CHANGED" ] && sudo -n systemctl daemon-reload
for u in $CHANGED; do
  [ "$u" != swarm-lb-ops.service ] && systemctl is-active --quiet $u && [ $RESTART = 0 ] \
    && echo "  ⚠️  $u 正在运行且 unit 已变化，加 --restart 才会生效"
done
sudo -n systemctl enable --quiet swarm-lb-ops.service
sudo -n systemctl restart swarm-lb-ops.service && echo "  重启 swarm-lb-ops.service"
if [ $RESTART = 1 ]; then
  for u in $ALLOWED; do
    [ "$u" = swarm-lb-ops.service ] && continue
    if systemctl is-active --quiet $u; then sudo -n systemctl restart $u && echo "  重启 $u"; fi
  done
fi
true
"""

if __name__ == "__main__":
    main()
