"""注册（连接）一个 CRM 账套：探活 → 写 instances.json → 预置 mcp.json 连接器条目。

用法:
  python setup.py --name 上海账套 --url http://192.168.1.100/crm [--python <python解释器路径>]

说明:
  - 只存账套名+地址，不存密码（密码由 MCP login 工具会话内提供）。
  - --python 用于指定 MCP server 的 Python 解释器；缺省自动检测（优先 WorkBuddy 内置 Python，v4 零依赖任意 Python 3.8+ 均可）。
  - 输出 JSON，供 AI 解析后转述给客户。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="注册灵当CRM账套")
    p.add_argument("--name", required=True, help="账套名称（唯一，如 上海账套）")
    p.add_argument("--url", required=True, help="CRM访问地址（如 http://192.168.1.100/crm）")
    p.add_argument("--python", default=None, help="指定 MCP server 的 Python 解释器（自动检测）")
    p.add_argument("--skip-probe", action="store_true", help="跳过探活（调试用）")
    args = p.parse_args()

    name = args.name.strip()
    url = args.url.strip()
    if not name or not url:
        print(json.dumps({"code": "error", "msg": "账套名与访问地址不能为空"}, ensure_ascii=False))
        return 1

    result: dict = {"code": "success", "name": name, "base_url": url}

    # 1. 探活
    if args.skip_probe:
        result["probe"] = "已跳过（调试模式）"
    else:
        ok, detail = common.probe(url)
        result["probe"] = detail
        if not ok:
            result["code"] = "error"
            result["msg"] = f"账套地址探测失败，未保存: {detail}"
            print(json.dumps(result, ensure_ascii=False))
            return 1

    # 2. 写 instances.json（注册表）
    common.save_instance(name, url)
    result["instances_file"] = str(common.INSTANCES_FILE)
    result["msg"] = f"账套 '{name}' 已保存并探测通过"

    # 3. 预置 mcp.json 连接器条目（merge，不覆盖其它连接器）
    mcp_info = common.ensure_mcp_json(args.python)
    result["mcp_json"] = mcp_info
    if mcp_info.get("warning"):
        result["warning"] = mcp_info["warning"]
    if mcp_info["changed"]:
        result["msg"] += "；已写入 WorkBuddy 连接器条目，请在连接器管理页对 lingdang-crm-auth 点一次「信任」启用"
    else:
        result["msg"] += "；连接器条目已存在"

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
