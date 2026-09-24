#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""增强提示词（润色按钮）链路诊断 —— 只读，默认不发任何真实补全请求。

定位「润色没用界面当前选中的模型 / 报 HTTP 400 Model is unavailable」这类跨机差异。

    python enhance_doctor.py                 # 完整体检
    python enhance_doctor.py --json          # 机器可读
    python enhance_doctor.py --probe         # 额外做真实连通性探测（会消耗极少量额度）
    python enhance_doctor.py --probe --only <providerId>
    python enhance_doctor.py --model-value "bc73e3ab-.../glm-5.3" --model-label "glm-5.3"
    python enhance_doctor.py --override-provider <id> --override-model <model>
                                             # 模拟右键菜单选中的模型（explicit 档）

它逐段复刻 app.asar 里 `zcode:enhance-prompt` handler 的解析逻辑，因此
**本脚本判定用哪个供应商/模型，就等于润色按钮实际会用哪个**。

检查顺序（与 handler 一一对应）：
  1. 数据根定位（setting.json 的 dataBaseDir 优先，退回 ~/.zcode/v2）
  2. 两份配置：provider_config.json（**权威**，客户端就是从这里发请求）与
     config.json（旧格式，可能长期不更新）
  2b. 两份配置的差异审计
  2c. enhance_config.json（右键菜单 / 手改的热配置，**零重启生效**）
  3. 归一化后的候选表 + 模型解析**六档**：
     explicit（右键菜单本次指定）→ config（enhance_config.json）→ ref → ref-label
     → label → fallback。后两档是「模型不可用」的高发区 —— 它会把请求打到不相干的供应商上
  4. 命中供应商的关键字段：baseURL / apiKey / kind
  5. 请求构造复核：URL 拼接、鉴权头、max_tokens（会被供应商上限卡 400）
  6. --probe 时的真实 HTTP 探测 + 错误码归因

退出码：0 = 没发现阻断项；1 = 发现会导致润色失败的问题。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
try:
    from _console import safe_stdio
except ImportError:
    sys.path.insert(0, str(HERE))
    from _console import safe_stdio

# —— 只能用在 cp936 下编得出来的符号（见 _console.py 说明）——
OK = "[OK]"
BAD = "[!!]"
WARN = "[!]"

MAX_TOKENS = 2048          # 与 handler 一致
TIMEOUT = 60000


def resolve_root() -> tuple[Path, str]:
    """复刻 handler：先读 <home>/.zcode/v2/setting.json 的 dataBaseDir，否则用默认。"""
    home = Path(os.path.expanduser("~"))
    base = home
    how = "默认 (~/.zcode/v2)"
    sfile = home / ".zcode" / "v2" / "setting.json"
    try:
        s = json.loads(sfile.read_text(encoding="utf-8"))
        if isinstance(s, dict):
            dbd = s.get("dataBaseDir")
            if isinstance(dbd, str) and dbd.strip():
                base = Path(dbd.strip())
                how = f"setting.json dataBaseDir = {base}"
    except FileNotFoundError:
        how += "（没有 setting.json，正常）"
    except Exception as e:
        how += f"（setting.json 读取失败 {e!r}，已退回默认）"
    return base / ".zcode" / "v2", how


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def zkind(t: str) -> str:
    t = str(t or "").lower()
    if "anthropic" in t:
        return "anthropic"
    return "openai-compatible"


