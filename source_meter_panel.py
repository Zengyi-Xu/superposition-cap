# -*- coding: utf-8 -*-
"""Triple Keithley 2400 source-meter control panel for the superposition GUI.

This panel exposes three independent Keithley 2400 channels (SMU1 / SMU2 / SMU3)
so the user can bias multiple branches of the experiment (e.g. I/Q modulators
or LEDs) while keeping the same look-and-feel as the rest of the application.

The three control cards are arranged side-by-side; a shared communication log
is placed at the bottom.

The driver layer is provided by ``keithley2400_controller.py``, which is
ported from the DMT-NN project and supports both RS-232 and GPIB.
"""
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

import config as cfg
from keithley2400_controller import (
    Keithley2400,
    K2400ConnectionError,
    K2400CommandError,
    K2400ConfigError,
    refresh_port_list,
    parse_port_entry,
)


# Local colour constants so this module does not need to import superposition_gui.
COLOR_BG = "#F3F5F7"
COLOR_CARD = "#FFFFFF"
COLOR_BORDER = "#E2E8F0"
COLOR_TEXT = "#1F2937"
COLOR_TEXT_DIM = "#64748B"
COLOR_PRIMARY = "#164E63"

FONT_MONO = ("Liberation Mono", "DejaVu Sans Mono", "Consolas", "Courier New")


def _pick_font(candidates):
    """Return the first Tk font family that seems available, else the last."""
    for name in candidates:
        # Tk itself will fall back; we simply keep the first candidate.
        return name
    return candidates[-1] if candidates else "Courier New"


MONO_FONT = (_pick_font(FONT_MONO), 9)


def make_card(parent, **pack_kwargs):
    """White card container with a thin border."""
    card = tk.Frame(parent, bg=COLOR_CARD,
                    highlightbackground=COLOR_BORDER, highlightthickness=1, bd=0)
    if pack_kwargs:
        card.pack(**pack_kwargs)
    return card


