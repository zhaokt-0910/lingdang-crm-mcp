"""lingdang-crm-setup 共享模块（零依赖，仅 Python 标准库）。

Skill 包结构：
  lingdang-crm-setup/
  ├── SKILL.md
  ├── mcp/                # 内置 v4 MCP server 代码（运行层，零依赖）
  │   ├── server.py
  │   ├── config.py
  │   ├── crm_client.py
  │   └── instances.json  # 账套注册表（只存账套名+地址，不存密码）
  ├── scripts/            # 本目录脚本（配置向导）
  └── references/
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── 路径定位：scripts 的上一级 = Skill 包根，mcp/ 为其内置 MCP 运行层 ──
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PACKAGE_ROOT / "mcp"
INSTANCES_FILE = MCP_DIR / "instances.json"

TIMEOUT = 8.0  # 探活超时（秒）


# ═══════════════ 账套注册表（instances.json） ═══════════════

def load_instances() -> dict[str, dict[str, str]]:
    """读取账套注册表。返回 {账套名: {"base_url": 地址}}。"""
    instances: dict[str, dict[str, str]] = {}
    if not INSTANCES_FILE.exists():
        return instances
    try:
        data = json.loads(INSTANCES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return instances
        raw = data.get("instances", data)
        if not isinstance(raw, dict):
            return instances
        for name, cfg in raw.items():
            if isinstance(cfg, dict) and cfg.get("base_url"):
                instances[str(name)] = {"base_url": str(cfg["base_url"]).rstrip("/")}
    except (json.JSONDecodeError, OSError):
        instances = {}
    return instances


def save_instance(name: str, base_url: str) -> None:
    """写入/更新一个账套（保留其它条目），不落盘密码。"""
    instances = load_instances()
    instances[name] = {"base_url": base_url.rstrip("/")}
    payload = {"version": 1, "instances": instances}
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    INSTANCES_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def remove_instance(name: str) -> bool:
    """删除一个账套。返回是否存在并删除。"""
    instances = load_instances()
    if name not in instances:
        return False
    del instances[name]
    payload = {"version": 1, "instances": instances}
    INSTANCES_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True


# ═══════════════ 探活（标准库 urllib，无第三方依赖） ═══════════════

def _http_error_detail(e: urllib.error.HTTPError) -> str:
    try:
        return f"HTTP {e.code}"
    except Exception:  # noqa: BLE001
        return "HTTP 错误"


def probe(base_url: str, timeout: float = TIMEOUT) -> tuple[bool, str]:
    """探活：首页可达 + 登录接口存在（返回 JSON）。

    返回 (是否可用, 详情文本)。
    """
    base = base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return False, "地址必须以 http:// 或 https:// 开头"

    # 1. 首页
    status = 0
    try:
        with urllib.request.urlopen(f"{base}/", timeout=timeout) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        return False, f"首页访问失败 {_http_error_detail(e)}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, f"连接失败: {reason}"

    # 2. 登录接口：空 body POST 应返回 JSON（参数缺失类提示），证明端点存在
    req = urllib.request.Request(
        f"{base}/crmapi/mcp_login.php",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r2:
            body = r2.read(500).decode("utf-8", "replace")
        json.loads(body)
    except urllib.error.HTTPError as e:
        return False, f"登录接口异常 {_http_error_detail(e)}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, f"登录接口连接失败: {reason}"
    except json.JSONDecodeError:
        return False, f"登录接口返回非JSON: {body[:200]}"
    return True, f"首页 HTTP {status}，登录接口可达"


def diagnose(base_url: str, timeout: float = TIMEOUT) -> dict[str, Any]:
    """多级连接诊断，返回结构化结果。"""
    base = base_url.rstrip("/")
    result: dict[str, Any] = {"url": base_url, "ok": False, "steps": []}

    # 步骤1：地址格式
    if not base.startswith(("http://", "https://")):
        result["steps"].append({"step": "地址格式", "ok": False, "detail": "必须以 http:// 或 https:// 开头"})
        return result
    result["steps"].append({"step": "地址格式", "ok": True, "detail": "格式正确"})

    # 步骤2：域名解析
    host = base.split("/")[2].split(":")[0]
    try:
        socket.getaddrinfo(host, None)
        result["steps"].append({"step": "域名解析", "ok": True, "detail": f"{host} 解析成功"})
    except socket.gaierror as e:
        result["steps"].append({"step": "域名解析", "ok": False, "detail": f"域名解析失败: {e}"})
        return result

    # 步骤3：HTTP 首页
    try:
        with urllib.request.urlopen(f"{base}/", timeout=timeout) as r:
            result["steps"].append({"step": "首页访问", "ok": r.status < 400, "detail": f"HTTP {r.status}"})
    except urllib.error.HTTPError as e:
        result["steps"].append({"step": "首页访问", "ok": False, "detail": f"HTTP {e.code}"})
        return result
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        result["steps"].append({"step": "首页访问", "ok": False, "detail": f"连接失败: {reason}"})
        return result

    # 步骤4：登录接口
    req = urllib.request.Request(
        f"{base}/crmapi/mcp_login.php",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r2:
            body = r2.read(500).decode("utf-8", "replace")
        try:
            json.loads(body)
            result["steps"].append({"step": "登录接口", "ok": True, "detail": "接口存在并返回 JSON"})
        except json.JSONDecodeError:
            result["steps"].append({"step": "登录接口", "ok": False, "detail": f"接口返回非JSON: {body[:200]}"})
            return result
    except urllib.error.HTTPError as e:
        result["steps"].append({"step": "登录接口", "ok": False, "detail": f"接口异常 HTTP {e.code}"})
        return result
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        result["steps"].append({"step": "登录接口", "ok": False, "detail": f"连接失败: {reason}"})
        return result

    result["ok"] = True
    return result


# ═══════════════ mcp.json 预置（WorkBuddy 连接器条目） ═══════════════

def _can_run_server(python_path: str) -> bool:
    """检查指定 Python 是否可用（零依赖版要求 Python 3.8+，无需任何第三方包）。"""
    try:
        result = subprocess.run(
            [python_path, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _find_server_python(preferred: str | None = None) -> tuple[str, str | None]:
    """自动检测能运行 MCP server 的 Python 解释器（v4 零依赖：任意 Python 3.8+ 均可）。

    候选顺序：显式传入路径 → WorkBuddy 内置 Python → 当前解释器。
    返回 (python_path, warning_msg)。warning_msg 为 None 表示检测通过。
    """
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)

    # WorkBuddy 内置 Python（各版本目录，取版本号最大的）
    wb_root = Path.home() / ".workbuddy" / "binaries" / "python" / "versions"
    if wb_root.exists():
        try:
            pythons = sorted(
                wb_root.glob("*/python.exe"),
                key=lambda p: p.parent.name,
                reverse=True,
            )
            candidates.extend(str(p) for p in pythons)
        except OSError:
            pass

    # 回退到当前解释器
    candidates.append(sys.executable)

    seen: set[str] = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        if _can_run_server(p):
            return p, None

    # 全部失败：回退到 preferred（或 sys.executable），并附加警告
    fallback = preferred or sys.executable
    return (
        fallback,
        f"警告：未找到可用的 Python 3.8+（尝试了 {len(seen)} 个候选）。"
        f"已回退到 {fallback}，连接器可能无法启动。",
    )


def ensure_mcp_json(python_cmd: str | None = None) -> dict[str, Any]:
    """确保 ~/.workbuddy/mcp.json 中存在 lingdang-crm-auth 条目。

    只做 merge，不覆盖其它连接器。返回变更信息。
    python_cmd 为 None 时自动检测可用的 Python 解释器。
    """
    detected_python, warning = _find_server_python(python_cmd)

    cfg_path = Path.home() / ".workbuddy" / "mcp.json"
    data: dict[str, Any] = {"mcpServers": {}}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {"mcpServers": {}}
        if not isinstance(data, dict):
            data = {"mcpServers": {}}

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers

    entry = {
        "command": detected_python,
        "args": [str(MCP_DIR / "server.py")],
        "env": {},
        "disabled": False,
    }
    existing = servers.get("lingdang-crm-auth")
    changed = existing != entry
    servers["lingdang-crm-auth"] = entry
    if changed:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result: dict[str, Any] = {
        "changed": changed,
        "path": str(cfg_path),
        "entry": servers.get("lingdang-crm-auth"),
        "server_py": str(MCP_DIR / "server.py"),
        "python": detected_python,
    }
    if warning:
        result["warning"] = warning
    return result
