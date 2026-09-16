"""Superposed 16QAM 收发链路框图（流程模板）与 PNG 导出。

build_superposition_flow_template() 用 lowcode 节点库搭出完整的叠加调制
收发框图（镜像 main.py / MATLAB A1_TX + A2_RX 链路）：

  PAM4 序列源 -> PAM6 差分映射 -> 发射成形 --(tx_sum)--> 虚拟信道
      -> 互相关同步 -> 下变频抽取 -> MIMO LMS 均衡 -> PAM6 判决
      -> PAM4 解码 -> 误码统计（PAM6 层 / PAM4 层）

另有一条硬件分支：发射成形的 data1/data2 -> AWG 双通道下载，
示波器采集 -> 互相关同步。

命令行：
    python -m lowcode.flowchart --png docs/superposition_flow.png
"""
import sys
from pathlib import Path
from typing import Dict, Optional

from lowcode.graph_model import FlowGraph
from lowcode.nodes import get_node_def


def build_superposition_flow_template() -> FlowGraph:
    """构建叠加调制标准收发流程框图（可直接运行，默认虚拟信道）。"""
    graph = FlowGraph()

    def add(node_id, node_type, x, y, label=None, params=None):
        nd = get_node_def(node_type)
        ports = []
        from lowcode.graph_model import Port
        for p in (nd.inputs if nd else []):
            ports.append(Port(name=p.name, label=p.label, direction="input",
                              data_type=p.data_type))
        for p in (nd.outputs if nd else []):
            ports.append(Port(name=p.name, label=p.label, direction="output",
                              data_type=p.data_type))
        node = graph.add_node(
            node_type, x, y, label=label or (nd.label if nd else node_type),
            data={"params": params or {}}, ports=ports, node_id=node_id)
        return node

    def connect(src, src_port, dst, dst_port):
        graph.add_edge(src, src_port, dst, dst_port)

    # -- TX 支路 ------------------------------------------------------
    add("src", "pam4_source", 40, 120, label="PAM4 序列源")
    add("map", "pam4_to_pam6", 280, 120, label="PAM6 差分映射")
    add("tx", "tx_generate", 520, 100, label="发射成形 (SRRC+上变频)")
    add("ch", "virtual_channel", 780, 100, label="虚拟信道 (VLC+AWGN)",
        params={"snr_db": 20.0})

    # -- 硬件支路（参考节点，默认禁用；实际使用时改连到同步节点并启用） --
    add("awg", "awg_download_dual", 520, 320,
        label="AWG 下载 (CH1=I / CH2=Q)", params={"enabled": False})
    add("scope", "scope_capture", 780, 320, label="示波器采集",
        params={"enabled": False})
    add("note_hw", "note", 1040, 330,
        label="硬件分支说明",
        params={"text": "硬件实验时: tx.data1/data2 -> AWG CH1/CH2; "
                        "scope.波形 -> sync.rx"})

    # -- RX 支路 ------------------------------------------------------
    add("sync", "sync_xcorr", 1040, 100, label="互相关同步")
    add("dconv", "downconvert", 1300, 100, label="下变频 + 匹配滤波 + 抽取")
    add("lms", "mimo_lms", 1560, 100, label="2x2 MIMO LMS 均衡")
    add("judge", "pam6_decide", 1820, 100, label="PAM6 判决")
    add("decode", "pam4_decode", 2080, 100, label="PAM4 差分解码")
    add("ber1", "ber", 2340, 60, label="PAM4-1 BER")
    add("ber2", "ber", 2340, 160, label="PAM4-2 BER")
    add("plot", "plot_constellation", 1820, 300, label="画接收星座图")

    connect("src", "dec1", "map", "dec1")
    connect("src", "dec2", "map", "dec2")
    connect("map", "v1", "tx", "v1")
    connect("map", "v2", "tx", "v2")
    connect("tx", "tx_sum", "ch", "waveform_in")
    connect("ch", "waveform", "sync", "rx")
    connect("tx", "tx_sum", "sync", "tx_ref")
    connect("sync", "datarx", "dconv", "datarx")
    connect("tx", "aux", "dconv", "aux")
    connect("dconv", "symbols", "lms", "symbols")
    connect("map", "v1", "lms", "v1")
    connect("map", "v2", "lms", "v2")
    connect("lms", "recover", "judge", "recover")
    connect("judge", "dec1", "decode", "dec6_1")
    connect("judge", "dec2", "decode", "dec6_2")
    connect("decode", "dec1", "ber1", "rx_dec")
    connect("src", "dec1", "ber1", "tx_dec")
    connect("decode", "dec2", "ber2", "rx_dec")
    connect("src", "dec2", "ber2", "tx_dec")
    connect("lms", "recover", "plot", "symbols")

    return graph


