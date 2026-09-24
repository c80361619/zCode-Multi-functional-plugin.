#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zcode_patcher 回归测试（纯标准库 unittest，无第三方依赖）
==========================================================
运行：
  python -m unittest discover -s tests -v
  python tests/test_patcher.py            # 等价

为什么需要这些测试：本工具做的是**二进制级改写**（asar 头解析、offset 重排、integrity 重算、
字节级原地覆盖、备份指纹校验），靠人工点一遍根本覆盖不到。历史上踩过的坑都能在这里复现：
  * 「offset 重排后用旧位置切片」→ 抽查 50 个文件错 11 个（test_repack_*）
  * 「全局替换 integrity 哈希串」误伤同内容条目（test_sync_integrity_*）
  * 「文本模式读写把 CRLF 归一为 LF」（test_kernel_patch_preserves_bytes）
  * 「升级后还原把旧版内核盖回新客户端」（test_kernel_backup_fingerprint_*）
"""

import contextlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

# 兼容两种仓库布局：扁平（scripts/）与插件（skills/zcode-tokenspeed/scripts/）
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parent / "scripts",
              _HERE.parent / "skills" / "zcode-tokenspeed" / "scripts"):
    if (_cand / "zcode_patcher.py").is_file():
        sys.path.insert(0, str(_cand))
        break
import zcode_patcher as zp          # noqa: E402


# ------------------------------------------------------------------ 测试夹具

def build_asar(path: Path, files: dict, unpacked: tuple = ()) -> None:
    """构造一个最小合法 asar（头 16 字节 + JSON + pad + 数据区），与 Electron/asar 同构。"""
    data = bytearray()
    placed = {}
    for p in sorted(files):
        if p in unpacked:
            continue
        placed[p] = (len(files[p]), len(data))
        data += files[p]

    tree = {"files": {}}

    def node_for(p: str) -> dict:
        node = tree
        parts = p.split("/")
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        return node["files"]

    for p, (size, off) in placed.items():
        node_for(p)[p.split("/")[-1]] = {
            "size": size, "offset": str(off), "integrity": zp._asar_integrity(files[p]),
        }
    for p in unpacked:
        node_for(p)[p.split("/")[-1]] = {
            "size": len(files.get(p, b"")), "offset": "0", "unpacked": True,
        }

    js = json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(js) % 4) % 4
    header = (struct.pack("<4I", 4, 8 + len(js) + pad, 4 + len(js) + pad, len(js))
              + js + b"\x00" * pad)
    path.write_bytes(header + bytes(data))


def read_entry(asar: Path, entry_path: str) -> bytes:
    raw, header, data_start = zp._asar_header_raw(asar)
    return zp._asar_entry_bytes(raw, data_start, zp._asar_find_entry(header, entry_path))


def quiet(fn, *a, **kw):
    """吞掉被测函数的打印输出（测试关注返回值与副作用，不看日志）。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


@contextlib.contextmanager
def patcher_stubbed(sync, states=None, verdicts=None):
    """把 sync 里三处会真正碰客户端的地方换成记录桩，并返回调用记录列表。

    **单元测试绝不能真的跑 zcode_patcher.py** —— 那会按当前开关改写本机的 app.asar，
    跑一次测试就顺手把用户的客户端改了。所以：
      check_state   → 查表返回（默认全 "on"，即「客户端已一致」，最安全的基线）
      run_patcher   → 只记录，不执行（默认返回 "ok"；verdicts 可让某组参数返回 "refused"/"fail"）
      start_watchdog→ 只记录，不起进程
    调用记录形如 ("run", ("--usage-chart",), False) / ("watchdog", {"tps_footer": True})。
    """
    table = states or {}
    vtable = verdicts or {}
    calls = []
    orig = (sync.check_state, sync.run_patcher, sync.start_watchdog)
    sync.check_state = lambda args: table.get(tuple(args), "on")
    sync.run_patcher = lambda args, revert: (
        calls.append(("run", tuple(args), revert)),
        vtable.get(tuple(args), "ok"))[1]
    sync.start_watchdog = lambda wanted: calls.append(("watchdog", dict(wanted)))
    try:
        yield calls
    finally:
        sync.check_state, sync.run_patcher, sync.start_watchdog = orig


def make_cjs(anchor: str, prefix: str = "/*pre*/", suffix: str = "/*post*/",
             newline: str = "\n") -> bytes:
    """用真实锚点拼一个假的 zcode.cjs（含换行，用于验证字节级改写）。"""
    body = f"{prefix}{newline}{anchor}{newline}{suffix}{newline}"
    return body.encode("utf-8")


