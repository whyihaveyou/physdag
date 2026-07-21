"""推导 DAG 校验器 v0.
- sympy 边: 执行嵌入 snippet, assert 全过即 PASS
- sympy_eq 边: 机械构造 lhs-rhs==0 + 变异测试 (rhs 取负/翻倍必须不成立)
- numerical 边: 对照 npz 数值数据复核 (expect 形如 npz_pass:<文件名>, 与 DAG 文件同目录)
- llm 边: LLM checker 审依据是否成立 + 反模式扫描
输出: 每边 PASS/FAIL/CONCERN + step pass rate + JSON 报告 + 存疑库.

LLM 配置全部走环境变量:
  DAG_LLM_API_KEY   (必需; 未配置则 llm 边返回 CONCERN)
  DAG_LLM_BASE_URL  (默认 https://api.kimi.com/coding/v1/messages)
  DAG_LLM_MODEL     (默认 k3)
  DAG_LLM_FORMAT    (anthropic | openai, 默认 anthropic;
                     openai 走 chat/completions 兼容端点, BASE_URL 需含完整路径)
Wolfram:
  DAG_WOLFRAMSCRIPT (wolframscript 路径; 未设则从 PATH 查找)
DAG 文件:
  DAG_FILE          (默认 ./dag.json)
"""
import json
import os
import re
import shutil
import sys
import urllib.request

from derivation_dag import DAG

LLM_API_KEY = os.environ.get("DAG_LLM_API_KEY")
LLM_BASE_URL = os.environ.get("DAG_LLM_BASE_URL", "https://api.kimi.com/coding/v1/messages")
LLM_MODEL = os.environ.get("DAG_LLM_MODEL", "k3")
LLM_FORMAT = os.environ.get("DAG_LLM_FORMAT", "anthropic")  # anthropic | openai

CHECKER_PROMPT = """你是数学推导的严格检查员。给定推导中的一步（父命题、结论、所用依据），判断这一步是否成立。

逐项排查以下反模式，任一命中即 FAIL：
1. 循环论证（依据里偷用了要证的结论）
2. 小样本/特例冒充一般证明
3. 偷加未声明的额外假设
4. 编造不存在或不适用的定理
5. 实例化缺失：只引用或验证了某个定理/性质本身，却没有把它应用到本推导的具体对象上并从中推出这一步的结论。引用定理为真 ≠ 代入本推导的对象后结论为真；依据必须展示实例化的关键操作（代入、换基、分量对应），不能只甩定理名。

对近似（approx）步骤，还要审计元数据：近似类型、适用条件、误差阶是否齐全且合理。

只输出 JSON：{"verdict": "PASS|FAIL|CONCERN", "reason": "一两句话"}"""


def llm_call(system_prompt, payload, who="LLM"):
    """按 DAG_LLM_FORMAT 分派请求格式调 LLM checker, 3 次重试.
    返回解析后的 dict (至少含 verdict); 任何失败返回 {"verdict": "CONCERN", "reason": ...}."""
    if not LLM_API_KEY:
        return {"verdict": "CONCERN", "reason": "未配置 DAG_LLM_API_KEY"}
    if LLM_FORMAT == "openai":
        body = json.dumps({
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload},
            ],
            "temperature": 0.1,
        }).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        }
    else:  # anthropic
        body = json.dumps({
            "model": LLM_MODEL,
            "max_tokens": 32768,
            "system": system_prompt,
            "messages": [{"role": "user", "content": payload}],
        }).encode()
        headers = {
            "Content-Type": "application/json",
            "x-api-key": LLM_API_KEY,
            "anthropic-version": "2023-06-01",
        }
    text = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(LLM_BASE_URL, data=body, headers=headers)
            resp = json.load(urllib.request.urlopen(req, timeout=300))
            if LLM_FORMAT == "openai":
                text = resp["choices"][0]["message"]["content"]
            else:
                parts = resp.get("content", [])
                text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            break
        except Exception as e:
            if attempt == 2:
                return {"verdict": "CONCERN", "reason": f"{who} 调用失败: {type(e).__name__}"}
    if text is None:
        return {"verdict": "CONCERN", "reason": f"{who} 无响应"}
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            d.setdefault("verdict", "CONCERN")
            return d
        except json.JSONDecodeError:
            pass
    return {"verdict": "CONCERN", "reason": text[:200]}


def llm_check(payload):
    d = llm_call(CHECKER_PROMPT, payload, who="checker")
    return d.get("verdict", "CONCERN"), d.get("reason", "")


def sympy_check(snippet):
    env = {}
    try:
        exec(snippet, env)
        return "PASS", ""
    except AssertionError as e:
        return "FAIL", f"assert failed: {e}"
    except Exception as e:
        return "FAIL", f"{type(e).__name__}: {e}"


