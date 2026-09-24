# 增强提示词：根因分析与自定义方案（0.6.3 → 热配置版）

> **0.6.7 起最省事的用法**：在润色按钮上**右键** → 菜单按供应商分组列出所有可用模型 →
> 点选即生效，**不需要手动改任何文件**。菜单选择会写进下面说的 `enhance_config.json`
> （等价于手改配置，重启后依然沿用），并且本次点击还会通过 `override` 参数走 **explicit 档**
> —— 文件写入万一失败也不会影响这一次点击。
> 下文的手改方式仍然有效，适合需要一并调好思考强度 / 温度等参数的场景。

> 结论先行：`增强失败：HTTP 404/400 ... Model ... not supported/unavailable` 的根因是
> **前端取不到界面所选模型 → 恒走兜底 → 兜底恰好选「默认-临时」（供应商表第 0 位）的第一个模型
> `deepseek-v4-flash-0731` → 而该供应商的 key 在中转站已失效**。
> 请求实际到达了中转站（它能返回结构化错误），是中转站路由层直接拒绝、未转发上游，
> 所以渠道日志里看不到记录。代码取值逻辑、本地配置、进程本身都不是直接元凶。

## 一、根因链（已逐层实测验证）

1. **前端取模型稳定失灵**：`currentModel()` 全局搜 `[data-model-current-value]` 并按
   「offsetParent 可见 + 最靠下」挑节点。但 ZCode 输入框工具栏位于 **fixed 定位容器**内
   （fixed 元素 `offsetParent` 恒为 null，被可见过滤整批误杀），后台又常驻设置/工作流面板的
   隐藏节点（它们反而「更靠下」），于是 `modelValue/modelLabel` 恒为空或取错 → 无论怎么切模型、
   收不收起下拉都一样。实测 asar：属性本身挂在主界面模型按钮上（`data-chat-toolbar-popover-trigger`），
   问题纯在取值启发式。
2. **主进程四档解析全空 → ④兜底**：兜底排序把内置供应商排最后，取**第一个可用自定义供应商**
   的第一个模型 = 「默认-临时」的 `deepseek-v4-flash-0731`。
3. **「默认-临时」key 已失效**：实测该 key 发 `deepseek-v4-flash-0731` 与 `glm-5.3` 均 404
   `not supported by any configured account in this group`；「默认-永久」key 发同模型 200。
   第一轮的 `HTTP 400 Model is unavailable` 是同一兜底路径撞上临时 key 的坏渠道（间歇性），
   后来该 key 彻底失效表现为 404。两次报错同根。
4. **tip 误判**：classify 正则未覆盖 `is not supported`，404 落入「接口路径可能不对」——已修复。

## 二、修复内容（本次已改，重打 asar + 重启一次后生效）

| 文件 | 修改 |
|---|---|
| `scripts/zcode-enhance-prompt.js` | `currentModel()` 先锚定 composer dock 再找模型按钮；dock 命中时不再因 `offsetParent=null` 丢弃；全局兜底排除 `workflow-run-settings-model` 面板节点 |
| `scripts/zcode_patcher.py` `_ENHANCE_HANDLER` | ① 新增 **⓪热配置档**（最高优先级，读 `enhance_config.json`）；② body 参数全部热读（`maxTokens`/`temperature`）；③ 思考强度：openai 协议加 `reasoning_effort`，anthropic 协议加 `thinking.budget_tokens`（自动抬高 max_tokens）；④ classify 正则补 `not supported`，404 模型错误不再误报「路径问题」 |

向后兼容：`enhance_config.json` 不存在或 `providerId/modelId` 留空 = 与旧行为完全一致。

> **档位优先级（发版前已修正）**：⓪热配置 是**真正的最高优先级**，压过界面选择。
> 初版实现漏了 `!pick` 守卫，①档会在界面取值可读时把 ⓪ 覆盖掉 ——
> 结果是「配置了却不生效，只有当界面取不到模型时才生效」，与设计意图正好相反。
> 现在 ①②③ 三档都带 `!pick` 守卫，语义统一为「先到先得」。
> 回归测试：`tests/test_enhance_handler.py::TestHotConfigResolution`。

## 三、自定义增强的供应商 / 模型 / 思考强度（改完即生效，零重启）

**只换供应商/模型的话，用右键菜单最省事**（见文首）。要连思考强度 / 温度一起调，
再编辑 `<数据根>/.zcode/v2/enhance_config.json`（handler 每次点击都重新读取）：

```jsonc
{
  "providerId": "ff20a569-dfb4-4721-9749-072008d84504",  // 供应商 id（provider_config.json → providerRules[].providerId）
  "modelId": "glm-5.3-flash",                             // 发给中转站的模型名（可不在此供应商的模型表里）
  "reasoningEffort": "medium",                            // openai 协议: reasoning_effort；留空不发
  "thinkingBudget": 0,                                    // anthropic 协议: thinking budget_tokens(1024-32768)；0 不发
  "maxTokens": 2048,                                      // 输出上限（anthropic 开思考时会自动 ≥ budget+1024）
  "temperature": 0.3                                      // openai 协议温度；anthropic 不发温度
}
```

- **换供应商/模型**：改 `providerId` + `modelId`，下一次点增强立即生效。
- **思考强度**：openai-compatible 中转 → `reasoningEffort`（low/medium/high，按中转站支持的值）；
  也可以直接把 `modelId` 换成中转站的思考变体名。anthropic 协议 → `thinkingBudget`。
- **查 providerId**：跑 `python enhance_doctor.py`（scripts 目录），第 4 节直接显示命中供应商的 id；
  或看 `provider_config.json`。
- **诊断**：增强失败后控制台看 `window.__zenhanceDiag.lastRequest / lastResult`（含 `how=config/ref/fallback`
  与 `tried` 列表），一眼定位走的是哪一档。

## 四、生效路径（重要边界）

| 改动 | 生效方式 |
|---|---|
| **右键菜单选模型**（0.6.7） | **零重启**：写进 `enhance_config.json`，且本次点击走 explicit 档（双保险） |
| `enhance_config.json`（供应商/模型/思考强度/参数） | **零重启**：下次点击即生效 |
| 本次代码修改（前端脚本 + handler） | 需**一次性**：完全退出 ZCode → 跑 `python zcode_patcher.py --enhance-prompt`（或等看护自动写入）→ 重启 ZCode；之后永远只改配置、不再重启 |

> 无法做到「改 asar 内已注入的代码还不重启」：主进程 handler 在启动时注册进内存；
> 但本方案把所有可调项都收敛进热读配置，重启只有一次，以后不再需要。

## 五、临时止血（不改代码、零重启，今天即可用）

任选其一（推荐第 1 种，客户端自己写文件最安全）：

1. ZCode 设置里**删除或停用「默认-临时」供应商**（其 key 已失效）→ 兜底自动落到「默认-永久」；
2. 在设置里把「默认-永久」的模型列表把 `glm-5.3-flash` 调到第一位（兜底取列表第一个）；
3. 手改 `provider_config.json`（先关闭设置页，避免写冲突）：删掉「默认-临时」条目，或把它
   `personalModelIds` 清空（④兜底会 `continue` 跳过无模型的供应商）。

## 六、验证与回滚

- 验证：重启后输入文字点增强 → 成功则 toast 显示 `已用「glm-5.3-flash」增强`；
  失败则看 `window.__zenhanceDiag.lastResult.tried` 首项应为 `config:...`。
- 回滚：`python zcode_patcher.py --revert --enhance-prompt`；删除 `enhance_config.json` 即回到纯界面选择行为。
