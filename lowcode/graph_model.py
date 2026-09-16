"""低代码节点图数据模型。

仿照 RS-232_lab_device 项目 lab_engine/core/setup_graph.py 的方法，
为 Superposed 16QAM 收发机低代码编程定制的轻量版：

- Port      : 节点端口（输入/输出 + 数据类型，用于连线匹配）
- Node      : 节点（类型、位置、参数）
- Edge      : 连线（输出端口 -> 输入端口，传递变量）
- FlowGraph : 节点 + 连线的集合，负责 JSON 序列化与基础校验
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json
import uuid


# 节点默认尺寸（像素，逻辑坐标）
NODE_WIDTH = 170
NODE_HEIGHT = 80
PORT_RADIUS = 6


@dataclass
class Port:
    """节点端口。"""
    name: str
    label: str
    direction: str          # "input" | "output"
    data_type: str = "any"  # 用于连线匹配（"any" 匹配所有类型）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "direction": self.direction,
            "data_type": self.data_type,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Port":
        return cls(
            name=d["name"],
            label=d.get("label", d["name"]),
            direction=d["direction"],
            data_type=d.get("data_type", "any"),
        )


@dataclass
class Node:
    """流程图节点。node_type 对应 nodes.py 中注册的功能类型。"""
    node_id: str
    node_type: str
    x: float = 100.0
    y: float = 100.0
    label: str = ""
    data: Dict[str, Any] = field(default_factory=dict)   # data["params"] 存参数
    ports: List[Port] = field(default_factory=list)

    def __post_init__(self):
        if not self.label:
            self.label = self.node_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.node_id,
            "type": self.node_type,
            "x": self.x,
            "y": self.y,
            "label": self.label,
            "data": self.data,
            "ports": [p.to_dict() for p in self.ports],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Node":
        return cls(
            node_id=d["id"],
            node_type=d["type"],
            x=d.get("x", 100.0),
            y=d.get("y", 100.0),
            label=d.get("label", ""),
            data=dict(d.get("data", {})),
            ports=[Port.from_dict(p) for p in d.get("ports", [])],
        )

    def port(self, name: str) -> Optional[Port]:
        for p in self.ports:
            if p.name == name:
                return p
        return None


@dataclass
class Edge:
    """流程图连线：把一个输出端口的变量传递给一个输入端口。"""
    edge_id: str
    source_node: str
    source_port: str
    target_node: str
    target_port: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.edge_id,
            "source_node": self.source_node,
            "source_port": self.source_port,
            "target_node": self.target_node,
            "target_port": self.target_port,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Edge":
        return cls(
            edge_id=d["id"],
            source_node=d["source_node"],
            source_port=d["source_port"],
            target_node=d["target_node"],
            target_port=d["target_port"],
        )


def port_type_compatible(src_type: str, dst_type: str) -> bool:
    """判断输出端口类型能否连到输入端口类型。"""
    return src_type == dst_type or src_type == "any" or dst_type == "any"


class FlowGraph:
    """流程图：节点 + 连线的集合。"""

    FILE_SUFFIX = ".spflow.json"

    def __init__(self):
        self.nodes: Dict[str, Node] = {}
        self.edges: Dict[str, Edge] = {}

    # ------------------------------------------------------------------
    # 节点操作
    # ------------------------------------------------------------------
    def add_node(self, node_type: str, x: float, y: float,
                 label: Optional[str] = None,
                 data: Optional[Dict[str, Any]] = None,
                 ports: Optional[List[Port]] = None,
                 node_id: Optional[str] = None) -> Node:
        node_id = node_id or _new_id("n")
        node = Node(
            node_id=node_id,
            node_type=node_type,
            x=x,
            y=y,
            label=label or node_type,
            data=data or {},
            ports=ports or [],
        )
        self.nodes[node_id] = node
        return node

    def remove_node(self, node_id: str) -> None:
        """删除节点及其所有连线。"""
        if node_id in self.nodes:
            del self.nodes[node_id]
        for eid in [eid for eid, e in self.edges.items()
                    if e.source_node == node_id or e.target_node == node_id]:
            del self.edges[eid]

    def get_node(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(node_id)

    # ------------------------------------------------------------------
    # 状态覆盖（执行引擎/UI 同步运行状态）
    # ------------------------------------------------------------------
    def set_node_status(self, node_id: str, status: str, message: str = "") -> None:
        """status: "normal" | "warning" | "error" | "running" | "done"
        """
        node = self.nodes.get(node_id)
        if node is not None:
            node.data["_status"] = status
            node.data["_status_msg"] = message

    def clear_node_status(self, node_id: Optional[str] = None) -> None:
        nodes = [self.nodes[node_id]] if node_id else list(self.nodes.values())
        for node in nodes:
            node.data.pop("_status", None)
            node.data.pop("_status_msg", None)

    # ------------------------------------------------------------------
    # 边操作
    # ------------------------------------------------------------------
    def add_edge(self, source_node: str, source_port: str,
                 target_node: str, target_port: str,
                 edge_id: Optional[str] = None,
                 allow_multi_input: bool = False,
                 strict_types: bool = False) -> Optional[Edge]:
        """添加一条边，连接非法则返回 None。"""
        src = self.get_node(source_node)
        dst = self.get_node(target_node)
        if src is None or dst is None:
            return None
        src_port = src.port(source_port)
        dst_port = dst.port(target_port)
        if src_port is None or dst_port is None:
            return None
        if src_port.direction != "output" or dst_port.direction != "input":
            return None
        if strict_types and not port_type_compatible(src_port.data_type,
                                                     dst_port.data_type):
            return None

        # 同一 input 端口只允许一条边（一个变量来源）
        if not allow_multi_input:
            for e in self.edges.values():
                if e.target_node == target_node and e.target_port == target_port:
                    return None

        edge = Edge(
            edge_id=edge_id or _new_id("e"),
            source_node=source_node,
            source_port=source_port,
            target_node=target_node,
            target_port=target_port,
        )
        self.edges[edge.edge_id] = edge
        return edge

    def remove_edge(self, edge_id: str) -> None:
        if edge_id in self.edges:
            del self.edges[edge_id]

    def edges_connected_to_node(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges.values()
                if e.source_node == node_id or e.target_node == node_id]

    def get_source(self, node_id: str, port_name: str) -> Optional[Tuple[Node, Port]]:
        """获取某个 input 端口的来源节点/端口。"""
        for e in self.edges.values():
            if e.target_node == node_id and e.target_port == port_name:
                src = self.get_node(e.source_node)
                src_port = src.port(e.source_port) if src else None
                if src and src_port:
                    return src, src_port
        return None

    def get_incoming(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges.values() if e.target_node == node_id]

    def get_outgoing(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges.values() if e.source_node == node_id]

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "kind": "sp_lowcode_flow",
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges.values()],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FlowGraph":
        graph = cls()
        for nd in d.get("nodes", []):
            node = Node.from_dict(nd)
            node.data.pop("_status", None)
            node.data.pop("_status_msg", None)
            graph.nodes[node.node_id] = node
        for ed in d.get("edges", []):
            edge = Edge.from_dict(ed)
            graph.edges[edge.edge_id] = edge
        return graph

    def save(self, path: Path) -> None:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "FlowGraph":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def validate(self) -> List[str]:
        """返回错误信息列表；空列表表示通过。"""
        errors = []
        for e in self.edges.values():
            src = self.get_node(e.source_node)
            dst = self.get_node(e.target_node)
            if src is None or dst is None:
                errors.append(f"连线 {e.edge_id} 的端点节点不存在")
                continue
            if src.port(e.source_port) is None or dst.port(e.target_port) is None:
                errors.append(f"{dst.label}: 端口 {e.target_port} 不存在")
        if self.has_cycle():
            errors.append("流程图存在环路（当前执行引擎只支持有向无环图）")
        return errors

    def has_cycle(self) -> bool:
        return _has_cycle(self)


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------
def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _has_cycle(graph: FlowGraph) -> bool:
    indeg = {nid: 0 for nid in graph.nodes}
    for e in graph.edges.values():
        if e.target_node in indeg and e.source_node in indeg:
            indeg[e.target_node] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        nid = queue.pop()
        seen += 1
        for e in graph.get_outgoing(nid):
            if e.target_node not in indeg:
                continue
            indeg[e.target_node] -= 1
            if indeg[e.target_node] == 0:
                queue.append(e.target_node)
    return seen != len(graph.nodes)
