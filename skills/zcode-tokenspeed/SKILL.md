---
name: zcode-tokenspeed
description: "[仅手动调用，禁止自动触发] ZCode 客户端本地补丁注入工具：①思考档位配置（3.14+ 原生 optionSpecs，无需内核补丁）②思考档位透传（≤3.11 内核补丁）③用量页去截断（趋势图/饼图全量）④模型弹窗加宽 ⑤TPS 状态栏（输入框统计条：本轮指标+会话累计）⑥思考强度吸附滑条 ⑦增强提示词按钮（一键润色输入框草稿，右键可选润色所用模型）⑧设置页一键模型拉取按钮。全部幂等、可 --check、可 --revert 精确还原。只有当用户明确要求执行本 skill、或明确点名「zcode-tokenspeed」时才加载；用户只是泛泛提到思考等级、用量图、状态栏、补丁等话题时，一律不要自动触发本 skill。"
---

# ZCode 客户端补丁工具

> 本 skill 由 **zcode-tokenspeed 插件**提供，脚本就在本 skill 目录下的 `scripts/`（下文所有 `<skill目录>` 均指本目录）。

**先跑这一条**（只读，不改任何文件，一条命令看全部 8 项功能的状态）：

```bash
python "<skill目录>/scripts/zcode_patcher.py" --all --check
```

`--all` = 对所有功能生效，等价于把所有补丁参数都写一遍；配合 `--check` 是体检、不带参数是全装、
配合 `--revert` 是全还原。要单项操作时仍按后文的单功能命令来（`--all` 会覆盖单个参数）。

**用户说「插件装了但没生效」时，先跑安装自检**（同样只读；它会检查插件是否启用、
配置是否保存过、钩子是否跑过，并直接给出卡点结论）：

```bash
python "<skill目录>/scripts/doctor.py"          # 可读报告
python "<skill目录>/scripts/doctor.py" --json   # 结构化输出，便于贴给别人
python "<skill目录>/scripts/doctor.py" --where  # 只查「插件装在哪」+ 可直接复制的绝对路径
```