def export_png(graph: FlowGraph, path: Path, dpi: int = 150) -> Path:
    """把流程框图渲染为 PNG（圆角节点 + 贝塞尔连线，风格与编辑器一致）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.font_manager as fm
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch, Circle, PathPatch
    from matplotlib.path import Path as MplPath

    # 中文字体
    for name in ["Microsoft YaHei", "SimHei", "SimSun", "STSong",
                 "WenQuanYi Micro Hei", "Noto Sans CJK SC", "Source Han Sans SC"]:
        try:
            fm.findfont(name, fallback_to_default=False)
            matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            break
        except Exception:
            continue
    matplotlib.rcParams["axes.unicode_minus"] = False

    from lowcode.graph_model import NODE_HEIGHT, NODE_WIDTH
    from lowcode.nodes import CATEGORY_COLORS
    from lowcode.panel import PORT_COLORS

    def node_size(node):
        n_ports = max(2, len(node.ports))
        return NODE_WIDTH, max(NODE_HEIGHT, 50 + n_ports * 26)

    def port_pos(node, port):
        w, h = node_size(node)
        inputs = [p for p in node.ports if p.direction == "input"]
        outputs = [p for p in node.ports if p.direction == "output"]
        if port.direction == "input":
            idx = inputs.index(port)
            y = node.y + 30 + (idx + 1) * ((h - 40) / max(len(inputs), 1))
            return node.x, y
        idx = outputs.index(port)
        y = node.y + 30 + (idx + 1) * ((h - 40) / max(len(outputs), 1))
        return node.x + w, y

    max_x = max((n.x for n in graph.nodes.values()), default=0) + NODE_WIDTH + 120
    max_y = max((n.y for n in graph.nodes.values()), default=0) + NODE_HEIGHT + 120
    fig_w = max(8, min(40, max_x / 140))
    fig_h = max(4, min(28, max_y / 140))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, max_x)
    ax.set_ylim(max_y, 0)   # y 轴向下，与画布一致
    ax.axis("off")

    # 连线（先画线，再画节点，让线压在节点下面）
    for edge in graph.edges.values():
        src = graph.get_node(edge.source_node)
        dst = graph.get_node(edge.target_node)
        if src is None or dst is None:
            continue
        sp = src.port(edge.source_port)
        dp = dst.port(edge.target_port)
        if sp is None or dp is None:
            continue
        x1, y1 = port_pos(src, sp)
        x2, y2 = port_pos(dst, dp)
        cx = (x1 + x2) / 2
        bezier = MplPath([(x1, y1), (cx, y1), (cx, y2), (x2, y2)],
                         [MplPath.MOVETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4])
        color = PORT_COLORS.get(sp.data_type, "#2563EB")
        ax.add_patch(PathPatch(bezier, facecolor="none", edgecolor=color,
                               lw=1.6, alpha=0.9))
        ax.annotate("", xy=(x2 - 4, y2), xytext=(x2 - 14, y2),
                    arrowprops=dict(arrowstyle="-|>", color=color, lw=1.6))

    # 节点
    for node in graph.nodes.values():
        node_def = get_node_def(node.node_type)
        color = (CATEGORY_COLORS.get(node_def.category, "#2563EB")
                 if node_def else "#2563EB")
        w, h = node_size(node)
        ax.add_patch(FancyBboxPatch(
            (node.x, node.y), w, h,
            boxstyle="round,pad=0,rounding_size=8",
            facecolor="white", edgecolor="#CBD5E1", lw=1.4, zorder=3))
        ax.add_patch(FancyBboxPatch(
            (node.x, node.y), w, 24,
            boxstyle="round,pad=0,rounding_size=8",
            facecolor=color, edgecolor="none", zorder=4))
        ax.add_patch(FancyBboxPatch(
            (node.x, node.y + 14), w, 10,
            boxstyle="round,pad=0,rounding_size=0",
            facecolor=color, edgecolor="none", zorder=4))
        ax.text(node.x + w / 2, node.y + 12, node.label,
                ha="center", va="center", fontsize=9, weight="bold",
                color="white", zorder=5)
        ax.text(node.x + w / 2, node.y + h - 10, node.node_type,
                ha="center", va="center", fontsize=6.5,
                color="#64748B", zorder=5)
        if node.node_type == "note":
            note_text = str(node.data.get("params", {}).get("text", ""))
            if note_text:
                ax.text(node.x + w / 2, node.y + 40, note_text,
                        ha="center", va="center", fontsize=6,
                        color="#64748B", zorder=5, wrap=True)
        for port in node.ports:
            px, py = port_pos(node, port)
            pc = PORT_COLORS.get(port.data_type, "#94A3B8")
            ax.add_patch(Circle((px, py), 5, facecolor=pc, edgecolor="white",
                                lw=1.2, zorder=6))
            if port.direction == "input":
                ax.text(px + 10, py, port.label, ha="left", va="center",
                        fontsize=6.5, color="#475569", zorder=6)
            else:
                ax.text(px - 10, py, port.label, ha="right", va="center",
                        fontsize=6.5, color="#475569", zorder=6)

    ax.text(20, 20, "Superposed 16QAM 收发链路框图（lowcode 流程模板）",
            fontsize=13, weight="bold", color="#2563EB",
            ha="left", va="top")
    ax.text(20, 44, "对应 main.py 完整链路；节点即函数，连线即变量传递",
            fontsize=9, color="#64748B", ha="left", va="top")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi, bbox_inches="tight", facecolor="#F1F5F9")
    plt.close(fig)
    return path


def main():
    args = sys.argv[1:]
    if "--png" in args:
        idx = args.index("--png")
        out = Path(args[idx + 1]) if idx + 1 < len(args) \
            else Path("docs/superposition_flow.png")
        path = export_png(build_superposition_flow_template(), out)
        print(f"框图已导出: {path}")
        return
    print("用法: python -m lowcode.flowchart --png <输出.png>")


if __name__ == "__main__":
    main()
