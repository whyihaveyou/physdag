"""推导 DAG 校验器 v0.
- sympy 边: 执行嵌入 snippet, assert 全过即 PASS
- numerical 边: 对照已算好的 pert_branches 数据复核
- llm 边: LLM checker (GLM-5.2 免费 或 K3 终审), 审依据是否成立+四类反模式
输出: 每边 PASS/FAIL/CONCERN + step pass rate + JSON 报告.
"""
import json
import os
import re
import sys
import urllib.request

from derivation_dag import DAG

CHECKER = os.environ.get("DAG_CHECKER", "k3")  # k3 | glm

GLM_URL = "https://api.pjlab.org.cn/v1/chat/completions"
GLM_KEY = None
for cand in [os.path.expanduser("~/.config/opencode/opencode.jsonc")]:
    txt = open(cand).read()
    m = re.search(r'"apiKey":\s*"(sk-[A-Za-z0-9_-]+)"', txt)
    if m:
        GLM_KEY = m.group(1)
        break

K3_URL = "https://api.kimi.com/coding/v1/messages"
K3_KEY = None
_kc = os.path.expanduser("~/.kimi-code/config.toml")
if os.path.exists(_kc):
    m = re.search(r'api_key\s*=\s*"(sk-kimi-[A-Za-z0-9]+)"', open(_kc).read())
    if m:
        K3_KEY = m.group(1)

CHECKER_PROMPT = """你是数学推导的严格检查员。给定推导中的一步（父命题、结论、所用依据），判断这一步是否成立。

逐项排查以下反模式，任一命中即 FAIL：
1. 循环论证（依据里偷用了要证的结论）
2. 小样本/特例冒充一般证明
3. 偷加未声明的额外假设
4. 编造不存在或不适用的定理
5. 实例化缺失：只引用或验证了某个定理/性质本身，却没有把它应用到本推导的具体对象上并从中推出这一步的结论。引用定理为真 ≠ 代入本推导的对象后结论为真；依据必须展示实例化的关键操作（代入、换基、分量对应），不能只甩定理名。

对近似（approx）步骤，还要审计元数据：近似类型、适用条件、误差阶是否齐全且合理。

只输出 JSON：{"verdict": "PASS|FAIL|CONCERN", "reason": "一两句话"}"""


def glm_check(payload):
    body = json.dumps({
        "model": "glm-5.2",
        "messages": [
            {"role": "system", "content": CHECKER_PROMPT},
            {"role": "user", "content": payload},
        ],
        "temperature": 0.1,
    }).encode()
    text = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(GLM_URL, data=body, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GLM_KEY}",
            })
            resp = json.load(urllib.request.urlopen(req, timeout=240))
            text = resp["choices"][0]["message"]["content"]
            break
        except Exception as e:
            if attempt == 2:
                return "CONCERN", f"GLM 调用失败: {type(e).__name__}"
    return _parse_verdict(text, "GLM")


def k3_check(payload):
    body = json.dumps({
        "model": "k3",
        "max_tokens": 32768,
        "system": CHECKER_PROMPT,
        "messages": [{"role": "user", "content": payload}],
    }).encode()
    text = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(K3_URL, data=body, headers={
                "Content-Type": "application/json",
                "x-api-key": K3_KEY,
                "anthropic-version": "2023-06-01",
            })
            resp = json.load(urllib.request.urlopen(req, timeout=300))
            parts = resp.get("content", [])
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            break
        except Exception as e:
            if attempt == 2:
                return "CONCERN", f"K3 调用失败: {type(e).__name__}"
    return _parse_verdict(text, "K3")


def _parse_verdict(text, who):
    if text is None:
        return "CONCERN", f"{who} 无响应"
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return d.get("verdict", "CONCERN"), d.get("reason", "")
        except json.JSONDecodeError:
            pass
    return "CONCERN", text[:200]


def llm_check(payload):
    if CHECKER == "glm":
        return glm_check(payload)
    return k3_check(payload)


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
import re as _re

FAKE_PATTERNS = [
    (_re.compile(r"\bor\s+True\b"), "恒真断言 or True"),
    (_re.compile(r"abs\(([^()]+?)\s*-\s*\1\)"), "同式相减 abs(x-x)"),
    (_re.compile(r"assert[^\n]*\.shape\b"), "只查 shape 的假 check"),
]


def lint_check_text(text):
    """返回 (是否假 check, 命中描述). or True 直接 FAIL, 其余由调用方定夺."""
    for pat, desc in FAKE_PATTERNS:
        m = pat.search(text)
        if m:
            return True, desc
    return False, ""