# ---------- 假 check 静态扫描 (STANDARDS.md 零容忍) ----------
FAKE_PATTERNS = [
    (re.compile(r"\bor\s+True\b"), "恒真断言 or True"),
    (re.compile(r"abs\(([^()]+?)\s*-\s*\1\)"), "同式相减 abs(x-x)"),
    (re.compile(r"assert[^\n]*\.shape\b"), "只查 shape 的假 check"),
]


def lint_check_text(text):
    """返回 (是否假 check, 命中描述). or True 直接 FAIL, 其余由调用方定夺."""
    for pat, desc in FAKE_PATTERNS:
        m = pat.search(text)
        if m:
            return True, desc
    return False, ""


# ---------- sympy_eq: lhs/rhs 机械化等价检验 + 变异测试 ----------
def sympy_eq_check(symbols, lhs, rhs):
    """机械构造 lhs-rhs==0 检验; 随后变异测试: rhs 取负/翻倍必须不成立,
    否则 check 无法区分真假结论 (退化), 判 FAIL."""
    import sympy as sp
    env = {"sp": sp}
    try:
        if symbols:
            exec(symbols, env)
        L = eval(lhs, env)
        R = eval(rhs, env)
        if sp.simplify(L - R) != 0:
            return "FAIL", "lhs - rhs 不恒为 0"
        mutants = set()
        if sp.simplify(L + R) == 0:
            mutants.add("rhs 取负")
        if sp.simplify(L - 2 * R) == 0:
            mutants.add("rhs 翻倍")
        if mutants:
            return "FAIL", f"变异测试失败: {', '.join(mutants)} 也成立, check 退化无法区分真假"
        return "PASS", "真等式成立且两种变异均不成立 (变异测试通过)"
    except Exception as e:
        return "FAIL", f"{type(e).__name__}: {e}"


# ---------- numerical: 对照 npz 数值数据 ----------
def numerical_check(expect, base_dir):
    """expect 形如 npz_pass:<文件名>: 读 DAG 文件同目录的 npz, summary["pass"] 为真则 PASS."""
    if expect.startswith("npz_pass:"):
        fname = expect.split(":", 1)[1]
        path = os.path.join(base_dir, fname)
        if not os.path.exists(path):
            return "CONCERN", f"数值数据缺失: {fname} (应与 DAG 文件同目录)"
        import numpy as np
        s = np.load(path, allow_pickle=True)["summary"].item()
        ok = bool(s.get("pass", False))
        return ("PASS" if ok else "FAIL"), f"summary.pass={ok}"
    return "CONCERN", "unknown expect (仅支持 npz_pass:<文件名>)"


# ---------- 第四校验通道: Wolfram Engine ----------
WS = os.environ.get("DAG_WOLFRAMSCRIPT") or shutil.which("wolframscript")


def wolfram_check(code):
    """把 Wolfram Language 代码发给 wolframscript, 输出应为 True 才 PASS."""
    import subprocess
    if not WS or not os.path.exists(WS):
        return "CONCERN", "wolframscript 未安装 (可设 DAG_WOLFRAMSCRIPT 或加入 PATH)"
    try:
        out = subprocess.run([WS, "-code", code], capture_output=True, text=True, timeout=300)
        stdout = out.stdout.strip()
        if "not activated" in stdout or "not activated" in out.stderr:
            return "CONCERN", "Wolfram Engine 未激活（需 wolframscript -activate 一次）"
        if stdout.endswith("True"):
            return "PASS", stdout[-200:] if len(stdout) > 200 else ""
        return "FAIL", f"输出非 True: {stdout[:150]}"
    except Exception as e:
        return "CONCERN", f"wolframscript 调用失败: {type(e).__name__}"


