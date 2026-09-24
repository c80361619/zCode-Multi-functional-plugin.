/**
 * ZCode 输入框「增强提示词」按钮 —— 润色草稿
 * ============================================
 * 在输入框工具栏注入一个**纯图标**按钮（不显示任何文案）：
 *   左键点击 → 取当前草稿 → 经 preload 桥 / main handler 用**选定的模型**调一次补全 →
 *             把结果写回输入框，图标临时变成「撤销」可一键还原。
 *   右键点击 → 弹出**模型选择菜单**（按供应商分组列出所有可用模型），
 *             选中即持久化进 enhance_config.json，之后每次润色都用它，零重启生效。
 *
 * 三种状态只靠图标 + 悬浮提示区分（不占宽度、不出现中文文本）：
 *   待机  ✦ 星芒图标   title「增强提示词（右键选择模型）」
 *   进行中 ◌ 旋转图标   title「增强中…」
 *   可还原 ↺ 撤销图标   title「恢复原文」
 *
 * 依赖：由 zcode_patcher.py --enhance-prompt 注入，配套 preload 桥（enhancePrompt /
 *      listEnhanceModels / saveEnhanceModel）与 main handler（zcode:enhance-prompt、
 *      zcode:enhance-model-save）。缺桥时按钮自动隐藏，不报错。
 *
 * 安全：只读输入框内容、只写输入框与自己的按钮/菜单；不额外发网络请求（请求在主进程侧发起）；
 *      异常静默，找不到输入框时自清理。诊断：window.__zenhanceDiag
 *
 * ★ 挂载点（1.4 修复「图标跑到输入框左上角」）：
 *   图标必须落在工具栏**右侧操作区**（与发送按钮同一组）。旧实现拿「发送按钮的父节点」当
 *   唯一锚点，取不到就退回**卡片 / dock 本体的 firstChild** —— 那两处都是输入框的祖先，
 *   等价于把图标钉在输入框左上角。而发送按钮并非恒定存在：输入框为空 + 会话进行中时，
 *   内核用「停止按钮」替换它（`sn = canStop && !hasContent`），于是图标就跑到左上角。
 *   现在改为「右侧操作区 → 发送/停止按钮父节点 → 工具栏行（贴行尾）」三级解析，
 *   任何一级都**不会**退到卡片 / dock 本体；全解析失败就保持原位、不动。
 *
 * ★ 右键菜单（1.5）：菜单是挂在 document.body 上的 fixed 浮层，**不进 composer 子树** ——
 *   这样既不干扰输入框布局，也不会被虚拟列表/重渲染搬走。模型清单与真正发请求时用的
 *   候选表**同源**（同一个 IPC 通道的 {list:true} 分支），不会出现「菜单里能选、点了报不可用」。
 * ★ 菜单的关闭策略（1.6 修「一出现就自动关闭」）：只有 ①点菜单外部 ②按 Esc ③再右键收起
 *   三种情况会关。**滚动不再关闭**（改节流重定位）——客户端消息区是虚拟列表、输入区 sticky，
 *   流式输出时几乎每帧都在滚，把 scroll 当关闭信号等于菜单刚出现就被关掉。
 *   点击关闭还带 **350ms 保护期**，防止触发「打开」的那串事件（右键 mousedown/mouseup、
 *   触控板多出来的事件）在监听器注册之后到达而误关。
 */
