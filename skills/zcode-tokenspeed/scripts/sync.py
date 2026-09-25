#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zcode-tokenspeed 插件开关同步（由 SessionStart hook 调用）

把客户端补丁同步到期望状态：
  字节级补丁（用量图表 / 弹窗加宽 / 档位配置）—— 立即写入，下次启动可见
  重打包级补丁（状态栏 / 滑条 / 增强 / 拉取）—— 交给退出后看护，ZCode 退出时写入

**零配置自动注入**：ZCode 只在用户点过「保存配置」后才把开关写进 config.json。
如果没保存过就什么都不做，用户看到的就是「装好了但没生效」。所以这里改为：
**已保存的开关优先，没保存过的键退回插件清单 `plugin.json` 里声明的默认值**。
装完 → 重启 ZCode → 开个新会话，钩子就会按默认值自动注入，**不需要打开配置页**。

三种调用形态：
  sync.py --detach   钩子用：登记心跳后**立刻后台化**并返回，绝不阻塞会话启动
  sync.py --worker   内部用：真正干活的子进程（stdout 已被丢弃）
  sync.py            人工调试用：前台执行，输出直接打在终端上

为什么要后台化：hook 是**内联**执行的（`async` 字段当前无运行时效果），
同步一次要跑多次 `--check`（每次约 2 秒），会话启动会被硬生生拖住；
而且钩子有超时上限，机器慢/杀软扫盘时可能直接被砍掉，表现为「什么都没发生」。

诊断日志：scripts/_sync.log
心跳文件：scripts/_sync.last（每次被调用都刷新，用来证明「钩子到底跑没跑」）
安装自检：python doctor.py（一条命令给出整条链路的结论）
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:                                   # 控制台编码/窗口安全网（见 _console.py 的说明）
    from _console import no_window_kwargs
except ImportError:                    # 被别处 import 时脚本目录可能不在 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _console import no_window_kwargs

HERE = Path(__file__).resolve().parent
PATCHER = HERE / "zcode_patcher.py"
WATCHDOG = HERE / "apply_after_exit.py"
CONFIG = Path.home() / ".zcode" / "cli" / "config.json"
# 插件 id 会随安装方式变化（本地 dev-default-xxxx、市场安装的 id 等），
# 因此按前缀匹配而不是写死某个 id —— 换台机器/换安装方式也能正确定位配置。
PLUGIN_ID_PREFIX = "zcode-tokenspeed"
LOG = HERE / "_sync.log"
# 「钩子到底跑没跑」的心跳文件：每次被调用都刷新一次时间戳。
# 为什么需要它：没拨过开关时 sync 什么也不做、日志也是空的，「钩子没触发」与
# 「触发了但无事可做」在日志里长得一模一样——排查安装问题时这是最关键的一条信息。
STAMP = HERE / "_sync.last"
# 首次自动注入的标记：用来保证「装好了，正在自动注入」这条会话提示只出现一次，
# 不然后面每次开会话都弹一遍，很快就变成噪声。
MARKER = HERE / "_autoinject.done"

