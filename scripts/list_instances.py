"""列出已配置的 CRM 账套。

用法:
  python list_instances.py

输出 JSON：{code, instances:[{name, base_url}]}。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402


def main() -> int:
    instances = common.load_instances()
    result = {
        "code": "success",
        "instances": [
            {"name": name, "base_url": cfg["base_url"]}
            for name, cfg in instances.items()
        ],
        "instances_file": str(common.INSTANCES_FILE),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
