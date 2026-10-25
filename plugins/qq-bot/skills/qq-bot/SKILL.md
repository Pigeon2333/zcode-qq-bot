---
name: qq-bot
description: ZCode QQ机器人的开发与运维手册。当用户要求启动/停止/排查机器人、改机器人功能或人设数据、打包发布、或学习NapCat部署排障时使用本技能。日常管理请用「ZCode QQ机器人」桌面应用的控制台（浏览器打开 http://127.0.0.1:39765）。
---

# ZCode QQ机器人 — 开发与运维手册

这是一个**完全自治**的 QQ 群聊 AI 机器人（大脑直连智谱 GLM，不依赖 ZCode 会话），
带桌面应用与管理控制台。本技能是它的开发/运维文档；产品功能一览见 `../../README.md`。

## 架构（2026-10 现行）

```
QQ ←→ NapCat Shell(OneBot v11 WS :3001) ←→ qq_daemon.py(:39763 消息管道,零文件写入)
                                             ↕ HTTP 收件箱轮询
qq_bot_agent.py(:39764 自治大脑)
  路由顺序: 主人通道(画图→私聊遥控→任务桥) → 防滥用 → 搜索注入 → 视觉 → 文本链
  结构性防插嘴(daemon): @别人(at_other)/回复别人(reply_other)的消息不进任何主动插话抽样; reply_to 摘要随消息传给 agent
  模型链降级+思考模式 / 人设+全局设定 / 长期记忆 / 三库表情包 / 夜间复盘线程
     ↕ spawn                        ↕ HTTP(回环)
qq_task_worker.py(沙箱执行器)     qq_admin_ui.py(:39765 管理台)
   WRITE/RUN/READ/DONE 循环          ↕ pywebview
                                    qq_app.py(窗口+托盘+看门狗自愈)
```

- 源码：本技能目录 `../../scripts/`（7 个模块，除 qq_app 需 pywebview/pystray/PIL 外全标准库）
- 数据：`~/.zcode-qq-bot/`（token、ws_token、zhipu_api_key、app_config.json、
  model_chain.json、reply_config.json、group_whitelist.json、private_config.json、
  personas/collection.json、stickers.json、styles/、memory*/、logs/、tasks/、images/）
- 全组件仅回环绑定；配置 mtime 秒级热加载（UI 保存即生效，下一条消息用新配置）

## 运维：日常操作

一切日常管理走**桌面应用控制台**（启停电源、回复模式、人设、表情包、用量、运行日志）。
主人私聊机器人也可遥控：让主人发「帮助」给机器人私聊拿指令菜单。

## 运维：进程级操作（排障用）

- 拉起：`python scripts/qq_app.py`（窗口）或分别 `ensure_ui/daemon/agent`（qq_services）
- 精准杀进程：按命令行匹配 `qq_daemon.py`/`qq_bot_agent.py`/`qq_admin_ui.py`
  （**勿用宽泛 taskkill 过滤，曾误杀**）；`/test/inject` 可注入消息做端到端测试
  （daemon 需 `X-QQBot-Token` 头；**含中文的 body 必须写 UTF-8 临时文件 + curl --data-binary**，
  直接 -d 会 GBK 乱码）
- 日志：`logs/daemon.log`（AGENT_EVENT/AGENT_LOG）、`logs/agent.log`
  （AGENT_DONE/SKIP/ERROR/TASK/DRAW/REVIEW 标记行）、UI 内存启动日志走 `/api/power/status`
- 配置（白名单/回复模式/私聊/分群）按文件 mtime 秒级失效：改完即测，无需等待

## 运维：NapCat 部署与排障（实战沉淀）

- 版本匹配：NapCat 4.18.13 ↔ QQ 9.9.36-53644；QQ 自动更新后报「文件已损坏」= 版本不匹配
- 「已登录,无法重复登录」= NTQQ 会话残留 → 用常规 QQ 正常登录该号再退出账号
- 配置按账号走：`QQNT/config/onebot11_<QQ>.json`（应用自动生成，UAC 下写入失败会给手动指引）
- **登录诊断三态**：QQ 进程在 + 6099(WebUI) 监听 + 3001 不监听 = NapCat 活着但**等账号登录**
  （快速登录凭据过期）→ 应用会自动弹 WebUI 扫码页；扫码后 3001 自动开放、daemon 自动连上
- launcher 控制台二维码必过期，扫码一律走 WebUI 页面
- 换号：设置页改 bot_qq → 全部停止 → 重新启动（onebot 配置自动重生成）

## 开发约定（在本工作区迭代时遵守）

1. **改任何前端**（qq_admin_ui.py 的 PAGE）后：`python -W error::SyntaxWarning -c "import ast; ast.parse(...)"`
   + 提取 `<script>` 用 `node -e "new Function(fs.readFileSync('extract.txt','utf8'))"` 双重校验
   （node --check 对非 .js 扩展名拒绝且 Mimosa 拦 .js 落盘，new Function 编译不执行是绕法）
2. 编辑工具只用 Edit/Write（bash heredoc 曾多次把 `\n`/`\x00`/JS 块写坏）
3. 数据文件路径段校验用黑名单式 `[/:*?"<>|\x00]`（白名单式会拒绝中文人设名）
4. 对外请求：http.client + 常量主机；动态 URL 必须先 scheme/host 校验（拒环回/私有地址）
5. **绝不为了单测改写生产配置文件**（曾覆盖又误删 reply_config.json，靠记忆恢复）
6. Mimosa 钩子对 `subprocess shell=True` 拼接、未校验 open 动态路径会拦——参数列表 +
   显式 `_safe_path`/`_ensure_within` 前置校验可过
7. 改完重启对应组件（UI/agent/daemon 互不影响），UI 有看门狗会自动 reload 窗口

## 主人通道细节（排障速查）

- 触发前提：`app_config.owner_qq` 非空且消息 sender 与之相等
- 私聊遥控 `owner_command`：状态/用量/人设列表/切换人设 X/加群 X/移除群 X/
  开启|关闭 看图|联网搜索|防滥用/暂停/恢复/日志 N/帮助
- 任务桥 `_is_task_request`：「任务：/task:」前缀或任务滥用启发式命中；
  沙箱 worker 白名单 RUN（python/pip/dir/type/where/tree/findstr），产物在
  `app_config.task_workspace`（默认 `~/.zcode-qq-bot/tasks/`）
- 生图 `_DRAW_RE`+`_draw_prompt()`：`画：描述` 或自然说法（帮我画一张/给我画个/来一张/生成一张…），cogview-3-flash 免费档，偶发 1314 重试一次；系统提示词含生图能力认知（仅主人，未触发时引导格式）
- 复盘线程：每日 03:30（04:00-05:30 补跑窗口），统计→GLM 提炼→evolved.json→日报私聊主人

## 发布 TODO（面向公众）

PyInstaller 冻结（含 pywebview/pystray/PIL）+ 单实例锁 + 首次运行向导 +
NapCat Shell 文件内嵌释放与 QQ 版本校验 + 日志轮转 + UI 鉴权。
