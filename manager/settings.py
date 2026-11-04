"""本机私有参数：从环境变量读取，缺失时回退读取仓库根目录的 .env（不进 git，模板见 .env.example）。

systemd 启动时已通过 EnvironmentFile 注入；手动运行时（python manager_xxx.py）由这里加载 .env。
"""
import os
from pathlib import Path

_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

# manager 通过 SSH 读取 worker 资源用的账号
NODE_SSH_USER = os.environ.get("NODE_SSH_USER", "pi")
NODE_SSH_PASSWORD = os.environ.get("NODE_SSH_PASSWORD", "")
if not NODE_SSH_PASSWORD:
    print("[settings] 警告：未设置 NODE_SSH_PASSWORD（见 .env.example），SSH 到 worker 会失败")
