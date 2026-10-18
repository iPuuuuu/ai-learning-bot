# Linux systemd 部署（单人飞书私聊）

本目录提供**单实例常驻**的示例服务，不是 `systemd timer`：进程内的定时逻辑按 `TIMEZONE=Asia/Shanghai` 与 `DAILY_SEND_TIME=09:00` 运行。仅安装 unit 或执行 `daemon-reload` 不会主动发送；**启动、重启、启用开机自启以及由收件人发送 `/start`/`/next` 均须先得到另行明确授权**。服务联通后会与飞书/文章来源建立连接，生成导读时第三方 API 会收到学习画像、文章正文与问题并可能按量收费；先由项目负责人确认收件人、服务商的数据处理条款、计费方式及首日是否允许发送。本文档未在 Linux 主机、飞书或第三方服务上做真实联调；窗口补发、调用限额和重复发送等行为须以当前代码和授权后的测试为准，不承诺消息必达或计费必然封顶。

## 准备条件与隔离

- 一台有受控出站访问的 Linux 主机，已安装 Python 3.12（含 `venv`）、`rsync` 和 `systemd`，并提供能够查询 `Asia/Shanghai` 的时区数据。服务器可以保持 UTC 系统时钟，但须确保时钟同步；无需公网入站 Webhook。
- **新建独立飞书企业自建应用**，启用机器人、长连接订阅 `im.message.receive_v1` 与 `card.action.trigger`，授权发送消息；不要与旧 bot 共用 App ID/Secret，也不要双进程运行同一个应用。仅配置一个目标用户的 `ALLOWED_FEISHU_OPEN_ID=ou_...`，只在与该用户的私聊中使用。上线前核对应用可见范围、权限与用户同意。
- 选定第三方兼容 API `https://meta-api.vip/v1` 后仍需独立确认可用 `NAN_MODEL`、费率/预算、数据政策；`AI_PROVIDER=api` 为默认。**此前在聊天或其他公开渠道出现过的 API Key 和飞书凭据必须在服务商后台撤销并轮换；不得再使用、复制或保存旧值。** 新签发的密钥只在最终受限的服务器配置中填写，不写在命令历史、仓库、工单或聊天中。
- 以下命令须由有权限的管理员在**目标 Linux 主机**人工执行；`/path/to/ai-learning-bot` 是安全传入主机的项目源码目录，不应包含 `.env`、旧密钥、`data/`、`logs/` 或 Windows 虚拟环境。本文档是操作样例，不授权执行真实联网操作。

## 安装（授权启动前可准备文件）

以 root 管理、服务进程仅以无登录权限的专用账户运行；代码及依赖对服务账户只读，数据库位于单独可写目录。请先审核源文件与依赖，不要把本机 `.env` 或数据库同步到服务器：

```sh
sudo useradd --system --user-group --no-create-home --home-dir /var/lib/ai-learning-bot --shell /usr/sbin/nologin ai-learning-bot
sudo install -d -o root -g ai-learning-bot -m 0750 /opt/ai-learning-bot
sudo install -d -o ai-learning-bot -g ai-learning-bot -m 0700 /var/lib/ai-learning-bot
sudo rsync -r --exclude='.env' --exclude='.venv/' --exclude='data/' --exclude='logs/' \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='.git/' \
  /path/to/ai-learning-bot/ /opt/ai-learning-bot/
sudo python3 -m venv /opt/ai-learning-bot/.venv
sudo /opt/ai-learning-bot/.venv/bin/python -m pip install -r /opt/ai-learning-bot/requirements.txt
sudo chown -R root:ai-learning-bot /opt/ai-learning-bot
sudo chmod -R u=rwX,g=rX,o= /opt/ai-learning-bot
```

如服务账户已存在，请勿重复运行 `useradd`；先核对其 UID、权限及目录归属。绝不将代码或密钥目录设为服务用户可写，以免被错误修改。若 `rsync` 来源包含 `.env.example`，它只能包含占位值。