(() => {
  if (window.__zenhance) return;
  window.__zenhance = true;

  const MARK = "data-zenhance";
  const BTN_ID = "zcode-enhance-prompt-btn";
  const MENU_ID = "zenhance-model-menu";
  const STYLE_ID = "zenhance-style";
  const diag = (window.__zenhanceDiag = window.__zenhanceDiag || {});
  diag.scriptVersion = "1.6";

  const TIP_IDLE = "增强提示词（右键选择模型）";
  const TIP_BUSY = "增强中…";
  const TIP_REVERT = "恢复原文";
  const REVERT_WINDOW_MS = 20000;

  const COMPOSER_INPUT_SELECTORS = [
    "[data-testid='v4-composer-input']",
    "[data-testid*='composer-input']",
    "textarea[data-testid]",
    "form textarea",
    "textarea",
    "[contenteditable='true']",
  ];

  // ---------- 图标（纯 SVG，无文案） ----------
  const ICONS = {
    // 星芒：增强
    idle: '<path fill="currentColor" d="M12 2.6l1.75 4.9 4.9 1.75-4.9 1.75L12 15.9l-1.75-4.9L5.35 9.25l4.9-1.75z"/>'
      + '<path fill="currentColor" d="M18.6 14.2l.85 2.3 2.3.85-2.3.85-.85 2.3-.85-2.3-2.3-.85 2.3-.85z"/>',
    // 旋转：进行中
    busy: '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="2.6" '
      + 'stroke-opacity="0.25"></circle>'
      + '<path d="M12 3.5a8.5 8.5 0 0 1 8.5 8.5" fill="none" stroke="currentColor" stroke-width="2.6" '
      + 'stroke-linecap="round"></path>',
    // 撤销箭头：恢复原文
    revert: '<path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
      + 'stroke-linejoin="round" d="M4.5 9.5h9a5 5 0 0 1 0 10H9"></path>'
      + '<path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
      + 'stroke-linejoin="round" d="M8 5.5l-3.5 4 3.5 4"></path>',
  };

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    try {
      const st = document.createElement("style");
      st.id = STYLE_ID;
      st.textContent = [
        "#" + BTN_ID + "{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;",
        "padding:0;border-radius:8px;border:0.5px solid transparent;background:transparent;cursor:pointer;",
        "color:var(--color-foreground-subtle,#5b6470);transition:background .15s,color .15s,opacity .15s}",
        "#" + BTN_ID + ":hover{background:rgba(127,127,127,.14);color:var(--color-foreground,#1b1f24)}",
        "#" + BTN_ID + "[disabled]{opacity:.55;cursor:default}",
        "#" + BTN_ID + "[data-mode='revert']{color:var(--color-warning,#b45309)}",
        "#" + BTN_ID + " svg{width:14px;height:14px;display:block}",
        "#" + BTN_ID + " svg.zenhance-spin{animation:zenhanceSpin .9s linear infinite;transform-origin:50% 50%}",
        "@keyframes zenhanceSpin{to{transform:rotate(360deg)}}",
        ".zenhance-toast{position:fixed;left:50%;bottom:96px;transform:translateX(-50%);z-index:2147483000;",
        "background:var(--color-background,#fff);color:var(--color-foreground,#1b1f24);",
        "border:1px solid rgba(127,127,127,.28);border-radius:10px;padding:8px 14px;font-size:12px;",
        "box-shadow:0 8px 24px rgba(0,0,0,.18);max-width:72vw;white-space:pre-wrap}",
        // —— 右键菜单（fixed 浮层，挂在 body 上，不进 composer 子树）——
        "#" + MENU_ID + "{position:fixed;z-index:2147483001;min-width:230px;max-width:360px;",
        "max-height:60vh;overflow-y:auto;overflow-x:hidden;box-sizing:border-box;padding:4px;",
        "background:var(--color-background,#fff);color:var(--color-foreground,#1b1f24);",
        "border:1px solid rgba(127,127,127,.28);border-radius:10px;font-size:12px;line-height:1.5;",
        "box-shadow:0 10px 30px rgba(0,0,0,.22);-webkit-user-select:none;user-select:none}",
        "#" + MENU_ID + " .zenhance-menu-title{padding:6px 8px 4px;font-size:11px;font-weight:600;opacity:.7}",
        "#" + MENU_ID + " .zenhance-menu-group{padding:7px 8px 2px;font-size:11px;opacity:.55;",
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
        "#" + MENU_ID + " .zenhance-menu-item{padding:5px 8px;border-radius:6px;cursor:pointer;",
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
        "#" + MENU_ID + " .zenhance-menu-item:hover{background:rgba(127,127,127,.16)}",
        "#" + MENU_ID + " .zenhance-menu-item[data-active='1']{font-weight:600;",
        "color:var(--color-primary,#2563eb)}",
        "#" + MENU_ID + " .zenhance-menu-empty{padding:8px;opacity:.6;white-space:normal}",
      ].join("");
      document.head.appendChild(st);
    } catch (err) { /* 静默 */ }
  }

  function toast(msg, ms) {
    try {
      const old = document.querySelector(".zenhance-toast");
      if (old) old.remove();
      const el = document.createElement("div");
      el.className = "zenhance-toast";
      el.textContent = String(msg);
      document.body.appendChild(el);
      setTimeout(() => el.remove(), ms || 3600);
    } catch (err) { /* 静默 */ }
  }

  // ---------- 右键菜单：选择润色使用的模型 ----------
  // 菜单挂在 document.body 上（fixed 浮层），**不进 composer 子树**：
  //   · 不会被虚拟列表 / 重渲染搬走；
  //   · 不参与输入框布局，不会把图标位置搞乱。
  // 模型清单走 listEnhanceModels()（主进程 {list:true} 分支），与真正发请求时用的是
  // **同一份候选表** —— 杜绝「菜单里能选、点了却报不可用」的两套逻辑漂移。
  //
  // ★ 关闭策略（1.6 修「菜单一出现就自动关闭」）：
  //   只有三种情况才关 —— ①点菜单**外部** ②按 Esc ③再右键收起。
  //   **滚动不再关闭**（改为节流重定位）：客户端消息区是虚拟列表、输入区 sticky，
  //   流式输出时几乎每帧都在滚，把 scroll 当关闭信号 = 菜单刚出现就被关掉。
  //   **点击关闭带 350ms 保护期**：触发「打开」的那串事件（右键 mousedown/mouseup、
  //   触控板可能多出来的事件）若在监听器注册之后才到达，会把菜单当场关掉。
  //   按钮被重渲染摘掉时**不立刻关**（4 秒宽限 + 位置缓存），避免 composer 抖动误伤。
  let menuEl = null;
  let menuOpen = false;
  let menuOpenedAt = 0;        // 打开时刻（保护期用）
  let menuOrphanSince = 0;     // 按钮被摘掉的时刻（宽限期用）
  let menuLastRect = null;     // 按钮最近一次位置（按钮暂时不在时菜单不跳位）
  let menuScrollTick = null;
  let override = null;   // 本次会话内选中的模型 {providerId, modelId}；null = 跟随界面选择
  diag.overrideModel = null;
  diag.menuOpens = 0;
  diag.menuClosedBy = null;

  function onDocDown(e) {
    // ★ 保护期：刚打开的那一瞬间不响应「点击外部」——
    //   否则触发打开的那串事件会把菜单当场关掉（表现就是「右击弹出后立刻消失」）。
    if (Date.now() - menuOpenedAt < 350) return;
    // 点菜单外部 → 关闭。捕获阶段监听，避免被内部元素的 stopPropagation 吃掉。
    if (menuEl && e && e.target && menuEl.contains(e.target)) return;
    closeMenu("outside");
  }
  function onDocKey(e) {
    if (e && (e.key === "Escape" || e.keyCode === 27)) closeMenu("escape");
  }
  // ★ 滚动只重定位、不关闭（节流 80ms：滚动事件密集，直接重排会掉帧）
  function onDocScroll() {
    if (!menuOpen || menuScrollTick) return;
    menuScrollTick = setTimeout(() => {
      menuScrollTick = null;
      if (menuOpen) placeMenu();
    }, 80);
  }

  function closeMenu(why) {
    if (!menuOpen && !menuEl) return;
    menuOpen = false;
    menuOrphanSince = 0;
    if (why) diag.menuClosedBy = why;
    if (menuScrollTick) { try { clearTimeout(menuScrollTick); } catch (err) { /* ignore */ } menuScrollTick = null; }
    if (menuEl) { try { menuEl.remove(); } catch (err) { /* ignore */ } }
    menuEl = null;
    try { document.removeEventListener("mousedown", onDocDown, true); } catch (err) { /* ignore */ }
    try { document.removeEventListener("keydown", onDocKey, true); } catch (err) { /* ignore */ }
    try { document.removeEventListener("scroll", onDocScroll, true); } catch (err) { /* ignore */ }
  }

  /** 定位菜单：优先贴在按钮**上方**（按钮在输入框底部，向上弹不会顶出屏幕）。 */
  function placeMenu() {
    if (!menuEl) return;
    // 按钮暂时不在文档里（重渲染窗口）时沿用上次位置，避免菜单跳到屏幕角落
    let r = menuLastRect || { left: 8, top: 8, right: 8, bottom: 8, width: 0, height: 0 };
    try {
      if (btn && btn.isConnected && btn.getBoundingClientRect) {
        const rr = btn.getBoundingClientRect();
        if (rr && (rr.width || rr.height || rr.top || rr.left)) { r = rr; menuLastRect = rr; }
      }
    } catch (err) { /* ignore */ }
    const mh = menuEl.offsetHeight || 260;
    const mw = menuEl.offsetWidth || 240;
    let left = Number(r.left) || 8;
    let top = (Number(r.top) || 8) - mh - 6;
    if (top < 8) top = (Number(r.bottom) || 8) + 6;
    let vw = 0, vh = 0;
    try { vw = document.documentElement ? (document.documentElement.clientWidth || 0) : 0; } catch (err) { vw = 0; }
    if (!vw) { try { vw = window.innerWidth || 0; } catch (err) { vw = 0; } }
    try { vh = window.innerHeight || 0; } catch (err) { vh = 0; }
    if (vw && left + mw > vw - 8) left = Math.max(8, vw - mw - 8);
    if (left < 8) left = 8;
    if (vh && top + mh > vh - 8) top = Math.max(8, vh - mh - 8);
    if (top < 8) top = 8;
    menuEl.style.cssText = "left:" + Math.round(left) + "px;top:" + Math.round(top) + "px";
  }

  function menuItem(cls, text) {
    const el = document.createElement("div");
    el.className = cls;
    el.textContent = text;
    return el;
  }

  function renderMenu(data) {
    if (!menuEl) return;
    menuEl.innerHTML = "";
    menuEl.appendChild(menuItem("zenhance-menu-title", "润色使用的模型（选中即生效）"));

    const cur = data && data.current;
    const follow = menuItem("zenhance-menu-item",
      (cur ? "" : "✓ ") + "跟随界面选择（默认）");
    follow.setAttribute("data-value", "__follow__");
    if (!cur) follow.setAttribute("data-active", "1");
    menuEl.appendChild(follow);

    const groups = (data && data.groups) || [];
    if (!groups.length) {
      menuEl.appendChild(menuItem("zenhance-menu-empty",
        "没有可用的供应商：请先在设置里填好 Base URL 与 API Key"));
    }
    for (const g of groups) {
      const pid = String((g && g.providerId) || "");
      menuEl.appendChild(menuItem("zenhance-menu-group",
        String((g && g.name) || pid || "未命名供应商")));
      const models = (g && g.models) || [];
      if (!models.length) {
        menuEl.appendChild(menuItem("zenhance-menu-empty", "（该供应商没有可用模型）"));
        continue;
      }
      for (const m of models) {
        const mid = String((m && m.id) || "");
        const active = !!(cur && cur.providerId === pid && cur.modelId === mid);
        const it = menuItem("zenhance-menu-item",
          (active ? "✓ " : "") + String((m && m.name) || mid));
        it.setAttribute("data-provider", pid);
        it.setAttribute("data-model", mid);
        it.setAttribute("title", pid + "/" + mid);
        if (active) it.setAttribute("data-active", "1");
        menuEl.appendChild(it);
      }
    }
    placeMenu();
  }

  /** 从被点中的节点向上找带 data-model / data-value 的菜单项。 */
  function menuTarget(node) {
    let n = node;
    while (n && n !== menuEl) {
      if (typeof n.getAttribute === "function"
          && (n.getAttribute("data-model") != null
              || n.getAttribute("data-value") != null)) return n;
      n = n.parentElement;
    }
    return null;
  }

  async function chooseModel(sel) {
    const api = window.zcode;
    closeMenu("choose");
    if (!api || typeof api.saveEnhanceModel !== "function") {
      toast("通信桥不可用：请重跑 --enhance-prompt 注入后重启 ZCode", 7000);
      return;
    }
    try {
      const r = await api.saveEnhanceModel(
        sel ? { providerId: sel.providerId, modelId: sel.modelId } : { clear: true });
      if (!r || !r.success) {
        toast("保存模型选择失败：" + String((r && r.error) || "未知错误"), 7000);
        return;
      }
      override = sel || null;
      diag.overrideModel = override;
      toast(sel ? ("润色模型已设为「" + sel.modelId + "」，下次点击即生效")
                : "润色模型已恢复为「跟随界面选择」");
    } catch (err) {
      toast("保存模型选择异常：" + String((err && err.message) || err), 7000);
    }
  }

  function bindMenu() {
    if (!menuEl) return;
    menuEl.addEventListener("click", (e) => {
      if (e && e.preventDefault) e.preventDefault();
      if (e && e.stopPropagation) e.stopPropagation();
      const it = menuTarget(e && e.target);
      if (!it) return;
      if (it.getAttribute("data-value") === "__follow__") { chooseModel(null); return; }
      const pid = it.getAttribute("data-provider");
      const mid = it.getAttribute("data-model");
      if (pid && mid) chooseModel({ providerId: pid, modelId: mid });
    });
  }

  async function openMenu() {
    if (menuOpen) { closeMenu("toggle"); return; }   // 再按一次右键 = 收起
    const api = window.zcode;
    if (!api || typeof api.listEnhanceModels !== "function") {
      toast("通信桥不可用：请重跑 --enhance-prompt 注入后重启 ZCode", 7000);
      return;
    }
    closeMenu("reopen");
    menuOpen = true;
    menuOpenedAt = Date.now();
    menuOrphanSince = 0;
    diag.menuOpens = (diag.menuOpens || 0) + 1;
    diag.menuClosedBy = null;
    menuEl = document.createElement("div");
    menuEl.id = MENU_ID;
    menuEl.setAttribute(MARK, "menu");
    menuEl.style.cssText = "left:8px;top:8px";
    menuEl.appendChild(menuItem("zenhance-menu-title", "正在读取模型…"));
    try { document.body.appendChild(menuEl); } catch (err) { /* ignore */ }
    bindMenu();
    placeMenu();
    try { document.addEventListener("mousedown", onDocDown, true); } catch (err) { /* ignore */ }
    try { document.addEventListener("keydown", onDocKey, true); } catch (err) { /* ignore */ }
    try { document.addEventListener("scroll", onDocScroll, true); } catch (err) { /* ignore */ }
    try {
      const data = await api.listEnhanceModels();
      if (!menuOpen || !menuEl) return;     // 读取期间已被关掉，别往已摘除的节点上写
      if (!data || !data.success) {
        menuEl.innerHTML = "";
        menuEl.appendChild(menuItem("zenhance-menu-empty",
          "读取模型失败：" + String((data && data.error) || "未知错误")));
        placeMenu();
        return;
      }
      renderMenu(data);
    } catch (err) {
      if (menuEl) {
        menuEl.innerHTML = "";
        menuEl.appendChild(menuItem("zenhance-menu-empty",
          "读取模型异常：" + String((err && err.message) || err)));
        placeMenu();
      }
    }
  }

  // ---------- 输入框定位 / 读写 ----------
  // ★ 关键：绝不能在整个 document 里搜「textarea / [contenteditable]」——
  //   会话消息区（timeline）里同样可能存在这类节点（消息内嵌编辑器、选区工具等），
  //   一旦命中就会被当成交互输入框，按钮随即被插进消息流里，表现为「按钮跑出输入框」。
  //   因此所有查找都先锚定到 composer dock（输入框所在的固定容器）内部。
  const DOCK_SELECTORS = [
    "[data-v4-composer-dock='true']",
    "[data-v4-composer-dock]",
    "[data-testid='v4-composer']",
  ];

  /** 取 composer dock —— 输入框与其工具栏行的最近公共容器。 */
  function findDock() {
    for (const sel of DOCK_SELECTORS) {
      let els = [];
      try { els = Array.from(document.querySelectorAll(sel)); } catch (err) { continue; }
      if (!els.length) continue;
      // 可见优先；仍以「最靠下」为准则（多会话/侧边栏场景可能有多个）
      const vis = els.filter((e) => e.offsetParent != null);
      const pool = vis.length ? vis : els;
      pool.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
      return pool[0];
    }
    return null;
  }

  function findInput() {
    // ① 严格模式：只在 dock 内找输入框。找不到宁可返回 null 不动，
    //    也绝不去消息区里误抓一个元素回来。
    const dock = findDock();
    if (dock) {
      for (const sel of COMPOSER_INPUT_SELECTORS) {
        let els = [];
        try { els = Array.from(dock.querySelectorAll(sel)); } catch (err) { continue; }
        if (!els.length) continue;
        const vis = els.filter((e) => e.offsetParent != null);
        const pool = vis.length ? vis : els;
        pool.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
        return pool[0];
      }
    }
    // ② 兼容模式：dock 锚点不存在才退回全局，但仅认「带明确 composer 语义」的选择器，
    //    不含 textarea / [contenteditable] 这类会误伤消息区的通用选择器。
    for (const sel of COMPOSER_INPUT_SELECTORS) {
      if (sel === "textarea" || sel === "[contenteditable='true']" || sel === "form textarea") continue;
      let els = [];
      try { els = Array.from(document.querySelectorAll(sel)); } catch (err) { continue; }
      if (!els.length) continue;
      const vis = els.filter((e) => e.offsetParent != null);
      const pool = vis.length ? vis : els;
      pool.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
      return pool[0];
    }
    return null;
  }

  function readText(el) {
    if (!el) return "";
    if (typeof el.value === "string") return el.value;
    return el.innerText || el.textContent || "";
  }

  /** 写回受控输入框：优先 execCommand('insertText')（textarea 与 contenteditable 都能触发
   *  React 受控更新），失败退回「原生 setter + input 事件」。 */
  function writeText(el, text) {
    if (!el) return false;
    try { el.focus(); } catch (err) { /* ignore */ }
    try {
      if (typeof el.select === "function") el.select();
      else if (typeof el.setSelectionRange === "function") el.setSelectionRange(0, readText(el).length);
      else {
        const range = document.createRange();
        range.selectNodeContents(el);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
      if (document.execCommand && document.execCommand("insertText", false, text)) return true;
    } catch (err) { /* 落到兜底 */ }
    try {
      const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : (el instanceof HTMLInputElement ? HTMLInputElement.prototype : null);
      const setter = proto && Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(el, text);
      else el.textContent = text;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    } catch (err) {
      return false;
    }
  }

  /** 当前选中的模型：模型按钮就挂在 composer 工具栏上，因此先锚定 dock 再找
   *  [data-model-current-value]（形如 providerId/modelId）。
   *  ★ 修复：旧实现全局搜 + 「最靠下」启发式会稳定失灵——输入框工具栏位于
   *  fixed 定位容器内，offsetParent 恒为 null 被「可见过滤」整批误杀；而后台
   *  残留的设置/工作流面板节点反而「更靠下」被取走（或整池为空），于是
   *  mv/ml 恒为空 → 主进程各档解析全空 → 掉进兜底档，把请求发给供应商表里
   *  第一个可用供应商的第一个模型。典型表现：报错里的模型不是你选的那个。 */
  function currentModel() {
    let value = "", label = "";
    try {
      const dock = findDock();
      let pool = [];
      if (dock) pool = Array.from(dock.querySelectorAll("[data-model-current-value]"));
      if (!pool.length) {
        // dock 内没有（极端布局）才退回全局，但排除工作流运行设置等面板的残留节点
        pool = Array.from(document.querySelectorAll("[data-model-current-value]"))
          .filter((e) => !e.closest("[data-testid='workflow-run-settings-model']"));
      }
      if (pool.length) {
        const vis = pool.filter((e) => e.offsetParent != null);
        // dock 命中时不再因 offsetParent=null 丢弃：fixed 容器里该属性本就恒为 null
        const usablePool = (dock && pool.length) ? pool : (vis.length ? vis : pool);
        usablePool.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
        const el = usablePool[0];
        value = String(el.getAttribute("data-model-current-value") || "").trim();
        // 显示名取该节点里最长的可见文本（模型名 + 可能的连接方式后缀）
        const t = String(el.textContent || "").replace(/\s+/g, " ").trim();
        label = t;
        diag.modelCandidates = pool.length;
      }
    } catch (err) { /* ignore */ }
    return { value, label };
  }

  // ---------- 挂载点解析 ----------
  // 客户端 composer 工具栏的真实结构（3.14.3，out/renderer 实证）：
  //
  //   div[data-testid='v4-composer']                ← 卡片 = 输入框所在区域(.chat-composer-region)
  //    └─ div.chat-composer-input-surface
  //       └─ div.group/toolbar.flex.items-end.gap-3 ← 工具栏行
  //          ├─ div[data-composer-leading-actions]      ← 左侧（+ / 附件）
  //          └─ div[data-composer-trailing-actions]     ← ★ 右侧操作区（恒存在）
  //             └─ div.flex.min-w-0.items-center.gap-1  ← 提交控件容器
  //                ├─ span …                              ← 模型胶囊
  //                └─ button[data-testid='v4-composer-send'] ← 发送
  //
  // ★ 发送按钮不是恒定锚点：**输入框为空 + 会话进行中**时，内核用
  //   button[data-testid='v4-stop']（停止）替换它（sn = canStop && !hasContent）。
  //   旧实现只认 v4-composer-send，取不到就退回卡片/dock 的 firstChild —— 那正是
  //   输入框左上角，于是「输入框为空」和「会话进行中」两种情况下图标都会跑到左上角。
  const CARD_SELECTOR = "[data-testid='v4-composer']";
  const TRAILING_SELECTOR = "[data-composer-trailing-actions]";
  const ANCHOR_BUTTON_SELECTORS = [
    "[data-testid='v4-composer-send']",
    "[data-testid='v4-stop']",
  ];

  /** 输入框所在的 composer 卡片（.chat-composer-region）；找不到才退回 dock。 */
  function findCard(input, dock) {
    let card = null;
    try { card = input && input.closest(CARD_SELECTOR); } catch (err) { /* ignore */ }
    if (!card && dock) {
      try { card = dock.querySelector(CARD_SELECTOR); } catch (err) { /* ignore */ }
    }
    return card || dock || null;
  }

  /** 工具栏行：class 同时含 flex 与 items-end 的 div（与 TPS 状态栏同一套结构判定，
   *  不依赖任何会随版本/语言变化的文案）。 */
  function findToolbarRow(card) {
    if (!card) return null;
    let rows = [];
    try {
      rows = Array.from(card.querySelectorAll("div")).filter((el) => {
        const c = typeof el.className === "string" ? el.className : "";
        return /(^|\s)flex(\s|$)/.test(c) && /items-end/.test(c);
      });
    } catch (err) { /* ignore */ }
    if (!rows.length) return null;
    return rows.find((el) => el.querySelector("button,select,[role='combobox'],input"))
        || rows[rows.length - 1];
  }

  /** 解析挂载点，返回 { host, where }：where = "prepend"（右侧操作区，与发送按钮同组）
   *  或 "append"（兜底工具栏行，贴行尾）。host 为 null = 解析失败。
   *  ★ 任何一级都**不得**退到卡片 / dock 本体：它们是输入框的祖先，往其 firstChild
   *    插入就等于把图标钉在输入框左上角（本次要修的 bug）。 */
  function findMount(input, dock) {
    const card = findCard(input, dock);
    // ① 首选：右侧操作区 —— 唯一跨状态恒存在的锚点（发送/停止按钮都在它内部）
    let host = null;
    if (card) {
      try { host = card.querySelector(TRAILING_SELECTOR); } catch (err) { /* ignore */ }
    }
    if (host) return { host, where: "prepend" };
    // ② 次选：发送 / 停止按钮的父节点（两者同属一个提交控件容器）。
    //    先在 card 内查，避免多会话/多 composer 时抓到别的卡片的按钮。
    for (const sel of ANCHOR_BUTTON_SELECTORS) {
      let el = null;
      if (card) {
        try { el = card.querySelector(sel); } catch (err) { /* ignore */ }
      }
      if (!el) {
        try { el = document.querySelector(sel); } catch (err) { /* ignore */ }
      }
      if (el && el.parentElement && (!card || card.contains(el))) {
        return { host: el.parentElement, where: "prepend" };
      }
    }
    // ③ 末选：工具栏行 —— 追加到**行尾**（右端），绝不插行首（= 输入框左侧/左上角）
    const row = findToolbarRow(card);
    if (row) return { host: row, where: "append" };
    return { host: null, where: null };
  }

  /** 落位：prepend = 图标落在模型胶囊/发送按钮**左侧**（即既有正确位置）；
   *  append 仅用于兜底工具栏行。 */
  function place(el, host, where) {
    if (where === "append") host.appendChild(el);
    else host.insertBefore(el, host.firstChild);
  }

  // ---------- 按钮 ----------
  let btn = null;
  let busy = false;
  let original = null;
  let revertTimer = null;

  function setButton(mode) {
    if (!btn) return;
    const tip = mode === "busy" ? TIP_BUSY : (mode === "revert" ? TIP_REVERT : TIP_IDLE);
    btn.setAttribute("data-mode", mode);
    btn.setAttribute("title", tip);
    btn.setAttribute("aria-label", tip);
    btn.disabled = busy;
    btn.innerHTML = "";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    if (mode === "busy") svg.setAttribute("class", "zenhance-spin");
    svg.innerHTML = ICONS[mode] || ICONS.idle;
    btn.appendChild(svg);
  }

  function armRevert() {
    clearTimeout(revertTimer);
    setButton("revert");
    revertTimer = setTimeout(() => {
      original = null;
      setButton("idle");
    }, REVERT_WINDOW_MS);
  }

  async function enhance() {
    const el = findInput();
    if (!el) { toast("没找到输入框"); return; }
    const text = readText(el).trim();
    if (!text) { toast("请先输入要增强的内容"); return; }

    const api = window.zcode;
    if (!api || typeof api.enhancePrompt !== "function") {
      toast("通信桥不可用：请重跑 --enhance-prompt 注入后重启 ZCode");
      return;
    }

    busy = true;
    setButton("busy");
    const m = currentModel();
    diag.lastRequest = { modelValue: m.value, modelLabel: m.label, chars: text.length,
                         override: override || null };
    try {
      // 右键菜单选中的模型随请求一起传（主进程 explicit 档，最高优先级）：
      // 即使 enhance_config.json 写入失败或被别处改动，这一次点击也一定用它。
      const res = await api.enhancePrompt(text, m.value, m.label, override || undefined);
      diag.lastResult = res && res.success
        ? { model: res.model, provider: res.provider, how: res.how, chars: String(res.text || "").length }
        : { code: (res && res.code) || "unknown", error: String((res && res.error) || "未知错误") };
      if (!res || !res.success) {
        // 「模型不可用」是配置问题，不是网络问题 —— 给出可执行的下一步，而不是甩一个 HTTP 400
        const tip = String((res && res.tip) || "");
        const head = "增强失败：" + String((res && res.error) || "未知错误");
        const body = tip ? ("\n→ " + tip) : "";
        toast(head + body, tip ? 9000 : 5200);
        if (res && (res.code === "model" || res.code === "auth" || res.code === "no-model")) {
          console.warn("[zcode-enhance] 配置类失败", res);
        }
        return;
      }
      const enhanced = String(res.text || "").trim();
      if (!enhanced) { toast("增强失败：模型返回空内容", 5200); return; }
      original = text;
      if (!writeText(el, enhanced)) {
        toast("已生成结果但写回输入框失败，请手动复制：\n" + enhanced.slice(0, 200), 8000);
        return;
      }
      armRevert();
      toast("已用「" + (res.model || "当前模型") + "」增强");
    } catch (err) {
      diag.lastResult = { code: "exception", error: String(err) };
      toast("增强异常：" + String((err && err.message) || err), 5200);
    } finally {
      busy = false;
      if (btn) {
        if (btn.getAttribute("data-mode") === "revert") btn.disabled = false;
        else setButton("idle");
      }
    }
  }

  function onRevert() {
    const el = findInput();
    if (el && original != null) writeText(el, original);
    original = null;
    clearTimeout(revertTimer);
    setButton("idle");
  }

  function ensureButton() {
    ensureStyle();
    const input = findInput();
    if (!input) { if (btn) { btn.remove(); btn = null; } diag.hiddenReason = "未找到输入框"; return; }

    // ★ 菜单开着时的看护：按钮在 → 跟随重定位；按钮被重渲染摘掉 → 给 4 秒宽限
    //   （菜单保持可点），宽限内回来就继续跟随，超时才收起。
    //   绝**不**因为「按钮这一瞬间不在」就立刻关菜单 —— composer 频繁重渲染，
    //   那会让菜单在用户眼皮底下凭空消失。
    if (menuOpen) {
      if (btn && btn.isConnected) {
        menuOrphanSince = 0;
        placeMenu();
      } else if (!menuOrphanSince) {
        menuOrphanSince = Date.now();
      } else if (Date.now() - menuOrphanSince > 4000) {
        closeMenu("btn-gone");
      }
    }

    // ★ 挂载点 = 工具栏「右侧操作区」（与发送按钮同一组），见 findMount()。
    //   绝不再退回卡片 / dock 本体：那是输入框的祖先，往其 firstChild 插入
    //   就等于把图标钉在输入框左上角（历史 bug：输入框为空 / 会话进行中）。
    const dock = findDock();
    const mount = findMount(input, dock);
    const host = mount.host;
    if (!host) {
      // 解析不到操作区：保持现状，绝不搬到左上角。从未挂上过才允许下次重试。
      diag.hiddenReason = "未找到工具栏操作区";
      if (btn && !btn.isConnected) btn = null;
      return;
    }
    diag.hiddenReason = null;
    diag.mountWhere = mount.where;

    if (btn && btn.isConnected) {
      // ★ 位置自愈：按钮虽还在文档里，但已不在正确容器内（客户端重渲染把按钮
      //   连同旧节点一起搬走 / 被消息列表的 DOM 复用吞掉）→ 主动搬回来。
      const okPlace = btn.parentElement === host;
      const stillInDock = !dock || dock.contains(btn);
      if (!okPlace || !stillInDock) {
        diag.reattaches = (diag.reattaches || 0) + 1;
        place(btn, host, mount.where);
      }
      return;
    }
    btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.type = "button";
    btn.setAttribute(MARK, "1");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (busy) return;
      if (btn.getAttribute("data-mode") === "revert") onRevert();
      else enhance();
    });
    // 右键 = 选模型（不触发左键的增强/还原）
    btn.addEventListener("contextmenu", (e) => {
      if (e && e.preventDefault) e.preventDefault();
      if (e && e.stopPropagation) e.stopPropagation();
      openMenu();
    });
    place(btn, host, mount.where);
    setButton("idle");
    diag.buttonAttached = true;
  }

  function start() {
    ensureStyle();
    let timer = null;
    // 抖动：DOM 变动很密集（流式输出时每帧都在改），300ms 合并一次足够；
    // 且 ensureButton() 内部只在「位置不对」时才真正写 DOM，不会引起抖动循环。
    const schedule = () => {
      if (timer) return;
      timer = setTimeout(() => { timer = null; if (!document.hidden) ensureButton(); }, 300);
    };
    try {
      // ★ 只监听 dock 所在的子树 + 顶层结构变化，避免被消息流每帧刷新增量拖慢；
      //   订阅 childList 即可——按钮错位总是源于节点被移动/替换。
      new MutationObserver(schedule).observe(document.body, {
        childList: true, subtree: true,
      });
    } catch (err) { /* 静默 */ }
    // 兜底轮询：即使 MutationObserver 漏事件（如纯 style 变更导致的 sticky 失效），
    // 也能在一个短周期内把按钮搬回正确位置。
    setInterval(() => { if (!document.hidden) ensureButton(); }, 1000);
    ensureButton();
  }

  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);

  setTimeout(() => {
    if (!diag.buttonAttached) console.warn("[zcode-enhance] 按钮未挂载，window.__zenhanceDiag =", diag);
    else console.info("[zcode-enhance] 已就绪，window.__zenhanceDiag =", diag);
  }, 5000);
})();