# 配置键 -> (zcode_patcher.py 参数, 是否重打包级)
#
# **关于「立即写」这条路**：zcode_patcher.py 的运行预检是**全局**的 —— `zcode_running()`
# 只查 tasklist 里有没有 `ZCode.exe`，与 target 无关；命中就直接 `return 2` 拒绝写入
# （理由：app.asar 被锁、config.json / provider_config.json 会被客户端回写覆盖）。
# 而 SessionStart 钩子**必然**在 ZCode 运行中触发，所以标 False 的三项
# （reasoning_config / usage_chart / model_width）在钩子里永远写不进去 ——
# 早期版本就是这样：每次装完都报「未处理: xxx(执行失败)」。
# 现在改成**先试立即写，被拒（refused）就自动转交 apply_after_exit.py**：
# 桌面端场景退化成「退出时写入」，纯 CLI 场景（没有 ZCode.exe 在跑）仍能即时生效。
PATCHES = [
    ("reasoning_config", ["--reasoning-config"], False),   # 3.14+ 档位配置（配置侧原生）
    ("usage_chart", ["--usage-chart"], False),
    ("model_width", ["--model-width"], False),
    ("tps_footer", ["--tps-footer"], True),
    ("thought_slider", ["--thought-slider"], True),
    ("enhance_prompt", ["--enhance-prompt"], True),
    ("model_puller", ["--model-puller"], True),
    ("core_patch", [], False),                             # ≤3.11 内核补丁
]

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    try:
        import time
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def beat(msg: str) -> None:
    """刷新心跳文件，证明「钩子确实被调用过」，并写明这次做了什么。

    排查「插件装了没生效」时这是第一条要看的信息：心跳文件不存在 = 钩子没跑；
    存在但写着「未保存过开关」= 钩子跑了，只是用户没在配置里拨开关（按设计不动客户端）。
    """
    try:
        import time
        STAMP.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [{_invocation()}] {msg}\n",
                         encoding="utf-8")
    except Exception:
        pass


def _invocation() -> str:
    """这次是被谁调起来的（写进心跳，便于区分「钩子」与「人工」）。"""
    argv = sys.argv[1:]
    if "--detach" in argv:
        return "钩子"
    if "--worker" in argv:
        return "钩子→后台" if "--from-hook" in argv else "后台"
    return "手动"


def spawn_detached(extra_args: list) -> bool:
    """把本脚本以后台进程方式再起一份，立刻返回（不等待、不阻塞会话启动）。"""
    cmd = [sys.executable, str(Path(__file__).resolve()), *extra_args]
    kw = {"cwd": str(HERE), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kw)   # no-window-ok: 标志在 kw 里（含 CREATE_NO_WINDOW）
        return True
    except Exception as exc:
        log(f"后台启动失败: {exc!r}")
        return False


def _is_our_key(key) -> bool:
    """这个配置键是不是本插件的？

    ⚠️ 不能用裸 `startswith("zcode-tokenspeed")`：那会把 **别的插件** 的配置也命中 ——
    `zcode-tokenspeed-extra`、`zcode-tokenspeed-pro`、`zcode-tokenspeed-lite` 之类
    同前缀插件一旦同时安装，`_search()` 会先撞上谁取决于 dict 插入顺序，
    表现为「开关莫名串台」（甚至把别人的配置当自己的开关去注入/还原）。

    宿主的真实配置键形态是 `<插件名>@<市场名>`（见本项目记忆里的既有结论），
    所以只认两种：精确等于插件名，或 `<插件名>@` 开头。
    """
    s = str(key)
    return s == PLUGIN_ID_PREFIX or s.startswith(PLUGIN_ID_PREFIX + "@")


def _search(node, path="", depth=0):
    """在配置树里找本插件的配置对象（键以插件名前缀开头的那一层）。"""
    if depth > 6 or not isinstance(node, dict):
        return None
    for key, val in node.items():
        if isinstance(val, dict) and val and _is_our_key(key):
            return val, f"{path}.{key}"
    for key, val in node.items():
        if isinstance(val, dict):
            got = _search(val, f"{path}.{key}", depth + 1)
            if got:
                return got
    return None


def _coerce(raw):
    """把宿主可能写入的字符串布尔归一成 bool。"""
    out = {}
    for key, val in raw.items():
        if isinstance(val, bool):
            out[key] = val
        elif isinstance(val, str) and val.strip().lower() in ("true", "false"):
            out[key] = val.strip().lower() == "true"
    return out


