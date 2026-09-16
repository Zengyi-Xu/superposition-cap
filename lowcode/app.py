"""低代码平台主窗口：流程编辑器 + 日志面板 + 运行控制。

运行：
    python -m lowcode
    或 python lowcode_app.py
"""
import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import matplotlib

from lowcode.executor import run_graph_async
from lowcode.flowchart import build_superposition_flow_template, export_png
from lowcode.panel import FlowPanel, set_dpi_aware, set_tk_scaling


class LowCodeApp:
    """低代码平台主窗口。"""

    def __init__(self, root: tk.Tk, scale: float, panel: FlowPanel,
                 paned: ttk.PanedWindow):
        self.root = root
        self.scale = scale
        self.panel = panel
        self.paned = paned
        self._log_queue: "queue.Queue" = queue.Queue()
        self._status_queue: "queue.Queue" = queue.Queue()
        self._popup_queue: "queue.Queue" = queue.Queue()
        self._stop_event = None

        # 菜单
        menubar = tk.Menu(root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="新建", command=self.panel._new_graph)
        file_menu.add_command(label="打开...", command=self.panel._load_graph)
        file_menu.add_command(label="保存...", command=self.panel._save_graph)
        file_menu.add_separator()
        file_menu.add_command(label="导出框图 PNG...", command=self._export_png)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=root.destroy)
        menubar.add_cascade(label="文件", menu=file_menu)

        template_menu = tk.Menu(menubar, tearoff=0)
        template_menu.add_command(label="载入叠加调制标准流程模板",
                                  command=self._load_template)
        menubar.add_cascade(label="模板", menu=template_menu)
        root.config(menu=menubar)

        # 主区域：上编辑器，下日志
        paned.add(self.panel, weight=4)

        log_frame = tk.Frame(self.paned)
        ttk.Label(log_frame, text="运行日志",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(anchor=tk.W)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        paned.add(log_frame, weight=1)

        self.log_text.tag_config("info", foreground="#1F2937")
        self.log_text.tag_config("warning", foreground="#B45309")
        self.log_text.tag_config("error", foreground="#DC2626")

        self.root.after(100, self._poll_queues)

    # ------------------------------------------------------------------
    # 日志 / 状态（供执行线程调用，经队列转回主线程）
    # ------------------------------------------------------------------
    def log(self, text: str, level: str = "info"):
        self._log_queue.put((text, level))

    def node_status(self, node_id: str, state: str, message: str):
        self._status_queue.put((node_id, state, message))

    def popup(self, title: str, text: str):
        """执行线程的弹窗请求（经队列转回主线程，复用唯一弹窗）。"""
        self._popup_queue.put((title, text))

    def _poll_queues(self):
        try:
            while True:
                text, level = self._log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, text + "\n", level)
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        try:
            while True:
                node_id, state, message = self._status_queue.get_nowait()
                self.panel.update_node_status(node_id, state, message)
        except queue.Empty:
            pass
        try:
            while True:
                title, text = self._popup_queue.get_nowait()
                self.panel._show_popup(title, text)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queues)

    # ------------------------------------------------------------------
    # 运行控制
    # ------------------------------------------------------------------
    def on_run(self, graph):
        if self._stop_event is not None:
            return
        errors = graph.validate()
        if errors:
            messagebox.showerror("流程图校验失败", "\n".join(errors))
            return
        self.panel.set_running(True)
        self.log("========== 开始运行流程 ==========")
        self._stop_event = run_graph_async(
            graph,
            log=self.log,
            status=self.node_status,
            on_done=self._on_run_done,
            popup=self.popup,
        )

    def on_stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
            self.log("正在停止...", "warning")

    def _on_run_done(self, error, results):
        def _finish():
            self.panel.set_running(False)
            self._stop_event = None
            if error is None:
                self.log("========== 运行结束 ==========")
            else:
                self.log(f"========== 运行中断: {error} ==========", "error")
        self.root.after(0, _finish)

    # ------------------------------------------------------------------
    # 模板 / 导出
    # ------------------------------------------------------------------
    def _load_template(self):
        self.panel.set_graph(build_superposition_flow_template())
        self.log("已载入叠加调制标准流程模板")

    def _export_png(self):
        path = filedialog.asksaveasfilename(
            title="导出框图 PNG", defaultextension=".png",
            initialfile="superposition_flow.png",
            filetypes=[("PNG", "*.png")])
        if not path:
            return
        try:
            export_png(self.panel.graph, Path(path))
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self.log(f"框图已导出: {path}")


def main():
    matplotlib.use("Agg")  # 画图节点默认保存 PNG，不弹窗阻塞
    dpi_mode = set_dpi_aware()
    if dpi_mode == "failed":
        print("[WARN] DPI awareness 设置失败，高分辨率屏幕下界面可能发虚")
    root = tk.Tk()
    root.title("Superposed 16QAM 低代码编程平台")
    scale = set_tk_scaling(root)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{min(int(sw * 0.9), 1600)}x{min(int(sh * 0.9), 1000)}")

    paned = ttk.PanedWindow(root, orient=tk.VERTICAL)
    paned.pack(fill=tk.BOTH, expand=True)
    panel = FlowPanel(paned, scale=scale)
    app = LowCodeApp(root, scale, panel, paned)
    panel.on_log = app.log
    panel.on_run = app.on_run
    panel.on_stop = app.on_stop

    root.mainloop()


if __name__ == "__main__":
    main()
