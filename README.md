# 灵当CRM WorkBuddy 连接器 — 交付包 v4.3（零依赖 · 支持 URL 一键安装）

> 自包含交付物：**配置向导 Skill + 零依赖 MCP 运行层**。
> 客户只需「提供下载地址 → 说一句话配置 → 点一次信任 → 提供账号密码」，之后全部纯对话使用。
>
> ★ v4.0：运行层 **零第三方依赖**（纯 Python 标准库），客户机器无需安装 Python 环境。
> ★ v4.1：新增 **URL 一键安装** —— 交付包可放云环境/服务器，客户给一个下载链接即可自动下载安装。
> ★ v4.2：**create_record 支持带明细分录**（如销售订单多行产品）——走 CRM 官方 save 链路落主表+明细表，
>         新装交付包账套注册表为空（不带任何开发测试账套）。
> ★ v4.3：**create_record 明细金额补全 + lineitem_id 防护**（基于官方 newApiSaveProduct 对照实测）——
>         只传 listprice+tax2 自动算含税单价/价税合计/税额，主表合计=Σ明细税额；
>         禁止透传自增主键 lineitem_id（防 Duplicate entry）；缺金额字段返回 warning 提示。

## 目录结构

```
lingdang-crm-setup/
├── SKILL.md                  # Skill 入口：触发词 + 配置向导标准流程
├── README.md                 # 本文件（交付说明）
├── mcp/                      # 内置 v4 MCP server（运行层，勿手工改动）
│   ├── server.py             #   9 个工具：setup_instance / list_instances / login /
│   │                         #   logout / list_modules / get_module_fields /
│   │                         #   query_module_data / get_record / create_record
│   ├── mcp_stdio.py          #   自研 MCP stdio 协议核心（Content-Length 帧 + NDJSON 双兼容）
│   ├── config.py             #   配置 + instances.json 账套注册表读写（热重载）
│   ├── crm_client.py         #   HTTP 客户端（urllib，session_token 认证，24h 有效）
│   ├── requirements.txt      #   ★ 零依赖声明（无需安装任何包）
│   ├── .env.example          #   可选环境变量样例（客户一般无需配置）
│   └── instances.json        #   账套注册表（只存名称+地址，不存密码）
├── scripts/                  # 配置向导脚本（零依赖，仅标准库）
│   ├── install_from_url.py   # ★ URL 一键安装：下载→校验→解压到 skills 目录
│   ├── setup.py              #   注册账套：探活 → 写注册表 → 预置 mcp.json
│   ├── diagnose.py           #   多级连接诊断（格式/解析/首页/登录接口）
│   └── list_instances.py     #   列出已配置账套
└── references/
    └── guide.md              # 客户引导话术 + 常见错误处理手册
```

## 两种客户安装方式

### 方式 A：URL 一键安装（推荐，v4.1 新增）

实施方把 zip 放到云环境/客户可访问的地址，客户只需在对话里给一个链接：

```
① 对话：从 https://你的地址/lingdang-crm-setup-v4.1.zip 下载安装灵当CRM，
         然后连接我的CRM，地址是 http://192.168.1.100/crm，叫"上海账套"
② 信任：WorkBuddy 连接器管理页对 lingdang-crm-auth 点一次「信任」（仅首次）
③ 登录：admin / ******** → 之后纯对话查询、建单、切账套
```

WorkBuddy 会自动：下载 zip → 校验（是 zip / 含 SKILL.md / 无非法路径）→
解压到 `~/.workbuddy/skills/` → 跑配置向导注册账套。

### 方式 B：手动安装（兜底，与方式 A 完全等价，可随时混用）

**B1 装包式（客户无命令行）**
```
① 安装：把 lingdang-crm-setup 文件夹放入 ~/.workbuddy/skills/，重启 WorkBuddy
② 对话：连接我的CRM，地址是 http://192.168.1.100/crm，叫"上海账套"
③ 信任：WorkBuddy 连接器管理页对 lingdang-crm-auth 点一次「信任」（仅首次）
④ 登录：admin / ******** → 之后纯对话查询、建单、切账套
```

**B2 命令行式（实施方 / 技术型客户，零依赖）**
```
python scripts/setup.py --name 上海账套 --url http://192.168.1.100/crm
python scripts/list_instances.py            # 列出已配置账套
python scripts/diagnose.py --name 上海账套  # 多级连接诊断
```

**B3 纯手动编辑（极端兜底，仅限熟悉配置的实施方）**
- 账套注册表：手动编辑 `mcp/instances.json`（name + url，不存密码）
- 连接器配置：手动编辑 `~/.workbuddy/mcp.json` 加入 `lingdang-crm-auth` 条目
  （command 用 WorkBuddy 内置 Python，args 指向本包 `mcp/server.py`）
- 完成后仍需在连接器管理页点一次「信任」

## 安装前置条件（v4 已大幅简化）

1. **Python 环境**：✅ **无需任何安装**。运行层只依赖 Python 标准库（Python 3.8+），
   自动使用 WorkBuddy 内置 Python。旧版需要的 `mcp / httpx / pydantic-settings`
   三个第三方依赖在 v4 中已彻底移除。
2. **PHP 端部署**（CRM 服务器，由实施方完成）：
   - `crmapi/mcp_login.php`（登录端点）
   - `crmapi/modules/Mcp_auth_api.php`（session 认证模块）
   - `crmapi/modules/Mcp_api.php`（业务基类）
3. **首次信任**：连接器条目写入 mcp.json 后需在连接器管理页点一次「信任」（系统安全机制）。

