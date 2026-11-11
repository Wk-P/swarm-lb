# swarm-lb 集群访问包（@NAME@）

这个包里有一把**专门给你的私钥**，能以 `pi` 账号（有 sudo）登录集群全部 4 个节点。
**不要转发，也不要上传到网盘、git 或者群聊。** 配置完成后把 zip 和解压出来的文件删掉。

| 文件 | 用途 |
| --- | --- |
| `swarm_lb_@NAME@_ed25519` | 私钥 |
| `swarm_lb_@NAME@_ed25519.pub` | 公钥（已经加到 4 个节点上，不用再操作） |
| `ssh_config.txt` | 4 个节点的 ssh 别名，追加到 `~/.ssh/config` |
| `@ENV@.env` | 环境参数（IP、账号、路径，含密码），放到仓库的 `envs/@ENV@.env`，不进 git |

> 只想**查看状态、启停采集器、看日志**的话，不用做下面任何配置：在同一个 192.168.1.x 网段里，用浏览器打开 `http://<manager IP>:5000/` 就行（manager IP 见 `@ENV@.env` 里的 `MANAGER_IP`）。

本机要跑的运维脚本都是 Python（`ops/local/*.py`），用 `uv run` 运行：Linux、macOS 用终端，Windows 用 **PowerShell**（Git Bash 也可以）。

---

## Linux / macOS

### 1. 安装工具

```bash
# git：Debian/Ubuntu 用 apt，macOS 用 xcode-select --install 或 brew
sudo apt install -y git          # 或者 brew install git
curl -LsSf https://astral.sh/uv/install.sh | sh    # 安装 uv，装完重新打开终端
```

### 2. 放好密钥和 ssh 配置

在解压出来的目录里执行：

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cp swarm_lb_@NAME@_ed25519 swarm_lb_@NAME@_ed25519.pub ~/.ssh/
chmod 600 ~/.ssh/swarm_lb_@NAME@_ed25519
cat ssh_config.txt >> ~/.ssh/config
chmod 600 ~/.ssh/config
```

### 3. 获取代码、放好环境参数

```bash
git config --global user.name  "@NAME@"          # 部署记录里会显示这个名字
git config --global user.email "<你的邮箱>"
git clone https://github.com/Wk-P/swarm-lb.git
cp @ENV@.env swarm-lb/envs/@ENV@.env
cd swarm-lb && uv sync
```

### 4. 验证

见文末[验证](#验证)。

---

## Windows

下面的命令都在 **PowerShell** 里执行（开始菜单搜 PowerShell）。Windows 10/11 自带 ssh 客户端（OpenSSH），它和 Git Bash、VS Code Remote-SSH 共用同一个 `C:\Users\<你的用户名>\.ssh\` 目录，配置一次几处都能用。

### 1. 安装工具

1. 安装 [Git for Windows](https://git-scm.com/download/win)，一路默认即可。
2. 安装 uv：
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. **关掉 PowerShell 再重新打开**，执行 `ssh -V`、`git --version`、`uv --version` 确认三个都能用。
   `ssh` 找不到的话：设置 → 系统 → 可选功能 → 添加 "OpenSSH 客户端"。

### 2. 放好密钥和 ssh 配置

进入解压出来的目录（例如 `cd $HOME\Downloads\@NAME@-access`）执行：

```powershell
New-Item -ItemType Directory -Force "$HOME\.ssh" | Out-Null
Copy-Item swarm_lb_@NAME@_ed25519, swarm_lb_@NAME@_ed25519.pub "$HOME\.ssh\"
Get-Content ssh_config.txt | Add-Content -Encoding ascii "$HOME\.ssh\config"

# 私钥只能自己读，否则 Windows 的 ssh 会报 UNPROTECTED PRIVATE KEY FILE 并拒绝使用
$k = "$HOME\.ssh\swarm_lb_@NAME@_ed25519"
icacls $k /inheritance:r
icacls $k /grant:r "$($env:USERNAME):(R)"
```

### 3. 获取代码、放好环境参数

**仓库不要放在中文或带空格的路径下**：

```powershell
git config --global user.name  "@NAME@"          # 部署记录里会显示这个名字
git config --global user.email "<你的邮箱>"
git config --global core.autocrlf false           # 保持 LF 换行，否则部署到 manager 的 bash 脚本会报 $'\r' 错误
cd D:\projects                                    # 换成你想放的位置
git clone https://github.com/Wk-P/swarm-lb.git
Copy-Item $HOME\Downloads\@NAME@-access\@ENV@.env swarm-lb\envs\@ENV@.env   # 路径换成你的解压目录
cd swarm-lb
uv sync
```

### 4. 验证

见下一节。

---

## 验证

在仓库目录里执行（Linux/macOS 用终端，Windows 用 PowerShell，命令一样）：

```bash
# 4 个节点都能免密登录。第一次连接会问是否信任主机指纹，输入 yes
@SSH_CHECKS@

ssh @MANAGER_SSH@ "sudo -n true && echo sudo OK"
uv run ops/local/remote.py @ENV@                   # 能列出集群内脚本，说明 env 文件没问题
uv run ops/local/remote.py @ENV@ check-stack.sh    # 能看到 collect_data_stack 的 3 个服务
```

全部通过后，浏览器打开 `http://<manager IP>:5000/`，能看到运维界面就说明配置完成了。

## 之后怎么做

- 改代码、部署：看仓库 README 的「三、代码改动、部署与 GitHub」。流程是 **改仓库 → commit → push → `uv run ops/local/deploy_host.py @ENV@`**。
- **不要直接在 manager 上改代码**，下次部署会被覆盖。`~/appv8`、`~/apps` 是旧目录，已经没有服务从那里运行。
- 部署需要 GitHub 仓库的 push 权限，找仓库所有者把你加为 collaborator。
- 启停服务：运维界面上点按钮。启停别人的服务会弹窗确认，操作前先打个招呼。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `Permission denied (publickey)` | 检查 `~/.ssh/config` 里的 `IdentityFile` 路径和私钥文件是否存在；Windows 检查是否执行了上面的 `icacls` |
| `UNPROTECTED PRIVATE KEY FILE` | Linux/macOS：`chmod 600 ~/.ssh/swarm_lb_@NAME@_ed25519`；Windows：执行上面的 `icacls` |
| `ssh: connect to host … timed out` | 确认本机在集群所在的 192.168.1.x 网段，能 `ping` 通 manager |
| `错误：未指定环境` / `找不到 envs/@ENV@.env` | `@ENV@.env` 没放到仓库的 `envs/` 下，或者脚本第一个参数没写 `@ENV@` |
| Windows 上 `uv` / `ssh` 找不到 | 安装后要重新打开 PowerShell；ssh 要在“可选功能”里装 OpenSSH 客户端 |
| 脚本报 `$'\r': command not found` | Windows 把换行改成了 CRLF：执行 `git config --global core.autocrlf false`，删掉仓库重新 clone |
