"""低代码流程执行引擎。

按拓扑排序依次执行节点：
- 每个节点的输入变量来自连到其输入端口的上游节点输出
- 节点输出按端口名缓存，供下游节点取用
- 支持日志回调、节点状态回调、停止事件

只支持有向无环图（DAG）；有环时直接报错。
"""
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import config

from lowcode.graph_model import FlowGraph
from lowcode.nodes import NODES, NodeDef, get_node_def


class FlowError(Exception):
    """流程执行错误（携带节点信息）。"""

    def __init__(self, message: str, node_id: Optional[str] = None):
        super().__init__(message)
        self.node_id = node_id


class ExecContext:
    """节点执行上下文：日志、运行目录、停止标志、弹窗回调。"""

    def __init__(self,
                 run_dir: Path,
                 log: Optional[Callable[[str, str], None]] = None,
                 stop_event: Optional[threading.Event] = None,
                 popup: Optional[Callable[[str, str], None]] = None):
        self.run_dir = Path(run_dir)
        self._log = log
        self._popup = popup
        self._stop_event = stop_event or threading.Event()

    def log(self, text: str, level: str = "info") -> None:
        if self._log is not None:
            self._log(str(text), level)
        else:
            print(text)

    def popup(self, title: str, text: str) -> None:
        """变量内容等详细信息走弹窗；无弹窗回调时（CLI/测试）回退到日志。"""
        if self._popup is not None:
            self._popup(str(title), str(text))
        else:
            self.log(f"{title}: {text}")

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()


def make_run_dir(base: Optional[Path] = None) -> Path:
    """创建本次运行目录：data/lowcode_runs/<时间戳>/。"""
    base = Path(base) if base else config.DATA_DIR / "lowcode_runs"
    run_dir = base / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def topological_order(graph: FlowGraph) -> List[str]:
    """Kahn 拓扑排序；有环时抛 FlowError。"""
    indeg = {nid: 0 for nid in graph.nodes}
    for e in graph.edges.values():
        if e.target_node in indeg and e.source_node in indeg:
            indeg[e.target_node] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    order: List[str] = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for e in graph.get_outgoing(nid):
            if e.target_node not in indeg:
                continue
            indeg[e.target_node] -= 1
            if indeg[e.target_node] == 0:
                queue.append(e.target_node)
    if len(order) != len(graph.nodes):
        raise FlowError("流程图存在环路，无法确定执行顺序")
    return order


def resolve_params(node_def: NodeDef, node_data: Dict[str, Any]) -> Dict[str, Any]:
    """合并参数默认值与节点上保存的参数。"""
    params = {p.name: p.default for p in node_def.params}
    params.update(node_data.get("params", {}))
    return params


def run_graph(graph: FlowGraph,
              log: Optional[Callable[[str, str], None]] = None,
              status: Optional[Callable[[str, str, str], None]] = None,
              stop_event: Optional[threading.Event] = None,
              run_dir: Optional[Path] = None,
              popup: Optional[Callable[[str, str], None]] = None) -> Dict[str, Dict[str, Any]]:
    """执行整个流程图。

    Args:
        graph: 流程图
        log: 日志回调 log(text, level)
        status: 节点状态回调 status(node_id, state, message)，
                state ∈ "running" | "done" | "error" | "skipped"
        stop_event: 置位后在下一个节点开始前停止
        run_dir: 运行目录（画图/保存节点把文件写到这里），缺省自动创建
        popup: 详情弹窗回调 popup(title, text)，缺省时详细内容回退到日志

    Returns:
        {node_id: {输出端口名: 值}}
    """
    if run_dir is None:
        run_dir = make_run_dir()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = ExecContext(run_dir, log=log, stop_event=stop_event, popup=popup)

    errors = graph.validate()
    if errors:
        raise FlowError("流程图校验失败:\n" + "\n".join(f"  - {e}" for e in errors))

    order = topological_order(graph)
    ctx.log(f"运行目录: {run_dir}")
    ctx.log(f"共 {len(order)} 个节点，开始执行")

    results: Dict[str, Dict[str, Any]] = {}
    failed = False
    for nid in order:
        node = graph.get_node(nid)
        node_def = get_node_def(node.node_type) if node else None
        label = node.label if node else nid

        if node_def is None:
            _notify(status, nid, "error", f"未知节点类型: {node.node_type}")
            raise FlowError(f"节点 {label} 类型未注册: {node.node_type}", node_id=nid)

        if node_def.func is None or not node_def.outputs:
            # 注释节点或无输出节点（无实质计算）
            _notify(status, nid, "skipped", "")
            results[nid] = {}
            continue

        if node.data.get("params", {}).get("enabled") is False:
            # 节点被禁用（如模板中的硬件节点）：跳过执行
            _notify(status, nid, "skipped", "已禁用")
            ctx.log(f"[{label}] 已禁用，跳过")
            results[nid] = {}
            continue

        if ctx.is_stopped():
            ctx.log("用户请求停止", level="warning")
            _notify(status, nid, "skipped", "已停止")
            break

        # 收集输入变量
        inputs: Dict[str, Any] = {}
        missing: List[str] = []
        for port_def in node_def.inputs:
            src = graph.get_source(nid, port_def.name)
            if src is None:
                if port_def.required:
                    missing.append(port_def.label)
                continue
            src_node, src_port = src
            value = results.get(src_node.node_id, {}).get(src_port.name)
            if value is None and port_def.required:
                missing.append(f"{port_def.label}（上游未产出）")
                continue
            inputs[port_def.name] = value

        if missing:
            msg = f"缺少输入: {', '.join(missing)}"
            _notify(status, nid, "error", msg)
            ctx.log(f"[{label}] {msg}", level="error")
            failed = True
            raise FlowError(f"节点 {label} {msg}", node_id=nid)

        params = resolve_params(node_def, node.data)
        ctx.log(f"--- 执行节点: {label} ({node_def.type_id}) ---")
        _notify(status, nid, "running", "")
        try:
            outputs = node_def.func(inputs, params, ctx) or {}
        except Exception as exc:
            _notify(status, nid, "error", str(exc))
            ctx.log(f"[{label}] 执行失败: {exc}", level="error")
            ctx.log(traceback.format_exc(), level="error")
            failed = True
            raise FlowError(f"节点 {label} 执行失败: {exc}", node_id=nid) from exc

        results[nid] = outputs
        _notify(status, nid, "done", "")

    if not failed and not ctx.is_stopped():
        ctx.log("流程执行完成")
    return results


def run_graph_async(graph: FlowGraph,
                    log: Optional[Callable[[str, str], None]] = None,
                    status: Optional[Callable[[str, str, str], None]] = None,
                    on_done: Optional[Callable[[Optional[Exception], Dict[str, Dict[str, Any]]], None]] = None,
                    run_dir: Optional[Path] = None,
                    popup: Optional[Callable[[str, str], None]] = None) -> threading.Event:
    """在后台线程中执行流程图，返回可用于停止的 Event。"""
    stop_event = threading.Event()

    def _worker():
        error: Optional[Exception] = None
        results: Dict[str, Dict[str, Any]] = {}
        try:
            results = run_graph(graph, log=log, status=status,
                                stop_event=stop_event, run_dir=run_dir,
                                popup=popup)
        except Exception as exc:  # FlowError 或意外错误
            error = exc
        if on_done is not None:
            on_done(error, results)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return stop_event


def _notify(status, nid, state, message):
    if status is not None:
        status(nid, state, message)