> **别站在插件的缓存目录里敲相对路径**——用户最常踩的坑，报错长这样：
> `can't open file '...\.zcode\cli\plugins\cache\zcode-toolkit\skills\...\doctor.py'`。
> `cache\<市场名>\` 是**市场目录**，插件根在更深一层：GitHub 来源的市场缓存成
> **`cache\<市场名>\<插件名>\<版本>\`**，`skills\` 在**版本目录里面**。
> 找不到时：`--where` 列命中路径；或用系统命令搜
> （PowerShell `Get-ChildItem "$env:USERPROFILE\.zcode\cli\plugins" -Recurse -Filter doctor.py`；
> macOS/Linux `find ~/.zcode/cli/plugins -name doctor.py`）。

插件这条路要过「安装 → 启用 → 钩子触发 → 自动注入 → 打补丁」五关，任何一关没走通，
界面上的表现都一样是「什么也没发生」。**最常见的卡点是「插件没启用」**。
钩子是否真的执行过，看 `scripts/_sync.last` 心跳文件；不存在 = 钩子从未被调用。

> **装完即用，不需要打开配置页。** 插件清单里 8 个开关的 `default` 全是 `true`，没保存过配置时> `sync.py` 就按这份默认值注入。ZCode 的插件清单**没有「安装时钩子」**——官方规范
> `plugin-json-spec.md` 原文只允许声明 `skills` / `commands` / `hooks` / `mcpServers`
> 四类组件（「Optional components: `skills` and `commands` point to directories; `hooks` points to
> `hooks/hooks.json`; `mcpServers` can point to `.mcp.json` or contain the server configuration」），
> 没有任何「点下安装按钮时执行」的入口。所以最早能自动触发的时机是
> **下一次会话启动的 `SessionStart`** —— 这是规范决定的，不是实现取巧。

## 版本现状（动手前先看）

补丁会随 ZCode 升级失效或过时，先确认目标版本，再决定打哪个：

| 补丁 | 3.11.2 及更早 | 3.14.x（实测 3.14.1 / 3.14.3） |
|---|---|---|
| ① 思考档位透传（内核补丁） | 需要，走 `providerOptionsByLevel` 兜底 | **已过时**：内核机制整体重写（`providerOptionsByLevel` 只剩 schema 定义，`variants/defaultVariant` 已从内核消失），锚点与结构提取都失效。脚本会提示「原生档位机制，本补丁不适用」 |
| ①′ 档位配置 `--reasoning-config` | 不需要（用 ①） | **需要**：把档位写进 `provider_config.json` 的 `providerModelRules[].config.optionSpecs.reasoningLevel`（`values` = 界面档位、末位即默认；`map` = CEL，内核发请求前求值并合并进请求体）。无需内核补丁 |
| ② 用量页去截断 | 需要 | 需要 |
| ③ 模型弹窗加宽 | 两处锚点 | 需要，但只剩**扁平弹窗**一处（渠道子菜单上游已改为自适应宽度 `w-max min-w-48`，脚本识别后报「无需补丁」） |
| ④ TPS 状态栏 | 需要 | 需要 |
| ⑤ 模型拉取按钮 | 需要 | 需要，模板已按 `optionSpecs` 新格式写入 |
| ⑥ 思考强度滑条 | 需要 | 需要 |
| ⑦ 增强提示词 | —（新功能） | 需要：按钮经 preload 桥 / main handler 用**选定**的模型调一次补全；提示词模板内置。0.5.9 起解析只选**真正可用**的供应商，并对失败做归因与分类重试；0.6.7 起支持**右键选模型**（菜单按供应商分组、选中即持久化、零重启生效，见第七节） |

> **升级会整体覆盖 app.asar**：除 ①（配置侧，写 `provider_config.json`）外，②–⑦ 升级后都需重跑。
> 其中**重打包级**（④ TPS 状态栏 / ⑤ 模型拉取按钮 / ⑥ 思考强度滑条 / ⑦ 增强提示词）重跑时会按内容比对
> 自动热更新脚本，无需先 `--revert`；② 用量图 / ③ 弹窗加宽是**字节级原地覆盖**，重跑即可。

- 3.14.x 下**不要**再跑思考等级内核补丁（`python zcode_patcher.py` 不带参数那条）：脚本会明确提示
  「该内核使用 3.14+ 原生档位机制（optionSpecs），本补丁不适用」——这不是故障，是预期行为。
  档位配置改跑 `python zcode_patcher.py --reasoning-config`（见下节）。
- **3.14.x 档位配置的标准命令**（把 `config.json` 的档位迁移进 `provider_config.json`）：
  ```bash
  python zcode_patcher.py --reasoning-config --check   # 只读：哪些模型已配/缺档位/已在界面手动配置过
  python zcode_patcher.py --reasoning-config --dry-run # 预演：只报告将改哪几条规则
  python zcode_patcher.py --reasoning-config           # 写入（先完全退出 ZCode）
  python zcode_patcher.py --reasoning-config --revert  # 还原（provider_config.json.reasoning-bak）
  ```
  已在界面「手动配置」过的模型（`manualProviderModelRules`）会自动跳过——内核 schema 禁止同一
  模型同时出现在两个规则列表，重复声明会让整份供应商配置降级为空。
- `model_pull.py` 与 `zcode-model-puller.js` 已适配新格式：拉取模型时直接写 `optionSpecs`，`--refresh` 会把旧 `reasoning` 条目迁移过来。
- 通用开关（所有补丁适用）：`--all` 对所有功能生效（等价于写全所有补丁参数）·
  `--dry-run` 只报告改动不写盘 · `--verbose` 打印安装探测细节 ·
  `--force` 跳过备份指纹校验（慎用）· `--prune` 清理安装目录里的补丁产物
  （`--prune --deep` 连当前 `.bak` 与 sidecar 一起清，之后无法 `--revert`，需重打才有记录）。
  每次执行结束会打印「执行汇总」表（补丁 × 目标 × 成功/失败），失败项返回退出码 1。
- **打补丁/还原前会预检 ZCode 进程**：运行中直接拒绝（退出码 2）。只读 `--check` 与 `--dry-run` 不受限。
- 备份带**版本指纹**（`*.bak.meta.json`）：客户端升级后旧备份自动归档（改名 `.stale-<时间>`），
  还原时若当前文件与备份不是同一版本会**拒绝执行**，避免把旧内核/asar 盖回新客户端。
- 确认版本：看客户端「关于」，或安装根目录（如 `D:\ZCode`）的版本信息。

### 插件开关（配置区）

插件在 ZCode 的「设置 → 插件 → 已安装 → 点开插件 → 高级信息 → 配置」里声明了 8 个开关。**不拨也能用**：没保存过配置时按清单默认值（全开）注入。拨动开关并点「保存配置」后，插件会在**下次会话启动时登记期望状态**，再由 `apply_after_exit.py` 在 **ZCode 退出时写入**客户端（写完自动重启 ZCode），不需要手动跑命令（插件的安装方式见仓库 README 的「安装」章节）：

> **为什么八项一律等退出**：`zcode_patcher.py` 的运行预检是**全局**的 —— `zcode_running()` 只查
> `tasklist` 里有没有 `ZCode.exe`，与 target 无关，命中就直接 `return 2` 拒绝写入（app.asar 被锁、
> `config.json` / `provider_config.json` 会被客户端回写覆盖）。而会话钩子**必然**在 ZCode 运行中触发，
> 所以「会话内立即生效」这条路走不通。`sync.py` 的策略是**先试立即写，被拒就自动转交退出后看护**，
> 这样纯 CLI 场景（没有 ZCode.exe 在跑）仍能即时生效。

| 开关 | 控制的功能 | 默认 | 生效时机 |
|---|---|---|---|
| 思考档位配置（3.14+） | `--reasoning-config` | 开 | ZCode 退出时自动应用，**再启动**才生效（两次启停） |
| 用量页去截断 | `--usage-chart` | 开 | ZCode 退出时自动应用，**再启动**才生效（两次启停） |
| 模型弹窗加宽 | `--model-width` | 开 | ZCode 退出时自动应用，**再启动**才生效（两次启停） |
| TPS 状态栏 | `--tps-footer` | 开 | ZCode 退出时自动应用，**再启动**才生效（两次启停） |
| 思考强度滑条 | `--thought-slider` | 开 | ZCode 退出时自动应用，**再启动**才生效（两次启停） |
| 增强提示词按钮 | `--enhance-prompt` | 开 | ZCode 退出时自动应用，**再启动**才生效（两次启停） |
| 设置页模型拉取按钮 | `--model-puller` | 开 | ZCode 退出时自动应用，**再启动**才生效（两次启停） |
| 思考档位内核补丁（旧版专用） | 无参数 | 开 | 仅 ≤3.11.2 需要；3.14+ 自动跳过并报「本版本不适用」 |

行为约定（`scripts/sync.py`，由 SessionStart hook 调用）：

- **零配置自动注入**：`resolve_wanted()` 的合并规则是**已保存的开关优先，没保存过的键退回
  `plugin.json` 里 `userConfig.*.default` 声明的默认值**。三种情形：
  ① 从没保存过 → 全部用默认值（这就是「装完即用」）；② 保存过一部分 → 保存的照做、缺的键补默认值
  （插件升级新增开关时不会漏）；③ 显式关掉 → 保存值是 `false`，优先于默认值，会被正常还原。
  **这一条是「安装成功但不生效」的根治点**：ZCode 只在用户点过「保存配置」后才写
  `plugins.options`，旧逻辑把「没表态」当成「不要做」，于是装完重启什么都没发生、界面上也看不出原因。
- **`na` 是必需的第三态**：`check_state()` 除了 `on` / `off` 还要能报 `na`（本版本不适用）。
  ≤3.11 专用的内核补丁在 3.14+ 会打印「本补丁不适用」，若只看「未打」→ 去执行 → 脚本空转一圈，
  最后却报成「已生效」。默认值全开之后这个误报每次装完都会出现，必须单独成一态、只标注不执行。
- **钩子是「登记心跳 + 后台化」就返回**（`sync.py --detach` → 子进程 `--worker`）。
  原因：hook 是**内联**执行的（`async` 字段当前无运行时效果），而同步要跑多次 `--check`
  （每次约 2 秒），同步做完再返回会拖住会话启动，还可能撞上钩子超时被砍掉。
- **首次自动注入时往会话里发一条说明**（`emit_notice()`），让「到底做没做」看得见；
  标记文件 `scripts/_autoinject.done` 保证只出一次。输出只用**顶层 `additionalContext`**：
  内核 `Lio()` 里 `t.additionalContext && n.additionalContexts.push(...)` 是**无条件**消费的，
  而 `hookSpecificOutput` 还要校验 `hookEventName` 与本次事件一致（写错 → 「Hook returned wrong
  event name」→ 这次钩子被标成失败）。另外 stdout 只在 `wQs()` 的 `startsWith("{")` 成立时才解析。
- 字节级补丁（图表 / 加宽）当场执行；重打包级补丁（TPS / 滑条 / 增强 / 拉取）写进退出后看护
  `scripts/apply_after_exit.py`，等 ZCode 完全退出时自动应用（**asar 本身没有被锁**，实测
  `CreateFileW(share=0)` 能独占打开；真正的原因是 ZCode **只在启动时读一次** app.asar，
  运行期写入不影响当前会话）。
- 开关状态与客户端实际状态不一致是正常的：清单里的默认值是静态声明，不反映历史补丁状态。
  **把开关拨成你想要的状态并保存**，同步后两者就一致了。
- 日志与心跳：`scripts/_sync.log`（同步日志）、`scripts/_sync.last`（每次被调用都刷新，
  证明「钩子到底跑没跑」）。没找到配置时会记录 `config.json` 里 `plugins` 的实际键名。
- 一键自检：`python scripts/doctor.py`（只读）——插件是否安装/启用、配置是否保存过、
  钩子是否跑过、八项补丁状态，末尾直接给卡点结论。`--where` 只查安装位置，
  `--where-all` 连扫过的全部候选目录一起列（本机 120 个，默认只列命中项）。
- **插件副本的三种落点**（判断「装没装」要**按清单里的 name 认**，不能靠目录名）：
  - `<数据>/cli/plugins/marketplaces/<市场 id>/` —— directory 来源；`source: "./"` 时**市场根即插件根**
  - `<数据>/cli/plugins/cache/<市场名>/<插件名>/<版本>/` —— GitHub/URL 来源，**插件根在版本目录里**
    （实测 `cache/zcode-plugins-official/computer-use/0.5.13/.zcode-plugin/plugin.json`）
  - `<数据>/cli/plugins/cache/<市场名>/plugins/<插件名>/` —— 市场仓库里带 `plugins/` 子目录时
  - **缓存会堆积多个历史版本**（本机 `cache/zcode-toolkit/zcode-tokenspeed/` 下有 0.3.1、0.5.x 等，
    更早还有 `dev-default-22da16fd/zcode-patcher/` 的 0.1.0/0.1.1/0.2.0/0.2.1），
    所以自检会报出全部命中项并按 mtime 提示最新的那份。
- **配置键格式是 `<插件名>@<市场名>`**（实测 `computer-use@zcode-plugins-official`）——
  匹配时只认 `<插件名>@…`，别用裸 `startswith`。

### 钩子机制要点（官方 `diagnosing-hooks` skill + 内核实测）

排查钩子问题时按这些硬事实对照，别凭直觉猜：

- **插件钩子无需额外开关**：只要**任意一个插件**提供了钩子，钩子运行器就自动启用。
  而配置文件（`~/.zcode/cli/config.json` 的顶层 `hooks`）里的钩子**默认关闭**，
  必须写 `hooks.enabled: true`；它的形状是 `{enabled, timeoutMs, maxOutputBytes, events:{<事件>:[...]}}`
  （比插件 `hooks/hooks.json` 多一层 `events`）。
- **没有信任门禁**：官方明确说明「所有插件钩子都是 runnable」，第三方与内置一视同仁；
  早期「未信任前仅诊断」的说法已过时。
- **事件名只有 7 个**：`SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PermissionRequest` /
  `PostToolUse` / `PostToolUseFailure` / `Stop`。`Notification` / `SubagentStop` / `PreCompact` **不支持**。
- **matcher 是大小写敏感的正则**，测试值随事件不同：`SessionStart` 的值是
  `startup` / `resume` / `clear` / `compact`（**省略 matcher = 匹配全部**）。
  ⚠️ 本插件的钩子**故意不写 matcher**：只写 `startup|clear|compact` 会漏掉 `resume`，
  而「重开 ZCode 恢复上次会话」走的正是 `resume`——那就会静默不触发。
- **`SessionStart` 在新会话的第一轮触发**（日志里表现为 `turnNumber: 0` 的 `session_start_hooks` 阶段），
  不是开机自启那一刻。所以「重启后必须真的开个会话」。
- **超时单位**：`type:"command"` 的 `timeout` 是**秒**，`process` 的 `timeoutMs` 是**毫秒**；
  解析顺序 `timeoutMs` → `timeout×1000` → 配置的 `timeoutMs` → 默认 60000ms。
- **字段不能混用**：`process` 只认 `command`/`args`/`timeoutMs`；`command` 认 `command`/`shell`/`timeout`/`timeoutMs`。混了钩子会被丢弃。
- **`async` 字段当前无运行时效果**，钩子一律内联执行。
- **钩子的 stdout 会被按严格 JSON schema 解析**：输出非 JSON（或含多余键）会被判为
  「运行失败」并丢弃输出。所以 `sync.py` 在后台路径上**一声不吭**（要反馈就写日志/心跳）。
- **模板变量**：`${CLAUDE_PLUGIN_ROOT}` / `${ZCODE_PLUGIN_ROOT}`（仅插件钩子）、
  `${CLAUDE_PROJECT_DIR}` / `${ZCODE_PROJECT_DIR}`、`${CLAUDE_SESSION_ID}`；
  这些变量同时会注入为环境变量。**`userConfig` 的值不在其中**——所以 `sync.py` 必须自己读
  `config.json` 的 `plugins.options`。
- **执行记录在 ZCode 日志里**：`~/.zcode/cli/log/zcode-<日期>.jsonl`。两类记录最有用：
  - `turn.phase.*` 且 `context.phase == "session_start_hooks"` —— 会话启动阶段跑过（链路本身是通的）；
  - `bootstrap.app.startup.plugins.completed` —— context 带 `pluginCount` /
    `enabledPluginCount` / **`hookCount`** / `diagnosticCount` / `skillRootCount`。
    **`hookCount` 是最靠前的一层证据**：它表示这次启动 ZCode 到底注册了几个钩子。
    为 0 就说明「插件没启用 / hooks.json 没被读到」——**轮不到讨论钩子有没有执行**。
    `doctor.py` 第 7 节会直接读它并给出结论，不用让用户自己去翻日志。
- **⚠ 中文 Windows 的编码坑（本插件踩过一次真故障）**：控制台是 cp936（GBK）。
  输出**走管道**时（`> log.txt`、`subprocess.run(capture_output=True)`）Python 不再走
  WriteConsoleW，而是按 cp936 编码 —— print 一个 GBK 里没有的字符（`✓` `✗` `⚠` `✅` `↻`）
  会抛 `UnicodeEncodeError`，**把整段输出打断**。实测：doctor 捕获 patcher 输出时，
  汇总表在 `✓ 用量页去截断补丁` 那行崩掉，用户只看到半张表 + traceback，
  还以为是补丁本身失败。交互式控制台不受影响 → **这个坑只在管道里露头，手跑脚本测不出来**。
  → 所有会 print 的脚本都从 `_console.py` 取 `safe_stdio()`（`errors=replace` 兜底，
  永不崩）与 `ok_mark()` / `bad_mark()` / `warn_mark()` / `glyph()`（编不出就换 ASCII 备选）。
  **`√` `×` `→` `·` `□` 在 cp936 里是有的**，所以标记用 `√`/`×` 而不是 `✓`/`✗`。
  新增会打印的符号前，先 `'字'.encode('cp936')` 试一下。

> **如果详情页「高级信息」里没有出现「配置」区**：这是 ZCode 侧的渲染问题，与插件清单无关——界面拿到的插件信息里 `userConfig` 为空时，配置区整个不渲染（`Y2t` 组件里 `userConfig` 为空直接 `return null`）。清单本身是正确的（Agent 侧 `M5s` 完整解析、`f5s` 赋 `userConfig: e.manifest.userConfig`、`jGo` 条件展开，链路已逐环节核对）。此时**直接写配置文件**，效果完全一样：
>
> `~/.zcode/cli/config.json` → `plugins.options["zcode-tokenspeed@<市场名>"]`（`@` 后面按实际安装的市场填；本机是 `zcode-toolkit`）：
> ```json
> "plugins": {
>   "options": {
>     "zcode-tokenspeed@zcode-toolkit": { "tps_footer": false, "model_puller": true }
>   }
> }
> ```
> 键名见上表；写入后同样由 `sync.py` 在会话启动时读取并应用。也可以直接打 `/zcode-patch-toggle` 让 AI 代改（会先展示"配置值 vs 实际状态"再改）。改 `config.json` 前先备份一份。

### 退出后自动注入

重打包级补丁（TPS 状态栏、拉取按钮）要求 ZCode **完全退出**。插件里由 `scripts/apply_after_exit.py` 承担：轮询等 ZCode 退出 → 按期望状态应用/还原 → 写日志。它由开关同步（`sync.py`）在检测到重打包级差异时自动拉起，平时不运行，**不需要常驻服务**。

> **为什么必须等退出（不是「文件被锁」）**：实测用 Win32 `CreateFileW(..., share=0, ...)` 能对
> `app.asar` 与 `zcode.cjs` 取得独占句柄，ZCode 运行期间**并没有锁住它们**，直接写也能成功。
> 真正的原因是 ZCode **只在启动时读一次** `app.asar` —— 运行期改写不会影响当前会话，
> 所以延迟到退出后写入纯粹是为了「让下一次启动读到的就是新内容」，而不是绕开锁定。

手动执行同类操作时，直接跑命令即可（ZCode 已退出的前提下）：

```bash
python "<skill目录>/scripts/zcode_patcher.py" --tps-footer
python "<skill目录>/scripts/zcode_patcher.py" --model-puller
```

> 也可以自己挂一条计划任务调用 `scripts/apply_after_exit.py`（它自行探测安装位置，
> 退出后按期望状态应用补丁并重启 ZCode）；不需要时删掉该任务即可。

八个补丁，均幂等、可检查、可还原、ZCode 升级后需重打：

| 能力 | 说法 | 命令 | 改哪里 |
|---|---|---|---|
| **档位配置（3.14+）** | 给自定义模型配思考等级 | `python zcode_patcher.py --reasoning-config [--check/--revert]` | `provider_config.json` 的 `providerModelRules.optionSpecs`（配置侧原生，**不打内核**） |
| 思考等级透传（≤3.11） | 给自定义模型配思考等级 | `python zcode_patcher.py [--check/--revert/--extract]` | 内核 zcode.cjs（原地改写，.bak 备份） |
| 打开统计图 | 用量页趋势图/饼图去截断 | `python zcode_patcher.py --usage-chart [--check/--revert]` | app.asar 内渲染文件（同长度原地改字节 + integrity 同步） |
| 打开状态栏 | 输入框下方居中统计条（**v2 纯 DOM 观测，3.12.2+ 安全**；右键可切工具栏/会话顶部 sticky）：本轮指标 + 会话累计（第 N 轮/输入/命中+平均命中率/累出） | `python zcode_patcher.py --tps-footer [--check/--revert]` | app.asar（重打包级：注入脚本 + 挂载 index.html） |
| 思考强度滑条 | 工具栏「思考 · 档名」入口，点击弹出拖动条面板（dsh-reasoning-effort 同款：胶囊轨道 + canvas 像素辐射 + 白色旋钮，连续跟手、松手吸附）；原生下拉隐藏，拖完即时生效 | `python zcode_patcher.py --thought-slider [--check/--revert]` | app.asar（重打包级：注入脚本 + 挂载 index.html） |
| 加宽模型弹窗 | 模型选择浮窗加宽，长模型名不再截断 | `python zcode_patcher.py --model-width [--check/--revert]` | app.asar 内主 bundle（同长度原地改字节） |
| **增强提示词** | 输入框旁一键润色草稿（可恢复原文）；**右键**弹出菜单选择使用哪个模型 | `python zcode_patcher.py --enhance-prompt [--check/--revert]` | app.asar（重打包级：renderer 脚本 + index.html + preload 桥 + main IPC） |
| 模型拉取按钮 | 设置页一键拉取/勾选模型 | `python zcode_patcher.py --model-puller [--check/--revert]` | app.asar（重打包级：renderer 脚本 + index.html + preload 桥 + main IPC） |

两个及以上功能可一次执行：`python zcode_patcher.py --usage-chart --model-width --tps-footer --thought-slider --model-puller`。
全部功能一条命令：`python zcode_patcher.py --all [--check/--revert]`（等价于写全上表所有补丁参数 + 内核补丁）。
另有命令行版拉模型（不动 asar，直接同步 config.json + provider_config.json）：`python scripts/model_pull.py --all [--dry-run]`。

**通用开关**：`--all`（对所有功能生效）· `--dry-run`（只报告改动不写盘）· `--verbose`（打印探测细节）· `--force`（跳过备份指纹校验，慎用）。

## 标准执行流程（AI 代执行与人工自助通用）

调用本 skill 时按以下序列执行，**AI 代执行时必须走完全部步骤，不得跳过核实与展示**：

1. **定位安装**：脚本自动探测（运行中进程 → 注册表 → 常见目录，跨 Windows/macOS/Linux，见「跨平台约定」）；探测不到就把安装根目录作为位置参数传入（加 `--verbose` 看每一步探测结果）。
2. **只读核实**（能否生效的判断，全部只读，可放心先跑）：
   - `python zcode_patcher.py --all --check`：**一条命令看全部 8 项**（含客户端版本、8 行执行汇总表）。
     用户问「现在什么状态」「装好了吗」时先跑这条；下面各条是单项细看。
   - `python zcode_patcher.py --reasoning-config --check`：**3.14.x 先看这个**——哪些模型已配档位、哪些缺档位、哪些已在界面手动配置过（冲突会跳过）。
   - `python zcode_patcher.py --check`：内核补丁状态；输出「原生档位机制（optionSpecs），本补丁不适用」即 3.14+，直接走上面的 `--reasoning-config`；≤3.11 且版本不在「已知符号表」时跑 `--extract`——能提取出锚点即可生效，提取失败说明内核结构变了，按「新版本锚点提取」人工分析后再动。
   - `python zcode_patcher.py --usage-chart --check`：两个截断表达式是否命中。
   - `python zcode_patcher.py --model-width --check`：扁平弹窗锚点是否命中（3.14.x 渠道子菜单锚点已不存在，报「上游已改为自适应宽度，无需补丁」属预期）。
   - `python zcode_patcher.py --tps-footer --check`：index.html 是否找到、注入状态。
   - `python zcode_patcher.py --thought-slider --check`：滑条脚本注入状态。
   - `python zcode_patcher.py --model-puller --check`：四组件（renderer / index 挂载 / preload 桥 / main handler）逐个是否就位。
   - `python zcode_patcher.py --enhance-prompt --check`：同上四组件（增强提示词与拉取按钮共用 preload/main 锚点，
     注入段按 `/*zp:begin:<块名>*/` 标记定界，两者可独立装卸、互不干扰）。
   - 脚本对「表达式出现次数 ≠1」「锚点不唯一」等情况一律拒绝盲改并报告原因——报告即结论，不要绕过。
3. **确认备份就绪并展示还原命令**（打补丁前必须完成，AI 代执行时明确提示用户保存还原命令）：
   - 档位配置：首次写入自动生成 `provider_config.json.reasoning-bak`
   - 思考等级：首次打补丁自动生成 `zcode.cjs.bak`（整文件备份）+ `zcode.cjs.bak.meta.json`（指纹）
   - 状态栏/滑条/拉取按钮：首次注入自动生成 `app.asar.<补丁>.bak`（整包备份，带 `.meta.json` 指纹）+ sidecar json
   - 统计图/弹窗加宽：sidecar `app.asar.chart-patch.json` / `app.asar.width-patch.json` 记录全部原始字节
   - **备份带版本指纹**：客户端升级覆盖内核/asar 后，旧备份指纹失配会被自动归档（改名 `.stale-<时间>`），
     还原时若当前文件与备份不同版本会**拒绝执行**（提示用 `--force` 或 `restore_clean.py`），
     避免"把旧版内核/asar 盖回新客户端"。
4. **展示执行命令与还原命令**——**单功能单命令，按用户点名的功能给对应命令，不要捆绑其他功能**（各补丁相互独立；低风险的档位配置/思考等级/统计图 AI 可在核实与备份确认后直接代执行，重打包级的状态栏/滑条/拉取按钮交由用户执行）。以「打开状态栏」为例：
   ```bash
   # 执行
   python "<skill目录>/scripts/zcode_patcher.py" --tps-footer
   # 还原（万一异常，保存备用；完全退出 ZCode 后执行，还原后重启 ZCode）
   python "<skill目录>/scripts/zcode_patcher.py" --tps-footer --revert
   ```
   3.14.x 配档位则是：
   ```bash
   python "<skill目录>/scripts/zcode_patcher.py" --reasoning-config          # 写入
   python "<skill目录>/scripts/zcode_patcher.py" --reasoning-config --revert # 还原
   ```
   人工自助时用户自行执行；AI 代执行时经用户确认后由 AI 运行，或用户复制命令自己跑。
   不确定会改什么时先加 `--dry-run` 预演（不写盘）。
5. **重启验证**：完全退出并重启 ZCode（Windows 运行中锁 app.asar，打补丁前必须退出；脚本已内置进程预检）后，按各功能的「验证」说明确认。
6. **失败回退**：执行上面展示的还原命令 → 重启 ZCode → 重新核实。思考等级补丁还原走 zcode.cjs.bak；状态栏还原自动清理 `.tps.bak` 与 sidecar。

## 全自动流水线（`autopilot.py`，无人值守场景用这条）

用户在**会话里**要求「一把跑完」「自动部署」「全自动」「不用我管」时，不要手敲上面那一串单命令，
直接用仓库根的流水线 —— 它把「环境自检 → 装依赖 → 构建 → 测试 → 部署 → 验证」串成一条链，
并自带重试、双通道日志与精确退出码。

```bash
python autopilot.py                                  # 本机全自动（含部署）
python autopilot.py --no-deploy                      # 只跑到测试，完全不碰客户端
python autopilot.py --unattended --report ci.md      # 无人值守 + 汇总另存（CI 用）
python autopilot.py --dry-run                        # 全程预演，不写任何文件
python autopilot.py --only build,test --verbose      # 只跑指定步骤，看调试细节
python autopilot.py --max-retries 5                  # 提高可恢复错误的默认重试次数
```

跨平台入口：`./run.sh`（macOS / Linux / Git Bash）与 `run.cmd`（Windows，双击可用）——
自动探测可用的 Python、切到脚本所在目录、参数原样透传。仓库里**没有任何写死的本机路径**。

### ★ 无人值守的核心矛盾与解法（AI 代执行前必须理解）

`zcode_patcher.zcode_running()` 是**全局运行守卫**：只要 `ZCode.exe` 还在进程表里就拒绝写入。
而无人值守的调用**恰恰总是发生在 ZCode 运行中**（SessionStart 钩子、用户在会话里喊「跑一遍」）。
所以 `deploy` 步骤不会去硬闯守卫，而是**自动挂载 `apply_after_exit.py` 看护**：

- 客户端没跑 → 立即 `--all` 注入，本步当场完成；
- 客户端在跑 → 挂看护并记 `deferred_step["scheduled"] = True`，报告里写「已排期」。

看护会等 ZCode **完全退出**、自动写入、再把 ZCode 拉起来 —— 全程无需人工。
汇报给用户时务必说清：**「本次改动已排期，下次完全退出 ZCode 时自动生效」**，
不要说成「已完成」，否则用户会以为补丁没起作用。

> 想让改动**立刻**生效（不想等下次退出）：请用户完全退出 ZCode，然后前台跑
> `python "<插件目录>/skills/zcode-tokenspeed/scripts/sync.py"`（结论打终端 + `_sync.log`）。
> 沙箱/工具进程**无法**起脱离进程，所以这件事只能请用户做。

### 错误处理：重试与否只看类别，不看「是否致命」

`ErrorKind.RETRYABLE = {transient, network, timeout, lock}` 这四类会退避重试
（1s → 2s → 4s → 8s 封顶）；build / test / permission / env / dep 立刻停下并给出修复建议。

**这里有个已经踩过的退化，改动 `execute_step()` 时别踩回去**：早先写成
`retryable = exc.kind in RETRYABLE and not exc.fatal`，而 `StepFailure.fatal` 默认 `True`
—— 于是所有可恢复错误都被静默剥夺了重试机会，重试机制**看起来实现、实际从未生效**，
且不报任何错，只表现为「偶尔失败」。`fatal` 的语义是「这一步最终会记为失败」，
**不是**「禁止重试」。

### 日志与退出码（汇报时引用这两个）

每次运行在 `logs/`（已 gitignore）同时写两份：`autopilot-<时间戳>.log`（人读）与
`.jsonl`（机器读，含 `step`/`kind`/`duration`/`attempts`/`retries`，结尾 `summary` 带 `exit_code`）。

| 码 | 含义 |
|---|---|
| `0` | 全绿 |
| `1` | 有步骤失败 |
| `2` | 命令行参数写错（步骤名拼错、过滤后无事可做） |
| `3` | 环境不满足（Python < 3.10、目录结构不对） |
| `4` | 依赖无法自动满足且不可忽略 |
| `5` | 部署被阻塞（客户端在运行且看护挂不上） |
| `130` | 被 Ctrl-C 中断 |

`2` 与 `1` 分开是有意为之：CI 里「参数写错」和「测试没过」得往完全不同的方向查。

### CI

`.github/workflows/ci.yml` 用 `matrix.include` 覆盖 `ubuntu-latest`（py 3.10 / 3.12 / 3.13）、
`windows-latest`（3.12）、`macos-latest`（3.12），主步骤为
`python autopilot.py --no-deploy --unattended --report ci-report.md`（CI 不注入 app.asar）。
仓库里还没有 `autopilot.py` 时会自动回退到逐项语法检查 + `unittest discover`。
报告与日志用 `actions/upload-artifact@v4` 在 `always()` 下上传，便于定位偶发失败。

## 跨平台约定

| 系统 | 安装根目录（resources 的上一级） | 典型位置 |
|---|---|---|
| Windows | `D:\ZCode`、`%LOCALAPPDATA%\Programs\ZCode` 之类 | 探测顺序：运行中进程路径 → 注册表卸载信息 → Program Files 系目录 |
| macOS | `/Applications/ZCode.app/Contents` | 探测 /Applications 与 ~/Applications 下的 `.app` 包（自动进入 `Contents`） |
| Linux | `/opt/ZCode`、`/usr/share/ZCode` 之类 | 探测 /opt、/usr/share |

- 关键文件相对安装根目录固定：`resources/glm/zcode.cjs`（内核）、`resources/app.asar`（桌面端资源包）
- Python ≥ 3.10，用系统可用的 `python3`/`python` 即可，脚本仅用标准库
- 改动脚本后先跑回归测试：`python -m unittest discover -s tests -v`（纯标准库；含 asar 重打包往返、
  备份指纹、档位配置迁移、注入代码语法、峰值内存约束等用例（数量以 `tests/` 实际为准）；
  本机装了 ZCode 时还会只读校验真实 asar 的 integrity）
- 一键跑完整链（含只读核实与自动部署排期）用仓库根的全自动流水线，见上节「全自动流水线」
- Program Files / /Applications 类目录可能需要管理员/sudo 权限
- 执行 AI 可按上述规则自行定位安装（如 `ls /Applications`、查运行中进程的 exe 路径）

## 升级后自查清单

ZCode 升级会覆盖 zcode.cjs 与 app.asar，升级后过一遍（**先完全退出 ZCode**；`--check` 不受此限）：

```bash
python zcode_patcher.py --check                    # 思考等级：3.14+ 会提示"不需要本补丁"
python zcode_patcher.py --reasoning-config --check # 档位配置（3.14+ 看这个）
python zcode_patcher.py --usage-chart --check
python zcode_patcher.py --model-width --check
python zcode_patcher.py --tps-footer --check
python zcode_patcher.py --thought-slider --check
python zcode_patcher.py --model-puller --check
python zcode_patcher.py --enhance-prompt --check
```

失配的按「自助使用流程」重打；思考等级补丁在 ≤3.11 新内核上先 `--extract` 确认锚点可提取。
重打包级补丁（状态栏/滑条/拉取按钮）被升级覆盖后直接重跑即可——旧备份会被自动归档，不会误还原。

## 一、思考等级：完整结论一张表

| 场景 | 档位来源 | 是否需要 config 配置 | 是否需要内核补丁 |
|---|---|---|---|
| 模型 id 含 "ox-alpha"，ZCode ≥ 3.9.1 | 内核硬编码白名单（`isOxAlphaReasoningModelId`），天生 low/high/max，defaultLevel=max | **不需要**（配了也是冗余） | **不需要** |
| 其它模型，ZCode ≥ 3.9.1，标准档名（low/medium/high/xhigh/max） | 引擎原生通用表：anthropic → `thinking:{type:"adaptive"}` + `output_config.effort` | **需要**：模型条目配 `reasoning.variants` | **不需要** |
| 其它模型，自定义档名（如 "turbo"） | 无原生表，留空 | 需要 | **需要**：补丁兜底合成 |
| ZCode ≤ 3.8.1，任何模型任何档位 | 无原生表 | 需要 | **需要** |

判别某次请求走的哪条路：`thinking:{type:"adaptive"} + output_config.effort` = 原生路径；`thinking:{type:"enabled", budget_tokens:N}` = 补丁兜底。补丁在原生表非空时惰性（`??` 短路），留着无害但升级后要重打才有意义。

### 档位从配置到请求的完整链路（补丁原理）

```
~/.zcode/v2/config.json  模型条目 reasoning.variants      ← 档位名数组（UI 显示的就是它）
      │  桌面端 host：variants → 内部 levels（每档参数对象）
      │  内核 override 构建器 → catalogOverrides 注入模型目录
      ▼
