# 甲骨文 ARM 部署记录（deploy-oci 分支）

本分支保存 2026-11-07 在 Oracle Cloud Always Free ARM64 实例上跑通的部署适配版本。
基于原版 main（61dac37）增量修改，**原 main 分支未改动**。

## 运行环境

- Oracle Cloud VM.Standard.A1.Flex：1 OCPU / 6 GB，aarch64
- Ubuntu 24.04.5 LTS，Docker 29.x + Compose v2
- 对外仅开放 80（前端）与 22（SSH）；数据库与后端端口仅 127.0.0.1 监听
- 宿主机时区 Asia/Shanghai，2G swap，BBR 已开启

## 本分支修复的三个问题

| 问题 | 原因 | 处理 |
| --- | --- | --- |
| 后端启动即崩溃、登录校验失败 | `passlib 1.7.4` 与新版 `bcrypt 5.x` 不兼容 | `common/pyproject.toml` 锁定 `bcrypt==4.0.1`；`common/utils/security.py` 增加兼容补丁 |
| 前端容器无限重启 | `frontend/nginx.conf` 含 UTF-8 BOM，nginx 报 unknown directive | 移除 BOM |
| MySQL/Redis/后端端口公网裸奔 | 默认 compose 映射到 0.0.0.0，且使用默认弱口令 | `docker-compose.yml` 改为 `127.0.0.1:` 前缀；所有默认口令改为随机强密码 |
| 登录页报「网络异常」，所有接口 404 | 仓库缺少 `frontend/.env.production`，`VITE_API_BASE_URL` 回退成 `/api`，而后端 111 个路由都在 `/api/v1` 下 | 补 `frontend/.env.production` 设 `VITE_API_BASE_URL=/api/v1`；`request.js` 回退值同步改为 `/api/v1`；`nginx.conf` 给 index.html 加 no-cache |

## 日常运维

```
cd ~/pdd-auto-reply
sudo docker compose --env-file .env up -d          # 启动
sudo docker compose --env-file .env ps             # 状态
sudo docker compose --env-file .env logs -f        # 日志
sudo docker compose --env-file .env down           # 停止
```

**不要运行 `bash deploy.sh -y`**：该脚本会 `down --rmi local` 删除镜像后全量重建，
在 1 核机器上需 8 分钟以上，且会丢掉构建缓存。

## 备份与恢复

- 数据库每天 03:00 自动备份到 `~/pdd-backups/db-*.sql.gz`，保留 7 天；脚本副本见 `scripts/backup-db.sh`
- 完整 git 历史离线包：`~/pdd-backups/pdd-auto-reply-*.bundle`，恢复方式 `git clone <bundle文件> pdd-auto-reply`
- `.env` 含数据库口令，已被 `.gitignore` 忽略，**切勿提交**；迁移机器时从备份包单独取回
- 管理员与数据库密码存于服务器 `~/pdd_admin_credentials.txt`（权限 600），不入仓库

## 迁移到新机器的步骤

1. `git clone -b deploy-oci git@github.com:crezzi-boop/pdd-auto-reply.git`
2. 从离线备份包取回 `.env`（或按 `.env.example` 重新生成并修改）
3. `sudo docker compose --env-file .env up -d --build`
4. 如需恢复数据：`gunzip -c db-*.sql.gz | sudo docker exec -i -e MYSQL_PWD=... pdd_mysql mysql pdd_auto_reply`

## 业务接入提醒

- 店铺请用后台「扫码登录（推荐）」或「Cookie 导入」，服务器无图形界面，不要用账号密码登录
- AI 模型请在后台「获取模型」选择该密钥实际可用的模型，旧模型名可能返回 404
- API 密钥不限制格式（前缀 / 长度均可不同），直接完整粘贴，用「测试连接」验证是否可用
- 店铺 Cookie 会过期，可开启「系统设置 → 登录态自动维护」每日自动保活

## AI 密钥格式兼容（2026-11-07 修正）

早期版本对 Gemini 密钥做了「必须 AIza 开头、约 39 位」的本地白名单校验，会把
其它渠道生成的合法密钥误报为「API 密钥格式不对」。现已移除全部格式判断：

- 密钥只做无损清洗：自动去掉空格 / 换行 / 全角空格 / 零宽字符 / BOM / 成对引号 /
  `GEMINI_API_KEY=xxx` 之类误粘贴前缀，其余字符原样保留
