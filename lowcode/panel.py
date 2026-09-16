"""低代码节点编辑器画布。

仿照 RS-232_lab_device 项目 lab_engine/gui/setup_panel.py 的可视化方法，
为叠加调制低代码编程定制的 standalone 版本（不依赖 lab_engine）：

- 从节点库（lowcode.nodes）添加功能节点
- 拖拽移动节点、端口间拖出贝塞尔连线（传递变量）
- Ctrl+滚轮缩放、空格/中键平移、Delete 删除
- 属性面板按节点参数定义自动生成编辑控件
- 保存 / 加载 .spflow.json
- 运行状态着色（运行中 / 完成 / 出错）
"""
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from lowcode.graph_model import (
    NODE_HEIGHT,
    NODE_WIDTH,
    PORT_RADIUS,
    FlowGraph,
    Node,
    Port,
    port_type_compatible,
)
from lowcode.nodes import CATEGORY_COLORS, NODES, get_node_def, node_types_by_category


# UI 配色
COLOR_BG = "#F1F5F9"
COLOR_CARD = "#FFFFFF"
COLOR_PRIMARY = "#2563EB"
COLOR_TEXT_DIM = "#64748B"
UI_FONT = "Microsoft YaHei UI"

# 端口数据类型配色
PORT_COLORS = {
    "bits": "#F97316",
    "dec": "#FB7185",
    "symbols": "#8B5CF6",
    "waveform": "#3B82F6",
    "rq": "#EF4444",
    "power": "#EAB308",
    "avt": "#14B8A6",
    "snr": "#22C55E",
    "mask": "#A3A380",
    "channel": "#0EA5E9",
    "dict": "#6D28D9",
    "scalar": "#64748B",
    "array": "#64748B",
    "any": "#94A3B8",
}

# 节点运行状态配色
STATUS_COLORS = {
    "running": "#3B82F6",
    "done": "#22C55E",
    "error": "#EF4444",
    "warning": "#EAB308",
    "skipped": "#94A3B8",
}

GRID_SIZE = 20
GRID_COLOR = "#94A3B8"


def set_dpi_aware() -> str:
    """Windows 高 DPI 适配，返回实际生效的模式。

    优先 Per-Monitor V2（Win10 1703+），依次回退到
    Per-Monitor（Vista+ shcore）与 System DPI Aware；
    全部失败时返回 "failed"（此时 Windows 会对窗口做位图拉伸，界面发虚）。
    """
    try:
        import ctypes
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
            if ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
                return "PerMonitorV2"
        except Exception:
            pass
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
                return "PerMonitor"
        except Exception:
            pass
        try:
            if ctypes.windll.user32.SetProcessDPIAware():
                return "SystemAware"
        except Exception:
            pass
    except Exception:
        pass
    return "failed"


def set_tk_scaling(root: tk.Tk) -> float:
    """按系统 DPI 设置 tk 缩放，返回缩放系数。"""
    try:
        dpi = root.winfo_fpixels("1i")
        scale = max(dpi / 96.0, 1.0)
        root.tk.call("tk", "scaling", scale * 96 / 72.0)
        return scale
    except Exception:
        return 1.0


