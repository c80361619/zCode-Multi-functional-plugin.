// TPS 状态栏「平均命中率」格式化冒烟测试。
//
// 为什么需要它：状态栏里这个百分比一度是 `Math.round(x*100)` 的整数，
// 改成「两位小数四舍五入」后，真正的风险不是写错表达式，而是**悄悄退回整数**
// （`pct` 直接拼进字符串）——那时语法照样合法、`node --check` 照样通过、界面
// 只是少两个小数位，肉眼 review 极易漏掉。这里锁两件事：
//   ① 渲染路径**必须**经 fmtPct，且不得再把裸 pct 拼进文本；
//   ② fmtPct 的行为确实是「两位小数 + 四舍五入」，包括 toFixed 的二进制陷阱值。
//
// 用法：node tests/tps_pct_smoke.js skills/zcode-tokenspeed/scripts/zcode-tps.js
// 退出码 0 = 通过；1 = 有断言失败；2 = 用法/读取错误。
const fs = require("fs");

const target = process.argv[2];
if (!target) {
  console.error("用法：node tests/tps_pct_smoke.js <zcode-tps.js 的路径>");
  console.error("例如：node tests/tps_pct_smoke.js skills/zcode-tokenspeed/scripts/zcode-tps.js");
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

const failures = [];
const check = (ok, label, extra) => {
  if (ok) return;
  failures.push(label + (extra ? "  —— " + extra : ""));
};

// ---------- ① 从源码里抠出 fmtPct 并真跑一遍 ----------
// 用花括号配对而不是正则：函数体后续若加分支/嵌套，正则抠法会静默截断。
function extractFn(name) {
  const head = "const " + name + " = ";
  const i = src.indexOf(head);
  if (i < 0) return null;
  const braceStart = src.indexOf("{", i + head.length);
  if (braceStart < 0) return null;
  let depth = 0;
  for (let j = braceStart; j < src.length; j++) {
    const c = src[j];
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) {
        // 返回 `(v) => { ... }` 形式，外层加括号即可当表达式求值
        return src.slice(i + head.length, j + 1).trim();
      }
    }
  }
  return null;
}

const fnSrc = extractFn("fmtPct");
check(!!fnSrc, "源码里找不到 `const fmtPct = ...`（函数被改名或删除了？）");

let fmtPct = null;
if (fnSrc) {
  try {
    // eslint-disable-next-line no-eval
    fmtPct = eval("(" + fnSrc + ")");
  } catch (err) {
    check(false, "fmtPct 源码无法求值", err.message);
  }
}

if (typeof fmtPct === "function") {
  const cases = [
    // [输入, 期望, 说明]
    [84, "84.00", "整数补零到两位"],
    [0, "0.00", "零"],
    [7, "7.00", "个位数补零"],
    [12.3, "12.30", "一位小数补零"],
    [100, "100.00", "满命中"],
    [33.333333, "33.33", "常规截断"],
    [66.66666, "66.67", "常规进位"],
    [84.567, "84.57", "第三位 7 进位"],
    [84.564, "84.56", "第三位 4 舍去"],
    [84.565, "84.57", "第三位 5 进位（四舍五入）"],
    [99.999, "100.00", "进位到整数边界"],
    [2.675, "2.68", "★ toFixed 陷阱值（裸 toFixed 会给 2.67）"],
    [1.005, "1.01", "★ toFixed 陷阱值（裸 toFixed 会给 1.00）"],
    [0.5, "0.50", "小于 1"],
    [0.004, "0.00", "极小值归零"],
  ];
  for (const [input, want, why] of cases) {
    let got;
    try {
      got = fmtPct(input);
    } catch (err) {
      check(false, "fmtPct(" + input + ") 抛错", err.message);
      continue;
    }
    check(got === want, "fmtPct(" + input + ") 期望 " + want + " 实得 " + got, why);
    check(/^\d+\.\d{2}$/.test(got), "fmtPct(" + input + ") 输出不是两位小数格式", got);
  }
  // 非有限值不该把 "NaN"/"Infinity" 渲染到状态栏上
  for (const bad of [NaN, Infinity, -Infinity, undefined, null]) {
    const got = fmtPct(bad);
    check(/^\d+\.\d{2}$/.test(got), "fmtPct(" + String(bad) + ") 输出不可渲染", String(got));
  }
  // 数值型入参也要能接住（调用侧可能给字符串）
  check(fmtPct("84.567") === "84.57", "fmtPct 未兼容字符串入参", String(fmtPct("84.567")));
}

// ---------- ② 渲染路径：必须经 fmtPct，且不得再拼裸 pct ----------
check(/平均命中[^"]*"\s*\+\s*fmtPct\(/.test(src),
      "状态栏「平均命中」段没有走 fmtPct（退回整数显示了？）");
check(!/"\s*\+\s*pct\s*\+\s*"%"/.test(src),
      "源码里仍有 `\" + pct + \"%\"`：绕过了 fmtPct，会渲染成整数百分比");
check(!/Math\.round\(\s*\(?\s*agg\.cache\s*\/\s*agg\.input\s*\)?\s*\*\s*100/.test(src),
      "pct 又被提前 Math.round 成整数了（两位小数会被抹平）");

// ---------- ③ 布局/刷新逻辑未被牵连 ----------
// 这两条是本次改动的边界：只动显示格式，不碰内容签名与降级优先级。
check(/agg\.rounds,\s*agg\.input,\s*agg\.cache,\s*agg\.output\]\.join/.test(src),
      "状态栏内容签名（决定是否重绘）被改动了，刷新逻辑应保持不变");
check(/for \(const drop of \[3, 1, 2\]\)/.test(src),
      "渐进降级顺序被改动了，状态栏布局应保持不变");

// ---------- 汇报 ----------
if (failures.length) {
  console.error("FAIL " + failures.length + " 项：");
  failures.forEach((f, i) => console.error("  " + (i + 1) + ". " + f));
  process.exit(1);
}
console.log("OK 平均命中率格式化冒烟通过（fmtPct 两位小数四舍五入 + 渲染路径未绕过）");
