# AI 个性化学习 Bot

通过飞书私聊陪一位学习者阅读 AI 文章：从文章来源挑选内容，结合水平、兴趣、目标及反馈生成中文导读；可继续追问。内置文章来源包括 MIT News AI、Hugging Face Blog 与 Berkeley AI Research，可用 `RSS_FEEDS` 替换。Linux 可使用 [systemd 部署说明](deploy/README.md) 作为常驻服务；现有 Windows 使用方式仍列于下文。

> **上线前必须另行取得明确授权。** 启动服务、绑定 `/start`、触发 `/next` 或等待每天北京时间 09:00 都可能真实向飞书发送消息；第三方 API 会外发学习画像、文章正文、提问等内容，并可能产生费用。请先确认收件人、独立飞书应用的权限、服务商数据政策/价格/预算及首日发送安排，再进行实际联调和上线。本文档及示例配置不是发送授权，也不表示未完成的限额、补发和去重逻辑已经过线上验证。

## 功能与边界

- 飞书长连接接收私聊文本与卡片操作，无需公网 Webhook；配置一个 `ALLOWED_FEISHU_OPEN_ID`，由该用户在**独立飞书应用**私聊发送 `/start` 完成绑定。长连接退出后程序会尝试重连，仍需监控状态；切勿让旧 bot 共用应用凭据并同时占用长连接。
- 阅读正文并按画像匹配后续学习文档；水平、兴趣、目标、难度反馈与问答次数保存在本地 SQLite。
- `DAILY_SEND_TIME=09:00`、`TIMEZONE=Asia/Shanghai` 指定应用内的北京时区定时；不是 systemd timer。没有绑定收件人时不应期待自动推送。`DAILY_CATCHUP_MINUTES` 是补发窗口配置项，断线、重启、跨日及首日实际行为须上线前专门验证，不能据此承诺必达或绝不重复。
- 默认 `AI_PROVIDER=api` 调用第三方兼容接口；可单独配置 `AI_PROVIDER=codex` 使用本地 Codex。旧文档中的 `auto` 不是当前 `config.py` 接受的值，不能假定自动回退。文章正文、本地画像与问答记录保存在 SQLite；调用远端 API 会外发相关输入。
- 配置了 `API_DAILY_MAX_CALLS`、`API_MAX_OUTPUT_TOKENS` 和 `API_TIMEOUT_SECONDS` 等预算相关键；它们的实际执行效果需与部署时的代码及测试逐项核对，不能替代服务商控制台的费用上限和人工监控。

## 1. Windows 本地准备（保留原使用方式）

可使用 Python 3.12。在 PowerShell 中运行：

```powershell
cd D:\项目\ai-learning-bot
& 'D:\项目\ai-coding-agent-bot\.venv\Scripts\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在私有 `.env` 中填写新建的飞书应用 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、唯一收件人 `ALLOWED_FEISHU_OPEN_ID`（`ou_...`），以及服务商确认支持的 `NAN_MODEL` 和**新签发**的 `NAN_API_KEY`。已在聊天或其他公共场合暴露的旧飞书凭据、API Key 必须先在各自后台撤销/轮换，不要再使用或保存；不要将真实密钥写入 `.env.example`、命令行、日志或版本库。`NAN_BASE_URL` 示例是已选定的第三方 `https://meta-api.vip/v1`，使用前核实接口、费率及数据处理条款。不要猜测模型 ID；`NAN_MODEL` 留空时 API 模式不能通过启动配置校验。

网络代理只在必要时用 `PROXY_URLS` 明确配置；留空表示不指定代理，Linux 服务器上不要照搬 Windows 本机的 7890/7897/7896 端口。飞书应用需启用机器人能力、长连接订阅 `im.message.receive_v1` 和 `card.action.trigger`，并开通发送消息所需权限；只允许一个指定用户的私聊，不建议加入群聊。

## 2. 本地检查与交互

以下单元测试不需要连接飞书或第三方 API；只有**另行获得真实发送和费用授权后**才手动运行 `bot.py`，并由指定用户执行 `/start`、`/next` 等联调操作：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 已获授权后才执行：
.\.venv\Scripts\python.exe bot.py
```

绑定后可设置画像并主动选文（以下为会真实发消息、可能调用付费 API 的示例，不属于离线测试）：

```text
/start
/level beginner
/topics 大模型,Agent,RAG
/goal 能独立做一个个人知识库
/next
```

收到文章后可直接追问，例如“文中的 token 是什么？用生活例子解释”。

## 3. 常驻运行

Linux 按 [deploy/README.md](deploy/README.md) 安装独立服务账户、私有配置和 systemd 服务；即使服务已安装，也必须等到另行授权后才启用/启动。服务数据应写入 `/var/lib/ai-learning-bot`，日志由 `journalctl` 查看；不要依赖 Windows 的 `logs\bot.log`。

Windows 方式保持可用：在已授权并验证手动运行后，以普通用户 PowerShell 执行 `install-startup-task.ps1`，它会创建登录后自动启动的计划任务 `AI-Learning-Bot`。日志位于 `logs\bot.log`，默认学习数据位于 `data\learning.db`。Linux 和 Windows 不要使用同一飞书应用并发运行。

## 命令

| 命令 | 用途 |
|---|---|
| `/start` | 首次私聊绑定并查看使用说明 |
| `/today` | 再看当前文章 |
| `/next` | 立即推荐下一篇 |
| `/profile` | 查看学习画像 |
| `/level beginner/intermediate/advanced` | 设置水平 |
| `/topics 主题1,主题2` | 设置兴趣 |
| `/goal 目标` | 设置学习目标 |
| `/feedback easy/ok/hard` | 反馈文章难度 |

## 隐私与安全

- `.env`、数据库目录 `data/` 和日志目录 `logs/` 已加入 `.gitignore`；Linux `/etc/ai-learning-bot/bot.env` 与 `/var/lib/ai-learning-bot` 位于项目外，仍需通过系统权限及受控备份保护。不要把这两个路径或数据库内容粘贴到工单/日志。
- Codex 模式需要额外安装及授权；其进程参数使用 `read-only` 沙箱，但不意味着网络调用或数据处理已经被独立审计。
- 第三方 API 模式会向所配置服务商外发学习画像、文章内容和问题；文章来源站点和飞书自身也处理通信内容。若不接受外发，请不要启动 API 模式；切换 Codex 也要先评估对应账号与网络行为。
- 已公开的任何旧凭据在正式使用前必须先**撤销并轮换**。新凭据只放在访问受限的私有配置中；检查服务日志时防止敏感内容被转发。上线前核对每日调用上限、输出长度、超时、成本和收件人；保存备份和恢复演练记录。