def build_candidates(root: Path) -> tuple[list[dict], dict, list[str], dict]:
    """复刻 handler 的候选表构建：provider_config.json 优先，config.json 补缺。"""
    by_id: dict[str, dict] = {}
    notes: list[str] = []
    stats: dict = {}

    pcp = root / "provider_config.json"
    pccands: list[dict] = []
    raw = _read_json(pcp)
    if raw is not None:
        pc = raw.get("config") if isinstance(raw, dict) and "config" in raw else raw
        if isinstance(pc, dict):
            rules = ((pc.get("providerConfigRules") or {}).get("providerRules")) or []
            pm: dict[str, list[str]] = {}
            for r in ((pc.get("modelConfigRules") or {}).get("providerModelRules")) or []:
                pid = str(r.get("providerId") or "")
                if pid:
                    pm.setdefault(pid, []).append(str(r.get("modelId") or ""))
            for r in rules:
                pid = str(r.get("providerId") or "")
                if not pid:
                    continue
                c = r.get("config") or {}
                acc = c.get("access") or {}
                api = c.get("api") or {}
                models = {}
                for i in list(c.get("personalModelIds") or []) + pm.get(pid, []):
                    s = str(i or "").strip()
                    if s and s not in models:
                        models[s] = {"name": s}
                pccands.append({
                    "pid": pid,
                    "name": str(r.get("providerName") or ""),
                    "kind": zkind(api.get("type")),
                    "baseURL": str(api.get("baseUrl") or ""),
                    "apiKey": str(acc.get("apiKey") or ""),
                    "models": list(models),
                    "systemDisabledReason": (c.get("systemDisabledReason")
                                             or r.get("systemDisabledReason")),
                    "source": "provider_config.json",
                })
        stats["provider_config.json"] = len(pccands)
    else:
        stats["provider_config.json"] = None
        notes.append(f"{WARN} 读不到 {pcp}（客户端可能用其它数据根，或从未配置过）")

    cfgcands: list[dict] = []
    cf = root / "config.json"
    raw2 = _read_json(cf)
    if isinstance(raw2, dict):
        for pid, pp in (raw2.get("provider") or {}).items():
            if not isinstance(pp, dict):
                continue
            o = pp.get("options") or {}
            cfgcands.append({
                "pid": pid,
                "name": str(pp.get("name") or ""),
                "kind": str(pp.get("kind") or "openai-compatible"),
                "baseURL": str(o.get("baseURL") or ""),
                "apiKey": str(o.get("apiKey") or ""),
                "models": list((pp.get("models") or {}).keys()),
                "systemDisabledReason": pp.get("systemDisabledReason"),
                "source": "config.json",
            })
        stats["config.json"] = len(cfgcands)
    else:
        stats["config.json"] = None

    for c in pccands:
        by_id[c["pid"]] = c
    merged = list(pccands)
    for c in cfgcands:
        if c["pid"] in by_id:
            continue
        merged.append(c)
        by_id[c["pid"]] = c

    # 差异审计：同一 providerId 在两份配置里是否一致
    audit = {}
    pc_map = {c["pid"]: c for c in pccands}
    for c in cfgcands:
        p = pc_map.get(c["pid"])
        if not p:
            continue
        diffs = []
        if p["baseURL"] and c["baseURL"] and p["baseURL"] != c["baseURL"]:
            diffs.append(f"baseURL: {c['baseURL']} (config) vs {p['baseURL']} (provider_config)")
        if p["apiKey"] and c["apiKey"] and p["apiKey"] != c["apiKey"]:
            diffs.append("apiKey 两处不一致（provider_config.json 才是生效的）")
        if set(p["models"]) != set(c["models"]):
            only_pc = sorted(set(p["models"]) - set(c["models"]))
            only_cfg = sorted(set(c["models"]) - set(p["models"]))
            diffs.append(f"模型列表不同: 仅 provider_config={only_pc} 仅 config={only_cfg}")
        if diffs:
            audit[c["pid"]] = diffs
    return merged, by_id, notes, {"stats": stats, "audit": audit}


def read_hot_config(root: Path) -> dict:
    """读 enhance_config.json（右键菜单 / 手改的热配置）—— 不存在或写坏都当空。"""
    d = _read_json(root / "enhance_config.json")
    return d if isinstance(d, dict) else {}


