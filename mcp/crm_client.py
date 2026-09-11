"""灵当CRM 动态 API 客户端 v4（用户名+密码登录认证，零依赖：urllib 替代 httpx）。

与 v3 的区别：
  - HTTP 层从 httpx 换成标准库 urllib（同步），不再有第三方依赖
  - 接口、调用链、返回结构完全不变

调用链路：
  1. POST {base}/crmapi/mcp_login.php  body {username, password}  →  获取 session_token
  2. POST {base}/crmapi/crmoperation.php  body {module:Mcp_auth, func, session_token, ...业务参数}
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("lingdang-crm.client")


@dataclass
class CRMConfig:
    base_url: str
    http_timeout: float = 15.0
    login_path: str = "/crmapi/mcp_login.php"
    operation_path: str = "/crmapi/crmoperation.php"


class CRMError(Exception):
    """CRM API 调用异常。"""


class CRMNotLoggedInError(CRMError):
    """未登录（session_token 不存在），需先调用 login。"""


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    """POST JSON 并解析响应。网络/HTTP/JSON 错误统一抛 CRMError。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    raw = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        if e.code >= 500 and not detail.strip():
            raise CRMError(
                f"HTTP {e.code}: 后端服务异常（响应体为空，通常是 PHP Fatal Error，"
                f"请检查 CRM 服务器 PHP 错误日志定位具体原因）"
            ) from None
        raise CRMError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise CRMError(f"连接失败: {reason}") from None
    except OSError as e:
        raise CRMError(f"网络错误: {e}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise CRMError(f"返回非JSON: {raw[:300]}") from None


class LingdangClient:
    """灵当CRM 动态 API 客户端（登录认证版，同步）。"""

    def __init__(self, config: CRMConfig):
        self._base = config.base_url.rstrip("/")
        self._timeout = config.http_timeout
        self._login_path = config.login_path
        self._operation_path = config.operation_path
        self._session_token: str | None = None
        self._username: str | None = None
        self._user_id: int | None = None

    @property
    def is_logged_in(self) -> bool:
        return self._session_token is not None

    @property
    def username(self) -> str | None:
        return self._username

    # ─────────────────── 登录 / 注销 ───────────────────

    def login(self, username: str, password: str) -> dict:
        """用户名+密码登录，获取 session_token。"""
        url = f"{self._base}{self._login_path}"
        data = _post_json(url, {"username": username, "password": password}, self._timeout)

        if data.get("code") != "success" or not data.get("session_token"):
            raise CRMError(f"登录失败: {data.get('msg', data)}")

        self._session_token = data["session_token"]
        self._username = data.get("username", username)
        self._user_id = data.get("user_id")
        logger.info("登录成功: user=%s id=%s", self._username, self._user_id)
        return data

    def logout(self) -> dict:
        """注销当前 session。"""
        if not self._session_token:
            return {"code": "success", "msg": "未登录，无需注销"}
        result = self._call("logout", {})
        self._session_token = None
        self._username = None
        self._user_id = None
        return result

    # ─────────────────── 底层：session 认证调用 ───────────────────

    def _call(self, func: str, params: dict | None = None) -> dict:
        """调用 Mcp_auth 模块业务接口（携带 session_token）。"""
        if not self._session_token:
            raise CRMNotLoggedInError("未登录，请先调用 login 工具")

        body: dict[str, Any] = {
            "module": "Mcp_auth",
            "func": func,
            "session_token": self._session_token,
        }
        if params:
            body.update(params)

        url = f"{self._base}{self._operation_path}"
        data = _post_json(url, body, self._timeout)

        # session 过期或业务错误统一处理
        if isinstance(data, dict):
            code = data.get("code")
            msg = data.get("msg", "")
            # 服务端返回 500 + session/登录关键词 → 视为 session 过期，清除本地 token
            if code == "500" and ("session" in msg or "登录" in msg):
                self._session_token = None
                raise CRMNotLoggedInError(msg)
            # PHP 兑底 fatal error handler 返回的结构化错误
            if code == "fatal":
                detail = f"{msg} (file={data.get('file', '?')}, line={data.get('line', '?')})"
                raise CRMError(detail)
            if code in ("error", "500", "400"):
                raise CRMError(str(msg or data))
            return data
        return {"code": "success", "raw": data}

    # ─────────────────── 业务工具（与 v2/v3 接口一致） ───────────────────

    def list_modules(self) -> dict:
        """列出全部模块（name 索引名 / zhlabel 中文名 / enlabel 英文名）。"""
        return self._call("getModules")

    def get_module_fields(self, modules: list[str]) -> dict:
        """获取指定模块的字段清单（表头 + 表体）。"""
        return self._call("getFields", {"getfields": modules})

    def query_module_data(
        self,
        module: str,
        fields: list[str] | None = None,
        filters: dict | None = None,
        order_by: str | None = None,
        order: str = "desc",
        limit: int = 50,
        page: int = 1,
        count_only: bool = False,
    ) -> dict:
        """通用条件查询。count_only=True 时仅返回总数（不取记录明细）。"""
        params: dict[str, Any] = {
            "target_module": module,
            "order": order,
            "limit": max(1, min(int(limit), 100)),
            "page": max(1, int(page)),
        }
        if fields:
            params["fields"] = fields
        if filters:
            params["filters"] = filters
        if order_by:
            params["order_by"] = order_by
        if count_only:
            params["count_only"] = True
        return self._call("query", params)

    def get_record(
        self,
        module: str,
        record_id: str,
        fields: list[str] | None = None,
        include_detail: bool = True,
    ) -> dict:
        """单条记录详情（含表体明细）。"""
        params: dict[str, Any] = {
            "target_module": module,
            "record_id": str(record_id),
            "include_detail": bool(include_detail),
        }
        if fields:
            params["fields"] = fields
        return self._call("getRecord", params)

    def get_picklist_values(self, module: str, fields: list[str] | None = None) -> dict:
        """获取下拉框字段的有效值（走官方 cls_PickList 链路，与前台下拉框一致）。"""
        params: dict = {"target_module": module}
        if fields:
            params["fields"] = fields
        return self._call("getPicklistValues", params)

    def create_record(
        self,
        module: str,
        data: dict,
        detail: list | None = None,
        upgradefrommodule: str | None = None,
        upgradefromid: str | None = None,
        upgradestatus: str | None = None,
        confirm_token: str | None = None,
        allow_duplicate: bool = False,
        is_public_account: str | None = None,
    ) -> dict:
        """动态创建记录（可携带表体明细行，走 CRM 官方 save 链路）。

        线索升级转换：传 upgradefrommodule=Leads + upgradefromid + upgradestatus=升级为客户，
        官方保存链路走升级逻辑（更新线索状态、建立关联）。
        两阶段确认：首次调用（不带 confirm_token）返回 confirm_required + 预览，
        用户确认后携相同参数 + confirm_token 再次调用才真正保存。
        查重/报备：fieldonly=1 字段重复、客户名称报备提醒时返回 need_confirm，
        用户确认后携 allow_duplicate=True 重调跳过（硬性重复 ##error## 不可跳过）。
        公海客户：is_public_account='unassigned' 公海未分配 / 'assigned' 公海已分配，
        配合 data 中的 pool_id 指定目标公海池。
        """
        params: dict = {"target_module": module, "data": data}
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
            params["allow_duplicate"] = "1"
        if is_public_account:
            params["is_public_account"] = is_public_account
        return self._call("create", params)

    def update_record(
        self,
        module: str,
        record_id: str,
        data: dict | None = None,
        detail: list | None = None,
        delete_lineitem_ids: list | None = None,
        allow_duplicate: bool = False,
    ) -> dict:
        """动态编辑记录（mode=edit 走 CRM 官方编辑保存链路）。

        参数语义（与 PHP update() 接口一致）：
          - data: 表头字段增量更新，未传字段保留原值
          - detail: 明细行数组
            · 行内带 lineitem_id → 更新该行（未传字段保留旧值）
            · 行内不带 lineitem_id → 追加新行
          - delete_lineitem_ids: 要删除的明细行 lineitem_id 数组
          - detail 与 delete_lineitem_ids 均不传 → 明细不动
          - allow_duplicate: 用户确认后跳过可继续类查重/报备提醒
        """
        params: dict[str, Any] = {
            "target_module": module,
            "record_id": str(record_id),
        }
        if data:
            params["data"] = data
        if detail:
            params["detail"] = detail
        if delete_lineitem_ids:
            params["delete_lineitem_ids"] = delete_lineitem_ids
        if allow_duplicate:
            params["allow_duplicate"] = "1"
        return self._call("update", params)

    # ─────────────────── 工商查询与回填 ───────────────────

    def business_search(self, module: str, keyword: str, search_type: str = "exact") -> dict:
        """查询模块的工商信息；仅返回结果或候选，不会写入 CRM。"""
        return self._call("businessSearch", {
            "target_module": module,
            "keyword": keyword,
            "search_type": search_type,
        })

    def business_backfill_preview(
        self,
        module: str,
        business_data: dict,
        selected_fields: list[str] | None = None,
    ) -> dict:
        """把用户选定的工商结果转换为 CRM 回填字段预览，不会写入 CRM。"""
        params: dict[str, Any] = {"target_module": module, "business_data": business_data}
        if selected_fields:
            params["selected_fields"] = selected_fields
        return self._call("businessBackfillPreview", params)

    def business_backfill_apply(
        self,
        action: str,
        module: str,
        business_data: dict,
        data: dict | None = None,
        selected_fields: list[str] | None = None,
        record_id: str | None = None,
        detail: list | None = None,
        confirm_token: str | None = None,
        confirmed: bool = False,
    ) -> dict:
        """在客户确认后将工商预览字段合并到创建或编辑请求。"""
        if not confirmed:
            return {
                "code": "confirmation_required",
                "msg": "工商回填字段尚未获得客户明确确认，未写入 CRM。",
            }
        if action not in ("create", "update"):
            return {"code": "error", "msg": "action 仅支持 create 或 update"}

        preview = self.business_backfill_preview(module, business_data, selected_fields)
        if preview.get("code") != "success":
            return preview
        backfill_data = preview.get("data") or {}
        if action == "create" and preview.get("no_business_no_save") and not backfill_data:
            return {
                "code": "error",
                "msg": "该模块要求新建记录必须有已确认的工商回填信息，但未生成有效回填字段。",
            }
        merged_data = dict(data or {})
        # 仅将客户在 preview 中确认的工商字段覆盖到业务数据，其他输入字段保持不变。
        merged_data.update(backfill_data)
        if not merged_data:
            return {"code": "error", "msg": "没有可写入的业务字段或工商回填字段"}

        if action == "create":
            return self.create_record(module, merged_data, detail=detail, confirm_token=confirm_token)
        if not record_id:
            return {"code": "error", "msg": "编辑工商回填时缺少 record_id"}
        return self.update_record(module, record_id, data=merged_data, detail=detail)

    def query_module_summary(
        self,
        module: str,
        sum_fields: list[str] | None = None,
        filters: dict | None = None,
    ) -> dict:
        """聚合查询：count + sum（不返回明细记录）。"""
        params: dict[str, Any] = {"target_module": module}
        if sum_fields:
            params["sum_fields"] = sum_fields
        if filters:
            params["filters"] = filters
        return self._call("querySummary", params)

    def resolve_reference(
        self,
        module: str,
        value: str | list[str] | None = None,
        field: str | None = None,
        limit: int = 10,
        page: int = 1,
    ) -> dict:
        """名称/编号 → record_id 查找器。value 可为字符串（单个）或列表（批量）。"""
        params: dict[str, Any] = {"target_module": module}
        if isinstance(value, list):
            params["values"] = value
        elif value is not None:
            params["value"] = value
        if field:
            params["field"] = field
        if limit != 10:
            params["limit"] = max(1, min(int(limit), 50))
        if page != 1:
            params["page"] = max(1, int(page))
        return self._call("resolveReference", params)

    def resolve_href_by_ids(self, module: str, ids: list[str | int]) -> dict:
        """批量将记录 ID 解析为 CRM 配置的超链接字段值。"""
        return self._call("resolveHrefByIds", {"target_module": module, "ids": ids})

    def resolve_ids_by_href(self, module: str, href_values: list[str]) -> dict:
        """批量按 CRM 超链接字段值精确解析记录 ID。"""
        return self._call("resolveIdsByHref", {"target_module": module, "href_values": href_values})

    # ─────────────────── ERP 库存 ───────────────────

    def get_erp_stock(self, productno: list[str]) -> dict:
        """按产品编码批量查询 ERP 实时库存。"""
        return self._call("getErpStock", {"productno": productno})

    # ─────────────────── 公海客户操作 ───────────────────

    def get_public_pool_list(self) -> dict:
        """获取公海池配置列表（公海池 ID、名称、管理员、成员等）。"""
        return self._call("getPublicPoolList", {})

    def allocate_account(self, record_id: str, allocatetype: int = 3, userid: str | None = None) -> dict:
        """领取/分配公海客户。allocatetype=3 领取（默认当前用户），allocatetype=2 分配给指定用户。"""
        params: dict[str, Any] = {"record_id": str(record_id), "allocatetype": int(allocatetype)}
        if userid:
            params["userid"] = str(userid)
        return self._call("allocateAccount", params)

    def release_account(self, record_id: str, release_reason: str = "") -> dict:
        """释放公海客户（将已分配客户释放回公海未分配状态）。"""
        params: dict[str, Any] = {"record_id": str(record_id)}
        if release_reason:
            params["release_reason"] = release_reason
        return self._call("releaseAccount", params)

    def delay_account(self, record_id: str, delay_days: int = 0) -> dict:
        """延期公海客户（延长保护期结束日期）。"""
        return self._call("delayAccount", {"record_id": str(record_id), "delay_days": int(delay_days)})

    def convert_account(self, record_id: str, transfer: str, pool_id: str = "") -> dict:
        """公海分配/变更公海。transfer=transfer_to 转公海（需 pool_id），transfer_out 转普通客户。"""
        params: dict[str, Any] = {"record_id": str(record_id), "transfer": transfer}
        if pool_id:
            params["pool_id"] = str(pool_id)
        return self._call("convertAccount", params)

    # ─────────────── 销售线索公海操作 ───────────────

    def get_leads_public_pool_list(self) -> dict:
        """获取线索公海池配置列表。"""
        return self._call("getLeadsPublicPoolList", {})

    def allocate_leads(self, record_id: str, allocatetype: int = 3, userid: str | None = None) -> dict:
        """领取/分配公海线索。allocatetype=3 领取（默认当前用户），allocatetype=2 分配给指定用户。"""
        params: dict[str, Any] = {"record_id": str(record_id), "allocatetype": int(allocatetype)}
        if userid:
            params["userid"] = str(userid)
        return self._call("allocateLeads", params)

    def release_leads(self, record_id: str, release_reason: str = "") -> dict:
        """释放公海线索。"""
        params: dict[str, Any] = {"record_id": str(record_id)}
        if release_reason:
            params["release_reason"] = release_reason
        return self._call("releaseLeads", params)

    def delay_leads(self, record_id: str, delay_days: int = 0) -> dict:
        """延期公海线索。"""
        return self._call("delayLeads", {"record_id": str(record_id), "delay_days": int(delay_days)})

    def convert_leads(self, record_id: str, transfer: str, pool_id: str = "") -> dict:
        """线索公海分配/变更公海。transfer=transfer_to 转公海，transfer_out 转普通线索。"""
        params: dict[str, Any] = {"record_id": str(record_id), "transfer": transfer}
        if pool_id:
            params["pool_id"] = str(pool_id)
        return self._call("convertLeads", params)

    # ─────────────── 工作汇报（日志/周计划/月计划） ───────────────

    # 注意：server.py 的 _run() 以 **kwargs 方式调用下列方法，签名必须能接收关键字参数。
    def workreport_list(self, **params) -> dict:
        """工作汇报列表查询（type: 1日志/2周计划/3月计划）。"""
        return self._call("workreportList", params)

    def workreport_create(self, **params) -> dict:
        """新建工作汇报（日志/周计划/月计划）。"""
        return self._call("workreportCreate", params)

    def workreport_update(self, **params) -> dict:
        """编辑工作汇报（仅创建者、未点评状态可编辑）。"""
        return self._call("workreportUpdate", params)

    # ─────────────── 自动化推进（任务查看/推进/推进日志） ───────────────

    # 注意：server.py 的 _run() 以 **kwargs 方式调用下列方法，签名必须能接收关键字参数。
    def get_advance_tasks(self, **params) -> dict:
        """查看单据自动化推进任务及状态（只读，不触发推进）。"""
        return self._call("getAdvanceTasks", params)

    def advance_push(self, **params) -> dict:
        """推进单据阶段（两阶段确认；走官方推进方法并记录推进日志）。"""
        return self._call("advancePush", params)

    def get_advance_logs(self, **params) -> dict:
        """查看单据推进日志（阶段变更记录）及任务完成日志。"""
        return self._call("getAdvanceLogs", params)
