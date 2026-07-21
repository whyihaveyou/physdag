"""verify_dag 的端到端测试 (纯 stdlib, 直接 python3 tests/test_verifier.py 运行).

测试 ③④ 会把整个项目复制到临时目录再跑, 避免污染项目根的 review_queue.json.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import verify_dag as V  # noqa: E402

SYM = "a, b = sp.symbols('a b')"


def copy_project(dst):
    for f in ["derivation_dag.py", "verify_dag.py", "STANDARDS.md"]:
        shutil.copy(os.path.join(ROOT, f), dst)
    shutil.copytree(os.path.join(ROOT, "examples"), os.path.join(dst, "examples"))


def run_verifier(cwd, dag_rel):
    env = dict(os.environ, DAG_FILE=dag_rel)
    return subprocess.run([sys.executable, "verify_dag.py"], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=300)


def test_lint():
    """① lint 三种假 check 模式都能抓到."""
    fake, desc = V.lint_check_text("assert x > 0 or True")
    assert fake and "or True" in desc, desc
    fake, desc = V.lint_check_text("assert abs(rho - rho) < 1e-9")
    assert fake and "abs(x-x)" in desc, desc
    fake, desc = V.lint_check_text("assert y.shape == (3,)")
    assert fake and "shape" in desc, desc
    # 正常 check 不误报
    fake, _ = V.lint_check_text("assert sp.simplify((a+b)**2 - (a**2+2*a*b+b**2)) == 0")
    assert not fake
    print("✓ ① lint 三种假 check 模式")


def test_sympy_eq_mutation():
    """② sympy_eq: 真等式过, rhs 篡改 FAIL, 退化等式 FAIL."""
    v, r = V.sympy_eq_check(SYM, "(a+b)**2", "a**2 + 2*a*b + b**2")
    assert v == "PASS", r
    v, r = V.sympy_eq_check(SYM, "(a+b)**2", "a**2 + 3*a*b + b**2")
    assert v == "FAIL" and "不恒为 0" in r, (v, r)
    v, r = V.sympy_eq_check(SYM, "a - a", "0")
    assert v == "FAIL" and "变异测试" in r, (v, r)
    print("✓ ② sympy_eq 变异测试 (真过 / 篡改 FAIL / 退化 FAIL)")


def test_toy_dag_runs(tmp):
    """③ toy_dag 全跑通: pass rate 3/3, special-case 边进存疑库."""
    p = run_verifier(tmp, "examples/toy_dag.json")
    assert p.returncode == 0, p.stderr
    assert "step pass rate: 3/3 = 100%" in p.stdout, p.stdout
    assert "存疑库: 1 条待专家审核" in p.stdout, p.stdout
    queue = json.load(open(os.path.join(tmp, "review_queue.json")))
    assert len(queue) == 1 and queue[0]["edge"] == "e3" and queue[0]["status"] == "pending"
    print("✓ ③ toy_dag 3/3 PASS 且 special-case 边进存疑库")


def test_review_resolved(tmp):
    """④ 把 e3 的 special-case 改为 full 重跑, 存疑库条目标记 resolved."""
    dag_path = os.path.join(tmp, "examples", "toy_dag.json")
    spec = json.load(open(dag_path))
    e3 = next(e for e in spec["edges"] if e["id"] == "e3")
    e3["check"]["coverage"] = "full"
    json.dump(spec, open(dag_path, "w"), ensure_ascii=False, indent=1)
    p = run_verifier(tmp, "examples/toy_dag.json")
    assert p.returncode == 0, p.stderr
    assert "step pass rate: 3/3 = 100%" in p.stdout, p.stdout
    assert "待专家审核" not in p.stdout, p.stdout  # 不再有 pending
    queue = json.load(open(os.path.join(tmp, "review_queue.json")))
    e3q = next(q for q in queue if q["edge"] == "e3")
    assert e3q["status"] == "resolved", e3q
    print("✓ ④ coverage 改 full 重跑后存疑库条目 resolved")


def main():
    test_lint()
    test_sympy_eq_mutation()
    with tempfile.TemporaryDirectory() as tmp:
        copy_project(tmp)
        test_toy_dag_runs(tmp)
        test_review_resolved(tmp)
    print("\n全部 4 项测试通过")


if __name__ == "__main__":
    main()