def resolve(cands: list[dict], by_id: dict, mv: str, ml_raw: str,
            override: dict | None = None, cfg: dict | None = None):
    """逐字复刻 handler 的**六档**解析。

    顺序与 handler 源码必须一致（谁在前谁赢）：
      explicit（右键菜单本次指定）→ config（enhance_config.json）→ ref
      → ref-label → label → fallback
    """
    ml = str(ml_raw or "").strip().lower()
    tried: list[str] = []

    def usable(c):
        if not c or c.get("systemDisabledReason"):
            return False
        return bool(str(c.get("baseURL") or "").strip() and str(c.get("apiKey") or "").strip())

    def cand(pid, mid, n):
        c = by_id.get(pid)
        tried.append(f"{n}:{pid}/{mid}" + ("" if usable(c) else "(跳过:不可用)"))
        if not usable(c):
            return None
        return {"pid": pid, "mid": mid, "how": n, "c": c}

    def models_of(pid):
        c = by_id.get(pid)
        return c["models"] if c else []

    # ★ explicit：右键菜单本次选中的模型（随请求一起传），最高优先级。
    #   只校验供应商可用性，不校验模型表 —— 与 handler 同口径。
    if override:
        opid = str(override.get("providerId") or "").strip()
        omid = str(override.get("modelId") or "").strip()
        if opid and omid:
            c = cand(opid, omid, "explicit")
            if c:
                return c, "explicit", tried

    # ⓪ config：enhance_config.json 里的持久化选择（右键菜单写入的也是它）。
    if cfg:
        cpid = str(cfg.get("providerId") or "").strip()
        cmid = str(cfg.get("modelId") or "").strip()
        if cpid and cmid:
            c = cand(cpid, cmid, "config")
            if c:
                return c, "config", tried

    if mv and "/" in mv:
        k = mv.index("/")
        pid, mid = mv[:k], mv[k + 1:]
        if mid in models_of(pid):
            c = cand(pid, mid, "ref")
            if c:
                return c, "ref", tried

    if mv and "/" in mv and ml:
        k = mv.index("/")
        pid = mv[:k]
        for mid in models_of(pid):
            if any(str(x).strip().lower() == ml for x in (mid, pid + "/" + mid)):
                cc = cand(pid, mid, "ref-label")
                if cc:
                    return cc, "ref-label", tried

    if ml:
        for c in cands:
            for mid in c["models"]:
                if any(str(x).strip().lower() == ml for x in (mid, c["pid"] + "/" + mid)):
                    cc = cand(c["pid"], mid, "label")
                    if cc:
                        return cc, "label", tried

    ordered = sorted(cands, key=lambda c: 1 if c["pid"].startswith(("builtin:", "account:")) else 0)
    for c in ordered:
        if not c["models"]:
            continue
        cc = cand(c["pid"], c["models"][0], "fallback")
        if cc:
            return cc, "fallback", tried
    return None, "none", tried


def probe(c: dict, mid: str) -> str:
    u = c["baseURL"].rstrip("/")
    k = c["apiKey"]
    if c["kind"] == "anthropic":
        urls = [(u if u.endswith("/v1") else u + "/v1") + "/messages"]
        body = {"model": mid, "max_tokens": 32, "system": "ping",
                "messages": [{"role": "user", "content": "ping"}]}
    else:
        urls = [(u if u.endswith("/v1") else u + "/v1") + "/chat/completions",
                u + "/chat/completions"]
        body = {"model": mid, "stream": False, "max_tokens": 32,
                "messages": [{"role": "user", "content": "ping"}]}
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Authorization": "Bearer " + k, "x-api-key": k}
    payload = json.dumps(body).encode()
    last = "未能连通"
    for url in urls:
        h = dict(headers)
        h["Content-Length"] = str(len(payload))
        req = urllib.request.Request(url, data=payload, method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT // 1000) as r:
                return f"{OK} {url} -> HTTP {r.status}，端到端可用"
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:300]
            msg = raw
            try:
                j = json.loads(raw)
                msg = (j.get("error") or {}).get("message") or j.get("message") or raw
            except Exception:
                pass
            line = f"{BAD} {url} -> HTTP {e.code}：{msg}\n       归因：{diagnose_http(e.code, str(msg))}"
            if e.code == 400:
                return line
            last = line
            continue
        except Exception as e:
            last = f"{BAD} {url} -> {type(e).__name__}: {e}"
            continue
    return last


def diagnose_http(code: int, msg: str) -> str:
    m = msg.lower()
    if code == 400:
        if any(s in m for s in ("model is unavailable", "model_not_found", "not available")):
            return ("★ 模型不被该供应商接受：模型名写了，但这个 key/套餐里没有它。"
                    "检查模型 id 的拼写与大小写（glm-5.2 != GLM-5.2）")
        if "invalid" in m:
            return "请求体字段不被接受（max_tokens 超上限 / 参数名不符）"
        return "上游判定请求非法：核对 model id、max_tokens、endpoint 路径前缀"
    if code == 401:
        return "★ 鉴权失败：API Key 过期/错填/不属于该 endpoint"
    if code == 402:
        return "★ 余额或套餐不足：需要充值或换供应商"
    if code == 403:
        return "★ 权限/地区被拒：key 无权访问该模型，或出口 IP 被风控"
    if code == 404:
        return "★ 路径不存在：baseURL 少了或多写了 /v1"
    if code == 429:
        return "限流或额度耗尽（可重试；若持续则升级套餐）"
    if code >= 500:
        return "供应商侧故障（可重试）"
    return "未知错误码"