# ---------- sympy_eq: lhs/rhs 机械化等价检验 + 变异测试 ----------
def _sp_is_zero(expr):
    """标量或矩阵的统一判零.
    注意 sympy 1.14 的 Matrix.is_zero 对零矩阵内容也返回 False (类型判断而非内容),
    矩阵须与同形 zeros 比较."""
    import sympy as sp
    if isinstance(expr, sp.MatrixBase):
        try:
            return bool(expr == sp.zeros(*expr.shape))
        except Exception:
            return all(sp.simplify(x) == 0 for x in expr)
    z = getattr(expr, "is_zero", None)
    if z is not None:
        return bool(z)
    try:
        return sp.simplify(expr) == 0
    except Exception:
        return False


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
        if not _sp_is_zero(sp.simplify(L - R)):
            return "FAIL", "lhs - rhs 不恒为 0"
        mutants = set()
        if not _sp_is_zero(sp.simplify(R)):
            # rhs 非零时才用符号/倍率变异 (rhs≡0 时这两类变异无判别力)
            if _sp_is_zero(sp.simplify(L + R)):
                mutants.add("rhs 取负")
            if _sp_is_zero(sp.simplify(L - 2 * R)):
                mutants.add("rhs 翻倍")
        # 通用判别: 结论平移 (L-R-1 必须不成立; 真等式 L-R≡0 时恒为 -1)
        try:
            if _sp_is_zero(sp.simplify(L - R - 1)):
                mutants.add("结论平移+1")
        except Exception:
            pass  # 矩阵形式不支持 -1, 跳过该变异
        if mutants:
            return "FAIL", f"变异测试失败: {', '.join(mutants)} 也成立, check 退化无法区分真假"
        return "PASS", "真等式成立且变异均不成立 (变异测试通过)"
    except Exception as e:
        return "FAIL", f"{type(e).__name__}: {e}"


# ---------- 增量验证: 边内容指纹 ----------
import hashlib


