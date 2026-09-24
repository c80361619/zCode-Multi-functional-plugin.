// 润色按钮「挂载点」回归测试：用最小 DOM 桩复刻真实 composer 结构，把
// zcode-enhance-prompt.js 真跑一遍，逐状态核对按钮落在哪。
//
// 为什么需要它：这次要修的 bug 是**纯布局**的 —— 「输入框为空 / 会话进行中时，
// 图标跑到输入框左上角」。`node --check` 通过、肉眼 review 也看不出问题，
// 因为错的是「选哪个容器」而不是语法。而且触发条件是**状态相关**的：
//   内核里提交控件是 `sn = canStop && !hasContent ? 停止按钮 : 发送按钮`
//   —— 输入框为空 + 会话进行中时，发送按钮根本不存在。
// 所以必须把「发送按钮被替换成停止按钮」这件事在 DOM 里做出来，才算真验证。
//
// 用法：node tests/enhance_mount_smoke.js <zcode-enhance-prompt.js 路径>
// 退出码 0 = 通过；非 0 时逐条打印失败原因。
const fs = require("fs");
const vm = require("vm");

const target = process.argv[2];
if (!target) {
  console.error("用法：node tests/enhance_mount_smoke.js <zcode-enhance-prompt.js 的路径>");
  console.error("例如：node tests/enhance_mount_smoke.js skills/zcode-tokenspeed/scripts/zcode-enhance-prompt.js");
  process.exit(2);
}
let src;
try {
  src = fs.readFileSync(target, "utf8");
} catch (err) {
  console.error("读不到被测脚本：" + target);
  console.error("  " + err.message);
  process.exit(2);
}

// ---------------------------------------------------------------- 最小 DOM
// 只实现被测脚本真正用到的那部分：属性选择器 / 逗号选择器 / closest / contains /
// insertBefore / querySelector(All)。够用即止，不引 jsdom。
function parseSelector(sel) {
  return String(sel).split(",").map((part) => {
    const c = { tag: null, id: null, attrs: [], classes: [] };
    const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\[([\w-]+)(?:([*^$]?=)["']?([^"'\]]*)["']?)?\]|\.([\w-]+)/g;
    let m;
    while ((m = re.exec(part))) {
      if (m[1]) c.tag = m[1].toLowerCase();
      else if (m[2]) c.id = m[2];
      else if (m[3]) c.attrs.push({ name: m[3].toLowerCase(), op: m[4] || null, value: m[5] == null ? "" : m[5] });
      else if (m[6]) c.classes.push(m[6]);
    }
    return c;
  });
}