def declared_defaults() -> dict:
    """读插件清单 `plugin.json` 里 `userConfig.*.default` 声明的默认值。

    **这是「零配置自动注入」的关键一环**：ZCode 只在用户点过「保存配置」之后才把
    `plugins.options` 写进 `config.json`；从没保存过就什么都没有。此时若按旧逻辑
    「没表态就不动」，用户看到的就是「装好了但没生效」——而这恰恰是最常见的抱怨。
    退回清单默认值之后，装完重启一次即自动注入，不必打开配置页、不必跑任何命令。

    向上找几层是为了兼容三种插件落点（市场根即插件根 / cache 的版本目录 / plugins 子目录）。

    ⚠️ 两道防护，都是为了「别读到**别人的**清单」：

    1. **只找到插件边界为止**（`_plugin_boundary`）—— 原先无条件 `HERE.parents[2:6]`，
       在仓库布局下会一路走到**盘符根**（`F:\\`）。用户把压缩包解到盘符根是常见操作，
       一旦那里躺着一个无关的 `.zcode-plugin/plugin.json`，开关默认值就会被它劫持，
       而且完全不报错。
    2. **清单必须确实是本插件的**（`name` 对得上）—— 上级目录里出现任何别的清单时，
       不能因为「它先被扫到」就用它的默认值。
    """
    for base in _plugin_boundary():
        for rel in (".zcode-plugin/plugin.json", ".claude-plugin/plugin.json"):
            p = base / rel
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            # 必须是本插件的清单：`name` 精确等于插件 id（`_is_our_key` 的同一套判定）
            if not _is_our_key(data.get("name")):
                log(f"跳过不是本插件的清单（name={data.get('name')!r}）：{p}")
                continue
            uc = data.get("userConfig")
            if not isinstance(uc, dict):
                continue
            out = {k: v["default"] for k, v in uc.items()
                   if isinstance(v, dict) and isinstance(v.get("default"), bool)}
            if out:
                return out
    log("没找到插件清单，取不到默认值")
    return {}


def _plugin_boundary() -> list[Path]:
    """从脚本目录向上返回「可能放着本插件清单」的目录，**到插件边界为止**。

    三种落点（见模块 docstring）对应清单所在层：
      * 市场根即插件根 : `<市场>/skills/<插件>/scripts` → 清单在 `parents[2]`
      * cache 版本目录 : `.../<插件>/<版本>/skills/<插件>/scripts` → 清单在 `parents[2]`
      * plugins 子目录 : `<市场>/plugins/<插件>/skills/<插件>/scripts` → 清单在 `parents[2]`

    所以正常情况下 `parents[2]` 就够了；再往上多找几层只是为历史/异形布局兜底。
    但**绝不能无限向上**：遇到盘符根（`F:\\`）或文件系统根（`/`）必须停 ——
    那里出现的同名清单不属于本插件，用它等于让无关文件劫持开关默认值。

    另外一旦发现某个目录**就是**本插件的根（含 `skills/<插件>` 结构），
    说明已经到边界，再往上只会看到无关目录，直接停。
    """
    out: list[Path] = []
    for base in list(HERE.parents):
        if base == base.parent:         # 盘符根 / 文件系统根 → 停，绝不参与
            break
        out.append(base)
        # 插件根特征：本目录下直接有 skills/<插件名> → 已到边界，再往上只会看到无关目录
        if (base / "skills" / PLUGIN_ID_PREFIX).is_dir():
            break
        if len(out) >= 6:               # 与原实现的搜索深度保持一致的兜底上限
            break
    return out


