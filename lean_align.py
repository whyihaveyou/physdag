"""陈述对齐门禁 (statement alignment gate) — 防"弱化命题骗过编译".

给定 DAG 节点的非形式命题 + 对应的 Lean 定理陈述, 用 LLM 审查形式化是否忠实:
量词覆盖 / 假设只减不增 / 结论强度 / 对象定义对应.

LLM 配置走 verify_dag 的环境变量 (DAG_LLM_API_KEY / DAG_LLM_BASE_URL /
DAG_LLM_MODEL / DAG_LLM_FORMAT). 登记文件里找不到定理时, 在 DAG_LEAN_DIR
(未设则为 lean.file 同目录) 下搜索所有 .lean 文件.

用法:
  python3 lean_align.py <dag.json> <edge_id>     # 审查一条边 (需 check.lean 已登记)
  python3 lean_align.py <dag.json> --all          # 批量审查该 DAG 所有登记边
  python3 lean_align.py --selftest                # 合成用例单元测试 (需配置 LLM)
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_dag import llm_call  # noqa: E402

ALIGN_PROMPT = """你是"陈述对齐"审查员。给定一个非形式数学命题（推导 DAG 节点）和它对应的 Lean 4 形式化定理陈述，判断形式化是否忠实——专门防范"为了让编译通过而弱化命题"。

逐项审查：
1. 量词覆盖：Lean 版作用域不得窄于原命题。原命题是"任意 n / 任意反对称 W / 任意 ε>0"而 Lean 版改成具体 n=3、具体矩阵、具体取值 → FAIL；原命题本身就是具体实例时，不要求推广。
2. 假设只减不增：Lean 版新增的每个前提（原命题没有的，如 K≠0、可逆、正性、光滑性）都是弱化 → FAIL；反过来，去掉前提而结论仍成立是加强，允许（在 note 注明）。
3. 结论强度：Lean 版结论不得弱于原命题——只证充分性丢必要性、不等号方向反转、绝对值/模长丢失、常数改变、符号约定不一致（如反对称定义 Wᵀ=−W vs Wᵀ=W）均为 FAIL。
4. 对象定义对应：Lean 里的定义（矩阵、内积、序参量、分布）必须与原命题的数学对象一一对应，不允许"同名不同物"。

