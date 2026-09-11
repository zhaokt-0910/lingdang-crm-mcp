---
name: lingdang-crm-setup 
description: 灵当CRM 账套配置向导。当用户提到「连接/接入/添加/配置 CRM 账套」「CRM 地址」「加个账套」「诊断 CRM 连接」「CRM 连不上」「查看已配置账套」「有哪些账套」等配置类诉求时使用。负责：探活验证 CRM 地址 → 写入账套注册表 → 预置 WorkBuddy 连接器条目 → 引导客户首次信任与登录。
agent_created: true
---

# 灵当CRM 配置向导（lingdang-crm-setup）

> 本 Skill 只负责**配置/管理**（一次性、偶发操作）。日常**业务使用**（登录、查询、建单、退出）不经过本 Skill，由 MCP 连接器 `lingdang-crm-auth` 的标准工具直接处理。

## 分工边界

| 动作 | 走哪层 |
|---|---|
| 连接账套、加账套、诊断、查看账套 | **本 Skill**（要改本机 instances.json / mcp.json） |
| 登录、查询、创建、注销（日常高频） | **MCP v4**（lingdang-crm-auth 工具，无需 Skill） |

## 包结构

```
lingdang-crm-setup/
├── SKILL.md
├── mcp/                  # 内置 v4 MCP server（运行层，零依赖，勿改）
│   ├── server.py / config.py / crm_client.py / mcp_stdio.py
│   ├── requirements.txt  # 零依赖：无需安装任何第三方包
│   └── instances.json    # 账套注册表（只存名称+地址，不存密码）
├── scripts/
│   ├── install_from_url.py # ★ 从 URL 一键安装（下载→校验→解压到 skills 目录）
│   ├── setup.py            # 注册账套：探活 → 写注册表 → 预置 mcp.json
│   ├── diagnose.py         # 多级连接诊断
│   └── list_instances.py   # 列出已配置账套
└── references/guide.md   # 引导话术 + 常见错误处理
```

## 标准流程

### 场景零：从 URL 下载安装（客户首次使用）

客户把交付包 zip 放在云环境/服务器上，只给 WorkBuddy 一个下载地址，即可自动安装：

1. 从用户描述中提取**下载地址**（zip 链接）。
2. 执行：
   ```
   python scripts/install_from_url.py --url <zip下载地址>
   ```
   （可选 `--skills-dir` 指定 skills 目录，缺省 `~/.workbuddy/skills`）
3. 解析 JSON 输出：
   - `code: success` → 已解压到 skills 目录，转场景一继续配置账套（用户可一句话同时给地址和账套信息）。
   - `code: error` → 按 `msg` 提示（地址不可达 / 不是 zip / 不是灵当交付包 / 含非法路径）。
4. **注意**：新装的 Skill 需新会话生效；当前会话可直接读取刚解压的 SKILL.md 继续执行配置流程。

### 场景一：连接/添加账套
1. 从用户描述中提取 `账套名` 与 `访问地址`（缺哪个问哪个）。
2. 探活注册：
   ```
   python scripts/setup.py --name <账套名> --url <地址> [--python <python路径>]
   ```
   - `--python` 缺省自动检测（优先 WorkBuddy 内置 Python）；v4 零依赖，任意 Python 3.8+ 均可。
3. 解析 JSON 输出：
   - `code: success` → 转述结果；若 `mcp_json.changed: true`，**引导客户**：请在 WorkBuddy「连接器管理」页对 `lingdang-crm-auth` 点一次「信任」（系统安全机制，仅首次需要）。
   - `code: error` → 按 `msg` 提示客户修正（地址写错/未开端口/路径不对，见 guide.md）。
4. 请客户提供该账套的**账号密码**（不落盘，会话内使用），引导其调用 MCP `login(instance=账套名, ...)` 登录后即可开始使用。

### 场景二：诊断连接（连不上时）
```
python scripts/diagnose.py --url <地址>
```
按输出的 `steps` 逐级判断：地址格式 → 域名解析 → 首页 → 登录接口，告诉客户问题在哪一级。

### 场景三：查看已配置账套
```
python scripts/list_instances.py
```

### 场景四：删除账套（可选）
```
python -c "import sys; sys.path.insert(0,'scripts'); import common; print(common.remove_instance('账套名'))"
```

## 重要约定

- **不落盘密码**：instances.json 与 mcp.json 均不保存密码；密码只在登录对话中出现。
- **一次信任**：mcp.json 条目已存在时 `changed=false`，无需再次引导信任。
- **探活失败不保存**：地址不可达时 setup.py 会拒绝写入，先诊断再注册。
- **多账套切换**：所有 MCP 业务工具均有 `instance` 参数（默认 `default`），登录与查询时带上对应账套名即可；客户只需在对话中指定账套名。

## 常见错误处理速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 首页访问失败 HTTP 404/403 | 地址路径不对（缺 /crm） | 补全路径，见 guide.md |
| 连接失败 Connection refused | 端口未开 / 服务未启动 | 检查 CRM 服务与端口 |
| 域名解析失败 | 域名写错 | 核对地址，改用 IP |
| 登录接口返回非 JSON | 该地址不是灵当CRM / PHP 端未部署 | 确认 mcp_login.php 已部署到 crmapi/ |
| 信任后仍提示未连接 | 未重启会话/未启用条目 | 在连接器管理页启用并重启对话 |

完整话术与排查细节见 `references/guide.md`。
