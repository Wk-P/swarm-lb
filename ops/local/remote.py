# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# ↑ 只用标准库：uv run 时不安装项目依赖，也不要求 Python 3.11；没有 uv 时直接 python 运行也可以
"""【开发机执行】在 manager 上运行集群内运维脚本（ops/cluster/bin/，需先 deploy_host.py 部署过去）。

    uv run ops/local/remote.py <环境名>                          # 列出可用脚本
    uv run ops/local/remote.py <环境名> <脚本名> [参数...]
    uv run ops/local/remote.py <环境名> --sudo <脚本名> [参数...]   # 以 root 运行（如 setup-udisk.sh）

例：
    uv run ops/local/remote.py lab check-stack.sh
    uv run ops/local/remote.py lab check-queue.sh 8084
    uv run ops/local/remote.py lab build-image.sh v9
    uv run ops/local/remote.py lab rebuild-swarm.sh       # 交互式，会要求输入各 worker 密码
"""
from __future__ import annotations

import sys

from common import REPO_ROOT, die, load_env, q, ssh


def main() -> None:
    args = sys.argv[1:]
    env = load_env(args.pop(0) if args and not args[0].startswith("-") and not args[0].endswith(".sh") else None)
    env.require("MANAGER_SSH", "REMOTE_DIR")
    bin_dir = REPO_ROOT / "ops/cluster/bin"

    sudo = ""
    if args and args[0] == "--sudo":
        sudo, args = "sudo ", args[1:]
    if not args:
        print("ops/cluster/bin/ 下可用脚本：")
        for p in sorted(bin_dir.glob("*.sh")):
            print("  " + p.name)
        return
    script, rest = args[0], args[1:]
    if not (bin_dir / script).is_file():
        die(f"ops/cluster/bin/{script} 不存在")
    cmd = f"{sudo}bash {q(env['REMOTE_DIR'])}/ops/cluster/bin/{q(script)}" + "".join(" " + q(x) for x in rest)
    # -t：交互式脚本（rebuild-swarm.sh 要输密码）需要终端；不在终端里（被管道调用）时不加
    r = ssh(env["MANAGER_SSH"], cmd, tty=sys.stdin.isatty(), batch=False, check=False)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
