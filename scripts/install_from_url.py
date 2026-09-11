#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 URL 一键安装灵当CRM 交付包（零依赖，仅标准库）。

用法:
  python install_from_url.py --url https://example.com/lingdang-crm-setup.zip
  python install_from_url.py --url https://example.com/xxx.zip --skills-dir C:/Users/xx/.workbuddy/skills

流程:
  1) 下载 zip 到临时文件
  2) 校验是合法 zip 且包含 lingdang-crm-setup/SKILL.md
  3) 解压到目标 skills 目录（默认 ~/.workbuddy/skills）
  4) 输出 JSON 结果（供 AI 解析）

安全:
  - 拒绝 zip 内路径穿越 (../) 与绝对路径条目
  - 只解压文件，不执行任何内容
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

TOP_DIR = "lingdang-crm-setup"
SKILL_ENTRY = f"{TOP_DIR}/SKILL.md"


def _encode_url(url: str) -> str:
    """对 URL 中的非 ASCII 字符（如中文文件名）做百分号编码。"""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=~-._?")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def download(url: str, dest: str, timeout: int = 60) -> None:
    """下载 URL 到本地文件。"""
    req = urllib.request.Request(_encode_url(url), headers={"User-Agent": "WorkBuddy-Setup/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        if total == 0:
            raise ValueError("下载内容为空（地址可能返回了空文件）")


def safe_extract(zf: zipfile.ZipFile, dest_dir: Path) -> int:
    """安全解压：拒绝路径穿越与绝对路径，返回解压文件数。"""
    count = 0
    for member in zf.infolist():
        # 归一化并检查路径安全
        name = member.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"发现非法路径条目，已中止：{member.filename}")
        if os.path.isabs(name):
            raise ValueError(f"发现绝对路径条目，已中止：{member.filename}")
        # 目录条目
        if member.is_dir():
            (dest_dir / name).mkdir(parents=True, exist_ok=True)
            continue
        target = dest_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as dst:
            dst.write(src.read())
        count += 1
    return count


def install(url: str, skills_dir: Path) -> dict:
    """执行完整安装流程，返回结果字典。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        download(url, tmp_path)

        if not zipfile.is_zipfile(tmp_path):
            return {"code": "error", "msg": "下载的文件不是有效的 zip 压缩包", "url": url}

        with zipfile.ZipFile(tmp_path) as zf:
            names = zf.namelist()
            if SKILL_ENTRY not in names:
                return {
                    "code": "error",
                    "msg": f"不是灵当CRM 交付包：缺少 {SKILL_ENTRY}（共 {len(names)} 个条目）",
                    "url": url,
                }
            # 先探测一遍安全性，再落盘
            for member in zf.infolist():
                name = member.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if any(p == ".." for p in parts) or os.path.isabs(name):
                    return {
                        "code": "error",
                        "msg": f"交付包含非法路径条目，已拒绝安装：{member.filename}",
                        "url": url,
                    }
            n = safe_extract(zf, skills_dir)

        return {
            "code": "success",
            "msg": f"安装完成：已解压 {n} 个文件到 {skills_dir / TOP_DIR}",
            "installed_dir": str(skills_dir / TOP_DIR),
            "skill_entry": str(skills_dir / SKILL_ENTRY),
            "url": url,
        }
    except Exception as e:  # noqa: BLE001
        return {"code": "error", "msg": f"安装失败：{e}", "url": url}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="从 URL 一键安装灵当CRM 交付包")
    parser.add_argument("--url", required=True, help="交付包 zip 的下载地址")
    parser.add_argument(
        "--skills-dir",
        default=str(Path.home() / ".workbuddy" / "skills"),
        help="WorkBuddy skills 目录（缺省 ~/.workbuddy/skills）",
    )
    args = parser.parse_args()

    result = install(args.url, Path(args.skills_dir))
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["code"] == "success" else 1)


if __name__ == "__main__":
    main()