只输出 JSON：{"verdict": "PASS|FAIL|CONCERN", "mismatches": ["..."], "note": "一句话"}"""


def extract_lean_statement(lean_file, theorem_name):
    """从 .lean 文件提取定理陈述 (theorem <name> ... 到首个 := 为止)."""
    src = open(os.path.expanduser(lean_file)).read()
    m = re.search(r"(?:theorem|lemma)\s+" + re.escape(theorem_name) + r"\b(.*?):=", src, re.S)
    if not m:
        return None
    stmt = m.group(0)
    return stmt[: stmt.rfind(":=")].strip()


def align_check(node_statement, lean_statement, context=""):
    payload = (f"非形式命题: {node_statement}\n\n"
               f"Lean 定理陈述:\n{lean_statement}\n")
    if context:
        payload += f"\n补充上下文: {context}\n"
    return _call_llm(payload)


def _call_llm(payload):
    """用对齐 prompt 调 LLM (复用 verify_dag.llm_call 的 env 配置), 解析 {verdict, mismatches, note}."""
    d = llm_call(ALIGN_PROMPT, payload, who="对齐审查")
    mism = d.get("mismatches") or []
    if isinstance(mism, str):
        mism = [mism]
    note = d.get("note", "")
    detail = ("; ".join(mism) + (" | " + note if note else "")).strip(" |")
    return d.get("verdict", "CONCERN"), detail or d.get("reason", "")


def check_edge(dag, edge):
    lean = edge.get("check", {}).get("lean")
    if not lean:
        return "CONCERN", "该边未登记 Lean 证明"
    node_map = {n["id"]: n for n in dag["nodes"]}
    to_id = edge["to"]
    node_stmt = node_map[to_id]["statement"]
    thms = [t.strip() for t in re.split(r"[,，]", lean["theorem"].split("(")[0])]
    parts, missing = [], []
    import glob
    lean_dir = os.environ.get("DAG_LEAN_DIR") or os.path.dirname(os.path.expanduser(lean["file"]))
    fallback = sorted(glob.glob(os.path.join(lean_dir, "*.lean")))
    for t in thms:
        if not t:
            continue
        s = extract_lean_statement(lean["file"], t)
        if s is None:  # 登记文件里找不到 -> 同目录搜索 (定理可能分布在多个批次文件)
            for f in fallback:
                s = extract_lean_statement(f, t)
                if s:
                    break
        (parts.append(s) if s else missing.append(t))
    if missing:
        return "CONCERN", f"定理未找到: {', '.join(missing)}"
    lean_text = "\n\n".join(parts)
    ctx = f"边 {edge['id']} 方法: {edge['method']}; 依据: {edge['justification'][:200]}"
    return align_check(node_stmt, lean_text, ctx)


def selftest():
    cases = [
        ("忠实(任意 n 版)", "对任意实数 K,g,w (K≠0, g−w/K≠0) 和实数 ρ: (K·g−w)·(g−w/K)⁻¹·ρ = K·ρ",
         "theorem t1 {K g w ρ : ℝ} (hK : K ≠ 0) (h : g - w/K ≠ 0) : (K*g - w) * (g - w/K)⁻¹ * ρ = K*ρ",
         "PASS"),
        ("量词弱化(任意n→n=3)", "对任意正整数 n 和任意实数 x: xⁿ ≥ 0 当 n 为偶数",
         "theorem t2 {x : ℝ} : x^3 ≥ 0",
         "FAIL"),
        ("新增假设(正性)", "对任意实数 x: |x| ≥ 0",
         "theorem t3 {x : ℝ} (hx : x > 0) : |x| ≥ 0",
         "FAIL"),
        ("结论符号翻转", "对任意实数 x,y: x ≤ y → x - y ≤ 0",
         "theorem t4 {x y : ℝ} (h : x ≤ y) : x - y ≥ 0",
         "FAIL"),
        ("加强(去假设)", "设 M 可逆, 则 M⁻¹ 的转置等于 Mᵀ 的逆",
         "theorem t5 {M : Matrix n n ℝ} : (M⁻¹)ᵀ = (Mᵀ)⁻¹",
         "PASS"),
        ("结论弱化(只证充分性)", "f(ρ)=0 当且仅当 ρ=0 或 ρ²=1−2D/K",
         "theorem t6 {ρ D K : ℝ} (h : ρ = 0 ∨ ρ^2 = 1 - 2*D/K) : f ρ = 0",
         "FAIL"),
    ]
    n_ok = 0
    for name, node, lean, expect in cases:
        verdict, reason = align_check(node, lean)
        ok = verdict == expect or (expect == "FAIL" and verdict == "FAIL")
        n_ok += ok
        print(f"[{'✓' if ok else '✗'}] {name}: 期望 {expect}, 实得 {verdict} -- {str(reason)[:80]}")
    print(f"\nselftest: {n_ok}/{len(cases)} 通过")
    return n_ok == len(cases)


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(0 if selftest() else 1)
    dag = json.load(open(args[0]))
    if "--all" in args:
        results = {}
        for e in dag["edges"]:
            if "lean" in e.get("check", {}):
                v, r = check_edge(dag, e)
                results[e["id"]] = v
                print(f"{e['id']:>4} {v:<8} {str(r)[:100]}")
        n_pass = sum(1 for v in results.values() if v == "PASS")
        print(f"\n对齐通过率: {n_pass}/{len(results)}")
    else:
        e = next(x for x in dag["edges"] if x["id"] == args[1])
        v, r = check_edge(dag, e)
        print(f"{args[1]}: {v} -- {r}")


if __name__ == "__main__":
    main()