capability.reasoning = { enabled, levels, providerOptionsByLevel }
      │  内核档位解析函数 sD(3.8.1)/AD(3.9.1)/mN(3.11.2)(modelRef, 选中档名, catalog)
      ▼
{ level, providerOptions: providerOptionsByLevel[档名] }   ← 断点：自定义模型此表为空
      │  合并进请求
      ▼
anthropic: thinking:{type:"enabled",budget_tokens:N} + effort
openai 系: reasoning_effort:"档名"
```

`providerOptionsByLevel`（档名→请求参数表）只给内核认识的白名单家族（claude/glm/deepseek 等）下发；自定义模型拿到空表，档位能选中却不产生任何请求参数。补丁 = 在取参处加 `?? zCfgEffort(档名)` 兜底，按档名现场合成参数；白名单模型表非空，`??` 短路，行为零变化。

### ox-alpha 白名单的代码依据（3.9.1+ 原生支持）

- **(a) 硬编码白名单**：内核 override 构建函数 `t2t` 首分支 `t.reasoningProfile===Iue || j5(t.modelId)` → anthropic 协议拿 `jye()` = `{defaultLevel:"max", levels:["low","high","max"], providerOptionsByLevel:每档{effort, thinking:{type:"adaptive"}}}`。其中 `Iue=iLe([111,120,45,97,108,112,104,97])="ox-alpha"`，`j5` 注册名 `isOxAlphaReasoningModelId`（正则 `/ox-alpha/i` 子串匹配 + 两个内部预览模型）。与端点协议无关，同端点其它模型无此待遇。
- **(b) 标准档名通用表**：非白名单模型配了 `reasoning.variants` 且档名为标准名，构建时也套通用表（anthropic → adaptive+effort）。config 里配的档位集合即 UI 显示的集合。

### 工作流

1. **配档位**（每模型一次，完全退出 ZCode 后改 `~/.zcode/v2/config.json`）：
   ```json
   "reasoning": {"enabled": true, "variants": ["low", "high", "max"], "defaultVariant": "max"}
   ```
   **关于 `"zcode": {"modified": true}`**（客户端内部的"用户已改过、目录同步别覆盖"豁免标记：内核做模型目录合并时，`modified===true || deleted===true` 的条目整体保留本地版本）：
   - **走 UI 新增/编辑模型时客户端会自动盖章**——保存模型列表会重写整条 provider 的模型（3.11.2 实测：UI 里新增 deepseek-v4-pro 后，同 provider 的 deepseek-v4-flash 也一并被补上该标记）。UI 路径无需手动补。
   - **手改 config.json 绕过了 UI，没有任何路径替你盖章**，需要豁免就手动补——并入现有 `zcode` 块（如 `"zcode": {"modalitiesConfigured": true, "modified": true}`），别整体替换，否则丢掉既有键。对参与目录同步的条目是刚需；对自建 provider 的模型目前是纯保险（没有同步会碰它们，实测不补也长期存活）。
   - **手改更隐蔽的风险在 UI 保存**：保存 provider 的链路会先剥掉旧条目的 `variants`/`modified`/`contextWindow` 等（`omitModelCarryoverKeys`）再按表单状态重建——手改的 `reasoning.variants` 可能被一次 UI 保存吃掉（"档位退回开启/关闭开关"的另一条成因，与目录同步并列）。改完 config 后避免在 UI 里保存该 provider；保存过就回头确认档位仍在。
2. **仅当需要补丁时**（见判断表）：`--check` → 打补丁 → 完全退出并重启 ZCode。新版本无已知锚点先 `--extract`。
3. **验证**：rollout（`~/.zcode/cli/rollout/model-io-*.jsonl`）请求体里 `thinking`/`effort` 随档位变化；引擎日志（`~/.zcode/cli/log/`）无 400。**注意 rollout 请求体是脱敏的，`thinking` 字段会被剥掉（内置模型也一样），不能作为判据**——以 OpenRouter Dashboard → Activity Log 之类的外部请求日志为准。

### 内核补丁点与版本匹配

断点只有一处——内核 `resources/glm/zcode.cjs` 里的**思考档位解析函数**（见上方链路图）。zcode.cjs 是 esbuild 压缩产物，**每个版本的顶层符号名整体重排**，锚点（解析函数完整原文）必须与已安装版本逐字符一致：

| 版本 | 已知符号 |
|---|---|
| 3.8.1 | sD / G_e / Fgo / gXe |
| 3.9.1 | AD / Zye / fxo / utt |
| 3.9.2 | RD / Xye / Sxo / ptt |
| 3.11.2 | mN / k2e / CIo / _nt（本版返回处局部变量名为 s，替换逻辑已通用化） |

脚本按「全文唯一匹配」自动选择版本：对每个已知锚点统计出现次数，**恰好有一版 =1 才动手**；全为 0 或多版命中都拒绝修改。锚点匹配失败 ≠ 补丁思路失效——要改的内核点（解析函数返回处 `providerOptionsByLevel?.[X]` 查表表达式）所有版本语义不变，变的只是符号名。

**新版本锚点提取**：首选 `--extract` 自动提取——按结构特征（函数以 `{level:...providerOptionsByLevel?.[...]}:void 0}` 收尾、体内无嵌套 function）定位，已打补丁的文件自动回退 .bak 原始件，打印可直接粘贴进脚本 `ANCHORS` 字典的锚点（打印格式即条目格式，加个版本号键即可）。`--extract` 失败（候选数 ≠1）时人工提取：在内核里搜 `providerOptionsByLevel?.[`，找到以 `:void 0}` 收尾的完整解析函数整段复制为锚点；若函数体结构有变（不止符号改名），同步调整 `replacement_for()` 的拼接假设。加锚点后用 `--check` 验证恰好唯一命中再打。

### 档名与预算映射（补丁内置）

| 档名 | anthropic budget_tokens | openai 系 reasoning_effort |
|---|---|---|
| low | 4000 | low |
| medium | 8000 | medium |
| high | 16000 | high |
| xhigh | 32000 | xhigh |
| max | 32000 | max（**原名透传**，不折算成 xhigh） |
| 其它任意名 | 16000 兜底 | 不带 |
| disabled / none / off / nothink | thinking disabled | `reasoning_effort:"off"`（**不省略字段**） |

改数值或加档名：编辑脚本 `HELPER` 里的映射行 `{low:4e3,medium:8e3,high:16e3,xhigh:32e3,max:32e3}[t]??16e3`，重打补丁。

**openai 侧为何原名透传**（实测 workbuddy2api 网关联动得出）：智谱系网关的档位表是 low/high/max 且带 supported_efforts 自动降级——补丁若把 max 折算成 xhigh，网关会降级成 high（max 档丢失）；若「关闭」省略 reasoning_effort 字段，上游会落到默认档（glm-5.3 默认≈max，等于关不掉）。因此 openai 侧一律发原始档名：网关认识就透传，不认识自动降级/floor 到最近档（有日志）。注意「关闭」发 `off` 在"支持档全高于 off"的网关会被 floor 到最低档（如 low）而非真关闭——真关闭需要网关侧特判（如 workbuddy2api 正在加的 canDisableThinking 处理）；对官方 OpenAI 端点 `max`/`off` 是非法值会 400，此折中面向智谱系自定义网关生态。

**输出上限约束**：anthropic 协议要求 `budget_tokens < max_tokens`，等值会被内核钳到 max-1。模型条目不写 `limit.output` 时请求不带 max_tokens，由网关兜默认值；若写了 `limit.output`，必须大于所选档位预算（如 output 32000 配 max 档 32000 会直接 400）。

### deepseek 家族实测记录（3.11.2）

- 内核 override 构建器（`gwt`）分支顺序：ox-alpha 白名单 → glm-5.3 → kimi-k3 → **config.reasoning（自定义档位在此生效）** → 家族兜底
- 无自定义配置时 deepseek 走兜底 `CA()`：只有 enabled/disabled 两档开关、预算固定 1024
- 配了 variants + 内核补丁后：deepseek-flash 实测 max 档下发 `budget_tokens:32000 + effort:"max"`（补丁兜底路径，与内置 1024 明显区分）

### 故障排查

| 现象 | 原因与处理 |
|---|---|
| 档位能选但请求无 thinking | 内核补丁没打或打完没重启；`--check` 确认（3.9.1+ 标准档名走原生，无补丁也应生效） |
| 400: budget_tokens 必须小于 max_tokens | 所选档位预算 ≥ `limit.output`；调大上限或不设上限 |
| 400: max_tokens 缺失 | 不设 output 上限且网关不兜默认时出现；给模型设较大的 `limit.output` |
| 升级后失效 | 内核/app.asar 被覆盖；重跑补丁（无已知锚点先 `--extract`） |
| 档位选不到某名字 | `variants` 里没写，或 `defaultVariant` 不在列表内 |
| 档位配置丢失（退回"开启/关闭"开关） | 手改的 `reasoning` 被覆盖，两条成因：①目录同步——补 `zcode.modified: true` 豁免（UI 加的模型客户端会自动补）；②UI 保存 provider 时条目被重建、连带剥掉 `variants`——改完 config 别在 UI 里保存该 provider，保存过就回头确认档位仍在 |

## 二、打开统计图：用量页去截断

「设置 → 用量」两处展示截断，本补丁一并放开：

| 位置 | 原始行为 | 补丁点 |
|---|---|---|
| 每日 Token 趋势图 | 只画 Top 6 模型折线（`n.models.slice(0,6)`；每日 total 含全部模型） | 改为 `n.models` 全量出线 |
| 模型用量饼图 | 模型 >6 个时只画 Top 5，其余合并为「其他模型」（`i=n.length>Q,a=i?Q-1:Q`，Q=6） | 改为 `i=!1,a=1/0` 全量出块、无合并 |

```bash
python zcode_patcher.py --usage-chart            # 打补丁（asar 内同长度字节级原地覆盖）
python zcode_patcher.py --usage-chart --check    # 查状态
python zcode_patcher.py --usage-chart --revert   # 从 sidecar 还原全部原始字节
```

- 原理：解析 asar 头定位渲染文件偏移，替换截断表达式后用空格补齐到原字节长度原地写回——asar 头、offset、unpacked 结构零改动，无需重打包
- **integrity 同步**：改内容的同时按 sha256 hex 定长特性，同长度原地交换 asar 头里该条目的 integrity 哈希串，使记录与内容一致（Electron 默认不校验，但保持诚实；打/还原/已打重跑三条路径都会自动同步，可修复历史遗留的失配）
- 原始字节 base64 存同目录 `app.asar.chart-patch.json`（sidecar，含全部补丁点记录）
- **sidecar 带 asar 尺寸指纹**：每条记录绑定当时的 app.asar 总大小，ZCode 升级覆盖 asar 后旧 offset 不可信，指纹失配的记录自动作废重建（实测 3.9.1→3.9.2 升级后重打正常；TPS 重打包后脚本自动同步本 sidecar 的 offset/指纹）
- 两处调色盘均 6 色循环取色，第 7+ 个模型颜色重复，靠图例/标签区分
- 定位按文件名特征 + 截断表达式匹配，与文件名哈希无关，跨版本稳定（3.9.1→3.9.2 文件名哈希变化仍能命中）

## 三、加宽模型选择弹窗

模型选择浮窗（点工具栏模型名弹出的那个，含「先选渠道、再选模型」的二级子菜单）默认宽 `w-48` = **192px**，长模型名（如 `cn:deepseek-v4.1-flash`、带前缀的供应商模型）会被 `truncate` 截断，只能靠 hover 的 `title` 看全。本补丁把它加宽到 `w-80` = **320px**。

```bash
python zcode_patcher.py --model-width            # 打补丁（同长度字节级原地覆盖）
python zcode_patcher.py --model-width --check    # 查状态
python zcode_patcher.py --model-width --revert   # 从 sidecar 还原
```

两处补丁点（都在渲染层主 bundle，如 `out/renderer/assets/styles-*.js`）：

| 位置 | 原始 | 补丁后 |
|---|---|---|
| 二级子菜单（渠道 → 模型列表，`j??\`w-48\``） | 192px | 320px |
| 扁平弹窗（无渠道分组的单层列表，`\`w-48 max-h-72 overflow-y-auto\``） | 192px | 320px |