def main():
    dag_path = os.environ.get("DAG_FILE", os.path.join(os.path.dirname(__file__), "dag.json"))
    if not os.path.exists(dag_path):
        print(f"DAG 文件不存在: {dag_path} (用 DAG_FILE 环境变量指定)")
        sys.exit(2)
    dag = DAG.load(dag_path)
    dag_dir = os.path.dirname(os.path.abspath(dag_path))
    layers = dag.layers()
    print(f"DAG: {len(dag.nodes)} nodes, {len(dag.edges)} edges, {len(layers)} layers")
    results = {}
    for li, layer in enumerate(layers):
        for nid in layer:
            for e in dag.edges_to(nid):
                kind = e["check"]["kind"]
                # 假 check 静态扫描: or True 直接 FAIL, 其余形态 CONCERN
                raw_text = e["check"].get("snippet") or e["check"].get("code") or ""
                is_fake, fake_desc = lint_check_text(raw_text)
                if is_fake and "or True" in fake_desc:
                    verdict, reason = "FAIL", f"假 check (静态扫描): {fake_desc}"
                elif kind == "sympy":
                    verdict, reason = sympy_check(e["check"]["snippet"])
                elif kind == "sympy_eq":
                    verdict, reason = sympy_eq_check(e["check"].get("symbols"),
                                                     e["check"]["lhs"], e["check"]["rhs"])
                elif kind == "numerical":
                    verdict, reason = numerical_check(e["check"]["expect"], dag_dir)
                elif kind == "wolfram":
                    verdict, reason = wolfram_check(e["check"]["code"])
                elif kind == "llm":
                    payload = (f"父命题: {dag.edge_from_text(e)}\n结论 [{nid}]: {dag.nodes[nid]['statement']}\n"
                               f"依据 ({e['method']}): {e['justification']}\n")
                    if e["type"] == "approx":
                        payload += f"近似元数据: {json.dumps(e['approx'], ensure_ascii=False)}\n"
                    verdict, reason = llm_check(payload)
                else:
                    verdict, reason = "CONCERN", "unknown check kind"
                if is_fake and verdict == "PASS":
                    verdict, reason = "CONCERN", f"疑似假 check (静态扫描): {fake_desc}"
                tag = {"PASS": "✓", "FAIL": "✗", "CONCERN": "?"}.get(verdict, "?")
                cov = e["check"].get("coverage")  # full | sampled | special-case (llm 边免标)
                if kind != "llm" and not cov:
                    print(f"  !! {e['id']} 缺 coverage 标注 (full/sampled/special-case)")
                cov_s = f"[{cov}]" if cov else ""
                print(f"[L{li}] {e['id']:>4} {e['type']:>6}/{kind:>9} {cov_s:<13}{tag} {verdict:<8} {e['method']}"
                      + (f"  -- {reason[:90]}" if reason else ""))
                results[e["id"]] = {"verdict": verdict, "reason": reason, "edge": e["id"],
                                    "type": e["type"], "method": e["method"], "to": nid,
                                    "coverage": cov}
    n_pass = sum(1 for r in results.values() if r["verdict"] == "PASS")
    n = len(results)
    print(f"\nstep pass rate: {n_pass}/{n} = {n_pass/n:.0%}")
    weak = [k for k, r in results.items() if r.get("coverage") == "special-case"]
    if weak:
        print(f"⚠ 仅特例覆盖的边（验证强度有限, 需 justification 兜住实例化）: {', '.join(weak)}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_report.json")
    json.dump({"dag": dag.meta["title"], "pass_rate": n_pass / n, "results": results},
              open(out, "w"), ensure_ascii=False, indent=1)
    print("saved", out)
    n_pending = update_review_queue(dag, results, os.path.dirname(os.path.abspath(__file__)))
    if n_pending:
        print(f"📋 存疑库: {n_pending} 条待专家审核 (review_queue.json)")


# ---------- 存疑库 (review queue): 存疑边汇总, 供专家人工审核 ----------
QUEUE_FILE = "review_queue.json"


def update_review_queue(dag, results, base_dir):
    """把 FAIL/CONCERN 或 special-case 覆盖的边写入存疑库; 全过且 full 覆盖的既往条目
    自动标记 resolved。返回待审条目数。"""
    import datetime
    path = os.path.join(base_dir, QUEUE_FILE)
    queue = json.load(open(path)) if os.path.exists(path) else []
    idx = {(q["dag"], q["edge"]): q for q in queue}
    title = dag.meta.get("title", "")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for eid, r in results.items():
        suspect = r["verdict"] in ("FAIL", "CONCERN") or r.get("coverage") == "special-case"
        key = (title, eid)
        if suspect:
            entry = {"dag": title, "edge": eid, "to": r["to"], "method": r["method"],
                     "type": r["type"], "verdict": r["verdict"], "coverage": r.get("coverage"),
                     "reason": r.get("reason", ""), "updated": now, "status": "pending"}
            if key in idx:
                idx[key].update(entry)
            else:
                entry["created"] = now
                queue.append(entry)
                idx[key] = entry
        elif key in idx and idx[key]["status"] == "pending" and r["verdict"] == "PASS" \
                and r.get("coverage") in (None, "full"):
            idx[key]["status"] = "resolved"
            idx[key]["updated"] = now
    json.dump(queue, open(path, "w"), ensure_ascii=False, indent=1)
    return sum(1 for q in queue if q["status"] == "pending")


def show_review_queue(base_dir):
    path = os.path.join(base_dir, QUEUE_FILE)
    if not os.path.exists(path):
        print("存疑库为空")
        return
    queue = json.load(open(path))
    pending = [q for q in queue if q["status"] == "pending"]
    print(f"存疑库: {len(pending)} 条待审 / {len(queue)} 条总计\n")
    for q in pending:
        print(f"[{q['dag'][:30]}] {q['edge']} ({q['type']}/{q.get('coverage') or '-'}) {q['verdict']}"
              f"  {q['method']}\n    {q['reason'][:120]}\n    updated {q['updated']}")


if __name__ == "__main__":
    if "--review" in sys.argv:
        show_review_queue(os.path.dirname(os.path.abspath(__file__)))
    else:
        main()
