"""纯标准库实现的 MCP stdio 服务器核心（零第三方依赖，Python 3.8+）。

只实现 WorkBuddy 连接器 / 官方 MCP 客户端实际用到的最小协议子集：
  - initialize 握手（回显客户端声明的协议版本，保证任意客户端兼容）
  - notifications/initialized（通知，不响应）
  - tools/list / tools/call
  - ping

★ 传输格式自动适配（两种 stdio 协议并存）：
  - Content-Length 帧（LSP 风格）：WorkBuddy / MCP TypeScript SDK / 官方 Python SDK ≤1.x 使用
  - NDJSON（每行一个 JSON）：官方 Python SDK 2.0 使用
  以客户端发来的第一帧自动探测并锁定，输出格式跟随客户端。

使用方式（由 server.py 调用）：
    registry = ToolRegistry()
    @registry.register("login", "登录", {"type": "object", ...})
    def login(username: str, password: str) -> str: ...
    run_stdio(registry, server_info={"name": "...", "version": "..."})

工具函数签名须与注册时的 inputSchema 属性名一致，返回字符串（JSON 文本）。
"""

from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO, Optional

# 客户端未在 initialize 中声明协议版本时回此值（尽量兼容旧客户端）
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


class ToolRegistry:
    """工具注册表：name → {description, inputSchema, fn}。"""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    def register(self, name: str, description: str, input_schema: dict[str, Any]):
        """装饰器：注册一个 MCP 工具。"""

        def deco(fn) -> Any:
            self._tools[name] = {
                "description": description,
                "inputSchema": input_schema,
                "fn": fn,
            }
            return fn

        return deco

    def list(self) -> list[dict[str, Any]]:
        return [
            {"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
            for n, t in self._tools.items()
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未知工具: {name}")
        return str(tool["fn"](**arguments))


# ═══════════════════ 传输层：双协议自动适配 ═══════════════════

def _safe_loads(text: str) -> Optional[dict[str, Any]]:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError:
        return None
    return msg if isinstance(msg, dict) else None


def _parse_header_line(line: bytes) -> tuple[str, str]:
    text = line.decode("utf-8", "replace").strip()
    key, _, value = text.partition(":")
    return key.strip().lower(), value.strip()


class _Transport:
    """自动探测并适配 Content-Length 帧 与 NDJSON 两种输入，输出跟随客户端格式。"""

    def __init__(self, fin: BinaryIO, fout: BinaryIO) -> None:
        self.fin = fin
        self.fout = fout
        self.mode: Optional[str] = None  # None | "framed" | "ndjson"

    # ── 读取 ──

    def read(self) -> Optional[dict[str, Any]]:
        if self.mode is None:
            msg = self._read_first()
        elif self.mode == "framed":
            msg = self._read_framed()
        else:
            msg = self._read_ndjson()
        return msg

    def _read_first(self) -> Optional[dict[str, Any]]:
        first = self.fin.readline()
        if not first:
            return None  # EOF
        if first.rstrip(b"\r\n").lower().startswith(b"content-length"):
            self.mode = "framed"
            headers: dict[str, str] = dict([_parse_header_line(first)])
            while True:
                line = self.fin.readline()
                if not line:
                    return None
                if line in (b"\r\n", b"\n", b"\r"):
                    break
                key, value = _parse_header_line(line)
                headers[key] = value
            return self._read_body(headers.get("content-length"))
        self.mode = "ndjson"
        return _safe_loads(first.decode("utf-8", "replace"))

    def _read_framed(self) -> Optional[dict[str, Any]]:
        headers: dict[str, str] = {}
        while True:
            line = self.fin.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n", b"\r"):
                break
            key, value = _parse_header_line(line)
            headers[key] = value
        return self._read_body(headers.get("content-length"))

    def _read_body(self, length: Optional[str]) -> Optional[dict[str, Any]]:
        try:
            n = int(length or "0")
        except ValueError:
            return None
        if n <= 0:
            return None
        body = self.fin.read(n)
        return _safe_loads(body.decode("utf-8", "replace"))

    def _read_ndjson(self) -> Optional[dict[str, Any]]:
        line = self.fin.readline()
        if not line:
            return None
        return _safe_loads(line.decode("utf-8", "replace"))

    # ── 写入 ──

    def write(self, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if self.mode == "ndjson":
            self.fout.write(body + b"\n")
        else:  # 默认帧协议（未探测到输入时也按此输出，兼容绝大多数客户端）
            self.fout.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body)
        self.fout.flush()


# ═══════════════════ 请求处理 ═══════════════════

def _handle_initialize(params: Any, server_info: dict[str, Any]) -> dict[str, Any]:
    """回显客户端协议版本：任何客户端声明的版本都在其自身支持范围内，保证握手成功。"""
    protocol_version = DEFAULT_PROTOCOL_VERSION
    if isinstance(params, dict) and isinstance(params.get("protocolVersion"), str):
        protocol_version = params["protocolVersion"]
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": server_info,
    }


def _handle_tools_call(registry: ToolRegistry, params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise TypeError("tools/call 缺少 params")
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise TypeError("arguments 必须是 JSON 对象")
    if not isinstance(name, str):
        raise TypeError("缺少工具名 name")

    text = registry.call(name, arguments)

    # 工具返回 JSON 且 code == "error" → 按 MCP 规范标记 isError，便于客户端感知失败
    is_error = False
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("code") == "error":
            is_error = True
    except json.JSONDecodeError:
        pass

    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def run_stdio(
    registry: ToolRegistry,
    server_info: dict[str, Any],
    stream_in: Optional[BinaryIO] = None,
    stream_out: Optional[BinaryIO] = None,
) -> None:
    """主循环：从 stdin 读请求、执行、写响应，直到 stdin 关闭。"""
    fin = stream_in if stream_in is not None else sys.stdin.buffer
    fout = stream_out if stream_out is not None else sys.stdout.buffer
    transport = _Transport(fin, fout)

    while True:
        msg = transport.read()
        if msg is None:
            return  # EOF → 退出

        if msg.get("jsonrpc") != "2.0" or "id" not in msg:
            # 通知（如 notifications/initialized）不需要响应
            continue

        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params")

        result: Any = None
        error: Optional[dict[str, Any]] = None

        try:
            if method == "initialize":
                result = _handle_initialize(params, server_info)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": registry.list()}
            elif method == "tools/call":
                result = _handle_tools_call(registry, params)
            else:
                error = {"code": -32601, "message": f"Method not found: {method}"}
        except KeyError as e:
            error = {"code": -32602, "message": str(e)}
        except TypeError as e:
            error = {"code": -32602, "message": f"参数错误: {e}"}
        except Exception as e:  # noqa: BLE001 — 兜底：任何异常都转 JSON-RPC 错误
            error = {"code": -32603, "message": f"内部错误: {e}"}

        resp: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            resp["error"] = error
        else:
            resp["result"] = result
        transport.write(resp)
