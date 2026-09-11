"""灵当CRM MCP Server v4 — 配置加载（零依赖：纯标准库，无 pydantic-settings）。

与 v3 的区别：
  - 不再依赖 pydantic-settings，改用「进程环境变量 > .env 文件 > 默认值」简单合并
  - 多账套注册表 instances.json 逻辑保持不变（热重载，只存账套名+地址）

环境变量优先级（高 → 低）：
  1. WorkBuddy 连接器 mcp.json 的 env 字段
  2. 本目录 .env 文件
  3. 下方代码默认值
"""

import json
import os
from functools import lru_cache
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent
_INSTANCES_FILE = _PROJECT_DIR / "instances.json"
_ENV_FILE = _PROJECT_DIR / ".env"


# ═══════════════════ 极简 .env 解析 ═══════════════════

def _load_dotenv() -> dict[str, str]:
    """读取 .env 文件：KEY=VALUE，支持 # 注释与空行。"""
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


@lru_cache
def get_settings() -> dict:
    """读取配置。返回 dict：{crm_base_url, crm_http_timeout, session_expires}。"""
    file_env = _load_dotenv()

    def _get(key: str, default: str) -> str:
        return os.environ.get(key) or file_env.get(key) or default

    return {
        "crm_base_url": _get("CRM_BASE_URL", "").rstrip("/"),
        "crm_http_timeout": float(_get("CRM_HTTP_TIMEOUT", "15.0")),
        "session_expires": int(_get("SESSION_EXPIRES", "86400")),
    }


# ═══════════════════ 多账套注册表（instances.json） ═══════════════════
#
# 注册表只存「账套名 + 访问地址」，不存密码（密码由 login 工具会话内提供）。
# 文件热重载：每次调用重新读取，客户在对话中加账套后无需重启连接器。
# 兼容策略：instances.json 中未定义 default 账套时，自动用环境变量 CRM_BASE_URL 兜底。


def _read_file_instances() -> dict[str, dict]:
    """只读 instances.json 文件内容（不含 env 兜底）。"""
    instances: dict[str, dict] = {}
    if not _INSTANCES_FILE.exists():
        return instances
    try:
        data = json.loads(_INSTANCES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return instances
        raw = data.get("instances", data)  # 兼容 {"version":1,"instances":{...}} 与直接 {...}
        if not isinstance(raw, dict):
            return instances
        for name, cfg in raw.items():
            if isinstance(cfg, dict) and cfg.get("base_url"):
                instances[str(name)] = {"base_url": str(cfg["base_url"]).rstrip("/")}
    except (json.JSONDecodeError, OSError):
        instances = {}
    return instances


def load_instances() -> dict[str, dict]:
    """读取账套注册表（含 env 兜底的 default 账套）。返回 {账套名: {"base_url": 地址}}。"""
    instances = _read_file_instances()
    s = get_settings()
    if "default" not in instances and s["crm_base_url"]:
        instances["default"] = {"base_url": s["crm_base_url"].rstrip("/")}
    return instances


def save_instance(name: str, base_url: str) -> None:
    """写入/更新一个账套（保留其它条目），不落盘密码。"""
    instances = _read_file_instances()
    instances[name] = {"base_url": base_url.rstrip("/")}
    payload = {"version": 1, "instances": instances}
    _INSTANCES_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