def main() -> int:
    safe_stdio()
    ap = argparse.ArgumentParser(description="增强提示词（润色）链路诊断（只读）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--probe", action="store_true",
                    help="额外做真实连通性探测（会消耗极少量额度）")
    ap.add_argument("--only", default="", help="配合 --probe：只探测指定 providerId")
    ap.add_argument("--model-value", default="",
                    help="模拟界面给的 data-model-current-value（providerId/modelId）")
    ap.add_argument("--model-label", default="",
                    help="模拟界面显示的模型名（ref 解析失败时的后备）")
    ap.add_argument("--override-provider", default="",
                    help="模拟右键菜单选中的供应商 id（explicit 档，最高优先级）")
    ap.add_argument("--override-model", default="",
                    help="模拟右键菜单选中的模型 id（需与 --override-provider 同时给出）")
    args = ap.parse_args()

    root, how = resolve_root()
    cands, by_id, notes, extra = build_candidates(root)
    hot = read_hot_config(root)
    problems: list[str] = []
    report: dict = {"config_root": str(root), "root_how": how,
                    "candidate_count": len(cands),
                    "sources": extra["stats"],
                    "cross_config_diffs": extra["audit"],
                    "hot_config": {"path": str(root / "enhance_config.json"),
                                   "present": bool(hot),
                                   "providerId": str(hot.get("providerId") or ""),
                                   "modelId": str(hot.get("modelId") or ""),
                                   "maxTokens": hot.get("maxTokens"),
                                   "temperature": hot.get("temperature"),
                                   "reasoningEffort": hot.get("reasoningEffort"),
                                   "thinkingBudget": hot.get("thinkingBudget")}}

    def say(*a):
        """人读模式下才打印——--json 时保持 stdout 只剩 JSON。"""
        if not args.json:
            print(*a)

    say("增强提示词（润色按钮）链路诊断 —— 只读")
    say("=" * 68)
    say(f"1. 数据根  {root}")
    say(f"   {how}")
    st = extra["stats"]
    pc_n = st.get("provider_config.json")
    cj_n = st.get("config.json")
    say(f"{OK if pc_n else BAD} 2. provider_config.json（权威）: "
        f"{pc_n if pc_n is not None else '读不到'} 个供应商")
    say(f"   config.json（旧格式，仅补缺）: {cj_n if cj_n is not None else '读不到'} 个")
    for n in notes:
        say("   " + n)
    if pc_n is None:
        problems.append("读不到 provider_config.json —— 客户端发请求用的就是它，"
                        "拿不到就无法保证用对了供应商")

    if extra["audit"]:
        say(f"{WARN} 2b. 两份配置不一致的供应商（provider_config.json 才是生效的）：")
        for pid, diffs in extra["audit"].items():
            say(f"   - {pid}")
            for d in diffs:
                say(f"       {d}")

    # 2c. 热配置（右键菜单写入的就是它）—— 它压过界面选择，最容易造成「我明明选了 A，却用了 B」
    hc = report["hot_config"]
    if hc["providerId"] and hc["modelId"]:
        say(f"{WARN} 2c. enhance_config.json 指定了模型 → **压过界面选择**")
        say(f"   {hc['providerId']} / {hc['modelId']}")
        say("   （右键菜单选模型写的就是这里；要恢复「跟随界面选择」，"
            "在菜单里点「跟随界面选择」或删掉这两个键）")
    elif hc["present"]:
        say(f"{OK} 2c. enhance_config.json 存在，但未指定供应商/模型 → 跟随界面选择")
    else:
        say(f"{OK} 2c. 没有 enhance_config.json → 完全跟随界面选择")

    say("3. 模型解析（复刻 handler 六档：explicit → config → ref → ref-label → label → fallback）")
    mv, ml = args.model_value.strip(), args.model_label.strip()
    ov = None
    if args.override_provider.strip() and args.override_model.strip():
        ov = {"providerId": args.override_provider.strip(),
              "modelId": args.override_model.strip()}
        say(f"   右键菜单 explicit = {ov['providerId']} / {ov['modelId']}")
    elif args.override_provider.strip() or args.override_model.strip():
        say(f"{WARN}   --override-provider/--override-model 必须成对给出，本次忽略")
    if mv:
        say(f"   界面 ref = {mv!r} / 显示名 = {ml!r}")
    else:
        say("   （未提供 --model-value，演示最坏情况：界面 ref 读不到）"
            f" 显示名 = {ml or '(空)'!r}")
    pick, how_used, tried = resolve(cands, by_id, mv, ml, override=ov, cfg=hot)
    report["resolve"] = {"modelValue": mv, "modelLabel": ml, "override": ov,
                         "how": how_used,
                         "providerId": pick and pick["pid"],
                         "modelId": pick and pick["mid"],
                         "tried": tried[:6]}
    if not pick:
        problems.append("没有解析出任何模型 -> 润色直接报「没有可用的模型」")
        say(f"{BAD}   解析结果：无")
    else:
        tag = OK if how_used in ("explicit", "config", "ref") else WARN
        if how_used == "fallback":
            tag = BAD
        say(f"{tag}   命中 {pick['pid']} / {pick['mid']}   (how={how_used})")
        say(f"         来源: {pick['c']['source']}  名称: {pick['c']['name'] or '-'}")
        if how_used == "fallback":
            say("         兜底档：把请求打给了「第一个可用的自定义供应商」")
            say("         ★ 跨机差异的主因 —— 与你界面上选的模型无关")
        if how_used == "explicit":
            say("         右键菜单本次指定：压过热配置与界面选择")
        if how_used == "config":
            say("         热配置档：由 enhance_config.json 指定（右键菜单写入）")
            say("         ★ 若这不是你想用的模型：在菜单里重选，或删掉该文件的 providerId/modelId")
        if how_used == "label":
            say("         按显示名反查：ref 通道没取到值"
                "（界面 DOM 里没有 data-model-current-value 或取值失败）")

    urls = []
    if pick:
        c = pick["c"]
        info = {"source": c["source"], "name": c["name"], "kind": c["kind"],
                "baseURL": c["baseURL"], "apiKey_len": len(c["apiKey"]),
                "systemDisabledReason": c["systemDisabledReason"],
                "model_in_config": pick["mid"] in c["models"],
                "models": c["models"][:8]}
        report["provider"] = info
        say("4. 命中供应商的关键字段")
        for k, v in info.items():
            say(f"   {k:24} = {v}")
        if info["systemDisabledReason"]:
            problems.append(f"命中供应商被系统禁用：{info['systemDisabledReason']}")
            say(f"{BAD}   ★ 套餐未生效/未授权，服务端会直接判不可用")

        u = c["baseURL"].rstrip("/")
        k = c["apiKey"]
        say("5. 请求构造复核")
        if not u:
            problems.append(f"供应商「{pick['pid']}」没有 baseURL")
            say(f"{BAD}   没有 baseURL")
        elif not k:
            problems.append(f"供应商「{pick['pid']}」没有 apiKey")
            say(f"{BAD}   没有 apiKey")
        else:
            if c["kind"] == "anthropic":
                urls = [(u if u.endswith("/v1") else u + "/v1") + "/messages"]
            else:
                urls = [(u if u.endswith("/v1") else u + "/v1") + "/chat/completions",
                        u + "/chat/completions"]
            for i, x in enumerate(urls):
                say(f"   {'  首次 ' if i == 0 else '  备选 '}POST {x}")
            say(f"   kind={c['kind']}  max_tokens={MAX_TOKENS}"
                "（部分供应商 output 上限 <2048 会返回 400）")
            report["request"] = {"urls": urls, "max_tokens": MAX_TOKENS}

    if args.probe and pick and urls:
        if args.only and args.only != pick["pid"]:
            say(f"   （--only {args.only} 与命中供应商不符，跳过探测）")
        else:
            say("6. 真实连通性探测")
            res = probe(pick["c"], pick["mid"])
            report["probe"] = res
            for ln in res.splitlines():
                say("   " + ln)
            if BAD in res:
                problems.append("探测失败：" + res.splitlines()[0])

    report["problems"] = problems
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if problems else 0

    print("=" * 68)
    if not problems:
        print(f"{OK} 结论：配置侧没发现阻断项。")
        print("   若仍不正常，请确认界面选中的模型与解析结果一致"
              "（看第 3 节 how= 是否为 explicit / config / ref）")
        return 0
    print(f"{BAD} 结论：发现 {len(problems)} 个会导致润色失败的问题：")
    for i, s in enumerate(problems, 1):
        print(f"   {i}) {s}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