class TempCase(unittest.TestCase):
    def setUp(self):
        # Windows 上临时目录偶尔会被索引/杀软短暂占用，cleanup 失败不该让测试变红
        self._tmp = tempfile.TemporaryDirectory(prefix="zpatch-test-", ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


# ------------------------------------------------------------------ asar 读写 / 重打包

class TestAsarRepack(TempCase):
    FILES = {
        "out/renderer/index.html": b"<html><body></body></html>",
        "out/renderer/assets/a.js": b"console.log('a');",
        "out/renderer/assets/deep/b.js": b"console.log('b');",
        "out/main/index.js": b"require('electron');",
        "package.json": b'{"name":"demo","version":"1.2.3"}',
        "node_modules/native/dup.txt": b"same-content",          # 与下一条内容完全相同
        "node_modules/native/dup2.txt": b"same-content",
    }

    def setUp(self):
        super().setUp()
        self.asar = self.tmp / "app.asar"
        build_asar(self.asar, self.FILES, unpacked=("node_modules/native/big.node",))
        self.asar_unpacked = self.tmp / "app.asar.unpacked"
        self.asar_unpacked.mkdir()

    def test_header_parse_matches_fixture(self):
        """头解析出的偏移必须能取回原始字节（验证 16 字节头 / data_start 公式）。"""
        for p, want in self.FILES.items():
            self.assertEqual(read_entry(self.asar, p), want, f"{p} 字节不一致")

    def test_package_version_readable(self):
        self.assertEqual(zp.asar_version(self.asar), "1.2.3")

    def test_repack_without_changes_keeps_every_entry(self):
        """空重打包：每个条目字节必须与原来完全一致（offset 重排不能错位）。"""
        before = {p: read_entry(self.asar, p) for p in self.FILES}
        zp._repack_asar(self.asar, {}, set())
        for p, want in before.items():
            self.assertEqual(read_entry(self.asar, p), want, f"{p} 重打包后错位")

    def test_repack_overwrite_and_add(self):
        """覆盖 + 新增：改动的条目字节与 integrity 都要对，未改动的原样保留。"""
        new_index = b"<html><body><script src='./x.js'></script></body></html>"
        new_script = b"/* injected */"
        zp._repack_asar(self.asar, {
            "out/renderer/index.html": new_index,
            "out/renderer/x.js": new_script,
        }, set())

        self.assertEqual(read_entry(self.asar, "out/renderer/index.html"), new_index)
        self.assertEqual(read_entry(self.asar, "out/renderer/x.js"), new_script)
        for p in ("out/main/index.js", "package.json", "out/renderer/assets/deep/b.js"):
            self.assertEqual(read_entry(self.asar, p), self.FILES[p], f"{p} 被误改")

        raw, header, data_start = zp._asar_header_raw(self.asar)
        for p in ("out/renderer/index.html", "out/renderer/x.js"):
            ent = zp._asar_find_entry(header, p)
            self.assertEqual(ent["integrity"], zp._asar_integrity(read_entry(self.asar, p)),
                             f"{p} integrity 未重算")

    def test_repack_relayouts_offsets_contiguously(self):
        """重排后 offset 必须紧邻且等于真实位置，不能出现空洞或重叠。"""
        zp._repack_asar(self.asar, {"out/renderer/index.html": b"x" * 100}, set())
        raw, header, data_start = zp._asar_header_raw(self.asar)
        pos = []
        for p, ent in zp._asar_walk_entries(header):
            pos.append((int(ent["offset"]), ent["size"], p))
        pos.sort()
        cursor = 0
        for off, size, p in pos:
            self.assertEqual(off, cursor, f"{p} offset 不连续")
            cursor += size
        self.assertEqual(data_start + cursor, len(raw), "数据区末尾与文件长度不符")

    def test_repack_removes_entry(self):
        zp._repack_asar(self.asar, {}, {"out/renderer/assets/a.js"})
        raw, header, _ = zp._asar_header_raw(self.asar)
        self.assertIsNone(zp._asar_find_entry(header, "out/renderer/assets/a.js"))
        self.assertEqual(read_entry(self.asar, "out/renderer/assets/deep/b.js"),
                         self.FILES["out/renderer/assets/deep/b.js"])

    def test_repack_skips_unpacked_entry(self):
        """unpacked 条目要留在树里（Electron 从 app.asar.unpacked 读它），但不能进数据区。"""
        before = read_entry(self.asar, "out/renderer/assets/a.js")
        zp._repack_asar(self.asar, {"out/renderer/index.html": b"y" * 50}, set())
        raw, header, data_start = zp._asar_header_raw(self.asar)
        ent = zp._asar_find_entry(header, "node_modules/native/big.node")
        self.assertIsNotNone(ent, "unpacked 条目被从树里删掉了")
        self.assertTrue(ent.get("unpacked"))
        self.assertEqual(ent["offset"], "0", "unpacked 条目不应被分配数据区偏移")
        # 数据区长度 == 所有非 unpacked 条目之和（unpacked 没被塞进去）
        total = sum(int(e["size"]) for _, e in zp._asar_walk_entries(header))
        self.assertEqual(len(raw) - data_start, total)
        self.assertEqual(read_entry(self.asar, "out/renderer/assets/a.js"), before)

    def test_repack_leaves_every_entry_integrity_consistent(self):
        """★ 全域 integrity 自洽（2026-09-23 事故回归）。

        重打包会整体位移数据区，**未改动条目**的 integrity 必须仍然对得上自己的
        新位置。事故当天旧实现只回读校验了「本次被覆盖」的条目，导致 4,138 个
        未改动条目内容错位而 integrity 未同步；asar 结构校验（offset 连续/无重叠）
        完全看不出来，只有 Electron 按 integrity 拒绝加载时才暴露，表现为
        「客户端打不开、无任何日志」。这里对**每一个**条目重算哈希。"""
        zp._repack_asar(self.asar, {
            "out/renderer/index.html": b"<html>bigger content than before</html>",
            "out/main/index.js": b"require('electron');" + b"/* pad */" * 40,
        }, set())
        raw, header, data_start = zp._asar_header_raw(self.asar)
        checked = 0
        for p, ent in zp._asar_walk_entries(header):
            itg = ent.get("integrity")
            if not itg:
                continue
            data = zp._asar_entry_bytes(raw, data_start, ent)
            self.assertEqual(itg["hash"], zp._sha256(data),
                             f"{p} 重打包后 integrity 与实际内容不符（布局错位）")
            checked += 1
        self.assertGreater(checked, 3, "夹具条目太少，校验没有意义")

    def test_repack_refuses_to_write_misaligned_layout(self):
        """★ 布局错位的 asar 必须拒绝落盘（同上事故的拦截面）。

        构造一个 header 里 offset 被写错的 asar（内容与声明不符），
        `_repack_asar` 必须在回读校验阶段抛错、**不改动原文件**。"""
        build_asar(self.asar, {
            "a.js": b"A" * 100,
            "b.js": b"B" * 200,
            "c.js": b"C" * 300,
        })
        # 手工把 c.js 的 offset 改错 50 字节，integrity 保持原样（= 错位态）
        raw, header, data_start = zp._asar_header_raw(self.asar)
        header["files"]["c.js"]["offset"] = str(int(header["files"]["c.js"]["offset"]) - 50)
        json_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
        pad = (4 - len(json_bytes) % 4) % 4
        blob = struct.pack("<4I", 4, 8 + len(json_bytes) + pad,
                           4 + len(json_bytes) + pad, len(json_bytes)) + json_bytes + b"\x00" * pad
        with open(self.asar, "r+b") as f:
            f.write(blob)
        before_bytes = self.asar.read_bytes()

        with self.assertRaises(ValueError) as cm:
            zp._repack_asar(self.asar, {"a.js": b"A" * 120}, set())
        self.assertIn("integrity", str(cm.exception))
        self.assertEqual(self.asar.read_bytes(), before_bytes,
                         "校验失败时不应改动原文件")

    def test_repack_is_serialized_by_write_lock(self):
        """★ 同一 asar 的重打包必须互斥（2026-09-23 事故根因之一）。

        看护与手动流程同时进入 `_repack_asar` 会交错落盘、产出错位文件。
        这里用线程模拟「另一个写入者占着锁」，验证第二个进入者会等待而不是并发。"""
        import threading

        order = []
        started = threading.Event()

        def holder():
            with zp._AsarWriteLock(self.asar, timeout=10):
                order.append("hold-start")
                started.set()
                time.sleep(0.6)
                order.append("hold-end")

        t = threading.Thread(target=holder)
        t.start()
        started.wait(5)
        assert order == ["hold-start"], f"锁没被持有: {order}"

        # 主线程此时进入 → 必须等到 holder 释放
        with zp._AsarWriteLock(self.asar, timeout=10):
            order.append("second-acquired")
        t.join(5)

        self.assertEqual(order, ["hold-start", "hold-end", "second-acquired"],
                         "两个写入者交错了（锁未生效）")

    def test_write_lock_times_out_with_actionable_message(self):
        """拿不到锁时要在有限时间内报错，而不是永久挂死。"""
        import threading

        started = threading.Event()

        def holder():
            with zp._AsarWriteLock(self.asar, timeout=10):
                started.set()
                time.sleep(1.5)

        t = threading.Thread(target=holder)
        t.start()
        started.wait(5)
        with self.assertRaises(TimeoutError) as cm:
            with zp._AsarWriteLock(self.asar, timeout=0.4, poll=0.05):
                pass
        self.assertIn("写入锁超时", str(cm.exception))
        t.join(5)


class TestIntegritySync(TempCase):
    """integrity 同步必须只动目标条目——同内容条目不能被误伤。"""

    def setUp(self):
        super().setUp()
        self.asar = self.tmp / "app.asar"
        build_asar(self.asar, {
            "dup1.txt": b"same-content",
            "dup2.txt": b"same-content",
            "target.js": b"old-target",
        })

    def test_sync_only_touches_target_entry(self):
        new = b"new-target"
        raw, header, data_start = zp._asar_header_raw(self.asar)
        ent = zp._asar_find_entry(header, "target.js")
        # 原地覆盖 + 同步 integrity（模拟字节级补丁）
        with open(self.asar, "r+b") as f:
            f.seek(data_start + int(ent["offset"]))
            f.write(new)
        quiet(zp._asar_sync_integrity, self.asar, "target.js", new)

        _, header2, _ = zp._asar_header_raw(self.asar)
        self.assertEqual(zp._asar_find_entry(header2, "target.js")["integrity"],
                         zp._asar_integrity(new))
        for p in ("dup1.txt", "dup2.txt"):
            self.assertEqual(zp._asar_find_entry(header2, p)["integrity"],
                             zp._asar_integrity(b"same-content"),
                             f"{p} 的 integrity 被误改")

    def test_sync_is_idempotent(self):
        new = b"new-target"
        zp._asar_sync_integrity(self.asar, "target.js", new)
        before = self.asar.read_bytes()
        quiet(zp._asar_sync_integrity, self.asar, "target.js", new)
        self.assertEqual(self.asar.read_bytes(), before, "重复同步不应改文件")

    def test_sync_dry_run_does_not_write(self):
        before = self.asar.read_bytes()
        quiet(zp._asar_sync_integrity, self.asar, "target.js", b"zzz", dry_run=True)
        self.assertEqual(self.asar.read_bytes(), before)


class TestRepackMemory(TempCase):
    """重打包不该把整包读进内存——真实 app.asar 三百多 MB，整包读入会让峰值接近 2× 包体。"""

    def setUp(self):
        super().setUp()
        self.big = b"x" * (3 * 1024 * 1024)          # 单条 3MB，跨多个 1MB 搬运块
        self.files = {"big.bin": self.big, "out/renderer/index.html": b"<html></html>"}
        for i in range(12):
            self.files[f"pad/{i}.bin"] = bytes([65 + i]) * (3 * 1024 * 1024)
        self.asar = self.tmp / "app.asar"
        build_asar(self.asar, self.files)

    def test_peak_memory_far_below_package_size(self):
        import tracemalloc
        size = self.asar.stat().st_size
        self.assertGreater(size, 30 * 1024 * 1024, "夹具太小，测不出内存行为")

        tracemalloc.start()
        zp._repack_asar(self.asar, {"out/renderer/index.html": b"z" * 1000}, set())
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertLess(peak, size // 3,
                        f"峰值 {peak / 1048576:.1f}MB 相对包体 {size / 1048576:.1f}MB 过大")

    def test_large_entries_survive_chunked_copy(self):
        zp._repack_asar(self.asar, {"out/renderer/index.html": b"z" * 1000}, set())
        self.assertEqual(read_entry(self.asar, "big.bin"), self.big)
        for i in (0, 5, 11):
            self.assertEqual(read_entry(self.asar, f"pad/{i}.bin"), self.files[f"pad/{i}.bin"])
        self.assertEqual(read_entry(self.asar, "out/renderer/index.html"), b"z" * 1000)


class TestPrune(TempCase):
    """--prune 只清理本工具自己的产物：默认不碰当前备份与 sidecar，也绝不碰邻居文件。"""

    def setUp(self):
        super().setUp()
        self.asar = self.tmp / "app.asar"
        build_asar(self.asar, {"a.js": b"aaaa"})
        for name in ("app.asar.tps.bak", "app.asar.tps.bak.meta.json",
                     "app.asar.chart-patch.json",
                     "app.asar.puller.bak.stale-20260101-000000",
                     "app.asar.123.tmp", "app.asar.tps-tmp",
                     "unrelated.txt", "app.asar.old", "app.asarfoo"):
            (self.tmp / name).write_bytes(b"x" * 10)

    def names(self):
        return {p.name for p in self.tmp.iterdir()}

    def test_default_prune_only_archives_and_temps(self):
        quiet(zp.prune_artifacts, [self.asar], False)
        left = self.names()
        for keep in ("app.asar.tps.bak", "app.asar.chart-patch.json",
                     "unrelated.txt", "app.asar.old", "app.asarfoo", "app.asar"):
            self.assertIn(keep, left, f"{keep} 不该被删")
        for gone in ("app.asar.puller.bak.stale-20260101-000000",
                     "app.asar.123.tmp", "app.asar.tps-tmp"):
            self.assertNotIn(gone, left, f"{gone} 应被清理")

    def test_deep_prune_also_removes_backups_and_sidecars(self):
        quiet(zp.prune_artifacts, [self.asar], True)
        left = self.names()
        for gone in ("app.asar.tps.bak", "app.asar.tps.bak.meta.json",
                     "app.asar.chart-patch.json"):
            self.assertNotIn(gone, left, f"{gone} 应被 --deep 清理")
        self.assertIn("app.asar", left, "包本体不能删")
        self.assertIn("unrelated.txt", left)

    def test_dry_run_keeps_everything(self):
        quiet(zp.prune_artifacts, [self.asar], True, True)
        self.assertTrue((self.tmp / "app.asar.tps.bak").exists())
        self.assertTrue((self.tmp / "app.asar.123.tmp").exists())


class TestReplaceWithRetry(TempCase):
    """Windows 上 os.replace 会遇到杀软/索引器的瞬时占用（WinError 5 / 32）。

    实测：ZCode 进程持有 app.asar 读句柄时，裸 os.replace 直接 WinError 5 失败；
    这个占用可能是另一个进程正在扫描/释放句柄的瞬时状态，短退避即可跨过去。
    """

    def setUp(self):
        super().setUp()
        self.dst = self.tmp / "app.asar"
        self.dst.write_bytes(b"old")
        self.tmpfile = self.tmp / "app.asar.123.tmp"
        self.tmpfile.write_bytes(b"new")

    def test_happy_path_replaces(self):
        zp._replace_with_retry(self.tmpfile, self.dst)
        self.assertEqual(self.dst.read_bytes(), b"new")
        self.assertFalse(self.tmpfile.exists())

    def test_retries_through_transient_lock(self):
        """模拟「先占用、0.6 秒后释放」——裸 os.replace 会失败，带重试必须成功。"""
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                err = OSError(13, "拒绝访问")
                err.winerror = 5
                raise err
            return real(src, dst)

        with unittest.mock.patch.object(zp.os, "replace", flaky):
            with unittest.mock.patch.object(zp.time, "sleep", lambda _s: None):
                zp._replace_with_retry(self.tmpfile, self.dst)
        self.assertEqual(calls["n"], 3, "应在第 3 次尝试时成功")
        self.assertEqual(self.dst.read_bytes(), b"new")

    def test_non_lock_errors_are_not_retried(self):
        """不是占用类的错误（如文件不存在）必须原样抛出，不能白等 5 轮。"""
        calls = {"n": 0}

        def bad(src, dst):
            calls["n"] += 1
            raise FileNotFoundError(2, "No such file")

        with unittest.mock.patch.object(zp.os, "replace", bad):
            with self.assertRaises(FileNotFoundError):
                zp._replace_with_retry(self.tmpfile, self.dst)
        self.assertEqual(calls["n"], 1, "非占用类错误只应尝试一次")

    def test_gives_up_after_max_attempts(self):
        calls = {"n": 0}

        def always_locked(src, dst):
            calls["n"] += 1
            err = OSError(13, "拒绝访问")
            err.winerror = 5
            raise err

        with unittest.mock.patch.object(zp.os, "replace", always_locked):
            with unittest.mock.patch.object(zp.time, "sleep", lambda _s: None):
                with self.assertRaises(OSError):
                    zp._replace_with_retry(self.tmpfile, self.dst, attempts=3)
        self.assertEqual(calls["n"], 3)

    def test_repack_uses_the_retrying_replace(self):
        """重打包路径必须走 _replace_with_retry，而不是裸 os.replace。"""
        src = (Path(zp.__file__)).read_text(encoding="utf-8")
        self.assertIn("_replace_with_retry(tmp, asar)", src)
        self.assertNotIn("os.replace(tmp, asar)", src)


# ------------------------------------------------------------------ 内核补丁（≤3.11）

class TestKernelPatch(TempCase):
    ANCHOR = zp.ANCHORS["3.11.2"]

    def setUp(self):
        super().setUp()
        self.cjs = self.tmp / "zcode.cjs"
        self.original = make_cjs(self.ANCHOR)
        self.cjs.write_bytes(self.original)

    def patch(self, **kw):
        return quiet(zp.process, self.cjs, False, False, **kw)

    def revert(self, **kw):
        return quiet(zp.process, self.cjs, False, True, **kw)

    def test_patch_then_check_then_revert(self):
        self.assertTrue(self.patch())
        self.assertIn(zp.MARKER.encode(), self.cjs.read_bytes())
        self.assertNotIn(self.ANCHOR.encode(), self.cjs.read_bytes())
        self.assertTrue(quiet(zp.process, self.cjs, True, False))   # --check
        self.assertTrue(self.revert())
        self.assertEqual(self.cjs.read_bytes(), self.original)

    def test_patch_preserves_bytes_and_crlf(self):
        """关键回归：改补丁不能顺手把整个文件的 CRLF 归一为 LF。"""
        self.cjs.write_bytes(make_cjs(self.ANCHOR, newline="\r\n"))
        before = self.cjs.read_bytes()
        crlf_before = before.count(b"\r\n")
        self.assertTrue(self.patch())
        after = self.cjs.read_bytes()
        self.assertGreater(crlf_before, 0)
        self.assertEqual(after.count(b"\r\n"), crlf_before, "CRLF 数量被改变（文本模式读写回归）")
        # 只在锚点处新增字节：前后缀必须原样保留
        self.assertTrue(after.startswith(b"/*pre*/"))
        self.assertTrue(after.endswith(b"/*post*/\r\n"))

    def test_backup_fingerprint_blocks_cross_version_revert(self):
        """升级后（内核被替换）还原必须被拒绝，且不得改动文件。"""
        self.assertTrue(self.patch())
        upgraded = make_cjs(self.ANCHOR.replace("RD", "QQ"), prefix="/*new-kernel*/")
        self.cjs.write_bytes(upgraded)
        self.assertFalse(self.revert(), "跨版本还原竟然成功了")
        self.assertEqual(self.cjs.read_bytes(), upgraded, "拒绝还原时不应改动文件")
        self.assertTrue(self.revert(force=True), "--force 应可强制还原")

    def test_patch_after_upgrade_archives_stale_backup(self):
        """升级到另一个已支持版本后重打：旧备份要归档（改名 .stale-*），并按新内核重建。"""
        self.assertTrue(self.patch())
        upgraded = make_cjs(zp.ANCHORS["3.9.2"], prefix="/*upgraded-kernel*/")
        self.cjs.write_bytes(upgraded)
        self.assertTrue(self.patch())
        stale = list(self.tmp.glob("zcode.cjs.bak.stale-*"))
        self.assertTrue(stale, "旧备份未归档")
        meta = json.loads((self.tmp / "zcode.cjs.bak.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["sha256"], zp._sha256(upgraded), "备份不是升级后内核的原始副本")
        self.assertEqual(meta["patched_sha256"], zp._sha256(self.cjs.read_bytes()))
        self.assertEqual(stale[0].read_bytes(), make_cjs(self.ANCHOR), "归档内容应是旧内核原始副本")

    def test_dry_run_does_not_write(self):
        before = self.cjs.read_bytes()
        self.assertTrue(self.patch(dry_run=True))
        self.assertEqual(self.cjs.read_bytes(), before, "dry-run 竟然写了盘")
        self.assertFalse((self.tmp / "zcode.cjs.bak").exists(), "dry-run 不应创建备份")

    def test_unknown_structure_is_refused(self):
        self.cjs.write_bytes(b"/* totally different kernel */")
        self.assertFalse(self.patch(), "结构未知时必须拒绝盲改")

    def test_native_mechanism_is_reported_as_not_applicable(self):
        """3.14+ 内核（有 optionSpecs、无 defaultVariant）应判为原生机制并跳过。"""
        self.cjs.write_bytes(b"/* kernel with optionSpecs and no legacy symbols */")
        self.assertEqual(zp.detect_reasoning_mechanism(self.cjs), "native")
        self.assertTrue(self.patch(), "原生机制应视为「无需改动」而非失败")

    def test_replacement_contains_helper_and_marker(self):
        patched = zp.replacement_for(self.ANCHOR)
        self.assertIn(zp.MARKER, patched)
        self.assertIn("providerOptionsByLevel", patched)


# ------------------------------------------------------------------ 3.14+ 档位配置

CONFIG_FIXTURE = {
    "provider": {
        "custom-a": {
            "name": "A 网关", "kind": "openai-compatible", "source": "custom",
            "options": {"baseURL": "https://a.example/v1", "apiKey": "sk-secret"},
            "models": {
                "m-variants": {"limit": {"context": 200000, "output": 32000},
                               "reasoning": {"enabled": True, "variants": ["low", "high", "max"],
                                             "defaultVariant": "high"}},
                "m-specs": {"limit": {"context": 100000},
                            "optionSpecs": {"reasoningLevel": {"values": ["off", "max"], "map": "{}"}}},
                "m-plain": {"limit": {"context": 100000}},
            },
        },
        "builtin:zai": {          # 内置模板供应商：不应被处理
            "name": "内置", "kind": "anthropic", "source": "custom",
            "models": {"GLM": {"reasoning": {"enabled": True, "variants": ["low", "max"]}}},
        },
    }
}

PROVIDER_CONFIG_FIXTURE = {
    "schemaVersion": 1,
    "config": {
        "providerConfigRules": {"providerRules": [
            {"providerId": "custom-a", "providerName": "A 网关",
             "config": {"personalModelIds": ["m-variants", "m-specs", "m-plain"]}},
        ]},
        "modelConfigRules": {
            "providerModelRules": [
                {"modelId": "m-variants", "providerId": "custom-a",
                 "config": {"properties": {"contextWindow": 200000}}},
                {"modelId": "m-specs", "providerId": "custom-a",
                 "config": {"properties": {"contextWindow": 100000},
                            "optionSpecs": {"reasoningLevel": {"values": ["off", "max"], "map": "{}"}}}},
            ],
            "manualProviderModelRules": [
                {"modelId": "m-manual", "providerId": "custom-a",
                 "config": {"optionSpecs": {"reasoningLevel": {"values": ["low", "high"], "map": "{}"}}}},
            ],
        },
    },
}


class TestReasoningConfig(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "v2"
        self.root.mkdir()
        (self.root / "config.json").write_text(
            json.dumps(CONFIG_FIXTURE, ensure_ascii=False), encoding="utf-8")
        self.pc_path = self.root / "provider_config.json"
        self.pc_path.write_text(
            json.dumps(PROVIDER_CONFIG_FIXTURE, ensure_ascii=False), encoding="utf-8")
        self.orig_pc = self.pc_path.read_text(encoding="utf-8")

    def run_cfg(self, check=False, revert=False, dry_run=False):
        return quiet(zp.process_reasoning_config, self.root, check, revert, dry_run=dry_run)

    def rules(self):
        pc = json.loads(self.pc_path.read_text(encoding="utf-8"))
        return pc["config"]["modelConfigRules"]["providerModelRules"]

    def rule(self, model_id):
        return next(r for r in self.rules() if r["modelId"] == model_id)

    def test_write_creates_option_specs(self):
        self.assertTrue(self.run_cfg())
        rl = self.rule("m-variants")["config"]["optionSpecs"]["reasoningLevel"]
        # defaultVariant 要排到末位（3.14 约定：values 末位即默认档）
        self.assertEqual(rl["values"], ["low", "max", "high"])
        self.assertIn("reasoning_effort", rl["map"])
        self.assertIn("contextWindow", self.rule("m-variants")["config"]["properties"])

    def test_new_rule_gets_context_window(self):
        """config.json 里存在但 providerModelRules 缺失的模型要新建规则（含 contextWindow）。"""
        pc = json.loads(self.pc_path.read_text(encoding="utf-8"))
        pc["config"]["modelConfigRules"]["providerModelRules"] = [
            r for r in pc["config"]["modelConfigRules"]["providerModelRules"]
            if r["modelId"] != "m-specs"]
        self.pc_path.write_text(json.dumps(pc, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(self.run_cfg())
        self.assertEqual(self.rule("m-specs")["config"]["properties"]["contextWindow"], 100000)

    def test_manual_conflicts_are_skipped(self):
        self.assertTrue(self.run_cfg())
        keys = {(r["providerId"], r["modelId"]) for r in self.rules()}
        manual = json.loads(self.pc_path.read_text(encoding="utf-8"))[
            "config"]["modelConfigRules"]["manualProviderModelRules"]
        manual_keys = {(r["providerId"], r["modelId"]) for r in manual}
        self.assertFalse(keys & manual_keys, "同一模型同时出现在两个规则列表（内核会整表降级）")

    def test_preexisting_duplicate_is_reported_not_silently_ignored(self):
        """已存在的重复声明（同一模型在两个列表）要报警——它是「模型全没了」的成因。"""
        pc = json.loads(self.pc_path.read_text(encoding="utf-8"))
        pc["config"]["modelConfigRules"]["providerModelRules"].append(
            {"modelId": "m-manual", "providerId": "custom-a",
             "config": {"properties": {"contextWindow": 1}}})
        self.pc_path.write_text(json.dumps(pc, ensure_ascii=False), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            zp.process_reasoning_config(self.root, True, False)
        self.assertIn("降级为空", buf.getvalue())

    def test_builtin_provider_untouched(self):
        self.assertTrue(self.run_cfg())
        self.assertFalse([r for r in self.rules() if r["providerId"] == "builtin:zai"])

    def test_models_without_levels_are_skipped(self):
        self.assertTrue(self.run_cfg())
        self.assertFalse([r for r in self.rules() if r["modelId"] == "m-plain"
                          and "optionSpecs" in (r.get("config") or {})])

    def test_idempotent_and_dry_run_and_revert(self):
        self.assertTrue(self.run_cfg(dry_run=True))
        self.assertEqual(self.pc_path.read_text(encoding="utf-8"), self.orig_pc, "dry-run 写了盘")

        self.assertTrue(self.run_cfg())
        snapshot = self.pc_path.read_bytes()
        self.assertTrue(self.run_cfg())                      # 二次运行
        self.assertEqual(self.pc_path.read_bytes(), snapshot, "非幂等：二次运行改动了文件")
        self.assertTrue((self.root / "provider_config.json.reasoning-bak").is_file())

        self.assertTrue(self.run_cfg(revert=True))
        self.assertEqual(self.pc_path.read_text(encoding="utf-8"), self.orig_pc, "还原内容不一致")
        self.assertFalse((self.root / "provider_config.json.reasoning-bak").exists())

    def test_json_written_is_valid_and_atomic(self):
        self.assertTrue(self.run_cfg())
        json.loads(self.pc_path.read_text(encoding="utf-8"))   # 必须是合法 JSON
        self.assertFalse(list(self.root.glob("*.tmp")), "残留了临时文件")

    def test_missing_config_is_reported(self):
        (self.root / "config.json").unlink()
        self.assertFalse(self.run_cfg())


# ------------------------------------------------------------------ sidecar 指纹

class TestSidecarFingerprint(TempCase):
    def setUp(self):
        super().setUp()
        self.asar = self.tmp / "app.asar"
        build_asar(self.asar, {"a.js": b"aaaa", "b.js": b"bbbb"})

    def test_mismatched_size_invalidates_records(self):
        side = self.asar.with_name(self.asar.name + ".chart-patch.json")
        side.write_text(json.dumps({"patches": [
            {"path": "a.js", "offset": 0, "size": 4, "asar_size": 12345, "original_b64": ""},
        ]}), encoding="utf-8")
        self.assertEqual(zp._load_sidecar(side, self.asar.stat().st_size), [])
        self.assertEqual(len(zp._load_sidecar(side)), 1, "不带指纹时不应过滤")

    def test_refresh_updates_byte_level_offsets(self):
        side = self.asar.with_name(self.asar.name + ".chart-patch.json")
        side.write_text(json.dumps({"patches": [
            {"path": "b.js", "offset": 0, "size": 4,
             "asar_size": self.asar.stat().st_size, "original_b64": ""},
        ]}), encoding="utf-8")
        zp._repack_asar(self.asar, {"a.js": b"aaaaaa"}, set())
        quiet(zp._refresh_chart_sidecar, self.asar)
        rec = json.loads(side.read_text(encoding="utf-8"))["patches"][0]
        _, header, data_start = zp._asar_header_raw(self.asar)
        ent = zp._asar_find_entry(header, "b.js")
        self.assertEqual(rec["offset"], data_start + int(ent["offset"]))
        self.assertEqual(rec["asar_size"], self.asar.stat().st_size)

    def test_refresh_updates_repack_level_sidecar_size(self):
        """TPS/滑条/拉取按钮的 sidecar 只记 asar_size，也要跟着重打包更新。"""
        side = self.asar.with_name(self.asar.name + ".tps-patch.json")
        side.write_text(json.dumps({"asar_size": 1, "script_entry": "x.js"}), encoding="utf-8")
        quiet(zp._refresh_chart_sidecar, self.asar)
        self.assertEqual(json.loads(side.read_text(encoding="utf-8"))["asar_size"],
                         self.asar.stat().st_size)


# ------------------------------------------------------------------ 注入段比对

class TestPullerInjectionState(unittest.TestCase):
    def test_main_segment_comparison_ignores_leading_newline(self):
        """回归：注入段以换行开头，比对/剥离必须把它算进去，否则永远误报「版本旧」。"""
        blob = b"var x=1;\n" + zp._models_main_block("j") + \
               b"j.handle(E.SaveMcpToUserDirectory,()=>{});"
        injected, synced, clean, alias = zp._models_main_state(blob)
        self.assertTrue(injected)
        self.assertTrue(synced, "main 注入段被误判为旧版")
        self.assertEqual(alias, "j")
        self.assertNotIn(b"zcode:read-model-config", clean)

    def test_preload_segment_comparison(self):
        blob = b"_.contextBridge.exposeInMainWorld(\"zcode\",{" + \
               zp._models_preload_block("_") + b"other:1});"
        injected, synced, _clean, alias = zp._models_preload_state(blob)
        self.assertTrue(injected and synced)
        self.assertEqual(alias, "_")

    def test_marker_absent_means_not_injected(self):
        blob = b'_.contextBridge.exposeInMainWorld("zcode",{other:1});'
        self.assertEqual(zp._models_preload_state(blob)[:2], (False, False))


class TestGeneratedJs(unittest.TestCase):
    """注入到 main/preload 的 JS 是拼接出来的——必须保证拼出来仍是合法 JS。"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node") or shutil.which("node.exe")
        if not cls.node:
            raise unittest.SkipTest("本机没有 node，跳过注入代码语法校验")

    def _check(self, code: str) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "snippet.js"
            f.write_text(code, encoding="utf-8")
            r = subprocess.run([self.node, "--check", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"生成的 JS 语法错误：{r.stderr[:300]}")

    def test_main_injection_is_valid_js(self):
        code = zp._models_main_block("j").decode()
        self._check(code)
        self.assertIn("renameSync", code, "配置写入应为原子替换")
        self.assertIn("oldOrder", code, "modelOrder 应保序")

    def test_preload_injection_is_valid_js(self):
        self._check("const o={" + zp._models_preload_block("_").decode()
                    + zp._enhance_preload_block("_").decode() + "x:1};")

    def test_enhance_preload_exposes_menu_bridges(self):
        """右键菜单依赖的两个桥方法必须都暴露，且 override 必须能透传。

        缺 `listEnhanceModels` → 菜单打不开；缺 `saveEnhanceModel` → 选了不生效；
        缺 `override` 形参 → 菜单选了但本次点击仍用旧模型（静默失效）。
        """
        code = zp._enhance_preload_block("_").decode()
        self.assertIn("enhancePrompt", code)
        self.assertIn("listEnhanceModels", code)
        self.assertIn("saveEnhanceModel", code)
        self.assertIn("override:ov", code, "enhancePrompt 必须把 override 透传给主进程")
        self.assertIn('zcode:enhance-model-save', code)

    def test_enhance_main_injection_is_valid_js(self):
        code = zp._enhance_main_block("j").decode()
        self._check(code)
        self.assertIn("zcode:enhance-prompt", code)
        self.assertIn("zcode:enhance-model-save", code)
        self.assertIn("chat/completions", code)

    def test_enhance_resolution_never_ships_models_from_disabled_providers(self):
        """★ 回归：跨机「HTTP 400 Model is unavailable」的根因。

        旧逻辑在 ref 档失败后会直接进兜底档，把请求打给「第一个带 baseURL 的
        自定义供应商」—— 与界面所选模型无关，且不检查该供应商是否可用
        （缺 key / 被 systemDisabledReason 禁用），于是 400。
        新逻辑必须先过 usable()（baseURL + apiKey + 未禁用），
        拿不出可用候选时返回可读原因，而不是构造一个注定失败的请求。
        """
        code = zp._enhance_main_block("j").decode()
        self.assertIn("function usable(", code, "必须存在可用性判定")
        self.assertIn("systemDisabledReason", code, "必须排除被系统禁用的供应商")
        # 各档解析都必须走 cand()/usable()，不能有任何一条绕过判定直接 pick
        self.assertIn('cand(pid,mid,"ref")', code)
        self.assertIn('cand(pid,mid,"ref-label")', code)
        self.assertIn('cand(pid,mid,"label")', code)
        self.assertIn('cand(c.pid,mid,"fallback")', code)
        # 0.6.7 新增的两档（右键菜单 explicit / 热配置 config）同样不得绕过可用性判定
        self.assertIn('cand(opid,omid,"explicit")', code,
                      "explicit 档必须经 cand()/usable()，不能无条件采信渲染进程传来的 override")
        self.assertIn('cand(cpid,cmid,"config")', code,
                      "config 档必须经 cand()/usable()，配置指向失效供应商时要能回退")
        self.assertNotIn('pick={pid:pid,mid:mid,p:pp};how="fallback"', code,
                         "兜底档不得绕过可用性判定")
        # 失败时必须给出原因与轨迹，前端才能做友好提示
        self.assertIn('code:"no-model"', code)
        self.assertIn("tried", code)

    def test_enhance_reads_authoritative_provider_config(self):
        """★ 回归：客户端真正发请求用的是 provider_config.json，不是 config.json。

        config.json 是遗留副本，可能滞后（供应商缺失、apiKey 过期、模型列表旧）。
        若只读 config.json，界面选中的 provider/model 可能根本查不到 →
        掉进兜底档 → 打到别的供应商 → HTTP 400 Model is unavailable。
        必须 provider_config.json 优先、config.json 兜底。
        """
        code = zp._enhance_main_block("j").decode()
        # 权威源必须被读取，且顺序在 config.json 之前
        self.assertIn('"provider_config.json"', code, "必须读取 provider_config.json")
        i_auth = code.index('"provider_config.json"')
        i_legacy = code.index('"config.json"')
        self.assertLess(i_auth, i_legacy,
                        "provider_config.json 必须在 config.json 之前读取（后者仅作兜底）")
        # 两个配置文件都要解析出候选
        for key in ("providerConfigRules", "providerRules", "personalModelIds", "modelConfigRules"):
            self.assertIn(key, code, f"provider_config.json 解析缺少 {key}")
        # 降级兜底必须把内置/账号级供应商排到最后（优先用户自定义供应商）
        self.assertIn("builtin:", code)
        self.assertIn("account:", code)
        self.assertRegex(code, r"\.sort\(function\(a,b\)\{",
                         "兜底候选需要排序，用户自定义供应商优先")

    def test_enhance_handler_classifies_errors_and_retries_only_retryable(self):
        """错误必须分类：model/auth/quota 不重试，超时/限流/5xx 才退避重试。"""
        code = zp._enhance_main_block("j").decode()
        self.assertIn("function classify(", code)
        for kind in ('"model"', '"quota"', '"auth"', '"path"', '"rate"', '"server"'):
            self.assertIn(f"kind:{kind}", code, f"缺少错误分类 {kind}")
        self.assertIn("if(!cls.retry)break;", code, "不可重试的错误必须立刻停")
        # 分类命中「模型不可用」的关键词（上游原样透传的英文/中文都要覆盖）
        self.assertIn("model is unavailable", code)
        self.assertIn("model_not_found", code)
        # 返回体要带 tip，前端据此给「可执行的下一步」
        self.assertIn("tip:fc.tip", code)
        self.assertRegex(code, r"for\(let cur of cs\)[\s\S]*setTimeout",
                         "重试前应有退避等待")

    def test_main_segment_matches_regenerated(self):
        """注入 → 状态判定 → 再生成，三段必须逐字节一致（否则每次都会白重写）。"""
        blob = b"var x=1;\n" + zp._models_main_block("j") + b"j.handle(E.SaveMcpToUserDirectory,1);"
        injected, synced, clean, alias = zp._models_main_state(blob)
        self.assertTrue(injected and synced)
        rebuilt = clean[:0] + b"var x=1;\n" + zp._models_main_block(alias) + \
            b"j.handle(E.SaveMcpToUserDirectory,1);"
        self.assertEqual(rebuilt, blob)


# ------------------------------------------------------------------ 真实安装（只读，可选）

def _real_asar() -> Path | None:
    try:
        for cjs in zp.discover():
            asar = cjs.parent.parent / "app.asar"
            if asar.is_file():
                return asar
    except Exception:
        pass
    return None


class TestRealInstall(unittest.TestCase):
    """对本机真实 app.asar 做只读自洽性校验（没装 ZCode 就跳过）。

    这一组能验证「头公式与 Electron 实际产物一致」——纯合成夹具做不到这点。"""

    @classmethod
    def setUpClass(cls):
        cls.asar = _real_asar()
        if cls.asar is None:
            raise unittest.SkipTest("本机未探测到 ZCode 安装")
        cls.raw, cls.header, cls.data_start = zp._asar_header_raw(cls.asar)

    def test_every_entry_integrity_matches_content(self):
        checked = 0
        for p, ent in zp._asar_walk_entries(self.header):
            data = zp._asar_entry_bytes(self.raw, self.data_start, ent)
            self.assertEqual(len(data), int(ent["size"]), f"{p} 长度不符")
            itg = ent.get("integrity")
            if itg:
                self.assertEqual(itg["hash"], zp._sha256(data), f"{p} integrity 与实际内容不符")
            checked += 1
        self.assertGreater(checked, 1000, "真实 asar 条目数异常偏少")

    def test_data_area_is_contiguous(self):
        ends = [self.data_start + int(e["offset"]) + int(e["size"])
                for _, e in zp._asar_walk_entries(self.header)]
        self.assertLessEqual(max(ends), len(self.raw), "条目越过文件末尾")

    def test_version_readable(self):
        self.assertRegex(zp.asar_version(self.asar) or "", r"^\d+\.\d+")


# ------------------------------------------------------------------ 模块符号与注入块

class TestModuleSurface(unittest.TestCase):
    """防止重构时误删符号——本轮就真发生过：整段替换把 _read_asar_header 与
    REASONING_MAP_* 一起删掉，直到跑测试才暴露。"""

    REQUIRED = [
        # asar 基础
        "_read_asar_header", "_asar_header_raw", "_asar_entry_bytes", "_asar_integrity",
        "_asar_walk_entries", "_repack_asar", "_asar_sync_integrity", "_load_sidecar",
        "_refresh_chart_sidecar", "_cleanup_stale_tmp", "_relocate_sidecar",
        # 备份指纹
        "_ensure_backup", "_load_backup_meta", "_mark_backup_patched", "_archive_backup",
        # 内核补丁
        "process", "replacement_for", "extract_anchor", "detect_reasoning_mechanism",
        "ANCHORS", "HELPER", "MARKER",
        # 补丁入口
        "process_usage_chart", "process_model_width", "process_tps_footer",
        "process_thought_slider", "process_model_puller", "process_enhance_prompt",
        "process_reasoning_config", "prune_artifacts", "_process_ipc_patch",
        # 注入块（标记定界）
        "MODELS_BLOCK", "ENHANCE_BLOCK", "_block_mark", "_wrap_block", "_block_span",
        "_bridge_block_state",
        "_models_preload_block", "_models_main_block", "_models_preload_state", "_models_main_state",
        "_enhance_preload_block", "_enhance_main_block", "_enhance_preload_state", "_enhance_main_state",
        "PULLER_SPEC", "ENHANCE_SPEC",
        # 档位映射 / 路径常量
        "REASONING_MAP_OPENAI", "REASONING_MAP_ANTHROPIC", "_desired_levels", "_v2_root",
        "TPS_SCRIPT_PATH", "SLIDER_SCRIPT_PATH", "PULLER_SCRIPT_PATH", "ENHANCE_SCRIPT_PATH",
        "TPS_TAG", "SLIDER_TAG", "PULLER_TAG", "ENHANCE_TAG",
        # --all 一键操作
        "ALL_PATCH_FLAGS", "_expand_all",
    ]

    def test_required_symbols_exist(self):
        missing = [n for n in self.REQUIRED if not hasattr(zp, n)]
        self.assertEqual(missing, [], f"模块缺少符号：{missing}")


class TestAllFlag(unittest.TestCase):
    """--all 是「一条命令体检/全装/全还原」的入口，必须真的覆盖到每一个补丁——
    漏一个就是用户装完发现某个功能没生效，却从汇总表上看不出来。"""

    def test_expand_all_sets_every_patch_flag(self):
        import argparse
        ns = argparse.Namespace(**{name: False for name in zp.ALL_PATCH_FLAGS})
        zp._expand_all(ns)
        off = [n for n in zp.ALL_PATCH_FLAGS if not getattr(ns, n)]
        self.assertEqual(off, [], f"--all 没有打开这些补丁：{off}")

    def test_every_flag_is_a_real_cli_option(self):
        """ALL_PATCH_FLAGS 里写了名字但没加 add_argument → --all 直接 AttributeError。"""
        r = subprocess.run([sys.executable, str(zp.__file__), "--help"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        missing = [n for n in zp.ALL_PATCH_FLAGS
                   if "--" + n.replace("_", "-") not in r.stdout]
        self.assertEqual(missing, [], f"这些补丁名不是真实命令行参数：{missing}")
        self.assertIn("--all", r.stdout)

    def test_all_covers_every_plugin_switch(self):
        """插件开关表（sync.py 的 PATCHES）与 --all 必须一一对应，否则
        「在插件里能开关、但命令行 --all 覆盖不到」这类不一致会悄悄存在。
        例外只有 core_patch：它是无参数的内核补丁（≤3.11 专用）。"""
        import sync
        switch_keys = {key for key, _args, _repack in sync.PATCHES}
        self.assertEqual(switch_keys - {"core_patch"}, set(zp.ALL_PATCH_FLAGS))

    def test_kernel_patch_is_included_by_all(self):
        """内核补丁没有命令行参数，--all 走的是 main() 里的分支条件，
        这里守住「--all 时该分支一定会跑」这个前提。"""
        src = Path(zp.__file__).read_text(encoding="utf-8")
        self.assertIn("if args.all or (not asar_flags and not args.reasoning_config):", src)


class TestMarketplaceManifest(unittest.TestCase):
    """marketplace.json 与 plugin.json 的一致性。ZCode 对这两份清单有两条硬性要求，
    违反了都不会在本地测试里露出来，只会表现成「装不上」或「永远不提示更新」：

    * 市场条目的 name 必须等于插件清单的 name（否则安装直接报
      `Plugin manifest name does not match marketplace entry`）
    * 两处 version 必须同步（「检查更新」拿 marketplace.json 当最新版本、plugin.json 当已安装版本）
    """

    @classmethod
    def setUpClass(cls):
        cls.root = _HERE.parent
        cls.market_path = cls.root / "marketplace.json"
        cls.manifest_path = cls.root / ".zcode-plugin" / "plugin.json"
        if not cls.market_path.is_file() or not cls.manifest_path.is_file():
            raise unittest.SkipTest("非插件形态布局（缺 marketplace.json / .zcode-plugin/plugin.json）")
        cls.market = json.loads(cls.market_path.read_text(encoding="utf-8"))
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    def _entry(self) -> dict:
        for p in self.market.get("plugins", []):
            if p.get("name") == self.manifest["name"]:
                return p
        self.fail(f"marketplace.json 里没有名为 {self.manifest['name']} 的条目")

    def test_entry_name_matches_plugin_manifest(self):
        self.assertEqual(self._entry()["name"], self.manifest["name"])

    def test_versions_are_in_sync(self):
        self.assertEqual(self._entry().get("version"), self.manifest.get("version"),
                         "marketplace.json 与 plugin.json 的 version 必须同步，否则「检查更新」永远不提示")

    def test_source_resolves_to_the_plugin_root(self):
        """source 相对市场根目录解析，必须落在含插件清单的目录上。
        「插件与市场同仓库」时写作 "./"（去掉前缀后为空 = 市场根）。"""
        src = str(self._entry()["source"])
        target = (self.market_path.parent / src[2:] if src.startswith("./") else
                  self.market_path.parent / src).resolve()
        self.assertTrue((target / ".zcode-plugin" / "plugin.json").is_file(),
                        f"source {src!r} 解析到 {target}，那里没有 .zcode-plugin/plugin.json")


class TestCheckStateWording(unittest.TestCase):
    """check_state() 必须认得每一套 `--check` 输出措辞。

    锁死的是一个真实故障：`--reasoning-config --check` 打的是
    「[ ] …（新建规则）」/「[=] 档位配置已是最新，无需写入」，一个
    `已打 / 未打 / 不适用` 都没有 → 判定落回 unknown → sync 报
    「未处理: reasoning_config(状态未知)」，这个开关**既不会被写入也不会被还原**。
    注意不能用「已是最新」判 on：有待写入项时它也会打印（说的是另外 N 个已配好的模型）。
    """

    def _state(self, text: str) -> str:
        import sync

        class _R:
            returncode = 0
            stdout = text
            stderr = ""

        class _FakeSub:
            run = staticmethod(lambda *a, **kw: _R())

        orig = sync.subprocess
        sync.subprocess = _FakeSub          # 只换 sync 里的名字，不动全局 subprocess
        try:
            return sync.check_state(["--reasoning-config"])
        finally:
            sync.subprocess = orig

    def test_reasoning_config_pending_is_off(self):
        out = ("=== 3.14+ 原生档位配置（provider_config.json），模式：检查 ===\n"
               "    [ ] openai/tierflow  →  4 档 off/low/high/max  （新建规则，来源 config.optionSpecs）\n"
               "    已是最新 12 个：a/b, c/d\n")
        self.assertEqual(self._state(out), "off")

    def test_reasoning_config_uptodate_is_on(self):
        out = ("=== 3.14+ 原生档位配置（provider_config.json），模式：检查 ===\n"
               "    已是最新 12 个：a/b, c/d\n"
               "[=] F:\\ZcodeData\\.zcode\\v2\\provider_config.json\n"
               "    档位配置已是最新，无需写入\n")
        self.assertEqual(self._state(out), "on")

    def test_kernel_not_applicable_wins_over_reasoning_branch(self):
        """内核补丁的「不适用」必须优先判成 na（它的提示里也带「原生档位机制」）。"""
        out = "    [i] 该内核使用 3.14+ 原生档位机制（optionSpecs），本补丁不适用："
        self.assertEqual(self._state(out), "na")

    def test_byte_level_wording_still_works(self):
        self.assertEqual(self._state("    [=] a.js | 已打（每日趋势图）"), "on")
        self.assertEqual(self._state("    [ ] a.js | 未打（每日趋势图）"), "off")

    def test_unrecognized_output_is_unknown(self):
        self.assertEqual(self._state("完全看不懂的输出"), "unknown")


class TestScriptInjectStaleDetection(TempCase):
    """`--tps-footer` / `--thought-slider` 的 `--check` 必须能报出「脚本是旧版」。

    锁死的是一个真实故障：`_process_script_inject` 的 check 分支原先只判
    「index.html 有 tag 且脚本条目存在」，**从不比对脚本内容** —— 于是旧脚本也打
    裸「已打」→ `sync.check_state()` 判 on → `run_sync` 走 `continue`（视为已一致）
    → **脚本改了永远不生效，而且一句报错都没有**。

    这与 `--model-puller` 那条链路（会算 old_script 并输出「含旧版组件」）不一致，
    正是同一个「静默失效链」的第三个入口。特征串必须逐字一致：
    `check_state()` 只认「含旧版组件」。
    """

    INDEX = "out/renderer/index.html"
    SCRIPT = "out/renderer/zcode-tps.js"
    TAG = '<script src="./zcode-tps.js"></script>'

    def _build(self, injected: bytes) -> None:
        self.asar = self.tmp / "app.asar"
        build_asar(self.asar, {
            self.INDEX: ("<html><body>" + self.TAG + "</body></html>").encode("utf-8"),
            self.SCRIPT: injected,
        })
        self.src = self.tmp / "zcode-tps.js"
        self.src.write_bytes(b"console.log('new-version');")

    def _check(self) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = zp.process_tps_footer(self.asar, True, False, self.src)
        self.assertTrue(ok)
        return buf.getvalue()

    def test_old_script_is_reported_stale(self):
        self._build(b"console.log('old-version');")
        self.assertIn("含旧版组件", self._check(),
                      "旧版脚本被误报成「已打」→ sync 会永久跳过它，改动永不生效")

    def test_same_script_is_plain_on(self):
        self._build(b"console.log('new-version');")
        out = self._check()
        self.assertNotIn("含旧版组件", out, "脚本同源时不该报 stale（否则每次会话都白跑一次注入）")
        self.assertIn("已打", out)

    def test_missing_source_neither_crashes_nor_lies(self):
        """注入源找不到时只能报结构状态：不能炸，也不能谎报 stale。"""
        self._build(b"console.log('old-version');")
        self.src.unlink()
        out = self._check()
        self.assertIn("已打", out)
        self.assertNotIn("含旧版组件", out)

    def test_slider_uses_the_same_wording(self):
        """滑条与 TPS 共用链路，特征串必须一致，否则只有一项能被 sync 修好。"""
        asar = self.tmp / "app2.asar"
        build_asar(asar, {
            self.INDEX: ("<html><body>" + '<script src="./zcode-thought-slider.js"></script>'
                         + "</body></html>").encode("utf-8"),
            "out/renderer/zcode-thought-slider.js": b"console.log('old');",
        })
        src = self.tmp / "zcode-thought-slider.js"
        src.write_bytes(b"console.log('new');")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(zp.process_thought_slider(asar, True, False, src))
        self.assertIn("含旧版组件", buf.getvalue())


class TestProcessProbeDecodesSafely(unittest.TestCase):
    """进程探测器必须能同时接住 subprocess 返回的 str 和 bytes。

    为什么值得钉死：`_from_running_processes` 用 `errors="replace"` 调 subprocess ——
    这个参数会让 subprocess **直接把输出解码成 str**。而原实现写的是
    `out = subprocess.run(...).stdout or b""` 再 `out.decode(...)`，对 str 调 `.decode()`
    必然抛 `AttributeError`；偏偏外层是 `except Exception: return`，异常被静默吞掉，
    于是探测器**永远命中 0 个候选目录**。

    这个 bug 极难发现：注册表/常见目录两条探测器还在工作，功能「看起来是好的」，
    只是丢掉了最准的那条线索（用户实际在跑哪个安装）。本机因为注册表恰好命中，
    完全看不出来。所以这里直接用假数据喂进去，把「str 也要能处理」钉下来。
    """

    def _run_probe(self, stdout_value):
        import zcode_patcher as zp

        class _FakeProc:
            def __init__(self, out):
                self.stdout = out

        fake = _FakeProc(stdout_value)
        orig_run, orig_nt = zp.subprocess.run, zp.os.name
        zp.subprocess.run = lambda *a, **k: fake
        zp.os.name = "nt"
        try:
            found: list = []
            zp._from_running_processes(found)
            return found
        finally:
            zp.subprocess.run, zp.os.name = orig_run, orig_nt

    def test_handles_str_stdout(self):
        """errors="replace" 时 subprocess 给的就是 str —— 必须能解析出安装目录。"""
        lines = "C:\\WINDOWS\\system32\\ApplicationFrameHost.exe\r\nD:\\ZCode\\ZCode.exe\r\n"
        found = self._run_probe(lines)
        self.assertEqual([p.name for p in found], ["ZCode"],
                         f"str 输出没被解析出来（很可能又是 .decode() 抛异常被吞了）：{found}")

    def test_handles_bytes_stdout(self):
        """没传 errors 时仍是 bytes —— 老路径也不能回归。"""
        lines = b"D:\\ZCode\\ZCode.exe\r\nC:\\Other\\notepad.exe\r\n"
        found = self._run_probe(lines)
        self.assertEqual([p.name for p in found], ["ZCode"])

    def test_non_zcode_paths_are_ignored(self):
        found = self._run_probe("C:\\WINDOWS\\explorer.exe\n/usr/bin/python\n")
        self.assertEqual(found, [])


class TestNoConsoleWindowFlags(unittest.TestCase):
    """每个会起 console 子进程的调用都必须带「别弹控制台窗口」的标志。

    为什么值得钉死：钩子用 `--detach` 把 sync.py 拉成 `DETACHED_PROCESS|CREATE_NO_WINDOW`
    的后台 worker —— 也就是**没有控制台**。Windows 的语义是「无控制台的父进程创建 console
    子进程时，系统会新建一个控制台并**显示**出来」，于是 worker 里每调一次
    `check_state()` 就闪一个 cmd 窗口；而 `run_sync()` 会为每个开关调一次 —— 一个会话能弹 8 个以上。

    实测（本机探针，EnumWindows 统计可见控制台窗口数）：无控制台父进程
      * 不传 creationflags → 期间出现 **2 个**可见控制台窗口
      * 传 `CREATE_NO_WINDOW` → **0 个**
    注意 `capture_output=True` 挡不住它 —— 它管的是管道，不是控制台分配。

    `apply_after_exit.py` 早就知道这个坑（注释里写着「pythonw 无控制台…会每次新弹 cmd 窗口」），
    但 sync.py / zcode_patcher.py / doctor.py 漏了。这条测试就是防它再漏。

    允许两种写法：`**no_window_kwargs()`（推荐）或 `creationflags=...`。
    确实不需要的调用，在调用处同一行写 `# no-window-ok: <理由>` 显式豁免。
    """

    @staticmethod
    def _mask(src: str) -> str:
        """把注释与字符串**内容**替换成空格（保持长度与行号不变）。

        必须屏蔽：`_console.py` 的文档里就写着 `subprocess.run(capture_output=True)` 作为例子，
        不屏蔽会把它当成真实调用误报。标记 `# no-window-ok:` 是注释，所以判定时要用原文。
        """
        import io as _io
        import tokenize
        starts = [0]
        for ln in src.splitlines(keepends=True):
            starts.append(starts[-1] + len(ln))

        def off(pos):
            row, col = pos
            return starts[row - 1] + col if row - 1 < len(starts) else len(src)

        buf = list(src)
        try:
            for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
                if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                    continue
                for i in range(off(tok.start), min(off(tok.end), len(buf))):
                    if buf[i] != "\n":
                        buf[i] = " "
        except Exception:
            return src
        return "".join(buf)

    def _calls(self, masked: str, original: str):
        """找出所有 subprocess.run/Popen 调用：返回 (行号, 调用原文, 调用后同行尾巴)。"""
        found = []
        for m in re.finditer(r"subprocess\.(?:run|Popen)\(", masked):
            i, depth = m.end(), 1
            while i < len(masked) and depth:
                if masked[i] == "(":
                    depth += 1
                elif masked[i] == ")":
                    depth -= 1
                i += 1
            line_end = masked.find("\n", i)
            line_end = len(masked) if line_end == -1 else line_end
            found.append((masked[:m.start()].count("\n") + 1,
                          masked[m.start():i],            # 已屏蔽，用于判定
                          original[i:line_end]))          # 原文，用于读豁免标记
        return found

    def test_every_subprocess_call_suppresses_the_console_window(self):
        d = _HERE.parent / "skills" / "zcode-tokenspeed" / "scripts"
        if not d.is_dir():
            self.skipTest("非插件形态布局")
        offenders = []
        for p in sorted(d.glob("*.py")):
            src = p.read_text(encoding="utf-8")
            masked = self._mask(src)
            for line, text, tail in self._calls(masked, src):
                if "no_window_kwargs" in text or "creationflags" in text:
                    continue
                if "no-window-ok" in tail:
                    continue
                offenders.append("%s:%d  %s" % (p.name, line, text.splitlines()[0][:72]))
        self.assertEqual(
            offenders, [],
            "这些子进程调用会弹出可见控制台窗口，请加 **no_window_kwargs()"
            "（或在不必要时写 # no-window-ok: 理由）：\n  " + "\n  ".join(offenders))

    def test_no_window_helper_is_a_noop_off_windows(self):
        """helper 在非 Windows 上必须返回空 dict，否则会污染别的平台。"""
        import _console
        self.assertEqual(set(_console.no_window_kwargs()),
                         {"creationflags"} if os.name == "nt" else set())


class TestInjectionBlocks(unittest.TestCase):
    """标记定界注入块：模型拉取与增强提示词共用 preload/main 锚点，必须互不干扰。"""

    def test_block_roundtrip(self):
        body = b"readConfigFile:()=>x,"
        blob = b"prefix" + zp._wrap_block("demo", body) + b"suffix"
        span = zp._block_span(blob, "demo")
        self.assertIsNotNone(span)
        i, j = span
        self.assertEqual(blob[i:j], zp._wrap_block("demo", body))
        self.assertEqual(blob[:i] + blob[j:], b"prefixsuffix")
        self.assertIsNone(zp._block_span(blob, "not-there"))

    def test_two_blocks_coexist_and_strip_independently(self):
        models = zp._models_preload_block("_")
        enhance = zp._enhance_preload_block("_")
        blob = (b'_.contextBridge.exposeInMainWorld("zcode",{' + models + enhance + b"other:1});")
        for block_id, want in ((zp.MODELS_BLOCK, models), (zp.ENHANCE_BLOCK, enhance)):
            span = zp._block_span(blob, block_id)
            self.assertIsNotNone(span, block_id)
            self.assertEqual(blob[span[0]:span[1]], want, block_id)
        # 摘掉 models 块后，enhance 块内容必须原样保留
        s = zp._block_span(blob, zp.MODELS_BLOCK)
        stripped = blob[:s[0]] + blob[s[1]:]
        self.assertIsNone(zp._block_span(stripped, zp.MODELS_BLOCK))
        e = zp._block_span(stripped, zp.ENHANCE_BLOCK)
        self.assertIsNotNone(e)
        self.assertEqual(stripped[e[0]:e[1]], enhance)

    def test_main_blocks_coexist(self):
        models = zp._models_main_block("j")
        enhance = zp._enhance_main_block("j")
        blob = models + enhance + b"j.handle(E.SaveMcpToUserDirectory,1);"
        inj, synced, clean, alias = zp._models_main_state(blob)
        self.assertTrue(inj and synced, "共用锚点时 models 块仍应判定为已同步")
        self.assertNotIn(b"read-model-config", clean)
        self.assertIn(zp._enhance_main_block(alias), clean)

    def test_legacy_preload_format_is_migrated(self):
        """老版本（无标记定界）注入段应判为「存在但需更新」，且能被安全剥离。"""
        legacy = (b'_.contextBridge.exposeInMainWorld("zcode",{'
                  b'readConfigFile:()=>_.ipcRenderer.invoke("zcode:read-model-config"),'
                  b'writeConfigFile:t=>_.ipcRenderer.invoke("zcode:write-model-config",t),'
                  b'fetchModelsFromUrl:(t,n)=>_.ipcRenderer.invoke("zcode:fetch-models-from-url",'
                  b'{baseUrl:t,apiKey:n}),other:1});')
        inj, synced, clean, _alias = zp._models_preload_state(legacy)
        self.assertTrue(inj)
        self.assertFalse(synced, "老格式应被判为需要更新（迁移到标记定界）")
        self.assertNotIn(b"read-model-config", clean)
        self.assertIn(b"other:1", clean)

    def test_enhance_block_absent_when_not_injected(self):
        blob = b'_.contextBridge.exposeInMainWorld("zcode",{other:1});'
        self.assertEqual(zp._enhance_preload_state(blob)[:2], (False, False))


class TestSliderScript(unittest.TestCase):
    """滑条注入脚本：能加载 + 注释里承诺的调试接口真实存在。

    这类「注释写了、代码里没有」的漂移肉眼 review 看不出来，`node --check` 也照样通过
    （语法完全合法）——历史上 window.__zsliderCtl 就只存在于注释里。所以这里用最小 DOM 桩
    把脚本真跑一遍，再核对接口成员。
    """

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node") or shutil.which("node.exe")
        if not cls.node:
            raise unittest.SkipTest("本机没有 node，跳过滑条脚本检查")
        cls.smoke = _HERE / "slider_smoke.js"
        cls.script = None
        for cand in (_HERE.parent / "scripts",
                     _HERE.parent / "skills" / "zcode-tokenspeed" / "scripts"):
            p = cand / "zcode-thought-slider.js"
            if p.is_file():
                cls.script = p
                break
        if cls.script is None or not cls.smoke.is_file():
            raise unittest.SkipTest("未找到滑条脚本或冒烟脚本")

    def _node(self, *args):
        return subprocess.run([self.node, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def test_slider_script_is_valid_js(self):
        r = self._node("--check", str(self.script))
        self.assertEqual(r.returncode, 0, f"滑条脚本语法错误：{r.stderr[:300]}")

    def test_slider_loads_and_exposes_control_api(self):
        r = self._node(str(self.smoke), str(self.script))
        self.assertEqual(r.returncode, 0, f"冒烟测试失败：{(r.stdout + r.stderr)[:400]}")
        self.assertIn("smoke OK", r.stdout)

    def test_smoke_without_argument_fails_helpfully(self):
        """缺参数时必须给「用法」提示并退 2，而不是把 readFileSync 的裸堆栈甩给用户。

        这个坑真实发生过：直接 `node tests/slider_smoke.js` 会看到
        `TypeError: The "path" argument must be of type string... Received undefined`，
        完全看不出缺的是「被测脚本路径」——排查成本全在不必要的猜测上。
        """
        r = self._node(str(self.smoke))
        self.assertEqual(r.returncode, 2, "缺参数应以退出码 2 结束（区别于测试失败）")
        self.assertIn("用法", r.stderr + r.stdout)
        self.assertNotIn("ERR_INVALID_ARG_TYPE", r.stderr,
                         "不该把 Node 的裸异常甩出来")

    def test_smoke_with_missing_file_fails_helpfully(self):
        """文件不存在时也要友好报错，而不是 traceback。"""
        r = self._node(str(self.smoke), str(self.smoke.parent / "no-such-file.js"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("读不到被测脚本", r.stderr + r.stdout)


class TestEnhancePromptScript(unittest.TestCase):
    """润色按钮的挂载逻辑：按钮既不能渲染到会话消息区，也不能跑到输入框左上角。

    历史 bug ①：`findInput()` 在**整个 document** 上按 `textarea` /
    `[contenteditable='true']` 这类通用选择器找「交互输入框」。但客户端的会话消息区
    （`[data-v4-timeline-scroll]` 内）也会出现这类节点，一旦命中就会：
      ① 把消息区元素误认成输入框 → 读写正文全错；
      ② 用它推导挂载点 → 按钮被插进消息流 → 表现为「按钮跑出输入框」。
    而且输入框 dock（`[data-v4-composer-dock]`）与消息层是**同级兄弟**，
    都位于滚动容器内，dock 仅靠 `sticky bottom-0` 贴底，所以插错位置后
    会随消息增长被推到列表底部。
    修法：所有查找先锚定 dock；兼容模式禁用会误伤消息区的通用选择器。

    历史 bug ②（1.4 修）：挂载点只认「发送按钮的父节点」，取不到就退回
    **卡片 / dock 本体的 firstChild** —— 那两处都是输入框的祖先，等价于把图标钉在
    输入框左上角。而发送按钮并非恒存在：内核里提交控件是
    `sn = canStop && !hasContent ? 停止按钮 : 发送按钮`，所以「输入框为空 + 会话进行中」
    时它被 `[data-testid='v4-stop']` 替换 → 图标跑到左上角。
    修法：三级解析（右侧操作区 → 发送/停止按钮父节点 → 工具栏行贴行尾），
    任何一级都不退回卡片/dock；解析失败保持原位。
    `tests/enhance_mount_smoke.js` 用最小 DOM 桩把这几个状态真跑一遍。
    """

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node") or shutil.which("node.exe")
        cls.smoke = _HERE / "enhance_mount_smoke.js"
        cls.script = None
        for cand in (_HERE.parent / "scripts",
                     _HERE.parent / "skills" / "zcode-tokenspeed" / "scripts"):
            p = cand / "zcode-enhance-prompt.js"
            if p.is_file():
                cls.script = p
                break
        if cls.script is None:
            raise unittest.SkipTest("未找到润色脚本")

    def setUp(self):
        self.src = self.script.read_text(encoding="utf-8")

    def _node(self, *args):
        return subprocess.run([self.node, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def test_mount_smoke_passes_in_all_composer_states(self):
        """把「待机 / 输入框为空 / 会话进行中（发送按钮被停止按钮替换）」等
        状态真跑一遍，按钮必须始终落在工具栏右侧操作区。"""
        if not self.node:
            self.skipTest("本机没有 node，跳过挂载冒烟测试")
        if not self.smoke.is_file():
            self.skipTest("未找到 enhance_mount_smoke.js")
        r = self._node(str(self.smoke), str(self.script))
        self.assertEqual(r.returncode, 0,
                         f"挂载冒烟测试失败：{(r.stdout + r.stderr)[:600]}")
        self.assertIn("enhance mount smoke OK", r.stdout)

    def test_mount_smoke_without_argument_fails_helpfully(self):
        """缺参数要给「用法」提示并退 2，而不是把 readFileSync 的裸堆栈甩出来。"""
        if not self.node:
            self.skipTest("本机没有 node，跳过挂载冒烟测试")
        if not self.smoke.is_file():
            self.skipTest("未找到 enhance_mount_smoke.js")
        r = self._node(str(self.smoke))
        self.assertEqual(r.returncode, 2, "缺参数应以退出码 2 结束（区别于测试失败）")
        self.assertIn("用法", r.stderr + r.stdout)
        self.assertNotIn("ERR_INVALID_ARG_TYPE", r.stderr, "不该把 Node 的裸异常甩出来")

    def test_scopes_input_lookup_to_composer_dock(self):
        """必须先解析 dock，再在 dock 内找输入框。"""
        self.assertIn("function findDock(", self.src, "必须存在 dock 解析函数")
        self.assertIn("data-v4-composer-dock", self.src,
                      "dock 锚点应使用客户端的 data-v4-composer-dock")
        # findInput 内必须先拿到 dock，并用 dock.querySelectorAll 而不是 document
        body = self.src[self.src.index("function findInput("):]
        body = body[:body.index("\n  function readText")]
        self.assertIn("findDock()", body, "findInput 必须先锚定 dock")
        self.assertIn("dock.querySelectorAll", body, "输入框只在 dock 内查找")
        self.assertLess(body.index("dock.querySelectorAll"),
                        body.index("document.querySelectorAll"),
                        "dock 内查找必须排在全局查找之前")

    def test_fallback_never_uses_generic_selectors(self):
        """兼容模式的全局查找必须排除会误伤消息区的通用选择器。"""
        body = self.src[self.src.index("function findInput("):]
        body = body[:body.index("\n  function readText")]
        # 兼容分支里必须把 textarea / contenteditable 挡掉
        self.assertIn('sel === "textarea"', body)
        self.assertIn('[contenteditable=', body)
        self.assertIn("continue", body)
        # COMPOSER_INPUT_SELECTORS 里仍保留这些选择器（供 dock 内查找使用），
        # 但整个 document 直接用它就是 bug
        self.assertIn("COMPOSER_INPUT_SELECTORS", body)

    def test_host_never_falls_back_to_card_or_dock(self):
        """挂载点绝不能退回卡片 / dock 本体 —— 那是输入框的祖先。

        历史 bug（0.6.6 修）：旧实现只认 `[data-testid='v4-composer-send']` 当锚点，
        取不到就 `dock.querySelector("[data-testid='v4-composer']") || … || dock`，
        再 `host.insertBefore(btn, host.firstChild)` —— 于是图标被钉在**输入框左上角**。
        而发送按钮并非恒存在：输入框为空 + 会话进行中时，内核用
        `[data-testid='v4-stop']`（停止）替换它（`sn = canStop && !hasContent`），
        所以「输入框为空」和「会话进行中」两种情况都会复现。
        """
        body = self.src[self.src.index("function ensureButton("):]
        body = body[:body.index("\n  function start(")]
        # 不得再出现「越界就回退到 dock 本身」这种兜底
        self.assertNotIn("host = dock;", body,
                         "挂载点不得回退到 dock 本体（= 输入框左上角）")
        self.assertNotIn("!dock.contains(host)", body,
                         "不应再用「越界回退 dock」的旧校验")
        # 解析失败时必须保持原位 / 不挂，而不是搬到某个大容器
        self.assertIn("未找到工具栏操作区", body)
        # 锚点解析集中在 findMount()，且同时覆盖发送与停止两种按钮
        self.assertIn("'v4-composer-send'", self.src, "兜底要认发送按钮")
        self.assertIn("'v4-stop'", self.src,
                      "生成中发送按钮会被停止按钮替换，必须一并认作锚点")
        mount = self.src[self.src.index("function findMount("):]
        mount = mount[:mount.index("\n  /** 落位")]
        self.assertIn("TRAILING_SELECTOR", mount, "首选锚点应是右侧操作区")
        self.assertIn("ANCHOR_BUTTON_SELECTORS", mount, "二级锚点是提交控件容器")
        self.assertIn('where: "append"', mount, "工具栏行兜底只能追加到行尾")
        self.assertIn("appendChild", self.src, "行尾追加用 appendChild")

    def test_button_self_heals_when_detached_from_dock(self):
        """客户端重渲染会把按钮搬走 —— 必须能自动搬回来（且记账到 diag）。"""
        body = self.src[self.src.index("function ensureButton("):]
        body = body[:body.index("\n  function start(")]
        self.assertIn("stillInDock", body, "需要判断按钮是否已脱离 dock")
        self.assertIn("reattaches", body, "自愈次数要记入诊断，便于线上确认")
        # 自愈分支：先记账，再按解析出的方式搬回，最后 return（不重复创建按钮）
        m = re.search(
            r"if \(!okPlace \|\| !stillInDock\) \{([\s\S]*?)\}", body)
        self.assertIsNotNone(m, "找不到「位置不对就搬回」的分支")
        branch = m.group(1)
        self.assertIn("reattaches", branch, "自愈分支要记账")
        self.assertIn("place(btn, host, mount.where)", branch,
                      "自愈分支要把按钮搬回挂载点")
        # 已连接的按钮分支不得重新 createElement（否则会重复插入）
        self.assertNotIn("createElement", branch, "自愈分支不应重建按钮")

    def test_place_prepends_into_right_side_action_group(self):
        """落位规则：右侧操作区用 prepend（图标落在发送按钮左侧 = 既有正确位置），
        只有兜底的工具栏行才 append（贴行尾）。prepend 绝不能作用在行容器上。"""
        body = self.src[self.src.index("function place("):]
        body = body[:body.index("\n  // ---------- 按钮 ----------")]
        self.assertIn('where === "append"', body)
        self.assertIn("appendChild", body)
        self.assertIn("insertBefore(el, host.firstChild)", body)

    def test_no_unscoped_generic_query_remains(self):
        """全局 document 查询里不得再出现裸 textarea / contenteditable。"""
        for bad in ('document.querySelectorAll("textarea")',
                    "document.querySelectorAll('[contenteditable='",
                    'document.querySelectorAll("form textarea")'):
            self.assertNotIn(bad, self.src,
                             f"存在会误伤消息区的全局查询：{bad}")

    def test_mount_poll_is_tight_enough_for_streaming(self):
        """流式输出时消息持续增长，轮询周期必须够短，否则按钮会肉眼可见地错位。"""
        m = re.search(r"setInterval\(\(\) => \{ if \(!document\.hidden\) ensureButton\(\); \}, (\d+)\)",
                      self.src)
        self.assertIsNotNone(m, "找不到挂载轮询")
        self.assertLessEqual(int(m.group(1)), 1000,
                             "轮询周期过长：流式对话中按钮会长时间停在错误位置")


class TestDoctor(unittest.TestCase):
    """doctor.py 是「插件装了没生效」时的第一入口。它靠一批常量去定位安装目录、
    配置键和开关表——这些常量一旦和真实实现漂移，自检报告会指向错误的目录，
    比没有自检更误导。所以这里把它们和 plugin.json / sync.py 对齐钉死。"""

    def test_plugin_name_matches_manifest(self):
        import doctor
        manifest = json.loads((_HERE.parent / ".zcode-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(doctor.PLUGIN_NAME, manifest["name"],
                         "doctor 按 PLUGIN_NAME 前缀找安装目录与配置键，必须等于清单里的 name")

    def test_patch_key_table_covers_every_switch(self):
        import doctor
        import sync
        self.assertEqual({k for k, _label in doctor.PATCH_KEYS} | {"core_patch"},
                         {k for k, _args, _repack in sync.PATCHES},
                         "doctor 的开关表漏项 → 自检报告会漏掉某个功能的状态")

    def test_repack_set_matches_sync(self):
        """doctor 用这个集合区分「重打包级」补丁，必须与 sync.PATCHES 的第三列一致。"""
        import doctor
        import sync
        self.assertEqual(doctor.REPACK_KEYS, {k for k, _a, r in sync.PATCHES if r})

    def test_prefix_entries_only_matches_this_plugin(self):
        import doctor
        cfg = {"plugins": {
            "options": {"zcode-tokenspeed@some-market": {"tps_footer": True},
                        "other-plugin@m": {"x": 1}},
            "enabledPlugins": {"zcode-tokenspeed@some-market": True},
        }}
        self.assertEqual(list(doctor._prefix_entries(cfg, "options")),
                         ["zcode-tokenspeed@some-market"])
        self.assertEqual(list(doctor._prefix_entries(cfg, "enabledPlugins")),
                         ["zcode-tokenspeed@some-market"])
        # 缺失 / 类型异常都不能抛异常（config.json 是用户可手改的文件）
        self.assertEqual(doctor._prefix_entries({}, "options"), {})
        self.assertEqual(doctor._prefix_entries({"plugins": {"options": []}}, "options"), {})
        self.assertEqual(doctor._prefix_entries(None, "options"), {})

    def test_manifest_version_reads_both_layouts(self):
        import doctor
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "x", "version": "1.2.3"}), encoding="utf-8")
            self.assertEqual(doctor._manifest_version(root), "1.2.3")
            self.assertEqual(doctor._manifest_version(root / "nope"), "?")

    def test_json_mode_emits_parseable_report(self):
        """--json 是让用户「贴给别人看」的输出，必须是合法 JSON 且包含关键字段。"""
        import doctor
        r = subprocess.run([sys.executable, str(doctor.__file__), "--json"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        data = json.loads(r.stdout)
        for key in ("python", "plugin_dirs", "enabled", "options_saved", "hook_fired"):
            self.assertIn(key, data)


class TestSyncHeartbeat(unittest.TestCase):
    """心跳文件 _sync.last 是「钩子到底跑没跑」的唯一证据：
    没拨过开关时 sync 什么都不做、日志也是空的，「钩子没触发」与「触发了但无事可做」
    在日志里长得一模一样。所以必须保证它在任何一条提前返回的路径上都被写出来。"""

    def setUp(self):
        import sync
        self.sync = sync
        self._orig = (sync.CONFIG, sync.STAMP, sync.LOG, sync.MARKER)
        self._tmp = tempfile.TemporaryDirectory(prefix="zpatch-hb-", ignore_cleanup_errors=True)
        d = Path(self._tmp.name)
        sync.CONFIG = d / "config.json"       # 故意不存在
        sync.STAMP = d / "_sync.last"
        sync.LOG = d / "_sync.log"
        sync.MARKER = d / "_autoinject.done"  # 别把标记写进真实安装目录

    def tearDown(self):
        (self.sync.CONFIG, self.sync.STAMP, self.sync.LOG,
         self.sync.MARKER) = self._orig
        self._tmp.cleanup()

    def test_beat_writes_timestamp_and_message(self):
        self.sync.beat("单元测试")
        text = self.sync.STAMP.read_text(encoding="utf-8")
        self.assertIn("单元测试", text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_main_beats_when_config_is_missing(self):
        """从未点过「保存配置」时，心跳必须写明这次是按**插件清单默认值**自动注入的。

        这正是「安装成功但不生效」的根因：ZCode 只在用户点过保存之后才写
        `plugins.options`，旧逻辑把「没表态」当成「不要做」，于是装完重启什么都没发生，
        而用户在界面上看不出任何原因。现在退回清单默认值，装完即自动注入。
        """
        with patcher_stubbed(self.sync) as calls:
            quiet(self.sync.main)
        text = self.sync.STAMP.read_text(encoding="utf-8")
        self.assertTrue(text.strip(), "心跳文件不能为空")
        self.assertIn("从未保存过开关", text)
        self.assertEqual(calls, [], "开关都已是目标状态时不应产生任何写入")

    def test_main_beats_when_nothing_to_do(self):
        """配置存在但所有开关都已一致时，也要留下心跳并写明「无需改动」。"""
        self.sync.CONFIG.write_text(json.dumps(
            {"plugins": {"options": {"zcode-tokenspeed@m": {}}}}), encoding="utf-8")
        with patcher_stubbed(self.sync):
            quiet(self.sync.main)
        text = self.sync.STAMP.read_text(encoding="utf-8")
        self.assertTrue(text.strip(), "心跳文件不能为空")


class TestSyncModes(unittest.TestCase):
    """钩子必须是「登记心跳 + 后台化」就立刻返回。

    为什么：hook 是**内联**执行的（`async` 字段当前无运行时效果），而同步一次要跑多次
    `--check`（每次约 2 秒）。同步做完再返回会把会话启动硬生生拖住，还可能撞上钩子的
    超时上限被砍掉——表现就是「什么都没发生」。所以 --detach 必须只做两件事：写心跳、
    拉起后台进程，然后立刻退出。
    """

    def setUp(self):
        import sync
        self.sync = sync
        self._orig = (sync.CONFIG, sync.STAMP, sync.LOG, sync.MARKER, list(sys.argv))
        self._tmp = tempfile.TemporaryDirectory(prefix="zpatch-mode-", ignore_cleanup_errors=True)
        d = Path(self._tmp.name)
        sync.CONFIG = d / "config.json"       # 故意不存在
        sync.STAMP = d / "_sync.last"
        sync.LOG = d / "_sync.log"
        sync.MARKER = d / "_autoinject.done"  # 别把标记写进真实安装目录

    def tearDown(self):
        (self.sync.CONFIG, self.sync.STAMP, self.sync.LOG,
         self.sync.MARKER) = self._orig[:4]
        sys.argv[:] = self._orig[4]
        self._tmp.cleanup()

    def _run(self, *args):
        sys.argv[:] = ["sync.py", *args]
        quiet(self.sync.main)

    def test_detach_spawns_worker_and_returns(self):
        spawned = []
        orig = self.sync.spawn_detached
        self.sync.spawn_detached = lambda extra: (spawned.append(extra), True)[1]
        try:
            with patcher_stubbed(self.sync):
                self._run("--detach")
        finally:
            self.sync.spawn_detached = orig
        self.assertEqual(spawned, [["--worker", "--from-hook"]])
        self.assertIn("[钩子]", self.sync.STAMP.read_text(encoding="utf-8"))

    def test_detach_falls_back_to_foreground_when_spawn_fails(self):
        """后台起不来（比如被安全软件拦）时必须自己把活干完，而不是静默失败。"""
        orig = self.sync.spawn_detached
        self.sync.spawn_detached = lambda extra: False
        try:
            with patcher_stubbed(self.sync):
                self._run("--detach")
        finally:
            self.sync.spawn_detached = orig
        text = self.sync.STAMP.read_text(encoding="utf-8")
        self.assertTrue(text.strip(), "兜底路径也必须留下心跳")
        self.assertIn("从未保存过开关", text)

    def test_worker_records_the_full_chain(self):
        """心跳要能证明「钩子 → 后台」这条链，否则看不出是钩子拉起来的。"""
        with patcher_stubbed(self.sync):
            self._run("--worker", "--from-hook")
        self.assertIn("[钩子→后台]", self.sync.STAMP.read_text(encoding="utf-8"))

    def test_hook_mode_never_writes_to_stdout(self):
        """钩子的 stdout 会被按严格 JSON schema 校验：输出非 JSON 会被判为「运行失败」。
        脚本副作用虽已生效，但日志里会留下假故障，所以后台路径必须一声不吭。"""
        buf = io.StringIO()
        with patcher_stubbed(self.sync):
            with contextlib.redirect_stdout(buf):
                self.sync.run_sync(echo=False)
        self.assertEqual(buf.getvalue(), "")


class TestSyncStaleIsReinjected(unittest.TestCase):
    """★ 回归：「插件更新了，但补丁没跟着更新」这一类**静默失效**（0.6.1 修复）。

    真实故障现场（用户报的：换了台电脑从市场更新插件、退出重启，润色还是不生效）：
      1. 插件市场「更新」只替换**插件目录**，**不会**重新注入 `app.asar`；
      2. 此时 asar 里躺的是上一版的注入片段；
      3. `--check` 输出「增强提示词注入: **已打**（含旧版组件，重跑可自动更新）」；
      4. `check_state()` 只做子串匹配，看到「已打」就返回 `on`；
      5. `run_sync()` 判 `want=True, state=on` → `continue`（视为已一致）；
      6. 结果：**永远不会重跑注入**，用户等多久都不生效，而且日志里一句
         「未处理」都没有 —— 只有一个彻底静默的「无事可做」。

    这个坑的恶劣之处是它**不报错**：心跳正常、日志正常、开关全开，界面也显示
    「已打」，唯一的表现是「修复没生效」。所以必须把 stale 态单独测出来。
    """

    def setUp(self):
        import sync
        self.sync = sync
        self._orig = (sync.CONFIG, sync.STAMP, sync.LOG, sync.MARKER)
        self._tmp = tempfile.TemporaryDirectory(prefix="zpatch-stale-", ignore_cleanup_errors=True)
        d = Path(self._tmp.name)
        sync.CONFIG = d / "config.json"        # 不存在 → 走插件清单默认值（全开）
        sync.STAMP = d / "_sync.last"
        sync.LOG = d / "_sync.log"
        sync.MARKER = d / "_autoinject.done"

    def tearDown(self):
        (self.sync.CONFIG, self.sync.STAMP, self.sync.LOG,
         self.sync.MARKER) = self._orig
        self._tmp.cleanup()

    # ---------- check_state 必须把「内容旧」和「已最新」分开 ----------

    def test_check_state_maps_old_components_to_stale_not_on(self):
        """`--check` 说「含旧版组件」时必须是 stale，不能是 on。

        这一条盯着的是一个**判断顺序**陷阱：`--check` 的两种措辞
        「已打（含旧版组件，重跑可自动更新）」与「已打（四组件均为当前版本）」**都含「已打」**。
        只要 `if "已打" in out` 排在 `if "含旧版组件" in out` 前面，
        stale 分支就永远走不到 —— 而这正是修复前的状态。
        """
        sync = self.sync
        samples = {
            "已打（含旧版组件，重跑可自动更新）": "stale",
            "已打": "on",
            "未打": "off",
            "本补丁不适用": "na",
            "完全看不懂的措辞": "unknown",
        }
        for text, want in samples.items():
            with self.subTest(text=text):
                orig_run = sync.subprocess.run

                class _R:
                    returncode = 0
                    stdout = text
                    stderr = ""

                sync.subprocess.run = lambda *a, **k: _R()
                try:
                    self.assertEqual(sync.check_state(["--enhance-prompt"]), want,
                                     f"{text!r} 应判为 {want}")
                finally:
                    sync.subprocess.run = orig_run

    def test_stale_branch_is_checked_before_generic_da(self):
        """源码层面再钉一次顺序：`含旧版组件` 必须出现在裸 `已打` 之前。

        上面那条靠打桩喂字符串，能验证语义；这条直接查源码顺序，
        防止以后有人「顺手」把两个 if 调换回来（调换后单测仍会红，
        但这条给出的报错更直指根因）。
        """
        import inspect
        src = inspect.getsource(self.sync.check_state)
        self.assertIn("含旧版组件", src)
        i_stale = src.index("含旧版组件")
        # 找裸 "已打" 判断（排除 "未打" 里不含此串、以及 stale 注释里的提及）
        i_plain = src.index('if "已打" in out')
        self.assertLess(i_stale, i_plain,
                        '判断顺序反了：`if "已打" in out` 会先命中，stale 永远走不到')

    # ---------- run_sync 必须真的重新安排注入 ----------

    def test_stale_repack_patch_is_rescheduled_for_watchdog(self):
        """重打包级补丁（enhance/puller）stale 时必须转交看护，而不是被跳过。"""
        states = {("--enhance-prompt",): "stale"}
        with patcher_stubbed(self.sync, states=states) as calls:
            summary = quiet(self.sync.run_sync)
        watchdogs = [c for c in calls if c[0] == "watchdog"]
        self.assertEqual(len(watchdogs), 1, f"必须挂载看护，实际调用：{calls}")
        self.assertTrue(watchdogs[0][1].get("enhance_prompt"),
                        "看护的 --want 里必须带上 enhance_prompt")
        self.assertIn("重注入为新版", summary,
                      "摘要要说清「插件已更新、退出时重注入」，不能静默")

    def test_stale_non_repack_patch_is_applied_immediately(self):
        """非重打包级补丁（usage_chart 等）stale 时应当**当场**重跑。"""
        states = {("--usage-chart",): "stale"}
        with patcher_stubbed(self.sync, states=states) as calls:
            summary = quiet(self.sync.run_sync)
        runs = [c for c in calls if c[0] == "run" and c[1] == ("--usage-chart",)]
        self.assertEqual(len(runs), 1, f"应当场重跑一次，实际：{calls}")
        self.assertFalse(runs[0][2], "stale 且 want=True 时不应走还原")
        self.assertIn("更新为当前版本", summary)

    def test_stale_with_switch_off_reverts_instead_of_updating(self):
        """开关是关的、装的却是旧版 → 直接还原（不必先更新再还原绕一圈）。"""
        states = {("--enhance-prompt",): "stale"}
        # 让 enhance_prompt 的期望值为 False
        orig_resolve = self.sync.resolve_wanted
        self.sync.resolve_wanted = lambda: ({"enhance_prompt": False}, "测试")
        try:
            with patcher_stubbed(self.sync, states=states) as calls:
                quiet(self.sync.run_sync)
        finally:
            self.sync.resolve_wanted = orig_resolve
        runs = [c for c in calls if c[0] == "run" and c[1] == ("--enhance-prompt",)]
        self.assertEqual(len(runs), 1, f"应还原一次，实际：{calls}")
        self.assertTrue(runs[0][2], "want=False 时 revert 必须为 True")

    def test_up_to_date_patch_is_left_alone(self):
        """对照组：真正最新的补丁不该被反复重写 —— 否则每次会话启动都重打包 asar。"""
        states = {("--enhance-prompt",): "on", ("--tps-footer",): "on"}
        with patcher_stubbed(self.sync, states=states) as calls:
            summary = quiet(self.sync.run_sync)
        self.assertEqual([c for c in calls if c[0] == "run" and
                          c[1] == ("--enhance-prompt",)], [],
                         "已是最新就不该重跑")
        self.assertNotIn("重注入为新版", summary)


class TestRunPatcherVerdict(unittest.TestCase):
    """★ 回归：`run_patcher()` 不能用裸 `[!]` 判失败（0.6.3 修复）。

    `zcode_patcher.py` 有 60+ 处 `[!]` 输出，其中相当一部分出现在**完全成功的路径**上，
    只是顺带提示「发现的问题」：

      * `发现 N 个模型同时存在于 providerModelRules 与 manualProviderModelRules…
        建议在界面重新保存` —— 这是**事先就存在**的问题，脚本只报告、不修，仍算成功；
      * `备份读取/写入失败`（流程继续）、`integrity 分块数变化，跳过同步`（正常降级）。

    修复前 `rejected = any(mark in out for mark in ("锚点匹配异常", "拒绝", "[!]"))`
    会把这类**成功**判成 `fail`。后果特别隐蔽：补丁其实写进去了，界面却报
    「未处理: xxx(执行失败)」，用户反复重试、每次都报失败 —— 重试永远不会有别的结果。

    实测复现（修复前）：退出码 0 + 汇总「失败 0」+ 一条良性 `[!]` → 判为 `fail`。
    """

    def setUp(self):
        import sync
        self.sync = sync

    def _verdict(self, out: str, rc: int = 0) -> str:
        orig = self.sync.subprocess.run

        class _R:
            returncode = rc
            stdout = out
            stderr = ""

        self.sync.subprocess.run = lambda *a, **k: _R()
        try:
            return self.sync.run_patcher(["--reasoning-config"], revert=False)
        finally:
            self.sync.subprocess.run = orig

    _BENIGN = ("    [!] 发现 2 个模型同时存在于 providerModelRules 与 "
               "manualProviderModelRules —— 内核会因此把整份供应商配置降级为空，"
               "建议在界面重新保存该供应商或手工清理：a/x, b/y\n"
               "    已写入 3 个模型的档位\n"
               "  合计 1 项：成功 1，失败 0\n")

    def test_benign_bang_is_not_a_failure(self):
        """核心回归：成功路径上的良性 `[!]` 不能被当成失败。"""
        self.assertEqual(self._verdict(self._BENIGN), "ok",
                         "良性 [!]（预先存在的重复模型警告）被误判成 fail")

    def test_benign_bang_with_integrity_skip_message(self):
        """另一类良性 `[!]`：integrity 分块数变化时的「跳过同步」提示，属正常降级。"""
        out = ("    [!] out/renderer/zcode-tps.js 的分块数变化，跳过 integrity 同步\n"
               "  合计 1 项：成功 1，失败 0\n")
        self.assertEqual(self._verdict(out), "ok")

    def test_explicit_reject_marks_still_fail(self):
        """反向保护：明确的「拒绝/异常」特征串**必须**仍然判失败。"""
        for mark in ("锚点匹配异常", "拒绝盲改", "无法安全更新，拒绝"):
            with self.subTest(mark=mark):
                out = f"    [!] {mark}（期望恰有一版=1）\n  合计 1 项：成功 0，失败 1\n"
                self.assertEqual(self._verdict(out), "fail", f"{mark} 应判 fail")

    def test_summary_failure_count_is_respected(self):
        """汇总行自报的失败计数是最可靠判据：脚本自己算出来的结论。"""
        self.assertEqual(
            self._verdict("  合计 1 项：成功 0，失败 1\n"), "fail")
        self.assertEqual(
            self._verdict("  合计 3 项：成功 3，失败 0\n"), "ok")

    def test_nonzero_exit_code_fails(self):
        self.assertEqual(self._verdict("  合计 1 项：成功 0，失败 1\n", rc=1), "fail")

    def test_refused_stays_distinct_from_fail(self):
        """`refused` 必须独立：它意味着「现在不能写，等退出后写」，不是失败。"""
        out = "[!] 检测到 ZCode 正在运行 —— 打补丁/还原前请完全退出 ZCode\n"
        self.assertEqual(self._verdict(out, rc=2), "refused",
                         "被运行守卫拒绝不能记成 fail（否则不会转交看护）")

    def test_no_summary_line_is_not_a_failure_by_default(self):
        """兜底：输出里没有汇总行（格式变了）且无拒绝特征串 → 不该凭猜判失败。"""
        self.assertEqual(self._verdict("  √ 增强提示词注入   app.asar\n"), "ok")

    def test_summary_parser_handles_spacing_variants(self):
        """汇总行的空格形态可能变（中英文混排），解析要稳。"""
        for text, want in (("合计 1 项：成功 0，失败 2", 2),
                           ("合计  4  项： 成功  4 ， 失败  0", 0),
                           ("完全不像汇总行", None)):
            with self.subTest(text=text):
                self.assertEqual(self.sync._summary_failures(text), want)

    def test_source_no_longer_uses_bare_bang(self):
        """源码层面钉死：判据里不许再出现裸 `"[!]"`。

        上面几条靠打桩喂字符串验证语义；这条直接查源码，
        防止有人「顺手」把宽泛判据加回来（加回来后单测会红，但这条报错更直指根因）。
        """
        import inspect
        src = inspect.getsource(self.sync.run_patcher)
        self.assertNotIn('"[!]"', src,
                         "run_patcher 的判据里又出现了裸 [!] —— "
                         "zcode_patcher.py 有 60+ 处 [!]，其中多处在成功路径上")

class TestSearchMatchesOnlyOurPlugin(unittest.TestCase):
    """★ 回归：配置树查找必须只认**本插件**的键（0.6.3 修复）。

    原实现 `str(key).startswith("zcode-tokenspeed")` 会把 **别的插件**也命中：
    `zcode-tokenspeed-extra`、`zcode-tokenspeed-pro`、`zcode-tokenspeed-lite` 之类
    同前缀插件一旦同时安装，`_search()` 撞上谁取决于 dict 的插入顺序 → 表现为
    「开关莫名串台」：把别人的配置当自己的开关去注入/还原，而且**不报任何错**。

    宿主的真实配置键形态是 `<插件名>@<市场名>`，所以只认「精确等于插件名」
    或「`<插件名>@` 开头」两种。
    """

    def setUp(self):
        import sync
        self.sync = sync

    def test_accepts_real_host_key_forms(self):
        for key in ("zcode-tokenspeed@dev-abc123",      # dev 市场
                    "zcode-tokenspeed@zcode-toolkit",   # 本机仓库即市场
                    "zcode-tokenspeed"):                # 无市场后缀
            with self.subTest(key=key):
                self.assertTrue(self.sync._is_our_key(key), f"{key} 应被接受")

    def test_rejects_same_prefix_other_plugins(self):
        """核心回归：同前缀的**别的插件**不能被认领。"""
        for key in ("zcode-tokenspeed-extra@mkt", "zcode-tokenspeed-pro",
                    "zcode-tokenspeed-lite@mkt", "zcode-tokenspeedfoo",
                    "my-zcode-tokenspeed@mkt"):
            with self.subTest(key=key):
                self.assertFalse(self.sync._is_our_key(key),
                                 f"{key} 是别的插件/别的位置，不该被认领")

    def test_search_picks_our_entry_not_the_same_prefix_one(self):
        """端到端：两份同前缀配置同时存在时，必须命中**我们自己**那份。

        注意把 -extra 放在前面 —— 修复前的实现会先撞上它。
        """
        cfg = {"plugins": {
            "zcode-tokenspeed-extra@mkt": {"reasoning_config": False, "enhance_prompt": False},
            "zcode-tokenspeed@mkt": {"reasoning_config": True, "enhance_prompt": True},
        }}
        got = self.sync._search(cfg, "config.json")
        self.assertIsNotNone(got, "应能找到本插件配置")
        val, path = got
        self.assertEqual(path, "config.json.plugins.zcode-tokenspeed@mkt")
        self.assertTrue(val["enhance_prompt"], "命中串到了 -extra 那份（开关会串台）")

    def test_search_returns_none_when_only_other_plugins_present(self):
        """只有同前缀的**别的插件**时，不能认领 —— 否则会拿别人的配置去注入。"""
        cfg = {"plugins": {"zcode-tokenspeed-extra@mkt": {"reasoning_config": True}}}
        self.assertIsNone(self.sync._search(cfg, "config.json"))


class TestPluginBoundaryStopsAtPluginRoot(unittest.TestCase):
    """★ 回归：向上找插件清单必须**到插件根为止**（0.6.3 修复）。

    原实现无条件 `list(HERE.parents)[2:6]`。在仓库布局下（市场根即插件根）
    `parents` 只有 5 层，于是搜索范围会一路包含 **盘符根**（`F:\\`）。
    用户把压缩包解到盘符根是很常见的操作 —— 一旦那里躺着一个无关的
    `.zcode-plugin/plugin.json`，`declared_defaults()` 就会用**它的** `userConfig`
    决定本插件的开关默认值，而且**完全不报错**（只表现为「开关状态莫名其妙」）。

    另外还加了「清单必须是本插件的」校验：上级目录里出现任何别的清单时，
    不能因为「它先被扫到」就用它的默认值。
    """

    def setUp(self):
        import sync
        self.sync = sync

    def test_boundary_never_includes_filesystem_root(self):
        """盘符根 / 文件系统根绝不能出现在搜索范围内。"""
        for p in self.sync._plugin_boundary():
            self.assertNotEqual(p, p.parent,
                                f"边界里出现了文件系统根：{p}")
            self.assertTrue(p.is_dir(), f"边界项应是已存在目录：{p}")

    def test_boundary_stops_at_the_plugin_root(self):
        """本仓库布局下，第一处「含 skills/<插件名>」的目录就是插件根，必须到此为止。

        它后面的目录（skills/ 的父、盘的父……）都只可能放着无关清单。
        """
        b = self.sync._plugin_boundary()
        roots = [p for p in b if (p / "skills" / self.sync.PLUGIN_ID_PREFIX).is_dir()]
        self.assertTrue(roots, "应能识别出插件根（含 skills/<插件名>）")
        self.assertIn(roots[0], b, "插件根必须在搜索范围内")
        # 插件根之后不应再有更上层目录
        self.assertEqual(b[-1], roots[0],
                         f"插件根 {roots[0]} 之后仍继续向上找了：{b}")

    def test_repo_layout_still_finds_defaults(self):
        """对照组：边界收紧后，本仓库仍必须能正常取到默认值（不能修坏）。"""
        d = self.sync.declared_defaults()
        self.assertTrue(d, "本仓库应能读到插件清单默认值")
        self.assertIn("enhance_prompt", d)
        self.assertTrue(all(isinstance(v, bool) for v in d.values()))

    def test_manifest_of_another_plugin_is_skipped(self):
        """上级目录里出现别人的清单时，必须跳过而不是拿它的默认值。"""
        tmp = tempfile.TemporaryDirectory(prefix="zpatch-boundary-",
                                          ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        # 造一个「别的插件」的清单放在某层，name 对不上
        (d / ".zcode-plugin").mkdir(parents=True)
        (d / ".zcode-plugin" / "plugin.json").write_text(
            json.dumps({"name": "some-other-plugin",
                        "userConfig": {"enhance_prompt": {"default": False}}}),
            encoding="utf-8")
        orig = self.sync.HERE
        # 让脚本目录位于 d/x/y/scripts，使 d 落在向上搜索范围内
        sd = d / "x" / "y" / "scripts"
        sd.mkdir(parents=True)
        self.sync.HERE = sd
        try:
            got = self.sync.declared_defaults()
        finally:
            self.sync.HERE = orig
        self.assertNotIn("enhance_prompt", got,
                         "用了别的插件的清单（name 校验失效）")


class TestAutoInject(unittest.TestCase):
    """零配置自动注入 —— 「从插件市场装完就能用」这条要求就靠它落地。

    ZCode 的插件清单里**没有安装时钩子**（`plugin-json-spec.md` 只允许声明
    `skills` / `commands` / `hooks` / `mcpServers`），所以最早能自动触发的时机是
    「下一次会话启动」的 `SessionStart`。在这条硬约束下，让「装完即生效」成立只能靠
    一件事：**没保存过开关时按插件清单里声明的默认值注入**。
    下面把这条链路的每一环都钉住，避免哪天有人把默认值改回 false 又变回「装了没生效」。
    """

    def setUp(self):
        import sync
        self.sync = sync
        self._tmp = tempfile.TemporaryDirectory(prefix="zpatch-auto-", ignore_cleanup_errors=True)
        self._d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, opts, source, defaults):
        """在受控输入下跑 resolve_wanted()。"""
        orig = (self.sync.read_options, self.sync.declared_defaults)
        self.sync.read_options = lambda: (opts, source)
        self.sync.declared_defaults = lambda: defaults
        try:
            return self.sync.resolve_wanted()
        finally:
            self.sync.read_options, self.sync.declared_defaults = orig

    def _switch_keys(self):
        return {key for key, _args, _repack in self.sync.PATCHES}

    # ---------------------------------------------------------------- 清单契约

    def test_manifest_declares_every_switch_on_by_default(self):
        """**这是「装完即用」的根契约**：清单里每个开关的 default 都必须是 true。

        插件市场安装后，ZCode 的配置里根本没有 `plugins.options` 这一项（用户没点过
        「保存配置」）。若默认值是 false，自动注入就变成「按默认值什么都不做」，
        用户看到的还是「安装成功但没生效」——正是这条要求要消灭的情形。
        """
        manifest = json.loads((_HERE.parent / ".zcode-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
        uc = manifest.get("userConfig") or {}
        self.assertEqual(set(uc), self._switch_keys(),
                         "plugin.json 的 userConfig 必须与 sync.PATCHES 一一对应")
        off = sorted(k for k, v in uc.items() if v.get("default") is not True)
        self.assertEqual(off, [], f"这些开关的 default 不是 true，装完不会自动生效: {off}")

    def test_declared_defaults_reads_the_real_manifest(self):
        got = self.sync.declared_defaults()
        self.assertEqual(set(got), self._switch_keys())
        self.assertTrue(all(got.values()), f"默认值应全为 true，实际 {got}")

    def test_declared_defaults_skips_non_boolean_entries(self):
        """userConfig 里可能混着非开关项（字符串/枚举），不能当成开关塞进结果。"""
        root = self._d / "plug"
        (root / ".zcode-plugin").mkdir(parents=True)
        (root / ".zcode-plugin" / "plugin.json").write_text(json.dumps({
            # name 必须是本插件（0.6.3 起会校验，防止拿到上级目录里别人的清单）
            "name": "zcode-tokenspeed",
            "userConfig": {"a": {"default": True}, "b": {"default": "high"},
                           "c": {"default": False}, "d": {"no_default": 1}},
        }), encoding="utf-8")
        scripts = root / "skills" / "zcode-tokenspeed" / "scripts"
        scripts.mkdir(parents=True)
        orig = self.sync.HERE
        self.sync.HERE = scripts
        try:
            self.assertEqual(self.sync.declared_defaults(), {"a": True, "c": False})
        finally:
            self.sync.HERE = orig

    def test_declared_defaults_without_manifest_is_empty(self):
        scripts = self._d / "lonely" / "skills" / "zcode-tokenspeed" / "scripts"
        scripts.mkdir(parents=True)
        orig = self.sync.HERE
        self.sync.HERE = scripts
        try:
            self.assertEqual(self.sync.declared_defaults(), {})
        finally:
            self.sync.HERE = orig

    # ------------------------------------------------------------ 合并优先级

    def test_never_saved_config_falls_back_to_defaults(self):
        """从未保存过开关 → 全部按默认值注入。这就是零配置自动注入本身。"""
        wanted, origin = self._resolve({}, None, {"a": True, "b": True})
        self.assertEqual(wanted, {"a": True, "b": True})
        self.assertIn("从未保存过开关", origin)

    def test_saved_value_wins_over_default(self):
        """保存过的值优先——用户明确关掉的开关不能被默认值悄悄打开。"""
        wanted, _ = self._resolve({"a": False}, "config", {"a": True, "b": True})
        self.assertEqual(wanted, {"a": False, "b": True})

    def test_missing_key_is_filled_from_defaults(self):
        """插件升级新增开关时，老配置里没有这个键 → 补默认值，不会漏注入。"""
        wanted, _ = self._resolve({"a": False}, "config", {"a": True, "new": True})
        self.assertEqual(wanted, {"a": False, "new": True})

    def test_explicit_false_survives_as_revert(self):
        """显式关掉必须是 false（触发还原），不能被当成「没表态」。"""
        wanted, origin = self._resolve({"a": False}, "config", {"a": True})
        self.assertIs(wanted["a"], False)
        self.assertEqual(origin, "config")

    def test_nothing_available_means_no_operation(self):
        """配置读不到 + 清单也读不到 → 只能什么都不做（并如实说明）。"""
        self.assertEqual(self._resolve({}, None, {}), ({}, ""))
        self.assertEqual(self._resolve({"a": "high"}, "config", {}), ({}, ""))

    # ------------------------------------------------------ 第三态 na（不适用）

    def test_check_state_reports_not_applicable(self):
        """≤3.11 专用的内核补丁在 3.14+ 会打印「不适用」，必须单独成一态。

        否则它会被当成「未打」→ 去执行 → 脚本空转一圈什么都没做，
        最后却被报成「已生效」——默认值全开之后这个误报每次装完都会出现。
        """
        cases = {"本补丁不适用（ZCode 3.14+ 已原生支持）": "na",
                 "[!] 未打": "off", "已打": "on", "看不懂的输出": "unknown"}
        for text, want in cases.items():
            with self.subTest(text=text):
                orig = self.sync.subprocess.run
                self.sync.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
                    [], 0, stdout=text, stderr="")
                try:
                    self.assertEqual(self.sync.check_state([]), want)
                finally:
                    self.sync.subprocess.run = orig

    def test_run_sync_skips_switches_that_do_not_apply(self):
        """na 的开关既不执行也不报错，只在结论里注明「本版本不适用」。"""
        orig = (self.sync.read_options, self.sync.declared_defaults)
        self.sync.read_options = lambda: ({"core_patch": True}, "config")
        self.sync.declared_defaults = lambda: {}
        try:
            with patcher_stubbed(self.sync, {(): "na"}) as calls:
                summary = self.sync.run_sync(echo=False)
        finally:
            self.sync.read_options, self.sync.declared_defaults = orig
        self.assertEqual(calls, [], "不适用的补丁不应被执行")
        self.assertIn("本版本不适用", summary)
        self.assertNotIn("已写入", summary)

    def test_na_is_skipped_even_when_user_asked_for_it(self):
        """用户显式打开也不该去跑不适用的补丁——照样只标注、不执行。"""
        orig = (self.sync.read_options, self.sync.declared_defaults)
        self.sync.read_options = lambda: ({"core_patch": False}, "config")
        self.sync.declared_defaults = lambda: {}
        try:
            with patcher_stubbed(self.sync, {(): "na"}) as calls:
                summary = self.sync.run_sync(echo=False)
        finally:
            self.sync.read_options, self.sync.declared_defaults = orig
        self.assertEqual(calls, [])
        self.assertIn("本版本不适用", summary)

    # ------------------------------------------- 被客户端拒绝 → 转交退出后看护

    def _resolve_only(self, opts):
        """把 read_options / declared_defaults 固定住，只观察 run_sync 的动作。"""
        orig = (self.sync.read_options, self.sync.declared_defaults)
        self.sync.read_options = lambda: (opts, "config")
        self.sync.declared_defaults = lambda: {}
        return orig

    def test_refused_write_is_deferred_not_reported_as_failure(self):
        """客户端在运行 → zcode_patcher.py 直接拒绝写入（exit 2）。

        这不是失败，而是「现在不能写，等退出后写」：必须转交 apply_after_exit.py。
        否则用户看到的就是「未处理: xxx(执行失败)」而**永远不生效** ——
        SessionStart 钩子必然在 ZCode 运行中触发，所以这条路是常态而非例外。
        """
        orig = self._resolve_only({"reasoning_config": True})
        try:
            with patcher_stubbed(self.sync,
                                 {("--reasoning-config",): "off"},
                                 {("--reasoning-config",): "refused"}) as calls:
                summary = self.sync.run_sync(echo=False)
        finally:
            self.sync.read_options, self.sync.declared_defaults = orig
        self.assertEqual(calls, [("run", ("--reasoning-config",), False),
                                 ("watchdog", {"reasoning_config": True})])
        self.assertNotIn("未处理", summary)
        self.assertIn("ZCode 退出时写入", summary)

    def test_hard_failure_is_still_reported(self):
        """真正的失败（锚点不匹配等）不能被「被拒」这条新分支吞掉。"""
        orig = self._resolve_only({"reasoning_config": True})
        try:
            with patcher_stubbed(self.sync,
                                 {("--reasoning-config",): "off"},
                                 {("--reasoning-config",): "fail"}) as calls:
                summary = self.sync.run_sync(echo=False)
        finally:
            self.sync.read_options, self.sync.declared_defaults = orig
        self.assertIn("未处理", summary)
        self.assertEqual([c[0] for c in calls], ["run"], "失败不该起看护")

    def test_env_option_keys_are_lowercased(self):
        """Windows 上 os.environ 的键名是**大写**，读配置时必须归一。

        不归一的话 `ZCODE_PLUGIN_CONFIG_reasoning_config` 会变成 `REASONING_CONFIG`，
        与开关名对不上 → `{**defaults, **explicit}` 里默认值（全 true）胜出，
        **用户保存的开关会被整体忽略**（静默失效，最难查）。
        """
        from unittest import mock
        env = {"ZCODE_PLUGIN_CONFIG_REASONING_CONFIG": "false",
               "ZCODE_PLUGIN_CONFIG_USAGE_CHART": "true"}
        with mock.patch.dict("os.environ", env, clear=False):
            opts, source = self.sync.read_options()
        self.assertEqual(source, "env")
        self.assertEqual(opts, {"reasoning_config": False, "usage_chart": True})

    def test_watchdog_knows_every_switch(self):
        """apply_after_exit 的参数表必须覆盖 sync.PATCHES 的每一个键。

        漏一个键的后果：`--want=<键>=on` 被 parse_wants 当「未知开关」忽略，
        那个开关**开不起来也关不干净**（enhance_prompt 就漏过这一条）。
        """
        import apply_after_exit as aae
        self.assertEqual({k for k, _a, _r in self.sync.PATCHES} - set(aae.PATCH_ARGS),
                         set(), "apply_after_exit.PATCH_ARGS 漏了开关，退出后看护会静默忽略它")

    # ------------------------------------------------------ 会话提示（可见性）

    def test_notice_is_a_schema_valid_hook_output(self):
        """提示必须是一个「以 { 开头的合法 JSON 对象」，且只带 additionalContext。

        内核只在 stdout 以 `{` 开头时才解析（`wQs()` 里 `startsWith("{")`），
        并按 zod schema 严格校验；`hookSpecificOutput` 还要核对 `hookEventName`
        与本次事件一致，写错会把这次钩子标成失败。所以只用被**无条件**消费的
        顶层 `additionalContext`（`Lio()` 里 `t.additionalContext && …push(…)`）。
        """
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.sync.emit_notice()
        raw = buf.getvalue()
        self.assertTrue(raw.startswith("{"), f"必须以 {{ 开头才会被解析: {raw[:40]!r}")
        data = json.loads(raw)
        self.assertEqual(list(data), ["additionalContext"], "只允许这一个字段")
        self.assertIsInstance(data["additionalContext"], str)
        self.assertIn(self.sync.NOTICE_HEAD, data["additionalContext"])

    def test_notice_explains_the_exit_requirement(self):
        """提示要讲清「八项都要完全退出 ZCode 才写入」——否则用户会以为没生效。"""
        notice = self.sync.build_notice()
        self.assertIn("完全退出 ZCode", notice)
        self.assertIn("自动把 ZCode 重新拉起", notice)
        self.assertIn("doctor.py", notice)

    def test_first_auto_inject_fires_exactly_once(self):
        """提示只出一次，之后每次开会话都弹就成了噪声。"""
        orig = self.sync.MARKER
        self.sync.MARKER = self._d / "_autoinject.done"
        try:
            self.assertTrue(self.sync.first_auto_inject())
            self.assertFalse(self.sync.first_auto_inject())
            self.assertTrue(self.sync.MARKER.exists())
        finally:
            self.sync.MARKER = orig

    def test_first_auto_inject_survives_unwritable_marker(self):
        """标记写不进去（只读安装目录）时不能抛异常，也不能每次都当首次而反复提示。"""
        orig = self.sync.MARKER
        self.sync.MARKER = self._d / "nope" / "x" / "_autoinject.done"
        try:
            self.assertFalse(self.sync.first_auto_inject())
        finally:
            self.sync.MARKER = orig

    def test_detach_emits_notice_only_on_the_first_session(self):
        """整条钩子链路：第一次开会话出提示，第二次安静。"""
        import sync
        orig = (sync.CONFIG, sync.STAMP, sync.LOG, sync.MARKER, sync.spawn_detached,
                list(sys.argv))
        d = self._d / "hook"
        d.mkdir()
        sync.CONFIG = d / "config.json"
        sync.STAMP = d / "_sync.last"
        sync.LOG = d / "_sync.log"
        sync.MARKER = d / "_autoinject.done"
        sync.spawn_detached = lambda extra: True
        try:
            outs = []
            with patcher_stubbed(sync):
                for _ in range(2):
                    sys.argv[:] = ["sync.py", "--detach"]
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        sync.main()
                    outs.append(buf.getvalue())
            self.assertTrue(outs[0].startswith("{"), "首次必须给出提示")
            self.assertEqual(outs[1], "", "第二次不该再提示")
        finally:
            (sync.CONFIG, sync.STAMP, sync.LOG, sync.MARKER,
             sync.spawn_detached) = orig[:5]
            sys.argv[:] = orig[5]


class TestDoctorDiscovery(unittest.TestCase):
    """doctor 必须能认出「插件根 = 市场根」这种安装（marketplace.json 里 `source: "./"`）。

    最初的实现只找 `<市场>/<插件名>/` 子目录——本地 directory 来源的市场恰好长这样，
    所以在本机"看起来是对的"；但 GitHub 来源的市场是把仓库整个 clone 下来，插件根就是
    市场根，根本没有同名子目录。这会在用户机器上误报「插件没装成功」，把人带偏。
    """

    def _with_storage(self, storage: Path):
        import doctor
        orig = doctor._storage_roots
        doctor._storage_roots = lambda: [storage]
        try:
            return doctor._installed_plugin_dirs()
        finally:
            doctor._storage_roots = orig

    def _make_plugin(self, root: Path, version: str = "9.9.9") -> None:
        (root / ".zcode-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".zcode-plugin" / "plugin.json").write_text(
            json.dumps({"name": "zcode-tokenspeed", "version": version}), encoding="utf-8")

    def test_finds_plugin_whose_root_is_the_marketplace_root(self):
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            root = storage / "cli" / "plugins" / "marketplaces" / "zcode-toolkit-abc123"
            self._make_plugin(root)
            self.assertEqual(self._with_storage(storage), [root])

    def test_finds_plugin_in_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            root = storage / "cli" / "plugins" / "marketplaces" / "some-market" / "plugins" / "x"
            self._make_plugin(root)
            self.assertEqual(self._with_storage(storage), [root])

    def test_ignores_plugins_with_other_names(self):
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            other = storage / "cli" / "plugins" / "marketplaces" / "m" / "other-plugin"
            (other / ".zcode-plugin").mkdir(parents=True)
            (other / ".zcode-plugin" / "plugin.json").write_text(
                json.dumps({"name": "other-plugin"}), encoding="utf-8")
            self.assertEqual(self._with_storage(storage), [])

    def test_manifest_version_reads_from_found_root(self):
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            root = storage / "cli" / "plugins" / "marketplaces" / "m"
            self._make_plugin(root, "1.2.3")
            import doctor
            self.assertEqual(doctor._manifest_version(root), "1.2.3")

    def test_finds_plugin_in_cache_version_dir(self):
        """用户机器上的真实落点：GitHub 来源的市场被缓存成
        `cache/<市场名>/<插件名>/<版本>/` —— 插件根在**版本目录**里。

        实测本机 `cache/zcode-plugins-official/computer-use/0.5.13/.zcode-plugin/plugin.json`。
        用户从 `cache/zcode-toolkit` 敲 `python skills/.../doctor.py` 报 No such file，
        就是因为那一层是**市场目录**，根本没有 skills/。
        """
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            root = (storage / "cli" / "plugins" / "cache"
                    / "zcode-toolkit" / "zcode-tokenspeed" / "0.5.2")
            self._make_plugin(root, "0.5.2")
            self.assertEqual(self._with_storage(storage), [root])

    def test_cache_dir_itself_is_not_a_plugin_root(self):
        """市场目录那一层（`cache/zcode-toolkit`）不该被认成插件根。"""
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            market = storage / "cli" / "plugins" / "cache" / "zcode-toolkit"
            (market / "zcode-tokenspeed" / "0.5.2").mkdir(parents=True)
            self.assertEqual(self._with_storage(storage), [])

    def test_multiple_cached_versions_all_found(self):
        """缓存里会堆积多个版本（本机实测 4 个 zcode-patcher），要全都报出来。"""
        import doctor
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            base = storage / "cli" / "plugins" / "cache" / "m" / "zcode-tokenspeed"
            for v in ("0.5.1", "0.5.2"):
                self._make_plugin(base / v, v)
            found = self._with_storage(storage)
            self.assertEqual(len(found), 2)
            self.assertEqual(sorted(doctor._manifest_version(p) for p in found),
                             ["0.5.1", "0.5.2"])

    def _capture_where(self, storage: Path, verbose: bool = False) -> str:
        """跑一次 --where 并把它打印的内容抓回来。"""
        import doctor
        orig = doctor._storage_roots
        doctor._storage_roots = lambda: [storage]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = doctor.print_where(verbose=verbose)
        finally:
            doctor._storage_roots = orig
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_where_mode_lists_hits_and_prints_usable_command(self):
        """`--where` 存在的意义：用户照着 README 敲相对路径失败时，一条命令
        告诉他脚本到底在哪、以及该用哪个绝对路径。

        默认**只列命中项**：本机实测候选目录有 120 个（claude-plugins-official 一家
        就几十个插件），全列出来会把答案淹没。
        """
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            root = (storage / "cli" / "plugins" / "cache"
                    / "zcode-toolkit" / "zcode-tokenspeed" / "0.5.2")
            self._make_plugin(root, "0.5.2")
            # 一堆噪声：别的插件不该出现在默认输出里
            for n in ("android-emulator", "browser-use", "computer-use"):
                other = storage / "cli" / "plugins" / "cache" / "zcode-plugins-official" / n / "0.1.0"
                (other / ".zcode-plugin").mkdir(parents=True)
                (other / ".zcode-plugin" / "plugin.json").write_text(
                    json.dumps({"name": n, "version": "0.1.0"}), encoding="utf-8")
            out = self._capture_where(storage)
            self.assertIn(str(root), out)
            self.assertIn("0.5.2", out)
            self.assertIn("doctor.py", out)          # 给出了可直接复制的命令
            self.assertIn(str(Path("skills") / "zcode-tokenspeed" / "scripts"), out)
            self.assertNotIn("browser-use", out)     # 默认不列别人的插件
            self.assertIn("--where-all", out)        # 想看全量时告诉用户怎么开

    def test_where_all_lists_every_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            other = storage / "cli" / "plugins" / "cache" / "zcode-plugins-official" / "browser-use" / "0.1.0"
            (other / ".zcode-plugin").mkdir(parents=True)
            (other / ".zcode-plugin" / "plugin.json").write_text(
                json.dumps({"name": "browser-use", "version": "0.1.0"}), encoding="utf-8")
            out = self._capture_where(storage, verbose=True)
            self.assertIn("browser-use", out)

    def test_where_mode_says_not_installed_when_no_hit(self):
        with tempfile.TemporaryDirectory() as d:
            storage = Path(d)
            (storage / "cli" / "plugins" / "cache" / "m" / "other" / "1.0.0").mkdir(parents=True)
            out = self._capture_where(storage)
            self.assertIn("没装成功", out)

    def test_where_flag_is_wired_into_cli(self):
        """--where 必须真能从命令行跑通（check_plugin 的提示里已经写了它）。"""
        import doctor
        r = subprocess.run([sys.executable, str(doctor.__file__), "--where"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("插件位置扫描", r.stdout)


class TestConsoleEncoding(unittest.TestCase):
    """中文 Windows 的控制台代码页是 cp936（GBK）。输出**走管道**时（`> log.txt`、
    `subprocess.run(capture_output=True)`）Python 不再走 WriteConsoleW，而是按 cp936 编码 ——
    这时 print 一个 GBK 里没有的字符会抛 UnicodeEncodeError，**把整段输出打断**。

    真实故障：doctor.py 捕获 zcode_patcher.py 的输出，汇总表在
    `✓ 用量页去截断补丁` 那一行崩掉，用户只看到半张表 + traceback，
    还以为是补丁本身失败。交互式控制台不受影响，所以这个坑**只在管道里露头**，
    平时手动跑脚本永远测不出来。
    """

    def test_marks_are_printable_in_the_current_stdout_encoding(self):
        import _console
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        for mark in (_console.ok_mark(), _console.bad_mark(), _console.warn_mark()):
            mark.encode(enc)      # 编不出来就会抛 UnicodeEncodeError

    def test_glyph_falls_back_when_encoding_cannot_represent_it(self):
        import _console
        buf = io.BytesIO()
        orig = sys.stdout
        sys.stdout = io.TextIOWrapper(buf, encoding="cp936", errors="strict")
        try:
            self.assertEqual(_console.glyph("✓", "v"), "v")
            self.assertEqual(_console.glyph("✗", "x"), "x")
            self.assertEqual(_console.glyph("⚠", "!"), "!")
            self.assertEqual(_console.glyph("✓"), "v")          # 走内置备选表
            self.assertEqual(_console.glyph("→", "->"), "→")     # cp936 里有 →，不该降级
        finally:
            sys.stdout = orig

    def test_safe_stdio_replaces_instead_of_crashing(self):
        """errors=replace 是最后一道防线：任何编不出的字符都降级，绝不抛异常。"""
        import _console
        buf = io.BytesIO()
        orig = sys.stdout
        sys.stdout = io.TextIOWrapper(buf, encoding="cp936", errors="strict")
        try:
            _console.safe_stdio()
            self.assertEqual(sys.stdout.errors, "replace")
            print("✓✗⚠ 混在中文里也不该崩")
            sys.stdout.flush()
            raw = buf.getvalue()      # 必须在换回 stdout 前读，否则 wrapper 被 GC 时连 buf 一起关掉
        finally:
            sys.stdout = orig
        self.assertIn("不该崩".encode("cp936"), raw)

    def test_safe_stdio_is_idempotent(self):
        import _console
        _console.safe_stdio()
        _console.safe_stdio()     # 重复调用不该抛（reconfigure 有状态）

    def test_patcher_summary_survives_a_cp936_pipe(self):
        """端到端回归：把子进程 stdout 强制成 cp936，汇总表必须完整打出来。

        这正是用户报的那次故障 —— 没有 _console 的话，这一行会抛
        `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`。
        """
        try:
            if not zp.resolve_target(None):
                self.skipTest("本机没有 ZCode，跳过端到端编码回归")
        except SystemExit:
            self.skipTest("本机没有 ZCode，跳过端到端编码回归")
        env = dict(os.environ, PYTHONIOENCODING="cp936")
        r = subprocess.run([sys.executable, str(zp.__file__), "--all", "--check"],
                           capture_output=True, encoding="cp936", errors="replace",
                           cwd=str(Path(zp.__file__).resolve().parent), timeout=300, env=env)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertNotIn("UnicodeEncodeError", out, out[-600:])
        self.assertNotIn("Traceback", out, out[-600:])
        self.assertIn("执行汇总", out)
        self.assertIn("合计 8 项", out)


class TestZcodeLogScan(unittest.TestCase):
    """doctor 会读 ZCode 自己的 jsonl 日志 —— 那里有比心跳文件更靠前的一层证据：
    `bootstrap.app.startup.plugins.completed` 的 `hookCount`（这次启动注册了几个钩子）。

    用户实测的那份报告里，心跳为空、补丁全未打，结论只能说「钩子从未运行过」，
    然后甩一张四选一清单。但日志里 hookCount=0 已经说明：
    那次启动 ZCode 根本没把钩子挂上，「钩子没跑」是必然结果，与钩子写法无关。
    """

    @staticmethod
    def _write_log(d: Path, startups, phases: int = 1) -> list[Path]:
        lines = []
        for ts, hooks, enabled, diag in startups:
            lines.append(json.dumps({
                "timestamp": ts, "level": "info",
                "event": "bootstrap.app.startup.plugins.completed",
                "message": "ZCode plugins resolved",
                "context": {"startupKind": "zcode_app", "pluginCount": 14,
                            "enabledPluginCount": enabled, "hookCount": hooks,
                            "diagnosticCount": diag, "skillRootCount": 9},
            }, ensure_ascii=False))
        for _ in range(phases):
            lines.append(json.dumps({
                "timestamp": "2026-09-22T01:00:00.000Z", "level": "info",
                "event": "turn.phase.completed", "message": "Turn phase completed",
                "context": {"queryId": "q", "turnNumber": 0, "phase": "session_start_hooks"},
            }, ensure_ascii=False))
        p = d / "zcode-2026-09-22.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return [p]

    def test_scan_reads_hook_count_and_phase_count(self):
        import doctor
        with tempfile.TemporaryDirectory() as d:
            files = self._write_log(Path(d), [("2026-09-22T01:00:00.000Z", 0, 11, 0),
                                              ("2026-09-22T02:00:00.000Z", 1, 12, 0)])
            data = doctor._scan_zcode_log(files)
        self.assertEqual(len(data["startups"]), 2)
        self.assertEqual(data["startups"][-1]["hooks"], 1)
        self.assertEqual(data["startups"][-1]["enabled"], 12)
        self.assertEqual(data["startups"][-1]["kind"], "zcode_app")
        self.assertEqual(data["phase_count"], 1)

    def test_scan_survives_garbage_lines(self):
        """日志是逐行追加的，写到一半被截断很正常，不能因此让自检崩掉。"""
        import doctor
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "zcode-2026-09-22.jsonl"
            p.write_text('{"hookCount" 这不是 json\n随机一行\n', encoding="utf-8")
            data = doctor._scan_zcode_log([p])
        self.assertEqual(data["startups"], [])
        self.assertEqual(data["phase_count"], 0)

    def test_report_flags_zero_hooks_as_the_cause(self):
        """hookCount=0 时要直接点明「必然结果」，而不是甩一张四选一清单。"""
        import doctor
        with tempfile.TemporaryDirectory() as d:
            files = self._write_log(Path(d), [("2026-09-22T02:00:00.000Z", 0, 11, 0)])
            orig = doctor._latest_zcode_logs
            doctor._latest_zcode_logs = lambda *a, **kw: files
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    data = doctor.report_zcode_log()
            finally:
                doctor._latest_zcode_logs = orig
        out = buf.getvalue()
        self.assertIn("0 个钩子", out)
        self.assertIn("不是钩子本身", out)
        self.assertEqual(data["startups"][-1]["hooks"], 0)

    def test_report_says_plugin_side_is_ready_when_hooks_registered(self):
        import doctor
        with tempfile.TemporaryDirectory() as d:
            files = self._write_log(Path(d), [("2026-09-22T02:00:00.000Z", 2, 12, 0)])
            orig = doctor._latest_zcode_logs
            doctor._latest_zcode_logs = lambda *a, **kw: files
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    doctor.report_zcode_log()
            finally:
                doctor._latest_zcode_logs = orig
        out = buf.getvalue()
        self.assertIn("已经就绪", out)
        self.assertIn("CLAUDE_PLUGIN_ROOT", out)     # 注册了但没执行 → 才轮到查这个

    def test_verdict_uses_log_evidence_when_hook_never_ran(self):
        import doctor
        log_info = {"startups": [{"ts": "2026-09-22 02:00:00", "kind": "zcode_app",
                                  "plugins": 14, "enabled": 11, "hooks": 0,
                                  "diagnostics": 0}],
                    "phase_count": 3, "last_phase": ""}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor.verdict(True, True, True, True, True, True, False, log_info)
        out = buf.getvalue()
        self.assertIn("注册的钩子数是 0", out)
        self.assertIn("完全退出", out)

    def test_verdict_falls_back_to_checklist_without_log(self):
        """读不到日志（旧版 ZCode / 日志被清过）时仍要给四选一清单。"""
        import doctor
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor.verdict(True, True, True, True, True, True, False, None)
        out = buf.getvalue()
        self.assertIn("依次确认", out)


class TestDoctorAutoInjectWording(unittest.TestCase):
    """自检报告里关于「配置没保存过」的措辞必须与零配置自动注入一致。

    旧措辞把它写成**卡点**并让人去点「保存配置」—— 那是旧行为（没保存过就什么都不做）。
    现在没保存过 = 按插件清单默认值自动注入，恰恰是「装完即用」的正常状态；
    如果自检还把它报成故障，用户就会被指去做一件根本不必要的事，
    而且会误以为「我没保存配置，所以插件没生效」——正是要消灭的那种误导。
    """

    def _options(self, saved):
        import doctor
        orig = doctor._saved_options
        doctor._saved_options = lambda: saved
        return orig

    def test_check_options_does_not_call_missing_config_a_fault(self):
        import doctor
        orig = self._options({})
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                ok = doctor.check_options()
        finally:
            doctor._saved_options = orig
        out = buf.getvalue()
        self.assertFalse(ok, "返回值仍表示「没有保存过的开关」")
        self.assertIn("这不是故障", out)
        self.assertIn("默认值", out)
        self.assertNotIn(doctor.BAD, out, "不该再用 ✗ 把它标成故障")

    def test_verdict_does_not_block_when_nothing_was_ever_saved(self):
        """saved=False + 钩子跑过 → 链路是完整的，不能停在这里。"""
        import doctor
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor.verdict(True, True, True, True, False, True, True, None)
        out = buf.getvalue()
        self.assertNotIn("★ 卡点", out)
        self.assertIn("链路完整", out)
        self.assertIn("默认值", out)

    def test_verdict_still_blocks_when_hook_never_ran(self):
        """去掉 saved 这道闸之后，「钩子没跑」仍然必须被报成卡点。"""
        import doctor
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor.verdict(True, True, True, True, False, True, False, None)
        out = buf.getvalue()
        self.assertIn("★ 卡点", out)
        self.assertIn("从未运行过", out)


class TestDoctorFlagsStaleInjection(unittest.TestCase):
    """自检第 8 节必须**点名**「已打但装的是旧版片段」。

    这条对应的是最难自查的一类故障：插件更新了、退出重启了、自检全绿，
    但新修的功能就是不生效。原因是 `app.asar` 里还是旧片段，
    而「已打」这两个字在界面上看起来毫无异常。

    如果自检不把「含旧版组件」单独拎出来说，用户唯一的线索就是
    「我觉得应该生效了但没生效」—— 排查方向会完全跑偏到模型配置/网络上去。
    """

    def _run_with(self, check_output):
        import doctor
        orig = doctor.subprocess.run

        class _R:
            returncode = 0
            stdout = check_output
            stderr = ""

        doctor.subprocess.run = lambda *a, **k: _R()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                doctor.check_patches()
        finally:
            doctor.subprocess.run = orig
        return buf.getvalue()

    _STALE = ("=== 增强提示词注入，目标 1 处，模式：检查 ===\n"
              "[*] D:\\ZCode\\resources\\app.asar\n"
              "    增强提示词注入: 已打（含旧版组件，重跑可自动更新） | "
              "renderer 脚本: 有 | main handler: 有（版本旧）\n")

    _CURRENT = ("=== 增强提示词注入，目标 1 处，模式：检查 ===\n"
                "[*] D:\\ZCode\\resources\\app.asar\n"
                "    增强提示词注入: 已打 | renderer 脚本: 有 | "
                "main handler: 有\n")

    def test_stale_state_is_called_out_with_fix(self):
        out = self._run_with(self._STALE)
        self.assertIn("旧版片段", out, "必须点名「旧版片段」，不能混在「已打」里过去")
        self.assertIn("不会重新注入 app.asar", out, "要解释为什么退出重启也没用")
        self.assertIn("--all", out, "要给出可执行的手动修复命令")

    def test_current_state_does_not_trigger_the_stale_warning(self):
        """对照组：真正最新的状态不该出现这条警告，否则用户会照着白改一遍。"""
        out = self._run_with(self._CURRENT)
        self.assertNotIn("旧版片段", out)

    def test_patcher_check_output_uses_the_documented_stale_wording(self):
        """`--check` 的措辞是 doctor 与 sync 共同依赖的接口，不能随手改。

        这个串同时被两处消费：
          * `sync.check_state()` 据此返回 `stale`（要求重跑注入）
          * `doctor.check_patches()` 据此提示用户
        改掉「含旧版组件」这几个字会让**两边同时静默失效** —— 又回到
        「全绿但不生效」的老坑。所以在这里把措辞本身钉死。
        """
        src = (Path(__file__).resolve().parent.parent
               / "skills" / "zcode-tokenspeed" / "scripts" / "zcode_patcher.py")
        text = src.read_text(encoding="utf-8")
        self.assertIn("含旧版组件，重跑可自动更新", text,
                      "zcode_patcher.py 改变了旧版组件的措辞；"
                      "sync.check_state 与 doctor 都依赖它，必须同步更新")


class TestDoctorWatchdogSection(unittest.TestCase):
    """自检第 7.5 节必须能区分「看护等到了退出」与「看护一直没等到」。

    这是「必须重启两遍才生效」这个现象的唯一直接证据来源。
    机制上写入发生在**退出**时（`while zcode_running(): sleep(3)` 之后才写 asar），
    所以第一次「退出」如果不是彻底退出（关窗口只是最小化到托盘，且 ZCode 是多进程，
    主窗口关了常留渲染/GPU 子进程），`zcode_running()` 就一直为真 → 看护永远不写 →
    用户必须再来一轮，而第二轮恰好退干净了，补丁才落盘。

    如果自检只报「看护启动 3 次」而不指出「一次都没等到退出」，用户能看到的全部信息就是
    「我明明重启了却不生效」—— 会误以为是模型配置、缓存或网络问题。
    """

    def _run_with(self, log_text: str | None):
        """把 WATCHDOG_LOG 指向临时文件（或不存在），跑第 7.5 节并捕获输出。"""
        import doctor
        orig = doctor.WATCHDOG_LOG
        tmp = Path(tempfile.mkdtemp(prefix="zcode-watchdog-")) / "_apply_after_exit.log"
        if log_text is not None:
            tmp.write_text(log_text, encoding="utf-8")
        doctor.WATCHDOG_LOG = tmp
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                doctor.check_watchdog()
        finally:
            doctor.WATCHDOG_LOG = orig
        return buf.getvalue()

    # 真实场景（本机实测日志）：三次挂看护，一次都没等到退出
    _NEVER_WOKE = (
        "[2026-09-23 15:03:55] 看护启动，等待 ZCode 退出…\n"
        "[2026-09-23 15:23:38] 看护启动，等待 ZCode 退出…\n"
        "[2026-09-23 15:29:11] 看护启动，等待 ZCode 退出…\n"
    )
    # 正常完成：等到了退出、处理完、已重启
    _COMPLETED = (
        "[2026-09-23 15:03:55] 看护启动，等待 ZCode 退出…\n"
        "[2026-09-23 15:04:12] ZCode 已退出（等待 15s），开始处理：应用 --enhance-prompt\n"
        "[2026-09-23 15:04:20] 处理完成（失败 0 项），重启 ZCode\n"
        "[2026-09-23 15:04:20] DONE\n"
    )

    def test_never_woke_watchdog_explains_the_two_restarts(self):
        out = self._run_with(self._NEVER_WOKE)
        self.assertIn("从未等到 ZCode 退出", out,
                      "必须点名「起来了却从未等到退出」——否则用户不知道卡在哪")
        self.assertIn("重启两遍", out, "要把现象和原因对上：这就是必须重启两遍的原因")
        self.assertIn("没退出干净", out, "要点出「第一次其实没退出干净」这个真正原因")
        self.assertIn("tasklist", out, "要给出确认残留进程的命令")

    def test_completed_watchdog_does_not_warn(self):
        """对照组：正常完成过写入时不能报「从未等到退出」，否则用户会去白折腾退出流程。"""
        out = self._run_with(self._COMPLETED)
        self.assertNotIn("从未等到 ZCode 退出", out)
        self.assertIn("正常完成过写入", out)

    def test_missing_log_is_not_an_error(self):
        """没有看护日志属正常（无待办时不挂看护），不该报成故障。"""
        out = self._run_with(None)
        self.assertIn("还没挂过看护", out)
        self.assertNotIn("从未等到 ZCode 退出", out)

    def test_timed_out_watchdog_is_not_counted_as_pending(self):
        """等 24h 超时放弃的看护已被单独统计，不能重复计成「从未等到退出」而刷屏误导。"""
        log = ("[2026-09-23 00:00:00] 看护启动，等待 ZCode 退出…\n"
               "[2026-09-24 00:00:00] 等待超时（24h），放弃\n")
        out = self._run_with(log)
        self.assertNotIn("从未等到 ZCode 退出", out)
        self.assertIn("24h 超时", out)

    def test_relaunch_skipped_is_reported_as_benign(self):
        """「已再次运行，跳过重启」不影响补丁生效，不能让用户以为出了问题。"""
        log = self._COMPLETED + "[2026-09-23 15:04:20] 检测到 ZCode 已再次运行，跳过重启\n"
        out = self._run_with(log)
        self.assertIn("跳过重启", out)
        self.assertIn("不影响补丁生效", out)

    def test_section_runs_in_full_doctor(self):
        """第 7.5 节要被 main() 真正调用 —— 只写函数不接入等于没有。"""
        src = (Path(__file__).resolve().parent.parent
               / "skills" / "zcode-tokenspeed" / "scripts" / "doctor.py")
        text = src.read_text(encoding="utf-8")
        # 只看 main() 里的调用点（函数定义处也含同名子串，不能拿 index() 全局找）
        body = text[text.index("def main()"):]
        self.assertIn("check_heartbeat(dirs)", body)
        self.assertIn("check_watchdog()", body)
        # 接入位置：紧跟心跳检查之后（检查顺序即排查顺序）
        self.assertLess(body.index("check_heartbeat(dirs)"),
                        body.index("check_watchdog()"))
        # 而且要在补丁状态之前 —— 先解释「为什么没写进去」，再看「写进去的是什么」
        self.assertLess(body.index("check_watchdog()"), body.index("check_patches()"))

    def test_watchdog_log_vocabulary_is_pinned(self):
        """看护的日志措辞是 doctor 与 sync 共同依赖的接口，不能随手改。

        第 7.5 节靠这些子串分类：`看护启动` / `已退出（等待` / `DONE` /
        `已再次运行` / `等待超时`。apply_after_exit.py 改了措辞 →
        自检会静默地把所有看护都统计成「启动」而永远不报警。
        """
        src = (Path(__file__).resolve().parent.parent
               / "skills" / "zcode-tokenspeed" / "scripts" / "apply_after_exit.py")
        text = src.read_text(encoding="utf-8")
        for phrase in ("看护启动，等待 ZCode 退出", "ZCode 已退出（等待", "DONE",
                       "已再次运行", "等待超时"):
            self.assertIn(phrase, text, f"apply_after_exit.py 改变了措辞：{phrase}")


class TestSubprocessTimeouts(unittest.TestCase):
    """★ 回归：所有会阻塞的 `subprocess.run` 都必须带 `timeout=`（0.6.3 修复）。

    这两处此前没有超时，而它们跑在**无人值守**路径上，一旦挂死就再也没有人来收拾：

      * `bootstrap.py` 的 `run()` —— 要跑 `pip install` / `git clone`。
        上游半开连接（代理不响应、registry 卡住）时进程**永久挂起**，
        用户只看到「卡住不动」，没有任何可操作信息，只能强杀。
        现在默认 1200s，超时返回 124（与 GNU timeout 一致），并把已产出的部分输出
        一起带出来（例如 pip 卡在哪个包），便于定位。

      * `apply_after_exit.py` 的两处 —— 看护是后台无人值守进程，
        `tasklist` 可被 WMI 打嗝挂住、`zcode_patcher.py` 可在被锁的 asar 上挂住。
        现象会和「7.5 节：看护从未等到退出」长得一模一样，极难区分。
        现在 `tasklist` 30s（超时按「仍在运行」保守处理）、补丁 600s。

    用 AST 扫源码而不是行为测试：超时行为要真的挂 20 分钟才测得出来，
    但「有没有写 timeout=」是静态可判定的，而且这正是会被人手滑删掉的东西。
    """

    SCRIPTS = (Path(__file__).resolve().parent.parent
               / "skills" / "zcode-tokenspeed" / "scripts")

    def _runs_without_timeout(self, path: Path) -> list[int]:
        """返回该文件里所有「缺 timeout=」的 subprocess.run 的行号。"""
        import ast
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        lines = src.splitlines()
        missing = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if not (isinstance(f, ast.Attribute) and f.attr == "run"
                    and isinstance(f.value, ast.Name) and f.value.id == "subprocess"):
                continue
            if "timeout" in {k.arg for k in n.keywords}:
                continue
            seg = "\n".join(lines[n.lineno - 1: n.end_lineno])
            if "timeout" in seg:          # 跨行写在同一调用里
                continue
            missing.append(n.lineno)
        return missing

    def test_apply_after_exit_has_all_timeouts(self):
        """看护是无人值守进程，任何一个子进程都不允许无限等。"""
        missing = self._runs_without_timeout(self.SCRIPTS / "apply_after_exit.py")
        self.assertEqual(missing, [],
                         f"apply_after_exit.py 第 {missing} 行的 subprocess.run 缺 timeout=")

    def test_bootstrap_runner_has_timeout(self):
        """bootstrap 的 run() 要跑网络安装，必须有超时（默认值即可）。"""
        missing = self._runs_without_timeout(Path(__file__).resolve().parent.parent
                                            / "bootstrap.py")
        self.assertEqual(missing, [],
                         f"bootstrap.py 第 {missing} 行的 subprocess.run 缺 timeout=")

    def test_bootstrap_handles_timeout_expired(self):
        """有超时还不够 —— 必须**处理** TimeoutExpired，否则异常直接冒到用户面前。"""
        src = (Path(__file__).resolve().parent.parent / "bootstrap.py").read_text(encoding="utf-8")
        self.assertIn("subprocess.TimeoutExpired", src,
                      "bootstrap.py 没有捕获 TimeoutExpired：超时会抛裸异常")
        self.assertIn("DEFAULT_TIMEOUT", src, "缺少默认超时常量")
        self.assertIn("124", src, "超时的返回码约定应为 124（与 GNU timeout 一致）")

    def test_watchdog_tasklist_timeout_is_conservative(self):
        """tasklist 超时必须按「仍在运行」处理。

        反过来（当成已退出）会在 ZCode 还锁着 app.asar 时动手写 —— 直接触发
        WinError 5，而且此时看护已经错过退出时机，补丁永远写不进去。
        宁可多等一轮轮询。
        """
        src = (self.SCRIPTS / "apply_after_exit.py").read_text(encoding="utf-8")
        self.assertIn("TASKLIST_TIMEOUT", src)
        self.assertIn("return True", src,
                      "tasklist 超时分支必须 return True（视为仍在运行）")


class TestPluginHookSpec(unittest.TestCase):
    """`hooks/hooks.json` 必须落在内核那两个 zod schema 的字段表里。

    内核 `a7s()`（反编译自 `resources/glm/zcode.cjs`）对每个 matcher 跑
    `qz.safeParse(u)`；**解析失败就 `continue` 直接丢掉这个钩子**，只留一条
    `plugin_hook_invalid` / severity=error 的诊断。后果是「钩子静默消失、`hookCount` 变 0」，
    而日志里那条 error 很容易被忽略。所以这里把 schema 钉死，避免以后手滑加字段。

    内核原文（已核对）：
        _rs = G.object({type:G.literal("process"), command:G.string().min(1),
                        enabled:G.boolean().optional(), args:G.array(G.string()).optional(),
                        timeoutMs:G.number().int().positive().optional(),
                        statusMessage:G.string().optional()})
        yrs = G.object({type:G.literal("command"), command:G.string().min(1),
                        enabled:G.boolean().optional(), async:G.boolean().optional(),
                        shell:G.union([G.literal(!0), G.string().min(1)]).optional(),
                        timeout:G.number().positive().optional(),          // 秒
                        timeoutMs:G.number().int().positive().optional(),  // 毫秒
                        statusMessage:G.string().optional()})
        qz  = G.object({matcher:G.string().optional(), hooks:G.array(vrs).min(1)})
    """

    EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
              "PostToolUse", "PostToolUseFailure", "Stop"}
    COMMAND_FIELDS = {"type", "command", "enabled", "async", "shell",
                      "timeout", "timeoutMs", "statusMessage"}
    PROCESS_FIELDS = {"type", "command", "enabled", "args", "timeoutMs", "statusMessage"}

    def _spec(self) -> dict:
        return json.loads((_HERE.parent / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    def _hooks(self):
        for event, matchers in self._spec()["hooks"].items():
            for i, m in enumerate(matchers):
                for j, h in enumerate(m["hooks"]):
                    yield f"{event}[{i}].hooks[{j}]", h

    def test_declares_at_least_one_supported_event(self):
        events = set(self._spec()["hooks"])
        self.assertTrue(events, "hooks.json 至少要声明一个事件")
        self.assertLessEqual(events, self.EVENTS,
                             "内核只认这 7 个事件，多写会被报 plugin_hook_unsupported_event 并跳过")

    def test_matcher_objects_only_use_matcher_and_hooks(self):
        for event, matchers in self._spec()["hooks"].items():
            self.assertIsInstance(matchers, list, event)
            self.assertTrue(matchers, f"{event} 的 matcher 列表不能为空")
            for m in matchers:
                self.assertLessEqual(set(m), {"matcher", "hooks"}, f"{event}: {sorted(m)}")
                self.assertIsInstance(m["hooks"], list)
                self.assertTrue(m["hooks"], "hooks 数组至少要有一条（内核 min(1)）")

    def test_session_start_omits_matcher_to_match_every_session(self):
        """刻意**不写** matcher —— 省略即匹配全部。

        内核的 matcher 取值是 `startup` / `resume` / `clear` / `compact`。若写成
        `startup|clear|compact`，会静默漏掉 `resume`（用户从历史会话恢复时钩子不跑）；
        写死 `startup` 则「恢复会话」这条路径永远不触发。省略最稳。
        """
        for m in self._spec()["hooks"]["SessionStart"]:
            self.assertNotIn("matcher", m)

    def test_every_hook_uses_declared_fields_only(self):
        seen = []
        for path, h in self._hooks():
            seen.append(path)
            self.assertIn(h.get("type"), ("command", "process"), path)
            allowed = self.PROCESS_FIELDS if h["type"] == "process" else self.COMMAND_FIELDS
            extra = set(h) - allowed
            self.assertEqual(extra, set(), f"{path} 用了内核 schema 未声明的字段: {sorted(extra)}")
            self.assertIsInstance(h.get("command"), str, path)
            self.assertTrue(h["command"].strip(), f"{path} 的 command 不能为空（内核 min(1)）")
        self.assertTrue(seen, "一个钩子都没有？")

    def test_timeout_units_follow_the_schema(self):
        """`timeout` 是**秒**、`timeoutMs` 是**毫秒** —— 混用会让超时变得荒谬。

        内核里两者都存在（`c7s()` 把它们分别搬进 details），解析顺序是
        `timeoutMs` → `timeout×1000` → 配置的 `timeoutMs` → 默认 60000ms。
        所以写成 `"timeout": 120000` 会被当成 12 万秒（33 小时），
        而 `"timeoutMs": 120` 只有 0.12 秒，钩子还没拉起后台进程就被砍掉。
        """
        for path, h in self._hooks():
            if "timeout" in h:
                self.assertIsInstance(h["timeout"], (int, float), path)
                self.assertGreater(h["timeout"], 0, path)
                self.assertLess(h["timeout"], 600, f"{path}: timeout 的单位是秒，{h['timeout']} 太大了")
            if "timeoutMs" in h:
                self.assertIsInstance(h["timeoutMs"], int, path)
                self.assertGreater(h["timeoutMs"], 0, path)
                self.assertGreater(h["timeoutMs"], 1000, f"{path}: timeoutMs 的单位是毫秒")

    def test_hook_command_has_a_python3_fallback(self):
        """Windows 上是 `python`，macOS / Linux 上常常只有 `python3`。

        钩子命令是插件唯一的自动入口，写死 `python` 会让一半用户在 macOS/Linux 上
        静默什么都不发生（钩子被调用但命令不存在），所以必须带 `|| python3 ...` 兜底。
        """
        for path, h in self._hooks():
            self.assertIn("python", h["command"], path)
            self.assertIn("python3", h["command"], f"{path} 缺少 python3 兜底")
            self.assertIn("CLAUDE_PLUGIN_ROOT", h["command"],
                          f"{path} 应该用 ${{CLAUDE_PLUGIN_ROOT}} 定位脚本，不要写死路径")


# ------------------------------------------------------- bootstrap.py（引导脚本）

class TestBootstrapScript(unittest.TestCase):
    """`bootstrap.py` 声称「Windows / macOS / Linux 都能直接运行、不含任何本机绝对路径」。

    这类声明最容易静默失效：开发者在自己的机器上跑一次看到绿字，就把
    `D:\\ZCode` 或 `C:\\Users\\xxx` 留在了源码里；换台机器表现是「莫名其妙找不到文件」。
    所以这里**不靠人眼 review**，而是把声明逐条钉成断言。
    """

    _repo = None

    @classmethod
    def setUpClass(cls):
        # 仓库根：tests/ 的上一级
        cls._repo = Path(__file__).resolve().parent.parent
        cls._src_path = cls._repo / "bootstrap.py"
        if not cls._src_path.is_file():
            raise unittest.SkipTest("未找到 bootstrap.py")
        cls._src = cls._src_path.read_text(encoding="utf-8")

    # ---------- 1. 无硬编码本机路径 ----------

    def test_source_has_no_machine_specific_absolute_paths(self):
        """源码里不得出现任何本机绝对路径字面量。

        这是脚本最核心的承诺：换台电脑、换个用户名、装在别的盘也照样能跑。
        """
        # 注意：这里刻意列「本机真实路径」而不是泛化的正则，命中即说明真的写死了。
        banned = [
            "D:\\ZCode", "D:/ZCode",
            "C:\\Users\\80361", "C:/Users/80361",
            "F:\\ZcodeData", "F:/ZcodeData",
            "F:\\WorkBuddyAI", "F:/WorkBuddyAI",
            ".workbuddy-ai/binaries",
            "ZcodeData",
        ]
        hits = [b for b in banned if b in self._src]
        self.assertEqual(hits, [], f"bootstrap.py 写死了本机路径：{hits}")

    def test_repo_root_is_derived_from_dunder_file(self):
        """仓库根必须由 `__file__` 推导 —— 这样脚本放哪、从哪调用都对。"""
        self.assertIn("Path(__file__).resolve().parent", self._src,
                      "仓库根应该用 Path(__file__).resolve().parent 推导")

    # ---------- 2. 可执行文件靠自动检测 ----------

    def test_locates_executables_via_which_not_hardcoded(self):
        """必须实现 which/where 等价的查找，而不是拼一个猜测的安装路径。"""
        self.assertIn("shutil.which", self._src, "应该用 shutil.which 做 PATH 查找")
        self.assertIn("def which(", self._src, "应该提供 which() 包装（含 PATH 未命中的兜底）")

    def test_which_finds_python_and_returns_none_for_garbage(self):
        """`which()` 的真实行为：能查到 python，查不到的返回 None（不是抛异常）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("_bs_probe", self._src_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        self.assertTrue(mod.which("python", "python3", "py"),
                        "which() 应该能在 PATH 中找到 python")
        self.assertIsNone(mod.which("definitely-not-a-real-binary-xyz-42"),
                          "找不到时应返回 None，而不是抛异常")

    def test_which_fallback_scans_convention_dirs_without_crashing(self):
        """PATH 查找失败时必须能安全降级到「约定目录」扫描。

        这里复现的是实现过程中真实踩到的坑：早先的兜底逻辑写成
        `Path("/").glob(绝对模式)`，在 Windows 上会抛
        `UnsupportedOperation: cannot instantiate 'PosixPath' on your system`。
        它平时「看不出来」——因为 shutil.which 总能先命中，兜底分支根本没被走到。
        这条测试主动把 shutil.which 打桩成 None，逼出兜底分支。
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("_bs_probe2", self._src_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        orig_which, orig_name = mod.shutil.which, mod.os.name
        try:
            mod.shutil.which = lambda n: None      # 模拟 PATH 里啥都没有
            mod.os.name = "posix"                  # 顺便走一遍 POSIX 目录表
            try:
                result = mod.which("sh", "bash", "ls", "env")
            except Exception as exc:               # noqa: BLE001
                self.fail(f"兜底扫描抛异常了（应当干净返回 None）：{exc!r}")
            self.assertTrue(result is None or Path(result).is_file(),
                            f"返回了不存在的路径：{result!r}")
        finally:
            mod.shutil.which, mod.os.name = orig_which, orig_name

    def test_fallback_dirs_are_portable_not_baked_in(self):
        """兜底目录表必须是跨平台惯例写法，且不能固化当前机器的家目录。

        早先的实现用 `os.path.expanduser(...)` 在 **import 时**求值，
        等于把「本机家目录」写进了模块属性；而模块属性是在别的机器上也会被加载的。
        现在要求表里写 `~`，运行时才展开。
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location("_bs_probe3", self._src_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for key, dirs in mod._FALLBACK_DIRS.items():
            for d in dirs:
                self.assertNotIn("80361", d, f"{key} 表里固化了本机用户名：{d}")
                self.assertNotIn("ZCodeData", d, f"{key} 表里固化了本机路径：{d}")
        # 家目录相关的项必须写成 ~ 形式（运行时展开）
        home_like = [d for d in mod._FALLBACK_DIRS["posix"] if ".local" in d or "pyenv" in d]
        self.assertTrue(all(d.startswith("~/") for d in home_like),
                        f"家目录相关兜底项应写成 ~ 形式：{home_like}")
        self.assertIn("expanduser", self._src,
                      "应该在运行时用 expanduser 展开 ~，而不是 import 时求值")

    def test_optional_dependency_absence_is_tolerated(self):
        """Node 是可选依赖：缺失只能降级跳过，不能让整个引导失败。"""
        self.assertIn("def find_node(", self._src)
        # 找不到 Node 的分支必须是「跳过」，不是 die()
        self.assertIn("Node（可选）", self._src)
        self.assertIn("跳过已有脚本", self._src.replace("跳过注入脚本", "跳过已有脚本")
                      .replace("跳过滑条冒烟", "跳过已有脚本"))

    # ---------- 3. 跨平台执行细节 ----------

    def test_subprocess_calls_never_use_shell(self):
        """一律用列表传参、不经 shell —— 路径含空格/中文才安全，也避开 shell 语法差异。"""
        import ast
        tree = ast.parse(self._src)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run":
                for kw in node.keywords:
                    if kw.arg == "shell" and getattr(kw.value, "value", False):
                        offenders.append(node.lineno)
        self.assertEqual(offenders, [], f"第 {offenders} 行使用了 shell=True")

    def test_windows_children_suppress_console_window(self):
        """Windows 下起子进程必须带 CREATE_NO_WINDOW（否则每步闪一个 cmd 窗口）。"""
        self.assertIn("CREATE_NO_WINDOW", self._src)
        self.assertIn("0x08000000", self._src)

    def test_subprocess_output_is_decoded_leniently(self):
        """子进程输出按 UTF-8 + errors=replace 解码。

        直接 apply cp936 管道下的 bytes.decode() 会抛 UnicodeDecodeError 打断整段输出，
        而钩子/CI 环境恰恰会把 stdout 重定向到管道。
        """
        self.assertIn('errors="replace"', self._src)

    def test_no_high_unicode_glyphs_in_output(self):
        """输出只用 ASCII 标记（[+] / [x] / [!]），不依赖 ✓✗⚠ 这类字符。

        理由：cp936 管道里打印这些字符会抛 UnicodeEncodeError（见 _console.py 的说明）。
        """
        bad = [g for g in "✓✗⚠✅↻✔✘" if g in self._src]
        self.assertEqual(bad, [], f"bootstrap.py 使用了高位 Unicode 符号：{bad}")

    # ---------- 4. 步骤编排 ----------

    def test_step_names_are_consistent(self):
        """STEP_ORDER 里每个名字都要有标题和处理器，别留下半截。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("_bs_steps", self._src_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for name in mod.STEP_ORDER:
            self.assertIn(name, mod.STEP_TITLES, f"步骤 {name} 缺标题")
            self.assertTrue(hasattr(mod, f"step_{name}"), f"步骤 {name} 缺实现函数")

    def test_dry_run_never_executes_writes(self):
        """`--dry-run` 必须真的不执行命令（只打印），否则「预演」就失去意义。"""
        self.assertIn("def run(", self._src)
        # run() 在 dry_run 时应当提前返回，不落到 subprocess.run
        body = self._src.split("def run(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if dry_run:", body)
        dry_pos = body.index("if dry_run:")
        exec_pos = body.index("subprocess.run")
        self.assertLess(dry_pos, exec_pos,
                        "dry_run 判断必须在 subprocess.run 之前")


# --------------------------------------------------------------------------- #
# autopilot.py —— 全自动流水线（无人工干预）
# --------------------------------------------------------------------------- #

def _load_autopilot():
    """把 autopilot.py 当独立模块加载。

    必须**先登记进 sys.modules**：文件里用了 `@dataclass`，dataclass 在装饰时会
    回头查 `sys.modules[cls.__module__]`，没登记就抛
    `AttributeError: 'NoneType' object has no attribute '__dict__'`
    —— 报错位置在装饰器里，看起来完全不像「模块没登记」，很难猜。
    """
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "autopilot.py"
    if not path.is_file():
        raise unittest.SkipTest("未找到 autopilot.py")
    spec = importlib.util.spec_from_file_location("_ap_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ap_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod, path


class TestAutopilotErrorClassification(unittest.TestCase):
    """`autopilot.py` 的错误分类是所有重试决策的唯一依据。

    分错类的后果不是「报错难看」，而是**行为错误**：
      * 把「文件被占用」判成不可恢复 → 明明等 1 秒就好，却直接失败收工；
      * 把「语法错误」判成可恢复 → 白白重试 3 轮、每次退避到 8 秒，
        用户盯着屏幕等半分钟才看到一句 SyntaxError。

    这些关键词的**判断顺序**也和内容一样重要（例如 "timed out" 必须归到
    TIMEOUT 而不是 NETWORK），所以下面用真实报错原文逐条钉死。
    """

    @classmethod
    def setUpClass(cls):
        cls.ap, cls._path = _load_autopilot()

    def test_known_error_texts_map_to_expected_kind(self):
        cases = [
            # —— 锁：客户端还在跑 / 文件句柄没释放 ——
            ("检测到 ZCode 正在运行，拒绝写入", "lock"),
            ("[WinError 32] The process cannot access the file", "lock"),
            # —— 权限 ——
            ("Permission denied: '/opt/ZCode/app.asar'", "permission"),
            ("拒绝访问", "permission"),
            # —— 网络 ——
            ("Connection reset by peer", "network"),
            ("proxy CONNECT aborted / tunnel connection failed", "network"),
            # —— 超时：注意这里刻意用 "timed out" 的原文，
            #    它早先被塞进 NETWORK 关键词表，导致超时永远不按超时退避 ——
            ("Command timed out after 300s", "timeout"),
            # —— 构建：语法错误不该重试 ——
            ("SyntaxError: invalid syntax (line 42)", "build"),
            # —— 测试：unittest 的失败摘要 ——
            ("FAILED (failures=3)", "test"),
            ("FAILED (errors=1)", "test"),
            # —— 依赖缺失 ——
            ("python.exe: command not found", "dep"),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    self.ap.classify(text), want,
                    f"{text!r} 应归为 {want}，实际 {self.ap.classify(text)!r}")

    def test_only_transient_kinds_are_retried(self):
        """可重试集合必须**只**包含「等一会儿就会好」的类型。

        多一个（比如 build）就是白等；少一个（比如 lock）就是把可自愈的问题
        直接判死。两个方向都在这条断言里钉住。
        """
        self.assertEqual(
            set(self.ap.ErrorKind.RETRYABLE),
            {"transient", "network", "timeout", "lock"},
            "可重试类别集合发生了变化，请同时更新退避策略的说明")

    def test_non_retryable_kinds_are_excluded(self):
        for k in ("permission", "build", "test", "env", "dep", "unknown"):
            with self.subTest(kind=k):
                self.assertNotIn(k, self.ap.ErrorKind.RETRYABLE)


class TestAutopilotRetryExecutor(unittest.TestCase):
    """`execute_step()` 的重试语义。

    这里防的是一个**已经真实发生过**的退化：实现早期写成
    `retryable = exc.kind in RETRYABLE and not exc.fatal`，
    而 `StepFailure.fatal` 默认就是 `True` —— 于是所有可恢复错误都在
    “fatal 保护”的名义下被静默剥夺了重试机会，重试机制**看起来实现了、实际从未生效**。
    更糟的是它不会报任何错，只表现为「偶尔失败」。
    下面的用例专门覆盖这个反例。
    """

    @classmethod
    def setUpClass(cls):
        cls.ap, cls._path = _load_autopilot()

    def _log(self, d):
        return self.ap.Logger(Path(d) / "t.log", echo=False)

    def test_retryable_error_retries_then_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            log = self._log(d)
            state = {"n": 0}

            def flaky(lg, dr):
                state["n"] += 1
                if state["n"] < 3:
                    # fatal 保持默认 True —— 正是过去把这个字段当“禁止重试”用的场景
                    raise self.ap.StepFailure("检测到 ZCode 正在运行",
                                              kind=self.ap.ErrorKind.LOCK)

            r = self.ap.execute_step("deploy", flaky, log,
                                     max_retries=5, dry_run=True)
            self.assertEqual(r.status, "ok", f"detail={r.detail}")
            self.assertEqual(r.retries_used, 2)
            self.assertEqual(state["n"], 3)

    def test_non_retryable_error_fails_on_first_attempt(self):
        """语法错误必须**一次就停** —— 重试不可能把写错的代码变对。"""
        with tempfile.TemporaryDirectory() as d:
            log = self._log(d)
            calls = {"n": 0}

            def broken(lg, dr):
                calls["n"] += 1
                raise self.ap.StepFailure("SyntaxError: bad",
                                          kind=self.ap.ErrorKind.BUILD)

            r = self.ap.execute_step("build", broken, log,
                                     max_retries=5, dry_run=True)
            self.assertEqual(calls["n"], 1, "不可恢复错误不该被重试")
            self.assertEqual(r.status, "failed")
            self.assertEqual(r.kind, self.ap.ErrorKind.BUILD)

    def test_retry_count_is_bounded_by_max_retries(self):
        with tempfile.TemporaryDirectory() as d:
            log = self._log(d)
            calls = {"n": 0}

            def always_locked(lg, dr):
                calls["n"] += 1
                raise self.ap.StepFailure("WinError 32 文件被占用",
                                          kind=self.ap.ErrorKind.LOCK)

            r = self.ap.execute_step("deploy", always_locked, log,
                                     max_retries=2, dry_run=True)
            self.assertEqual(calls["n"], 3, "总尝试次数应为 max_retries+1")
            self.assertEqual(r.status, "failed")
            # 这条断言防的是一个已经踩过的统计口径错误：原实现里
            # retries_used 只在**最终成功**的分支赋值，于是「失败了但重试过 2 次」
            # 会被报成「重试 0 次」—— 汇总报告与 JSONL 都在撒谎，
            # 而它恰恰是最需要用户看到的场景（重试了还是不行）。
            self.assertEqual(r.retries_used, 2)

    def test_unexpected_exception_is_classified_not_dropped(self):
        """没抛 StepFailure 的意外异常也要被接住、归类、留痕，而不是冒泡炸掉整条流水线。"""
        with tempfile.TemporaryDirectory() as d:
            log = self._log(d)

            def boom(lg, dr):
                raise ValueError("Permission denied while opening file")

            r = self.ap.execute_step("env", boom, log,
                                     max_retries=0, dry_run=True)
            self.assertEqual(r.status, "failed")
            self.assertEqual(r.kind, self.ap.ErrorKind.PERMISSION, r.kind)
            self.assertIn("ValueError", r.detail, "应保留原始异常类型名便于排查")

    def test_dry_run_does_not_sleep(self):
        """`--dry-run` 下重试不能真的 sleep，否则预演要等好几个 8 秒。"""
        with tempfile.TemporaryDirectory() as d:
            log = self._log(d)
            calls = {"n": 0}

            def always_locked(lg, dr):
                calls["n"] += 1
                raise self.ap.StepFailure("占用", kind=self.ap.ErrorKind.LOCK)

            t0 = time.time()
            self.ap.execute_step("deploy", always_locked, log,
                                 max_retries=4, dry_run=True)
            self.assertLess(time.time() - t0, 1.0,
                            "dry-run 下重试退避应被跳过（否则就是真的在等）")
            self.assertEqual(calls["n"], 5)


class TestAutopilotLogger(unittest.TestCase):
    """双通道日志：人看 `.log`，机器读 `.jsonl`。

    这个项目里「机器读」不是锦上添花 —— 看护脚本、CI、doctor 都要靠事件流判断
    流水线到底停在哪一步，所以 JSONL 的**字段名**是接口，必须有测试兜住。
    """

    @classmethod
    def setUpClass(cls):
        cls.ap, cls._path = _load_autopilot()

    def test_dual_channel_files_are_both_written(self):
        with tempfile.TemporaryDirectory() as d:
            lp = Path(d) / "run.log"
            log = self.ap.Logger(lp, echo=False)
            log.begin("build", "构建")
            log.info("普通信息")
            log.warn("警告信息")
            log.err("错误信息")

            self.assertTrue(lp.is_file() and lp.stat().st_size > 0, "文本日志未写入")
            jl = lp.with_suffix(".jsonl")
            self.assertTrue(jl.is_file() and jl.stat().st_size > 0, "JSONL 日志未写入")

            # 文本日志必须是 UTF-8 —— cp936 环境下写中文不能炸
            self.assertIn("警告信息", lp.read_text(encoding="utf-8"))

    def test_jsonl_is_one_object_per_line_with_stable_fields(self):
        with tempfile.TemporaryDirectory() as d:
            lp = Path(d) / "run.log"
            log = self.ap.Logger(lp, echo=False)
            log.begin("build", "构建")
            log.event("retry", "重试", kind=self.ap.ErrorKind.LOCK, extra={"delay": 2})
            log.event("step-end", "build failed", level="error",
                      extra={"duration": 1.5, "attempts": 2})

            lines = lp.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()
            entries = [json.loads(x) for x in lines]
            self.assertEqual(len(entries), len(lines), "存在解析不了的行")

            self.assertTrue(any(e.get("step") == "build" for e in entries),
                            "缺少 step 字段")
            self.assertTrue(any(e.get("kind") == self.ap.ErrorKind.LOCK for e in entries),
                            "缺少 kind 字段")
            self.assertTrue(any("duration" in e for e in entries), "缺少 duration 字段")
            self.assertTrue(any(e["event"] == "retry" for e in entries), "缺少 retry 事件")
            for e in entries:
                self.assertIn("ts", e, "每条事件都要带时间戳")

            # ISO 8601 ⇒ 字典序 == 时间序，CI 里可以直接 sort/比较
            ts = [e["ts"] for e in entries]
            self.assertEqual(ts, sorted(ts), "时间戳不是可排序的 ISO8601")

    def test_logger_without_path_writes_nothing(self):
        """`Logger(None)` 必须完全静默 —— dry-run / 单测里不能到处漏出文件。"""
        log = self.ap.Logger(None, echo=False)
        self.assertIsNone(log.text_path)
        log.info("不该落盘")

    def test_step_end_event_carries_retry_and_duration(self):
        """`step-end` 是流水线的权威记录点，缺失字段会让报告无从统计。"""
        with tempfile.TemporaryDirectory() as d:
            lp = Path(d) / "run.log"
            log = self.ap.Logger(lp, echo=False)
            self.ap.execute_step("env", lambda lg, dr: None, log,
                                 max_retries=0, dry_run=True)
            entries = [json.loads(x) for x in
                       lp.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()]
            ends = [e for e in entries if e["event"] == "step-end"]
            self.assertEqual(len(ends), 1, f"应有且仅有一条 step-end：{entries}")
            self.assertEqual(ends[0]["step"], "env")
            self.assertIn("duration", ends[0])
            self.assertIn("attempts", ends[0])
            self.assertIn("retries", ends[0])


class TestAutopilotSummary(unittest.TestCase):
    """汇总报告是**交给人看的唯一产物**（终端刷过去就没了）。

    所以它必须回答三个问题：哪一步挂了、为什么挂、接下来怎么办。
    这条测试把「为什么/怎么办」也钉住 —— 只报「build 失败」的日志，
    用户还得自己回来读源码才知道怎么修。
    """

    @classmethod
    def setUpClass(cls):
        cls.ap, cls._path = _load_autopilot()

    def test_summary_reports_failure_kind_and_remedy(self):
        results = [
            self.ap.StepResult(name="env", status="ok", duration=0.1),
            self.ap.StepResult(name="build", status="failed",
                               kind=self.ap.ErrorKind.BUILD,
                               detail="SyntaxError: invalid syntax\n  line 2",
                               duration=1.2),
            self.ap.StepResult(name="test", status="skipped", detail="前序步骤失败"),
        ]
        rep = self.ap.build_summary(results, elapsed=3.4, log_path=Path("logs/x.log"),
                                    deps={"client_version": "3.14.3",
                                          "node": "/usr/bin/node"},
                                    deferred_step={})
        self.assertIn("失败 1 步", rep)
        self.assertIn("构建失败", rep, "应把错误类别翻成中文可读名")
        self.assertIn("改完代码重跑", rep, "应附上针对该类错误的修复建议")
        # 路径分隔符随平台变化（Windows 是反斜杠），所以比文件名而不是完整路径
        self.assertIn("x.log", rep)
        self.assertIn("jsonl", rep, "应提示机器可读日志的位置")
        self.assertIn("3.14.3", rep, "应带上客户端版本，便于对号入座")

    def test_summary_explains_deferred_deploy_needs_no_human(self):
        """部署被排期（客户端正在运行）时，报告必须说清「不用你做任何事」。

        否则用户看到 deploy 是 ok 却没生效，只会以为流水线骗人。
        """
        rep = self.ap.build_summary(
            [self.ap.StepResult(name="deploy", status="ok", duration=1.0)],
            elapsed=1.0, log_path=None, deps={},
            deferred_step={"scheduled": True})
        self.assertIn("已排期", rep)
        self.assertIn("无需人工", rep)

    def test_every_retryable_kind_has_a_remedy(self):
        """每个可能出现在报告里的错误类别都要有对应建议，不能漏成空字符串。"""
        for kind in (self.ap.ErrorKind.LOCK, self.ap.ErrorKind.NETWORK,
                     self.ap.ErrorKind.TIMEOUT, self.ap.ErrorKind.PERMISSION,
                     self.ap.ErrorKind.BUILD, self.ap.ErrorKind.TEST,
                     self.ap.ErrorKind.DEP, self.ap.ErrorKind.ENV):
            with self.subTest(kind=kind):
                r = self.ap.StepResult(name="build", status="failed", kind=kind)
                self.assertTrue(self.ap._remedy(r).strip(),
                                f"{kind} 缺少修复建议")


class TestAutopilotSafety(unittest.TestCase):
    """流水线是「无人值守」的 —— 安全边界只能靠代码本身守，没有人在旁边喊停。"""

    @classmethod
    def setUpClass(cls):
        cls.ap, cls._path = _load_autopilot()
        cls._src = cls._path.read_text(encoding="utf-8")

    def test_no_machine_specific_absolute_paths(self):
        banned = [
            "D:\\ZCode", "D:/ZCode",
            "C:\\Users\\80361", "C:/Users/80361",
            "F:\\ZcodeData", "F:/ZcodeData",
            "F:\\WorkBuddyAI", "F:/WorkBuddyAI",
            ".workbuddy-ai/binaries",
        ]
        hits = [b for b in banned if b in self._src]
        self.assertEqual(hits, [], f"autopilot.py 写死了本机路径：{hits}")

    def test_repo_root_derived_from_dunder_file(self):
        self.assertIn("Path(__file__).resolve().parent", self._src)

    def test_running_client_defers_instead_of_forcing(self):
        """客户端在跑时必须**转交看护**，绝不能绕过运行守卫硬写。

        这条是整条流水线最关键的安全约束：绕过守卫直接改写 app.asar，
        轻则被客户端覆写回去，重则让正在运行的实例读到半截文件。
        """
        self.assertIn("_zcode_running()", self._src,
                      "部署前必须检查客户端是否在运行")
        body = self._src.split("def step_deploy(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("apply_after_exit", body,
                      "客户端在运行时应当挂看护脚本，而不是直接写")
        self.assertIn("_zcode_running()", body)

    def test_run_guard_is_delegated_to_zcode_patcher(self):
        """运行守卫必须复用 `zcode_patcher` 的实现，不能自己另写一份。

        自己再写一份 `tasklist` 解析，迟早会和主实现漂移（客户端改进程名、
        守卫那边补了「正在退出中」的宽限期判断……）。而这里的判断决定的正是
        「直接写 app.asar」还是「转交看护」—— 判错就会去动正在运行的文件。

        同时这条也钉住了「拿不到主实现时不能让模块 import 失败」：
        CI 里经常只把本文件单独加载，没有 scripts 目录在 sys.path 上。
        """
        self.assertIsNotNone(self.ap._native_zcode_running,
                             "应提供 _native_zcode_running() 包装")
        # 在本仓库环境里，scripts 目录是可导入的，守卫应当真的借到了
        self.assertIsNotNone(self.ap._import_run_guard(),
                             "在本仓库里应当能导入 zcode_patcher.zcode_running")
        guard = self.ap._import_run_guard()
        self.assertTrue(callable(guard))
        # 借来的必须就是主实现本体，而不是又被包了一层
        import zcode_patcher as zp
        self.assertIs(guard, zp.zcode_running,
                      "_native_zcode_running 必须直接复用 zcode_patcher.zcode_running")

    def test_running_guard_unavailable_falls_back_without_crashing(self):
        """主实现不可用时要降级到自带探测，而不是抛异常拖垮流水线。

        `_native_zcode_running()` 用 None 表示「拿不到权威判断」，
        必须和「客户端没在运行」的 False 区分开 —— 混为一谈会让降级路径
        在客户端真的在跑时也返回「没在跑」，进而绕过运行守卫。
        """
        saved = self.ap._NATIVE_ZCODE_RUNNING
        try:
            self.ap._import_run_guard = lambda: None      # 模拟导入不到
            self.ap._NATIVE_ZCODE_RUNNING = None
            self.assertIsNone(self.ap._native_zcode_running(),
                              "拿不到守卫时应返回 None（而不是 False）")
            # 兜底实现仍然要给出一个 bool，而不是崩掉
            self.ap._native_zcode_running = lambda: None
            self.assertIsInstance(self.ap._zcode_running(), bool)
        finally:
            self.ap._NATIVE_ZCODE_RUNNING = saved

    def test_children_suppress_console_window_on_windows(self):
        self.assertIn("CREATE_NO_WINDOW", self._src)
        self.assertIn("0x08000000", self._src)

    def test_subprocess_calls_avoid_shell(self):
        import ast
        tree = ast.parse(self._src)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run":
                for kw in node.keywords:
                    if kw.arg == "shell" and getattr(kw.value, "value", False):
                        offenders.append(node.lineno)
        self.assertEqual(offenders, [], f"第 {offenders} 行使用了 shell=True")

    def test_no_high_unicode_glyphs_in_source(self):
        """源码里不得出现 ✓✗⚠ 这类字符。

        它们既可能出现在 print 里（cp936 管道下抛 UnicodeEncodeError），
        也可能被写进 .cmd 批处理（cmd.exe 按 OEM 代码页解析，非 ASCII 会破坏语法）。
        统一用 [*]/[+]/[!]/[x] 标记。
        """
        bad = [g for g in "✓✗⚠✅↻✔✘" if g in self._src]
        self.assertEqual(bad, [], f"autopilot.py 使用了高位 Unicode 符号：{bad}")

    def test_never_downloads_and_pipes_a_binary(self):
        """自动装依赖只走包管理器，不 `curl ... | sh`。

        无人值守场景下静默下载并执行二进制是不可接受的安全风险
        （无人复核、出错也没人拦）。

        注意断言的是**可执行行为**，不是字符串出现 —— 实现里有一句
        「不去 curl 下载安装包」的说明性注释，用子串匹配会把注释也判成违规。
        """
        src = self._src
        # 真正的风险形态是「下载器 + 管道进解释器」
        for pat in (r"\|\s*(?:sudo\s+)?(?:ba)?sh\b",
                    r"\|\s*python[0-9.]*\s*$",
                    r"Invoke-Expression",
                    r"\biex\b"):
            with self.subTest(pattern=pat):
                self.assertIsNone(re.search(pat, src, re.MULTILINE),
                                  f"不应出现下载即执行的形态：{pat}")
        # curl/wget 本身不算违规（下载到文件是可接受的），但必须没有管道执行
        m = re.search(r"(curl|wget)[^\n]*\|", src)
        self.assertIsNone(m, f"curl/wget 被接进了管道：{m.group(0) if m else ''}")

    def test_step_names_have_titles_and_handlers(self):
        """STEPS 里每个名字都要有标题和实现，别留下半截。

        `deps` 的实现函数叫 `ensure_dependencies` 而不是 `step_deps`
        —— 它还要把依赖清单**返回**给 deploy/verify 用。所以这里查
        `STEP_FUNCS` 映射表，而不是硬按 `step_<name>` 拼名字。
        """
        self.assertEqual(self.ap.STEPS, list(self.ap.STEP_FUNCS),
                         "STEPS 与 STEP_FUNCS 的键必须一一对应且顺序一致")
        for name in self.ap.STEPS:
            with self.subTest(step=name):
                self.assertIn(name, self.ap.STEP_TITLES, f"步骤 {name} 缺标题")
                fn_name = self.ap.STEP_FUNCS[name]
                self.assertTrue(hasattr(self.ap, fn_name),
                                f"步骤 {name} 的实现 {fn_name}() 不存在")
                self.assertTrue(callable(getattr(self.ap, fn_name)))

    def test_exit_codes_are_distinct_and_documented(self):
        """退出码是给 CI/看护读的接口：必须彼此不同，且文档里说清了语义。"""
        codes = {
            "EXIT_OK": self.ap.EXIT_OK,
            "EXIT_FAILED": self.ap.EXIT_FAILED,
            "EXIT_BAD_ARGS": self.ap.EXIT_BAD_ARGS,
            "EXIT_ENV": self.ap.EXIT_ENV,
            "EXIT_DEP": self.ap.EXIT_DEP,
            "EXIT_DEPLOY_BLOCKED": self.ap.EXIT_DEPLOY_BLOCKED,
            "EXIT_INTERRUPTED": self.ap.EXIT_INTERRUPTED,
        }
        self.assertEqual(codes["EXIT_OK"], 0)
        self.assertEqual(len(set(codes.values())), len(codes),
                         f"退出码有重复（外部无法区分）：{codes}")
        # 全部非 0 码都必须落在 1..255 这个进程退出码的合法区间里
        for name, code in codes.items():
            with self.subTest(code=name):
                self.assertTrue(0 <= code <= 255, f"{name}={code} 超出退出码范围")
        # 文档字符串是这套码对外唯一的说明处，逐个核对
        for name, code in codes.items():
            with self.subTest(documented=name):
                self.assertIn(str(code), self._src, f"{name} 未在文档字符串中说明")

    def test_failure_exit_code_reflects_which_step_broke(self):
        """env/dep 挂掉要返回各自的专用码 —— 否则调用方只能去猜。"""
        mk = lambda kind, name="env": self.ap.StepResult(  # noqa: E731
            name=name, status="failed", kind=kind)
        self.assertEqual(self.ap._exit_code_for(mk(self.ap.ErrorKind.ENV)),
                         self.ap.EXIT_ENV)
        self.assertEqual(self.ap._exit_code_for(mk(self.ap.ErrorKind.DEP, "deps")),
                         self.ap.EXIT_DEP)
        self.assertEqual(
            self.ap._exit_code_for(mk(self.ap.ErrorKind.LOCK, "deploy")),
            self.ap.EXIT_DEPLOY_BLOCKED)
        self.assertEqual(self.ap._exit_code_for(mk(self.ap.ErrorKind.BUILD, "build")),
                         self.ap.EXIT_FAILED)


class TestAutopilotCli(unittest.TestCase):
    """命令行入口的行为契约（子进程真跑，捕获取消就露不出来）。"""

    @classmethod
    def setUpClass(cls):
        cls.ap, cls._path = _load_autopilot()

    def _run(self, *args, timeout=180):
        p = subprocess.run([sys.executable, str(self._path), *args],
                           cwd=str(self._path.parent),
                           capture_output=True, errors="replace",
                           encoding="utf-8", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def test_missing_argument_is_rejected_with_hint(self):
        """步骤名写错时必须返回「参数错误」专用码（2），且提示合法取值。

        CI 就是靠退出码区分「配置写错」和「测试不过」的 —— 都返回 1 会让
        排查方向完全跑偏。
        """
        rc, out = self._run("--only", "definitely-not-a-step")
        self.assertEqual(rc, 2, f"应返回参数错误码 2，实际 {rc}\n{out[-600:]}")
        self.assertIn("definitely-not-a-step", out)
        self.assertIn("env", out, "应把合法步骤名列出来")

    def test_quiet_mode_suppresses_progress_but_keeps_verdict(self):
        """`--quiet` 只该压掉过程噪音，结论行必须留着（日志分析全靠它）。"""
        rc, out = self._run("--only", "env", "--quiet", "--unattended")
        self.assertEqual(rc, 0, f"rc={rc}\n{out[-800:]}")
        self.assertIn("结论", out, "quiet 模式也必须输出结论行")
        self.assertLess(len(out.splitlines()), 30, "quiet 模式行数过多，没真正安静")


class TestRunnerEntrypoints(unittest.TestCase):
    """`run.sh` / `run.cmd` —— 「一条命令跑完全流程」的入口。

    跨平台入口最难测也最容易坏：**开发机器上永远是好的**。
    真出事的地方是「从别的目录调用」「路径含空格」「换台机器换了 Python 安装方式」，
    所以这里刻意用不同调用姿势去跑，而不是只测一次 `./run.sh`。
    """

    @classmethod
    def setUpClass(cls):
        cls._repo = Path(__file__).resolve().parent.parent
        cls._sh = cls._repo / "run.sh"
        cls._cmd = cls._repo / "run.cmd"
        if not cls._sh.is_file() and not cls._cmd.is_file():
            raise unittest.SkipTest("未找到 run.sh / run.cmd")

    # ---------- run.cmd（Windows） ----------

    def test_cmd_script_is_pure_ascii(self):
        """`run.cmd` 必须**全 ASCII**。

        cmd.exe 解析 .cmd/.bat 用的是 **OEM 代码页**（中文 Windows = 936/GBK），
        不是 UTF-8。文件里一旦有中文注释，cmd 会按 GBK 把字节流拆错，
        报出 `'�?rem' 不是内部或外部命令` 这种完全指不到根因的错误 —— 
        实测直接把整份脚本跑崩（rc=255）。
        """
        if not self._cmd.is_file():
            self.skipTest("无 run.cmd（非 Windows 布局）")
        raw = self._cmd.read_bytes()
        bad = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        if bad:
            line = raw[:bad[0][0]].count(b"\n") + 1
            self.fail(f"run.cmd 第 {line} 行含非 ASCII 字节 {bad[0][1]:#x}；"
                      "cmd.exe 会按 OEM 代码页解析，导致语法错乱")

    @unittest.skipUnless(os.name == "nt", "run.cmd 仅 Windows 可执行")
    def test_cmd_runs_and_returns_zero(self):
        if not self._cmd.is_file():
            self.skipTest("无 run.cmd")
        p = subprocess.run(["cmd.exe", "/c", "run.cmd", "--only", "env", "--quiet"],
                           cwd=str(self._repo), capture_output=True,
                           errors="replace", encoding="utf-8", timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        self.assertEqual(p.returncode, 0, f"rc={p.returncode}\n{out[-800:]}")
        self.assertIn("结论", out)
        self.assertNotIn("请按任意键", out)
        self.assertNotIn("Press any key", out)

    @unittest.skipUnless(os.name == "nt", "run.cmd 仅 Windows 可执行")
    def test_cmd_propagates_failure_exit_code(self):
        """参数错误必须把非 0 退出码透传出去（批处理最容易吞掉 ERRORLEVEL）。"""
        if not self._cmd.is_file():
            self.skipTest("无 run.cmd")
        p = subprocess.run(["cmd.exe", "/c", "run.cmd", "--only", "bogus-step"],
                           cwd=str(self._repo), capture_output=True,
                           errors="replace", encoding="utf-8", timeout=180)
        self.assertNotEqual(p.returncode, 0, "参数错误却返回了成功")

    # ---------- run.sh（POSIX / Git Bash） ----------

    @unittest.skipIf(os.name == "nt" and not shutil.which("sh"),
                     "无 sh 可用")
    def test_sh_is_invokable_from_multiple_cwds(self):
        """不管从哪调用、怎么拼路径，都不能被 MSYS 路径改写污染。

        真实踩过的坑：`pwd -P` 在 Git Bash 里给的是 `/f/ZcodeData/...`，
        再交给原生 python.exe 时 MSYS 会把它当成「相对路径」二次转换，
        最终变成 `F:\\f\\ZcodeData\\...` —— 一个**看起来像路径、其实不存在**的东西，
        报错是「找不到 autopilot.py」，和真实原因（路径改写）毫无关系。
        """
        if not self._sh.is_file():
            self.skipTest("无 run.sh")
        invocations = [
            ("相对路径 ./run.sh", str(self._repo), ["sh", "./run.sh"]),
            ("绝对 POSIX 路径", str(self._repo),
             ["sh", str(self._sh).replace("\\", "/")]),
            ("从上级目录调用", str(self._repo.parent),
             ["sh", f"{self._repo.name}/run.sh"]),
        ]
        for label, cwd, argv in invocations:
            with self.subTest(label=label):
                p = subprocess.run([*argv, "--only", "env", "--quiet"],
                                   cwd=cwd, capture_output=True,
                                   errors="replace", encoding="utf-8", timeout=180)
                out = (p.stdout or "") + (p.stderr or "")
                self.assertEqual(p.returncode, 0, f"{label}: rc={p.returncode}\n{out[-800:]}")
                self.assertIn("结论", out, f"{label}: 没有汇总输出")
                lowered = out.lower()
                self.assertNotIn("f:\\f\\", lowered,
                                 f"{label}: 路径被子串转换污染（F:\\f\\...）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