- **零重打包**：`w-48` 与 `w-80` 字面量**等长**（同为 4 字符），属同长度原地覆盖，asar 头/offset 零改动；ZCode 运行中也能写（不涉及文件替换）
- 宽度类名需已编译进 CSS：已核实 `.w-80` 存在（若未来版本裁剪了该类，可换 `w-64`/`w-72`/`w-96`，都已在 CSS 中；`min-w-*` 多数未编译，勿用）
- **文件按内容定位**（不依赖 assets 文件名里的哈希），跨版本稳定
- 状态判定兼容"已打"：锚点已被替换后，用替换后形态反查确认，不会误报「未找到锚点」
- 原始字节存 `app.asar.width-patch.json`，与图表补丁同为字节级 sidecar

## 四、打开状态栏：TPS 统计胶囊

> **实现：`zcode-tps.js`（唯一方案）—— 读 ServicePort 事件流，含精确 tok/s / 首 token / out**
>
> 数据来自 preload 转交的端口事件流：`usage.delta`（真实 outputTokens/inputTokens）、
> `stream.chunk`、行事件（turnHeader/userInput/reasoning/assistantText/row.delta）。
>
> ⚠️ **脚本内绝不调用 `port.start()`**（这是唯一但关键的约束）
>
> MessagePort 队列一旦启用，消息只派发给"启用瞬间已注册的监听器"。脚本是普通 script，
> 早于 `type=module` 的应用主包执行；若先 `start()` 就会消费掉服务端的 `Initialize`
> 启动握手 → 应用协议客户端永远收不到握手 → **ZCode 3.12.2 卡在启动界面**。
> 正确做法：只 `addEventListener`，把 `start()` 留给应用（届时双方都收到消息）。
>
> 真实 Chrome / MessageChannel 实测（双方持有同一 port）：
>
> | 做法 | 我们收到 | 应用收到 | 结果 |
> |---|---|---|---|
> | 我们先 `start()` | `["Initialize"]` | `[]` | ✗ 卡启动界面 |
> | 只监听、不 start | `["Initialize"]` | `["Initialize"]` | ✓ 双方正常 |
>
> **注意：不可用 Node 验证此语义** —— Node 的 MessagePort 在 `addEventListener` 时隐式
> start，会让"不 start"也失败，从而误判为"方案不可行"（曾据此绕过一圈）。
>
> **已知特性**：tok/s 在开始生成后约 1~4 秒才出现（滑动窗口需 ≥2 个采样点，且思考阶段
> 无文本增量），期间只显示 `●` 与时间，属预期；out（本轮累计输出）到达后立即显示。
>
> 本脚本基于社区分享版本实现，只删除了 `port.start()` 一行并补充自诊断，**计算逻辑与原版一致**。
> ⚠️ **回滚预案**：改动 asar 前先 `python scripts/restore_clean.py --backup` 存一份干净副本，
> 若客户端异常，完全退出后 `python scripts/restore_clean.py --latest` 秒级还原，无需重装 ZCode。

> 本能力与思考等级补丁基于社区分享版本实现（原作者已授权"可以直接借鉴定制"）；本仓库在原基础上做了打包内核重写（纯 Python、保留 unpacked、原子替换）、跨平台/跨版本适配、全外科手术式还原等工程化改造。

**默认挂在输入框（composer 卡片）下方、水平居中**的独立统计行（右键可切到工具栏行内居中或会话顶部 sticky），左组为**当前会话最近一轮**的即时指标，右组为**当前会话累计**（不含时间）：

```
生成中:  ● 32 tok/s · out 410 │ 第 8 轮 │ 输入 45.2k · 命中 38.1k · 平均命中 84.00% · 累出 12.3k
结束后:  ● 首 token 37s · out 1.7k │ 第 8 轮 │ 输入 45.2k · 命中 38.1k · 平均命中 84.00% · 累出 12.3k
空会话:  ●            （绿点空态常驻，不显示假时钟）
```

- **会话累计口径**：轮数=可见轮次数；输入/命中/累出=各轮 `usage.delta` 累加（API 计费视角，每轮 inputTokens 含历史所以累计值偏大，属预期）；平均命中=累计命中 ÷ 累计输入，**固定两位小数四舍五入**（`fmtPct`，不足补零，如 `84.00%`）；流式中未报 usage 的轮以内容估算兜底。
- **渐进降级顺序**：溢出时先丢本轮 out → 首 token → tok/s，会话累计段保留。

```bash
python zcode_patcher.py --tps-footer             # 注入 scripts/zcode-tps.js
python zcode_patcher.py --tps-footer --check     # 查状态
python zcode_patcher.py --tps-footer --revert    # 整体还原
python zcode_patcher.py --tps-footer --tps-src /path/to/zcode-tps.js   # 指定注入源
```

### 行为规则（验收标准）

- **绿点 ● 常驻**：有可展示的轮次就在；流式生成中绿点发亮，空闲静态。不显示时间。
- **分隔符**：组内 `·`、本轮组与会话累计组之间竖线 `│`；标签灰、数值白、tok/s 与平均命中橙、tabular-nums 对齐。
- **同一 turnId 复用（编辑重发/重试）自动清零**：检测到新一轮开始即重置旧统计，杜绝「时间变新、指标是旧的」残留。
- **out 语义**：**本轮累计输出**（最近一次提问→回答完成为止），非会话累计。
- **动态刷新**：流式中 1 秒节奏刷新——tok/s 为 4s 滑动窗口即时速度、out 为本轮估算值；基于回答文本的 token 估算（CJK 1 字≈1 token、其余 4 字符≈1 token）。`usage.delta` 精确值随每次模型请求完成到达即覆盖估算；轮结束后为精确值（精确 out ÷ 首块→末次 usage 的解码窗口）。
- **静默期保持**：工具执行期间文本停止增长，速度保持最近值不消失；点停止/出错时该次请求不报 usage，out 以内容估算兜底、速度保持最近值——已产生的数据不凭空消失。
- **切换会话立即消失**：渲染只认「DOM 可见轮次（`section[data-turn-id]`）+ `data-session-id` 匹配当前会话」双重条件，不依赖任何会话切换事件；多会话并行时各 tab 互不干扰。
- **历史会话累计缺失**：usage.delta 不回放，重新打开旧会话拿不到当时的 token 统计，属预期。
- **无假时钟**：不显示时间段，绝不拿当前时间冒充轮次时间。
- **空会话常驻**：当前会话无任何轮次（新会话/未对话）时显示仅绿点的空态胶囊，进入对话后自然切换为完整指标。

### 位置切换（胶囊右键）