def read_options():
    """读本插件的开关值。返回 (options, source)；source 为 None 表示没找到配置。"""
    # 宿主若把插件配置注入 hook 环境，优先用环境变量。
    # 键名必须 `.lower()` 归一：Windows 上 os.environ 会把键名**转成大写**
    # （CPython 对 nt 的实现就是「Env Var Names Must Be UPPERCASE」），
    # 于是 ZCODE_PLUGIN_CONFIG_reasoning_config 读出来是 REASONING_CONFIG，
    # 与开关名对不上 → `merged = {**defaults, **explicit}` 里默认值（全 true）胜出，
    # 用户保存的开关会被**整体忽略**。
    env_opts = _coerce({key[len("ZCODE_PLUGIN_CONFIG_"):].lower(): val
                        for key, val in os.environ.items()
                        if key.startswith("ZCODE_PLUGIN_CONFIG_")})
    if env_opts:
        return env_opts, "env"

    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"读配置失败: {exc}")
        return {}, None
    found = _search(cfg, CONFIG.name)
    if found:
        return _coerce(found[0]), found[1]
    # 没找到条目：把 plugins 下的键名记下来，便于确认宿主的实际存储位置
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    log(f"配置里没有 {PLUGIN_ID_PREFIX}；plugins 现有键: "
        f"{sorted(plugins) if isinstance(plugins, dict) else plugins}")
    return {}, None


def check_state(args) -> str:
    """跑 --check 判断当前注入状态：on / stale / off / na（本版本不适用）/ unknown。

    `na` 是必需的第三态：例如 ≤3.11 专用的内核补丁在 3.14+ 上会明确打印
    「本补丁不适用」。旧逻辑只看「未打」→ 把它当成 off → 去执行 → 脚本空转一圈，
    最后却报成「已生效」。默认值全开之后，这个误报每次装完都会出现，必须区分开。

    ★ `stale`（已打但内容旧）是 0.6.1 补的第四态，修的是一类**最容易被误判成
    「已生效」**的场景：

        插件更新到新版（注入脚本改了，比如 0.5.11 的按钮跑出输入框修复）
        → 用户按提示退出并重启 ZCode
        → 但 app.asar 里的注入片段**还是旧版**，因为「更新插件」只换了插件目录，
          **不会**重新注入 app.asar

    旧逻辑把 `--check` 输出里的「已打」一律当成 on，于是 `run_sync` 走
    `continue`（视为已一致）→ **永远不会重跑注入** → 用户看到的改动永远不生效，
    而且报告里一句「未处理」都没有，只有一个可疑的静默。

    判据是 zcode_patcher.py 自己在 `--check` 里给出的两种措辞：
      * 「已打（含旧版组件，重跑可自动更新）」   → 四组件齐但内容 != 现行实现
      * 「已打（四组件均为当前版本），跳过」     → 真正的最新
    前者必须归为 stale（要求重跑），不能归为 on。
    """
    r = subprocess.run([sys.executable, str(PATCHER), *args, "--check"],
                       capture_output=True, encoding="utf-8", errors="replace",
                       cwd=str(HERE), timeout=120,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                       **no_window_kwargs())
    out = (r.stdout or "") + (r.stderr or "")
    if "不适用" in out:
        return "na"
    if "原生档位配置" in out:
        # 「无需写入」只出现在「not planned」分支（档位配置已是最新）；
        # 否则就是有 [ ] 待写入项 → off。别用「已是最新」判：该词在有待写入项时也会打印
        # （指的是另外 N 个已配好的模型），会误判成 on。
        return "on" if "无需写入" in out else "off"
    # 「已打（含旧版组件，重跑可自动更新）」必须排在 "已打" 之前判：
    # 这个串本身包含 "已打"，顺序反了就会被后面那条吃掉，stale 永远走不到。
    if "含旧版组件" in out:
        return "stale"
    if "未打" in out:
        return "off"
    if "已打" in out:
        return "on"
    return "unknown"


