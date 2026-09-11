"""连接诊断：对 CRM 地址做多级检查（地址格式/域名解析/首页/登录接口）。

用法:
  python diagnose.py --url http://192.168.1.100/crm

输出 JSON：{url, ok, steps:[{step, ok, detail}]}，供 AI 定位问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="诊断灵当CRM连接")
    p.add_argument("--url", required=True, help="CRM访问地址（如 http://192.168.1.100/crm）")
    args = p.parse_args()

    result = common.diagnose(args.url.strip())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