统计条右键弹出自绘菜单，可在「输入框下方（默认）/ 输入框工具栏 / 会话顶部 sticky」间切换，`localStorage`（键 `ztps-pos`）记忆，重开保持：

- **输入框下方**：composer 卡片之后的独立行（`insertAdjacentElement` 于卡片后），`alignSelf: stretch` 与输入框同宽。
- **输入框工具栏**：行内水平居中。
- **会话顶部**：吸附到消息区滚动容器（从 `section[data-turn-id]` 向上找第一个 `overflow-y: auto/scroll` 且高 >120px 的祖先）第一个子元素，`position: sticky; top: 8px` 悬浮；host 上 `pointer-events: none` 放行下方消息。
- 数据层零改动，仅挂载点不同；挂载点找不到（空态/设置页）时自清理，`window.__ztpsDiag.posMode`/`hiddenReason` 可诊断。右键修复注记：菜单用 pointerdown 捕获关外部点击时必须放行菜单内部按下，否则 click 在已移除节点上落空、选项永远点不中。

### 数据链路原理（无常驻服务）

1. ZCode 桌面端 preload 把主进程的 MessagePort 经 `window.postMessage("zcode:service-port", "*", [port])` 转交渲染页面；注入脚本监听该事件接管端口（`window.__ztpsHook` 可对存量端口手动补挂，`window.__ztpsPort` 暴露端口供调试旁路监听）。
2. 会话协议帧为二进制（Uint8Array）内嵌 JSON（自首个 `{` 起），两类：
   - **version:1 事件流**（顶层带 sessionId/sourceCommandId/occurredAt）：`usage.delta`（inputTokens/outputTokens/cacheReadTokens/totalTokens/reasoningTokens，**每次模型请求完成时发**——一轮含工具调用会有多条，out 为该次请求输出）、`stream.chunk`（`assistantMessageId` + `chunkLength` + `channel`，流式期间每 50-100ms 一批）
   - **conversation 行事件**（`frame.payload.deltas`/`events`）：`turnHeader`（startedAt/endedAt/state）、`userInput`（createdAt）、`reasoning`/`assistantText`（`text` 全量 + `assistantResponseId`）、`row.delta`（`{rowId, path:"text", append:"文本增量"}`）
3. **轮关联链**（事件里的 id 有两套，务必分清）：轮的 key 是 productTurnId（`msg_xxx`，与 DOM `section[data-turn-id]` 一致）；stream.chunk 的 `assistantMessageId` 是 assistantResponseId（另一个 msg_xxx），需经行事件的 `assistantResponseId → turnId` 映射中转；usage.delta 经 `sourceCommandId` 关联（turnHeader/userInput 行携带）。关联断了的表现：out/tok/s 一直不出现。
4. 渲染：扫描 `section[data-turn-id]` + sessionId 双条件取当前会话最新轮 → 算指标 → 更新胶囊。
5. 刷新机制：MutationObserver 回调里 16ms 节流的**同步**刷新（切换会话零残留）＋ 60ms 防抖全量扫 ＋ 1s 估算刷新节奏；渲染带内容签名（stamp/ttft/tps/out/streaming），数据未变零 DOM 写，保证同步刷新不触发 observer 自激。

### 注入原理（asar 重打包级）

与统计图补丁的同长度原地覆盖不同，状态栏要**新增文件**，必须整体重打包：

1. 注入内容：`out/renderer/index.html` 的 `</body>` 前插 `<script src="./zcode-tps.js"></script>`；zcode-tps.js 作为新条目写入 `out/renderer/`。index.html 无 CSP meta、无 nonce，普通脚本标签即可（在 `type="module"` 的 React bundle 之前同步执行，注册监听早于应用挂载）。
2. asar 布局（读/写同一公式）：头 16 字节 = 4 个 uint32 LE `[4, headerSize, pickleLen, jsonLen]`，`pickleLen = 4 + jsonLen + pad4`，`headerSize = 8 + jsonLen + pad4`，数据区起点 = `16 + jsonLen + pad4`（pad4 把 JSON 补齐到 4 字节倍数）；文件条目 `offset` 为相对数据区起点的字符串，全部文件带 integrity（SHA256 全文 + 4MB 分块 hex）。
3. 重打包流程：读全量 → 树上删条目/插占位/标记覆盖 → 全部条目 offset 重排 → 覆盖与新增条目重算 integrity → 写临时文件 → **回读校验**（逐条比对注入条目字节）→ 原子替换。
4. **实现关键坑**（改 `_repack_asar` 前必读）：offset 重排会直接改写条目，此后从旧文件切片必须用**重排前快照的旧位置**，否则「新 offset + 旧数据区起点」错位读取（实测 50 个抽查文件错 11 个，且改动文件恰好走覆盖分支不受影响，极易漏测）；新增条目的树插入必须在重打包函数内部做（外层持有的树引用与函数内部重新读入的不是同一棵）。
5. 备份与记录：首次注入前整包备份 `app.asar.tps.bak`；`app.asar.tps-patch.json` 记录原始 index.html（base64）与 asar 尺寸指纹。
6. 与统计图补丁联动：重打包使 chart sidecar 的绝对 offset/指纹失效，脚本自动按「文件路径 + 尺寸」重定位同步；反向无影响（统计图是同长度覆盖，不改 offset）。

### 升级 / 回退 / 排障

| 现象 | 处理 |
|---|---|
| 升级后胶囊消失 | app.asar 被覆盖，重跑 `--tps-footer`（注入源默认 skill 自带 zcode-tps.js） |
| 重启后无胶囊 | `--tps-footer --check` 看 state；渲染进程 console 查 `window.__ztps` 是否存在 |
| console 出现 CSP 拦截报错 | 当前版本 index.html 无 CSP；若未来版本加了，需同步放宽 `script-src` 允许同目录脚本 |
| 指标一直只有「● 时间」 | usage.delta 未关联到轮（看 `window.__ztpsTurns` 里轮的 out/lastUsageAt 是否为空）；版本升级导致帧结构变化时，用 `window.__ztpsPort` 旁路监听原始帧比对字段 |
| tok/s 不出现 | 该轮从未有过文本流（纯工具调用轮）时无速度可算，属预期；有文本流后静默期（工具执行）保持最近值 |
| 想换脚本逻辑 | 改 zcode-tps.js 后重跑 `--tps-footer`（内容变了会自动热更新脚本条目，无需 revert） |

## 五、模型拉取按钮：设置页一键拉取/勾选模型

设置 → 模型供应商页注入「⚡️ 自动拉取模型」按钮：点开弹窗自动拉取该供应商 `/models` 接口的模型列表，标注「已添加/新模型」并勾选，保存后经**原生 IPC** 读写 `~/.zcode/v2/config.json` 并自动刷新界面——不用退出 ZCode、不用手改 config。

```bash
python zcode_patcher.py --model-puller             # 注入（默认用本 skill scripts/zcode-model-puller.js）
python zcode_patcher.py --model-puller --check     # 查状态（逐组件：renderer/挂载/preload/main）
python zcode_patcher.py --model-puller --revert    # 整体还原
python zcode_patcher.py --model-puller --puller-src /path/to/zcode-model-puller.js
```

- **来源**：前端脚本为 vendored 的第三方实现（MIT，版权声明见脚本文件头）；原实现的 npx @electron/asar 全量解包/重打包方案未采纳（Node 依赖 + 会把 12 个 unpacked 原生模块打进 asar 内部，Electron 无法从 asar 加载 .node），改用本 skill 自带的纯 Python `_repack_asar`（unpacked 条目原样跳过、原子替换、回读校验），路径走 `discover()` 跨平台探测（原项目仅支持 macOS）
- **注入四件套**：① `out/renderer/zcode-model-puller.js` 新文件；② index.html `</body>` 前挂 `<script type="module">`（与 TPS 同文件共存、互不干扰）；③ preload 在 `contextBridge.exposeInMainWorld("zcode",{` 对象开头插入 3 个 IPC 桥方法；④ main 在 `SaveMcpToUserDirectory` 注册语句前插入 3 个 IPC handler（读 config / 写 config（先落 config.json.puller-bak）/ 代理拉模型列表）
- **锚点跨版本设计**：preload/main 锚点用语义字符串（`exposeInMainWorld("zcode",{`、`SaveMcpToUserDirectory`——后者是 IPC 通道名，非压缩符号），正则捕获周边的压缩别名（electron 导入别名 / ipcMain 包装别名）拼进注入代码——**无需按版本维护符号表**；命中数 ≠1 一律拒绝
- **保存语义（重要）**：整份 config 读出 → 只对不存在的模型 `p.models[mid] = {模板}` → 整份写回。**已有条目原样保留**（手改的 reasoning.variants 安全，比 ZCode 自带设置页保存还会剥 variants 更安全）
- **新供应商一步到位**：保存时按 baseURL（去尾斜杠）匹配供应商，匹配不到再按界面名称兜底，仍匹配不到则**自动创建** provider 条目（UUID id、source:custom、表单里的 baseURL/apiKey），创建后 toast 提示——破解 ZCode 自带流程的死锁（「添加供应商」按钮要求先有 ≥1 个模型，而手填模型 id 正是痛点）。`kind`（API 协议）判定优先读表单「API 格式」下拉的显示文案（3.11.2 实测三选项：`Anthropic Messages (/v1/messages)` → anthropic、`Chat Completions (/chat/completions)` → openai-compatible、`Responses (/responses)` → openai），读不到再按 URL 特征（含 anthropic → anthropic，`/v1` 结尾 → openai-compatible）→ 现有自定义供应商的 kind → openai-compatible。猜错表现为请求协议不对，设置页改 API 格式保存即可
- **新模型模板（元数据感知）**：拉取时读取 `/v1/models` 透出的每模型元数据——`context_length`/`max_input_tokens` → `limit.context`、`max_output_tokens`/`max_completion_tokens` → `limit.output`、`supported_efforts` → `reasoning.variants`（自动前插 off 档）、`default_effort` → `defaultVariant`（workbuddy2api 等网关透出这些字段）；元数据缺失时退回保守模板 `limit 1M/128k + off/high/max`。`zcode.modified: true` 恒有——与 CLI `model_pull.py` 一致
- **还原全外科手术式**：index.html 只摘自己的 tag、preload/main 用正则精确摘除自己注入的字节段（别名通配），**不依赖 sidecar 指纹**——其他补丁重打包改了 asar 大小也能精确还原；TPS 的还原同样已改为外科手术式，两个重打包级补丁任意顺序装/卸互不误伤
- **组件级热更新**：`--check`/注入以「内容比对」判定每个组件是否为现行版本（不能只看 marker——换实现后 marker 不变会漏更新）；不一致的组件自动重注入，一致的原样跳过；注入段形态不符（被手工改过）时拒绝改写并提示
- **备份**：首次注入整包备份 `app.asar.puller.bak` + sidecar `app.asar.puller-patch.json`；与 TPS/chart 的 sidecar 联动同现有机制（重打包后自动同步 chart sidecar 的 offset/指纹）

### 命令行版（不动 asar）

```bash
python scripts/model_pull.py                 # 交互选供应商
python scripts/model_pull.py --all           # 同步全部自定义供应商
python scripts/model_pull.py --provider 关键字 [--dry-run] [--refresh] [--no-reasoning]
python scripts/model_pull.py --test https://api.example.com/v1 [KEY]
```

新模型默认**元数据感知**：网关透出 `context_length/max_output_tokens/supported_efforts/default_effort` 时按模型写实 limit 与思考档位，缺失退回 `1M/128k + off/high/max` 模板（`--no-reasoning` 关闭）。已有条目默认原样保留；**`--refresh` 按服务器元数据刷新已有条目的 limit 与思考档位**（其余键不动，手改过的条目也只动这两处）。注意 CLI 在 ZCode 运行中写 config 有被回写覆盖的竞态，建议退出后跑；注入版无此问题。

### 排障

| 现象 | 处理 |
|---|---|
| 设置页没有按钮 | `--model-puller --check` 看四组件哪个缺；渲染 console 查 `window.__ZCODE_MODEL_PULLER_LOADED_PRO__` |
| 按钮报「通信桥不可用」 | preload 桥缺失（`window.zcode.readConfigFile` 应为函数）；重跑注入会逐组件补齐 |
| 拉取失败 | 供应商 baseURL 不通或鉴权失败；先用 `model_pull.py --test <URL> [KEY]` 单测 |
| 自动创建的供应商协议不对（请求报错/无响应） | kind 猜错（如 OpenAI 兼容端点被判成 anthropic）：设置页打开该供应商，把 API 格式改对保存即可 |
| 想换注入脚本逻辑 | 改 `scripts/zcode-model-puller.js` 或改 preload/main 注入构造器后重跑 `--model-puller` 即可——**四组件（renderer/preload/main/index）均按内容比对**，只更新与现行实现不一致的组件，无需 revert；`--check` 会显示「含旧版组件，重跑可自动更新」 |
| 保存后档位丢失 | 正常不会（整读整写不重建条目）；若用 ZCode 自带设置页保存过该 provider，属其重建路径剥掉 variants，重配 reasoning 即可 |

## 六、思考强度滑条：点击弹出的拖动条（dsh-reasoning-effort 同款）

工具栏常驻「思考 · 档名 ▾」入口（带迷你电量条，造型同原生），点击弹出拖动条面板（260ms 弹出动效：模糊渐显 + 过冲缩放；档名脉冲），拖拽/点击/←→键即换档，点外部或 Esc 收起；**原生「思考级别」下拉经 CSS 隐藏（单档位固定徽章除外），走原生切换链路，会话内即时生效、无需重启**：

```bash
python zcode_patcher.py --thought-slider             # 注入 scripts/zcode-thought-slider.js
python zcode_patcher.py --thought-slider --check     # 查状态
python zcode_patcher.py --thought-slider --revert    # 整体还原
python zcode_patcher.py --thought-slider --slider-src /path/to/zcode-thought-slider.js
```

### 样式规格（1:1 复刻 HanaAyane/dsh-reasoning-effort，MIT）