# 判定「真的没写成」的**精确**特征串。
#
# ⚠️ 这里绝不能用裸 "[!]"：zcode_patcher.py 有 60+ 处 `[!]` 输出，其中不少出现在
# **完全成功的路径**上，只是提示「顺带发现的问题」，例如：
#   * `发现 N 个模型同时存在于 providerModelRules 与 manualProviderModelRules…
#      建议在界面重新保存` —— 这是**事先就存在**的问题，脚本只报告、不修，仍然算成功；
#   * `备份读取/写入失败`（流程继续）、`integrity 分块数变化，跳过同步`（正常降级）。
# 早先写成 `any(mark in out for mark in ("锚点匹配异常", "拒绝", "[!]"))`，
# 后果是「补丁其实写成功了，却被判成 fail」→ summary 报「未处理: xxx(执行失败)」→
# 用户反复重试、每次都报失败（重试永远不会有别的结果）。实测已复现。
# 所以：只认「明确的拒绝/异常」特征串 + 退出码 + 汇总行的失败计数。
REJECT_MARKS = (
    "锚点匹配异常",
    "拒绝盲改",
    "无法安全更新，拒绝",
    "不符合预期，拒绝",
)


def _summary_failures(out: str) -> int | None:
    """解析 zcode_patcher.py 汇总行 `合计 N 项：成功 X，失败 Y` 里的 Y。

    这是比任何特征串都可靠的判据：它是脚本**自己**算出来的结论。
    解析不到（如 --check 之外的输出被截断）返回 None，交由其他判据决定。
    """
    m = re.search(r"合计\s*\d+\s*项：\s*成功\s*\d+\s*，\s*失败\s*(\d+)", out)
    return int(m.group(1)) if m else None


def run_patcher(args, revert: bool) -> str:
    """立即执行一次改写。返回 "ok" / "refused" / "fail"。

    `refused` 是独立的一态，不能和 `fail` 混在一起：zcode_patcher.py 在「ZCode 正在运行」
    时会明确打印拒绝原因并返回 2 —— 这不是失败，而是「现在不能写，等退出后写」，
    调用方要据此把这项转交给 apply_after_exit.py（见 PATCHES 上方注释）。
    """
    cmd = [sys.executable, str(PATCHER), *args] + (["--revert"] if revert else [])
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                       cwd=str(HERE), timeout=300,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                       **no_window_kwargs())
    out = (r.stdout or "") + (r.stderr or "")
    refused = r.returncode == 2 or "检测到 ZCode 正在运行" in out
    # zcode_patcher.py 拒绝改写时可能仍返回 0（只在输出里说明原因），必须看输出判定。
    # 三条判据，任一命中即为 fail（详见 REJECT_MARKS / _summary_failures 的注释）：
    #   1. 退出码非 0（2 已被 refused 吸收）
    #   2. 出现明确的「拒绝/异常」特征串
    #   3. 汇总行自报「失败 N」且 N > 0
    rejected = any(mark in out for mark in REJECT_MARKS)
    summary_failed = _summary_failures(out)
    ok = (r.returncode == 0 and not rejected
          and not (summary_failed is not None and summary_failed > 0))
    verdict = "refused" if refused else ("ok" if ok else "fail")
    log(f"$ zcode_patcher.py {' '.join(args)}{' --revert' if revert else ''} -> "
        f"{verdict}\n{out}".rstrip())
    return verdict


def start_watchdog(wanted: dict) -> None:
    """启动退出后看护：等 ZCode 退出 → 应用重打包级补丁。"""
    args = [f"--want={k}={'on' if v else 'off'}" for k, v in wanted.items()]
    kw = {"cwd": str(HERE), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        # POSIX：自成会话组（与 spawn_detached 同一做法），否则看护留在钩子的
        # 进程组里，应用退出/清理时可能被整组信号连带杀掉。
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, str(WATCHDOG), *args], **kw)  # no-window-ok: kw 已按平台携带 DETACHED_PROCESS|CREATE_NO_WINDOW（Windows）/ start_new_session（POSIX）
    log(f"已启动退出后看护: {' '.join(args)}")


