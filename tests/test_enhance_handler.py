#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「增强提示词」模型解析 / 右键菜单选模型 —— 行为级回归测试
=========================================================
两代功能叠加在这个 handler 上：

(A) **热配置档**（外部贡献的 PR#1）：每次点击都热读 `<root>/enhance_config.json`，
    可指定 providerId/modelId/maxTokens/temperature 与思考强度，改文件即生效、零重启。

(B) **右键菜单选模型**（0.6.7）：菜单选中 → 写进同一个 enhance_config.json（零重启生效），
    同时本次点击经 `override` 参数走 **explicit 档**（最高优先级）。
    菜单的模型清单复用 `{list:true}` 分支，与真正发请求时**同源**。

这两块都极易静默失效：
  * 优先级排错 → 用户配了却不生效，或反过来把界面选择也劫持了；
  * 新插「最高优先级」分支忘了给后面的 `pick=` 补 `!pick` → 语法对、位置对、行为全静默；
  * 键名拼错（`reasoning_effort` vs `reasoningEffort`）→ 配置被无声忽略；
  * 「注释掉的键」若被当成空串发出去，会让中转站整条请求 400；
  * 菜单里能选、点了却报不可用（清单与候选表两套逻辑漂移）；
  * 菜单选完没传进请求（选了 A 实际用 B）。

所以这里**真跑 handler**（提取源码 → 替换 H/SYS/TPL 标记 → 在 node 里执行），
并用一个本地 HTTP 假服务器收包，直接断言**收到的请求体**长什么样。
比只做源码字符串断言强得多：能抓住「语法对、语义错」。

覆盖：
  ① 配置生效：providerId/modelId 指哪打哪
  ② 配置覆盖界面选择（⓪ 优先于 ①ref 档）
  ③ 向后兼容：无配置文件 / 只配一半 → 回退到界面选择
  ④ 可用性守卫：配置指向不可用供应商 → 回退，且 tried 里可见原因
  ⑤ 参数：maxTokens / temperature 真的进了 body
  ⑥ 思考强度：openai 发 reasoning_effort、anthropic 发 thinking.budget_tokens
     且 anthropic 的 max_tokens 自动抬到 budget+1024
  ⑦ **注释掉的键不发**（防「空串参数」把请求搞坏）
  ⑧ 档位标注：成功时 how == "config"，便于线上定位走的是哪一档
  ⑨ ★ explicit 档（右键菜单本次指定）压过 ⓪config 与 ①ref
  ⑩ ★ `{list:true}` 清单模式：只回候选表、**一个请求都不发**
  ⑪ ★ `zcode:enhance-model-save`：持久化选择，且保留其它热配置参数
  ⑫ ★ `enhance_doctor.py` 的档位顺序必须与 handler 同构（复刻不能漂移）
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parent / "scripts",
              _HERE.parent / "skills" / "zcode-tokenspeed" / "scripts"):
    if (_cand / "zcode_patcher.py").is_file():
        sys.path.insert(0, str(_cand))
        break
import zcode_patcher as zp          # noqa: E402


# --------------------------------------------------------------- 假 HTTP 服务器

class _Capture:
    """收集所有到达的请求（供断言）。"""

    def __init__(self):
        self.requests = []          # [(path, body_dict, headers_dict), ...]
        self.lock = threading.Lock()

    def add(self, path, body, headers):
        with self.lock:
            self.requests.append((path, body, headers))

    @property
    def last(self):
        with self.lock:
            return self.requests[-1] if self.requests else None


