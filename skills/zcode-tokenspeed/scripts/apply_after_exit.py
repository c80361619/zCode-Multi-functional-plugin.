#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZCode 退出后自动打补丁看护（计划任务 / 插件开关同步调用，勿手动常跑）
轮询等待 ZCode.exe 全部退出 → 按期望状态应用/还原补丁（重打包级需文件未被占用）
→ 重启 ZCode → 记录日志后退出。日志: scripts/_apply_after_exit.log

两种调用方式：
  1) 无参数（本地计划任务）：应用 档位配置 + TPS 状态栏 + 滑条 + 拉取按钮（见 DEFAULT_TASKS）
  2) `--want=<键>=on|off`（插件 sync.py 传入，可多个）：只处理指定开关，off 走 --revert

取消方式: schtasks /Delete /TN ZCodePatchApply /F（并删除本脚本）

退出码约定（与 zcode_patcher.py 一致）：0=成功、1=有项目失败/等待超时、2=预检失败（ZCode 仍在运行）。
"""

import os
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
LOG = HERE / "_apply_after_exit.log"
POLL_SEC = 3
MAX_WAIT_SEC = 24 * 3600
PYTHON = sys.executable or "python"
#: 子进程超时（秒）。看护是**无人值守**运行的，一旦某个子进程挂死就再也没有人
#: 会来收拾 —— 表现为「明明退出了却永远不生效」（与 7.5 节那个故障长得一样）。
#: tasklist 正常 <1s；zcode_patcher 在 311MB asar 上实测 2~3s，给足余量。
TASKLIST_TIMEOUT = 30
PATCH_TIMEOUT = 600


def _import_patcher():
    """导入主脚本模块；sys.path 只在缺失时插入。

    看护轮询循环每 POLL_SEC 秒调一次 zcode_running() → 本函数被反复执行，
    不能每次都 insert（sys.path 会无界增长——24h 上限约 2.9 万个重复项）。
    """
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import zcode_patcher as zp
    return zp


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def zcode_running() -> bool:
    # 复用主脚本 zcode_patcher.zcode_running() 的跨平台检测（Windows: tasklist 检索
    # ZCode.exe；POSIX: pgrep -f ZCode）。看护的判定必须与补丁预检同源，否则会出现
    # 「看护以为退干净了、动手时又被预检拒绝」的永久错位。
    # 历史问题：这里曾直接跑 ["tasklist"] 并检索 b"ZCode.exe" —— macOS/Linux 上没有
    # tasklist，FileNotFoundError 未被捕获（except 只接 TimeoutExpired），看护在第一次
    # 轮询就崩溃；启动方又把 stderr 定向到 DEVNULL，崩溃完全无声，表现为
    # 「等退出 0 次、完成 0 次」、补丁永远写不进去。
    try:
        return _import_patcher().zcode_running()
    except Exception as e:
        log(f"运行检测失败: {type(e).__name__}: {e}，本轮按「仍在运行」处理")
        return True


def _find_exe(res: Path) -> Path | None:
    """从 asar 所在目录（<安装根>/resources）推出各平台的 ZCode 主程序路径。"""
    root = res.parent                       # Windows: 安装根；macOS: ZCode.app/Contents
    cands: list[Path] = [root / "ZCode.exe"]            # Windows
    if sys.platform == "darwin":
        cands.insert(0, root / "MacOS" / "ZCode")       # macOS .app 包
    else:
        cands += [root / "zcode", root / "ZCode"]       # Linux 常见命名
    for c in cands:
        if c.is_file():
            return c
    return None


def resolve_install() -> tuple[Path | None, Path | None]:
    """复用主脚本的跨平台探测拿到 (asar 目录, ZCode 可执行文件)。"""
    try:
        zp = _import_patcher()
        for cjs in zp.discover():
            res = cjs.parent.parent
            if (res / "app.asar").is_file():
                return res, _find_exe(res)
    except Exception as e:
        log(f"安装探测失败: {type(e).__name__}: {e}")
    return None, None


# 配置键 -> zcode_patcher.py 参数（与 sync.py 的 PATCHES 一一对应，顺序也保持一致）
# 漏一个键的后果：`--want=<键>=on` 会被 parse_wants 当「未知开关」忽略，
# 于是那个开关**开不起来也关不干净**（enhance_prompt 就漏过这一条）。
PATCH_ARGS = {
    "reasoning_config": ["--reasoning-config"],
    "usage_chart": ["--usage-chart"],
    "model_width": ["--model-width"],
    "tps_footer": ["--tps-footer"],
    "thought_slider": ["--thought-slider"],
    "enhance_prompt": ["--enhance-prompt"],
    "model_puller": ["--model-puller"],
    "core_patch": [],
}
# 未收到 --want 时的默认动作（本地计划任务用）：档位配置 + 三个重打包级补丁
DEFAULT_TASKS = [["--reasoning-config"], ["--tps-footer"], ["--thought-slider"], ["--model-puller"]]


def parse_wants(argv: list[str]):
    """解析 sync.py 传入的 --want=<key>=on|off。
    返回 [(args, revert), ...]；没有任何 --want 时返回 None（走 DEFAULT_TASKS）。
    历史问题：早期版本忽略 --want，一律按"全部打开"处理 —— 于是把开关关掉并不会真正还原。"""
    tasks = []
    for a in argv:
        if not a.startswith("--want="):
            continue
        key, _, val = a[len("--want="):].partition("=")
        key = key.strip()
        if key not in PATCH_ARGS:
            log(f"忽略未知开关: {a}")
            continue
        tasks.append((PATCH_ARGS[key], val.strip().lower() in ("off", "false", "0", "no")))
    return tasks or None


def main() -> int:
    log("看护启动，等待 ZCode 退出…")
    waited = 0
    while zcode_running():
        time.sleep(POLL_SEC)
        waited += POLL_SEC
        if waited >= MAX_WAIT_SEC:
            log("等待超时（24h），放弃")
            return 1

    tasks = parse_wants(sys.argv[1:])
    if tasks is None:
        tasks = [(args, False) for args in DEFAULT_TASKS]
    desc = "、".join(("还原 " if rev else "应用 ") + (" ".join(a) or "内核补丁") for a, rev in tasks)
    log(f"ZCode 已退出（等待 {waited}s），开始处理：{desc}")

    failed = 0
    for args, revert in tasks:
        cmd = [PYTHON, str(HERE / "zcode_patcher.py"), *args] + (["--revert"] if revert else [])
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=PATCH_TIMEOUT,
                               **no_window_kwargs())
        except subprocess.TimeoutExpired:
            # 超时 = 这一项没做成。继续跑剩下的项（能写一项是一项），最后计入 failed。
            log(f"$ zcode_patcher.py {' '.join(cmd[2:])}  [超时 {PATCH_TIMEOUT}s]")
            failed += 1
            continue
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
        log(f"$ zcode_patcher.py {' '.join(cmd[2:])}  [exit={r.returncode}]\n{out}".rstrip())
        if r.returncode == 2:
            log("预检未通过（ZCode 在等待期间被重新拉起），本次放弃，不重启")
            return 1
        if r.returncode != 0:
            failed += 1

    res, exe = resolve_install()
    if exe is None:
        log("未找到 ZCode 主程序（Windows: ZCode.exe / macOS: .app 包内 MacOS/ZCode），请手动启动")
        return 1
    log(f"处理完成（失败 {failed} 项），重启 ZCode")
    if zcode_running():
        log("检测到 ZCode 已再次运行，跳过重启")
        return 0
    try:
        if os.name == "nt":
            subprocess.Popen([str(exe)], cwd=str(exe.parent),
                             creationflags=0x00000008)   # DETACHED_PROCESS——保持上游原样，Windows 行为零变化
        elif sys.platform == "darwin" and res is not None and res.parent.parent.suffix == ".app":
            # macOS 经 Launch Services 打开 .app 包。注意 creationflags 是 Windows 专属
            # 参数，POSIX 上传入会直接抛 ValueError，所以必须按平台分流。
            subprocess.Popen(["open", str(res.parent.parent)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **no_window_kwargs())
        else:
            subprocess.Popen([str(exe)], cwd=str(exe.parent), start_new_session=True,
                             **no_window_kwargs())
    except Exception as e:
        log(f"重启 ZCode 失败（请手动启动）: {e}")
        return 1
    log("DONE")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # 看护无人值守：崩溃必须留痕。此前 stderr 被 start_watchdog 定向到 DEVNULL，
        # 崩溃无声 —— 这正是 macOS 上「等退出 0 次」长期没被发现的原因。
        import traceback
        log("看护异常退出:\n" + traceback.format_exc())
        sys.exit(1)
