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

- 店铺请用后台「Cookie 导入」，服务器无图形界面，不要用账号密码登录
- AI 模型请在后台「拉取模型列表」选择当前可用模型，`gemini-1.5-*` 等旧模型名已下线
- 店铺 Cookie 会过期，失效后重新导入一次即可