拖动条 = 32px 行 `.re-effort` + 30px 胶囊 `.re-effort-slider`（`--re-progress` 百分比驱动一切）：
轨道 `.re-effort-track`（深色：暗夜蓝→紫渐变 `linear-gradient(100deg,#03040a…#5d35a0)` + 内高光；
浅色：浅蓝底 `#e5f0ff` + `::before` 进度填充，width = `--re-progress`）+ 特效层 `.re-effort-fx`
（canvas `.re-effort-canvas` 像素辐射 `drawRadiation`：4px 像素格能量柱 + 14 条拖尾粒子 + 旋钮径向辉光，
深色 screen / 浅色 multiply×0.78）+ 拖尾光斑 `.re-effort-flare`（78px 椭圆 + 十字辉光伪元素）+
白色圆形旋钮 `.re-effort-knob`（28px，`clamp(14px,…)` 贴边）。
类名与数值与上游 styles.ts 逐字一致，便于对照；改样式只动 `ensureStyle()`，**不碰任何交互/提交逻辑**。

- **主题映射**：上游 `body[data-ds-dark-theme]` ↔ 本插件 `panel.zs-dark`（打开面板时按
  「应用主题类 → 系统偏好 → 兜底」判一次，与 TPS 同构）；浅色分支即上游
  `body:not([data-ds-dark-theme])` 块逐条移植。canvas 的 isDark 逐帧读 panel 类。
- **状态类（与上游同款）**：`is-dragging`（旋钮 scale 1.07 + 过渡归零跟手、canvas
  saturate/brightness 增辉、粒子加速 2.8×）、`is-busy`（提交中 opacity .72）、
  `is-error`（未知档位描边）、slider `data-top`（顶端时轨道呼吸动画 `re-effort-*-breathe`
  + 旋钮强泛光，深浅各一套 keyframes）。
- **拖拽手感（上游同款）**：raw ∈ [0, n-1] **浮点连续**跟手（旋钮/光斑/canvas/档名预览实时跟随），
  松手 `Math.round` 吸附最近档位提交；提交期间 is-busy，失败回弹原档位并 console.warn。
  ←→/Home/End 直接提交一档（上游 onKeyDown 同款）。
- **无障碍**：`prefers-reduced-motion` 下呼吸动画/跟手过渡全关，canvas 只画一帧（preview 变化补帧）。
- **面板（本插件自有 chrome，非上游）**：宽 `min(236px, 100vw - 24px)`，玻璃渐变 +
  backdrop-filter；标题行「思考强度 + 档名胶囊」；`outline:none` 压掉 `focus()` 的系统色焦点环。
  resize / 滚动按 rAF 节流重新贴合入口。
- **调试接口**：`__zsliderCtl.config`（直接改参数）、`.setThinking(true|false|null)`（手动锁定/恢复自动）、
  `.setSegments(n)`（已废弃，兼容保留）、`.refresh()`、`.state()`、`.diag()`。

### 原理（纯 DOM 观测 + 原生回调，零协议逆向）

1. **读状态**：V4ComposerToolbar 渲染的隐藏 span（`className:"hidden"`）带 `data-thought`（当前档位）、`data-thought-levels`（该模型全部可用档位，逗号分隔）、`data-provider`/`data-model`，React 随会话实时更新——入口与面板 MutationObserver 监听其属性变化自动跟随原生操作（含 `t` 键循环切档），双向同步。
2. **档位动态**：取 `data-thought-levels`（off/minimal/low/medium/high/xhigh/max/ultra 等），几档就映射到 0..n-1 连续轨道，不硬编码；切模型导致档位数变化时自动跟随。
3. **UI**：注入 `<style>` 隐藏原生触发器（`[data-composer-thought-control]:not([data-thought-level-fixed="true"])`，被隐藏的触发器仅作入口插入定位基准，`display:none` 元素的事件派发仍有效，菜单降级不受影响）；特效层 `.re-effort-fx` 自带 `overflow:hidden`，canvas/光斑被圆角裁住；面板主题类 `zs-dark` 在打开时判定一次，主题中途切换需重开面板。
4. **写档位**（按优先级）：
   - React fiber：从触发器 DOM 沿 `__reactFiber$` return 链找 `memoizedProps` 含 `onValueChange` 且 `option.type==='select'` 带数组选项的组件，直调之——等价于用户点选菜单项，原生继续走 `session/setThoughtLevel` 会话 RPC；
   - 降级：模拟点击触发器打开 Radix 菜单，按 options 顺序点第 index 个 `[role="option"]`（档位显示名是 i18n 文案，按序号而非文本定位）。
5. **注入**：与 TPS 同链路（index.html `</body>` 前挂 `<script>` + 新增脚本条目，整体重打包），sidecar `app.asar.slider-patch.json`、备份 `app.asar.slider.bak`，外科手术式还原只摘自己的 tag。
6. **隐藏条件**：模型无思考档位（`data-thought-levels` 空）、探针未命中、原生触发器不存在——均自动隐藏，不占空间。
7. **生成中判定**（仅记录到 `state().thinking` 与 track 的 `data-thinking` 供诊断，新版样式无「思考中流动」态；选择器来自 3.14.3 renderer bundle 实证，非猜测）：① 停止按钮在场——客户端把「停止生成」与「发送」做成同一按钮位的**互斥渲染**，`aria-label` 取 i18n `chat.stop`（中文「停止生成」/ 英文「Stop generating」）；② `[data-v4-running-live-tail]`（正在跑的轮次容器）；③ `[data-reasoning-streaming-line]`（更窄，仅推理流期间）。命中结果 250ms 复用，避免流式期间反复强制布局；可用 `__zsliderCtl.setThinking(true|false|null)` 手动锁定/恢复自动。

### 验证 / 排障

- 渲染 console 查 `[zslider] 已就绪: low/medium/high/max 当前 max`（加载 5s 后自检）；`window.__zsliderCtl.state()` 看档位 / 预览值 / 思考态快照。
- 拖拽后原生下拉状态同步变化（入口档名/电量条、`t` 键联动）= fiber 路径生效；console 出现 `[zslider] 档位提交失败` = fiber 与菜单降级均未命中（版本结构大改，需按「原理」重新对锚点），此时滑条会自动回弹原档位。
- `__zsliderCtl.state().thinking` 用于确认生成中判定（新版本改了 i18n 或按钮结构就要补 `THINK_SELECTORS`）；该字段不影响拖动条样式。
- 入口不出现：先看探针——`document.querySelector('[data-thought][data-thought-levels]')` 是否有值；当前模型未配思考档位时不显示属预期。

## 七、增强提示词：「润色」按钮、右键选模型与跨机「Model is unavailable」

输入框工具栏一键润色草稿（图标 3 态：✦ 待机 / ◌ 增强中 / ↺ 可还原 20s）。

```bash
python zcode_patcher.py --enhance-prompt            # 注入（renderer 脚本 + index.html + preload 桥 + main IPC）
python zcode_patcher.py --enhance-prompt --check
python zcode_patcher.py --enhance-prompt --revert
```

**左键**润色；**右键**弹出模型选择菜单（按供应商分组列出所有可用模型，选中即持久化，
之后每次润色都用它，零重启生效）—— 见下文「右键菜单」一节。

链路：渲染层按钮 → preload 桥 `window.zcode.enhancePrompt(text, modelValue, modelLabel, override)`
→ main handler `zcode:enhance-prompt`（`_ENHANCE_HANDLER`）读**供应商配置**解析 → 直接 POST 补全接口。
菜单另走两条桥：`listEnhanceModels()`（复用同一通道的 `{list:true}` 分支取清单）与
`saveEnhanceModel({providerId, modelId})` → main handler `zcode:enhance-model-save`（写配置文件）。

### ★★ 关键前提：客户端发请求用的是 `provider_config.json`，不是 `config.json`（0.5.10 修复）

**两份配置文件并存、语义不同**（同一个数据根下）：

| 文件 | 角色 | 说明 |
|---|---|---|
| `~/.zcode/v2/provider_config.json` | **权威源** | 客户端真正据此构造请求。结构：`providerConfigRules.providerRules[]`（`providerId` / `providerName` / `config.access.apiKey` / `config.api.baseUrl` / `config.api.type` / `config.personalModelIds`）+ `modelConfigRules.providerModelRules[]` |
| `~/.zcode/v2/config.json` | **遗留副本** | 旧格式（`provider.options.baseURL/apiKey/models`）。可能滞后：供应商缺失、apiKey 过期、模型列表旧 |

客户端 bundle 里的硬证据：`ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` 环境变量、
常量 `MXe="provider_config.json"`、以及
`a=e.personalProviderConfigPath??e.env[tte]?.trim()??join(dirname(credentialStore.filePath),MXe)`。
即**默认就是读 `provider_config.json`**。

> 本机实测两份不一致：`config.json` 里某供应商只有 `['glm-5.2']` 且 apiKey 是旧的，
> `provider_config.json` 里有 8 个模型（含 `glm-5.3`）且 apiKey 不同；
> 另有供应商（如 `openai`、`590ce4cc-…`）**只存在于 `provider_config.json`**。

**0.5.9 的残留缺陷**：0.5.9 修好了「兜底档无视界面选择」，但 handler 仍只读 `config.json`。
于是当界面选中的是只在 `provider_config.json` 里存在的供应商/模型时，
ref 档必然查不到 → 掉进兜底档 → 打到别的供应商 → `HTTP 400 Model is unavailable`。

**修复（0.5.10）**：handler 改为 **`provider_config.json` 优先、`config.json` 仅补缺**，
把两份归一化成统一候选表（去重，保留权威源同名条目），再走档位解析。
这样「界面选中的 provider/model」与「handler 读到的」来自**同一个文件**，从根上对齐。

### ★ 已知坑：请求打到不相干的供应商上（0.5.9 起修复）

**症状**：本机润色正常，别的电脑报
`HTTP 400：Upstream request failed: Model is unavailable.`，或部分机器显示
「已用 glm-5.2 增强」却成功。

**根因（两层）**：
1. **0.5.9 之前**：模型解析的兜底档**无视界面选择**——只要 ref / label 两档没命中，
   就把请求发给「第一个带 baseURL 的自定义供应商的首个模型」，且**不校验该供应商是否可用**
   （`apiKey` 为空、`systemDisabledReason` 存在都照样发）。
   不同机器 `provider` 的**插入顺序不同** → 兜底落到不同供应商 → 有的机器撞对了就能用。
2. **0.5.10 之前**：读错了配置文件（见上一节），界面所选模型在旧副本里根本查不到。

**为什么本机能用纯属巧合**：本机 `provider` 里前面几条是 `builtin:*`（被旧逻辑跳过），
兜底恰好落到一个有效供应商上。

**修复（0.5.9）**：各档解析全部经 `usable(pp)` 判定（`baseURL` + `apiKey` + 无
`systemDisabledReason`）；新增 `ref-label` 档；`label` 档放开 `builtin:` 限制；
兜底只选真正可用的。拿不出可用候选时返回 `code:"no-model"` + 可读原因，不再构造注定失败的请求。
**修复（0.5.10）**：权威源改为 `provider_config.json`；兜底候选排序时把
`builtin:` / `account:` 前缀的供应商**排到最后**（用户自定义供应商优先）。

### 六档解析顺序（0.6.7 起）

| 档 | 来源 | 说明 |
|---|---|---|
| **explicit** | 右键菜单**本次**选中的模型（`override` 参数） | 最高优先级。只校验供应商可用性，不校验模型表 |
| **config** | `<数据根>/enhance_config.json` 的 `providerId`/`modelId` | 右键菜单持久化的选择也写在这里；手改同效。每次点击热读，**零重启** |
| **ref** | 界面 `data-model-current-value` 的 `${providerId}/${modelId}` | 精确命中 |
| **ref-label** | ref 的 provider 段命中，模型改用界面显示名匹配 | |
| **label** | 全部候选里按显示名匹配 | |
| **fallback** | 排序后的第一个可用候选（`builtin:` / `account:` 排最后） | 跨机差异的高发区 |

> **★ 每档都必须带 `!pick` 守卫**：新增更高优先级档时，必须给**后面每一个** `pick=` 赋值
> 补上 `!pick`，否则后面的档会静默覆盖它 —— 语法对、位置对、行为全静默。
> 这个坑踩过两次（PR#1 的 config 档、0.6.7 的 explicit 档），现已由
> `tests/test_enhance_handler.py::TestHotConfigSourceInvariants` 的源码顺序断言钉死。

`how` 会回填到返回值（`window.__zenhanceDiag.lastResult.how`）：
**只有 `explicit` / `config` / `ref` 才代表「用上了你指定的模型」**；`ref-label` / `label` /
`fallback` 都值得怀疑。

### ★ 右键菜单：选择润色使用的模型（0.6.7）

**用法**：在润色按钮上**右键** → 菜单按供应商分组列出所有**可用**模型（`usable()`：有
baseURL + 有 apiKey + 无 `systemDisabledReason`）→ 点选某项即生效。
菜单顶部有「跟随界面选择（默认）」用于清除选择（等价于删掉配置里的 `providerId`/`modelId`）。

**为什么"选了就生效"**：一次选择同时走两条路，互为兜底 ——

1. `saveEnhanceModel()` → `zcode:enhance-model-save` → 写进 `enhance_config.json`
   （= **config 档**，handler 每次点击热读）→ **零重启、重启后依然沿用**；
2. 本次点击把选择作为 `override` 参数传给 `enhancePrompt()` → **explicit 档**（最高优先级）
   → 即使文件写入失败或被别处改动，这一次点击也一定用它。

**设计要点**：

- 菜单清单走 `listEnhanceModels()`，它在主进程里**复用 enhance-prompt 的同一份候选表**
  （`{list:true}` 分支，不发任何补全请求）。这样「菜单里能选的」与「发请求时能用的」
  **永远同源**，不会出现两套逻辑漂移导致的「菜单里能选、点了报不可用」。
- 菜单是挂在 `document.body` 上的 **fixed 浮层**，**不进 composer 子树** —— 既不会被
  虚拟列表 / 重渲染搬走，也不参与输入框布局（不会再把图标位置搞乱）。
- `zcode:enhance-model-save` **只增删 `providerId`/`modelId` 两个键**，
  `maxTokens` / `temperature` / 思考强度等参数原样保留 —— 选个模型不该抹掉你调好的参数。
