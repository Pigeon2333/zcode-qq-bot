# ZCode QQ机器人 · 插件开发与上架指南

任何人都可以给「ZCode QQ机器人」写插件并发布到插件市场。本文是完整的开发 → 本地测试 → 上架流程。

## 一、插件长什么样

一个插件就是一个文件夹，里面两个必需文件：

```
my_plugin/
├── manifest.json    # 插件声明（名字/描述/设置项/分类/管理页）
└── plugin.py        # 逻辑：register(api) 里挂钩子
```

### manifest.json

```json
{
  "name": "my_plugin",
  "title": "我的插件",
  "description": "一句话描述（市场卡片上显示的）",
  "default_enabled": false,
  "category": "其他",
  "settings": [
    {"key": "city", "type": "text", "label": "城市", "default": ""},
    {"key": "enabled_x", "type": "toggle", "label": "某功能开关", "default": false},
    {"key": "interval", "type": "number", "label": "周期（分钟）", "default": 60, "min": 10, "max": 1440}
  ],
  "page": {"js": "myPageHtml", "data": "/api/my/status"}
}
```

- `name`：仅小写字母/数字/下划线，2-32 位（也是安装目录名）
- `default_enabled`：建议 `false`（涉及隐私/打扰的功能必须默认关）
- `settings`：声明式设置项，自动出现在插件的设置页里，可省略
- `page`：进阶项，仅随宿主内置的插件可用（管理页渲染函数与数据端点都住在宿主 UI 里，第三方插件带不进来）。第三方插件用声明式 settings 就够

### plugin.py

```python
# -*- coding: utf-8 -*-

def register(api):
    api.every(30, my_round)        # 周期任务：每 30 分钟至多跑一次（首拍立即跑）
    api.inject_context(context)    # 给每轮对话注入一行上下文（返回 str 或 ""）

def my_round(daemon):
    ...                            # 周期逻辑（daemon 可查历史/发消息）

def context(chat_type, chat_id):
    return "现在外面在下雪"   # 返回空串则不注入
```

PluginAPI 提供：

| 能力 | 写法 | 说明 |
| --- | --- | --- |
| 节流周期任务 | `api.every(30, fn)` | 每 30 分钟调一次 fn（首拍立即跑），注册时声明 |
| 读自己的设置 | `api.setting("city", "")` | 用户在插件设置页改的值 |
| 日志 | `api.log("...")` | 进运行日志页（AGENT_PLUGIN 前缀） |
| 消息钩子 | `api.on_tick(fn)` | fn(daemon)，daemon 可发消息/查历史 |
| 上下文注入 | `api.inject_context(fn)` | fn(chat_type, chat_id) → str |

周期任务**必须**用 `api.every(minutes, fn)` 而不是在 tick 里自己算时间——它自带窗口节流与首拍立即执行。

## 二、环境与安全红线

- 运行环境是 Python 3.10+（发布版为冻结解释器），只用标准库 + 已随附的第三方库
- 网络请求只用 `http.client`/`urllib` 且仅 https 公网；拒绝 localhost/内网/环回地址
- 不写 `~/.zcode-qq-bot/` 之外的任何路径；不读其他插件的数据文件
- API Key 一律走宿主的 Key 体系，插件代码与设置里**不得**出现密钥字面量
- 涉及设备/隐私信息（读窗口、定位等）的功能：独立开关且默认关闭
- 安装器会拒绝路径穿越（`../`）、绝对盘符写入口的 zip

## 三、本地开发与测试

1. Fork 并克隆本仓库；插件放 `plugins/qq-bot/scripts/plugins/<name>/`（内置目录）或 `~/.zcode-qq-bot/plugins/<name>/`（用户目录，同名覆盖内置的启停）
2. 启动机器人 → 插件页能看到你的插件；改动 `plugin.py` 需重启大脑组件（启停与设置是热加载，代码不是）
3. 用「插件页 → ⚙ 设置」验证 settings 读写；运行日志页看 `api.log` 输出
4. 参考 `plugins/qq-bot/scripts/plugins/` 下的内置插件（weather 是最简范例，bili_scroll 展示了管理页/分享/LLM 调用的进阶用法）

## 四、打包

zip 结构必须是一层插件目录在根：

```
my_plugin.zip
└── my_plugin/
    ├── manifest.json
    └── plugin.py
```

仓库内的**纯市场插件**源码放 `market/<name>/`（不进应用内置目录；例：bili_scroll）。内置插件源码在 `plugins/qq-bot/scripts/plugins/<name>/`（随应用打包，仅保留基础设施类）。

打包示例（在插件上级目录）：

```
python -m zipfile -c my_plugin.zip my_plugin
```

同名安装=更新覆盖（用户装过的设置保留）。

## 五、上架到插件市场

市场清单与 zip 托管在本仓库 GitHub Release（tag：`plugins`）。**清单文件名带版本**（market-v2.json、market-v3.json…）：release 资产的 CDN 缓存近乎永久，clobber 同名清单更新后用户拿到的仍是旧字节——**清单内容有变必须换新文件名**（zip 同理，已在条目规则里要求）。流程：

1. **提 PR**：把你的插件目录加到 `plugins/qq-bot/scripts/plugins/` 下（作为内置插件候选），或在 PR 里附上 zip 与说明；建议在 PR 描述里带上期望的市场条目（维护者会写进市场清单）：

   ```json
   {"name": "my_plugin", "title": "我的插件", "version": "1.0.0",
    "zip": "https://github.com/Pigeon2333/zcode-qq-bot/releases/download/plugins/my_plugin-1.0.0.zip",
    "category": "其他", "description": "一句话描述"}
   ```

   `zip` 是完整下载地址（发版后指向 `plugins` tag 的 release 资产）。文件名必须带版本号——市场下载走 CDN，同名文件更新后会被缓存，**更新时必须换文件名**
2. 仓库维护者审核（安全红线 + 代码可读性），通过后把 zip 与更新后的 `plugins.json` 传到 `plugins` release，全网用户即可在插件市场看到并一键安装
3. 发新版本：改代码 → bump `version` 与 `file` 里的版本号 → 再提 PR

### 审核要点（提前自查，过审更快）

- [ ] manifest 的 name/title/description 完整，default_enabled 合理（隐私类必须 false）
- [ ] 周期任务用 api.every；tick 里不做重活
- [ ] 无密钥、无内网地址、无数据目录之外的写入
- [ ] 声明式 settings 都能即时生效（热加载）
- [ ] 在干净数据目录（新装机器人）上验证过安装→启用→设置→卸载全流程

## 六、许可

本仓库采用 PolyForm Noncommercial 1.0.0（可修改、可分发修改版、禁止商用）。提交 PR 即表示你同意以同一许可发布你的插件代码。