class FlowPanel(ttk.Frame):
    """低代码流程编辑器面板。"""

    def __init__(
        self,
        parent,
        scale: Optional[float] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        on_run: Optional[Callable[[FlowGraph], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
        viewer_mode: bool = False,
    ):
        super().__init__(parent)
        self.on_log = on_log
        self.on_run = on_run
        self.on_stop = on_stop
        self.viewer_mode = viewer_mode
        self.scale = scale if scale is not None else 1.0

        self.graph = FlowGraph()
        self.selected_node_id: Optional[str] = None
        self._drag_node_id: Optional[str] = None
        self._drag_node_start: Optional[Tuple[float, float]] = None
        self._drag_mouse_start: Optional[Tuple[float, float]] = None
        self._edge_start: Optional[Tuple[str, str]] = None
        self._temp_edge_line: Optional[int] = None
        self._hover_edge_id: Optional[str] = None

        self.zoom = 1.0
        self._space_pressed = False
        self._panning = False

        self._node_items: Dict[str, Dict[str, Any]] = {}
        self._edge_items: Dict[str, int] = {}
        self._grid_items: List[int] = []
        self._grid_photo = None          # 栅格化网格图片（PIL ImageTk）
        self._grid_item: Optional[int] = None
        self._view_after: Optional[str] = None   # 网格/视口防抖重绘定时器
        self._minimap_viewport: Optional[int] = None
        self._prop_vars: Dict[str, tk.Variable] = {}
        self._popup_win: Optional[tk.Toplevel] = None

        self._build_ui()
        self._bind_events()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        if not self.viewer_mode:
            toolbar = tk.Frame(self, bg=COLOR_BG)
            toolbar.pack(fill=tk.X, pady=(0, 6))

            ttk.Label(toolbar, text="节点:").pack(side=tk.LEFT, padx=(0, 4))
            self._node_type_var = tk.StringVar()
            self._node_combo = ttk.Combobox(
                toolbar, textvariable=self._node_type_var, state="readonly", width=28)
            choices = []
            for cat, defs in node_types_by_category().items():
                for nd in defs:
                    choices.append(f"{cat} | {nd.label} ({nd.type_id})")
            self._node_combo["values"] = choices
            if choices:
                self._node_combo.current(0)
            self._node_combo.pack(side=tk.LEFT, padx=4)
            ttk.Button(toolbar, text="+ 添加节点", command=self._add_selected_node).pack(
                side=tk.LEFT, padx=4)

            ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
            ttk.Button(toolbar, text="新建", command=self._new_graph).pack(side=tk.LEFT, padx=2)
            ttk.Button(toolbar, text="打开", command=self._load_graph).pack(side=tk.LEFT, padx=2)
            ttk.Button(toolbar, text="保存", command=self._save_graph).pack(side=tk.LEFT, padx=2)
            ttk.Button(toolbar, text="载入叠加调制流程模板",
                       command=self._load_template).pack(side=tk.LEFT, padx=2)
            ttk.Button(toolbar, text="适应视图",
                       command=self.fit_view).pack(side=tk.LEFT, padx=2)

            ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
            self.run_btn = ttk.Button(toolbar, text="▶ 运行",
                                      command=self._on_run_clicked)
            self.run_btn.pack(side=tk.LEFT, padx=4)
            self.stop_btn = ttk.Button(toolbar, text="■ 停止",
                                       command=self._on_stop_clicked, state=tk.DISABLED)
            self.stop_btn.pack(side=tk.LEFT, padx=2)

        body = tk.Frame(self, bg=COLOR_BG)
        body.pack(fill=tk.BOTH, expand=True)

        canvas_frame = tk.Frame(body, bg=COLOR_BG)
        canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(
            canvas_frame, bg=COLOR_BG,
            highlightthickness=1, highlightbackground="#CBD5E1",
            scrollregion=(0, 0, 4000 * self.scale, 3000 * self.scale),
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tk_scaling = self.canvas.tk.call("tk", "scaling")

        vbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        hbar = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        hbar.pack(fill=tk.X)
        self._vbar, self._hbar = vbar, hbar
        # 包装滚动回调：滚动后刷新网格贴图与小地图视口
        self.canvas.configure(xscrollcommand=self._on_xscroll,
                              yscrollcommand=self._on_yscroll)
        self.canvas.bind("<Configure>", lambda _e: self._schedule_view_refresh())

        self._draw_grid()
        self._draw_help_text()
        self._build_minimap(canvas_frame)

        # 属性面板
        prop_card = tk.Frame(body, bg=COLOR_CARD,
                             highlightbackground="#E2E8F0", highlightthickness=1)
        prop_card.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        prop_card.pack_propagate(False)
        prop_card.configure(width=int(300 * self.scale))

        prop_inner = tk.Frame(prop_card, bg=COLOR_CARD)
        prop_inner.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        ttk.Label(prop_inner, text="属性", font=(UI_FONT, 11, "bold")).pack(
            anchor=tk.W, pady=(0, 6))

        self.prop_canvas = tk.Canvas(prop_inner, bg=COLOR_CARD, highlightthickness=0,
                                     width=int(260 * self.scale))
        prop_vsb = ttk.Scrollbar(prop_inner, orient=tk.VERTICAL,
                                 command=self.prop_canvas.yview)
        self.prop_canvas.configure(yscrollcommand=prop_vsb.set)
        prop_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.prop_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.prop_frame = tk.Frame(self.prop_canvas, bg=COLOR_CARD)
        self.prop_canvas.create_window((0, 0), window=self.prop_frame, anchor="nw",
                                       width=int(260 * self.scale))
        self.prop_frame.bind(
            "<Configure>",
            lambda _e: self.prop_canvas.configure(
                scrollregion=self.prop_canvas.bbox("all")))

        self.status_lbl = ttk.Label(self, text="就绪")
        self.status_lbl.pack(side=tk.BOTTOM, anchor=tk.W)

    def _draw_help_text(self):
        help_lines = [
            "低代码流程编辑器",
            "拖拽端口连线传递变量",
            "Ctrl+滚轮缩放  空格/中键平移",
            "悬停高亮连线及其两端节点",
            "右键连线查看详情  Delete 删除节点",
        ]
        for item in getattr(self, "_help_items", []):
            self.canvas.delete(item)
        self._help_items = []
        for i, line in enumerate(help_lines):
            font = self._font(10, bold=True) if i == 0 else self._font(8)
            fill = COLOR_PRIMARY if i == 0 else COLOR_TEXT_DIM
            item = self.canvas.create_text(
                0, 0, text=line, font=font, fill=fill, anchor=tk.NE,
                tags=("help_text",))
            self._help_items.append(item)
        self._reposition_help()

    def _reposition_help(self):
        """帮助文字钉在可视区域右上角（随滚动/缩放移动）。"""
        if not getattr(self, "_help_items", None):
            return
        cw = self.canvas.winfo_width()
        if cw <= 1:
            return
        x = self.canvas.canvasx(cw) - 30 * self.scale
        y0 = self.canvas.canvasy(0) + 30 * self.scale
        for i, item in enumerate(self._help_items):
            self.canvas.coords(item, x, y0 + i * self._to_screen_scalar(18))
            self.canvas.tag_raise(item)

    def _draw_grid(self):
        # 网格改为单张栅格化贴图，仅覆盖可视区域，滚动/缩放后防抖重绘
        self._schedule_view_refresh()

    def _schedule_view_refresh(self):
        if self._view_after is not None:
            try:
                self.after_cancel(self._view_after)
            except Exception:
                pass
        self._view_after = self.after(40, self._refresh_view)

    def _refresh_view(self):
        self._view_after = None
        self._redraw_grid_image()
        self._reposition_help()
        self._update_minimap_viewport()

    def _grid_step_px(self) -> int:
        """屏幕上网格点间距（像素），缩得太小时按 2 倍逻辑步长递增。"""
        step = GRID_SIZE * self.zoom * self.scale
        while step < 22:
            step *= 2
        return max(int(step), 4)

    def _redraw_grid_image(self):
        if not self.canvas.winfo_exists():
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        step = self._grid_step_px()
        margin = step
        x0 = self.canvas.canvasx(0) - margin
        y0 = self.canvas.canvasy(0) - margin
        w, h = int(cw + 2 * margin), int(ch + 2 * margin)
        try:
            from PIL import Image, ImageDraw, ImageTk
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            r = 1 if step < 48 else 2
            fx = (step - (x0 % step)) % step
            fy = (step - (y0 % step)) % step
            for ix in range(int(fx), w, step):
                for iy in range(int(fy), h, step):
                    draw.ellipse([ix - r, iy - r, ix + r, iy + r], fill=GRID_COLOR)
            self._grid_photo = ImageTk.PhotoImage(img)
            if self._grid_item is None:
                self._grid_item = self.canvas.create_image(
                    x0, y0, image=self._grid_photo, anchor=tk.NW, tags=("grid",))
            else:
                self.canvas.itemconfigure(self._grid_item, image=self._grid_photo)
                self.canvas.coords(self._grid_item, x0, y0)
            self.canvas.tag_lower("grid")
        except ImportError:
            # 无 PIL 时退化为视口内的稀疏点阵
            for item in self._grid_items:
                self.canvas.delete(item)
            self._grid_items.clear()
            if self._grid_item is not None:
                self.canvas.delete(self._grid_item)
                self._grid_item = None
            fx = (step - (x0 % step)) % step
            fy = (step - (y0 % step)) % step
            for ix in range(int(fx), w, step):
                for iy in range(int(fy), h, step):
                    self._grid_items.append(self.canvas.create_oval(
                        x0 + ix - 1, y0 + iy - 1, x0 + ix + 1, y0 + iy + 1,
                        fill=GRID_COLOR, outline="", tags=("grid",)))
            self.canvas.tag_lower("grid")

    def _on_xscroll(self, *args):
        self._hbar.set(*args)
        self._update_minimap_viewport()
        self._schedule_view_refresh()

    def _on_yscroll(self, *args):
        self._vbar.set(*args)
        self._update_minimap_viewport()
        self._schedule_view_refresh()

    # ------------------------------------------------------------------
    # 小地图
    # ------------------------------------------------------------------
    def _build_minimap(self, canvas_frame):
        mw, mh = int(150 * self.scale), int(100 * self.scale)
        self.minimap = tk.Canvas(
            canvas_frame, width=mw, height=mh, bg=COLOR_CARD,
            highlightthickness=1, highlightbackground="#CBD5E1")
        self.minimap.place(relx=1.0, rely=1.0, anchor=tk.SE, x=-12, y=-12)
        self.minimap.bind("<ButtonPress-1>", self._on_minimap_press)
        self.minimap.bind("<B1-Motion>", self._on_minimap_press)
        self.minimap.bind("<Configure>", lambda _e: self._redraw_minimap())

    def _minimap_ratio(self) -> float:
        sr = str(self.canvas.cget("scrollregion")).split()
        if len(sr) != 4:
            return 0.0
        sr_w, sr_h = float(sr[2]), float(sr[3])
        mw, mh = self.minimap.winfo_width(), self.minimap.winfo_height()
        if sr_w <= 0 or sr_h <= 0 or mw <= 1 or mh <= 1:
            return 0.0
        return min(mw / sr_w, mh / sr_h)

    def _redraw_minimap(self):
        if not self.minimap.winfo_exists():
            return
        self.minimap.delete("all")
        k = self._minimap_ratio()
        if k > 0:
            for n in self.graph.nodes.values():
                w, h = self._node_size(n)
                x1, y1 = self._to_screen(n.x, n.y)
                zw, zh = self._to_screen_scalar(w), self._to_screen_scalar(h)
                self.minimap.create_rectangle(
                    x1 * k, y1 * k, (x1 + zw) * k, (y1 + zh) * k,
                    fill=self._node_color(n), outline="")
        self._minimap_viewport = self.minimap.create_rectangle(
            0, 0, 1, 1, outline=COLOR_PRIMARY, width=max(1, int(1.5 * self.scale)))
        self._update_minimap_viewport()

    def _update_minimap_viewport(self):
        if (not hasattr(self, "minimap") or not self.minimap.winfo_exists()
                or self._minimap_viewport is None):
            return
        k = self._minimap_ratio()
        if k <= 0:
            return
        x0, y0 = self.canvas.canvasx(0), self.canvas.canvasy(0)
        x1 = self.canvas.canvasx(self.canvas.winfo_width())
        y1 = self.canvas.canvasy(self.canvas.winfo_height())
        self.minimap.coords(self._minimap_viewport, x0 * k, y0 * k, x1 * k, y1 * k)
        self.minimap.tag_raise(self._minimap_viewport)

    def _on_minimap_press(self, event):
        k = self._minimap_ratio()
        if k <= 0:
            return
        # 主视图中心移动到小地图点击处
        target_x = event.x / k
        target_y = event.y / k
        vw = self.canvas.canvasx(self.canvas.winfo_width()) - self.canvas.canvasx(0)
        vh = self.canvas.canvasy(self.canvas.winfo_height()) - self.canvas.canvasy(0)
        sr = str(self.canvas.cget("scrollregion")).split()
        if len(sr) != 4:
            return
        sr_w, sr_h = float(sr[2]), float(sr[3])
        fx = (target_x - vw / 2) / sr_w if sr_w > 0 else 0.0
        fy = (target_y - vh / 2) / sr_h if sr_h > 0 else 0.0
        self.canvas.xview_moveto(min(max(fx, 0.0), 1.0))
        self.canvas.yview_moveto(min(max(fy, 0.0), 1.0))

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<ButtonPress-3>", self._on_canvas_right_click)
        self.canvas.bind("<ButtonPress-2>", self._on_middle_press)
        self.canvas.bind("<B2-Motion>", self._on_middle_drag)
        self.canvas.bind("<ButtonRelease-2>", self._on_middle_release)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas.bind("<Leave>", self._on_canvas_leave)
        self.canvas.bind("<KeyPress-space>", self._on_space_press)
        self.canvas.bind("<KeyRelease-space>", self._on_space_release)
        self.bind_all("<Delete>", self._on_delete_key)
        self.bind_all("<BackSpace>", self._on_delete_key)

    # ------------------------------------------------------------------
    # 坐标/字体换算
    # ------------------------------------------------------------------
    def _to_screen(self, x: float, y: float) -> Tuple[float, float]:
        return x * self.zoom * self.scale, y * self.zoom * self.scale

    def _to_screen_scalar(self, v: float) -> float:
        return v * self.zoom * self.scale

    def _from_screen(self, x: float, y: float) -> Tuple[float, float]:
        return x / (self.zoom * self.scale), y / (self.zoom * self.scale)

    def _font(self, size: int, bold: bool = False):
        s = max(1, int(round(size * 4 / 3 * self.zoom * self.scale / self.tk_scaling)))
        return (UI_FONT, s, "bold") if bold else (UI_FONT, s)

    def _node_size(self, node: Node) -> Tuple[float, float]:
        n_ports = max(2, len(node.ports))
        return NODE_WIDTH, max(NODE_HEIGHT, 50 + n_ports * 26)

    def _port_position(self, node: Node, port: Port,
                       node_h: Optional[float] = None) -> Tuple[float, float]:
        w, default_h = self._node_size(node)
        h = node_h if node_h is not None else default_h
        inputs = [p for p in node.ports if p.direction == "input"]
        outputs = [p for p in node.ports if p.direction == "output"]
        if port.direction == "input":
            idx = inputs.index(port)
            y = node.y + 30 + (idx + 1) * ((h - 40) / max(len(inputs), 1))
            return node.x, y
        idx = outputs.index(port)
        y = node.y + 30 + (idx + 1) * ((h - 40) / max(len(outputs), 1))
        return node.x + w, y

    # ------------------------------------------------------------------
    # 节点/边操作
    # ------------------------------------------------------------------
    def _add_selected_node(self):
        text = self._node_type_var.get()
        if not text:
            return
        type_id = text.rsplit("(", 1)[-1].rstrip(")")
        x = self.canvas.canvasx(self.canvas.winfo_width() / 2) / (self.zoom * self.scale)
        y = self.canvas.canvasy(self.canvas.winfo_height() / 2) / (self.zoom * self.scale)
        x += (len(self.graph.nodes) % 5) * 40
        y += (len(self.graph.nodes) % 3) * 120
        self.add_node_of_type(type_id, x, y)

    def add_node_of_type(self, type_id: str, x: float, y: float) -> Optional[Node]:
        node_def = get_node_def(type_id)
        if node_def is None:
            return None
        node = self.graph.add_node(node_def.type_id, x, y, label=node_def.label)
        node.ports = [
            Port(p.name, p.label, "input", p.data_type) for p in node_def.inputs
        ] + [
            Port(p.name, p.label, "output", p.data_type) for p in node_def.outputs
        ]
        node.data["params"] = {p.name: p.default for p in node_def.params}
        self._draw_node(node)
        self._redraw_minimap()
        self._select_node(node.node_id)
        self._set_status(f"添加节点: {node.label}")
        return node

    def set_graph(self, graph: FlowGraph):
        """设置整张图（模板/加载文件），并刷新画布。"""
        self.graph = graph
        self.selected_node_id = None
        # 按节点定义刷新端口（兼容旧文件）
        for node in self.graph.nodes.values():
            node_def = get_node_def(node.node_type)
            if node_def is not None:
                node.ports = [
                    Port(p.name, p.label, "input", p.data_type) for p in node_def.inputs
                ] + [
                    Port(p.name, p.label, "output", p.data_type) for p in node_def.outputs
                ]
                node.data.setdefault("params",
                                     {p.name: p.default for p in node_def.params})
        self._redraw_all()
        self.after_idle(self.fit_view)

    def _remove_node(self, node_id: str):
        self.graph.remove_node(node_id)
        self._erase_node(node_id)
        for edge_id in list(self._edge_items.keys()):
            if edge_id not in self.graph.edges:
                self._erase_edge(edge_id)
        if self.selected_node_id == node_id:
            self.selected_node_id = None
            self._clear_property_panel(placeholder=True)
        self._redraw_all_edges()
        self._redraw_minimap()

    # ------------------------------------------------------------------
    # Canvas 绘制
    # ------------------------------------------------------------------
    def _round_rect(self, x1, y1, x2, y2, r=8, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _node_color(self, node: Node) -> str:
        node_def = get_node_def(node.node_type)
        if node_def is not None:
            return CATEGORY_COLORS.get(node_def.category, COLOR_PRIMARY)
        return node.data.get("color", COLOR_PRIMARY)

    def _draw_node(self, node: Node):
        self._erase_node(node.node_id)
        items: Dict[str, Any] = {"ports": {}, "labels": []}

        w, h = self._node_size(node)
        x, y = self._to_screen(node.x, node.y)
        zw, zh = self._to_screen_scalar(w), self._to_screen_scalar(h)
        r = self._to_screen_scalar(8)
        color = self._node_color(node)

        rect = self._round_rect(
            x, y, x + zw, y + zh, r=r,
            fill=COLOR_CARD, outline="#CBD5E1", width=max(1, int(2 * self.zoom)),
            tags=(f"node:{node.node_id}", f"nodeall:{node.node_id}", "node"))
        items["rect"] = rect

        title_h = self._to_screen_scalar(24)
        items["title_bg"] = self._round_rect(
            x, y, x + zw, y + title_h, r=r,
            fill=color, outline="",
            tags=(f"node:{node.node_id}", f"nodeall:{node.node_id}", "node_title_bg"))
        items["title"] = self.canvas.create_text(
            x + zw / 2, y + title_h / 2,
            text=node.label, fill="white", font=self._font(9, bold=True),
            tags=(f"node:{node.node_id}", f"nodeall:{node.node_id}", "node_title"))
        items["type_label"] = self.canvas.create_text(
            x + zw / 2, y + zh - self._to_screen_scalar(10),
            text=node.node_type, fill=COLOR_TEXT_DIM, font=self._font(7),
            tags=(f"node:{node.node_id}", f"nodeall:{node.node_id}", "node_type"))

        for port in [p for p in node.ports if p.direction == "input"]:
            self._draw_port(node, port, h, items, is_input=True)
        for port in [p for p in node.ports if p.direction == "output"]:
            self._draw_port(node, port, h, items, is_input=False)

        # 状态指示
        status = node.data.get("_status", "normal")
        if status != "normal":
            dot_r = max(3, int(5 * self.zoom))
            dot_x = x + zw - dot_r * 2
            dot_y = y + title_h + dot_r * 1.5
            dot_color = STATUS_COLORS.get(status, "#64748B")
            items["status_dot"] = self.canvas.create_oval(
                dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r,
                fill=dot_color, outline="white", width=max(1, int(1.5 * self.zoom)),
                tags=(f"status:{node.node_id}", f"nodeall:{node.node_id}",
                      "status_dot"))

        self._node_items[node.node_id] = items
        self._update_node_selection_look(node.node_id)

    def _draw_port(self, node: Node, port: Port, node_h: float,
                   items: Dict[str, Any], is_input: bool):
        px, py = self._port_position(node, port, node_h)
        px, py = self._to_screen(px, py)
        pr = self._to_screen_scalar(PORT_RADIUS)
        items["ports"][port.name] = self.canvas.create_oval(
            px - pr, py - pr, px + pr, py + pr,
            fill=PORT_COLORS.get(port.data_type, "#64748B"),
            outline="white", width=max(1, int(2 * self.zoom)),
            tags=(f"port:{node.node_id}:{port.name}",
                  f"nodeall:{node.node_id}", "port"))
        anchor = tk.W if is_input else tk.E
        dx = self._to_screen_scalar(10) if is_input else -self._to_screen_scalar(10)
        label = port.label + ("" if is_input or True else "")
        items["labels"].append(self.canvas.create_text(
            px + dx, py, text=label, fill=COLOR_TEXT_DIM, font=self._font(7),
            anchor=anchor,
            tags=(f"port_label:{node.node_id}:{port.name}",
                  f"nodeall:{node.node_id}")))

    def _draw_edge(self, edge_id: str):
        self._erase_edge(edge_id)
        edge = self.graph.edges.get(edge_id)
        if edge is None:
            return
        src = self.graph.get_node(edge.source_node)
        dst = self.graph.get_node(edge.target_node)
        if src is None or dst is None:
            return
        src_port = src.port(edge.source_port)
        dst_port = dst.port(edge.target_port)
        if src_port is None or dst_port is None:
            return
        x1, y1 = self._port_position(src, src_port)
        x2, y2 = self._port_position(dst, dst_port)
        x1, y1 = self._to_screen(x1, y1)
        x2, y2 = self._to_screen(x2, y2)
        cx = (x1 + x2) / 2
        color = PORT_COLORS.get(src_port.data_type, COLOR_PRIMARY)
        line = self.canvas.create_line(
            x1, y1, cx, y1, cx, y2, x2, y2,
            fill=color, width=max(1, int(2 * self.zoom)),
            smooth=True, splinesteps=24, arrow=tk.LAST,
            tags=(f"edge:{edge_id}", "edge"))
        self._edge_items[edge_id] = line
        self.canvas.tag_lower(line, "node")

    def _erase_node(self, node_id: str):
        items = self._node_items.pop(node_id, {})
        for key, val in items.items():
            if key == "ports":
                for c in val.values():
                    self.canvas.delete(c)
            elif key == "labels":
                for lbl in val:
                    self.canvas.delete(lbl)
            else:
                self.canvas.delete(val)

    def _erase_edge(self, edge_id: str):
        line = self._edge_items.pop(edge_id, None)
        if line is not None:
            self.canvas.delete(line)

    def _redraw_all_edges(self):
        for edge_id in list(self._edge_items.keys()):
            self._erase_edge(edge_id)
        for edge_id in self.graph.edges.keys():
            self._draw_edge(edge_id)
        self._refresh_edge_highlights()

    # ------------------------------------------------------------------
    # 连线悬停 / 选中高亮
    # ------------------------------------------------------------------
    HIGHLIGHT_COLOR = "#F97316"   # 悬停高亮色（橙）

    def _edge_hit_test(self, event) -> Optional[str]:
        """检测鼠标附近的连线（带容差，因为线宽只有 2px）。"""
        sx = self.canvas.canvasx(event.x)
        sy = self.canvas.canvasy(event.y)
        r = max(4, int(7 * self.zoom * self.scale))
        items = self.canvas.find_overlapping(sx - r, sy - r, sx + r, sy + r)
        for item in reversed(items):
            for tag in self.canvas.gettags(item):
                if tag.startswith("edge:"):
                    return tag.split(":", 1)[1]
        return None

    def _on_canvas_motion(self, event):
        # 拖拽节点 / 连线中 / 平移中不做悬停检测，避免闪烁
        if (self._drag_node_id is not None or self._edge_start is not None
                or self._panning or self._space_pressed):
            if self._hover_edge_id is not None:
                self._set_hover_edge(None)
            return
        self._set_hover_edge(self._edge_hit_test(event))

    def _on_canvas_leave(self, _event):
        self._set_hover_edge(None)

    def _set_hover_edge(self, edge_id: Optional[str]):
        old = self._hover_edge_id
        if old == edge_id:
            return
        if old is not None:
            old_edge = self.graph.edges.get(old)
            if old_edge is not None:
                self._update_node_selection_look(old_edge.source_node)
                self._update_node_selection_look(old_edge.target_node)
                self._restore_endpoint_ports(old_edge)
        self._hover_edge_id = edge_id
        if edge_id is not None:
            edge = self.graph.edges.get(edge_id)
            if edge is not None:
                src = self.graph.get_node(edge.source_node)
                dst = self.graph.get_node(edge.target_node)
                for nid in (edge.source_node, edge.target_node):
                    items = self._node_items.get(nid)
                    if items and items.get("rect"):
                        self.canvas.itemconfigure(
                            items["rect"], outline=self.HIGHLIGHT_COLOR,
                            width=max(2, int(3 * self.zoom)))
                self._highlight_endpoint_ports(edge)
                if src is not None and dst is not None:
                    sp = src.port(edge.source_port)
                    dp = dst.port(edge.target_port)
                    if sp and dp:
                        self._set_status(
                            f"{src.label}.{sp.label} → {dst.label}.{dp.label}"
                            f"  (类型: {sp.data_type})")
        else:
            self._set_status("就绪")
        self._refresh_edge_highlights()

    def _highlight_endpoint_ports(self, edge):
        for node_id, port_name in ((edge.source_node, edge.source_port),
                                   (edge.target_node, edge.target_port)):
            items = self._node_items.get(node_id)
            if items:
                item = items.get("ports", {}).get(port_name)
                if item is not None:
                    self.canvas.itemconfigure(
                        item, outline=self.HIGHLIGHT_COLOR,
                        width=max(2, int(3 * self.zoom)))

    def _restore_endpoint_ports(self, edge):
        for node_id, port_name in ((edge.source_node, edge.source_port),
                                   (edge.target_node, edge.target_port)):
            items = self._node_items.get(node_id)
            if items:
                item = items.get("ports", {}).get(port_name)
                if item is not None:
                    self.canvas.itemconfigure(
                        item, outline="white",
                        width=max(1, int(2 * self.zoom)))

    def _refresh_edge_highlights(self):
        """按当前状态刷新所有连线外观：选中节点的连线加粗，悬停连线高亮。"""
        sel = self.selected_node_id
        for eid, line in self._edge_items.items():
            edge = self.graph.edges.get(eid)
            if edge is None:
                continue
            src = self.graph.get_node(edge.source_node)
            src_port = src.port(edge.source_port) if src else None
            color = (PORT_COLORS.get(src_port.data_type, COLOR_PRIMARY)
                     if src_port else COLOR_PRIMARY)
            width = max(1, int(2 * self.zoom))
            if sel and (edge.source_node == sel or edge.target_node == sel):
                width = max(2, int(3 * self.zoom))
            if eid == self._hover_edge_id:
                color = self.HIGHLIGHT_COLOR
                width = max(3, int(4 * self.zoom))
                self.canvas.tag_raise(line, "edge")  # 悬停连线浮到其他连线之上
            self.canvas.itemconfigure(line, fill=color, width=width)

    def _redraw_node(self, node_id: str):
        node = self.graph.get_node(node_id)
        if node is None:
            return
        self._draw_node(node)
        self._redraw_all_edges()

    def _fit_scrollregion(self):
        """根据内容范围自动调整滚动区域（含 zoom/scale 换算），避免裁剪节点。"""
        max_x = max_y = 0.0
        for n in self.graph.nodes.values():
            w, h = self._node_size(n)
            max_x = max(max_x, n.x + w)
            max_y = max(max_y, n.y + h)
        max_x = max(max_x + 300, 2400)
        max_y = max(max_y + 500, 1800)
        self.canvas.configure(scrollregion=(0, 0,
                                            max_x * self.zoom * self.scale,
                                            max_y * self.zoom * self.scale))

    def fit_view(self):
        """缩放并滚动到能完整看到整张图（模板/打开文件后调用）。"""
        if not self.graph.nodes:
            return
        min_x = min(n.x for n in self.graph.nodes.values())
        min_y = min(n.y for n in self.graph.nodes.values())
        max_x = max(n.x + self._node_size(n)[0] for n in self.graph.nodes.values())
        max_y = max(n.y + self._node_size(n)[1] for n in self.graph.nodes.values())
        gw = max(max_x - min_x, 1.0)
        gh = max(max_y - min_y, 1.0)
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        margin = 40.0  # 逻辑像素边距
        zoom = min(cw / ((gw + 2 * margin) * self.scale),
                   ch / ((gh + 2 * margin) * self.scale))
        self.zoom = max(0.2, min(1.5, zoom))
        self._redraw_all()
        sr = str(self.canvas.cget("scrollregion")).split()
        if len(sr) == 4:
            sr_w, sr_h = float(sr[2]), float(sr[3])
            if sr_w > 0 and sr_h > 0:
                fx = max((min_x - margin) * self.zoom * self.scale, 0) / sr_w
                fy = max((min_y - margin) * self.zoom * self.scale, 0) / sr_h
                self.canvas.xview_moveto(min(max(fx, 0.0), 1.0))
                self.canvas.yview_moveto(min(max(fy, 0.0), 1.0))
        self._set_status(f"适应视图: {self.zoom:.2f}x")

    def _redraw_all(self):
        self.canvas.delete("all")
        self._node_items.clear()
        self._edge_items.clear()
        self._grid_items.clear()
        self._grid_item = None
        self._grid_photo = None
        self._fit_scrollregion()
        self._draw_grid()
        self._draw_help_text()
        for node in self.graph.nodes.values():
            self._draw_node(node)
        self._redraw_all_edges()
        self._redraw_minimap()

    def _update_node_selection_look(self, node_id: str):
        items = self._node_items.get(node_id)
        if items is None:
            return
        rect = items.get("rect")
        if rect is None:
            return
        if self.selected_node_id == node_id:
            self.canvas.itemconfigure(rect, outline=self._node_color(self.graph.get_node(node_id)))
            self.canvas.itemconfigure(rect, width=max(2, int(3 * self.zoom)))
        else:
            self.canvas.itemconfigure(rect, outline="#CBD5E1")
            self.canvas.itemconfigure(rect, width=max(1, int(2 * self.zoom)))

    # ------------------------------------------------------------------
    # 鼠标交互
    # ------------------------------------------------------------------
    def _hit_test(self, x: float, y: float):
        sx, sy = self._to_screen(x, y)
        r = max(2, int(4 * self.zoom * self.scale))
        items = self.canvas.find_overlapping(sx - r, sy - r, sx + r, sy + r)
        for item in reversed(items):
            for tag in self.canvas.gettags(item):
                if tag.startswith("port:"):
                    _, node_id, port_name = tag.split(":", 2)
                    return "port", node_id, port_name
                if tag.startswith("node:"):
                    _, node_id = tag.split(":", 1)
                    return "node", node_id, None
        return None, None, None

    def _canvas_to_graph(self, x: float, y: float) -> Tuple[float, float]:
        return self._from_screen(self.canvas.canvasx(x), self.canvas.canvasy(y))

    def _on_canvas_press(self, event):
        if not self.canvas.winfo_exists():
            return
        x, y = self._canvas_to_graph(event.x, event.y)
        kind, node_id, port_name = self._hit_test(x, y)

        if self._space_pressed:
            self._panning = True
            self.canvas.scan_mark(event.x, event.y)
            self.canvas.config(cursor="fleur")
            return

        if kind == "port":
            self._edge_start = (node_id, port_name)
            self._highlight_connectable_ports(node_id, port_name)
        elif kind == "node":
            self._select_node(node_id)
            self._drag_node_id = node_id
            node = self.graph.get_node(node_id)
            if node:
                self._drag_node_start = (node.x, node.y)
            self._drag_mouse_start = (event.x, event.y)
        else:
            self._select_node(None)

    def _on_canvas_drag(self, event):
        if self._panning:
            self.canvas.scan_dragto(event.x, event.y, gain=1)
            return
        x, y = self._canvas_to_graph(event.x, event.y)
        if self._edge_start is not None:
            self._draw_temp_edge(x, y)
        elif self._drag_node_id is not None:
            node = self.graph.get_node(self._drag_node_id)
            if node and self._drag_node_start and self._drag_mouse_start:
                dx = (event.x - self._drag_mouse_start[0]) / (self.zoom * self.scale)
                dy = (event.y - self._drag_mouse_start[1]) / (self.zoom * self.scale)
                new_x = round((self._drag_node_start[0] + dx) / GRID_SIZE) * GRID_SIZE
                new_y = round((self._drag_node_start[1] + dy) / GRID_SIZE) * GRID_SIZE
                if new_x != node.x or new_y != node.y:
                    old_sx, old_sy = self._to_screen(node.x, node.y)
                    node.x, node.y = new_x, new_y
                    new_sx, new_sy = self._to_screen(new_x, new_y)
                    # 整组移动节点图元，只重画与该节点相连的连线
                    self.canvas.move(f"nodeall:{node.node_id}",
                                     new_sx - old_sx, new_sy - old_sy)
                    self._redraw_incident_edges(node.node_id)

    def _redraw_incident_edges(self, node_id: str):
        for eid, edge in self.graph.edges.items():
            if edge.source_node == node_id or edge.target_node == node_id:
                self._draw_edge(eid)
        self._refresh_edge_highlights()

    def _on_canvas_release(self, event):
        if self._panning:
            self._panning = False
            self.canvas.config(cursor="")
            return
        x, y = self._canvas_to_graph(event.x, event.y)
        if self._edge_start is not None:
            self._clear_temp_edge()
            kind, node_id, port_name = self._hit_test(x, y)
            if kind == "port" and node_id and port_name:
                src_id, src_port = self._edge_start
                edge = self.graph.add_edge(src_id, src_port, node_id, port_name,
                                           strict_types=True)
                if edge:
                    self._draw_edge(edge.edge_id)
                    self._set_status("已创建连线")
                else:
                    self._set_status("连线无效（方向/类型不匹配或该输入已有连线）")
            self._edge_start = None
        elif self._drag_node_id is not None:
            self._drag_node_id = None
            self._drag_node_start = None
            self._drag_mouse_start = None
            self._fit_scrollregion()
            self._redraw_minimap()
            self._schedule_view_refresh()

    def _on_canvas_right_click(self, event):
        x, y = self._canvas_to_graph(event.x, event.y)
        kind, node_id, _ = self._hit_test(x, y)
        if kind == "node":
            node = self.graph.get_node(node_id)
            if node is None:
                return
            menu = tk.Menu(self, tearoff=0)
            node_def = get_node_def(node.node_type)
            if node_def is not None and node_def.description:
                menu.add_command(label=f"说明: {node_def.description[:40]}...",
                                 state=tk.DISABLED)
            menu.add_command(label="删除节点",
                             command=lambda: self._remove_node(node.node_id))
            menu.post(event.x_root, event.y_root)
            return
        # 连线右键：详情弹窗 / 删除
        edge_id = self._edge_hit_test(event)
        if edge_id is None or edge_id not in self.graph.edges:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="查看连线详情",
                         command=lambda: self._show_edge_popup(edge_id))
        if not self.viewer_mode:
            menu.add_command(label="删除连线",
                             command=lambda: self._remove_edge(edge_id))
        menu.post(event.x_root, event.y_root)

    def _remove_edge(self, edge_id: str):
        self.graph.remove_edge(edge_id)
        self._erase_edge(edge_id)
        if self._hover_edge_id == edge_id:
            self._hover_edge_id = None
        self._set_status("已删除连线")

    def _show_edge_popup(self, edge_id: str):
        """在（唯一的）弹窗中显示连线两端与传递的数据类型。"""
        edge = self.graph.edges.get(edge_id)
        if edge is None:
            return
        src = self.graph.get_node(edge.source_node)
        dst = self.graph.get_node(edge.target_node)
        sp = src.port(edge.source_port) if src else None
        dp = dst.port(edge.target_port) if dst else None
        text = (
            f"输出: {src.label if src else '?'} · {sp.label if sp else edge.source_port}\n"
            f"  ↓ 传递变量（数据类型: {sp.data_type if sp else '?'}）\n"
            f"输入: {dst.label if dst else '?'} · {dp.label if dp else edge.target_port}\n"
        )
        self._show_popup("连线详情", text)

    def _show_popup(self, title: str, text: str):
        """可复用的单例弹窗：再次调用时更新内容并提到最前，不会越弹越多。"""
        if self._popup_win is not None and self._popup_win.winfo_exists():
            win = self._popup_win
            txt = win._popup_text
            win.title(title)
            txt.configure(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            txt.insert(tk.END, text)
            txt.configure(state=tk.DISABLED)
            win.lift()
            win.focus_force()
            return
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry(f"{int(420 * self.scale)}x{int(240 * self.scale)}")
        from tkinter import scrolledtext
        txt = scrolledtext.ScrolledText(
            win, font=("Microsoft YaHei UI", max(8, int(10 * self.scale / 2))),
            state=tk.NORMAL, wrap=tk.WORD)
        txt.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        txt.insert(tk.END, text)
        txt.configure(state=tk.DISABLED)
        win._popup_text = txt
        self._popup_win = win

    def _on_middle_press(self, event):
        self._panning = True
        self.canvas.scan_mark(event.x, event.y)
        self.canvas.config(cursor="fleur")

    def _on_middle_drag(self, event):
        if self._panning:
            self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _on_middle_release(self, event):
        self._panning = False
        self.canvas.config(cursor="")

    def _on_mousewheel(self, event):
        if event.state & 0x0004:  # Ctrl
            factor = 1.1 if event.delta > 0 else 0.9
            old_zoom = self.zoom
            self.zoom = max(0.2, min(3.0, self.zoom * factor))
            if self.zoom != old_zoom:
                self._redraw_all()
                self._set_status(f"缩放: {self.zoom:.2f}x")

    def _on_space_press(self, _event):
        self._space_pressed = True

    def _on_space_release(self, _event):
        self._space_pressed = False
        self._panning = False
        self.canvas.config(cursor="")

    def _on_delete_key(self, _event):
        if self.viewer_mode:
            return
        # 焦点在输入框时不删除
        focus = self.focus_get()
        if isinstance(focus, (ttk.Entry, tk.Entry, ttk.Combobox)):
            return
        if self.selected_node_id:
            self._remove_node(self.selected_node_id)
            self._set_status("已删除节点")

    def _select_node(self, node_id: Optional[str]):
        old = self.selected_node_id
        self.selected_node_id = node_id
        if old:
            self._update_node_selection_look(old)
        if node_id:
            self._update_node_selection_look(node_id)
            self._build_property_panel()
        else:
            self._clear_property_panel(placeholder=True)
        self._refresh_edge_highlights()

    # ------------------------------------------------------------------
    # 临时连线
    # ------------------------------------------------------------------
    def _draw_temp_edge(self, x2: float, y2: float):
        self._clear_temp_edge()
        src_id, src_port_name = self._edge_start
        src = self.graph.get_node(src_id)
        src_port = src.port(src_port_name) if src else None
        if src is None or src_port is None:
            return
        x1, y1 = self._port_position(src, src_port)
        x1, y1 = self._to_screen(x1, y1)
        x2, y2 = self._to_screen(x2, y2)
        cx = (x1 + x2) / 2
        self._temp_edge_line = self.canvas.create_line(
            x1, y1, cx, y1, cx, y2, x2, y2,
            fill="#94A3B8", width=max(1, int(2 * self.zoom)),
            smooth=True, splinesteps=24, dash=(4, 4), tags=("temp_edge",))

    def _highlight_connectable_ports(self, source_node_id: str, source_port_name: str):
        src_node = self.graph.get_node(source_node_id)
        src_port = src_node.port(source_port_name) if src_node else None
        if src_port is None:
            return
        for node_id, items in self._node_items.items():
            node = self.graph.get_node(node_id)
            if node is None:
                continue
            for port_name, item_id in items.get("ports", {}).items():
                port = node.port(port_name)
                if port is None:
                    continue
                can_connect = (
                    port.direction != src_port.direction
                    and port_type_compatible(src_port.data_type, port.data_type)
                )
                if can_connect:
                    self.canvas.itemconfigure(item_id, outline="#22C55E",
                                              width=max(2, int(3 * self.zoom)))
                else:
                    self.canvas.itemconfigure(item_id, outline="#CBD5E1",
                                              width=max(1, int(1 * self.zoom)))

    def _clear_port_highlights(self):
        for items in self._node_items.values():
            for item_id in items.get("ports", {}).values():
                self.canvas.itemconfigure(item_id, outline="white",
                                          width=max(1, int(2 * self.zoom)))

    def _clear_temp_edge(self):
        if self._temp_edge_line is not None:
            self.canvas.delete(self._temp_edge_line)
            self._temp_edge_line = None
        self._clear_port_highlights()

    # ------------------------------------------------------------------
    # 属性面板
    # ------------------------------------------------------------------
    def _clear_property_panel(self, placeholder: bool = False):
        for w in self.prop_frame.winfo_children():
            w.destroy()
        self._prop_vars.clear()
        if placeholder:
            ttk.Label(self.prop_frame, text="未选择节点",
                      foreground=COLOR_TEXT_DIM).pack(anchor=tk.W, pady=(4, 0))

    def _build_property_panel(self):
        self._clear_property_panel()
        node = self.graph.get_node(self.selected_node_id)
        if node is None:
            return
        node_def = get_node_def(node.node_type)

        self._add_prop_entry(node, "label", "名称", node.label)
        ttk.Label(self.prop_frame, text=f"类型: {node.node_type}",
                  foreground=COLOR_TEXT_DIM).pack(anchor=tk.W, pady=(2, 6))
        if node_def is not None:
            ttk.Label(self.prop_frame, text=f"类别: {node_def.category}",
                      foreground=COLOR_TEXT_DIM).pack(anchor=tk.W)
            if node_def.description:
                ttk.Label(self.prop_frame, text=node_def.description,
                          foreground=COLOR_TEXT_DIM, wraplength=int(240 * self.scale),
                          justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 8))
            for p in node_def.params:
                current = node.data.get("params", {}).get(p.name, p.default)
                if p.type == "choice":
                    self._add_prop_choice(node, p, current)
                elif p.type == "bool":
                    self._add_prop_bool(node, p, current)
                else:
                    self._add_prop_param_entry(node, p, current)

        status = node.data.get("_status")
        if status:
            msg = node.data.get("_status_msg", "")
            ttk.Label(self.prop_frame, text=f"运行状态: {status} {msg}",
                      foreground=STATUS_COLORS.get(status, COLOR_TEXT_DIM),
                      wraplength=int(240 * self.scale)).pack(anchor=tk.W, pady=(8, 0))

    def _add_prop_entry(self, node: Node, key: str, label: str, default: str):
        row = tk.Frame(self.prop_frame, bg=COLOR_CARD)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=f"{label}:", width=12).pack(side=tk.LEFT)
        var = tk.StringVar(value=str(default))
        var.trace_add("write", lambda *_: self._on_label_changed(node, var))
        ttk.Entry(row, textvariable=var, width=18).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._prop_vars[key] = var

    def _add_prop_param_entry(self, node: Node, p, current):
        row = tk.Frame(self.prop_frame, bg=COLOR_CARD)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=f"{p.label}:", width=12).pack(side=tk.LEFT)
        var = tk.StringVar(value=str(current))
        var.trace_add("write", lambda *_: self._on_param_changed(node, p.name, var))
        ttk.Entry(row, textvariable=var, width=18).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._prop_vars[p.name] = var

    def _add_prop_choice(self, node: Node, p, current):
        row = tk.Frame(self.prop_frame, bg=COLOR_CARD)
        row.pack(fill=tk.X, pady=2)
        ttk.Label(row, text=f"{p.label}:", width=12).pack(side=tk.LEFT)
        var = tk.StringVar(value=str(current))
        combo = ttk.Combobox(row, textvariable=var, values=p.choices,
                             state="readonly", width=16)
        combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        combo.bind("<<ComboboxSelected>>",
                   lambda _e: self._on_param_changed(node, p.name, var))
        self._prop_vars[p.name] = var

    def _add_prop_bool(self, node: Node, p, current):
        row = tk.Frame(self.prop_frame, bg=COLOR_CARD)
        row.pack(fill=tk.X, pady=2)
        var = tk.BooleanVar(value=bool(current))
        cb = ttk.Checkbutton(row, variable=var, text=p.label)
        cb.pack(side=tk.LEFT)
        var.trace_add("write", lambda *_: self._on_param_changed(node, p.name, var))
        self._prop_vars[p.name] = var

    def _on_label_changed(self, node: Node, var: tk.Variable):
        value = var.get()
        node.label = value or node.node_type
        items = self._node_items.get(node.node_id)
        if items and "title" in items:
            self.canvas.itemconfigure(items["title"], text=node.label)

    def _on_param_changed(self, node: Node, key: str, var: tk.Variable):
        value = var.get()
        node.data.setdefault("params", {})[key] = value

    # ------------------------------------------------------------------
    # 工具栏动作
    # ------------------------------------------------------------------
    def _new_graph(self):
        if self.graph.nodes and not messagebox.askyesno("新建", "清空当前流程图？"):
            return
        self.graph = FlowGraph()
        self.selected_node_id = None
        self._clear_property_panel(placeholder=True)
        self._redraw_all()
        self._set_status("已新建空白流程图")

    def _load_graph(self):
        path = filedialog.askopenfilename(
            title="打开流程图", filetypes=[("叠加调制流程图", "*.spflow.json"),
                                         ("JSON", "*.json")])
        if not path:
            return
        try:
            self.set_graph(FlowGraph.load(Path(path)))
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))
            return
        self._set_status(f"已打开: {path}")

    def _save_graph(self):
        path = filedialog.asksaveasfilename(
            title="保存流程图", defaultextension=".spflow.json",
            filetypes=[("叠加调制流程图", "*.spflow.json"), ("JSON", "*.json")])
        if not path:
            return
        try:
            self.graph.save(Path(path))
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._set_status(f"已保存: {path}")

    def _load_template(self):
        from lowcode.flowchart import build_superposition_flow_template
        self.set_graph(build_superposition_flow_template())
        self._set_status("已载入叠加调制标准流程模板")

    def _on_run_clicked(self):
        if self.on_run is not None:
            self.on_run(self.graph)

    def _on_stop_clicked(self):
        if self.on_stop is not None:
            self.on_stop()

    def set_running(self, running: bool):
        if self.viewer_mode:
            return
        self.run_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.configure(state=tk.NORMAL if running else tk.DISABLED)

    def update_node_status(self, node_id: str, state: str, message: str = ""):
        """执行引擎回调：更新节点运行状态并刷新显示。"""
        self.graph.set_node_status(node_id, state, message)
        node = self.graph.get_node(node_id)
        if node is not None:
            self._draw_node(node)  # 位置未变，无需重画连线
        if self.selected_node_id == node_id:
            self._build_property_panel()

    def _set_status(self, text: str):
        # 只更新状态栏；运行日志由执行引擎的 app.log 负责，
        # 避免悬停连线等瞬时信息刷进日志。
        self.status_lbl.configure(text=text)