私有配置使用 systemd 的 `EnvironmentFile`；它**不是** `/opt/ai-learning-bot/.env`。`config.py` 也会尝试读取项目根目录下的 `.env`，因此部署目录必须确保没有该文件，避免来源混淆。将示例复制为仅 root 可修改、服务组可读取的文件，用受控编辑器在本机补齐飞书新凭据、唯一 open_id、服务商确认的模型和新 Key：

```sh
sudo install -d -o root -g ai-learning-bot -m 0750 /etc/ai-learning-bot
sudo install -o root -g ai-learning-bot -m 0640 /opt/ai-learning-bot/.env.example /etc/ai-learning-bot/bot.env
sudoedit /etc/ai-learning-bot/bot.env
sudo install -o root -g root -m 0644 /opt/ai-learning-bot/deploy/ai-learning-bot.service /etc/systemd/system/ai-learning-bot.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/ai-learning-bot.service
sudo stat -c '%U:%G %a %n' /etc/ai-learning-bot /etc/ai-learning-bot/bot.env /var/lib/ai-learning-bot
```

`bot.env` 使用逐行 `KEY=value`；不要把密钥写入 shell 命令、`systemctl set-environment` 或 unit。安装前检查 `bot.env` 模式为 `root:ai-learning-bot 0640`，`/etc/ai-learning-bot` 为 `0750`，数据目录为 `ai-learning-bot:ai-learning-bot 0700`。依赖安装可能联网并应单独审核；以上只是部署命令，不是本次实际执行记录。

关键配置参考项目根目录 `.env.example` / `config.py`：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`ALLOWED_FEISHU_OPEN_ID`、`AI_PROVIDER=api`、`NAN_BASE_URL`、`NAN_API_KEY`、`NAN_MODEL`、`TIMEZONE=Asia/Shanghai`、`DAILY_SEND_TIME=09:00`。unit 固定 `DATA_DIR=/var/lib/ai-learning-bot`，避免写入代码目录；`PROXY_URLS` 默认空，只有确有服务器代理时才填写可达地址。`DAILY_CATCHUP_MINUTES`、`API_DAILY_MAX_CALLS`、`API_MAX_OUTPUT_TOKENS`、`API_TIMEOUT_SECONDS` 可配置，但不能单凭存在这些键就认为补发、限额、输出长度和超时都已生效；上线前检查运行版本和服务商的账单告警/硬额度。`NAN_MODEL` 留空将无法通过 API 模式配置检查；不要猜测模型标识。

## 授权后启用、验证及首日安排

**获得对飞书发送、API 外发与费用的明确批准之前，不运行以下启用/启动命令，也不发送 `/start`、`/next` 测试消息。** 先离线运行单元测试可帮助发现本地回归，但不能证明实际对接或北京时间调度正确：

```sh
# 离线检查；不启动服务、不主动发消息或调用模型。
(cd /opt/ai-learning-bot && .venv/bin/python -m unittest discover -s tests -v)
# 下列操作仅在获批后执行：
sudo systemctl enable --now ai-learning-bot.service
sudo systemctl status ai-learning-bot.service --no-pager
sudo journalctl -u ai-learning-bot.service -n 100 --no-pager
TZ=Asia/Shanghai date
```

经授权后，只由目标 open_id 的用户私聊新应用发送 `/start` 绑定；确认私聊、无群聊扩散和实际收件人正确后，决定是否允许 `/next` 做**可能收费的**主动联调。第一次启动与第一次定时发送之间的关系、错过 09:00 后是否补发、重启后的重复推送处理、`DAILY_CATCHUP_MINUTES` 的实际效果，都需要在当日授权窗口里单独确认。为防首日意外推送，可先保持服务未启动，直到负责人批准上线时刻并明确当日是否应发送；不要把 09:00 承诺成一定到达。服务器时区可用 `TZ=Asia/Shanghai date` 核对。不要同时运行 Windows 计划任务实例或任何使用同一应用的 bot。

