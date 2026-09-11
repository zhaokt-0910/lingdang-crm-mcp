"""灵当CRM MCP Server v4 — 零依赖版（纯 Python 标准库，可在任意 Python 3.8+ 直接运行）。

与 v3 的区别：
  - 不再依赖 mcp / httpx / pydantic-settings 三个第三方包
  - MCP stdio 协议由自研 mcp_stdio.py 实现（JSON-RPC 2.0 over stdio）
  - HTTP 调用由 urllib 实现（crm_client.py）
  - 工具集在 v3 基础上扩展（13 个），新增 search_modules / query_module_summary / resolve_reference / update_record，多账套注册表 instances.json 热重载

v4.7 更新：
  - 新增公海客户五件套：get_public_pool_list（公海池列表）/ allocate_account（领取/分配）/
    release_account（释放）/ delay_account（延期）/ convert_account（变更公海/转普通）
  - create_record 新增 is_public_account 参数：新建客户时可直接指定公海状态（unassigned/assigned/normal）
  - PHP 端复用官方 cls_AllocateAccount/cls_ReleaseAccount/cls_DelayAccount/cls_ConvertAccount 类，与 CRM 前端行为一致

v4.6 更新：
  - 新增工作汇报三件套：workreport_list（列表）/ workreport_create（新建日志/周计划/月计划）/
    workreport_update（编辑）——工作汇报非标准 CRMEntity 模块，PHP 端新增专用 API，
    内部直接调用官方 WorkReportController::getList/saveSendLog/editReport

v4.5 更新：
  - 新增 update_record 工具：编辑已有记录（表头增量更新 + 明细行级增删改，走 PHP update() 官方编辑链路）
  - PHP 端 update() 已实现：mode=edit 装载旧值、表头增量覆盖、明细行级语义（带 lineitem_id=更新、不带=追加、delete_lineitem_ids=删行）

v4.4 更新：
  - resolve_reference 增强：工具描述明确人名/客户名/单据号查找策略，支持「我」→当前用户 user_id 的快捷映射
  - query_module_data / query_module_summary 增加字段口径确认引导：模糊业务词（业绩/销售额等）必须先 get_module_fields 确认 fieldName
  - PHP 端新增 resolveReference（名称模糊查找）+ querySummary（聚合统计）两个接口实现
  - create() 新增明细行验证：save 后检查实际明细行数，不足时返回 record_id 并提示用户自行处理

工具集（26 个）：
  setup_instance        — 注册 CRM 账套（探活通过后保存）
  list_instances        — 列出已配置账套
  login                 — 用户名+密码登录（获取 session_token，后续工具依赖此步骤）
  logout                — 注销当前登录
  list_modules          — 列出所有可用模块
  search_modules        — 按关键词搜索模块（避免拉全量 300+ 模块列表）
  get_module_fields     — 获取指定模块的字段清单（compact 模式只返回 fieldName+zhlabel）
  resolve_reference     — 名称/编号 → record_id 查找器（模糊匹配，支持 Users/Accounts 等）
  query_module_data     — 通用条件查询（字段白名单 + 过滤 + 排序 + 分页）
  query_module_summary  — 聚合查询（count + sum，不返明细，统计类问题首选）
  get_record            — 单条记录详情（含表体明细）
  create_record         — 动态创建记录（带明细时自动验证明细行数，失败时提示用户处理）
  update_record         — 编辑已有记录（表头增量更新 + 明细行级增删改，审批中/已批准拒绝编辑）
  get_erp_stock        — ERP 实时库存查询（按产品编码批量查询）
  get_public_pool_list  — 公海池配置列表（查看可用公海池 ID）
  allocate_account      — 领取/分配公海客户
  release_account       — 释放公海客户（释放回公海未分配）
  delay_account         — 延期公海客户（延长保护期）
  convert_account       — 公海分配/变更公海（转公海/转普通/池间变更）
  workreport_list       — 工作汇报列表（日志/周计划/月计划，非标准模块专用）
  workreport_create     — 新建工作汇报（日志/周计划/月计划，含编号/接收人/提醒）
  workreport_update     — 编辑工作汇报（仅创建者、未点评可编辑，写编辑痕迹）

使用前提：
  1. 部署 PHP 侧文件到 CRM：crmapi/mcp_login.php、crmapi/modules/Mcp_auth_api.php、Mcp_api.php
  2. 在 WorkBuddy 连接器 mcp.json 中以本文件为入口启动（无需任何第三方依赖）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from config import get_settings, load_instances, save_instance
from crm_client import CRMConfig, CRMError, CRMNotLoggedInError, LingdangClient
from mcp_stdio import ToolRegistry, run_stdio

# stdio 模式 → 日志输出到 stderr（stdout 被 MCP 帧占用）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("lingdang-crm-mcp")

# ── 工具注册表（零依赖 MCP 核心） ──
registry = ToolRegistry()


# ── 多账套客户端池 ──
# 每个账套一个独立 LingdangClient（独立 base_url + 独立 session），按账套名懒加载。
_clients: dict[str, LingdangClient] = {}


def _get_client(instance: str) -> LingdangClient:
    """按账套名获取客户端（未创建则从注册表懒加载创建）。"""
    client = _clients.get(instance)
    if client is None:
        instances = load_instances()
        if instance not in instances:
            raise CRMError(
                f"账套 '{instance}' 未配置。请先调用 setup_instance 注册该账套，或用 list_instances 查看已配置的账套"
            )
        s = get_settings()
        client = LingdangClient(CRMConfig(
            base_url=instances[instance]["base_url"],
            http_timeout=s["crm_http_timeout"],
        ))
        _clients[instance] = client
    return client


def _probe_base_url(base_url: str) -> tuple[bool, str]:
    """探活：首页可达 + 登录接口存在（返回 (是否可用, 详情)）。"""
    timeout = get_settings()["crm_http_timeout"]
    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=timeout) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        return False, f"首页访问失败 HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, f"连接失败: {reason}"
    # 登录接口：空 body POST 应返回 JSON（参数缺失类提示），证明端点存在
    req = urllib.request.Request(
        f"{base_url}/crmapi/mcp_login.php",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r2:
            body = r2.read(500).decode("utf-8", "replace")
        json.loads(body)
    except urllib.error.HTTPError as e:
        return False, f"登录接口异常 HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, f"登录接口连接失败: {reason}"
    except json.JSONDecodeError:
        return False, f"登录接口返回非JSON: {body[:200]}"
    return True, f"首页 HTTP {status}，登录接口可达"


def _run(instance: str, func: str, args: dict[str, Any]) -> str:
    """统一执行入口：调用客户端方法，异常转 JSON 文本返回。

    错误分类与 AI 引导策略：
      - 权限不足 → 建议停止并告知用户联系管理员开通
      - HTTP 5xx / 空响应（后端服务异常）→ 建议停止并告知用户检查服务器日志
      - 登录失效 → 提示重新 login
      - 其他业务错误 → 原样返回错误信息（可能是参数问题，AI 可调整参数重试）
    """
    started = time.monotonic()
    try:
        client = _get_client(instance)
        result = getattr(client, func)(**args)
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info("tool=%s instance=%s ok %.0fms", func, instance, elapsed_ms)
        return json.dumps(result, ensure_ascii=False, default=str)
    except CRMNotLoggedInError as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.warning("tool=%s instance=%s not_logged_in %.0fms: %s", func, instance, elapsed_ms, e)
        return json.dumps({
            "code": "error",
            "msg": f"未登录或 session 已过期: {e}",
            "suggestion": f"请调用 login 工具重新登录账套 '{instance}'，然后再重试当前操作。",
        }, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — 工具错误统一转 JSON 返回给调用方
        elapsed_ms = (time.monotonic() - started) * 1000
        msg = str(e)
        logger.warning("tool=%s instance=%s error %.0fms: %s", func, instance, elapsed_ms, msg)
        # 区分错误类型，给出不同的 AI 引导
        is_permission_error = ("权限" in msg or "未开通" in msg)
        is_server_error = ("HTTP 5" in msg or "Fatal" in msg or "响应体为空" in msg
                           or "连接失败" in msg or "网络错误" in msg)
        resp: dict[str, Any] = {"code": "error", "msg": msg}
        if is_permission_error:
            resp["suggestion"] = (
                "⛔ 权限不足，必须立即停止。"
                "不要尝试其他途径获取数据，不要重试，不要换参数。"
                "直接回复用户：「您的账号未开通 MCP 接口权限，请联系 CRM 管理员开通后再使用。」"
                "除此之外不需要做任何其他操作。"
            )
        elif is_server_error:
            resp["suggestion"] = (
                "这是 CRM 后端服务异常，请勿重复重试相同请求。"
                "应直接告知用户：CRM 服务器发生内部错误，建议联系管理员检查 PHP 错误日志（error_log）"
                "以定位具体原因（通常是 SQL 字段不匹配或模块配置问题）。"
            )
        return json.dumps(resp, ensure_ascii=False)


# ════════════════════ 工具定义 ════════════════════

@registry.register(
    "setup_instance",
    "注册一个新的 CRM 账套（探活通过后保存）。name 为账套名称，需唯一（如\"上海账套\"）；base_url 为 CRM 访问地址（如 http://192.168.1.100/crm，末尾不要带 /）。保存后即可用该名称作为 login 等工具的 instance 参数。会先做连接探测，地址不可达会拒绝保存。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "账套名称，需唯一（如 上海账套）"},
            "base_url": {"type": "string", "description": "CRM 访问地址（如 http://192.168.1.100/crm，末尾不要带 /）"},
        },
        "required": ["name", "base_url"],
    },
)
def setup_instance(name: str, base_url: str) -> str:
    """注册一个新的 CRM 账套（探活通过后保存）。"""
    name = (name or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    if not name or not base_url:
        return json.dumps({"code": "error", "msg": "账套名与访问地址不能为空"}, ensure_ascii=False)
    ok, info = _probe_base_url(base_url)
    if not ok:
        return json.dumps({"code": "error", "msg": f"账套地址探测失败，未保存: {info}"}, ensure_ascii=False)
    save_instance(name, base_url)
    logger.info("已注册账套 %s -> %s", name, base_url)
    return json.dumps(
        {"code": "success", "msg": f"账套 '{name}' 已保存并探测通过", "name": name, "base_url": base_url, "probe": info},
        ensure_ascii=False,
    )


@registry.register(
    "list_instances",
    "列出所有已配置的 CRM 账套。返回每套的账套名 name（作为 login 等工具的 instance 参数）、访问地址 base_url、是否已登录 logged_in。首次使用前先调用本工具查看有哪些账套可选。",
    {"type": "object", "properties": {}},
)
def list_instances() -> str:
    """列出所有已配置的 CRM 账套。"""
    instances = load_instances()
    result = []
    for name, cfg in instances.items():
        client = _clients.get(name)
        result.append({
            "name": name,
            "base_url": cfg["base_url"],
            "logged_in": bool(client is not None and client.is_logged_in),
        })
    return json.dumps({"code": "success", "instances": result}, ensure_ascii=False)


@registry.register(
    "login",
    "登录灵当CRM。instance 为账套名（默认 default，可用 list_instances 查看有哪些账套）。username 为CRM系统中的用户账号，password 为密码。登录成功后 session 有效 24 小时，期间可调用其它工具。首次使用必须先调用本工具登录。不同用户登录后只能看到其权限范围内的数据。",
    {
        "type": "object",
        "properties": {
            "username": {"type": "string", "description": "CRM 用户账号"},
            "password": {"type": "string", "description": "密码"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["username", "password"],
    },
)
def login(username: str, password: str, instance: str = "default") -> str:
    """登录灵当CRM（用户名+密码）。"""
    return _run(instance, "login", {"username": username, "password": password})


@registry.register(
    "logout",
    "注销指定账套的当前登录 session。instance 为账套名（默认 default）。注销后需要重新调用 login 才能继续操作该账套。",
    {
        "type": "object",
        "properties": {"instance": {"type": "string", "description": "账套名（默认 default）"}},
    },
)
def logout(instance: str = "default") -> str:
    """注销指定账套的当前登录 session。"""
    return _run(instance, "logout", {})


@registry.register(
    "list_modules",
    "列出指定账套中当前登录用户有权限查看的所有模块。返回 name（模块索引名）和 zhlabel（中文显示名）。"
    "无权限的模块不会出现在列表中；若用户提到的模块不在列表内，告知其无该模块权限即可，不要再尝试操作。"
    "如果已经知道模块名，可直接使用，无需调用本工具。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {"instance": {"type": "string", "description": "账套名（默认 default）"}},
    },
)
def list_modules(instance: str = "default") -> str:
    """列出指定账套中所有可用模块。"""
    result_text = _run(instance, "list_modules", {})
    # 精简返回：去掉 enlabel，节省 token
    try:
        parsed = json.loads(result_text)
        if parsed.get("code") == "success" and isinstance(parsed.get("modules"), list):
            parsed["modules"] = [
                {"name": m.get("name", ""), "zhlabel": m.get("zhlabel", "")}
                for m in parsed["modules"] if isinstance(m, dict)
            ]
        result_text = json.dumps(parsed, ensure_ascii=False, default=str)
    except (json.JSONDecodeError, TypeError):
        pass
    return result_text


@registry.register(
    "search_modules",
    "按关键词搜索模块（仅搜索当前登录用户有权限查看的模块）。keyword 为搜索词（支持多个，任一匹配即可），会同时搜索中文名、英文名和模块索引名。"
    "适用场景：知道要找「客户」「商机」「订单」等但不知道模块索引名时，用本工具搜索——"
    "内部会拉取完整模块列表后按关键词过滤，返回匹配结果（比 list_modules 返回更少，节省后续 token）。"
    "需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "keyword": {"type": "array", "items": {"type": "string"}, "description": "搜索关键词列表，如 [\"商机\", \"销售\"]"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["keyword"],
    },
)
def search_modules(keyword: list[str], instance: str = "default") -> str:
    """按关键词搜索模块。"""
    result_text = _run(instance, "list_modules", {})
    try:
        parsed = json.loads(result_text)
        if parsed.get("code") == "success" and isinstance(parsed.get("modules"), list):
            keywords_lower = [k.lower() for k in keyword]
            matched = [
                {"name": m.get("name", ""), "zhlabel": m.get("zhlabel", "")}
                for m in parsed["modules"]
                if isinstance(m, dict) and any(
                    kw in (m.get("zhlabel", "") + m.get("enlabel", "") + m.get("name", "")).lower()
                    for kw in keywords_lower
                )
            ]
            return json.dumps({"code": "success", "modules": matched, "total": len(matched)}, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    return result_text


@registry.register(
    "get_picklist_values",
    "获取指定模块下拉框/选择类字段的有效值（走 CRM 官方 cls_PickList 链路，与前台下拉框一致）。"
    "不传 fields 时返回该模块全部下拉框字段；也可传 fields 只查指定字段（fieldName 或 columnName 均可）。"
    "【使用时机】create_record/update_record 要填下拉类字段（get_module_fields 中 uitype=15/33 的字段）时，"
    "必须先调本工具获取有效值并从返回的 values 中选取，不得凭猜测填写——无效值会导致保存失败或脏数据。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填，如 Accounts）"},
            "fields": {"type": "array", "items": {"type": "string"}, "description": "可选，只查指定字段（fieldName 或 columnName）；不传返回全部下拉框字段"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module"],
    },
)
def get_picklist_values(module: str, fields: list | None = None, instance: str = "default") -> str:
    """获取指定模块下拉框字段的有效值。"""
    params: dict = {"module": module}
    if fields:
        params["fields"] = fields
    return _run(instance, "get_picklist_values", params)


@registry.register(
    "get_module_fields",
    "获取指定模块的字段清单。"
    "compact=true（默认）时返回 fieldName、zhlabel、type（字段类型）、summable（是否可求和）、required（是否必填）、fieldonly（是否不允许重复），"
    "并按 CRM 新建页布局返回 blocks 区块分组（blockid/blocklabel/create_view/fields），需要按区块展示字段时使用；"
    "compact=false 时返回完整信息（含 columnName/tableName/uitype/typeofData），仅在需要了解数据库列名或数据类型时使用。"
    "【业务口径提示】当用户提到模糊的业务词（如「客户」「订单」「金额」「负责人」等），"
    "请用本工具查 zhlabel 确认对应的 fieldName。"
    "需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "modules": {"type": "array", "items": {"type": "string"}, "description": "模块索引名列表，如 [\"Accounts\",\"SalesOrder\"]"},
            "compact": {"type": "boolean", "description": "精简模式（默认 true），返回 fieldName + zhlabel + type + summable"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["modules"],
    },
)
def get_module_fields(modules: list[str], compact: bool = True, instance: str = "default") -> str:
    """获取指定模块的字段清单（表头 + 表体）。"""
    result_text = _run(instance, "get_module_fields", {"modules": modules})
    # compact 模式增强：保留 fieldName + zhlabel + type + summable，并生成摘要
    if compact:
        try:
            parsed = json.loads(result_text)
            if parsed.get("code") == "success" and isinstance(parsed.get("fields"), dict):
                summaries = []
                for mod_name, mod_data in parsed["fields"].items():
                    if not isinstance(mod_data, dict):
                        continue
                    all_fields = []
                    for section in ("header", "detail"):
                        if section in mod_data and isinstance(mod_data[section], list):
                            compacted = []
                            for f in mod_data[section]:
                                if not isinstance(f, dict):
                                    continue
                                entry = {
                                    "fieldName": f.get("fieldName", ""),
                                    "zhlabel": f.get("zhlabel", ""),
                                    "type": f.get("typeofData", ""),
                                    "summable": f.get("uitype") in (7, 71, 72, 84),
                                    "required": bool(f.get("required", False)),
                                    "fieldonly": bool(f.get("fieldonly", False)),
                                    "isenablebusiness": bool(f.get("isenablebusiness", False)),
                                    "isenablebusinessfuzzyquery": bool(f.get("isenablebusinessfuzzyquery", False)),
                                }
                                compacted.append(entry)
                                all_fields.append(entry)
                            mod_data[section] = compacted
                    # 区块分组精简：保留 blockid/blocklabel/create_view + 每个字段的 fieldName/zhlabel/required，
                    # 供客户端按区块展示字段内容（与 CRM 新建页布局一致）
                    blocks = mod_data.get("blocks")
                    if isinstance(blocks, list):
                        compacted_blocks = []
                        for b in blocks:
                            if not isinstance(b, dict):
                                continue
                            bfields = [
                                {
                                    "fieldName": bf.get("fieldName", ""),
                                    "zhlabel": bf.get("zhlabel", ""),
                                    "required": bool(bf.get("required", False)),
                                    "fieldonly": bool(bf.get("fieldonly", False)),
                                }
                                for bf in (b.get("fields") or [])
                                if isinstance(bf, dict)
                            ]
                            compacted_blocks.append({
                                "blockid": b.get("blockid", ""),
                                "blocklabel": b.get("blocklabel", ""),
                                "create_view": b.get("create_view", ""),
                                "fields": bfields,
                            })
                        mod_data["blocks"] = compacted_blocks
                    # 生成字段摘要：挑选关键字段，生成 AI 易记的简短中文描述
                    summary = _build_field_summary(mod_name, all_fields)
                    # 追加明细保存白名单摘要（AI 创建明细时必须参考）
                    save_fields = mod_data.get("detail_save_fields", [])
                    has_product = mod_data.get("has_product_detail", False)
                    if save_fields:
                        save_names = [f.get("fieldName", "") for f in save_fields if isinstance(f, dict)]
                        product_tag = "产品明细" if has_product else "非产品明细"
                        summary += f"\n  明细可写字段({product_tag}): {', '.join(save_names)}"
                    summaries.append(summary)
                if summaries:
                    parsed["field_summary"] = "\n".join(summaries)
                result_text = json.dumps(parsed, ensure_ascii=False, default=str)
        except (json.JSONDecodeError, TypeError):
            pass
    return result_text


def _build_field_summary(module: str, fields: list[dict]) -> str:
    """为单个模块生成简短字段摘要，便于 AI 快速记忆。

    策略：优先展示可求和字段（统计用）+ 常见引用字段（负责人/时间/状态），
    最多展示 12 个字段，其余用总数概括。
    """
    if not fields:
        return f"{module}: 无字段"

    # 常见引用字段（过滤/排序常用）
    common_refs = {
        "assigned_user_id", "createdtime", "modifiedtime", "status",
        "account_id", "contact_id", "parent_id", "related_to",
    }

    picked: list[str] = []
    seen: set[str] = set()

    # 0) 必填字段最优先（创建记录时必须传入，避免创建时被服务端拒绝）
    for f in fields:
        if f.get("required") and f["fieldName"] not in seen:
            label = f.get("zhlabel", "") or f["fieldName"]
            picked.append(f"{label}({f['fieldName']},必填)")
            seen.add(f["fieldName"])
            if len(picked) >= 4:
                break

    # 1) 可求和字段优先（统计场景最常用）
    for f in fields:
        if f.get("summable") and f["fieldName"] not in seen:
            label = f.get("zhlabel", "") or f["fieldName"]
            picked.append(f"{label}({f['fieldName']},可求和)")
            seen.add(f["fieldName"])
            if len(picked) >= 5:
                break

    # 2) 常见引用字段
    for f in fields:
        fn = f["fieldName"]
        if fn in common_refs and fn not in seen:
            label = f.get("zhlabel", "") or fn
            picked.append(f"{label}({fn})")
            seen.add(fn)
            if len(picked) >= 9:
                break

    # 3) 名称类字段（模块主标识）
    name_keywords = {"name", "no", "subject", "title"}
    for f in fields:
        fn = f["fieldName"]
        if fn not in seen and any(kw in fn for kw in name_keywords):
            label = f.get("zhlabel", "") or fn
            picked.append(f"{label}({fn})")
            seen.add(fn)
            if len(picked) >= 12:
                break

    total = len(fields)
    shown = len(picked)
    summary = f"{module}: " + "、".join(picked) if picked else f"{module}: "
    if total > shown:
        summary += f"。共 {total} 个字段"
    return summary


@registry.register(
    "query_module_data",
    "查询指定模块的记录明细列表。"
    "⚠️ 用户问「多少条/几个/总金额/合计」时请用 query_module_summary（轻量，只返回 count+sum）；"
    "本工具用于需要看逐条明细时使用。"
    "⚠️ 过滤条件中若涉及人名/客户名/单据编号等描述性内容，先用 resolve_reference 查出 record_id 再过滤。"
    "【按明细查询】销售订单等含明细的模块支持用明细字段过滤；例如先 resolve_reference(module=\"Products\") 查产品 ID，"
    "再以 filters={\"productid\": 产品ID} 查询 SalesOrder。明细字段不能放入 fields 返回，需对结果逐条 get_record 查看命中行。"
    "【字段口径确认】当用户提到模糊的业务描述词（如「业绩」「销售额」「回款」「合同金额」等），"
    "请用 get_module_fields 查 zhlabel 确认对应的 fieldName。"
    "禁止在字段含义不确定时自行猜测 fieldName。"
    "【大数据量确认】check_large_result=true（默认）时会自动预检记录总数：超过 500 条时返回预览，"
    "用户确认后设 force=true 重新调用。小查询可设 check_large_result=false 跳过预检，省一次后端调用。"
    "【使用策略】"
    "①口径确认：问题模糊时先少量查询(limit=5)并追问口径，禁止口径不明时查全量。"
    "②字段精简：用 fields 指定 5~8 个核心字段，不传则返回全部字段（非常耗token）。"
    "需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填，如 Accounts）"},
            "fields": {"type": "array", "items": {"type": "string"}, "description": "要返回的字段列表（建议指定 5~8 个核心字段，不传则返回全部）"},
            "filters": {"type": "object", "description": "过滤条件：{字段: 值} 或 {字段: {op, value}}"},
            "order_by": {"type": "string", "description": "排序字段（默认主键）"},
            "order": {"type": "string", "enum": ["asc", "desc"], "description": "排序方向（默认 desc）"},
            "limit": {"type": "integer", "description": "每页条数（默认10，最大100）"},
            "page": {"type": "integer", "description": "页码（从1开始）"},
            "force": {"type": "boolean", "description": "跳过大数据量确认（默认 false；超过 500 条时自动预检，用户确认后设为 true 重新调用获取数据）"},
            "check_large_result": {"type": "boolean", "description": "是否预检记录总数（默认 true；设为 false 可跳过预检，省一次后端调用，适合已知数据量不大的场景）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module"],
    },
)
def query_module_data(
    module: str,
    fields: list[str] | None = None,
    filters: dict | None = None,
    order_by: str | None = None,
    order: str = "desc",
    limit: int = 10,
    page: int = 1,
    force: bool = False,
    check_large_result: bool = True,
    instance: str = "default",
) -> str:
    """通用查询指定账套的模块数据（超过 500 条时自动预检，需用户确认）。"""
    # ── 大数据量预检：未强制且开启时先轻量获取总数，超过阈值则返回预览让用户确认 ──
    if not force and check_large_result:
        count_text = _run(instance, "query_module_data", {
            "module": module,
            "filters": filters,
            "count_only": True,
        })
        try:
            count_data = json.loads(count_text)
            total = count_data.get("total", 0)
            if isinstance(total, (int, float)) and total > 500:
                return json.dumps({
                    "code": "confirmation_needed",
                    "total": int(total),
                    "msg": f"匹配记录共 {int(total)} 条（超过 500 条），数据量较大。"
                           f"请向用户确认是否需要查看全部数据，或建议缩小过滤条件后重新查询。"
                           f"用户确认后请设置 force=true 重新调用本工具。",
                    "suggestion": "可尝试增加过滤条件缩小范围，或设置 force=true 继续查询全部数据",
                }, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass  # 预检失败则正常执行查询

    result_text = _run(instance, "query_module_data", {
        "module": module,
        "fields": fields,
        "filters": filters,
        "order_by": order_by,
        "order": order,
        "limit": limit,
        "page": page,
    })

    # 追加 query_meta：帮助 AI 识别查询形状，便于复用
    try:
        parsed = json.loads(result_text)
        if parsed.get("code") == "success":
            query_hash = hashlib.md5(
                json.dumps({"module": module, "fields": fields, "filters": filters,
                            "order_by": order_by, "order": order, "limit": limit},
                           sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:12]
            parsed["query_meta"] = {
                "module": module,
                "fields_used": fields or ["(全部)"],
                "filters_used": filters or {},
                "query_hash": query_hash,
                "hint": "相同 query_hash 表示查询形状相同，可直接复用口径和字段",
            }
            result_text = json.dumps(parsed, ensure_ascii=False, default=str)
    except (json.JSONDecodeError, TypeError):
        pass

    return result_text


@registry.register(
    "get_record",
    "查询指定账套中单条记录的完整详情。module 为模块索引名，record_id 为记录ID；instance 为账套名（默认 default）。include_detail 为 true（默认）时同时返回表体/分录明细（如订单的商品行），按明细表分组。"
    "明细行中会自动包含 lineitem_id（明细行主键），用于后续 update_record 编辑时定位具体行。"
    "需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填）"},
            "record_id": {"type": "string", "description": "记录ID"},
            "fields": {"type": "array", "items": {"type": "string"}, "description": "要返回的字段列表"},
            "include_detail": {"type": "boolean", "description": "是否同时返回表体/分录明细（默认 true）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "record_id"],
    },
)
def get_record(
    module: str,
    record_id: str,
    fields: list[str] | None = None,
    include_detail: bool = True,
    instance: str = "default",
) -> str:
    """查询指定账套中单条记录的完整详情。"""
    return _run(instance, "get_record", {
        "module": module,
        "record_id": record_id,
        "fields": fields,
        "include_detail": include_detail,
    })


@registry.register(
    "create_record",
    "在指定账套的指定标准 CRM 模块创建一条记录（单据）。module 为模块索引名，data 为要写入的表头字段字典（key 用字段名 fieldName，先用 get_module_fields 确认，也兼容列名 columnName）。"
    "⚠️ WorkReportList 是非标准模块，不能调用本工具；创建日志/周计划/月计划必须使用 workreport_create。"
    "【明细行 detail】可选，仅当模块含表体明细时支持（如销售订单 SalesOrder）。"
    "【下拉框字段】data 中含下拉/选择类字段（uitype 15/33 等）时，"
    "必须先调 get_picklist_values 获取有效值后从中选取，不得自行猜测填写。"
    "⚠️ 带明细创建前，必须先调用 get_module_fields 查看返回结果中的 detail_save_fields 列表——"
    "只有该列表中的 fieldName 才能作为明细行的 key，传了不在列表中的字段会被忽略或报错。"
    "若 detail_save_fields 为空数组，说明该模块不支持明细行。"
    "has_product_detail=true 表示产品明细，每行必须传 productid 或 product_no（二选一）指定产品。"
    "⚠️ productname 是显示字段，不是产品标识——传 productname 不会关联产品，会报「缺少产品」错误。"
    "若用户只给了产品名称没给编码，请先用 resolve_reference 或在 Products 模块查询找到对应的 product_no 再传入。"
    "has_product_detail=false 表示非产品明细，无需传产品字段。"
    "明细金额字段说明：taxprice 为含税单价、tax2 为税率（百分数，如 13 表示 13%）、list_amount 为价税合计、tax_amount 为税额——"
    "若只传 listprice（不含税单价）+ tax2，系统自动按标准公式补全含税单价/价税合计/税额，主表合计（total）自动为明细税额之和；"
    "若只传 listprice 不传税率，按无税处理（税额 0，价税合计按不含税金额计算）；"
    "若不传任何金额字段，金额将为 0（接口会返回 warning）。lineitem_id 为明细自增主键，请勿传。"
    "【明细保存验证】带 detail 创建时，服务端会自动验证明细行是否全部写入成功："
    "若部分明细未写入，会返回 error 并包含已创建的 record_id，"
    "请提示用户到 CRM 中检查该记录，如明细无法修复则手动删除此单据后重试。"
    "instance 为账套名（默认 default）。创建成功返回 record_id。注意：请确保传入该模块的必填字段——"
    "服务端会按官方新建页口径校验表头与明细行的必填字段（required=true），缺失时返回 missing_fields 列表，请补全后重试。"
    "⚠️ 报价类单据（Quotes/SalesOrder/ServiceContracts）的明细行失效日期 expiry_date 虽未在字段配置中标记必填，"
    "但业务上禁止为空：每行明细都必须由调用方显式传入 expiry_date（格式 YYYY-MM-DD），服务端不会自动填充；"
    "缺失时返回 code=error 并在 missing_fields 中逐行指出，请补全后重试。用户未提供失效日期时应先向用户询问，不得自行猜测或用表头 validtill 顶替。"
    "【查重与报备】服务端会对不允许重复字段（fieldonly=true）按官方口径查重："
    "硬性重复返回 duplicate_fields 并拒绝保存（不可跳过，请修改值）；"
    "可确认类重复提醒、客户名称报备提醒（Accounts）返回 need_confirm + duplicate_confirm_fields，"
    "必须把提醒内容完整转告用户并征得同意后，才能携相同参数 + allow_duplicate=true 重调（含后续 confirm_token 调用也要带）。需先调用 login 登录。"
    "【线索升级转换】用户说「把线索转成客户/联系人」时，禁止直接新建客户——必须传三个升级参数："
    "upgradefrommodule=Leads、upgradefromid=线索记录id（可传线索编号/名称自动解析）、upgradestatus=升级为客户，"
    "CRM 会走官方升级流程（更新线索状态、建立来源关联），否则线索状态不变且两边重复。"
    "【来源单据关联】基于已有单据创建下游单据时（如按报价单建订单），必须在 data 中传对应的来源关联字段"
    "（如销售订单的来源报价单/选择源单字段，可先用 get_module_fields 查看），值可传单据编号、主题或记录 id，"
    "服务端按官方选择源单流程绑定（同时写入源单类型）；解析失败会报错，请按错误提示改传记录 id。"
    "【⚠️ 必须先经用户确认（两阶段提交）】本工具首次调用不会真正创建：校验通过后返回 code=confirm_required、"
    "preview（待创建内容预览）和 confirm_token。你必须把 preview 完整展示给用户并明确询问是否创建；"
    "只有用户明确同意后，才用完全相同的参数 + 该 confirm_token 再次调用完成创建；"
    "用户要求修改则按新参数重新调用（不带 token）；用户拒绝则终止，不得自行确认或重试绕过。"
    "【负责人/创建人】单据负责人和创建人默认为当前登录用户，服务端自动设置，不得询问用户；"
    "仅当用户明确要求指定其他人负责时才传 smownerid（先用 resolve_reference(module=Users) 查 user_id）。"
    "【公海客户】新建客户时可通过 is_public_account 指定公海状态：unassigned=公海未分配、assigned=公海已分配、normal=普通客户（默认）。"
    "配合 data 中传入 pool_id（公海池 ID，可先用 get_public_pool_list 查看可用公海池）指定目标公海池。"
    "公海已分配客户会自动计算保护结束日期并写公海操作日志。仅 Accounts 模块支持公海状态。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填）"},
            "data": {"type": "object", "description": "表头字段字典（key 用字段名 fieldName，兼容列名 columnName）"},
            "detail": {"type": "array", "description": "可选，明细行数组，每行为 {detail_save_fields中的fieldName: 值}。必须先 get_module_fields 查看 detail_save_fields 确认该模块支持哪些明细字段。产品明细可用 productid 或 product_no 指定产品；金额可只传 listprice+tax2 自动补全。"},
            "upgradefrommodule": {"type": "string", "description": "可选，升级来源模块索引名。线索转客户/联系人时必传 Leads"},
            "upgradefromid": {"type": "string", "description": "可选，升级来源记录 id（可传线索编号/名称自动解析）。线索转客户时必传"},
            "upgradestatus": {"type": "string", "description": "可选，升级后线索状态，如「升级为客户」。线索转客户时必传"},
            "confirm_token": {"type": "string", "description": "两阶段确认令牌：首次调用不传（返回预览+令牌）；用户确认后用相同参数加令牌再调完成创建"},
            "allow_duplicate": {"type": "boolean", "description": "可选，默认 false。仅当接口返回 need_confirm（可确认类重复提醒/客户名称报备提醒）且用户已明确同意继续时传 true；硬性重复（duplicate_fields）不可跳过"},
            "is_public_account": {"type": "string", "enum": ["normal", "unassigned", "assigned"], "description": "可选，公海状态：normal=普通客户（默认）、unassigned=公海未分配、assigned=公海已分配。仅 Accounts 模块支持，需配合 data 中 pool_id 使用"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "data"],
    },
)
def create_record(
    module: str,
    data: dict,
    detail: list | None = None,
    upgradefrommodule: str | None = None,
    upgradefromid: str | None = None,
    upgradestatus: str | None = None,
    confirm_token: str | None = None,
    allow_duplicate: bool = False,
    is_public_account: str | None = None,
    instance: str = "default",
) -> str:
    """在指定账套的指定模块创建一条记录（单据），可携带明细行。首次调用仅返回预览，需用户确认后携 confirm_token 再调才创建。"""
    params: dict = {"module": module, "data": data}
    if detail:
        params["detail"] = detail
    if upgradefrommodule:
        params["upgradefrommodule"] = upgradefrommodule
    if upgradefromid:
        params["upgradefromid"] = upgradefromid
    if upgradestatus:
        params["upgradestatus"] = upgradestatus
    if confirm_token:
        params["confirm_token"] = confirm_token
    if allow_duplicate:
        params["allow_duplicate"] = True
    if is_public_account:
        params["is_public_account"] = is_public_account
    return _run(instance, "create_record", params)


@registry.register(
    "update_record",
    "编辑指定账套中已有记录（单据）。module 为模块索引名，record_id 为要编辑的记录ID。"
    "【表头更新 data】可选，字段增量更新字典——只传要改的字段，未传的字段保留原值。"
    "引用字段支持传编码/名称自动转 id（与 create_record 一致）。"
    "【明细行 detail】可选，行级语义："
    "· 行内带 lineitem_id → 更新该行（只传要改的字段，未传保留旧值；"
    "传了 quantity/listprice/taxprice/tax2 等金额字段时，未传的金额字段按公式重算）"
    "· 行内不带 lineitem_id → 追加新行（金额补全规则同 create_record；服务端会校验新增行的必填明细字段）"
    "⚠️ 报价类单据（Quotes/SalesOrder/ServiceContracts）追加新行时，同 create_record：每行必须显式传入失效日期 expiry_date（YYYY-MM-DD），"
    "服务端不会自动填充，缺失时返回 code=error 并在 missing_fields 中逐行指出（仅针对新增行，带 lineitem_id 的更新行未传则保留旧值）。"
    "【删除明细行 delete_lineitem_ids】可选，要删除的明细行 lineitem_id 数组。"
    "⚠️ detail 与 delete_lineitem_ids 均不传 → 明细完全不动（仅更新表头）。"
    "⚠️ 传了 detail 或 delete_lineitem_ids → 明细表按「旧行写回 + 行变更」整体重建。"
    "【审批限制】已批准/审批中的单据不允许编辑（与官方一致）。"
    "【自动化推进限制】若记录存在未处理的自动化推进任务，其推进依据字段（如商机阶段）会被官方推进机制锁定："
    "接口会跳过该字段的保存并在返回的 warning 中说明（其他字段不受影响）；"
    "遇到此类 warning 请引导用户先在 CRM 中处理推进任务，不要绕过。"
    "【⚠️ lineitem_id 易变】明细行采用 delete-all + re-insert 机制，每次保存后所有明细行的 lineitem_id 都会变成新值。"
    "因此：①编辑前必须先调用 get_record 获取当前最新的 lineitem_id（现在 get_record 已自动返回 lineitem_id）；"
    "②两次编辑操作之间如果插入了一次保存，旧的 lineitem_id 就失效了，必须重新 get_record；"
    "③如果用旧的 lineitem_id 会报「不存在」错误，按提示重新 get_record 即可。"
    "【查重与报备】修改后的表头字段若命中不允许重复字段（fieldonly=true），服务端按官方口径查重（自动排除当前记录）："
    "硬性重复返回 duplicate_fields 并拒绝；可确认类提醒（含客户名称报备提醒）返回 need_confirm，"
    "必须转告用户征得同意后才能携相同参数 + allow_duplicate=true 重调。"
    "instance 为账套名（默认 default）。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填，如 SalesOrder）"},
            "record_id": {"type": "string", "description": "要编辑的记录ID（必填，编辑明细前请先 get_record 获取最新 lineitem_id）"},
            "data": {"type": "object", "description": "表头字段增量更新字典（只传要改的字段，未传保留原值）"},
            "detail": {"type": "array", "description": "明细行数组。行内带 lineitem_id=更新该行（必须是 get_record 返回的最新值），不带=追加新行。每行为 {fieldName: 值}"},
            "delete_lineitem_ids": {"type": "array", "items": {"type": "string"}, "description": "要删除的明细行 lineitem_id 数组（必须是 get_record 返回的最新值）"},
            "allow_duplicate": {"type": "boolean", "description": "可选，默认 false。仅当接口返回 need_confirm（可确认类重复提醒/客户名称报备提醒）且用户已明确同意继续时传 true；硬性重复（duplicate_fields）不可跳过"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "record_id"],
    },
)
def update_record(
    module: str,
    record_id: str,
    data: dict | None = None,
    detail: list | None = None,
    delete_lineitem_ids: list | None = None,
    allow_duplicate: bool = False,
    instance: str = "default",
) -> str:
    """编辑指定账套中已有记录（单据），支持表头部分更新 + 明细行级增删改。"""
    params: dict[str, Any] = {"module": module, "record_id": str(record_id)}
    if data:
        params["data"] = data
    if detail:
        params["detail"] = detail
    if delete_lineitem_ids:
        params["delete_lineitem_ids"] = delete_lineitem_ids
    if allow_duplicate:
        params["allow_duplicate"] = True
    return _run(instance, "update_record", params)


@registry.register(
    "business_search",
    "查询指定模块的工商信息，严格按模块工商授权、模块设置及字段权限执行。search_type=exact 返回精确工商信息；精确未命中且已开模糊查询时返回 need_fuzzy_selection，再用 search_type=fuzzy 获取候选。模糊候选必须展示给客户选择，禁止自行选择或写入。需先 login。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名，如 Accounts"},
            "keyword": {"type": "string", "description": "公司名称或工商查询关键词"},
            "search_type": {"type": "string", "enum": ["exact", "fuzzy"], "description": "查询类型，默认 exact；仅精确无结果且服务端提示可模糊查询时传 fuzzy"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "keyword"],
    },
)
def business_search(module: str, keyword: str, search_type: str = "exact", instance: str = "default") -> str:
    """查询工商信息或模糊候选；本工具不会回填或保存记录。"""
    return _run(instance, "business_search", {
        "module": module,
        "keyword": keyword,
        "search_type": search_type,
    })


@registry.register(
    "business_backfill_preview",
    "把客户从 business_search 结果中明确选定的一家公司的 business_data 转为 CRM 回填字段预览，不写入记录。可传 selected_fields 限定客户同意回填的字段。必须完整展示返回 fields 后，再向客户确认是否回填。需先 login。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名"},
            "business_data": {"type": "object", "description": "客户选定的一条工商结果对象，必须来自 business_search 返回 entries"},
            "selected_fields": {"type": "array", "items": {"type": "string"}, "description": "客户同意回填的 CRM fieldName 列表；不传则预览全部已配置映射"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "business_data"],
    },
)
def business_backfill_preview(
    module: str,
    business_data: dict,
    selected_fields: list[str] | None = None,
    instance: str = "default",
) -> str:
    """生成工商回填字段预览，不写入 CRM。"""
    params: dict[str, Any] = {"module": module, "business_data": business_data}
    if selected_fields:
        params["selected_fields"] = selected_fields
    return _run(instance, "business_backfill_preview", params)


@registry.register(
    "business_backfill_apply",
    "将已获客户确认的工商结果回填到新建或已有记录。必须先调用 business_backfill_preview 并向客户展示 fields；仅在客户明确同意后传 confirmed=true。本工具会重新由 CRM 校验映射，不能伪造回填字段。action=create 仍遵循 create_record 两阶段确认：首次会返回 confirm_token，客户再次确认创建后用同参数+token 调用。",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update"], "description": "create=新建并回填，update=编辑已有记录并回填"},
            "module": {"type": "string", "description": "模块索引名"},
            "business_data": {"type": "object", "description": "客户选定的一条工商结果对象，必须来自 business_search 返回 entries"},
            "data": {"type": "object", "description": "其他业务字段；已确认的工商回填字段以预览值为准"},
            "selected_fields": {"type": "array", "items": {"type": "string"}, "description": "客户确认同意回填的 CRM fieldName 列表"},
            "record_id": {"type": "string", "description": "action=update 时必填"},
            "detail": {"type": "array", "description": "可选，创建或编辑时一并处理的明细行"},
            "confirmed": {"type": "boolean", "description": "客户已明确确认回填字段和值时必须为 true"},
            "confirm_token": {"type": "string", "description": "action=create 的第二阶段创建确认令牌"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["action", "module", "business_data", "confirmed"],
    },
)
def business_backfill_apply(
    action: str,
    module: str,
    business_data: dict,
    data: dict | None = None,
    selected_fields: list[str] | None = None,
    record_id: str | None = None,
    detail: list | None = None,
    confirmed: bool = False,
    confirm_token: str | None = None,
    instance: str = "default",
) -> str:
    """在客户确认后回填工商字段，并复用现有新建或编辑链路。"""
    params: dict[str, Any] = {
        "action": action,
        "module": module,
        "business_data": business_data,
        "data": data,
        "selected_fields": selected_fields,
        "record_id": record_id,
        "detail": detail,
        "confirmed": confirmed,
        "confirm_token": confirm_token,
    }
    return _run(instance, "business_backfill_apply", params)


@registry.register(
    "query_module_summary",
    "聚合查询：返回匹配记录的条数(count)和数值字段的合计(sum)。"
    "适用于「多少条」「几个」「总金额多少」「合计」等统计类问题——"
    "只返回 count + sum，不返回记录明细，比 query_module_data 轻量得多。"
    "⚠️ 过滤条件中若涉及人名/客户名/单据编号等描述性内容，先用 resolve_reference 查出 record_id 再过滤。"
    "用户说「我」或「我的」时，直接用 login 返回的 user_id 作为过滤值（如 assigned_user_id），无需 resolve_reference。"
    "【字段口径确认】当用户提到模糊的业务描述词（如「业绩」「销售额」「回款」「合同金额」等）作为统计对象时，"
    "请用 get_module_fields 查 zhlabel 确认对应的 fieldName。"
    "确认过的字段口径可记在本次会话中，后续同一口径的查询无需重复确认。"
    "sum_fields 传需要求和的数值字段名列表（如 [\"amount\"]）；"
    "不传则只返回 count。filters 写法同 query_module_data。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填）"},
            "sum_fields": {"type": "array", "items": {"type": "string"}, "description": "需要求和的数值字段名列表（如 [\"amount\"]）"},
            "filters": {"type": "object", "description": "过滤条件（同 query_module_data）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module"],
    },
)
def query_module_summary(
    module: str,
    sum_fields: list[str] | None = None,
    filters: dict | None = None,
    instance: str = "default",
) -> str:
    """聚合查询：count + sum（不返回明细记录）。"""
    params: dict[str, Any] = {"module": module}
    if sum_fields:
        params["sum_fields"] = sum_fields
    if filters:
        params["filters"] = filters
    return _run(instance, "query_module_summary", params)


@registry.register(
    "resolve_reference",
    "名称/编号 → record_id 查找器。当问题中出现人名、客户名、单据编号、产品名称等描述性内容时，"
    "先用本工具查出对应的 record_id，再用于其他工具的 filters 或 detail。"
    "⚠️【批量优先】需要查找多个名称/编号时，必须用数组一次性批量查找（如 value: [\"产品A\", \"产品B\", \"产品C\"]），"
    "不要逐个调用！批量查找单次 SQL 完成，省 token 省时间。"
    "【人名解析】module=\"Users\"、value=人名 → 返回 user_id。提到「我」直接用 login 的 user_id。"
    "【客户名解析】module=\"Accounts\"、value=客户名。"
    "【产品解析】module=\"Products\"、value=产品名称或编码 → 返回 productid，用于明细行的 productid。"
    "【单据编号解析】用对应模块（如 SalesOrder）查找。"
    "返回格式：批量时 {results: {搜索词: [{record_id, label}]}, not_found: [未找到的词]}。"
    "module 为要查找的目标模块；value 为搜索词（字符串或数组）；"
    "field 可选，指定在哪个字段搜索（不传则自动搜索该模块的标准名称/编号字段）。"
    "需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "目标模块（如 Accounts/Contacts/Users/Potentials/SalesOrder）"},
            "value": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "搜索词：单个名称/编号用字符串，多个名称/编号用字符串数组（批量查找）",
            },
            "field": {"type": "string", "description": "可选，指定搜索字段名（不传自动搜索标准名称/编号字段）"},
            "limit": {"type": "integer", "description": "每个搜索词最多返回的匹配条数（默认 10，最大 50）"},
            "page": {"type": "integer", "description": "页码（默认 1，当某搜索词匹配数超过 limit 时可翻页）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "value"],
    },
)
def resolve_reference(
    module: str,
    value: str | list[str],
    field: str | None = None,
    limit: int = 10,
    page: int = 1,
    instance: str = "default",
) -> str:
    """名称/编号 → record_id 查找器（支持批量 + 分页）。"""
    params: dict[str, Any] = {"module": module}
    if isinstance(value, list):
        params["values"] = value
    else:
        params["value"] = value
    if field:
        params["field"] = field
    if limit != 10:
        params["limit"] = limit
    if page != 1:
        params["page"] = page
    return _run(instance, "resolve_reference", params)


@registry.register(
    "resolve_href_by_ids",
    "批量将记录 ID 转换为 CRM 配置的超链接字段值（如客户名称、产品名称、单据主题），避免逐条调用 get_record。"
    "ids 最多 100 个；仅返回当前账号有模块查询权限且未删除的记录。"
    "返回 results:{record_id:{record_id,href,label}} 和 not_found。需先 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "目标模块，如 Accounts、Products、SalesOrder"},
            "ids": {"type": "array", "items": {"type": ["string", "integer"]}, "minItems": 1, "maxItems": 100, "description": "待解析的记录 ID 数组"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "ids"],
    },
)
def resolve_href_by_ids(module: str, ids: list[str | int], instance: str = "default") -> str:
    """批量解析记录 ID 对应的官方超链接字段值。"""
    normalized = [str(item).strip() for item in ids if str(item).strip()]
    if not normalized:
        return json.dumps({"code": "error", "msg": "ids 至少需提供一个非空记录 ID"}, ensure_ascii=False)
    if len(normalized) > 100:
        return json.dumps({"code": "error", "msg": "一次最多查询 100 个 ids"}, ensure_ascii=False)
    return _run(instance, "resolve_href_by_ids", {"module": module, "ids": normalized})


@registry.register(
    "resolve_ids_by_href",
    "批量按 CRM 模块的官方超链接字段值精确查找记录 ID，避免逐条调用 get_record。"
    "href_values 最多 100 个；同名记录会保留全部匹配 ID；仅返回当前账号有模块查询权限且未删除的记录。"
    "返回 results:{超链接值:[{record_id,href,label}]} 和 not_found。需先 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "目标模块，如 Accounts、Products、SalesOrder"},
            "href_values": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100, "description": "待精确匹配的超链接字段值数组"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "href_values"],
    },
)
def resolve_ids_by_href(module: str, href_values: list[str], instance: str = "default") -> str:
    """批量按官方超链接字段值精确解析记录 ID。"""
    normalized = [str(item).strip() for item in href_values if str(item).strip()]
    if not normalized:
        return json.dumps({"code": "error", "msg": "href_values 至少需提供一个非空超链接值"}, ensure_ascii=False)
    if len(normalized) > 100:
        return json.dumps({"code": "error", "msg": "一次最多查询 100 个 href_values"}, ensure_ascii=False)
    return _run(instance, "resolve_ids_by_href", {"module": module, "href_values": normalized})


# ─────────────────── ERP 库存 ───────────────────

@registry.register(
    "get_erp_stock",
    "按产品编码批量查询 ERP 实时库存。productno 必填，为产品编码数组（如 [\"acc1\"]），一次最多 100 个。"
    "该工具只读，不会修改库存；库存数据按当前登录 CRM 用户的 ERP 权限返回。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "productno": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100, "description": "产品编码数组，如 [\"acc1\", \"P001\"]"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["productno"],
    },
)
def get_erp_stock(productno: list[str], instance: str = "default") -> str:
    """按产品编码批量查询 ERP 实时库存。"""
    if not isinstance(productno, list):
        return json.dumps({"code": "error", "msg": "productno 必须为产品编码数组"}, ensure_ascii=False)
    normalized = [str(item).strip() for item in productno if str(item).strip()]
    if not normalized:
        return json.dumps({"code": "error", "msg": "productno 至少需提供一个非空产品编码"}, ensure_ascii=False)
    if len(normalized) > 100:
        return json.dumps({"code": "error", "msg": "一次最多查询 100 个产品编码"}, ensure_ascii=False)
    return _run(instance, "get_erp_stock", {"productno": normalized})


# ─────────────── 公海客户操作（领取/释放/延期/变更公海/公海池列表） ───────────────

@registry.register(
    "get_public_pool_list",
    "获取公海池配置列表（公海池 ID、名称、管理员、成员、上限等）。"
    "新建公海客户、变更公海时需要知道 pool_id，先用本工具查看可用公海池。"
    "若返回 status=no 表示当前账套未启用公海功能。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
    },
)
def get_public_pool_list(instance: str = "default") -> str:
    """获取公海池配置列表。"""
    return _run(instance, "get_public_pool_list", {})


@registry.register(
    "allocate_account",
    "领取或分配公海客户。allocatetype=3 为领取（将公海未分配客户领取为当前用户或指定用户负责）；"
    "allocatetype=2 为分配（管理员将公海客户分配给指定用户，必须传 userid）。"
    "仅支持 publicaccount=1（公海未分配）状态的客户。"
    "record_id 为客户记录 ID（可先用 query_module_data 或 resolve_reference 查找）。"
    "领取/分配成功后客户状态变为公海已分配，保护期开始计算。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "客户记录 ID（必填）"},
            "allocatetype": {"type": "integer", "enum": [2, 3], "description": "操作类型：3=领取（默认）、2=分配（需指定 userid）"},
            "userid": {"type": "string", "description": "目标用户 user_id。领取时不传则默认当前用户；分配时必填"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["record_id"],
    },
)
def allocate_account(
    record_id: str,
    allocatetype: int = 3,
    userid: str | None = None,
    instance: str = "default",
) -> str:
    """领取/分配公海客户。"""
    params: dict[str, Any] = {"record_id": str(record_id), "allocatetype": int(allocatetype)}
    if userid:
        params["userid"] = str(userid)
    return _run(instance, "allocate_account", params)


@registry.register(
    "release_account",
    "释放公海客户（将已分配客户释放回公海未分配状态）。"
    "释放后客户保护期清零，状态变为公海未分配，其他用户可领取。"
    "可选传 release_reason 释放原因（会记录到公海操作日志）。"
    "record_id 为客户记录 ID。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "客户记录 ID（必填）"},
            "release_reason": {"type": "string", "description": "释放原因（可选，记录到公海操作日志）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["record_id"],
    },
)
def release_account(
    record_id: str,
    release_reason: str = "",
    instance: str = "default",
) -> str:
    """释放公海客户。"""
    params: dict[str, Any] = {"record_id": str(record_id)}
    if release_reason:
        params["release_reason"] = release_reason
    return _run(instance, "release_account", params)


@registry.register(
    "delay_account",
    "延期公海客户（延长保护期结束日期）。仅支持公海已分配状态（publicaccount>1）的客户。"
    "delay_days 为延期天数（正整数），会在当前保护结束日期基础上增加指定天数。"
    "返回延期前后的保护结束日期（old_protectdate / new_protectdate）。"
    "延期操作会记录到公海日志并给负责人发送提醒。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "客户记录 ID（必填）"},
            "delay_days": {"type": "integer", "description": "延期天数（必填，正整数）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["record_id", "delay_days"],
    },
)
def delay_account(
    record_id: str,
    delay_days: int,
    instance: str = "default",
) -> str:
    """延期公海客户。"""
    return _run(instance, "delay_account", {"record_id": str(record_id), "delay_days": int(delay_days)})


@registry.register(
    "convert_account",
    "公海分配/变更公海（普通客户转公海、公海转普通、公海池之间变更）。"
    "transfer=transfer_to：转为公海客户（需指定 pool_id，可先用 get_public_pool_list 查看可用公海池）；"
    "transfer=transfer_out：转为普通客户（脱离公海，重新计算普通客户保护期）。"
    "公海池之间变更：先 transfer_to 到目标公海池，或直接用 transfer_to 加新 pool_id（服务端自动处理池间变更）。"
    "返回成功/失败条数和详细日志。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "客户记录 ID（必填）"},
            "transfer": {"type": "string", "enum": ["transfer_to", "transfer_out"], "description": "转换方向：transfer_to=转公海、transfer_out=转普通客户"},
            "pool_id": {"type": "string", "description": "目标公海池 ID（transfer_to 时必填，可先用 get_public_pool_list 查看）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["record_id", "transfer"],
    },
)
def convert_account(
    record_id: str,
    transfer: str,
    pool_id: str = "",
    instance: str = "default",
) -> str:
    """公海分配/变更公海。"""
    params: dict[str, Any] = {"record_id": str(record_id), "transfer": transfer}
    if pool_id:
        params["pool_id"] = str(pool_id)
    return _run(instance, "convert_account", params)


# ─────────────── 销售线索公海操作（领取/释放/延期/变更公海/公海池列表） ───────────────

@registry.register(
    "get_leads_public_pool_list",
    "获取线索公海池配置列表（公海池 ID、名称、管理员、成员、上限等）。"
    "新建公海线索、变更线索公海时需要知道 pool_id，先用本工具查看可用公海池。"
    "若返回 status=no 表示当前账套未启用线索公海功能。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
    },
)
def get_leads_public_pool_list(instance: str = "default") -> str:
    """获取线索公海池配置列表。"""
    return _run(instance, "get_leads_public_pool_list", {})


@registry.register(
    "allocate_leads",
    "领取或分配公海线索。allocatetype=3 为领取（将公海未分配线索领取为当前用户或指定用户负责）；"
    "allocatetype=2 为分配（管理员将公海线索分配给指定用户，必须传 userid）。"
    "仅支持 publicaccount=1（公海未分配）状态的线索。"
    "record_id 为线索记录 ID（可先用 query_module_data 或 resolve_reference 查找）。"
    "领取/分配成功后线索状态变为公海已分配，保护期开始计算。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "线索记录 ID（必填）"},
            "allocatetype": {"type": "integer", "enum": [2, 3], "description": "操作类型：3=领取（默认）、2=分配（需指定 userid）"},
            "userid": {"type": "string", "description": "目标用户 user_id。领取时不传则默认当前用户；分配时必填"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["record_id"],
    },
)
def allocate_leads(
    record_id: str,
    allocatetype: int = 3,
    userid: str | None = None,
    instance: str = "default",
) -> str:
    """领取/分配公海线索。"""
    params: dict[str, Any] = {"record_id": str(record_id), "allocatetype": int(allocatetype)}
    if userid:
        params["userid"] = str(userid)
    return _run(instance, "allocate_leads", params)


@registry.register(
    "release_leads",
    "释放公海线索（将已分配线索释放回公海未分配状态）。"
    "释放后线索保护期清零，状态变为公海未分配，其他用户可领取。"
    "可选传 release_reason 释放原因（会记录到公海操作日志）。"
    "record_id 为线索记录 ID。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "线索记录 ID（必填）"},
            "release_reason": {"type": "string", "description": "释放原因（可选，记录到公海操作日志）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["record_id"],
    },
)
def release_leads(
    record_id: str,
    release_reason: str = "",
    instance: str = "default",
) -> str:
    """释放公海线索。"""
    params: dict[str, Any] = {"record_id": str(record_id)}
    if release_reason:
        params["release_reason"] = release_reason
    return _run(instance, "release_leads", params)


@registry.register(
    "delay_leads",
    "延期公海线索（延长保护期结束日期）。仅支持公海已分配状态（publicaccount>1）的线索。"
    "delay_days 为延期天数（正整数），会在当前保护结束日期基础上增加指定天数。"
    "返回延期前后的保护结束日期（old_protectdate / new_protectdate）。"
    "延期操作会记录到公海日志并给负责人发送提醒。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "线索记录 ID（必填）"},
            "delay_days": {"type": "integer", "description": "延期天数（必填，正整数）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["record_id", "delay_days"],
    },
)
def delay_leads(
    record_id: str,
    delay_days: int,
    instance: str = "default",
) -> str:
    """延期公海线索。"""
    return _run(instance, "delay_leads", {"record_id": str(record_id), "delay_days": int(delay_days)})


@registry.register(
    "convert_leads",
    "线索公海分配/变更公海（普通线索转公海、公海转普通、公海池之间变更）。"
    "transfer=transfer_to：转为公海线索（需指定 pool_id，可先用 get_leads_public_pool_list 查看可用公海池）；"
    "transfer=transfer_out：转为普通线索（脱离公海，重新计算普通线索保护期）。"
    "返回成功/失败条数和详细日志。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "record_id": {"type": "string", "description": "线索记录 ID（必填）"},
            "transfer": {"type": "string", "enum": ["transfer_to", "transfer_out"], "description": "转换方向：transfer_to=转公海、transfer_out=转普通线索"},
            "pool_id": {"type": "string", "description": "目标公海池 ID（transfer_to 时必填，可先用 get_leads_public_pool_list 查看）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["record_id", "transfer"],
    },
)
def convert_leads(
    record_id: str,
    transfer: str,
    pool_id: str = "",
    instance: str = "default",
) -> str:
    """线索公海分配/变更公海。"""
    params: dict[str, Any] = {"record_id": str(record_id), "transfer": transfer}
    if pool_id:
        params["pool_id"] = str(pool_id)
    return _run(instance, "convert_leads", params)


# ─────────────── 工作汇报（日志/周计划/月计划） ───────────────

@registry.register(
    "workreport_list",
    "查询工作汇报列表（工作汇报不是标准 CRM 模块，不能用 query_module_data，必须用本工具；PHP 端直接调用官方 getList）。"
    "type 必填：1=日志，2=周计划，3=月计划。"
    "返回结构为官方 getList 原始结构外包 code=success/data，包含 summary/report/field/permit。"
    "【过滤】creator=创建人 user_id；date_from/date_to=创建时间范围（官方 getList 按 createdtime 筛选）；approved=0/1。"
    "【数据范围 scope】related=我相关（默认）、mine=我发起的、all=全部（仅管理员）。"
    "用户说「我的日志/我写的周报」→ scope=mine；说「某人发起的」→ 先 resolve_reference 查出其 user_id 再传 creator。"
    "官方 getList 固定每页 20 条，page 控制页码；需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "type": {"type": "integer", "description": "汇报类型（必填）：1=日志，2=周计划，3=月计划"},
            "creator": {"type": "integer", "description": "创建人 user_id（可选，人名先用 resolve_reference 查）"},
            "date_from": {"type": "string", "description": "创建时间起（YYYY-MM-DD，可选；官方 getList 按 createdtime 筛选）"},
            "date_to": {"type": "string", "description": "创建时间止（YYYY-MM-DD，可选；官方 getList 按 createdtime 筛选）"},
            "approved": {"type": "integer", "description": "点评状态过滤：0=待点评，1=已点评（不传查全部）"},
            "scope": {"type": "string", "description": "数据范围：related=我相关（默认）、mine=我发起、all=全部（仅管理员）"},
            "limit": {"type": "integer", "description": "兼容参数：官方 getList 固定每页20条，此参数暂不生效"},
            "page": {"type": "integer", "description": "页码（默认1）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["type"],
    },
)
def workreport_list(
    type: int,
    creator: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    approved: int | None = None,
    scope: str = "related",
    limit: int = 20,
    page: int = 1,
    instance: str = "default",
) -> str:
    """工作汇报列表查询（日志/周计划/月计划）。"""
    params: dict[str, Any] = {"type": int(type), "scope": scope, "page": int(page)}
    if creator is not None:
        params["creator"] = int(creator)
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    if approved is not None:
        params["approved"] = int(approved)
    return _run(instance, "workreport_list", params)


@registry.register(
    "workreport_create",
    "新建工作汇报：日志(type=1)、周计划(type=2)、月计划(type=3)。PHP 端直接调用官方 saveSendLog，以当前登录用户名义发布。"
    "【必填】type + reader（点评人 user_id，人名先用 resolve_reference(module=Users) 查出）。"
    "【内容】worksummary=工作总结，workplan=工作计划，workexperience=心得/需协调事项，三者至少填一项。"
    "周计划还可传 workplan_tuesday/wednesday/thursday/friday 分日计划；内容支持纯文本多行。"
    "【汇报日期 sendlog_workdate】不传默认本期：日志=当天（YYYY-MM-DD）；周计划=本周（\"周一 ~ 周日\"）；"
    "月计划=本月（\"月初 ~ 月末\"），周/月格式固定为 \"YYYY-MM-DD ~ YYYY-MM-DD\"。"
    "【可选】ccuser=抄送人 user_id 逗号分隔；ccgroup=部门 id 逗号分隔（自动展开为部门内全部用户）；"
    "atuser=@人 user_id 逗号分隔；account_ids/contact_ids/product_ids=关联客户/联系人/产品 id 逗号分隔"
    "（名称先用 resolve_reference 查 id）；signaddress=签到地址。"
    "【⚠️ 必须先经用户确认（两阶段提交）】本工具首次调用不会真正提交：校验通过后返回 code=confirm_required、"
    "preview（汇报类型/点评人/日期/内容预览）和 confirm_token。你必须把 preview 完整展示给用户并明确询问是否提交；"
    "只有用户明确同意后，才用完全相同的参数 + 该 confirm_token 再次调用完成提交；"
    "用户要求修改则按新参数重新调用（不带 token）；用户拒绝则终止，不得自行确认或重试绕过。"
    "【创建人】汇报固定以当前登录用户名义发布，无创建人参数，不得询问用户创建人是谁。"
    "创建成功后返回 workreportid 和单据编号 workreport_no，点评人会收到待点评提醒。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "type": {"type": "integer", "description": "汇报类型（必填）：1=日志，2=周计划，3=月计划"},
            "reader": {"type": "integer", "description": "点评人 user_id（必填，人名先用 resolve_reference 查）"},
            "worksummary": {"type": "string", "description": "工作总结/本期完成的工作"},
            "workplan": {"type": "string", "description": "工作计划/下期安排"},
            "workexperience": {"type": "string", "description": "心得体会/需协调事项"},
            "workplan_tuesday": {"type": "string", "description": "周二工作计划（仅周计划）"},
            "workplan_wednesday": {"type": "string", "description": "周三工作计划（仅周计划）"},
            "workplan_thursday": {"type": "string", "description": "周四工作计划（仅周计划）"},
            "workplan_friday": {"type": "string", "description": "周五工作计划（仅周计划）"},
            "sendlog_workdate": {"type": "string", "description": "汇报日期：日志传 YYYY-MM-DD；周/月传 \"YYYY-MM-DD ~ YYYY-MM-DD\"；不传默认本期"},
            "ccuser": {"type": "string", "description": "抄送人 user_id，逗号分隔（可选）"},
            "ccgroup": {"type": "string", "description": "抄送部门 id，逗号分隔，自动展开为部门内全部用户（可选）"},
            "atuser": {"type": "string", "description": "@人 user_id，逗号分隔（可选）"},
            "account_ids": {"type": "string", "description": "关联客户 id，逗号分隔（可选）"},
            "contact_ids": {"type": "string", "description": "关联联系人 id，逗号分隔（可选）"},
            "product_ids": {"type": "string", "description": "关联产品 id，逗号分隔（可选）"},
            "signaddress": {"type": "string", "description": "签到地址（可选）"},
            "confirm_token": {"type": "string", "description": "两阶段确认令牌：首次调用不传（返回预览+令牌）；用户确认后用相同参数加令牌再调完成提交"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["type", "reader"],
    },
)
def workreport_create(
    type: int,
    reader: int,
    worksummary: str | None = None,
    workplan: str | None = None,
    workexperience: str | None = None,
    workplan_tuesday: str | None = None,
    workplan_wednesday: str | None = None,
    workplan_thursday: str | None = None,
    workplan_friday: str | None = None,
    sendlog_workdate: str | None = None,
    ccuser: str | None = None,
    ccgroup: str | None = None,
    atuser: str | None = None,
    account_ids: str | None = None,
    contact_ids: str | None = None,
    product_ids: str | None = None,
    signaddress: str | None = None,
    confirm_token: str | None = None,
    instance: str = "default",
) -> str:
    """新建工作汇报（日志/周计划/月计划）。首次调用仅返回预览，需用户确认后携 confirm_token 再调才提交。"""
    params: dict[str, Any] = {"type": int(type), "reader": int(reader)}
    for k, v in {
        "worksummary": worksummary, "workplan": workplan, "workexperience": workexperience,
        "workplan_tuesday": workplan_tuesday, "workplan_wednesday": workplan_wednesday,
        "workplan_thursday": workplan_thursday, "workplan_friday": workplan_friday,
        "sendlog_workdate": sendlog_workdate, "ccuser": ccuser, "ccgroup": ccgroup, "atuser": atuser,
        "account_ids": account_ids, "contact_ids": contact_ids, "product_ids": product_ids,
        "signaddress": signaddress, "confirm_token": confirm_token,
    }.items():
        if v:
            params[k] = v
    return _run(instance, "workreport_create", params)


@registry.register(
    "workreport_update",
    "编辑已有工作汇报（日志/周计划/月计划）的内容，PHP 端直接调用官方 editReport。"
    "【前置】workreportid 必填——先用 workreport_list 查到目标汇报的 workreportid 和当前内容，再只传要改的字段。"
    "【限制】仅创建者本人可编辑；已点评（approved=1）的汇报不允许编辑（与官方规则一致）。"
    "【内容字段】worksummary/workplan/workexperience（按记录类型自动落到日志/周/月对应列，未传保留原值）；"
    "周计划还可改 workplan_tuesday~friday；sendlog_workdate 汇报日期（格式同 workreport_create）；"
    "signaddress 签到地址。"
    "【关联记录】account_ids/contact_ids/product_ids 传了即全量替换（不传不动）。"
    "编辑会写入变更痕迹（modtracker）并累加 editcount，与官方编辑行为一致。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "workreportid": {"type": "integer", "description": "要编辑的工作汇报 ID（必填，先用 workreport_list 查）"},
            "worksummary": {"type": "string", "description": "工作总结（新值，不传保留原值）"},
            "workplan": {"type": "string", "description": "工作计划（新值，不传保留原值）"},
            "workexperience": {"type": "string", "description": "心得体会（新值，不传保留原值）"},
            "workplan_tuesday": {"type": "string", "description": "周二工作计划（仅周计划，不传保留原值）"},
            "workplan_wednesday": {"type": "string", "description": "周三工作计划（仅周计划，不传保留原值）"},
            "workplan_thursday": {"type": "string", "description": "周四工作计划（仅周计划，不传保留原值）"},
            "workplan_friday": {"type": "string", "description": "周五工作计划（仅周计划，不传保留原值）"},
            "sendlog_workdate": {"type": "string", "description": "汇报日期：日志 YYYY-MM-DD；周/月 \"YYYY-MM-DD ~ YYYY-MM-DD\""},
            "account_ids": {"type": "string", "description": "关联客户 id 逗号分隔（传了即全量替换）"},
            "contact_ids": {"type": "string", "description": "关联联系人 id 逗号分隔（传了即全量替换）"},
            "product_ids": {"type": "string", "description": "关联产品 id 逗号分隔（传了即全量替换）"},
            "signaddress": {"type": "string", "description": "签到地址"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["workreportid"],
    },
)
def workreport_update(
    workreportid: int,
    worksummary: str | None = None,
    workplan: str | None = None,
    workexperience: str | None = None,
    workplan_tuesday: str | None = None,
    workplan_wednesday: str | None = None,
    workplan_thursday: str | None = None,
    workplan_friday: str | None = None,
    sendlog_workdate: str | None = None,
    account_ids: str | None = None,
    contact_ids: str | None = None,
    product_ids: str | None = None,
    signaddress: str | None = None,
    instance: str = "default",
) -> str:
    """编辑工作汇报（仅创建者、未点评状态可编辑）。"""
    params: dict[str, Any] = {"workreportid": int(workreportid)}
    for k, v in {
        "worksummary": worksummary, "workplan": workplan, "workexperience": workexperience,
        "workplan_tuesday": workplan_tuesday, "workplan_wednesday": workplan_wednesday,
        "workplan_thursday": workplan_thursday, "workplan_friday": workplan_friday,
        "sendlog_workdate": sendlog_workdate, "account_ids": account_ids,
        "contact_ids": contact_ids, "product_ids": product_ids, "signaddress": signaddress,
    }.items():
        if v is not None:
            params[k] = v
    return _run(instance, "workreport_update", params)


# ─────────────── 自动化推进（任务查看/推进/推进日志） ───────────────

@registry.register(
    "get_advance_tasks",
    "查看单据的自动化推进（销售自动化）任务及任务状态——只读，不写库、不触发推进。"
    "适用于「这个商机的推进任务完成了吗」「有哪些必做任务没做」「能推进到哪些阶段」等问题。"
    "返回：追踪字段、当前阶段、流程阶段顺序、每个可推进目标（relations：advanceid/stagename_target/是否回推/autopush 自动推进/任务清单及完成状态/未完成必做任务/allow_push）。"
    "任务类型：field=必填字段任务（tasklabel 为字段名）、module=创建单据任务（需创建关联单据，approve_demand=yes 时还须全部审批通过）、"
    "select=选择型任务、checkbox=勾选型任务、validata=验证规则任务（推进时校验）。"
    "⚠️ 推进依据字段（如商机阶段）在存在未处理推进任务时被锁定，update_record 无法直接修改，必须用 advance_push 走官方推进链路。"
    "需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填，如 Potentials/SalesOrder/Leads）"},
            "record_id": {"type": "string", "description": "记录 ID（必填，可先用 resolve_reference 查）"},
            "advanceid": {"type": "string", "description": "可选，只看指定推进关系；不传返回当前阶段全部可推进目标"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "record_id"],
    },
)
def get_advance_tasks(
    module: str,
    record_id: str,
    advanceid: str | None = None,
    instance: str = "default",
) -> str:
    """查看单据自动化推进任务及状态（只读）。"""
    params: dict[str, Any] = {"target_module": module, "record_id": str(record_id)}
    if advanceid:
        params["advanceid"] = str(advanceid)
    return _run(instance, "get_advance_tasks", params)


@registry.register(
    "advance_push",
    "推进单据的自动化阶段（如商机阶段、销售订单状态）——走 CRM 官方推进链路，与前台点击「推进」一致："
    "先按官方规则判断是否存在未完成的必做任务（TaskCompleteCheck），有则拒绝推进并返回未完成清单；"
    "无则执行官方推进方法（updateModuleAdvanceStage），自动记录推进日志（ld_modtracker）并处理工作流反写；"
    "若目标推进关系设置了自动推进（autopush=yes）且任务已全部完成，官方逻辑会自动完成推进。"
    "【参数】module + record_id + stagename_target（目标阶段名，可先用 get_advance_tasks 查看可推进目标及 advanceid，也可直接传 advanceid）。"
    "【两阶段确认】首次调用不传 confirm_token：校验通过后返回 code=confirm_required + preview（当前阶段/目标阶段/任务状态摘要/未完成必做任务）+ confirm_token；"
    "必须把 preview 完整展示给用户并获得明确同意后，用完全相同的参数 + confirm_token 再次调用完成推进；"
    "用户要求修改则按新参数重新调用（不带 token）；用户拒绝则终止，不得自行确认。"
    "【边界】映射模块类目标（如线索「升级到客户」）、非字段跟踪方式的模块不支持直接推进；回推目标（is_back_push=yes）按官方配置允许。"
    "需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填）"},
            "record_id": {"type": "string", "description": "记录 ID（必填）"},
            "stagename_target": {"type": "string", "description": "目标阶段名（与 advanceid 二选一；来自 get_advance_tasks 的 relations[].stagename_target）"},
            "advanceid": {"type": "string", "description": "推进关系 ID（与 stagename_target 二选一，来自 get_advance_tasks 的 relations[].advanceid）"},
            "confirm_token": {"type": "string", "description": "两阶段确认令牌：首次调用不传（返回预览+令牌）；用户确认后用相同参数加令牌再调完成推进"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "record_id"],
    },
)
def advance_push(
    module: str,
    record_id: str,
    stagename_target: str | None = None,
    advanceid: str | None = None,
    confirm_token: str | None = None,
    instance: str = "default",
) -> str:
    """推进单据阶段（两阶段确认；走官方推进方法记录推进日志）。"""
    params: dict[str, Any] = {"target_module": module, "record_id": str(record_id)}
    if stagename_target:
        params["stagename_target"] = stagename_target
    if advanceid:
        params["advanceid"] = str(advanceid)
    if confirm_token:
        params["confirm_token"] = confirm_token
    return _run(instance, "advance_push", params)


@registry.register(
    "get_advance_logs",
    "查看单据的推进日志：阶段变更记录（每次推进的前后阶段值、操作人、时间，来自官方推进方法写入的 ld_modtracker 日志）"
    "以及任务完成日志（每个推进任务的完成流水：任务类型/任务结果/完成人/时间）。"
    "适用于「这个单据的推进历史」「阶段什么时候推进的、谁推进的」「任务什么时候完成的」等问题。"
    "【参数】module + record_id；include_tasklog 默认 true 同时返回任务完成日志；limit 每类日志条数（默认 50，最大 200）。"
    "注意：任务完成日志为原始流水，回推后早于当前阶段推进时间的旧日志已失效。需先调用 login 登录。",
    {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "模块索引名（必填）"},
            "record_id": {"type": "string", "description": "记录 ID（必填）"},
            "include_tasklog": {"type": "boolean", "description": "是否同时返回任务完成日志（默认 true）"},
            "limit": {"type": "integer", "description": "每类日志返回条数（默认 50，最大 200）"},
            "instance": {"type": "string", "description": "账套名（默认 default）"},
        },
        "required": ["module", "record_id"],
    },
)
def get_advance_logs(
    module: str,
    record_id: str,
    include_tasklog: bool = True,
    limit: int = 50,
    instance: str = "default",
) -> str:
    """查看单据推进日志（阶段变更记录）及任务完成日志。"""
    params: dict[str, Any] = {"target_module": module, "record_id": str(record_id)}
    if not include_tasklog:
        params["include_tasklog"] = "0"
    if limit != 50:
        params["limit"] = int(limit)
    return _run(instance, "get_advance_logs", params)


# ── 启动 ──
if __name__ == "__main__":
    instances = load_instances()
    logger.info("启动 v4.8 零依赖版，已配置账套: %s", ", ".join(instances.keys()) or "(无，请先调用 setup_instance 注册)")
    run_stdio(
        registry,
        server_info={"name": "lingdang-crm-auth", "version": "4.8.0"},
    )