- 密钥是否有效一律由服务商接口判定（测试连接 / 获取模型 / 实际回复），失败时给中文原因
- 保存、测试、获取模型、运行时回复使用同一套清洗逻辑，避免历史脏数据导致调用失败
- 若使用第三方中转网关，请把接口类型选为「OpenAI 兼容」并填对方提供的地址
- 模型可用性同样以服务商 `models.list` 的真实返回为准：不再按本地写死的「已停用名单」
  过滤或改写模型名（只自动升级 1.5 / 2.0 这类已不再返回的老系列）
- HTTP 503（模型繁忙 / 过载）会给出中文提示，属临时状态，可稍后重试或换模型
- 2026-11-07 实测（当前密钥）：`gemini-flash-lite-latest` 可用并推荐；`gemini-3.8-flash` 常返回
  503 繁忙；`gemini-2.5-flash` 虽出现在 models.list 里，实际调用返回 404「对新增用户不再开放」

涉及文件：`common/services/ai_provider_service.py`、`backend/app/services/ai_config_service.py`、
`websocket/agent/llm_client.py`、`frontend/src/components/shop_settings/AiSettingsPanel.vue`。

## 默认回复多条轮换（2026-11-07 新增）

拼多多会拦截短时间内的重复回复，原系统每店铺只能填一条默认回复。现改为：

- 后台「店铺管理 → 设置 → 默认与商品回复」为**一个输入框**：多条话术直接粘贴，一条一行
  （最多 **100 条**、单条 ≤1000 字）；前后端都会按行拆分，空行与重复行自动忽略，页面实时显示条数
- 可设置「轮换时间」（1-120 分钟，默认 10）：同一店铺在该窗口内每次默认回复换用一条没发过的语句
- 窗口内语句全部用过后，自动选最久未用的一条并追加不可见的零宽字符变化，保证文本不重复
- AI 回复失败回退默认回复时同样参与轮换
- 发送历史落在 `pdd_default_reply_rotation_record` 表，容器重启不丢；过期记录自动清理
- 新增列 `pdd_default_reply.contents` / `rotate_minutes` 与轮换记录表由 backend 启动自检迁移器自动补齐，历史单条配置无需改动
- 100 条 JSON 可能超过 MySQL `TEXT` 的 64KB：`contents` 在 MySQL 下声明为 `LONGTEXT`，
  启动自检迁移器也会自动把历史的 `TEXT` 列幂等扩容为 `LONGTEXT`（不改数据，已是 LONGTEXT 则跳过）

涉及文件：`common/services/default_reply_variants.py`（公共纯逻辑）、
`websocket/engine/default_reply_rotation.py`（轮换器）、`common/models/reply_models.py`、
`backend/app/services/reply_service.py`、`backend/app/api/routes/replies.py`、
`websocket/engine/reply_engine.py`、`websocket/engine/message_consumer.py`、
`frontend/src/components/shop_settings/DefaultReplyPanel.vue`。

## 自动回复整体失效的修复（2026-11-07）

现象：消息进来但不自动回复；关闭 AI 后仍不回复。两个独立原因叠加：

1. **AI 调用必失败**：`websocket/agent/llm_client.py` 的 `_chat_gemini` 调用了
   `normalize_ai_model`，但函数内只导入了 `AI_PROVIDER_DEFAULT_BASE_URLS`，触发
   `NameError`，每次 AI 回复都回退默认回复。已补齐导入，真实密钥调用验证通过。
2. **服务卡死（关 AI 也没回复的真正原因）**：店铺「停用 → 启用」会关闭消息队列，
   但 `_consume_loop` 在队列「已关闭且为空」时被 `get()` 立即返回 `None` 反复空转，
   没有任何 `await` 让出事件循环，CPU 100%、`/health` 超时、心跳停止、消息无法接收。
   修复：`FifoMessageQueue.get()` 该分支 `await asyncio.sleep(0.05)`；`_consume_loop`
   检测到队列已关闭立即退出；`MessageQueueManager.get_or_create()` 不复用已关闭队列。
   回归验证：关闭队列的消费循环 0.05s 内退出，事件循环保持可响应。

排查命令（服务器）：
`docker compose logs websocket | grep -iE "error|traceback|ai_reply_failed"`、
`curl -m 5 http://127.0.0.1:8090/health`、`docker stats --no-stream pdd_websocket`。
若 health 超时且 CPU 接近 100%，优先检查消费循环是否有无 `await` 的分支。

涉及文件：`websocket/agent/llm_client.py`、`websocket/channel_pdd/message_queue.py`、
`websocket/channel_pdd/pdd_channel.py`（只重建 websocket 镜像即可）。

## 飞书机器人通知修复（2026-11-07）

现象：通知渠道填飞书 Webhook，点「测试发送」提示成功，群里却收不到。三个原因叠加：