检查状态与滚动日志：

```sh
systemctl is-enabled ai-learning-bot.service
systemctl is-active ai-learning-bot.service
sudo systemctl status ai-learning-bot.service --no-pager
sudo journalctl -u ai-learning-bot.service -f
```

`journalctl -f` 持续显示日志，可按 Ctrl-C 退出；只在受控终端查看，日志可能包含异常详情或用户内容，不要转发原文。`systemctl active` 仅代表进程存在，不代表飞书长连接、消息送达、扣费及按时发送均正常；应经授权核对飞书会话、服务商用量并设告警。停止/取消自启：`sudo systemctl disable --now ai-learning-bot.service`（取消自启不会撤销已发消息或服务商费用）。

## 更新、备份与恢复

升级前确认已获允许的停机/再启动与首日重发窗口。先停服务，再备份数据库；**不可在服务运行时直接复制 SQLite 文件**，以免 WAL 或并发写入导致备份不完整。以下示例备份整个数据目录到 root 独占位置；根据保留策略加密、校验、限制访问并定期演练恢复。私有环境文件不随代码覆盖，也不要把真实密钥打包进代码备份。

```sh
sudo systemctl stop ai-learning-bot.service
sudo install -d -o root -g root -m 0700 /var/backups/ai-learning-bot
sudo tar -C /var/lib -czf /var/backups/ai-learning-bot/learning-data-YYYY-MM-DD.tar.gz ai-learning-bot
sudo chmod 0600 /var/backups/ai-learning-bot/learning-data-YYYY-MM-DD.tar.gz
# 审核新版本来源，再按安装段的 rsync 排除规则复制新代码和 requirements.txt；无需覆盖 bot.env、data/。
sudo /opt/ai-learning-bot/.venv/bin/python -m pip install -r /opt/ai-learning-bot/requirements.txt
sudo chown -R root:ai-learning-bot /opt/ai-learning-bot
sudo chmod -R u=rwX,g=rX,o= /opt/ai-learning-bot
sudo install -o root -g root -m 0644 /opt/ai-learning-bot/deploy/ai-learning-bot.service /etc/systemd/system/ai-learning-bot.service
sudo systemctl daemon-reload
# 按已获批准的时间再执行：sudo systemctl start ai-learning-bot.service
```

恢复时先停服务，验证备份来源与时间后从该**可信备份**解包到 `/var/lib`，再检查权限与数据库。恢复旧数据库可能恢复旧绑定、发送/计费计数以及去重状态，造成**漏发、重发或费用预算误判**；恢复后由负责人决定是否跳过当日自动发送，审查数据并重新授权启动。`bot.env` 不应从含已轮换旧 Key 的旧备份恢复；如需恢复配置，请在受控位置重建非密部分并使用当前新密钥。

```sh
sudo systemctl stop ai-learning-bot.service
# 仅从可信备份恢复；先列出压缩包内容并确认只有 ai-learning-bot/ 下的预期条目。
sudo tar -tzf /var/backups/ai-learning-bot/learning-data-YYYY-MM-DD.tar.gz
sudo tar -C /var/lib -xzf /var/backups/ai-learning-bot/learning-data-YYYY-MM-DD.tar.gz --no-same-owner --no-same-permissions
sudo chown -R ai-learning-bot:ai-learning-bot /var/lib/ai-learning-bot
sudo chmod -R go= /var/lib/ai-learning-bot
sudo chmod 0700 /var/lib/ai-learning-bot
# 检查恢复后的文件与权限，核对私聊绑定和当天实际发送状态，再决定是否启动。
```

备份目录只供受控管理员访问；如需异地备份，应先获单独审批并加密，不在本项目中自动上传。滚动更新或单机多实例会争抢飞书长连接和定时任务；始终保证**同一个应用只有一个运行中的实例**。