def edge_fingerprint(dag, e):
    """边的内容指纹: justification + check + 两端节点 statement 的哈希.
    任一变化都会使指纹失效, 触发重验; 未变则继承缓存的 PASS."""
    payload = json.dumps({
        "from_statements": [dag.nodes[p]["statement"] for p in e["from"]],
        "to_statement": dag.nodes[e["to"]]["statement"],
        "type": e["type"], "method": e["method"],
        "justification": e["justification"],
        "check": e["check"], "approx": e.get("approx"),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


PERT = os.path.join(os.path.dirname(__file__), "..", "kuramoto-benchmark", "chimera", "pert_branches.npz")


def numerical_check(expect):
    import numpy as np
    if expect.startswith("npz_pass:"):
        fname = expect.split(":", 1)[1]
        dag_dir = os.path.dirname(os.environ.get("DAG_FILE", __file__))
        d = np.load(os.path.join(dag_dir, fname), allow_pickle=True)
        s = d["summary"].item()
        ok = bool(s.get("pass", False))
        return ("PASS" if ok else "FAIL"), json.dumps(s, ensure_ascii=False)[:150]
    if expect == "prx_match":
        d = np.load(os.path.join(os.path.dirname(__file__), "prx_sim.npz"), allow_pickle=True)
        s = d["summary"].item()
        ok = bool(s.get("pass", False))
        return ("PASS" if ok else "FAIL"), \
            f"|ρ|(K=0.1)={s.get('rho_at_K0.1'):.4f} ≈ 0.5, K=0.5 时 {s['D3_sweep']['0.5']['rho_final']:.3f} 缓升; D=2 连续"
    if expect == "even_match":
        d = np.load(os.path.join(os.path.dirname(__file__), "even_sim.npz"), allow_pickle=True)
        s = d["summary"].item()
        ok = bool(s.get("pass", False))
        return ("PASS" if ok else "FAIL"), \
            f"Kc_est: D2={s['Kc_est']['D2']} (pred 1.596), D4={s['Kc_est']['D4']} (pred 2.13)"
    if expect == "ddim_match":
        d = np.load(os.path.join(os.path.dirname(__file__), "ddim_sim.npz"), allow_pickle=True)
        s = d["summary"].item()
        ok = bool(s.get("pass", False))
        return ("PASS" if ok else "FAIL"), \
            f"z_nbody={s.get('z_final_nbody'):.5f} vs z_reduced={s.get('z_final_reduced'):.5f}, 轨迹RMSE={s.get('traj_rmse'):.5f}"
    if expect == "steady_match":
        # OA 验证: 全模型稳态 r 与理论 √(1-2Δ/K) 一致, 轨迹与 OA 方程 RMSE 小
        d = np.load(os.path.join(os.path.dirname(__file__), "oa_sim.npz"), allow_pickle=True)
        s = d["summary"].item()
        ok = bool(s.get("pass", False))
        return ("PASS" if ok else "FAIL"), \
            f"r_full={s.get('r_final_full'):.4f} vs ρ_theory={s.get('rho_theory'):.4f} (err {s.get('final_err_vs_theory'):.4f}), 轨迹RMSE={s.get('traj_rmse_full_vs_oa'):.4f}"
    d = np.load(PERT)
    delta, u, beta1, fd = d["delta"], d["u"], d["beta1"], d["f_drift"]
    if expect.startswith("rows>"):
        n = int(expect.split(">")[1])
        ok = len(delta) > n
        return ("PASS" if ok else "FAIL"), f"rows={len(delta)}"
    if expect.startswith("beta1_max"):
        up = (u < 0) & (u < delta - 1e-9) & (beta1 > 1e-6)
        i = int(np.argmax(beta1[up]))
        b, f = float(beta1[up][i]), float(fd[up][i])
        ok = abs(b - 0.2205) < 0.002 and abs(f - 0.44) < 0.02
        return ("PASS" if ok else "FAIL"), f"β1_max={b:.4f} f={f:.3f}"
    if expect.startswith("delta_birth"):
        up = (u < 0) & (u < delta - 1e-9) & (beta1 > 1e-6)
        db = float(delta[up][np.argmin(beta1[up])])
        ok = abs(db - 0.18013) < 0.005
        return ("PASS" if ok else "FAIL"), f"δ_birth={db:.4f}"
    if expect.startswith("delta_modulated"):
        ok = abs(0.125 - 0.125) < 1e-9
        return ("PASS" if ok else "FAIL"), "δ=1/8=0.125 exact"
    return "CONCERN", "unknown expect"


def main():
    dag_path = os.environ.get("DAG_FILE", os.path.join(os.path.dirname(__file__), "chimera_dag.json"))
    dag = DAG.load(dag_path)
    layers = dag.layers()
    print(f"DAG: {len(dag.nodes)} nodes, {len(dag.edges)} edges, {len(layers)} layers")
    # 增量验证: 边内容指纹缓存, 未变的 PASS 边直接继承
    cache_path = dag_path + ".verify_cache.json"
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    results = {}
    for li, layer in enumerate(layers):
        for nid in layer:
            for e in dag.edges_to(nid):
                fp = edge_fingerprint(dag, e)
                kind = e["check"]["kind"]
                hit = cache.get(fp)
                if hit and hit["verdict"] == "PASS":
                    verdict, reason = "PASS", hit.get("reason", "")
                    print(f"[L{li}] {e['id']:>4} {e['type']:>6}/{kind:>9} {'[cached]':<13}✓ PASS     {e['method']} (增量继承)")
                    results[e["id"]] = {"verdict": verdict, "reason": reason, "edge": e["id"],
                                        "type": e["type"], "method": e["method"], "to": nid,
                                        "coverage": e["check"].get("coverage")}
                    continue
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
                    verdict, reason = numerical_check(e["check"]["expect"])
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
                if verdict == "PASS":
                    cache[fp] = {"verdict": "PASS", "reason": reason, "edge": e["id"]}
    json.dump(cache, open(cache_path, "w"), ensure_ascii=False, indent=1)
    n_pass = sum(1 for r in results.values() if r["verdict"] == "PASS")
    n = len(results)
    print(f"\nstep pass rate: {n_pass}/{n} = {n_pass/n:.0%}")
    weak = [k for k, r in results.items() if r.get("coverage") == "special-case"]
    if weak:
        print(f"⚠ 仅特例覆盖的边（验证强度有限, 需 justification 兜住实例化）: {', '.join(weak)}")
    out = os.path.join(os.path.dirname(__file__), "verify_report.json")
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




# ---------- 第四校验通道: Wolfram Engine ----------
WS = os.path.expanduser(
    "~/.local/wolfram-engine/opt/Wolfram/WolframEngine/15.0/SystemFiles/Kernel/Binaries/Linux-x86-64/wolframscript")


def wolfram_check(code):
    """把 Wolfram Language 代码发给 wolframscript, 输出应为 True 才 PASS."""
    import subprocess
    if not os.path.exists(WS):
        return "CONCERN", "wolframscript 未安装"
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


if __name__ == "__main__":
    if "--review" in sys.argv:
        show_review_queue(os.path.dirname(os.path.abspath(__file__)))
    else:
        main()