1. **报文格式不对**：旧代码对 webhook/wecom 一律 POST `{"content":"文本"}`；飞书官方要求
   `{"msg_type":"text","content":{"text":"文本"}}`，企业微信要求
   `{"msgtype":"text","text":{"content":"文本"}}`。格式错误时飞书按「无关键词」拒绝。
2. **自定义关键词**：飞书后台安全设置要求消息文本命中关键词（本例「提醒」），默认测试
   文案「这是一条测试通知」不含关键词，飞书返回 `code=19024 Key Words Not Found`。
3. **只看 HTTP 状态码**：飞书业务失败也返回 HTTP 200（仅响应体 code≠0），旧代码记为
   success，出现「页面成功、群里没消息」的假成功（历史记录 id=4/5/6 即此类）。

修复内容：

- 新增渠道类型 `feishu`（中文「飞书机器人」）；按渠道类型或目标域名自动识别协议，
  历史以 webhook 类型填飞书地址的记录无需改动即按飞书格式投递
- 渠道表新增 `keyword`（自定义关键词）、`secret`（签名密钥）两列，启动自检迁移器自动补列；
  关键词为空时兜底「提醒」，发送前自动加 `【关键词】` 前缀（幂等，已含则不加）
- 可选签名校验：`HMAC-SHA256(timestamp+"\n"+secret)` 的 base64，随报文带 timestamp/sign
- 解析响应体 `code` / `StatusCode` / `errcode`，非 0 判失败并给中文排查提示
  （19024 关键词、19021 签名、19001 参数、9499 格式）
- 前端新增「飞书关键词」「飞书签名密钥」输入框（仅飞书渠道显示），通知记录保存实际发出的文本
- 现有渠道 id=1 已改为 `channel_type=feishu`、`keyword=提醒`

验证：`/api/v1/notify/channels/1/test` 返回 success、飞书 `code=0`；系统事件推送
success=1 / failed=0；`backend/tests/test_notify_api.py` 13 项回归全部通过；6 容器健康。

涉及文件：`backend/app/services/notify_service.py`、`backend/app/api/routes/notify.py`、
`backend/tests/test_notify_api.py`、`common/models/setting_models.py`、
`common/services/dict_seed_data.py`、
`frontend/src/components/shop_settings/NotifyChannelsPanel.vue`（需重建 backend + frontend 镜像）。

注意：服务器推送 GitHub 备份必须显式指定 deploy key（文件名非 ssh 默认名，
不指定会报 `Permission denied (publickey)`）：

```bash
GIT_SSH_COMMAND="ssh -i $HOME/.ssh/id_ed25519_github_backup -o IdentitiesOnly=yes" \
  git push backup HEAD:refs/heads/deploy-oci
```

## 回复保底与人工介入（2026-11-07）

背景：拼多多 8:00-23:00 考核「3 分钟人工回复率」，同一消费者当日任意一条消息
未在 3 分钟内被回复即计入「未回复」。人工接入后若机器人完全静默，客户最后
一条消息会直接拉低回复率并可能触发罚款。用户确认：本系统以商家客服账号发出的
机器回复也算人工回复，因此兜底用默认回复即可，无需真人补刀。

硬约束（**不可配置、不可关闭**）：

- 客户消息发出后 **90 秒**仍无任何商家消息 → 强制发一条轮换过的默认回复。
  90 秒触发 + 10 秒扫描周期 + 发送重试余量，端到端约 90~110 秒，短于用户
  要求的 2 分钟，也稳在平台 3 分钟红线内。
- scheduler 服务每 10 秒触发一次（启动时内置注册 `reply_guard` job，
  **不写 `scheduled_task` 表、不开放启停**），实际发送经 websocket 服务
  `POST /api/v1/guard/scan` 完成。scheduler 镜像只含 common + scheduler，
  拼多多发送能力仅在 websocket 镜像内，故采用「独立进程计时 + HTTP 触发发送」。
- 判重不建表：复用 `pdd_chat_message` 的 in/out 序列。每条 in 消息落库、每次
  自动回复 / 手动发送 / 保底发送成功后都落库一条 out；会话最新一条是 in
  即等价于「这条客户消息还没有回复」，保底发出后最新变 out 自然销账。
- 保底完全绕开 `default_reply_once`、风控限频、非营业时间、过滤 / 黑名单、
  AI 异常、转人工关键词直接 return 等所有会「不回复」的分支。
- 安全边界：只回补最近 10 分钟消息（防重启后群发历史会话）、单轮最多 50 个
  会话、停用店铺不发送、发送前二次确认最新消息未变（防与自动回复重复）、
  发送失败释放轮换名额并进入 60 秒冷却。

