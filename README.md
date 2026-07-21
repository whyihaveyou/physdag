# ProofDAG

**把物理/数学推导变成一张可机器验证的有向无环图（DAG），用多条独立通道逐边检验，让"验证推导"不再有漏洞可钻。**

[方法论](docs/methodology.md) · [编写与校验标准](STANDARDS.md)

## 这是什么

一段论文推导（自然语言+公式）进来，ProofDAG 把它编码成 DAG：

- **节点** = 命题（axiom / definition / claim）
- **边** = 推导步骤，带 `type`（exact/approx）、`method`、`justification`、`check` 四个字段；approx 边带近似元数据（类型/适用条件/误差阶）

然后逐边机器检验：

| 通道 | 干什么 | 配置 |
|------|--------|------|
| **SymPy** | 符号代数恒等式 | 开箱即用 |
| **sympy_eq** | lhs/rhs 机械化等价 + **变异测试** | 开箱即用 |
| **Wolfram** | 结构定理、极限、特殊函数 | 需 wolframscript（Wolfram Engine 免费版即可） |
| **LLM 评审** | 物理论证与近似（带反模式审计） | 需 `DAG_LLM_API_KEY` |
| **数值** | 对照模拟数据复核 | npz 数据文件 |
| **Lean** | 全称命题终审（任意 n、任意参数） | Lean 4 + mathlib |

## 为什么不是"跑个 SymPy 就完了"

因为验证系统会糊弄自己，而且方式很有规律。ProofDAG 内置三道防线（详见[方法论](docs/methodology.md)）：

1. **变异测试**——把结论改错一个符号，check 必须失败；不炸的就是假 check（`or True`、`abs(x-x)`、shape-only 静态扫描直接判死）
2. **coverage 标注**——每条边强制标注 `full / sampled / special-case`，特例冒充不了全验，校验器每轮单列弱覆盖边
3. **陈述对齐门禁**——Lean 定理登记前，必须过 `lean_align.py` 四向审查（量词覆盖/假设只减不增/结论强度/对象对应），防"翻译时偷弱命题骗过编译"

外加**存疑库**：FAIL / CONCERN / special-case 的边自动进入 `review_queue.json`，由专家人工审核；修复通过自动出库，`--review` 随时查看。

## 开箱即用

```bash
# 依赖：Python 3.10+，sympy，numpy（LLM/Wolfram/Lean 通道可选）
pip install sympy numpy

# 跑示例链
DAG_FILE=examples/toy_dag.json python3 verify_dag.py

# 查看存疑库
python3 verify_dag.py --review

# 跑测试
python3 tests/test_verifier.py
```

## 写自己的推导 DAG

```json
{
  "meta": {"title": "我的推导链"},
  "nodes": [
    {"id": "N0", "kind": "axiom", "statement": "模型定义 ..."},
    {"id": "N1", "kind": "claim", "statement": "中间结论 ..."}
  ],
  "edges": [
    {"id": "E1", "from": ["N0"], "to": "N1", "type": "exact",
     "method": "代入求解",
     "justification": "这一步为什么成立的完整论证 ...",
     "check": {"kind": "sympy_eq", "coverage": "full",
                "symbols": "a, b = sp.symbols('a b', real=True)",
                "lhs": "(a+b)**2", "rhs": "a**2 + 2*a*b + b**2"}}
  ]
}
```

LLM 通道配置（任一 OpenAI/Anthropic 兼容端点均可）：

```bash
export DAG_LLM_API_KEY=sk-...
export DAG_LLM_BASE_URL=https://api.kimi.com/coding/v1/messages   # 默认值
export DAG_LLM_MODEL=k3                                           # 默认值
export DAG_LLM_FORMAT=anthropic   # 或 openai
```

Wolfram 通道：`export DAG_WOLFRAMSCRIPT=/path/to/wolframscript`（或放入 PATH）。

## 核心概念速查

- **实例化检验**：check 的对象必须是"N_i 的表达式 ⟹ N_{i+1} 的表达式"这条边本身，而不是被引用的定理——定理为真 ≠ 代入我们的对象后结论为真
- **变异测试**：sympy_eq 自动把 rhs 取负/翻倍，变异也成立的 check 判退化 FAIL
- **coverage**：`full`（符号证明全覆盖）/ `sampled`（数值采样）/ `special-case`（特例，需 note 兜底）
- **LLM 反模式**：循环论证 / 特例冒充 / 偷加假设 / 编造定理 / **实例化缺失**（只甩定理名没展示代入操作）
- **Lean 路由**：每条边先 1 分钟探库（mathlib grep），按"有引理 4-15 分钟 / 要组装 30-90 分钟 / 没定理立项"三档报价——计算能搞定的边永远不去 Lean，物理判断永远不去 Lean

## 项目结构

```
├── derivation_dag.py   # DAG 数据结构与拓扑分层
├── verify_dag.py       # 四通道校验器 + coverage + lint + 变异测试 + 存疑库
├── lean_align.py       # 陈述对齐门禁（Lean 陈述 vs 原命题）
├── STANDARDS.md        # 编写与校验标准（防复发规则全集）
├── examples/           # 示例推导链
├── tests/              # 单元测试（纯 stdlib assert）
└── docs/methodology.md # 方法论：三个真问题与三道防线
```

## 出身

ProofDAG 从五条真实理论物理推导链（chimera 精确解 PRL 2004、Ott-Antonsen
降维 Chaos 2008、D 维复杂度约化 Chaos 2019、PRX 2019 奇/偶 D 相变理论）
的机器验证中打磨出来。它抓到过：论文复现中的 Z/Z* 系统性交换错误、
缺失的推导边、被弱化的形式化命题、以及若干个"看起来像验证"的假 check。

## License

MIT