- 写失败会**显式报错**（toast），不静默吞掉：否则用户以为选好了，下次点击仍走旧档位。
- **关闭策略**（0.6.8 修「菜单一出现就自动关闭」）：只有 ①点菜单外部 ②按 Esc ③再右键收起
  三种情况会关。**滚动不关闭**，改为节流（80ms）跟随重定位 —— 客户端消息区是虚拟列表、
  输入区 sticky，滚动极频繁，把 `scroll` 当关闭信号等于菜单刚挂上就被关掉；
  点击关闭带 **350ms 保护期**，防止触发「打开」的那串事件（右键 mousedown/mouseup、
  触控板多出来的事件）在监听器注册之后到达而误关；按钮被重渲染摘掉时**不立刻关**
  （4 秒宽限 + 位置缓存），避免 composer 抖动误伤。

**排障**：

```bash
python enhance_doctor.py --override-provider <id> --override-model <model>   # 模拟菜单选择
```

第 2c 节会直接显示 `enhance_config.json` 当前指定的模型（以及它是否在压过界面选择），
第 3 节显示最终命中的档位。DevTools 里 `window.__zenhanceDiag.overrideModel` 是当前会话内
选中的模型，`lastRequest.override` 是实际随请求发出的值；
`menuOpens` / `menuClosedBy` 记录菜单开合次数与**最后一次的关闭原因**
（`outside` 点外部 / `escape` / `toggle` 再右键 / `choose` 选完 / `btn-gone` 按钮长期不在）
—— 菜单「莫名消失」时看它就知道是谁关的。

### 错误归因与重试（0.5.9）

`classify(code, status, msg)` 把失败归成
`model / quota / auth / path / rate / server / timeout / network / bad-request`，
并回传 `tip`，前端展示成「增强失败：…」+「→ 可执行建议」。

| 类别 | 是否重试 |
|---|---|
| `model` / `auth` / `quota` / `bad-request` | **否**，立刻停 |
| `rate` | 退避 1600ms，原地重试 1 次 |
| `server` / `timeout` / `network` | 退避 600ms，原地重试 1 次 |
| `path`(404) | 换候选地址（`/v1` 两种拼法） |

### 排查（只读工具）

```bash
python "<skill目录>/scripts/enhance_doctor.py"                 # 解析链路体检
python enhance_doctor.py --json                                # 机器可读（含 resolve/provider/request/problems）
python enhance_doctor.py --probe                               # 加真实连通性探测（耗极少额度）
python enhance_doctor.py --probe --model-value "builtin:zai-coding-plan/GLM-5.2"
```

**它逐段复刻 handler 的解析逻辑**（同一套「provider_config.json 优先 + 六档」），
所以「脚本判定用哪个供应商」= 「按钮实际会用哪个」。支持
`--model-value`（界面 ref，形如 `providerId/modelId`）/ `--model-label`（界面显示名）
模拟界面选择，`--override-provider` + `--override-model` 模拟右键菜单的 explicit 档；
不给就演示最坏情况。
输出含解析路径（`explicit`/`config`/`ref`/`ref-label`/`label`/`fallback`）、关键字段、
请求 URL / `max_tokens`、**两份配置的差异审计**（`2b` 节）与
**热配置当前值**（`2c` 节，即右键菜单写入的选择）。
退出码 0 = 无阻断，1 = 发现会导致失败的问题。

> **判读要点**：只有 `how=explicit` / `config` / `ref` 才代表「用上了你指定的模型」。
> `fallback` = 打到了别的供应商（正是跨机差异的症状）；
> `config` 但模型不是你想要的 = 菜单/配置文件里残留了旧选择（看 `2c` 节）。

界面侧自诊断：DevTools（`Ctrl+Shift+I`）里看 `window.__zenhanceDiag`——
`lastRequest.modelValue`（空 = ref 通道失效）、`modelCandidates`（>1 = 页面有多个模型节点）、
`lastResult.code`、`hiddenReason`（按钮没挂上时的原因）。

### ★ 已知坑：按钮「跑出输入框」渲染进消息区（0.5.11 修复）

**症状**：多轮对话 / 消息列表增长时，润色按钮出现在会话消息区域里
（有的表现为「输入框旁边的按钮突然不见，消息流中间多出一个图标」）。

**根因**：`zcode-enhance-prompt.js` 的 `findInput()` 在**整个 `document`** 上按
`COMPOSER_INPUT_SELECTORS` 查找 —— 其中 `textarea` / `[contenteditable='true']` /
`form textarea` 是**通用选择器**。而客户端的消息区里也可能出现这类节点，于是：

1. 消息区元素被误认成「交互输入框」（读写正文也跟着错）；
2. 用它反推挂载点（`input.parentElement` / `card.querySelector("div")`）→
   按钮被插进消息流 → 视觉上「按钮跑出输入框」。

放大该问题的是客户端的真实 DOM 结构（`out/renderer/assets/styles-*.js` 实证）：

```
div[data-v4-timeline-scroll]          ← overflow-y-auto，真正的滚动宿主
└─ div                                 ← flex min-h-full flex-col
   ├─ div[data-v4-timeline-message-layer]      ← 消息层
   │  ├─ div[data-v4-timeline-header-slot]
   │  ├─ div[data-v4-timeline-virtual-history] ← 虚拟列表（absolute + translateY）
   │  ├─ div[data-v4-running-live-tail]
   │  └─ div[data-v4-timeline-content-column]
   └─ div[data-v4-composer-dock]        ← ★ 输入框 dock，与消息层**同级兄弟**
      └─ div[data-v4-composer-dock-content]
         └─ div[data-v4-back-to-bottom-anchor="composer-dock"]
            └─ … composer 卡片（含 v4-composer-send 工具栏行）
```

关键点：**dock 和消息层都在滚动容器内部**，dock 仅靠
`` K ? `mt-3 shrink-0` : oe ? `shrink-0` : `sticky bottom-0` `` 贴底。
一旦按钮被插进消息流那一侧，它就**不再受 dock 的 sticky 约束**，
随消息增长被一路推到列表底部 —— 这正是「多轮对话后才明显」的原因。

**修复（0.5.11）**：

1. 新增 `findDock()`：按 `[data-v4-composer-dock='true']` / `[data-v4-composer]`
   定位 composer dock（可见优先 + 最靠下，兼容多会话场景）。
2. `findInput()` 改为**两段式**：
   - ① 严格模式：只在 **dock 内**子查询；
   - ② 兼容模式：dock 不存在才退回全局，且**跳过** `textarea` /
     `[contenteditable='true']` / `form textarea` 这些会误伤消息区的通用选择器。
3. `ensureButton()` 的挂载点收敛到 dock 内：
   发送按钮的父节点 → dock 内的 toolbar/卡片 → 输入框向上爬到 dock 为止；
   最后一道校验 `if (host && dock && !dock.contains(host)) host = dock;`
   —— **越界就回退到 dock 本体，宁可不挂也不渲染进消息区**。
4. 位置自愈：已连接的按钮若 `btn.parentElement !== host` 或已脱离 dock，
   主动 `insertBefore` 搬回，并累加 `diag.reattaches`。
5. 轮询 2000ms → **1000ms**（流式输出时消息持续增长，周期过长会肉眼可见地错位）。

自诊断新增字段：`window.__zenhanceDiag.reattaches`（>0 说明发生过自愈搬迁，
可用于确认线上是否仍在错位）。

### ★ 已知坑：图标跑到输入框**左上角**（1.4 / 0.6.6 修复）

**症状**：润色图标不在工具栏右侧，而是贴在**输入框左上角**。两个可复现的时机：
① 输入框为空时；② 会话进行中（生成过程中）。正常对话（有内容、未生成）时位置是对的。

**根因**：挂载点解析的**兜底链会落到「输入框的祖先」上**，而 `insertBefore(host, host.firstChild)`
正好是那个容器的左上角。旧实现：

```js
// 旧（有 bug）：只认发送按钮；取不到就一路退到卡片/dock 本体
const send = document.querySelector("[data-testid='v4-composer-send']");
if (send && send.parentElement) host = send.parentElement;
if (!host && dock) {
  host = dock.querySelector("[data-testid*='composer-toolbar']")   // ← 选择器恒不命中（实测 0 个）
      || dock.querySelector("[data-testid='v4-composer']")         // ← 卡片 = 输入框所在区域
      || dock.querySelector("[data-v4-composer-dock-content]")
      || dock;                                                     // ← 最坏 = 整个 dock
}
if (host && dock && !dock.contains(host)) host = dock;
```

三个致命点：

1. **`[data-testid*='composer-toolbar']` 是死选择器**——在 3.14.3 的 asar 里命中数为 **0**
   （工具栏行只有 class `group/toolbar flex items-end gap-3`，没有 testid），所以它永远落到下一档；
2. **下一档 `[data-testid='v4-composer']` 就是卡片本身**，它是输入框的**祖先**；
   `insertBefore(btn, card.firstChild)` = 图标钉在输入框左上角；
3. **发送按钮不是恒定锚点**。内核里提交控件是
   `sn = canStop && !hasContent ? 停止按钮 : 发送按钮`
   （`sn = $t && !tn`，`$t = !!e?.control.canStop`，`tn` = 输入框/附件/上下文有无内容）——
   **输入框为空 + 会话进行中**时，`[data-testid='v4-composer-send']` 被
   `[data-testid='v4-stop']` **替换**，`querySelector` 直接返回 null → 触发上面的兜底 → 左上角。

所以「输入框为空」与「会话进行中」其实是**同一个内核条件**的两种描述：生成中清空输入框，
就同时满足两者。（输入框为空但**未**生成时发送按钮只是 `disabled`，仍在 DOM 里，
所以那个瞬间位置是对的——这也解释了为什么它看起来像"偶发"。）

**修复（1.4）**：改成三级解析，任何一级都不退回卡片 / dock 本体：

| 级 | 锚点 | 落位 |
|---|---|---|
| ① | `card.querySelector("[data-composer-trailing-actions]")` —— 工具栏**右侧操作区**，跨状态恒存在 | prepend（图标在模型胶囊/发送按钮左侧 = 既有正确位置） |
| ② | `[data-testid='v4-composer-send']` / `[data-testid='v4-stop']` 的父节点（两者同属一个提交控件容器） | prepend |
| ③ | 工具栏行（class 含 `flex` + `items-end`，与 TPS 状态栏同一套结构判定） | **append**（贴行尾；绝不插行首） |

解析全失败时**保持原位、不搬迁**（旧行为是搬到左上角），并在
`window.__zenhanceDiag.hiddenReason = "未找到工具栏操作区"` 里说明。

真实 DOM 结构（3.14.3 实证，也是这次定位的依据）：

```
div[data-testid='v4-composer']                  ← 卡片 = .chat-composer-region（输入框所在区域）
 └─ div.chat-composer-input-surface
    ├─ div[data-testid='v4-composer-input']     ← 输入框
    └─ div.group/toolbar.flex.items-end.gap-3   ← 工具栏行（无 testid！）
       ├─ div[data-composer-leading-actions]        ← 左侧（+ / 附件）
       └─ div[data-composer-trailing-actions]       ← ★ 右侧操作区（恒存在）
          └─ div.flex.min-w-0.items-center.gap-1    ← 提交控件容器
             ├─ span …                                ← 模型胶囊
             └─ button[data-testid='v4-composer-send']（生成中且无内容时 → 'v4-stop'）
```

自诊断新增字段：`window.__zenhanceDiag.mountWhere`（`prepend` / `append`，看走的是哪一级）。

回归测试：`tests/enhance_mount_smoke.js`（最小 DOM 桩，**真跑** 6 个状态：待机 / 输入框为空 /
**发送按钮被停止按钮替换** / 锚点全缺失 / 连操作区都没有 / 自愈 + 幂等），
Python 侧 `tests/test_patcher.py::TestEnhancePromptScript`
（含源码级不变量：不得出现 `host = dock;`、必须同时认 `v4-composer-send` 与 `v4-stop`）。
负向验证：把脚本回退到修复前，冒烟测试立刻红在「会话进行中 → CARD(top-left!)」。

### 排障速查

| 现象 | 处理 |
|---|---|
| ★ **要「重启两遍」才生效 / 退出重启后仍不生效** | ★ 写入时机在**退出**不在启动（`启动①补丁→退出时写→自动拉起→启动②生效`）。第一次多半**没退干净**：点 × 只是最小化到托盘，且 ZCode 多进程会残留 `ZCode.exe` → `zcode_running()` 恒真 → 看护一直等（最长 24h）**永不写入**。查 `doctor.py` **第 7.5 节**：`看护启动 N 次 / 等到退出 0 次` 即中。**彻底退出**（托盘右键退出 + 任务管理器确认无残留），或完全退出后手动 `--all`。详见「为什么有时要重启两遍」 |
| ★ **更新插件 + 退出重启后仍不生效** | ★ 插件市场「更新」只换插件目录，**不重新注入 app.asar**；而 ≤0.6.0 的同步脚本看到 `--check` 输出里的「已打」就判成 `on` → 永远跳过重跑 → 旧片段永远留在 asar 里，**且不报任何错**。判定：`--enhance-prompt --check` 出现「含旧版组件」即是。**0.6.1+ 已自动识别（新 `stale` 态）**；旧版手动 `--all`。详见「插件更新≠补丁更新」 |
| 报 `Model is unavailable` | ★ 解析没命中界面所选模型。跑 `enhance_doctor.py --model-value "<providerId>/<modelId>"` 看 `how=`；`ref` 之外都要查：① 选中供应商是否只在 `provider_config.json` 里（旧版 handler 读的是 `config.json`，见上文 0.5.10）；② 该供应商是否被 `systemDisabledReason` 禁用 |
| 按钮跑到消息区 / 看不见 | ★ 挂载点跑出了 composer dock。升级到 **0.5.11+**；确认注入也是新版（`--dry-run`）。诊断看 `window.__zenhanceDiag.reattaches`（>0 = 发生过自愈）与 `hiddenReason` |
| ★ **图标跑到输入框左上角**（输入框为空 / 会话进行中时） | ★ 挂载点兜底落到了「输入框的祖先」（卡片 / dock），`insertBefore(firstChild)` 正好是左上角。升级到 **0.6.6+**（脚本 `scriptVersion 1.4`）；确认注入也是新版（`--enhance-prompt --dry-run`）。诊断看 `window.__zenhanceDiag.mountWhere`（应为 `prepend`）与 `scriptVersion` |
| 升级到 0.5.10 后仍报错 | 确认**注入**也升到了新版：`--enhance-prompt --dry-run` 看是否有「将热更新…（X → Y 字节）」。`--check` 只看挂载标记，不比对脚本内容 |
| 报「通信桥不可用」 | preload 桥缺失；重跑 `--enhance-prompt` 注入后**重启** ZCode |
| 按钮不出现 | `window.__zenhanceDiag.hiddenReason`；找不到输入框 / 找不到工具栏行 |
| 报 `no-model` | 没有可用候选（全表缺 key/baseURL 或被禁用），按提示补配置 |
| 报 `no-key` / `no-baseurl` | 命中供应商缺凭据；`enhance_doctor.py` 第 4 节直接列出 |
| 本机能用、别人不能 | 典型兜底档差异：对比两台机器的 `enhance_doctor.py` 输出中的 `how` 与 `providerId` |