可配项（转人工设置面板顶部，按店铺存于 `pdd_sys_setting`，**秒为单位**）：

- `manual_delay_seconds`：人工介入后机器延迟回复，默认 30 秒，0~60。
- `manual_recover_seconds`：人工介入自动恢复时长，默认 120 秒，30~3600，
  0 = 不自动恢复。按「店铺 + 客户」独立计时，从最后一次人工消息起算，客服
  每回一句刷新计时；两个秒值前后端均做范围校验。
- 接口：`GET/PUT /api/v1/shops/{shop_pk}/reply-guard`。
- 人工介入登记：转人工关键词命中且转移成功、在线聊天页手动发送成功。状态存
  websocket 进程内存（`ManualSessionState`），容器重启丢失最多让相关客户提前
  回到立即回复，不影响保底。

涉及文件：`common/services/reply_guard_settings.py`（新）、
`websocket/engine/manual_session_state.py`（新）、`websocket/routes/guard.py`（新）、
`websocket/engine/message_consumer.py`、`websocket/routes/messages.py`、
`websocket/routes/__init__.py`、`scheduler/tasks/{constants,task_runners,service_client,scheduler_service}.py`、
`backend/app/api/routes/transfer.py`、
`frontend/src/api/auto_reply_api.js`、
`frontend/src/components/shop_settings/TransferPanel.vue`、
`websocket/tests/test_reply_guard.py`（新）、
`websocket/tests/test_message_consumer.py`（补测试隔离桩）。
需重建 **backend + websocket + scheduler + frontend** 四个镜像。

### 上线验证（2026-11-07 17:10）

- 四容器 healthy；scheduler 内置 `reply_guard` job 每 10 秒调用
  `POST /api/v1/guard/scan`（5 分钟内 30 次，全部 200），接口返回
  `deadline_seconds=90`。
- 真实链路端到端跑通：日志出现两次「保底回复已发送」并落库一条
  `direction=out`（触发源为测试假会话，详见下方「测试隔离」），证明
  扫描 → 轮换选句 → 拼多多发送 → 落库销账整条链路可用。
- `GET/PUT /api/v1/shops/{shop_pk}/reply-guard` 默认值 30/120 秒正确；
  延迟 61 秒、恢复 29 秒均返回中文校验错误；0/0 合法。
- 前端产物已包含「人工介入自动恢复时长」等新文案。
- 单测：`websocket/tests` 全量 **221 passed**（新增 39 项 +
  回归 182 项），`py_compile` 全部通过。

### 测试隔离（重要，勿回退）

`websocket/tests/test_message_consumer.py` 原先未注入轮换器 / 聊天落库桩，
跑测试会直接读写**生产库**：一是轮换记录互相污染导致用例偶发失败，二是
写入 `cust_1` / `cust_A` / `cust_B` 假会话，被 10 秒一次的保底扫描当成
「超时未回复」而真的向拼多多发起发送。已补齐 `StubRotator`、
`chat_message_writer`、`chat_history_reader` 三个桩；修复后重跑全量测试，
`pdd_chat_message` 行数不变（60 条）、假客户行数为 0。

清理记录：上线验证期间产生的 49 条假聊天记录、3 条假会话、4 条假轮换
记录已删除，清理前整表快照存于
`~/pdd-backups/guard_test_cleanup_20261107.sql`（**含业务数据，切勿提交 git**）。
剩余客户 UID 均为真实数字 UID，未误发真实客户。

### 运行依赖

websocket 容器默认未装 `hypothesis` / `pytest`，跑全量测试前需在容器内执行
`pip install -q pytest hypothesis`（仅测试需要，不写入镜像依赖）。

## 线上修复（2026-11-07 18:10）：人工介入后机器仍秒回客户

- 现象：人工在拼多多后台手发消息后，客户再发消息机器仍秒回，未按设置延迟。
- 根因：consume_raw 的 mall_cs 分支只转发展示、未登记人工介入；pdd_message 未提取 from.cs_uid，无法区分真人手发与系统回推。
- 修复：pdd_message.py 透传 from_cs_uid；message_consumer.py 在客服消息含 cs_uid 时 mark 人工介入；process() 第 501 行 pending_delay 据此延迟自动回复。系统自动回推（mall_cs 无 cs_uid）不 mark，避免自刷新。
- 验证：容器内 test_human_intervention_mark.py 2 passed；websocket 容器 17:35 重建后 Up healthy、代码已编译进镜像；提交 aaee8fd → backup/deploy-oci。
