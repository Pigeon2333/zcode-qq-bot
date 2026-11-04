# ZCode QQ Bot · Linux 部署

服务器 7×24 运行机器人（无 GUI）。架构与 Windows 版完全相同——同一套控制台/插件/数据，
只是跑法不同：**三个后台进程 + 浏览器控制台**，类似 MCSM 面板的使用方式。

## 组成

| 组件 | 端口 | 说明 |
|---|---|---|
| 控制台 UI | 39765 | 浏览器访问（首次用 `?tk=` 令牌解锁） |
| 消息管道 daemon | 39763 | OneBot WS 客户端，连 NapCat |
| AI 大脑 agent | 39764 | 模型链/记忆/安全，仅本机 |
| NapCat | 3001 (WS) / 6099 (WebUI) | 建议跑 Docker（官方镜像） |

## 快速开始（Docker 方式，推荐）

### 1. 准备

```bash
git clone https://github.com/Pigeon2333/zcode-qq-bot.git
cd zcode-qq-bot
pip3 install -r plugins/qq-bot/scripts/requirements.txt 2>/dev/null || pip3 install pywebview pystray pillow edge-tts
```

> headless 模式其实只需要 `edge-tts`（语音插件）和标准库；pywebview/pystray 是桌面外壳
> 依赖，Linux 服务器不需要。手工最小安装：
> `pip3 install edge-tts pycryptodome`

### 2. NapCat（Docker）

```bash
docker run -d --name napcat \
  -e NAPCAT_GID=$(id -u) -e NAPCAT_UID=$(id -g) \
  -e ACCOUNT=3126940548 \
  -p 3001:3001 -p 6099:6099 \
  -v $HOME/.zcode-qq-bot/QQNT:/app/qq/config \
  mlikiowa/napcat-docker:latest
```

首次启动打开 `http://服务器IP:6099` 扫码登录机器人 QQ。

### 3. 机器人核心

```bash
python3 linux_headless.py --check   # 环境自检
python3 linux_headless.py           # 前台跑（调试）
```

看到 `[boot] 控制台: http://0.0.0.0:39765/?tk=xxx` 后，把整个 URL 复制到浏览器打开——
即 Windows 版完全相同的控制台（首次用 `?tk=` 解锁，之后浏览器记住）。

### 4. 常驻（systemd）

```bash
sudo tee /etc/systemd/system/zcode-qq-bot.service <<'EOF'
[Unit]
Description=ZCode QQ Bot (headless)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=%i
WorkingDirectory=/opt/zcode-qq-bot
ExecStart=/usr/bin/python3 /opt/zcode-qq-bot/linux_headless.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now zcode-qq-bot
```

## 与 Windows 版的差异

| 功能 | Windows | Linux headless |
|---|---|---|
| 控制台 | 桌面窗口（pywebview） | 浏览器访问 39765 |
| 设备操控 | ✅（Windows API） | ❌ 隐藏/不适用 |
| 任务沙箱 | ✅ | ✅（Linux 白名单） |
| 开机自启 | 注册表 | systemd |
| 托盘 | pystray | 无（后台进程） |
| 聊天记录文件导入的"浏览"按钮 | tkinter 对话框 | 手动输入路径 |
| NapCat | 注入本机 QQNT | Docker 容器（隔离、自动重启） |

插件系统、控制台功能、数据目录结构完全一致——插件无需改动即可在两个平台运行。

## 安全提示

- 服务器部署时建议把 39765 端口**只监听内网/本机**，外网访问用 nginx 反代 + HTTPS +
  Basic Auth，或 SSH 隧道：`ssh -L 39765:127.0.0.1:39765 user@server`
- 令牌（`ui_token`）与 Windows 版同级别对待：不要发任何人
- Docker NapCat 的 6099 WebUI 用完扫码后建议 `docker stop`，登录后不需要常开