> 注意 `Upstream request failed` 是 vercel-ai 网关的错误措辞（对应
> `GatewayModelNotFoundError`），属**模型维度**判定（不在套餐内 / 已下线），
> **与地区限制、代理无关**。地域封锁表现为 403 或连接重置。

### ★★ 插件更新 ≠ 补丁更新：`stale` 态为什么必须独立存在（0.6.1 修复）

**跨机复现的真实故障**：用户在另一台电脑从插件市场「更新」插件 → 按提示退出并重启
→ 润色新修的功能（0.5.11 的按钮定位）**仍然不生效**；跑 `doctor.py` 一路全绿。

链路是一条**全静默**的失效：

1. 插件市场「更新」只替换**插件目录**（`cache/<市场>/<插件名>/<版本>/`），
   **不重新注入 `app.asar`** —— asar 里还是上一版的注入片段；
2. `zcode_patcher.py --check` 会明确给出两种措辞：
   `已打（含旧版组件，重跑可自动更新）` / `已打（四组件均为当前版本）`；
3. 但 ≤0.6.0 的 `sync.check_state()` 只做子串匹配：
   ```python
   if "未打" in out: return "off"
   if "已打" in out: return "on"     # ← 两种措辞都含「已打」，全被判成 on
   ```
4. `run_sync()` 拿到 `want=True, state=on` → `continue`（视为已一致）
   → **永远不会重跑注入**；
5. 结果：心跳正常、日志干净、`doctor` 全绿、`--check` 也显示「已打」，
   唯一表现是「修复没生效」——**没有任何一处会报错**。

**修复（0.6.1）**：`check_state()` 新增第四态 `stale`，把「已打但内容旧」单独分出来。

* **判断顺序是硬性要求**：`if "含旧版组件" in out: return "stale"`
  **必须排在** `if "已打" in out` **之前** —— 前者本身含「已打」，顺序反了
  `stale` 分支永远走不到。已有两条回归测试分别从**语义**（喂字符串）和
  **源码顺序**（`inspect.getsource`）两个角度钉死它。
* `run_sync()` 对 `stale`：重打包级补丁转交看护并报
  「插件已更新，客户端退出时重注入为新版」；非重打包级**当场重跑**；
  若开关是关的直接还原（不必先更新再还原绕一圈）。
* `doctor.py` 第 8 节末尾会点名「有 N 项注入的是**旧版片段**」。

**给用户的一句话**：重打包级功能（TPS 状态栏 / 滑条 / 润色 / 拉取按钮）在插件更新后，
仍要等**下一次完全退出 ZCode** 才由看护重新写入 —— 光重启不够，
因为「重启」期间看护会被先起来的 ZCode 挡住（它等的是**退出**）。

### ★★ 为什么有时要「重启两遍」才生效（0.6.2 新增第 7.5 节）

**用户报的原话**：「重启了两遍后润色功能就可以正常工作了」。
这不是玄学，也不是缓存/服务需要两轮预热 —— **写入时机在退出，不在启动**：

```
启动① → 钩子跑 sync → 有待办 → 挂看护 W（W 在 while zcode_running(): sleep(3) 里等）
退出  → W 醒来 → 写 app.asar → 主动拉起 ZCode
启动② → 新 asar 生效 ✓          ← 真正起作用的是这第二个启动
```

所以「一次重启」在本工具里就等于「退出 + 自动拉起」两个动作；
用户口中的「重启两遍」往往其实是**第二次退出才真的退干净**。

**真正的故障点：第一次退出没退干净。**

* 点窗口 × 是**最小化到托盘**，进程还在；
* ZCode 是**多进程**（主进程 / 渲染 / GPU / 工具进程），主窗口关了常留
  若干 `ZCode.exe` 子进程（本机实测残留过 14 个）。

`zcode_running()` 只判「`tasklist` 里有无 `ZCode.exe`」，有残留即为真 →
看护一直等（`MAX_WAIT_SEC = 24h`）→ **永不写入** → 用户必须再来一轮。

**新增的可观测性（doctor 第 7.5 节）**：`_apply_after_exit.log` 是唯一判据。

| 日志内容 | 含义 |
|---|---|
| `看护启动，等待 ZCode 退出…` | 看护起来了（**仅此不代表写入了**） |
| `ZCode 已退出（等待 Ns），开始处理` | 等到了退出 → 真正开始写 |
| `DONE` | 写完并已（尝试）重启 |
| `检测到 ZCode 已再次运行，跳过重启` | 写入成功，只是被别的原因抢先拉起（无害） |
| `等待超时（24h），放弃` | 等了 24 小时 |

第 7.5 节算 `pending = 启动数 - 等到退出数 - 超时数`；`pending > 0` 就直说
「N 个看护起来了却从未等到退出 = 补丁没写进去 = 必须重启两遍的直接原因」，
并给出托盘退出 + `tasklist` 确认残留的指引。
**没有这条输出时，用户能看到的全部信息就是「我重启了却不生效」**，
排查会跑偏到模型配置 / 缓存 / 网络上。

回归测试 `TestDoctorWatchdogSection`（7 条）钉死：报警文案、正常完成**不**报警、
无日志不算故障、24h 超时**不**重复计入 pending、「跳过重启」要说明无害、
第 7.5 节确实被 `main()` 调用且顺序在心跳之后 / 补丁状态之前、
以及 `apply_after_exit.py` 的五个日志措辞子串（被 doctor 当接口消费，不能随手改）。

**给用户的一句话**：要生效请**彻底退出** —— 托盘右键退出，并在任务管理器确认
没有 `ZCode.exe` 残留；拿不准就完全退出后手动跑一次
`python "<脚本目录>/zcode_patcher.py" --all`，不依赖看护时序。



### 连带修复：Windows 瞬时占用写不进补丁

`app.asar` 被杀软/索引器/未释放句柄短暂占用时，裸 `os.replace` 直接
`WinError 5 拒绝访问`。新增 `_replace_with_retry()`：仅对 `WinError 5 / 32`
做 0.25→0.5→1→2s 退避（共 5 次），其他错误立即抛出。
**后续新增任何对 asar / 配置的原子替换都应走它。**

## ★★ asar「布局错位」＝客户端打不开，且毫无日志（0.6.5 修复）

**症状**：注入后 ZCode **双击无反应**（无窗口、无错误弹窗、无崩溃记录）。
`--enable-logging=stderr` 跑它也**什么都不打印**，只是退出码 1。Windows 事件日志、
`CrashDumps`、ZCode 自己的日志目录全都是干净的。

**根因**：`app.asar` 的**数据区排布与 header 声明错位**。典型形态是：

```
header 里 out/main/index.js 声明 size = 760,736（注入后）
数据区实际只按 758,743（注入前）递增 —— 之后每个条目的 offset 都少 1,993
但每个条目自己的 integrity 记录还是【旧内容的哈希】
```

于是 4,138 个条目的 `integrity` 与实际内容不符。**asar 结构本身完全自洽**：
条目数对、offset 连续、零重叠、零越界、最后一个条目的 end 正好等于文件大小。
**所以结构校验全绿，问题被彻底掩盖。**

Electron 加载时按 `integrity` 校验，不匹配就**拒绝加载该模块**；主进程
`out/main/index.js` 的依赖链一断，进程在能够打印任何日志之前就退出了。

**为什么结构校验看不出来**：`offset` 与 `size` 自洽只保证「数据区没有空洞/重叠」，
不保证「offset 指向的是它自己那份内容」。**只有逐条重算哈希才能发现。**

**规矩**：
1. **回读校验必须覆盖「全部条目」，不能只校验本次改动的条目。** 旧实现只比对
   `overwrite` 里的条目 —— 而未改动条目恰恰是最容易静默损坏的那批。
   现在 `_repack_asar` 落盘前会逐条重算 sha256 与 `integrity` 记录比对，
   任一条不符即抛错拒绝落盘，并报出「声明 offset/size + 拒绝落盘」。
2. **同一 asar 的写入必须互斥。** 看护与手动流程并发时会各自读一份 header、
   各自算 offset，交错落盘 → 直接产出上面的错位文件。新增 `_AsarWriteLock`
   （进程内 `threading.Lock` + 跨进程 `msvcrt.locking` 文件锁 `<asar>.zp-lock`）
   覆盖整个「读 header → 排布 → 写数据 → 原子替换」。
3. **怀疑 asar 不干净时，跑全域体检**（不用猜）：
   `python .workbuddy-ai/tmp/find_integrity_mismatch.py` —— 它会报出全部失配条目
   与首个失配项。**首个失配条目往往就紧跟在注入点之后**，一眼看出错位基准。
4. **事故恢复**：直接换回 `app.asar.<补丁名>.bak`（写入前的备份，integrity 自洽），
   换完立刻再跑一次全域体检确认为 0。

> 排查心法：这次绕了很久，是因为「结构校验通过」就排除了 asar，转而去查 Electron
> 自动更新、`NODE_OPTIONS` 污染、崩溃日志…… **当客户端「静默不启动」时，
> 先做一次全域 integrity 体检，再考虑其它方向。**

## ★★ 给 if 链插「最高优先级」分支时，必须给后续分支补守卫（0.6.4）

`_ENHANCE_HANDLER` 的模型解析是一条 `if` 链，靠**顺序**表达优先级：
⓪热配置 → ①ref → ②ref-label → ③label → ④兜底。

外部贡献的 PR 在链首插入 ⓪档（`enhance_config.json` 热配置），但**忘了**给紧随其后的
①档补 `!pick` 守卫 —— ①档写的是 `if(mv){...pick=...}`，于是：

```
⓪ 设 pick=config:model-a
① 只要界面取值 mv 非空就重跑 → pick=ref:model-b   ← 覆盖掉了
```

**最讽刺的一点**：同一个 PR 的 `currentModel()` 修复**正是让界面取值变得可读**的，
所以这个 bug 让新功能「只在它修好的那个前提下失效」。排查时 `tried` 轨迹显示
`config:P-A/model-a` 已被记录，请求却发给了 `P-B/model-b` —— 极具误导性。

**规矩**：
1. 在 if 链**前面**插分支时，检查**后面每一个 `pick=` 赋值**是否都带 `!pick`；
   ②③ 两档本来就有，① 没有 → 补齐后语义统一为「先到先得」。
2. 这类「语法对、位置错」的 bug **行为上完全静默**，只做行为测试可能漏
   （若两档指向同一模型就测不出来）→ 源码级测试用 `assertRegex` 钉住
   `if\(!pick\s*&&\s*mv\)`，并单独测「⓪ 必须压过 ①」这个语义。
3. 顺手修了 `how` 恒为空串（声明后从未赋值）：新文档教用户看 `lastResult.how`
   判断走了哪一档，该字段不生效等于**文档教的排障法失效**。
   **教训：新增「可观测性字段」时要确认它真的被写入了** —— 空值不会报错。

## ★★ 通用教训：别把「输出字符串」当状态接口（0.6.3 修复三处）

0.6.1 / 0.6.2 / 0.6.3 三个版本**反复**踩在同一类根因上，所以单独拎出来当规矩讲。

**病根**：用「子串匹配别人打印的文字」来判断程序状态。它必然会在某天失效，因为
*文案会改*、*同一个词会出现在两种相反的状态里*、*正常的流程也会打印警示语*。
失效时**不报错**：日志干净、心跳正常、退出码 0，只有结论是错的。

### 三个实例（全部已修 + 已钉测试）

| 实例 | 失效方式 | 为什么只有子串匹配会错 |
|---|---|---|
| `check_state()` 判 `stale` | 见[上一节](#插件更新--补丁更新stale-态为什么必须独立存在061-修复) | `已打（含旧版组件…）` 与 `已打（四组件均为当前版本）` **都含「已打」** |
| `run_patcher()` 判失败 | 良性警告被当成失败 | `zcode_patcher.py` 有 **60+ 处** `[!]`，多处在**成功路径**上（如「发现 N 个模型同时存在于两张规则表」——**事先就存在**的问题，脚本只报告不修） |
| `declared_defaults()` 找清单 | 找到**别人**的清单 | 原先无条件向上找 2~6 层 → 仓库布局下会到**盘符根**（`F:\`） |

### 三条硬规矩（新增同类代码时照着做）

1. **优先找结构化信号**：退出码、汇总计数、独立的 marker 行。
   `run_patcher()` 现在解析 `合计 N 项：成功 X，失败 Y` 里的 Y ——
   那是脚本**自己算出来的结论**，比任何特征串都可靠。
2. **特征串必须是「只在该状态出现」的**。判断前先问：这个字眼会不会同时出现在
   正常流程里？（`[!]` 就是反面教材：它同时是「警示」和「失败」两个意思。）
3. **顺序敏感的判断要单独钉测试**。`stale` 之所以必须排在裸 `已打` 之前，
   是因为前者**包含**后者 —— 这种「包含关系导致的顺序依赖」光看代码看不出来，
   要用 `inspect.getsource` 查源码顺序。

### 无人值守路径必须带超时

同一批修复里还有一类：`subprocess.run` 不写 `timeout=` 时可能**永久挂起**，
而用户只看到「卡住不动」。无人在旁边时这类故障最难查。

* `bootstrap.py`：`pip install` / `git clone` 一旦上游半开连接就永久挂起 →
  默认 1200s，超时退 **124**（与 GNU timeout 一致）并带出部分输出。
* `apply_after_exit.py`：看护是**后台无人值守**进程 → `tasklist` 30s、
  补丁 600s。**`tasklist` 超时必须按「仍在运行」处理** ——
  反过来会在 asar 还被锁时动手写，且此时已错过退出时机，补丁永远写不进去，
  现象与[「看护从未等到退出」](#为什么有时要重启两遍才生效062-新增第-75-节)完全一样。