class Keithley2400ChannelPanel(ttk.LabelFrame):
    """Single Keithley 2400 control card."""

    SOURCE_MODES = ["voltage", "current"]
    INTERFACES = ["rs232", "gpib"]
    BAUDRATES = [9600, 19200, 38400, 57600, 115200]

    def __init__(self, parent, app, title: str,
                 default_addr: str, default_interface: str,
                 default_mode: str, default_level: float,
                 default_compliance: float, default_nplc: float,
                 log_callback=None):
        super().__init__(parent, text=f"  {title}  ", padding=8)
        self.app = app
        self.instrument: Optional[Keithley2400] = None
        self._log_callback = log_callback

        self.port_var = tk.StringVar(value=default_addr)
        self.interface_var = tk.StringVar(value=default_interface)
        self.source_mode_var = tk.StringVar(value=default_mode)
        self.level_var = tk.StringVar(value=str(default_level))
        self.compliance_var = tk.StringVar(value=str(default_compliance))
        self.nplc_var = tk.StringVar(value=str(default_nplc))
        self.auto_range_var = tk.BooleanVar(value=True)
        self.output_var = tk.BooleanVar(value=False)

        self._build_ui()
        self._on_mode_change()
        self._on_interface_change()
        self._refresh_ports()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.configure(style="TLabelframe")

        # Connection card
        conn_card = make_card(self)
        conn_card.pack(fill=tk.X, pady=(0, 8))
        inner = tk.Frame(conn_card, bg=COLOR_CARD)
        inner.pack(fill=tk.X, padx=8, pady=8)

        ttk.Label(inner, text="通信接口", style="Section.TLabel"
                  ).grid(row=0, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.interface_combo = ttk.Combobox(
            inner, textvariable=self.interface_var,
            values=self.INTERFACES, state="readonly", width=10)
        self.interface_combo.bind("<<ComboboxSelected>>", self._on_interface_change)
        self.interface_combo.grid(row=0, column=1, sticky=tk.W, padx=(0, 8), pady=4)

        self.port_lbl = ttk.Label(inner, text="串口", style="Section.TLabel")
        self.port_lbl.grid(row=1, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.port_combo = ttk.Combobox(inner, textvariable=self.port_var,
                                       values=[], width=18, state="readonly")
        self.port_combo.grid(row=1, column=1, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Button(inner, text="⟳", width=4, command=self._refresh_ports
                   ).grid(row=1, column=2, padx=(0, 6), pady=4)

        self.baud_lbl = ttk.Label(inner, text="波特率", style="Section.TLabel")
        self.baud_lbl.grid(row=2, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.baud_combo = ttk.Combobox(inner, values=self.BAUDRATES,
                                       width=10, state="readonly")
        self.baud_combo.set(str(cfg.SMU_BAUDRATE))
        self.baud_combo.grid(row=2, column=1, sticky=tk.W, padx=(0, 8), pady=4)

        self.conn_btn = ttk.Button(inner, text="连接", width=8,
                                   command=self._toggle_connect)
        self.conn_btn.grid(row=3, column=0, padx=(0, 6), pady=4)

        self.status_lbl = ttk.Label(inner, text="未连接", foreground=COLOR_TEXT_DIM)
        self.status_lbl.grid(row=3, column=1, sticky=tk.W, padx=(0, 8), pady=4)

        # Source settings card
        set_card = make_card(self)
        set_card.pack(fill=tk.X, pady=(0, 8))
        inner2 = tk.Frame(set_card, bg=COLOR_CARD)
        inner2.pack(fill=tk.X, padx=8, pady=8)

        ttk.Label(inner2, text="源模式", style="Section.TLabel"
                  ).grid(row=0, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.mode_combo = ttk.Combobox(inner2, textvariable=self.source_mode_var,
                                       values=self.SOURCE_MODES, state="readonly", width=10)
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)
        self.mode_combo.grid(row=0, column=1, sticky=tk.W, padx=(0, 4), pady=4)

        ttk.Label(inner2, text="输出电平", style="Section.TLabel"
                  ).grid(row=1, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.level_entry = ttk.Entry(inner2, textvariable=self.level_var, width=10)
        self.level_entry.grid(row=1, column=1, sticky=tk.W, padx=(0, 4), pady=4)
        self.level_unit_lbl = ttk.Label(inner2, text="V")
        self.level_unit_lbl.grid(row=1, column=2, sticky=tk.W, pady=4)

        ttk.Label(inner2, text="合规限值", style="Section.TLabel"
                  ).grid(row=2, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.comp_entry = ttk.Entry(inner2, textvariable=self.compliance_var, width=10)
        self.comp_entry.grid(row=2, column=1, sticky=tk.W, padx=(0, 4), pady=4)
        self.comp_unit_lbl = ttk.Label(inner2, text="A")
        self.comp_unit_lbl.grid(row=2, column=2, sticky=tk.W, pady=4)

        ttk.Label(inner2, text="NPLC", style="Section.TLabel"
                  ).grid(row=3, column=0, sticky=tk.W, padx=(0, 6), pady=4)
        self.nplc_entry = ttk.Entry(inner2, textvariable=self.nplc_var, width=10)
        self.nplc_entry.grid(row=3, column=1, sticky=tk.W, padx=(0, 4), pady=4)

        ttk.Checkbutton(inner2, text="自动量程", variable=self.auto_range_var
                        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=4)
        ttk.Button(inner2, text="应用设置", command=self._apply_settings
                   ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))

        # Output / measure card
        out_card = make_card(self)
        out_card.pack(fill=tk.X, pady=(0, 8))
        inner3 = tk.Frame(out_card, bg=COLOR_CARD)
        inner3.pack(fill=tk.X, padx=8, pady=8)

        self.out_btn = ttk.Button(inner3, text="输出 ON", width=10,
                                  command=self._toggle_output)
        self.out_btn.grid(row=0, column=0, padx=(0, 6), pady=4)
        ttk.Button(inner3, text="测量", width=10, command=self._measure
                   ).grid(row=1, column=0, padx=(0, 6), pady=4)
        ttk.Button(inner3, text="复位", width=10, command=self._reset
                   ).grid(row=2, column=0, padx=(0, 6), pady=4)

        self.last_measure_lbl = ttk.Label(
            inner3, text="上次测量：--", style="Section.TLabel", wraplength=220)
        self.last_measure_lbl.grid(row=3, column=0, sticky=tk.W, pady=(8, 0))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _log(self, text: str, tag: Optional[str] = None):
        if self._log_callback:
            self._log_callback(text, tag)
        # 没有回调时静默丢弃，避免独立使用时异常

    def _refresh_ports(self):
        try:
            ports = refresh_port_list(interface=self.interface_var.get())
        except Exception as exc:
            self._log(f"端口枚举失败：{exc}", "err")
            ports = []
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(parse_port_entry(ports[0]))
        elif ports:
            current = parse_port_entry(self.port_var.get())
            matching = [p for p in ports if parse_port_entry(p) == current]
            if not matching:
                self.port_var.set(parse_port_entry(ports[0]))

    def _on_mode_change(self, _event=None):
        mode = self.source_mode_var.get()
        if mode == "voltage":
            self.level_unit_lbl.configure(text="V")
            self.comp_unit_lbl.configure(text="A")
        else:
            self.level_unit_lbl.configure(text="A")
            self.comp_unit_lbl.configure(text="V")

    def _on_interface_change(self, _event=None):
        interface = self.interface_var.get().lower()
        if interface == "gpib":
            self.port_lbl.configure(text="GPIB 资源")
            self.baud_lbl.configure(text="波特率（GPIB 无效）")
            self.baud_combo.configure(state="disabled")
        else:
            self.port_lbl.configure(text="串口")
            self.baud_lbl.configure(text="波特率")
            self.baud_combo.configure(state="readonly")
        self._refresh_ports()

    # ------------------------------------------------------------------
    # Instrument control
    # ------------------------------------------------------------------
    def _toggle_connect(self):
        if self.instrument is not None and self.instrument.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = parse_port_entry(self.port_var.get())
        if not port:
            messagebox.showwarning("提示", "请先选择串口或 GPIB 资源。")
            return
        baud = int(self.baud_combo.get() or cfg.SMU_BAUDRATE)
        try:
            self.instrument = Keithley2400(
                port=port, baudrate=baud, timeout=cfg.SMU_TIMEOUT,
                interface=self.interface_var.get())
            self.instrument.connect()
            self.conn_btn.configure(text="断开")
            self.status_lbl.configure(text=f"已连接 ({port})", foreground="green")
            self._log(f"已连接 {port}，接口={self.interface_var.get()}，波特率={baud}")
            self._apply_settings()
        except K2400ConnectionError as exc:
            messagebox.showerror("连接失败", str(exc))
            self._log(f"连接失败：{exc}", "err")
            self.instrument = None
        except Exception as exc:
            messagebox.showerror("连接错误", str(exc))
            self._log(f"连接错误：{exc}", "err")
            self.instrument = None

    def _disconnect(self):
        if self.instrument is not None:
            try:
                self.instrument.disconnect()
            except Exception as exc:
                self._log(f"断开连接出错：{exc}", "err")
            finally:
                self.instrument = None
        self.conn_btn.configure(text="连接")
        self.status_lbl.configure(text="未连接", foreground=COLOR_TEXT_DIM)
        self.output_var.set(False)
        self.out_btn.configure(text="输出 ON")
        self._log("已断开")

    def _apply_settings(self):
        if self.instrument is None or not self.instrument.connected:
            self._log("未连接，设置未应用。")
            return
        try:
            mode = self.source_mode_var.get()
            level = float(self.level_var.get())
            compliance = float(self.compliance_var.get())
            nplc = float(self.nplc_var.get())
            auto_range = self.auto_range_var.get()

            self.instrument.set_source_mode(mode)
            self.instrument.set_compliance(compliance)
            self.instrument.set_nplc(nplc)
            self.instrument.set_range(auto=auto_range)
            self.instrument.set_output_level(level)

            self._log(f"设置已应用：{mode}，level={level}, "
                      f"compliance={compliance}, NPLC={nplc}, auto_range={auto_range}")
        except ValueError:
            messagebox.showerror("数值无效", "电平、限值和 NPLC 必须是数字。")
        except K2400ConfigError as exc:
            messagebox.showerror("配置错误", str(exc))
        except Exception as exc:
            messagebox.showerror("应用设置失败", str(exc))
            self._log(f"应用设置失败：{exc}", "err")

    def _toggle_output(self):
        if self.instrument is None or not self.instrument.connected:
            self._log("未连接。")
            return
        try:
            if self.output_var.get():
                self.instrument.output_off()
                self.output_var.set(False)
                self.out_btn.configure(text="输出 ON")
                self._log("输出 OFF")
            else:
                self.instrument.output_on()
                self.output_var.set(True)
                self.out_btn.configure(text="输出 OFF")
                self._log("输出 ON")
        except Exception as exc:
            messagebox.showerror("输出控制失败", str(exc))
            self._log(f"输出控制失败：{exc}", "err")

    def _measure(self):
        if self.instrument is None or not self.instrument.connected:
            self._log("未连接。")
            return
        try:
            data = self.instrument.measure()
            text = (f"V={data['voltage']:.6e} V, "
                    f"I={data['current']:.6e} A, "
                    f"R={data['resistance']:.6e} Ω, "
                    f"t={data['timestamp']:.6f} s")
            self.last_measure_lbl.configure(text=f"上次测量：{text}")
            self._log(f"测量：{text}")
        except K2400CommandError as exc:
            messagebox.showerror("测量失败", str(exc))
            self._log(f"测量失败：{exc}", "err")
        except Exception as exc:
            messagebox.showerror("测量错误", str(exc))
            self._log(f"测量错误：{exc}", "err")

    def _reset(self):
        if self.instrument is None or not self.instrument.connected:
            self._log("未连接。")
            return
        try:
            self.instrument.reset()
            self._log("仪器已复位。")
            self._apply_settings()
        except Exception as exc:
            messagebox.showerror("复位失败", str(exc))
            self._log(f"复位失败：{exc}", "err")

    def on_close(self):
        """Safely turn off output and disconnect when the GUI closes."""
        self._disconnect()


class DualKeithley2400Panel(ttk.Frame):
    """Notebook page that hosts three Keithley2400ChannelPanel instances
    side-by-side, with a shared communication log at the bottom."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

        # 上方：三列源表控制卡
        top_frame = tk.Frame(self, bg=COLOR_BG)
        top_frame.pack(fill=tk.BOTH, expand=True)

        self.smu1 = Keithley2400ChannelPanel(
            top_frame, app, "源表 1 (SMU1)",
            default_addr=cfg.SMU1_ADDR,
            default_interface=cfg.SMU1_INTERFACE,
            default_mode=cfg.SMU1_SOURCE_MODE,
            default_level=cfg.SMU1_LEVEL,
            default_compliance=cfg.SMU1_COMPLIANCE,
            default_nplc=cfg.SMU1_NPLC,
            log_callback=self._log,
        )
        self.smu1.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                       padx=(0, 6), pady=(0, 6))

        self.smu2 = Keithley2400ChannelPanel(
            top_frame, app, "源表 2 (SMU2)",
            default_addr=cfg.SMU2_ADDR,
            default_interface=cfg.SMU2_INTERFACE,
            default_mode=cfg.SMU2_SOURCE_MODE,
            default_level=cfg.SMU2_LEVEL,
            default_compliance=cfg.SMU2_COMPLIANCE,
            default_nplc=cfg.SMU2_NPLC,
            log_callback=self._log,
        )
        self.smu2.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                       padx=(0, 6), pady=(0, 6))

        self.smu3 = Keithley2400ChannelPanel(
            top_frame, app, "源表 3 (SMU3)",
            default_addr=cfg.SMU3_ADDR,
            default_interface=cfg.SMU3_INTERFACE,
            default_mode=cfg.SMU3_SOURCE_MODE,
            default_level=cfg.SMU3_LEVEL,
            default_compliance=cfg.SMU3_COMPLIANCE,
            default_nplc=cfg.SMU3_NPLC,
            log_callback=self._log,
        )
        self.smu3.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                       padx=(0, 0), pady=(0, 6))

        # 下方：共享通信日志
        log_card = make_card(self)
        log_card.pack(fill=tk.X, pady=(6, 0))
        log_inner = tk.Frame(log_card, bg=COLOR_CARD)
        log_inner.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        ttk.Label(log_inner, text="通信日志", style="Section.TLabel"
                  ).pack(anchor=tk.W)
        self.log_text = tk.Text(log_inner, height=8, wrap=tk.WORD, font=MONO_FONT,
                                bg="#FAFAFA", fg=COLOR_TEXT, relief=tk.FLAT,
                                highlightbackground=COLOR_BORDER, highlightthickness=1)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.log_text.configure(state=tk.DISABLED)

        self._log("提示：选择通信接口与端口后点击“连接”。")

    def _log(self, text: str, tag: Optional[str] = None):
        self.log_text.configure(state=tk.NORMAL)
        prefix = ""
        if tag == "err":
            prefix = "[ERR] "
        self.log_text.insert(tk.END, prefix + text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def on_close(self):
        self.smu1.on_close()
        self.smu2.on_close()
        self.smu3.on_close()
