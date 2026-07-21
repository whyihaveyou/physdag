"""推导 DAG v0: 数据结构 + 拓扑分层."""
import json


class DAG:
    def __init__(self, spec):
        self.meta = spec.get("meta", {})
        self.nodes = {n["id"]: n for n in spec["nodes"]}
        self.edges = spec["edges"]
        for e in self.edges:
            for p in e["from"]:
                assert p in self.nodes, f"edge {e['id']}: unknown parent {p}"
            assert e["to"] in self.nodes, f"edge {e['id']}: unknown target {e['to']}"

    @classmethod
    def load(cls, path):
        return cls(json.load(open(path)))

    def layers(self):
        """拓扑分层: 每层节点的所有父边来源都在更前层."""
        deps = {nid: set() for nid in self.nodes}
        for e in self.edges:
            for p in e["from"]:
                deps[e["to"]].add(p)
        layers, done = [], set()
        while len(done) < len(self.nodes):
            layer = [nid for nid, d in deps.items() if d <= done and nid not in done]
            if not layer:
                raise ValueError("DAG has a cycle")
            layers.append(sorted(layer))
            done |= set(layer)
        return layers

    def edges_to(self, nid):
        return [e for e in self.edges if e["to"] == nid]

    def edge_from_text(self, e):
        parents = "；".join(f"[{p}] {self.nodes[p]['statement']}" for p in e["from"])
        return parents