def resolve_wanted() -> tuple[dict, str]:
    """算出这次要同步成什么样，返回 (期望状态, 来源说明)。

    规则：**已保存的开关优先，没保存过的键退回清单里声明的默认值。**
      * 从没保存过配置 → 全部用默认值（这就是「零配置自动注入」）
      * 保存过一部分   → 保存的照做，没提到的用默认值（插件升级新增开关时不会漏）
      * 显式关掉的开关 → 保存值是 false，优先于默认值，会被正常还原
    """
    opts, source = read_options()
    defaults = declared_defaults()
    if source is None:
        if not defaults:
            return {}, ""
        log("配置里没有本插件（从未保存过开关）→ 改用插件清单声明的默认值")
        return dict(defaults), "插件默认值（从未保存过开关）"
    explicit = {k: v for k, v in opts.items() if isinstance(v, bool)}
    if not explicit:
        if not defaults:
            return {}, ""
        log(f"配置来自 {source}，但没有可用布尔值 → 退回插件默认值")
        return dict(defaults), "插件默认值（配置里没有可用开关）"
    merged = {**defaults, **explicit}          # 保存值优先，缺的键补默认值
    return merged, source


def run_sync(echo: bool = False) -> str:
    """真正干活的同步逻辑。返回一句话结论（同时写进日志与心跳）。

    echo=False 时**不往 stdout 写任何东西**：钩子的 stdout 会被按 JSON schema 严格校验，
    输出非 JSON 会被判为「钩子运行失败」（虽然脚本副作用已经生效，但日志里会留下假故障）。
    """
    wanted, origin = resolve_wanted()
    if not wanted:
        return "既没有已保存的开关，也读不到插件清单默认值 → 未做任何操作"

    changed, deferred, failed, skipped, refreshed = [], {}, [], [], []
    for key, args, repack in PATCHES:
        if key not in wanted:
            continue
        want = wanted[key]
        state = check_state(args)
        if state == "na":
            skipped.append(key)          # 本版本不需要这个补丁（如 3.14+ 的内核补丁）
            continue
        if state == "unknown":
            failed.append(f"{key}(状态未知)")
            continue
        if state == "stale":
            # 已装的是旧版注入片段 → 无论开关要求开还是关，都必须重跑一次：
            #   want=True  → 重跑会把四组件更新到与现行脚本一致
            #   want=False → 反正要还原，直接走还原分支，不必先更新
            if not want:
                verdict = run_patcher(args, revert=True)
                if verdict == "ok":
                    changed.append(f"{key}→关")
                elif verdict == "refused":
                    deferred[key] = False
                else:
                    failed.append(f"{key}(执行失败)")
                continue
            if repack:
                deferred[key] = want
                refreshed.append(key)
                continue
            verdict = run_patcher(args, revert=False)
            if verdict == "ok":
                changed.append(f"{key}→开（更新为当前版本）")
            elif verdict == "refused":
                deferred[key] = want
                refreshed.append(key)
            else:
                failed.append(f"{key}(更新失败)")
            continue
        if want and state == "off":
            need_revert = False
        elif not want and state == "on":
            need_revert = True
        else:
            continue  # 已一致
        if repack:
            deferred[key] = want
            continue
        verdict = run_patcher(args, revert=need_revert)
        if verdict == "ok":
            changed.append(f"{key}→{'开' if want else '关'}")
        elif verdict == "refused":
            # 客户端在运行 → 现在写不了，转交退出后看护（这不是失败）
            deferred[key] = want
        else:
            failed.append(f"{key}(执行失败)")

    if deferred:
        start_watchdog(deferred)

    parts = []
    if changed:
        parts.append("已写入: " + "、".join(changed))
    if refreshed:
        parts.append("插件已更新，客户端退出时重注入为新版（下次启动可见）: "
                     + "、".join(refreshed))
    # 排除已在 refreshed 里说明过的键，避免同一项被报两遍（一个空串也是这个原因）
    rest = {k: v for k, v in deferred.items() if k not in refreshed}
    if rest:
        parts.append("ZCode 退出时写入（下次启动可见）: " + "、".join(
            f"{k}→{'开' if v else '关'}" for k, v in rest.items()))
    if skipped:
        parts.append("本版本不适用: " + "、".join(skipped))
    if failed:
        parts.append("未处理: " + "、".join(failed))
    summary = (" | ".join(parts) if parts
               else f"所有开关都已与客户端一致（来源：{origin}），无需改动")
    log(f"同步结果（来源：{origin}）—— {summary}")
    if echo:
        print(f"[zcode-tokenspeed] {summary}")
    return summary