def _make_server(capture: _Capture, reply_text="OK-ENHANCED"):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):                                  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {"_raw": raw.decode("utf-8", "replace")}
            capture.add(self.path, body, dict(self.headers))
            payload = json.dumps({
                "choices": [{"message": {"content": reply_text}}],
                "content": [{"type": "text", "text": reply_text}],
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):                          # 静音
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ------------------------------------------------------------------- handler 提取

def _extract_handler_source() -> str:
    """把 _ENHANCE_HANDLER 模板按真实注入方式填好标记。

    真实注入见 `_enhance_main_block()`：替换 __H__/__SYS__/__TPL__。
    这里复用同一套替换，保证被测代码与线上一致。
    """
    src = zp._ENHANCE_HANDLER
    src = (src.replace("__H__", "H")
              .replace("__SYS__", json.dumps(zp._ENHANCE_SYSTEM_PROMPT, ensure_ascii=False))
              .replace("__TPL__", json.dumps(zp._ENHANCE_USER_TEMPLATE, ensure_ascii=False)))
    return src


def _build_runner(handler_src: str) -> str:
    """包一层 stub 环境：假 ipcMain.handle + 调用入口 + 结果打印。

    注意 handler 内部用动态 import 取 node:fs 等，所以只能真在 node 里跑。

    只把**开头那一处** `H.handle(` 换掉（注册调用），不能全局替换 ——
    否则会连 handler 结尾的 `});` 一起改坏（那是 `handle(...)` 的收尾括号）。
    开头的 `H.handle(` 用 `__register(` 顶替，再把 `__register` 定义成捕获函数。
    """
    lead = handler_src[:len(handler_src) - len(handler_src.lstrip())]
    stripped = handler_src.strip()
    assert stripped.startswith("H.handle("), \
        f"handler 模板开头变了，夹具需同步: {stripped[:60]!r}"
    body = lead + "__register(" + stripped[len("H.handle("):]
    return body + """
async function main(){
  const arg = JSON.parse(process.argv[2] || "{}");
  const r = await registered(arg.ev, arg.t);
  process.stdout.write("<<RESULT>>" + JSON.stringify(r));
}
main().catch(e => {
  process.stdout.write("<<RESULT>>" + JSON.stringify({success:false,code:"harness",error:String(e)}));
});
"""


class _Harness:
    """一次 node 子进程执行的封装。"""

    def __init__(self, home: Path, handler_src: str, tmp: Path):
        self.home = home
        self.handler_src = handler_src
        self.tmp = tmp

    def preamble(self) -> str:
        return ('let registered=null;\n'
                'const H={handle:(ch,fn)=>{registered=fn;}};\n'
                'const __register=(ch,fn)=>{registered=fn;};\n')

    def call(self, payload, timeout=40):
        """用任意 payload 调 handler（增强通道与 save 通道共用）。"""
        script = self.tmp / "runner.mjs"
        script.write_text(self.preamble() + _build_runner(self.handler_src),
                          encoding="utf-8")
        env = dict(os.environ)
        # handler 用 os.homedir() 定位 ~/.zcode/v2 —— 用 HOME/USERPROFILE 重定向到沙盒
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env["NODE_PATH"] = env.get("NODE_PATH", "")
        proc = subprocess.run(
            [shutil.which("node") or "node", str(script), json.dumps(payload)],
            capture_output=True, timeout=timeout, env=env, cwd=str(self.tmp))
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        m = re.search(r"<<RESULT>>(.*)", out, re.S)
        if not m:
            raise AssertionError(
                f"node 未产出结果。\nstdout={out[:2000]}\nstderr={err[:2000]}")
        return json.loads(m.group(1))

    def run(self, text="hello world", model_value="", model_label="",
            override=None, list_only=False, timeout=40):
        payload = {"t": {"text": text, "modelValue": model_value,
                         "modelLabel": model_label}}
        if override is not None:
            payload["t"]["override"] = override
        if list_only:
            payload["t"]["list"] = True
        return self.call(payload, timeout=timeout)


class EnhanceHandlerTestBase(unittest.TestCase):
    """搭一个假的 ~/.zcode/v2 环境 + 本地假供应商。"""

    PROVIDER_OK = "prov-ok-0001"
    PROVIDER_DEAD = "prov-dead-0002"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zenh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        root = self.home / ".zcode" / "v2"
        root.mkdir(parents=True, exist_ok=True)
        self.root = root

        self.capture = _Capture()
        self.srv = _make_server(self.capture)
        self.port = self.srv.server_address[1]
        self.addCleanup(self.srv.shutdown)

        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._write_provider_config()
        self.handler_src = _extract_handler_source()
        self.h = _Harness(self.home, self.handler_src, self.tmp)

    # ---- 夹具 ----

    def _write_provider_config(self):
        """权威源 provider_config.json：两个 openai 协议供应商。

        prov-ok 有可用 key/baseURL；prov-dead **没有** apiKey →
        `usable()` 必须拒绝它（这正是「配置指向失效供应商」的模拟）。
        """
        cfg = {
            "config": {
                "providerConfigRules": {"providerRules": [
                    {"providerId": self.PROVIDER_OK, "providerName": "OK供应商",
                     "config": {"access": {"apiKey": "sk-test-ok"},
                                "api": {"baseUrl": self.base_url,
                                        "type": "openai-compatible"},
                                "personalModelIds": ["model-a", "model-b"]}},
                    {"providerId": self.PROVIDER_DEAD, "providerName": "死供应商",
                     "config": {"access": {"apiKey": ""},
                                "api": {"baseUrl": self.base_url,
                                        "type": "openai-compatible"},
                                "personalModelIds": ["model-dead"]}},
                ]},
                "modelConfigRules": {"providerModelRules": []},
            }
        }
        (self.root / "provider_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def write_hot(self, obj):
        (self.root / "enhance_config.json").write_text(
            json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def remove_hot(self):
        p = self.root / "enhance_config.json"
        if p.exists():
            p.unlink()

    def last_body(self):
        got = self.capture.last
        self.assertIsNotNone(got, "没有任何请求到达假供应商")
        return got[1]

    def assert_request_count(self, n):
        self.assertEqual(len(self.capture.requests), n,
                         f"期望 {n} 个请求，实际 {len(self.capture.requests)}")


# ------------------------------------------------------- ① 配置生效 / ③ 兼容性

class TestHotConfigResolution(EnhanceHandlerTestBase):

    def test_no_config_file_falls_back_to_ui_selection(self):
        """向后兼容：没有 enhance_config.json 时用界面给的模型。"""
        self.remove_hot()
        r = self.h.run(model_value=f"{self.PROVIDER_OK}/model-b",
                       model_label="model-b")
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(r.get("how"), "ref", "应走 ①ref 档（界面直接给）")
        self.assertEqual(r.get("model"), "model-b")

    def test_hot_config_selects_provider_and_model(self):
        """⓪档：配置指定谁就用谁。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        r = self.h.run(model_value="", model_label="")
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(r.get("model"), "model-a")
        self.assertEqual(r.get("provider"), self.PROVIDER_OK)
        self.assertEqual(self.last_body().get("model"), "model-a")

    def test_hot_config_overrides_ui_selection(self):
        """⓪ 必须压过界面选择 —— 这是「配置了却不生效」的关键回归点。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        # 界面选的是 model-b，但配置说 model-a
        r = self.h.run(model_value=f"{self.PROVIDER_OK}/model-b",
                       model_label="model-b")
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(r.get("model"), "model-a", "配置应覆盖界面选择")
        self.assertEqual(self.last_body().get("model"), "model-a")

    def test_hot_config_allows_model_absent_from_provider_table(self):
        """有意设计：不校验模型表 —— 允许中转站支持但配置里没登记的模型名。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "glm-not-in-table"})
        r = self.h.run(model_value="", model_label="")
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(self.last_body().get("model"), "glm-not-in-table")

    def test_partial_config_falls_back(self):
        """只配一半（缺 modelId / providerId）→ 不能生效，应回退。"""
        for partial in ({"providerId": self.PROVIDER_OK},
                        {"modelId": "model-a"},
                        {"providerId": "  ", "modelId": "model-a"},
                        {"providerId": self.PROVIDER_OK, "modelId": ""}):
            with self.subTest(partial=partial):
                self.capture.requests.clear()
                self.write_hot(partial)
                r = self.h.run(model_value=f"{self.PROVIDER_OK}/model-b",
                               model_label="model-b")
                self.assertTrue(r.get("success"), f"应成功: {r}")
                self.assertNotEqual(r.get("how"), "config",
                                    "配置不完整时不得走 ⓪档")
                self.assertEqual(r.get("model"), "model-b",
                                 "应回退到界面选择")


# --------------------------------------------------------- ④ 供应商可用性守卫

class TestHotConfigUsabilityGuard(EnhanceHandlerTestBase):

    def test_unusable_provider_is_rejected_and_falls_back(self):
        """配置指向没有 apiKey 的供应商 → 不能拿它发请求。"""
        self.write_hot({"providerId": self.PROVIDER_DEAD, "modelId": "model-dead"})
        r = self.h.run(model_value=f"{self.PROVIDER_OK}/model-b",
                       model_label="model-b")
        self.assertTrue(r.get("success"), f"应回退后成功: {r}")
        self.assertNotEqual(r.get("provider"), self.PROVIDER_DEAD,
                            "不可用供应商不得被选中")
        self.assertEqual(self.last_body().get("model"), "model-b")

    def test_unusable_provider_without_ui_fallback_reports_no_model(self):
        """配置不可用 + 界面也没给 + 无其它可用供应商 → 应给可读错误，
        而不是硬发一次注定失败的请求。"""
        # 把唯一可用的供应商也废掉，确保④兜底无候选
        cfg = json.loads((self.root / "provider_config.json").read_text("utf-8"))
        rules = cfg["config"]["providerConfigRules"]["providerRules"]
        for r in rules:
            r["config"]["access"]["apiKey"] = ""
        (self.root / "provider_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        self.write_hot({"providerId": self.PROVIDER_DEAD, "modelId": "model-dead"})
        r = self.h.run(model_value="", model_label="")
        self.assertFalse(r.get("success"), f"无可用供应商时不该成功: {r}")
        self.assertEqual(r.get("code"), "no-model")
        self.assert_request_count(0)          # 关键：一个请求都不该发出去
        self.assertIn("sources", r, "no-model 响应应带各配置源读数，便于定位")
        self.assertTrue(any("provider_config.json" in s for s in r["sources"]),
                        f"sources 应报告权威源读数: {r['sources']}")

    def test_ui_selection_still_used_when_config_provider_unusable(self):
        """配置指向不可用供应商，但界面给了可用选择 → 应回退到界面选择。"""
        self.write_hot({"providerId": self.PROVIDER_DEAD, "modelId": "model-dead"})
        r = self.h.run(model_value=f"{self.PROVIDER_OK}/model-b",
                       model_label="model-b")
        self.assertTrue(r.get("success"), f"应回退后成功: {r}")
        self.assertEqual(r.get("provider"), self.PROVIDER_OK)
        self.assertEqual(r.get("how"), "ref", "应走 ①ref 档")
        self.assertEqual(self.last_body().get("model"), "model-b")


# ------------------------------- ★ 右键菜单：explicit 显式指定档 / list 清单模式

class TestExplicitOverride(EnhanceHandlerTestBase):
    """右键菜单选中的模型（`override`）必须是**最高优先级**。

    用户在菜单里刚点完某个模型，紧接着点润色，这一次点击就必须用刚选的模型 ——
    不能被 enhance_config.json（⓪档）或界面选择（①档）抢走。
    这是「选了模型却不生效」这类静默失效的直接回归点：
    菜单选完、下次点击才发现用的是别的模型，界面上完全看不出原因。
    """

    def test_override_wins_over_hot_config_and_ui(self):
        """override 压过 ⓪ 热配置与 ① 界面选择。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        r = self.h.run(text="hi", model_value=f"{self.PROVIDER_OK}/model-b",
                       model_label="model-b",
                       override={"providerId": self.PROVIDER_OK,
                                 "modelId": "model-b"})
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(r.get("how"), "explicit", "应走 explicit 档")
        self.assertEqual(r.get("model"), "model-b", "override 必须压过 ⓪/① 档")
        self.assertEqual(self.last_body().get("model"), "model-b")

    def test_override_used_when_no_config_and_no_ui(self):
        """没有热配置、界面也取不到模型时，override 仍要生效。"""
        self.remove_hot()
        r = self.h.run(model_value="", model_label="",
                       override={"providerId": self.PROVIDER_OK,
                                 "modelId": "model-a"})
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(r.get("how"), "explicit")
        self.assertEqual(self.last_body().get("model"), "model-a")

    def test_override_allows_model_absent_from_provider_table(self):
        """与 ⓪ 档同口径：不校验模型表（允许中转站的模型变体名）。"""
        self.remove_hot()
        r = self.h.run(model_value="", model_label="",
                       override={"providerId": self.PROVIDER_OK,
                                 "modelId": "glm-variant-x"})
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(self.last_body().get("model"), "glm-variant-x")

    def test_unusable_override_is_rejected_and_falls_back(self):
        """override 指向没有 apiKey 的供应商 → 不得采信，回退到 ⓪ 档。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        r = self.h.run(model_value="", model_label="",
                       override={"providerId": self.PROVIDER_DEAD,
                                 "modelId": "model-dead"})
        self.assertTrue(r.get("success"), f"应回退后成功: {r}")
        self.assertNotEqual(r.get("provider"), self.PROVIDER_DEAD,
                            "不可用供应商不得被选中")
        self.assertNotEqual(r.get("how"), "explicit")
        self.assertEqual(r.get("model"), "model-a", "应回退到 ⓪ 热配置档")

    def test_partial_override_is_ignored(self):
        """override 只给一半 → 忽略，回退到 ⓪ 档。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        for ov in ({"providerId": self.PROVIDER_OK},
                   {"modelId": "model-b"},
                   {"providerId": "  ", "modelId": "model-b"},
                   {"providerId": self.PROVIDER_OK, "modelId": ""}):
            with self.subTest(ov=ov):
                self.capture.requests.clear()
                r = self.h.run(model_value="", model_label="", override=ov)
                self.assertTrue(r.get("success"), f"应成功: {r}")
                self.assertNotEqual(r.get("how"), "explicit",
                                    "override 不完整时不得走 explicit 档")
                self.assertEqual(r.get("model"), "model-a")


class TestListModelsMode(EnhanceHandlerTestBase):
    """`{list:true}` 只回候选表，**绝不发请求** —— 右键菜单的模型清单靠它。

    与 enhance-prompt 共用同一通道/同一份候选表，是为了让「菜单里能选的」
    与「真正发请求时能用的」永远同源，不会出现两套逻辑漂移。
    """

    def test_list_returns_only_usable_groups(self):
        self.remove_hot()
        r = self.h.run(list_only=True)
        self.assertTrue(r.get("success"), f"应成功: {r}")
        ids = [g["providerId"] for g in r.get("groups") or []]
        self.assertIn(self.PROVIDER_OK, ids)
        self.assertNotIn(self.PROVIDER_DEAD, ids,
                         "不可用供应商不该出现在菜单里（点了只会撞 400/401）")
        self.assert_request_count(0)          # 关键：列清单不该发任何补全请求

    def test_list_groups_carry_model_ids(self):
        self.remove_hot()
        r = self.h.run(list_only=True)
        g = [x for x in r["groups"] if x["providerId"] == self.PROVIDER_OK][0]
        self.assertEqual(sorted(m["id"] for m in g["models"]),
                         ["model-a", "model-b"])
        self.assertTrue(g.get("name"), "分组应带可读的供应商名")

    def test_list_reports_current_selection_from_hot_config(self):
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-b"})
        r = self.h.run(list_only=True)
        self.assertEqual(r.get("current"),
                         {"providerId": self.PROVIDER_OK, "modelId": "model-b"})

    def test_list_without_hot_config_has_null_current(self):
        """没配过热配置 → current 为 null，菜单据此显示「跟随界面选择」。"""
        self.remove_hot()
        r = self.h.run(list_only=True)
        self.assertIsNone(r.get("current"))

    def test_list_ignores_empty_text(self):
        """list 模式不该被「输入框是空的」拦掉 —— 菜单随时可以打开。"""
        self.remove_hot()
        r = self.h.run(text="", list_only=True)
        self.assertTrue(r.get("success"), f"空输入框也应能列出模型: {r}")


class TestSaveModelHandler(EnhanceHandlerTestBase):
    """右键菜单的持久化 handler：把选择写进 enhance_config.json（= ⓪ 档来源）。

    写进去才有「零重启生效 + 重启后沿用」；写失败必须**显式报错**，
    否则用户以为选好了，下次点击仍走旧档位。
    """

    def setUp(self):
        super().setUp()
        # 这个类里默认跑 save 通道；enhance 通道的源码留着给端到端用例切回来
        self.enhance_src = self.h.handler_src
        self.save_src = zp._ENHANCE_SAVE_HANDLER.replace("__H__", "H")
        self.h.handler_src = self.save_src

    def save(self, payload):
        return self.h.call({"t": payload})

    def run_enhance(self, **kw):
        """临时切回 enhance 通道跑一次（端到端验收用）。"""
        self.h.handler_src = self.enhance_src
        try:
            return self.h.run(**kw)
        finally:
            self.h.handler_src = self.save_src

    def hot(self):
        p = self.root / "enhance_config.json"
        return json.loads(p.read_text("utf-8")) if p.exists() else None

    def test_saves_provider_and_model(self):
        r = self.save({"providerId": self.PROVIDER_OK, "modelId": "model-b"})
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(self.hot(),
                         {"providerId": self.PROVIDER_OK, "modelId": "model-b"})

    def test_preserves_other_hot_params(self):
        """★ 只动 providerId/modelId，maxTokens/temperature 等原样保留。

        否则「右键选个模型」会把用户精心调好的思考强度/温度一并抹掉。
        """
        self.write_hot({"providerId": "old-p", "modelId": "old-m",
                        "maxTokens": 4096, "temperature": 0.9,
                        "reasoningEffort": "high"})
        r = self.save({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        self.assertTrue(r.get("success"), f"应成功: {r}")
        got = self.hot()
        self.assertEqual(got["providerId"], self.PROVIDER_OK)
        self.assertEqual(got["modelId"], "model-a")
        self.assertEqual(got["maxTokens"], 4096)
        self.assertEqual(got["temperature"], 0.9)
        self.assertEqual(got["reasoningEffort"], "high")

    def test_clear_removes_only_selection_keys(self):
        """clear → 恢复「跟随界面选择」，但保留其它热配置参数。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a",
                        "maxTokens": 4096})
        r = self.save({"clear": True})
        self.assertTrue(r.get("success"), f"应成功: {r}")
        got = self.hot()
        self.assertNotIn("providerId", got)
        self.assertNotIn("modelId", got)
        self.assertEqual(got["maxTokens"], 4096)

    def test_partial_payload_is_rejected(self):
        for bad in ({"providerId": self.PROVIDER_OK}, {"modelId": "model-b"}):
            with self.subTest(bad=bad):
                r = self.save(bad)
                self.assertFalse(r.get("success"), f"半截选择不该写入: {r}")

    def test_malformed_existing_file_is_recovered(self):
        """现有文件是坏 JSON → 不能把整条链路搞崩，应重建为合法配置。"""
        (self.root / "enhance_config.json").write_text("{ not json", encoding="utf-8")
        r = self.save({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(self.hot(),
                         {"providerId": self.PROVIDER_OK, "modelId": "model-a"})

    def test_saved_selection_actually_takes_effect(self):
        """★ 端到端：菜单保存的选择，必须让下一次增强真的用上它。

        这是「确保所选模型正确生效」的最终验收点 —— 不是只写了个文件就算完。
        """
        self.save({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        # 界面选的是 model-b，但菜单已经选了 model-a
        r = self.run_enhance(text="hi", model_value=f"{self.PROVIDER_OK}/model-b",
                             model_label="model-b")
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(r.get("how"), "config", "应走 ⓪config 档")
        self.assertEqual(self.last_body().get("model"), "model-a")


# ------------------------------------------------------- ⑤⑥⑦ 请求体参数与思考强度

class TestHotConfigRequestBody(EnhanceHandlerTestBase):

    def test_max_tokens_and_temperature_reach_body(self):
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a",
                        "maxTokens": 4096, "temperature": 0.9})
        r = self.h.run()
        self.assertTrue(r.get("success"), f"应成功: {r}")
        b = self.last_body()
        self.assertEqual(b.get("max_tokens"), 4096)
        self.assertEqual(b.get("temperature"), 0.9)

    def test_defaults_when_params_absent(self):
        """不配参时保持旧默认（max_tokens 2048 / temperature 0.3）。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a"})
        r = self.h.run()
        self.assertTrue(r.get("success"), f"应成功: {r}")
        b = self.last_body()
        self.assertEqual(b.get("max_tokens"), 2048)
        self.assertEqual(b.get("temperature"), 0.3)

    def test_openai_reasoning_effort_sent_when_configured(self):
        """openai 协议：配了 reasoningEffort 才发，且键名必须是 reasoning_effort。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a",
                        "reasoningEffort": "high"})
        r = self.h.run()
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertEqual(self.last_body().get("reasoning_effort"), "high")

    def test_commented_out_reasoning_effort_is_not_sent(self):
        """★ 防「注释掉的键被当空串发出去」——空值绝不能出现在 body 里。

        ENHANCE_CUSTOM_PLAN.md 的示例里 `reasoningEffort` 被注释掉，
        用户很可能照抄成省略或空串。空 reasoning_effort 会被中转站判 400。
        """
        for val in ("", "   ", None):
            with self.subTest(val=val):
                self.capture.requests.clear()
                self.write_hot({"providerId": self.PROVIDER_OK,
                                "modelId": "model-a",
                                "reasoningEffort": val})
                r = self.h.run()
                self.assertTrue(r.get("success"), f"应成功: {r}")
                self.assertNotIn("reasoning_effort", self.last_body(),
                                 f"reasoningEffort={val!r} 时不该发该字段")

    def test_thinking_budget_zero_is_not_sent(self):
        """thinkingBudget=0（文档默认值）→ 不发 thinking，保持普通请求。"""
        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a",
                        "thinkingBudget": 0})
        r = self.h.run()
        self.assertTrue(r.get("success"), f"应成功: {r}")
        self.assertNotIn("thinking", self.last_body())

    def test_anthropic_thinking_raised_max_tokens(self):
        """anthropic 协议：开思考时必须 thinking.budget_tokens，
        且 max_tokens 要抬到 budget+1024（否则上游会因 budget>max_tokens 报错）。"""
        cfg = json.loads((self.root / "provider_config.json").read_text("utf-8"))
        rules = cfg["config"]["providerConfigRules"]["providerRules"]
        rules[0]["config"]["api"]["type"] = "anthropic"
        (self.root / "provider_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        self.write_hot({"providerId": self.PROVIDER_OK, "modelId": "model-a",
                        "thinkingBudget": 8192})
        r = self.h.run()
        self.assertTrue(r.get("success"), f"应成功: {r}")
        b = self.last_body()
        self.assertEqual(b.get("thinking"), {"type": "enabled",
                                             "budget_tokens": 8192})
        self.assertGreaterEqual(b.get("max_tokens"), 8192 + 1024,
                                "max_tokens 必须被抬到 budget+1024 以上")
        # anthropic 协议不该带 temperature / reasoning_effort
        self.assertNotIn("temperature", b)
        self.assertNotIn("reasoning_effort", b)

    def test_anthropic_thinking_budget_is_clamped(self):
        """budget 超出上游允许区间时必须 clamp 到 1024–32768。"""
        cfg = json.loads((self.root / "provider_config.json").read_text("utf-8"))
        rules = cfg["config"]["providerConfigRules"]["providerRules"]
        rules[0]["config"]["api"]["type"] = "anthropic"
        (self.root / "provider_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        for given, floor, ceil in ((100, 1024, 1024), (999999, 32768, 32768)):
            with self.subTest(given=given):
                self.capture.requests.clear()
                self.write_hot({"providerId": self.PROVIDER_OK,
                                "modelId": "model-a",
                                "thinkingBudget": given})
                r = self.h.run()
                self.assertTrue(r.get("success"), f"应成功: {r}")
                got = self.last_body()["thinking"]["budget_tokens"]
                self.assertTrue(floor <= got <= ceil,
                                f"budget {given} 应 clamp 到 [{floor},{ceil}]，实际 {got}")

    def test_malformed_config_json_does_not_crash(self):
        """配置写坏（非法 JSON）→ 必须退化为旧行为，而不是整条链路抛异常。"""
        (self.root / "enhance_config.json").write_text("{ this is not json",
                                                       encoding="utf-8")
        r = self.h.run(model_value=f"{self.PROVIDER_OK}/model-b",
                       model_label="model-b")
        self.assertTrue(r.get("success"), f"坏配置不该让增强不可用: {r}")
        self.assertEqual(r.get("model"), "model-b")


# ------------------------------------------------------------ 源码级顺序/锚点约束

class TestHotConfigSourceInvariants(unittest.TestCase):
    """顺序与锚点约束 —— 这类「语法对、位置错」的 bug 行为测试抓不到。"""

    def setUp(self):
        self.src = zp._ENHANCE_HANDLER

    def test_hot_config_is_authored_before_ref_tier(self):
        """⓪档（cfg）在源码里必须排在 ①ref 档之前。

        两处都是 `pick=` 赋值，靠 if 链顺序决定优先级；谁在前面谁赢。
        一旦有人调换，行为测试仍可能通过（若两者指向同一模型），
        所以这里额外钉死源码顺序。
        """
        i_cfg = self.src.index('if(!pick&&cfg&&String(cfg.providerId')
        i_ref = self.src.index("// ① 界面直接给的 ref")
        self.assertLess(i_cfg, i_ref,
                        "⓪热配置档必须排在 ①ref 档之前（否则配置永远不生效）")

    def test_explicit_tier_is_authored_before_config_tier(self):
        """★ 右键菜单的 explicit 档必须排在 ⓪config 档之前。

        两者都是「用户显式指定」，但 explicit 是**本次刚点的**，语义上更近；
        排错顺序会让「菜单选了模型、点润色却用了旧配置」——静默失效。
        """
        i_exp = self.src.index("if(!pick&&t&&t.override")
        i_cfg = self.src.index('if(!pick&&cfg&&String(cfg.providerId')
        self.assertLess(i_exp, i_cfg,
                        "explicit 档必须排在 ⓪config 档之前（否则菜单选择被配置覆盖）")

    def test_config_tier_is_guarded_by_not_pick(self):
        """★ ⓪档必须带 `!pick` 守卫，否则它会覆盖排它前面的 explicit 档。

        与 ①档当年踩的是同一个坑：新插入的最高优先级分支，必须给**后面每一个**
        `pick=` 赋值补上 `!pick`，否则语法对、位置对、行为全静默。
        """
        seg = self.src[self.src.index('if(!pick&&cfg&&String(cfg.providerId'):
                      self.src.index("// ① 界面直接给的 ref")]
        self.assertRegex(seg, r"if\(!pick\s*&&\s*cfg\s*&&",
                         "⓪档必须写成 if(!pick&&cfg&&…) —— 缺 !pick 会覆盖 explicit 档")

    def test_ref_tier_is_guarded_by_not_pick(self):
        """★ ①档必须带 `!pick` 守卫，否则它会覆盖 ⓪档。

        这是 PR#1 引入的真 bug：⓪ 设好 pick 后，① 无条件重跑并覆盖，
        导致「enhance_config.json 指哪打哪」只在界面取值失灵时才生效。
        """
        seg = self.src[self.src.index("// ① 界面直接给的 ref"):
                       self.src.index("// ② 按 ref")]
        self.assertRegex(seg, r"if\(!pick\s*&&\s*mv\)",
                         "①档必须写成 if(!pick&&mv) —— 缺 !pick 会覆盖热配置档")

    def test_hot_config_reads_at_handler_top_level(self):
        """读配置文件必须在每次调用时执行 —— 不能挪进任何条件分支/函数外。"""
        i_read = self.src.index('r.join(root,"enhance_config.json")')
        i_first_tier = self.src.index("let pcCands=[]")
        self.assertLess(i_read, i_first_tier,
                        "热配置的读取应发生在候选表构建之前（保证「改文件即生效」）")

    def test_hot_config_guards_provider_usability(self):
        """⓪档必须经 usable() 校验，不能无条件 cand()。"""
        seg = self.src[self.src.index('if(!pick&&cfg&&String(cfg.providerId'):
                      self.src.index("// ① 界面直接给的 ref")]
        self.assertIn("usable(", seg,
                      "⓪档必须校验供应商可用性（否则配置指向失效供应商会直撞报错）")

    def test_explicit_tier_guards_provider_usability(self):
        """★ explicit 档同样必须经 usable() 校验。

        菜单只列可用供应商，但 override 参数是渲染进程传来的 ——
        供应商可能在菜单打开后被停用/删 key，不能无条件采信。
        """
        seg = self.src[self.src.index("if(!pick&&t&&t.override"):
                       self.src.index('if(!pick&&cfg&&String(cfg.providerId')]
        self.assertIn("usable(", seg,
                      "explicit 档必须校验供应商可用性，不能无条件 cand()")

    def test_how_is_reported_for_diagnostics(self):
        """`how` 必须被赋值 —— 否则 lastResult.how 恒为空串，文档教的排障法失效。"""
        self.assertNotIn('how=""', self.src.replace("let pick=null,how=\"\",tried=[];", ""),
                         "how 不应再是恒空串")
        self.assertRegex(self.src, r"how=String\(pick\.how",
                         "应把 pick.how 回填到返回值")

    def test_classify_covers_not_supported(self):
        """`is not supported` 必须归入 model 档，不能误报成「接口路径不对」。"""
        seg = self.src[self.src.index("function classify("):]
        seg = seg[:seg.index('if(/insufficient')]
        self.assertIn("not supported", seg,
                      "404『Model ... is not supported』应判为 model 档")

    def test_handler_source_is_valid_js(self):
        """模板替换后必须是合法 JS（防占位符/引号被改坏）。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("未找到 node")
        src = _extract_handler_source()
        runner = ('let registered=null;\n'
                  'const H={handle:(ch,fn)=>{registered=fn;}};\n'
                  'const __register=(ch,fn)=>{registered=fn;};\n'
                  + _build_runner(src))
        import tempfile as _tf
        with _tf.TemporaryDirectory() as d:
            p = Path(d) / "check.mjs"
            p.write_text(runner, encoding="utf-8")
            proc = subprocess.run([node, "--check", str(p)],
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0,
                             f"handler 不是合法 JS:\n"
                             f"{(proc.stderr or b'').decode('utf-8', 'replace')[:2000]}")


class TestEnhanceScriptDockAnchoring(unittest.TestCase):
    """PR#1 的另一半：`currentModel()` 必须锚定 dock。

    旧实现全局搜 `[data-model-current-value]` 后按「offsetParent 可见 + 最靠下」
    挑节点，但输入框工具栏在 **fixed 容器**里（offsetParent 恒为 null），
    被可见过滤整批误杀 → 取值恒空 → 主进程各档全空 → 恒走兜底。
    """

    @classmethod
    def setUpClass(cls):
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
        body = self.src[self.src.index("function currentModel("):]
        self.body = body[:body.index("\n  function ", 10)]

    def test_anchors_dock_before_looking_for_model_button(self):
        self.assertIn("findDock()", self.body, "currentModel 必须先锚定 dock")
        self.assertIn("dock.querySelectorAll", self.body,
                      "应优先在 dock 内找模型按钮")

    def test_dock_hit_does_not_drop_by_offsetparent(self):
        """dock 命中时不得因 offsetParent=null 丢弃节点 —— 这正是原 bug。"""
        m = re.search(r"const usablePool\s*=\s*([^;]+);", self.body)
        self.assertIsNotNone(m, "找不到 usablePool 判定")
        expr = m.group(1)
        self.assertIn("dock", expr,
                      "usablePool 必须在 dock 命中时保留全部候选（绕过 offsetParent 过滤）")
        # 形态： (dock && pool.length) ? pool : (vis.length ? vis : pool)
        self.assertIn("pool", expr)

    def test_global_fallback_excludes_workflow_panel(self):
        """全局兜底必须排除工作流运行设置面板的残留节点。"""
        self.assertIn("workflow-run-settings-model", self.body,
                      "全局兜底应排除工作流面板节点（否则会取到后台面板的模型）")

    def test_diag_counts_pool_not_whole_document(self):
        self.assertIn("diag.modelCandidates = pool.length", self.body,
                      "诊断计数应报告实际参与挑选的候选数")


class TestDoctorMirrorsHandler(unittest.TestCase):
    """`enhance_doctor.py` 必须与 handler 的档位顺序保持一致。

    它是用户排障时**唯一能看到的真相**（GUI 里没有任何档位信息）。一旦两者漂移，
    doctor 说「会用 A」而按钮实际用 B —— 排障过程本身变成误导，比没有工具更糟。
    这也是这个仓库反复踩过的坑（见 MEMORY：别把输出字符串/复刻逻辑当接口）。
    """

    @classmethod
    def setUpClass(cls):
        path = None
        for cand in (_HERE.parent / "scripts",
                     _HERE.parent / "skills" / "zcode-tokenspeed" / "scripts"):
            p = cand / "enhance_doctor.py"
            if p.is_file():
                path = p
                break
        if path is None:
            raise unittest.SkipTest("未找到 enhance_doctor.py")
        spec = importlib.util.spec_from_file_location("zenh_doctor", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.doc = mod

    @staticmethod
    def _cands():
        return [
            {"pid": "p1", "name": "P1", "kind": "openai-compatible",
             "baseURL": "http://x/v1", "apiKey": "k", "models": ["m1", "m2"],
             "systemDisabledReason": None, "source": "provider_config.json"},
            {"pid": "p2", "name": "P2", "kind": "openai-compatible",
             "baseURL": "http://y/v1", "apiKey": "k", "models": ["m3"],
             "systemDisabledReason": None, "source": "provider_config.json"},
        ]

    def test_explicit_wins_over_config_and_ref(self):
        cands = self._cands()
        by_id = {c["pid"]: c for c in cands}
        pick, how, _ = self.doc.resolve(
            cands, by_id, "p1/m1", "m1",
            override={"providerId": "p2", "modelId": "m3"},
            cfg={"providerId": "p1", "modelId": "m2"})
        self.assertEqual(how, "explicit")
        self.assertEqual((pick["pid"], pick["mid"]), ("p2", "m3"))

    def test_config_wins_over_ref(self):
        cands = self._cands()
        by_id = {c["pid"]: c for c in cands}
        _, how, _ = self.doc.resolve(cands, by_id, "p1/m1", "m1",
                                     cfg={"providerId": "p2", "modelId": "m3"})
        self.assertEqual(how, "config")

    def test_ref_used_when_no_override_no_config(self):
        cands = self._cands()
        by_id = {c["pid"]: c for c in cands}
        pick, how, _ = self.doc.resolve(cands, by_id, "p1/m1", "m1")
        self.assertEqual(how, "ref")
        self.assertEqual(pick["mid"], "m1")

    def test_unusable_explicit_is_skipped(self):
        """override 指向无 key 的供应商 → doctor 也必须跳过（与 handler 同口径）。"""
        cands = self._cands()
        cands.append({"pid": "dead", "name": "D", "kind": "openai-compatible",
                      "baseURL": "http://z/v1", "apiKey": "", "models": ["m9"],
                      "systemDisabledReason": None, "source": "provider_config.json"})
        by_id = {c["pid"]: c for c in cands}
        _, how, _ = self.doc.resolve(cands, by_id, "p1/m1", "m1",
                                     override={"providerId": "dead", "modelId": "m9"})
        self.assertEqual(how, "ref", "不可用的 override 应被跳过，落到 ref 档")

    def test_tier_order_matches_handler_source(self):
        """doctor 的行为顺序必须与 handler 源码顺序同构。"""
        src = zp._ENHANCE_HANDLER
        i_exp = src.index("if(!pick&&t&&t.override")
        i_cfg = src.index('if(!pick&&cfg&&String(cfg.providerId')
        i_ref = src.index("// ① 界面直接给的 ref")
        self.assertLess(i_exp, i_cfg, "handler：explicit 必须排在 config 之前")
        self.assertLess(i_cfg, i_ref, "handler：config 必须排在 ref 之前")

        cands = self._cands()
        by_id = {c["pid"]: c for c in cands}
        # doctor 在「三档都有候选」时必须选 explicit —— 与源码顺序同构
        _, how, _ = self.doc.resolve(
            cands, by_id, "p1/m1", "m1",
            override={"providerId": "p2", "modelId": "m3"},
            cfg={"providerId": "p1", "modelId": "m2"})
        self.assertEqual(how, "explicit",
                         "doctor 的档位优先级与 handler 源码顺序不一致（复刻已漂移）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