function matchCompound(el, c) {
  if (c.tag && el.tagName.toLowerCase() !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  for (const cls of c.classes) {
    if (!(" " + (el.attrs["class"] || "") + " ").includes(" " + cls + " ")) return false;
  }
  for (const a of c.attrs) {
    const v = el.attrs[a.name];
    if (v == null) return false;
    if (a.op === "=" && v !== a.value) return false;
    if (a.op === "*=" && !String(v).includes(a.value)) return false;
    if (a.op === "^=" && !String(v).startsWith(a.value)) return false;
    if (a.op === "$=" && !String(v).endsWith(a.value)) return false;
  }
  return true;
}

function matches(el, sel) {
  return parseSelector(sel).some((c) => matchCompound(el, c));
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeName = this.tagName;
    this.attrs = Object.create(null);
    this.children = [];
    this.parentElement = null;
    this.style = { cssText: "", setProperty() {}, removeProperty() {} };
    this.dataset = {};
    this.textContent = "";
    this.innerHTML = "";
    this.id = "";
    this.title = "";
    this.type = "";
    this.disabled = false;
    this.__hidden = false;
    this.__rect = { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    this.__listeners = {};
  }
  get className() { return this.attrs["class"] || ""; }
  set className(v) { this.attrs["class"] = String(v); }
  get offsetParent() {
    if (this.__hidden) return null;
    return this.parentElement || null;
  }
  get isConnected() {
    let n = this;
    while (n) {
      if (n.__isBody) return true;
      n = n.parentElement;
    }
    return false;
  }
  setAttribute(k, v) { this.attrs[String(k).toLowerCase()] = String(v); }
  getAttribute(k) {
    const key = String(k).toLowerCase();
    return Object.prototype.hasOwnProperty.call(this.attrs, key) ? this.attrs[key] : null;
  }
  hasAttribute(k) { return this.getAttribute(k) != null; }
  removeAttribute(k) { delete this.attrs[String(k).toLowerCase()]; }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    const i = c.parentElement ? c.parentElement.children.indexOf(c) : -1;
    if (i >= 0) c.parentElement.children.splice(i, 1);
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at >= 0) this.children.splice(at, 0, c); else this.children.push(c);
    c.parentElement = this;
    return c;
  }
  remove() {
    if (this.parentElement) {
      const i = this.parentElement.children.indexOf(this);
      if (i >= 0) this.parentElement.children.splice(i, 1);
      this.parentElement = null;
    }
  }
  contains(node) {
    let n = node;
    while (n) { if (n === this) return true; n = n.parentElement; }
    return false;
  }
  closest(sel) {
    let n = this;
    while (n) { if (matches(n, sel)) return n; n = n.parentElement; }
    return null;
  }
  descendants() {
    const out = [];
    const walk = (el) => { for (const c of el.children) { out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelectorAll(sel) { return this.descendants().filter((el) => matches(el, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getBoundingClientRect() { return Object.assign({}, this.__rect); }
  getClientRects() { return [this.getBoundingClientRect()]; }
  addEventListener(t, fn) { (this.__listeners[t] = this.__listeners[t] || []).push(fn); }
  removeEventListener() {}
  dispatchEvent() { return true; }
  focus() {}
  select() {}
  setSelectionRange() {}
  blur() {}
  click() {
    for (const fn of this.__listeners.click || []) fn({ preventDefault() {}, stopPropagation() {} });
  }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
}

function makeDocument() {
  const doc = {
    hidden: false,
    head: new El("head"),
    body: new El("body"),
    createElement: (t) => new El(t),
    createElementNS: (_ns, t) => new El(t),
    createRange: () => ({ selectNodeContents() {} }),
    getSelection: () => ({ removeAllRanges() {}, addRange() {} }),
    execCommand: () => false,
    addEventListener() {},
    removeEventListener() {},
  };
  doc.body.__isBody = true;
  doc.getElementById = (id) =>
    doc.body.descendants().concat([doc.head]).find((el) => el.id === id) || null;
  doc.querySelectorAll = (sel) => doc.body.descendants().filter((el) => matches(el, sel));
  doc.querySelector = (sel) => doc.querySelectorAll(sel)[0] || null;
  return doc;
}

// ---------------------------------------------------------------- 搭真实结构
// 与 out/renderer 实测一致（3.14.3）：
//   div[data-v4-composer-dock=true]
//    └─ div[data-v4-composer-dock-content]
//       └─ div[data-v4-back-to-bottom-anchor=composer-dock]
//          └─ div[data-testid=v4-composer].chat-composer-region   ← 卡片
//             └─ div.chat-composer-input-surface
//                ├─ div[data-testid=v4-composer-input]            ← 输入框
//                └─ div.group/toolbar.flex.items-end.gap-3        ← 工具栏行
//                   ├─ div[data-composer-leading-actions]
//                   └─ div[data-composer-trailing-actions]        ← ★ 右侧操作区
//                      └─ div.flex.min-w-0.items-center.gap-1     ← 提交控件容器
//                         ├─ span (模型胶囊, data-model-current-value)
//                         └─ button[data-testid=v4-composer-send | v4-stop]
function buildTree(doc) {
  const mk = (tag, attrs, cls) => {
    const el = new El(tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    if (cls) el.className = cls;
    return el;
  };
  const dock = mk("div", { "data-v4-composer-dock": "true" }, "pointer-events-none z-20 flex w-full justify-center sticky bottom-0");
  const dockContent = mk("div", { "data-v4-composer-dock-content": "true" });
  const anchor = mk("div", { "data-v4-back-to-bottom-anchor": "composer-dock" }, "relative");
  const card = mk("div", { "data-testid": "v4-composer" }, "chat-composer-region z-20 w-full shrink-0");
  const surface = mk("div", null, "chat-composer-input-surface w-full");
  const input = mk("div", { "data-testid": "v4-composer-input", "contenteditable": "true" });
  input.textContent = "";
  const row = mk("div", null, "group/toolbar flex items-end gap-3");
  const leading = mk("div", { "data-composer-leading-actions": "true" }, "flex min-w-0 flex-1 items-center");
  const leadingContent = mk("div", { "data-composer-leading-content": "true" }, "flex shrink-0 items-center gap-1");
  const trailing = mk("div", { "data-composer-trailing-actions": "true" }, "ml-auto flex shrink-0 items-center justify-end gap-1.5");
  const submitBox = mk("div", null, "flex min-w-0 items-center gap-1");
  const pill = mk("span", { "data-model-current-value": "prov-1/glm-5.3" }, "inline-flex min-w-0");

  dock.appendChild(dockContent);
  dockContent.appendChild(anchor);
  anchor.appendChild(card);
  card.appendChild(surface);
  surface.appendChild(input);
  surface.appendChild(row);
  row.appendChild(leading);
  leading.appendChild(leadingContent);
  row.appendChild(trailing);
  trailing.appendChild(submitBox);
  submitBox.appendChild(pill);

  doc.body.appendChild(dock);
  // 待机态：提交控件里是「发送按钮」
  const send = mk("button", { "data-testid": "v4-composer-send" });
  submitBox.appendChild(send);
  return { dock, dockContent, anchor, card, surface, input, row, leading, trailing, submitBox, pill, send, mk };
}

// ---------------------------------------------------------------- 跑脚本
function boot(doc) {
  const timers = { interval: null };
  const sandbox = {
    document: doc,
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: (fn) => { timers.interval = fn; return 0; },
    clearInterval: () => {},
    MutationObserver: class { observe() {} disconnect() {} },
    Event: class {},
    HTMLTextAreaElement: class {},
    HTMLInputElement: class {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(src, sandbox, { filename: "zcode-enhance-prompt.js" });
  return { sandbox, timers, tick: () => timers.interval && timers.interval() };
}

const bad = [];
const check = (cond, msg) => { if (!cond) bad.push(msg); };

const doc = makeDocument();
const t = buildTree(doc);
const { sandbox, tick } = boot(doc);

const BTN = "#zcode-enhance-prompt-btn";
const btn = () => doc.getElementById("zcode-enhance-prompt-btn");
const where = (b) => {
  if (!b || !b.parentElement) return "detached";
  if (b.parentElement === t.trailing) return "trailing";
  if (b.parentElement === t.submitBox) return "submitBox";
  if (b.parentElement === t.row) return "toolbarRow";
  if (b.parentElement === t.card) return "CARD(top-left!)";
  if (b.parentElement === t.dock) return "DOCK(top-left!)";
  if (b.parentElement === t.anchor) return "ANCHOR(top-left!)";
  if (b.parentElement === t.dockContent) return "DOCK-CONTENT(top-left!)";
  return "other:" + (b.parentElement.className || b.parentElement.tagName);
};
// 「正确位置」= 工具栏右侧操作区（发送按钮所在的那一组）。
const inRightGroup = (b) => !!b && (b.parentElement === t.trailing || b.parentElement === t.submitBox);
// 「跑到左上角」的判定 = 按钮成了输入框某个祖先（卡片/dock/锚点）的直接子节点。
const atTopLeft = (b) => !!b && ["CARD(top-left!)", "DOCK(top-left!)", "ANCHOR(top-left!)",
  "DOCK-CONTENT(top-left!)"].includes(where(b));

// 状态 0：待机（发送按钮在，输入框为空且 disabled）
t.send.disabled = true;
check(!!btn(), "待机态：按钮没挂上（hiddenReason=" + sandbox.__zenhanceDiag.hiddenReason + "）");
check(inRightGroup(btn()), "待机态：按钮应落在右侧操作区，实际=" + where(btn()));
check(!atTopLeft(btn()), "待机态：按钮不得跑到输入框左上角，实际=" + where(btn()));

// 状态 1：输入框为空（发送按钮存在但 disabled）
tick();
check(inRightGroup(btn()), "输入框为空：按钮应仍在右侧操作区，实际=" + where(btn()));

// 状态 2：会话进行中 + 输入框为空 → 内核用「停止按钮」替换发送按钮
//         （sn = canStop && !hasContent）—— 这就是线上复现左上角的真实条件。
t.send.remove();
t.submitBox.appendChild(t.mk("button", { "data-testid": "v4-stop" }));
tick();
check(inRightGroup(btn()),
  "会话进行中(发送按钮已被停止按钮替换)：按钮应仍在右侧操作区，实际=" + where(btn()));
check(!atTopLeft(btn()), "会话进行中：按钮不得跑到输入框左上角，实际=" + where(btn()));

// 状态 3：发送/停止按钮都不在（更极端的重渲染窗口）—— 不得搬去卡片/dock
doc.querySelector("[data-testid='v4-stop']").remove();
tick();
check(!atTopLeft(btn()),
  "锚点全缺失：按钮不得被搬到卡片/dock（= 输入框左上角），实际=" + where(btn()));

// 状态 4：连 data-composer-trailing-actions 和发送/停止按钮都没有
//         → 只能落到工具栏行，且必须在**行尾**（插行首 = 输入框左侧/左上角）
t.trailing.removeAttribute("data-composer-trailing-actions");
tick();
check(where(btn()) === "toolbarRow",
  "兜底态：应落到工具栏行，实际=" + where(btn()));
check(btn() === t.row.children[t.row.children.length - 1],
  "兜底态：必须追加在工具栏**行尾**（插行首 = 输入框左侧/左上角）");

// 状态 5：操作区回来 + 发送按钮回位 → 自愈搬回右侧操作区
t.trailing.setAttribute("data-composer-trailing-actions", "true");
t.submitBox.appendChild(t.mk("button", { "data-testid": "v4-composer-send" }));
tick();
check(inRightGroup(btn()),
  "自愈：操作区恢复后应搬回右侧操作区，实际=" + where(btn()));

// 状态 6：幂等 —— 反复 tick 不得重复插入 / 重建按钮
const ref = btn();
for (let i = 0; i < 5; i++) tick();
check(btn() === ref, "幂等：不应重建按钮节点");
check(doc.querySelectorAll(BTN).length === 1, "幂等：按钮只应有一个");

// 诊断字段
const d = sandbox.__zenhanceDiag || {};
check(d.scriptVersion === "1.5", "diag.scriptVersion 应为 1.5，实际=" + d.scriptVersion);
check(d.buttonAttached === true, "diag.buttonAttached 应为 true");
check(d.mountWhere === "prepend", "diag.mountWhere 应为 prepend，实际=" + d.mountWhere);

// ---------------------------------------------------------------- 右键菜单
// 场景：右键按钮 → 菜单列出可用模型 → 选中某个 → 持久化 + 后续润色带上它。
// 为什么必须行为级验证：菜单里「选中了」和「真的用上了」是两件事 ——
// 只断言 DOM 里有对勾，抓不到「选择没传进 enhancePrompt」这类静默失效。
// 关键断言：①菜单是 body 上的 fixed 浮层，绝不进 composer 子树
//           ②清单来自 listEnhanceModels()（与发请求同源）
//           ③选中即持久化 ④下一次点击 enhancePrompt 第 4 参带 override
const tickAsync = () => new Promise((r) => setImmediate(r));
const fire = (el, type, ev) => { for (const fn of (el.__listeners[type] || [])) fn(ev); };
const evt = (extra) => Object.assign({ preventDefault() {}, stopPropagation() {} }, extra || {});

async function menuTests() {
  const calls = { list: 0, saved: [], enhance: [] };
  sandbox.zcode = {
    listEnhanceModels: async () => {
      calls.list++;
      return {
        success: true,
        groups: [
          { providerId: "prov-a", name: "供应商 A",
            models: [{ id: "model-a", name: "model-a" }, { id: "model-b", name: "model-b" }] },
          { providerId: "prov-b", name: "供应商 B", models: [{ id: "model-c", name: "model-c" }] },
        ],
        current: null,
      };
    },
    saveEnhanceModel: async (o) => { calls.saved.push(o); return { success: true }; },
    enhancePrompt: async (text, mv, ml, ov) => {
      calls.enhance.push({ text, mv, ml, ov });
      return { success: true, text: "ENHANCED", model: "model-a" };
    },
  };

  const menu = () => doc.getElementById("zenhance-model-menu");
  const btnEl = btn();
  check(!!btnEl, "菜单测试前置：按钮应存在");

  // ① 右键弹出菜单
  fire(btnEl, "contextmenu", evt());
  await tickAsync();
  check(!!menu(), "右键后应弹出模型选择菜单");
  check(calls.list === 1, "菜单应经 listEnhanceModels() 取清单，实际调用 " + calls.list + " 次");
  check(!!menu() && menu().parentElement === doc.body,
    "菜单应挂在 body 上（fixed 浮层），实际 parent="
    + (menu() && menu().parentElement && menu().parentElement.tagName));
  check(!!menu() && !t.dock.contains(menu()),
    "菜单不得进入 composer 子树（会被重渲染搬走 / 撑乱布局）");

  // ② 清单内容按供应商分组
  const labels = menu().querySelectorAll(".zenhance-menu-item").map((el) => el.textContent);
  check(labels.some((s) => s.indexOf("跟随界面选择") >= 0), "菜单应有「跟随界面选择」项");
  check(labels.some((s) => s.indexOf("model-a") >= 0), "菜单应列出 model-a");
  check(labels.some((s) => s.indexOf("model-c") >= 0), "菜单应列出其它供应商的 model-c");
  check(menu().querySelectorAll(".zenhance-menu-group").length === 2,
    "应按供应商分组，实际 " + menu().querySelectorAll(".zenhance-menu-group").length + " 组");
  check(labels.some((s) => s.indexOf("✓") === 0),
    "无 current 时「跟随界面选择」应打勾，实际=" + JSON.stringify(labels));

  // ③ 选中 model-a → 持久化
  const target = menu().querySelectorAll("[data-model='model-a']")[0];
  check(!!target, "应能找到 data-model='model-a' 的菜单项");
  fire(menu(), "click", evt({ target }));
  await tickAsync();
  check(calls.saved.length === 1, "选择后应调用 saveEnhanceModel 一次，实际 " + calls.saved.length);
  check(calls.saved[0] && calls.saved[0].providerId === "prov-a"
        && calls.saved[0].modelId === "model-a",
    "saveEnhanceModel 应收到 {providerId, modelId}，实际 " + JSON.stringify(calls.saved[0]));
  check(!menu(), "选完应关闭菜单");
  const d2 = sandbox.__zenhanceDiag || {};
  check(!!d2.overrideModel && d2.overrideModel.modelId === "model-a",
    "diag.overrideModel 应记录选择，实际 " + JSON.stringify(d2.overrideModel));

  // ④ 下一次点击必须把 override 交给主进程（explicit 档）
  t.input.textContent = "帮我写个函数";
  fire(btnEl, "click", evt());
  await tickAsync();
  check(calls.enhance.length === 1, "应触发一次 enhancePrompt，实际 " + calls.enhance.length);
  const last = calls.enhance[0] || {};
  check(!!last.ov && last.ov.providerId === "prov-a" && last.ov.modelId === "model-a",
    "enhancePrompt 第 4 参应带 override，实际 " + JSON.stringify(last.ov));

  // ⑤ 「跟随界面选择」→ clear:true，且 override 归零
  fire(btnEl, "contextmenu", evt());
  await tickAsync();
  const follow = menu().querySelectorAll("[data-value='__follow__']")[0];
  check(!!follow, "应能找到「跟随界面选择」项");
  fire(menu(), "click", evt({ target: follow }));
  await tickAsync();
  check(calls.saved.length === 2 && calls.saved[1] && calls.saved[1].clear === true,
    "「跟随界面选择」应传 clear:true，实际 " + JSON.stringify(calls.saved[1]));
  check(!(sandbox.__zenhanceDiag || {}).overrideModel,
    "clear 后 overrideModel 应回到 null");

  // ⑥ 菜单开着时再右键 = 收起（不能叠出两个）
  fire(btnEl, "contextmenu", evt());
  await tickAsync();
  check(!!menu(), "右键应再次打开菜单");
  fire(btnEl, "contextmenu", evt());
  await tickAsync();
  check(!menu(), "菜单打开时再右键应收起");
  check(doc.querySelectorAll("#zenhance-model-menu").length === 0, "不应残留菜单节点");
}

menuTests().then(() => {
  if (bad.length) {
    console.error("enhance mount smoke FAIL");
    for (const b of bad) console.error("  - " + b);
    process.exit(1);
  }
  console.log("enhance mount smoke OK (states=6, menu=ok, mount=" + where(btn()) + ")");
}).catch((err) => {
  console.error("enhance mount smoke CRASH");
  console.error("  " + (err && err.stack || err));
  process.exit(1);
});
