"""【开发机执行】给新成员生成集群访问包（zip）。

    uv run ops/local/make_access.py <环境名> <成员名>          # 例：uv run ops/local/make_access.py lab user02

做的事：
  1. 在 secrets/<成员名>-access/ 生成一把专用 ed25519 密钥（已存在就复用）
  2. 把公钥追加到 4 个节点 pi 账号的 authorized_keys（已存在就跳过），并用新密钥逐个验证登录
  3. 生成 ssh_config.txt（4 个节点的别名）、复制 envs/<环境名>.env、按模板生成 README.md（Linux / Windows 步骤）
  4. 打包成 secrets/swarm-lb-<成员名>-access.zip
secrets/ 在 .gitignore 里，不进 git，也不会被部署。zip 里有私钥和密码，私下交给对方。

撤销某人的权限：在 4 个节点删掉注释为 <成员名>@swarm-lb 的那行公钥，见 README「ssh 访问与成员管理」。
"""
from __future__ import annotations

import re
import shutil
import sys
import zipfile

from common import REPO_ROOT, die, load_env, out, q, run, ssh_out


def main() -> None:
    if len(sys.argv) != 3:
        die("用法：uv run ops/local/make_access.py <环境名> <成员名>")
    env = load_env(sys.argv[1])
    name = sys.argv[2]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        die("成员名只能用字母、数字、_、-")
    env.require("MANAGER_SSH", "MANAGER_IP", "WORKER1_SSH", "WORKER1_IP", "WORKER2_SSH", "WORKER2_IP",
                "WORKER3_SSH", "WORKER3_IP", "REMOTE_USER")
    user = env["REMOTE_USER"]
    nodes = [(env[f"{n}_SSH"], env[f"{n}_IP"]) for n in ("MANAGER", "WORKER1", "WORKER2", "WORKER3")]

    d = REPO_ROOT / "secrets" / f"{name}-access"
    d.mkdir(parents=True, exist_ok=True)
    key = d / f"swarm_lb_{name}_ed25519"
    zip_path = REPO_ROOT / "secrets" / f"swarm-lb-{name}-access.zip"

    print(">>> [1/4] 密钥")
    if key.exists():
        print(f"  复用已有密钥 {key.relative_to(REPO_ROOT)}")
    else:
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{name}@swarm-lb", "-f", str(key)])
        print(f"  已生成 {key.relative_to(REPO_ROOT)}")
    pub = (d / f"{key.name}.pub").read_text(encoding="utf-8").strip()

    print(">>> [2/4] 把公钥加到 4 个节点并验证")
    for alias, ip in nodes:
        state = ssh_out(alias, (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"if grep -qF {q(pub)} ~/.ssh/authorized_keys; then echo 已存在; "
            f"else echo {q(pub)} >> ~/.ssh/authorized_keys && echo 已添加; fi"))
        host = out(["ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
                    "-i", str(key), f"{user}@{ip}", "hostname"])
        print(f"  {alias}  公钥{state}，新密钥登录 OK（{host}）")

    print(">>> [3/4] ssh 配置、env、说明")
    cfg = [f"# swarm-lb 集群节点（{env.name} 环境），追加到 ~/.ssh/config\n"]
    for alias, ip in nodes:
        cfg.append(f"Host {alias}\n    HostName {ip}\n    User {user}\n"
                   f"    IdentityFile ~/.ssh/swarm_lb_{name}_ed25519\n    IdentitiesOnly yes\n\n")
    (d / "ssh_config.txt").write_text("".join(cfg), encoding="utf-8", newline="\n")
    shutil.copyfile(env.file, d / f"{env.name}.env")
    readme = (REPO_ROOT / "ops/local/access-pack-README.md").read_text(encoding="utf-8")
    for k, v in {"@NAME@": name, "@ENV@": env.name,
                 "@SSH_CHECKS@": "\n".join(f"ssh {a} hostname" for a, _ in nodes),
                 "@MANAGER_SSH@": env["MANAGER_SSH"]}.items():
        readme = readme.replace(k, v)
    (d / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    (d / "使用说明.txt").unlink(missing_ok=True)      # 旧版说明（已由 README.md 取代）

    print(">>> [4/4] 打包")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(d.iterdir()):
            zi = zipfile.ZipInfo.from_file(f, f"{name}-access/{f.name}")
            secret = not f.name.endswith((".pub", ".txt", ".md"))
            zi.external_attr = (0o600 if secret else 0o644) << 16
            z.writestr(zi, f.read_bytes(), zipfile.ZIP_DEFLATED)
    print(f"  {zip_path.relative_to(REPO_ROOT)}")
    for n in zipfile.ZipFile(zip_path).namelist():
        print(f"    {n}")
    print(f">>> 完成。zip 里有私钥和密码，私下交给 {name}；对方按里面的 README.md 操作。")


if __name__ == "__main__":
    main()