NOTICE_HEAD = "ZCode Patcher 已自动接管本地补丁注入（无需手动配置）"


def build_notice() -> str:
    """首次自动注入时注入会话的说明。"""
    doctor = HERE / "doctor.py"
    return (
        f"{NOTICE_HEAD}。本次会话启动时已在后台开始同步：\n"
        "· 八项补丁（思考档位配置、用量页去截断、模型弹窗加宽、TPS 状态栏、思考强度滑条、"
        "增强提示词、模型拉取按钮）都要等你**完全退出 ZCode**（托盘图标右键 → 退出）时才会写入 ——\n"
        "  客户端运行期间会拒绝改写 app.asar 与 provider_config.json；退出后由看护写入，"
        "并自动把 ZCode 重新拉起来，再启动即可见。\n"
        "这一条只在首次自动注入时出现。想核对结果可以运行（只读，可选）：\n"
        f'    python "{doctor}"'
    )


def emit_notice() -> None:
    """往会话里注入一条说明，让「自动注入到底做没做」看得见。

    输出必须符合 ZCode 的 HookJSONOutput schema —— 这份 schema 是从内核
    `resources/glm/zcode.cjs` 里反查出来的（`uyr` / `grs` 两个 zod 定义）：

        { additionalContext?, additional_context?, continue?, decision?,
          hookSpecificOutput?, reason?, stopReason?, suppressOutput?, systemMessage? }

    这里只用顶层 `additionalContext`：它在内核里被**无条件**推入 `additionalContexts`
    （`Lio()` 里 `t.additionalContext && n.additionalContexts.push(...)`），
    不像 `hookSpecificOutput` 那样还要校验 `hookEventName` 与本次事件一致 ——
    写错事件名会被判为「钩子返回了错误的事件名」并把这次运行标成失败，没必要冒这个险。
    输出以 `{` 开头才会被解析（`wQs()` 里 `if(!n||!n.startsWith("{"))return;`），
    所以任何异常都直接吞掉、什么都不打印，绝不影响注入本身。
    """
    try:
        print(json.dumps({"additionalContext": build_notice()}, ensure_ascii=False))
    except Exception as exc:
        log(f"提示输出失败: {exc!r}")


def first_auto_inject() -> bool:
    """是不是这个插件安装后的第一次自动注入（决定要不要出那条提示）。"""
    try:
        if MARKER.exists():
            return False
        MARKER.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        return True
    except Exception:
        return False


def main() -> None:
    """入口分流：钩子只负责「登记心跳 + 后台化」，干活交给 --worker 子进程。"""
    argv = sys.argv[1:]

    if "--worker" in argv:
        beat("后台同步已启动")
        beat(run_sync(echo=False))
        return

    if "--detach" in argv:
        # 钩子模式：先留下心跳（哪怕后台起不来也证明钩子跑过），再立刻返回
        beat("已启动（后台同步）")
        if first_auto_inject():
            emit_notice()          # 只在首次自动注入时往会话里说明一句
        if not spawn_detached(["--worker", "--from-hook"]):
            beat(run_sync(echo=False))  # 兜底：后台起不来就前台做完
        return

    # 人工前台执行：把结论直接打在终端上
    beat("手动执行")
    summary = run_sync(echo=True)
    beat(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # hook 绝不能因自身异常打断会话启动
        log(f"sync 异常: {exc!r}")
        beat(f"异常退出: {exc!r}")