## 多账套设计

- 账套注册表 `mcp/instances.json` 只存 `账套名 → 访问地址`，**密码不落盘**。
- 所有 MCP 业务工具带 `instance` 参数（默认 `default`）；登录、查询时指定账套名即切换。
- **热重载**：对话中通过 `setup_instance` 新增账套立即生效，无需重启连接器。
- 兼容老配置：若 mcp.json/.env 显式配置了 `CRM_BASE_URL`，自动作为 `default` 账套兜底。

## 工具一览（MCP v4）

| 工具 | 说明 |
|---|---|
| `setup_instance(name, base_url)` | 注册账套（探活通过后保存） |
| `list_instances()` | 列出账套及登录状态 |
| `login(username, password, instance)` | 登录（session 24h 有效） |
| `logout(instance)` | 注销 |
| `list_modules(instance)` / `get_module_fields(modules, instance)` | 模块/字段清单 |
| `query_module_data(...)` / `get_record(...)` / `create_record(...)` | 查询/详情/创建 |

### create_record 带明细（v4.2 新增，v4.3 补齐金额）

`create_record` 支持传 `detail` 参数新增**带明细分录**的单据（如销售订单多行产品），
底层走 CRM 官方 `save` 方法（非脚本直插），主表 + 明细表一起落库，单号自动生成：

```
create_record(
  module = "SalesOrder",
  data   = { "account_id": 382, "signed_data": "2026-08-18" },
  detail = [
    { "product_no": "P001", "quantity": 2, "listprice": 100, "tax2": 13, "comment": "第一行" },
    { "product_no": "P002", "quantity": 1, "listprice": 200, "tax2": 13 }
  ],
  instance = "上海账套"
)
```

- 明细行产品传 `productid`（数字 id）或 `product_no`（编码，自动转 id）均可；
- **金额字段**（v4.3）：明细金额由 CRM 官方链路**透传**（不传则落库 0）。为免"合计没值"，
  MCP 侧自动按标准公式补全（调用方已传的值优先，不覆盖）：
  - `taxprice`（含税单价）= `listprice`（不含税）× (1 + `tax2`/100)，传了 listprice+tax2 时自动算；
  - `list_amount`（价税合计）= `taxprice` × `quantity`，传了 taxprice 时自动算；
  - `tax_amount`（税额）= (`taxprice` − `listprice`) × `quantity`（无 listprice 时按税率反推），传了 taxprice 时自动算；
  - 主表 `total`/`subtotal` 自动 = Σ 明细 `tax_amount`；
  - 若一行**未传任何金额字段**，接口返回 `warning` 提示（该行金额为 0，可传 productid 让 CRM 带出产品默认价）；
  - ⚠ 不要传 `lineitem_id`（明细自增主键，create 模式下显式传值会与自增冲突报
    `Duplicate entry`，接口会忽略该值）；
- `get_record(..., include_detail=True)` 可读回明细，按明细表名分组返回
  （如 `ld_salesorderdetail` 明细行 + `ld_products` 产品字典）；
- 表头引用字段（如 `account_id`）传客户编码/名称或数字 id 均可（自动解析）。

## 测试情况（2026-08-18，v4.2 全链路回归通过；2026-08-20 追加 v4.3 明细金额修复）

- [x] **零依赖启动**：WorkBuddy 内置 Python 3.13.12（无 mcp/httpx/pydantic-settings）直接运行
- [x] **双协议兼容**：Content-Length 帧（WorkBuddy/TS SDK）与 NDJSON（官方 Python SDK 2.0）均握手成功
- [x] **官方 SDK client 直连**：initialize / tools/list / 9 工具调用全通过
- [x] **真实账套全链路**：login(zkt/123) → list_modules(300 模块) → query(Accounts 真实数据) → get_record(记录+明细) → logout
- [x] **create_record 带明细（v4.2）**：SalesOrder + 2 行产品明细 → 主表+明细表落库 → get_record 读回 2 行明细 + 产品字典 → 单号自动生成
- [x] **官方链路对照（v4.3 修复依据）**：CLI 直调官方 `newApiSaveProduct` 对照落库——
  明细金额字段（taxprice/list_amount/tax_amount）官方**仅透传不做计算**；主表 total = Σ 明细 tax_amount；
  lineitem_id 纯新增由 CRM 自增（传了会 Duplicate entry）
- [x] **明细金额补全（v4.3）**：只传 listprice+tax2 → taxprice/list_amount/tax_amount/total 全部自动落库
  （实测 100×16% → 116/116/16/16）；传 taxprice → list_amount/tax_amount 自动补全
- [x] **lineitem_id 防护（v4.3）**：create 显式传 lineitem_id=999 被忽略，落库仍为自增值，无主键冲突
- [x] **缺金额字段提示（v4.3）**：明细不传任何金额字段 → 返回 warning 明确提示
- [x] **多账套隔离**：各账套独立登录态，instance 参数切换验证通过
- [x] **干净交付（v4.2）**：交付包账套注册表强制为空，新装无任何默认/开发测试账套
- [x] **向导脚本**：setup.py 自动探测 WorkBuddy 内置 Python，探活→注册→预置 mcp.json 全通过
- [x] **URL 一键安装**：本地 HTTP 模拟云环境，中文文件名 zip 下载→校验→解压→SKILL.md/server.py 就位全通过
- [x] 探活拒绝坏地址 / 未登录提示 / session 过期处理 / 热重载新增账套立即可用
