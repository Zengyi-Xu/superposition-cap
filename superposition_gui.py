# -*- coding: utf-8 -*-
"""Superposed 16QAM Communication System Experiment Platform GUI.

Six tabs:
    1. Waveform & Spectrum    —— TX/RX time-domain waveforms and spectra
    2. Superposition Modulation —— 36QAM constellation and density plots
    3. Transmission Results   —— experiment record table and run-history trend
    4. Run Test               —— run the transceiver from the GUI with live log output
    5. Oscilloscope           —— TCP/IP control of the Keysight oscilloscope
    6. Source Meters          —— dual Keithley 2400 bias/control (SMU1 / SMU2)

Data sources:
    data/records/record_<run_id>.json          parameters and results for each run
    data/txdata/*_<run_id>.txt                 saved transmit waveforms / symbols
    data/rxdata/rx_<run_id>.txt                synchronized receive waveform
    data/rxdata/eq_<run_id>.txt                equalized complex symbols (I,Q columns)

How to run (from the project directory):
    python superposition_gui.py

Depends only on numpy / scipy / matplotlib / tkinter (bundled with Python).
"""
import json
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from typing import Optional

import numpy as np
import matplotlib

matplotlib.use("TkAgg")
import matplotlib.pyplot as plt

import config as cfg
import datetime
import main as main_flow
import superposition_core as core
import awg_m8190a
import optimizer
import instrument_discovery as instr_disc
from record import generate_run_id, save_record
from source_meter_panel import DualKeithley2400Panel
from settings import load_settings, save_settings

try:
    import openpyxl
    _HAS_OPENPYXL = True
except Exception:
    _HAS_OPENPYXL = False

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

# Preferred UI / plot sans-serif font
UI_FONT = "Liberation Sans"
UI_FONT_FALLBACKS = ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "DejaVu Sans", "Liberation Sans"]

# Preferred monospace font for code/log areas
MONO_FONT = "Liberation Mono Bold"
MONO_FONT_FALLBACKS = ["Liberation Mono", "DejaVu Sans Mono", "Courier"]

# 绘图字体必须支持中文
PLOT_FONT_CANDIDATES = [
    "Microsoft YaHei", "SimHei", "SimSun", "STSong",
    "WenQuanYi Micro Hei", "Noto Sans CJK SC", "Source Han Sans SC",
    "DejaVu Sans",
]

import matplotlib.font_manager as fm


def _pick_available_font(candidates):
    """Return the first font from candidates that exists on the system."""
    for name in candidates:
        try:
            fm.findfont(name, fallback_to_default=False)
            return name
        except Exception:
            continue
    return candidates[-1] if candidates else "DejaVu Sans"


FONT_FAMILY = _pick_available_font([UI_FONT] + UI_FONT_FALLBACKS)
FONT_MONO = _pick_available_font([MONO_FONT] + MONO_FONT_FALLBACKS)
PLOT_FONT = _pick_available_font(PLOT_FONT_CANDIDATES)

plt.rcParams["font.sans-serif"] = [PLOT_FONT] + [f for f in PLOT_FONT_CANDIDATES if f != PLOT_FONT]
plt.rcParams["axes.unicode_minus"] = False

PROJECT_ROOT = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_ROOT / "data" / "plots"
RECORDS_DIR = cfg.RECORD_DIR
TXDATA_DIR = cfg.TXDATA_DIR
RXDATA_DIR = cfg.RXDATA_DIR

APP_EMOJI = "🔀"  # emoji used for window icon and title

# 加载用户保存的地址/端口默认值（覆盖 config.py 中的硬编码）
_USER_SETTINGS = load_settings()


def _persist_addresses(osc_addr: str = None, awg_addr: str = None,
                       osc_channel: str = None, osc_dual: bool = None) -> None:
    """把当前 OSC/AWG 地址、OSC 通道/双通道状态保存到 data/settings.json。"""
    updates = {}
    if osc_addr is not None:
        updates["OSC_VISA_ADDR"] = osc_addr
    if awg_addr is not None:
        updates["AWG_VISA_ADDR"] = awg_addr
    if osc_channel is not None:
        updates["OSC_CHANNEL"] = osc_channel
    if osc_dual is not None:
        updates["OSC_DUAL"] = bool(osc_dual)
    if updates:
        save_settings(updates)


def _setting_bool(key: str, default: bool = False) -> bool:
    """从已保存设置中读取布尔值，兼容旧版字符串 'True'/'False'。"""
    value = _USER_SETTINGS.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _setting_int(key: str, default: int = 0) -> int:
    """从已保存设置中读取整数。"""
    value = _USER_SETTINGS.get(key, default)
    try:
        return int(value)
    except Exception:
        return default


def _setting_float(key: str, default: float = 0.0) -> float:
    """从已保存设置中读取浮点数。"""
    value = _USER_SETTINGS.get(key, default)
    try:
        return float(value)
    except Exception:
        return default


def _setting_str(key: str, default: str = "") -> str:
    """从已保存设置中读取字符串。"""
    value = _USER_SETTINGS.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _persist_setting(key: str, value) -> None:
    """保存单个运行参数到 data/settings.json。"""
    save_settings({key: value})


def _rate_info(rec) -> tuple:
    """从记录提取 (速率 Mbps, AWG 采样率 MSa/s, 上采样倍数, 带宽 MHz)，兼容旧记录。"""
    rate = rec.get("data_rate_mbps", 0) or 0
    awg = rec.get("awg_sample_rate_ms", 0) or 0
    up = rec.get("upsampleno", 0) or cfg.UPSAMPLENO
    bw = rec.get("bandwidth_mhz", 0) or (awg / up if awg else 0)
    return rate, awg, up, bw


def _rate_cell(rec) -> str:
    """结果表格“速率”列：速率 (采样率/×上采样/带宽) 同一列展示。"""
    rate, awg, up, bw = _rate_info(rec)
    if not rate:
        return "N/A"
    if awg and bw:
        return f"{rate:.0f} ({awg:.0f}/×{up}/{bw:.0f}M)"
    return f"{rate:.0f}"


# ═══════════════════════════════════════════════════════════════════════════════
# High-DPI adaptation (must be called before creating Tk)
# ═══════════════════════════════════════════════════════════════════════════════

def enable_dpi_awareness():
    """Render Windows at actual DPI to avoid tiny/blurry UI on high-DPI screens."""
    try:
        import ctypes
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Color palette (modern light dictionary style)
# ═══════════════════════════════════════════════════════════════════════════════

COLOR_BG = "#F3F5F7"          # window background (light gray)
COLOR_CARD = "#FFFFFF"        # card white
COLOR_BORDER = "#E2E8F0"      # card border
COLOR_PRIMARY = "#164E63"     # primary color (dark cyan)
COLOR_PRIMARY_HOVER = "#0E7490"
COLOR_TEXT = "#1F2937"        # primary text
COLOR_TEXT_DIM = "#64748B"    # secondary text
COLOR_DANGER = "#B91C1C"
COLOR_SELECT = "#164E63"      # selection color


# ═══════════════════════════════════════════════════════════════════════════════
# Data discovery
# ═══════════════════════════════════════════════════════════════════════════════

# Cache updated by SuperpositionGuiApp.reload_data so plot builders can locate record params.
_RECORD_BY_RUN = {}


def set_record_by_run(mapping):
    _RECORD_BY_RUN.clear()
    _RECORD_BY_RUN.update(mapping)


def _get_record(run_id):
    return _RECORD_BY_RUN.get(run_id, {})


def _record_tx_path(rec):
    """Return the saved TX (I+Q sum) waveform path for a record."""
    p = rec.get("txsum_path")
    if p:
        return Path(p)
    return TXDATA_DIR / f"txsum_{rec.get('run_id', '')}.txt"


def _record_rx_path(rec):
    """Return the saved RX waveform path for a record."""
    p = rec.get("rx_path")
    if p:
        return Path(p)
    return RXDATA_DIR / f"rx_{rec.get('run_id', '')}.txt"


def _record_eq_path(rec):
    """Return the saved equalized-symbol (I,Q) path for a record."""
    p = rec.get("eq_path")
    if p:
        return Path(p)
    return RXDATA_DIR / f"eq_{rec.get('run_id', '')}.txt"


def available_plots(run_id, names):
    """Return plot names that actually have data for this run (preserving names order)."""
    rec = _get_record(run_id)
    if not rec:
        return []
    has_tx = _record_tx_path(rec).is_file()
    has_rx = _record_rx_path(rec).is_file()
    has_eq = _record_eq_path(rec).is_file()
    out = []
    for n in names:
        if n in ("tx_waveform", "tx_spectrum", "tx_constellation", "constellation_density"):
            if has_tx:
                out.append(n)
        elif n in ("rx_waveform", "rx_spectrum"):
            if has_rx or has_tx:
                out.append(n)
        elif n == "rx_constellation":
            if has_eq:
                out.append(n)
    return out


def _load_signal(path):
    if not path.is_file():
        return None
    try:
        return np.loadtxt(path)
    except Exception:
        return None


def _load_run_signals(run_id):
    """Load TX/RX signals and metadata for a run_id."""
    rec = _get_record(run_id)
    tx_path = _record_tx_path(rec)
    rx_path = _record_rx_path(rec)
    tx = _load_signal(tx_path)
    rx = _load_signal(rx_path)
    if tx is None:
        tx = np.array([])
    if rx is None:
        rx = tx
    fs = cfg.AWG_SAMPLE * 1e6  # 发射/接收波形保存在 AWG 采样率下
    return {"tx": tx, "rx": rx, "fs": fs, "rec": rec,
            "has_rx": rx_path.is_file() and rx is not tx}


def _regen_symbols(rec, max_count=5000):
    """Regenerate the TX constellation (v1 + 1j*v2) for a record."""
    seed = int(rec.get("seed", cfg.SEED_BAND1))
    datano = int(rec.get("datano", cfg.DATANO))
    mode = rec.get("modulation_mode", core.MODULATION_36QAM)
    v1, v2, _, _ = core.generate_symbols(
        mode, min(datano, max_count), seed, seed + 100,
        upsampleno=int(rec.get("upsampleno", cfg.UPSAMPLENO)))
    return v1 + 1j * v2


def _load_eq_symbols(rec):
    """Load equalized complex symbols and strip the LMS tap edges."""
    eq = _load_signal(_record_eq_path(rec))
    if eq is None:
        return None
    iq = eq[:, 0] + 1j * eq[:, 1]
    taps = int(rec.get("lms_taps", cfg.LMS_TAPS))
    return iq[taps - 1: len(iq) - taps]


def _safe_log10(x):
    return 10 * np.log10(np.maximum(np.asarray(x, dtype=float), 1e-12))


def list_records():
    """Read all record_*.json under data/records and return sorted by timestamp ascending."""
    records = []
    if RECORDS_DIR.is_dir():
        for p in sorted(RECORDS_DIR.glob("record_*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                records.append(rec)
            except (json.JSONDecodeError, OSError):
                continue
    records.sort(key=lambda r: r.get("timestamp", ""))
    return records


# ═══════════════════════════════════════════════════════════════════════════════
# Plotting functions
# ═══════════════════════════════════════════════════════════════════════════════

def _spectrum(sig, fs):
    nfft = 2 ** int(np.ceil(np.log2(min(len(sig), 8192))))
    f = (np.arange(nfft) - nfft // 2) * fs / nfft
    spec = _safe_log10(np.abs(np.fft.fftshift(np.fft.fft(sig[:nfft]))))
    return f, spec


def build_tx_waveform(fig, run_id, title):
    data = _load_run_signals(run_id)
    ax = fig.add_subplot(111)
    tx = data["tx"]
    if len(tx):
        n = min(len(tx), 2000)
        t = np.arange(n) / data["fs"] * 1e6
        ax.plot(t, tx[:n], lw=0.8)
    ax.set_title(title)
    ax.set_xlabel("时间 (us)")
    ax.set_ylabel("幅度")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()


def build_rx_waveform(fig, run_id, title):
    data = _load_run_signals(run_id)
    ax = fig.add_subplot(111)
    rx = data["rx"]
    if len(rx):
        n = min(len(rx), 2000)
        t = np.arange(n) / data["fs"] * 1e6
        ax.plot(t, rx[:n], lw=0.8, color="#B91C1C")
    ax.set_title(title)
    ax.set_xlabel("时间 (us)")
    ax.set_ylabel("幅度")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()


def build_tx_spectrum(fig, run_id, title):
    data = _load_run_signals(run_id)
    ax = fig.add_subplot(111)
    if len(data["tx"]):
        f, spec = _spectrum(data["tx"], data["fs"])
        ax.plot(f / 1e6, spec, lw=0.8)
    ax.set_title(title)
    ax.set_xlabel("频率 (MHz)")
    ax.set_ylabel("幅度 (dB)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()


def build_rx_spectrum(fig, run_id, title):
    data = _load_run_signals(run_id)
    ax = fig.add_subplot(111)
    if len(data["rx"]):
        f, spec = _spectrum(data["rx"], data["fs"])
        ax.plot(f / 1e6, spec, lw=0.8, color="#B91C1C")
    ax.set_title(title)
    ax.set_xlabel("频率 (MHz)")
    ax.set_ylabel("幅度 (dB)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()


def build_tx_constellation(fig, run_id, title):
    rec = _get_record(run_id)
    iq = _regen_symbols(rec)
    ax = fig.add_subplot(111)
    if iq is not None and len(iq):
        ax.plot(iq.real, iq.imag, "b.", alpha=0.4, markersize=4)
    ax.set_title(title)
    ax.set_xlabel("同相 I")
    ax.set_ylabel("正交 Q")
    ax.grid(True, alpha=0.3)
    ax.axis("equal")
    fig.tight_layout()


def _constellation_axis_limit(mode: str) -> int:
    """根据调制模式返回星座图坐标轴范围。"""
    if mode == core.MODULATION_36QAM:
        return int(np.log2(cfg.PAM_ORDER)) * 2 + 2  # 6
    params = core.get_modulation_params(mode)
    return int(np.max(np.abs(params["levels"]))) + 2


def build_rx_constellation(fig, run_id, title):
    rec = _get_record(run_id)
    iq = _load_eq_symbols(rec)
    mode = rec.get("modulation_mode", core.MODULATION_36QAM)
    ax = fig.add_subplot(111)
    if iq is not None and len(iq):
        ax.plot(iq.real, iq.imag, "b.", alpha=0.4, markersize=4)
        maxaxis = _constellation_axis_limit(mode)
        ax.set_xlim(-maxaxis, maxaxis)
        ax.set_ylim(-maxaxis, maxaxis)
    ax.set_title(title)
    ax.set_xlabel("同相 I")
    ax.set_ylabel("正交 Q")
    ax.grid(True, alpha=0.3)
    ax.axis("equal")
    fig.tight_layout()


def build_constellation_density(fig, run_id, title):
    rec = _get_record(run_id)
    iq = _load_eq_symbols(rec)
    ax = fig.add_subplot(111)
    if iq is not None and len(iq):
        hb = ax.hexbin(iq.real, iq.imag, gridsize=80, cmap="GnBu", mincnt=1)
        fig.colorbar(hb, ax=ax, label="密度")
    ax.set_title(title)
    ax.set_xlabel("同相 I")
    ax.set_ylabel("正交 Q")
    ax.axis("equal")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()


def build_records_trend(fig, records, title):
    """Trend across runs: SNR and BER."""
    if not records:
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, "暂无实验记录", ha="center", va="center",
                transform=ax.transAxes)
        return
    xs = list(range(len(records)))
    labels = [r.get("run_id", "")[-6:] for r in records]
    snrs = [r.get("snr_db", 0) for r in records]

    def _ber(rec):
        return rec.get("ber_avg", np.nan)

    bers1 = [max(_ber(r) or 1e-12, 1e-12) for r in records]
    bers2 = [max(r.get("ber_band2", np.nan) or 1e-12, 1e-12) for r in records]

    ax1 = fig.add_subplot(211)
    ax1.plot(xs, snrs, "g-o", markersize=4, linewidth=1.5, label="SNR (dB)")
    ax1.set_ylabel("SNR (dB)", color="g")
    ax1.set_title(title)
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)
    ax1.set_xticks(xs)
    ax1.set_xticklabels(labels, rotation=45, fontsize=8)

    ax2 = fig.add_subplot(212, sharex=ax1)
    ax2.semilogy(xs, bers1, "r-x", markersize=4, linewidth=1.5, label="平均 BER")
    ax2.semilogy(xs, bers2, "b-s", markersize=4, linewidth=1.5, label="带2 BER")
    ax2.set_xlabel("实验（按时间顺序）")
    ax2.set_ylabel("误码率")
    ax2.legend()
    ax2.grid(True, which="both", ls="--", alpha=0.3)
    ax2.set_xticks(xs)
    ax2.set_xticklabels(labels, rotation=45, fontsize=8)
    fig.tight_layout()


# ═══════════════════════════════════════════════════════════════════════════════
# Figure catalog: name -> (plot function, title)
# ═══════════════════════════════════════════════════════════════════════════════

FIGURES = {
    # Tab 1: Waveform & Spectrum
    "tx_waveform":   (build_tx_waveform,   "发射时域波形（两路叠加）"),
    "rx_waveform":   (build_rx_waveform,   "接收时域波形（同步后）"),
    "tx_spectrum":   (build_tx_spectrum,   "发射频谱"),
    "rx_spectrum":   (build_rx_spectrum,   "接收频谱"),
    # Tab 2: Superposition Modulation
    "tx_constellation":      (build_tx_constellation,      "发射星座图"),
    "rx_constellation":      (build_rx_constellation,      "接收星座图（MIMO LMS 均衡后）"),
    "constellation_density": (build_constellation_density, "接收星座密度图"),
}

TAB_WAVEFORM = [
    "tx_waveform", "rx_waveform", "tx_spectrum", "rx_spectrum",
]
TAB_MODULATION = [
    "tx_constellation", "rx_constellation", "constellation_density",
]
TAB_RESULTS = [
    "rx_constellation", "constellation_density",
]

TREND_KEY = "__records_trend__"   # virtual plot name for run-history trend inside tab 3


# ═══════════════════════════════════════════════════════════════════════════════
# Styles
# ═══════════════════════════════════════════════════════════════════════════════

def apply_styles(root, scale):
    """Modern light theme (customized on top of clam)."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    f = max(scale, 1.0)
    font_base = (FONT_FAMILY, 11)
    font_bold = (FONT_FAMILY, 11, "bold")
    font_tab = (FONT_FAMILY, 12)
    pad_x = int(round(12 * f))
    pad_y = int(round(6 * f))

    style.configure(".", font=font_base, background=COLOR_BG,
                    foreground=COLOR_TEXT)

    style.configure("TFrame", background=COLOR_BG)
    style.configure("Card.TFrame", background=COLOR_CARD)
    style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
    style.configure("Card.TLabel", background=COLOR_CARD,
                    foreground=COLOR_TEXT)
    style.configure("Dim.TLabel", background=COLOR_BG,
                    foreground=COLOR_TEXT_DIM)
    style.configure("DimCard.TLabel", background=COLOR_CARD,
                    foreground=COLOR_TEXT_DIM)
    style.configure("Title.TLabel", background=COLOR_BG,
                    foreground=COLOR_PRIMARY,
                    font=(FONT_FAMILY, 18, "bold"))
    style.configure("Subtitle.TLabel", background=COLOR_BG,
                    foreground=COLOR_TEXT_DIM,
                    font=(FONT_FAMILY, 11))
    style.configure("Section.TLabel", background=COLOR_CARD,
                    foreground=COLOR_PRIMARY, font=font_bold)
    style.configure("Pill.TLabel", background=COLOR_PRIMARY,
                    foreground="#FFFFFF", font=font_bold,
                    padding=(pad_x, int(round(4 * f))))
    style.configure("Metrics.TLabel", background=COLOR_BG,
                    foreground=COLOR_PRIMARY_HOVER, font=font_bold)

    style.configure("TNotebook", background=COLOR_BG, borderwidth=0)
    style.configure("TNotebook.Tab", font=font_tab,
                    padding=(pad_x + 6, pad_y),
                    background="#E5EAEF", foreground=COLOR_TEXT)
    style.map("TNotebook.Tab",
              background=[("selected", COLOR_CARD)],
              foreground=[("selected", COLOR_PRIMARY)])

    style.configure("Accent.TButton", font=font_bold,
                    padding=(pad_x, pad_y),
                    background=COLOR_PRIMARY, foreground="#FFFFFF",
                    borderwidth=0, focusthickness=0)
    style.map("Accent.TButton",
              background=[("active", COLOR_PRIMARY_HOVER),
                          ("disabled", "#9FB3BC")],
              foreground=[("disabled", "#E5EAEF")])
    style.configure("TButton", font=font_base, padding=(pad_x, pad_y),
                    background="#E5EAEF", foreground=COLOR_TEXT,
                    borderwidth=0)
    style.map("TButton", background=[("active", "#D5DDE4")])
    style.configure("Danger.TButton", font=font_bold,
                    padding=(pad_x, pad_y),
                    background=COLOR_DANGER, foreground="#FFFFFF",
                    borderwidth=0)
    style.map("Danger.TButton",
              background=[("active", "#DC2626"), ("disabled", "#D1A5A5")])

    indicator = int(round(13 * f))
    style.configure("TRadiobutton", background=COLOR_CARD,
                    foreground=COLOR_TEXT, font=font_base,
                    indicatorsize=indicator)
    style.configure("TCheckbutton", background=COLOR_CARD,
                    foreground=COLOR_TEXT, font=font_base,
                    indicatorsize=indicator)
    style.configure("TLabelframe", background=COLOR_CARD,
                    bordercolor=COLOR_BORDER)
    style.configure("TLabelframe.Label", background=COLOR_CARD,
                    foreground=COLOR_PRIMARY, font=font_bold)

    style.configure("TCombobox", padding=(int(round(8 * f)),
                                          int(round(4 * f))))

    style.configure("Treeview", background=COLOR_CARD,
                    fieldbackground=COLOR_CARD, foreground=COLOR_TEXT,
                    rowheight=int(round(30 * f)), font=font_base,
                    borderwidth=0)
    style.configure("Treeview.Heading", background="#EEF2F5",
                    foreground=COLOR_PRIMARY, font=font_bold,
                    padding=(int(round(6 * f)), int(round(6 * f))))
    style.map("Treeview",
              background=[("selected", COLOR_SELECT)],
              foreground=[("selected", "#FFFFFF")])

    style.configure("TSeparator", background=COLOR_BORDER)
    style.configure("Vertical.TScrollbar", background="#D5DDE4",
                    troughcolor=COLOR_BG, borderwidth=0, arrowsize=12)


def make_card(parent, **pack_kwargs):
    """White card container (thin border + padding)."""
    card = tk.Frame(parent, bg=COLOR_CARD,
                    highlightbackground=COLOR_BORDER, highlightthickness=1,
                    bd=0)
    if pack_kwargs:
        card.pack(**pack_kwargs)
    return card


def _set_windows_taskbar_icon():
    """Set Windows taskbar icon: an explicit AppUserModelID is needed to escape the default feather icon."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        app_id = "SUPERPOSED.PAM16QAM.GUI.v1"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def _create_emoji_icon(emoji: str, size: int = 64):
    """Render an emoji into a window icon, returning (PhotoImage, ico_path)."""
    if not _HAS_PIL:
        return None, None
    try:
        img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        draw = ImageDraw.Draw(img)
        font = None
        for font_name, font_size in [("LiberationSans-Regular.ttf", size - 8),
                                      ("DejaVuSans.ttf", size - 8),
                                      ("seguiemj.ttf", size - 8),
                                      ("segoe ui emoji.ttf", size - 8),
                                      ("arial.ttf", size - 8)]:
            try:
                font = ImageFont.truetype(font_name, font_size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), emoji, font=font)
        x = (size - (bbox[2] - bbox[0])) / 2 - bbox[0]
        y = (size - (bbox[3] - bbox[1])) / 2 - bbox[1]
        try:
            draw.text((x, y), emoji, font=font, embedded_color=True)
        except Exception:
            draw.text((x, y), emoji, font=font)
        png_path = cfg.DATA_DIR / ".gui_icon.png"
        ico_path = cfg.DATA_DIR / ".gui_icon.ico"
        img.save(png_path)
        sizes = [16, 24, 32, 48, 64, 128, 256]
        img.save(ico_path, format="ICO", sizes=[(s, s) for s in sizes])
        photo = tk.PhotoImage(file=str(png_path))
        return photo, ico_path
    except Exception:
        return None, None


# ═══════════════════════════════════════════════════════════════════════════════
# GUI components
# ═══════════════════════════════════════════════════════════════════════════════

class PlotPanel(ttk.Frame):
    """Left-side plot list + right-side matplotlib canvas."""

    def __init__(self, parent, app, plot_names, include_trend=False):
        super().__init__(parent)
        self.app = app
        self.plot_names = list(plot_names)
        self.include_trend = include_trend

        left = make_card(self)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        ttk.Label(left, text="图形列表", style="Section.TLabel"
                  ).pack(anchor=tk.W, padx=12, pady=(10, 6))
        lb_frame = tk.Frame(left, bg=COLOR_CARD)
        lb_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.listbox = tk.Listbox(
            lb_frame, activestyle="none", exportselection=False,
            font=(FONT_FAMILY, 10),
            bg=COLOR_CARD, fg=COLOR_TEXT, bd=0, highlightthickness=0,
            selectbackground=COLOR_SELECT, selectforeground="#FFFFFF",
            selectborderwidth=0, width=24)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL,
                           command=self.listbox.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        ttk.Button(left, text="导出当前图像",
                   command=self._export_image
                   ).pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 4))
        ttk.Button(left, text="保存全部图像",
                   command=self._save_all_images
                   ).pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 4))
        ttk.Button(left, text="导出当前图形数据",
                   command=self._export_data
                   ).pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 4))

        right = make_card(self)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.fig = Figure(figsize=(7, 5), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.toolbar = NavigationToolbar2Tk(self.canvas, right)
        self.toolbar.update()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True,
                                         padx=4, pady=4)

        self._items = []

    def refresh(self):
        """Refill the list based on the current run."""
        run_id = self.app.current_run
        self.listbox.delete(0, tk.END)
        self._items = []
        if run_id:
            for name in available_plots(run_id, self.plot_names):
                self.listbox.insert(tk.END, f"  {FIGURES[name][1]}")
                self._items.append(name)
        if self.include_trend and self.app.records:
            self.listbox.insert(tk.END, "  实验趋势 (SNR / BER)")
            self._items.append(TREND_KEY)
        if self._items:
            self.listbox.selection_set(0)
            self._show(self._items[0])
        else:
            self.fig.clear()
            ax = self.fig.add_subplot(111)
            ax.text(0.5, 0.5, "当前实验无此类别数据",
                    ha="center", va="center", transform=ax.transAxes)
            self.canvas.draw_idle()

    def _on_select(self, _event):
        sel = self.listbox.curselection()
        if sel:
            self._show(self._items[sel[0]])

    def _show(self, name):
        self.fig.clear()
        try:
            if name == TREND_KEY:
                build_records_trend(self.fig, self.app.records, "Experiment Trend")
            else:
                builder, title = FIGURES[name]
                builder(self.fig, self.app.current_run, title)
        except Exception as exc:
            self.fig.clear()
            ax = self.fig.add_subplot(111)
            ax.text(0.5, 0.5, f"Plot failed:\n{exc}", ha="center", va="center",
                    transform=ax.transAxes, color="red")
        self.canvas.draw_idle()

    def _safe_savefig(self, path: Path):
        """Save the figure; if bbox_inches='tight' raises IndexError, fall back to normal save."""
        try:
            self.fig.savefig(path, dpi=300, bbox_inches="tight")
        except IndexError:
            self.fig.savefig(path, dpi=300)

    def _export_image(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        name = self._items[sel[0]]
        path = ASSETS_DIR / f"{name}_{self.app.current_run}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._safe_savefig(path)
        self.app.status_var.set(f"图像已保存: {path}")

    def _save_all_images(self):
        if not self.app.current_run:
            return
        for name in self._items:
            self._show(name)
            self.canvas.draw()
            path = ASSETS_DIR / f"{name}_{self.app.current_run}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._safe_savefig(path)
        self.app.status_var.set(f"全部图像已保存到 {ASSETS_DIR}")
        if self._items:
            self._show(self._items[0])

    def _gather_arrays(self, name, run_id):
        """Collect the raw arrays behind a named plot for data export."""
        if name == TREND_KEY:
            recs = self.app.records
            return {
                "run_index": np.arange(len(recs)),
                "run_id": np.array([r.get("run_id", "")[-6:] for r in recs]),
                "snr_db": np.array([r.get("snr_db", np.nan) for r in recs]),
                "ber_avg": np.array([r.get("ber_avg", np.nan) for r in recs]),
                "ber_band1": np.array([r.get("ber_band1", np.nan) for r in recs]),
                "ber_band2": np.array([r.get("ber_band2", np.nan) for r in recs]),
            }
        data = _load_run_signals(run_id)
        rec = _get_record(run_id)
        if name in ("tx_waveform", "rx_waveform"):
            sig = data["tx"] if name == "tx_waveform" else data["rx"]
            n = min(len(sig), 2000)
            return {"t_us": np.arange(n) / data["fs"] * 1e6, "amplitude": sig[:n]}
        if name in ("tx_spectrum", "rx_spectrum"):
            sig = data["tx"] if name == "tx_spectrum" else data["rx"]
            f, spec = _spectrum(sig, data["fs"])
            return {"freq_mhz": f / 1e6, "magnitude_db": spec}
        if name == "tx_constellation":
            iq = _regen_symbols(rec)
            return {"I": iq.real, "Q": iq.imag}
        if name in ("rx_constellation", "constellation_density"):
            iq = _load_eq_symbols(rec)
            if iq is None:
                return {}
            return {"I": iq.real, "Q": iq.imag}
        return {}

    def _export_data(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        name = self._items[sel[0]]
        arrays = self._gather_arrays(name, self.app.current_run)
        if not arrays:
            messagebox.showinfo("导出数据", "该图形没有可导出的数据。")
            return
        import csv
        path = ASSETS_DIR / f"{name}_{self.app.current_run}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = list(arrays.keys())
        n = max(len(np.atleast_1d(arrays[k])) for k in keys)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(keys)
            for i in range(n):
                row = []
                for k in keys:
                    v = np.atleast_1d(arrays[k])
                    if len(v) == 1:
                        row.append(v[0])
                    elif i < len(v):
                        row.append(v[i])
                    else:
                        row.append("")
                writer.writerow(row)
        self.app.status_var.set(f"图形数据已导出: {path}")


class ResultsPanel(ttk.Frame):
    """Tab 3: experiment record table on top, result plots for the selected run below."""

    COLUMNS = ("run_id", "time", "src", "datano", "snr", "rate", "ber1", "ber2", "ber_avg")
    HEADINGS = {
        "run_id": ("Experiment ID", 170),
        "time": ("Time", 150),
        "src": ("Source", 80),
        "datano": ("Symbols", 80),
        "snr": ("SNR (dB)", 80),
        "rate": ("速率 Mbps (采样率/×上采样/带宽)", 235),
        "ber1": ("BER 带1", 110),
        "ber2": ("BER 带2", 110),
        "ber_avg": ("平均 BER", 110),
    }

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

        top = ttk.LabelFrame(self, text=" 传输实验记录（点击行切换实验） ")
        top.pack(fill=tk.X, padx=2, pady=(2, 6))
        tree_frame = tk.Frame(top, bg=COLOR_CARD)
        tree_frame.pack(fill=tk.X, padx=6, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=self.COLUMNS,
                                 show="headings", height=7)
        for col in self.COLUMNS:
            text, width = self.HEADINGS[col]
            self.tree.heading(col, text=text)
            self.tree.column(col, width=int(width * app.font_scale),
                             anchor=tk.CENTER)
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL,
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.X, expand=True)
        ttk.Button(tree_frame, text="加载参数到运行测试",
                   command=self._load_selected_to_run
                   ).pack(anchor=tk.E, padx=0, pady=(4, 0))

        bottom = ttk.LabelFrame(self, text=" 当前实验结果 ")
        bottom.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self.plot_panel = PlotPanel(bottom, app, TAB_RESULTS,
                                    include_trend=True)
        self.plot_panel.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._iid_to_run = {}

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self._iid_to_run = {}
        for rec in reversed(self.app.records):   # newest first
            run_id = rec.get("run_id", "")
            ts = rec.get("timestamp", "")[:19].replace("T", " ")
            ber1 = rec.get("ber_band1", np.nan)
            ber2 = rec.get("ber_band2", np.nan)
            ber_avg = rec.get("ber_avg", np.nan)
            row = (
                run_id, ts,
                rec.get("data_source", ""),
                rec.get("datano", ""),
                f"{rec.get('snr_db', 0):.1f}",
                _rate_cell(rec),
                f"{ber1:.3e}" if ber1 is not None and not np.isnan(ber1) else "N/A",
                f"{ber2:.3e}" if ber2 is not None and not np.isnan(ber2) else "N/A",
                f"{ber_avg:.3e}" if ber_avg is not None and not np.isnan(ber_avg) else "N/A",
            )
            iid = self.tree.insert("", tk.END, values=row)
            self._iid_to_run[iid] = run_id
        for iid, rid in self._iid_to_run.items():
            if rid == self.app.current_run:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                break
        self.plot_panel.refresh()

    def _on_row_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        run_id = self._iid_to_run.get(sel[0])
        if run_id and run_id != self.app.current_run:
            self.app.select_run(run_id, source="results")

    def _load_selected_to_run(self):
        """把选中实验的参数写回运行测试页，便于复现/重新下载 AWG 波形。"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("未选择", "请先在记录表中选中一行。")
            return
        run_id = self._iid_to_run.get(sel[0])
        rec = _get_record(run_id)
        if not rec:
            return
        run = self.app.panel_run
        run.src_var.set(rec.get("data_source", cfg.DATA_SOURCE))
        run.mod_var.set(rec.get("modulation_mode", cfg.MODULATION_MODE))
        run.datano_var.set(int(rec.get("datano", cfg.DATANO)))
        run.seed_var.set(int(rec.get("seed", cfg.SEED_BAND1)))
        run.taps_var.set(int(rec.get("lms_taps", cfg.LMS_TAPS)))
        run.mu1_var.set(float(rec.get("lms_mu1", cfg.LMS_MU1)))
        run.mu2_var.set(float(rec.get("lms_mu2", cfg.LMS_MU2)))
        run.ts_var.set(int(rec.get("numof_ts", cfg.NUMOF_TS)))
        run.snr_var.set(float(rec.get("snr_db", cfg.SNR_DB)))
        if rec.get("data_source") == "file":
            run.rxfile_var.set(str(rec.get("rx_file", rec.get("rx_path", ""))))
        if rec.get("osc_addr"):
            run.oscaddr_var.set(rec["osc_addr"])
        run._on_src_change()
        run._save_all_parameters()
        self.app.notebook.select(3)
        self.app.status_var.set(f"已加载 {run_id} 的参数到运行测试页")


class RunPanel(ttk.Frame):
    """Tab 4: run the 36QAM / QAM transceiver from the GUI with live log output."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None

        # 运行日志持久化：所有写入 GUI 日志的文本同时追加到 data/log/gui_run.log
        log_dir = cfg.BASE_DIR / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / "gui_run.log"
        self._log_file = open(self._log_path, "a", encoding="utf-8", buffering=1)

        # 左右分栏：左侧可滚动（参数/AWG/优化卡片），右侧固定运行日志；分隔条可拖动
        paned = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg=COLOR_BG,
                               sashwidth=5, sashrelief=tk.FLAT, bd=0)
        paned.pack(fill=tk.BOTH, expand=True)
        left_frame = tk.Frame(paned, bg=COLOR_BG)
        right_frame = tk.Frame(paned, bg=COLOR_BG)
        paned.add(left_frame, minsize=620, stretch="always")
        paned.add(right_frame, minsize=440, stretch="always")
        self._paned = paned
        self.after(80, lambda: paned.sash_place(
            0, int(paned.winfo_width() * 0.60), 1))

        # 左侧滚动画布
        left_canvas = tk.Canvas(left_frame, bg=COLOR_BG, highlightthickness=0)
        left_scroll = ttk.Scrollbar(left_frame, orient=tk.VERTICAL,
                                    command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_scroll.set)
        left_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        left_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollable_frame = tk.Frame(left_canvas, bg=COLOR_BG)
        left_canvas.create_window((0, 0), window=scrollable_frame,
                                  anchor=tk.NW, tags="frame")

        def _on_left_configure(_event=None):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))
            # 让内部框架宽度随画布宽度变化
            canvas_width = left_canvas.winfo_width()
            left_canvas.itemconfig("frame", width=canvas_width)

        scrollable_frame.bind("<Configure>", _on_left_configure)
        left_canvas.bind("<Configure>", _on_left_configure)

        def _on_left_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        left_canvas.bind("<Enter>",
                         lambda _e: left_canvas.bind_all("<MouseWheel>", _on_left_mousewheel))
        left_canvas.bind("<Leave>",
                         lambda _e: left_canvas.unbind_all("<MouseWheel>"))

        # ── Parameter card ───────────────────────────────────────────
        opt = ttk.LabelFrame(scrollable_frame, text=" 实验参数 ")
        opt.pack(fill=tk.X, padx=2, pady=(2, 4))
        for c in (1, 3, 5):
            opt.columnconfigure(c, weight=1, uniform="param_entry")

        ttk.Label(opt, text="数据源:", style="Card.TLabel"
                  ).grid(row=0, column=0, sticky=tk.W, padx=(10, 2), pady=(10, 4))
        self.src_var = tk.StringVar(value=_setting_str("DATA_SOURCE", cfg.DATA_SOURCE))
        self.src_combo = ttk.Combobox(
            opt, textvariable=self.src_var,
            values=["virtual", "file", "scope"],
            state="readonly", width=12)
        self.src_combo.grid(row=0, column=1, sticky=tk.EW, padx=(2, 12), pady=(10, 4))
        self.src_combo.bind("<<ComboboxSelected>>", self._on_src_change)

        ttk.Label(opt, text="调制模式:", style="Card.TLabel"
                  ).grid(row=0, column=2, sticky=tk.W, padx=(10, 2), pady=(10, 4))
        self.mod_var = tk.StringVar(value=_setting_str("MODULATION_MODE", cfg.MODULATION_MODE))
        self.mod_combo = ttk.Combobox(
            opt, textvariable=self.mod_var,
            values=core.SUPPORTED_MODULATIONS,
            state="readonly", width=12)
        self.mod_combo.grid(row=0, column=3, sticky=tk.EW, padx=(2, 12), pady=(10, 4))
        self.mod_combo.bind("<<ComboboxSelected>>", self._on_mod_change)

        ttk.Label(opt, text="上采样倍数:", style="Card.TLabel"
                  ).grid(row=0, column=4, sticky=tk.W, padx=(10, 2), pady=(10, 4))
        self.upsampleno_var = tk.IntVar(value=_setting_int("UPSAMPLENO", cfg.UPSAMPLENO))
        self.upsampleno_spin = tk.Spinbox(opt, from_=1, to=16, increment=1,
                                          textvariable=self.upsampleno_var, width=12)
        self.upsampleno_spin.grid(row=0, column=5, sticky=tk.EW, padx=(2, 12), pady=(10, 4))
        self._bind_spinbox(self.upsampleno_spin, self.upsampleno_var, "UPSAMPLENO", int)
        self.upsampleno_var.trace_add("write", lambda *_: self._mark_awg_out_of_sync(
            "上采样倍数已更改，请重新下载 AWG 波形"))

        ttk.Label(opt, text="随机种子:", style="Card.TLabel"
                  ).grid(row=1, column=0, sticky=tk.W, padx=(10, 2), pady=4)
        self.seed_var = tk.IntVar(value=_setting_int("SEED", cfg.SEED_BAND1))
        self.seed_spin = tk.Spinbox(opt, from_=0, to=10000, textvariable=self.seed_var, width=12)
        self.seed_spin.grid(row=1, column=1, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.seed_spin, self.seed_var, "SEED", int)

        ttk.Label(opt, text="符号数:", style="Card.TLabel"
                  ).grid(row=1, column=2, sticky=tk.W, padx=(10, 2), pady=4)
        self.datano_var = tk.IntVar(value=_setting_int("DATANO", cfg.DATANO))
        self.datano_spin = tk.Spinbox(opt, from_=1024, to=1024 * 512, increment=1024,
                                      textvariable=self.datano_var, width=12)
        self.datano_spin.grid(row=1, column=3, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.datano_spin, self.datano_var, "DATANO", int)

        ttk.Label(opt, text="信噪比 (dB):", style="Card.TLabel"
                  ).grid(row=1, column=4, sticky=tk.W, padx=(10, 2), pady=4)
        self.snr_var = tk.DoubleVar(value=_setting_float("SNR_DB", cfg.SNR_DB))
        self.snr_spin = tk.Spinbox(opt, from_=0.0, to=50.0, increment=0.5,
                                   textvariable=self.snr_var, width=12)
        self.snr_spin.grid(row=1, column=5, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.snr_spin, self.snr_var, "SNR_DB", float)

        ttk.Label(opt, text="LMS 抽头数:", style="Card.TLabel"
                  ).grid(row=2, column=0, sticky=tk.W, padx=(10, 2), pady=4)
        self.taps_var = tk.IntVar(value=_setting_int("LMS_TAPS", cfg.LMS_TAPS))
        self.taps_spin = tk.Spinbox(opt, from_=3, to=51, increment=2,
                                    textvariable=self.taps_var, width=12)
        self.taps_spin.grid(row=2, column=1, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.taps_spin, self.taps_var, "LMS_TAPS", int)

        ttk.Label(opt, text="LMS 步长 μ1:", style="Card.TLabel"
                  ).grid(row=2, column=2, sticky=tk.W, padx=(10, 2), pady=4)
        self.mu1_var = tk.DoubleVar(value=_setting_float("LMS_MU1", cfg.LMS_MU1))
        self.mu1_spin = tk.Spinbox(opt, from_=0.0001, to=1.0, increment=0.0005,
                                   textvariable=self.mu1_var, width=12)
        self.mu1_spin.grid(row=2, column=3, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.mu1_spin, self.mu1_var, "LMS_MU1", float)

        ttk.Label(opt, text="LMS 步长 μ2:", style="Card.TLabel"
                  ).grid(row=2, column=4, sticky=tk.W, padx=(10, 2), pady=4)
        self.mu2_var = tk.DoubleVar(value=_setting_float("LMS_MU2", cfg.LMS_MU2))
        self.mu2_spin = tk.Spinbox(opt, from_=0.0001, to=1.0, increment=0.0005,
                                   textvariable=self.mu2_var, width=12)
        self.mu2_spin.grid(row=2, column=5, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.mu2_spin, self.mu2_var, "LMS_MU2", float)

        ttk.Label(opt, text="训练符号数:", style="Card.TLabel"
                  ).grid(row=3, column=0, sticky=tk.W, padx=(10, 2), pady=4)
        self.ts_var = tk.IntVar(value=_setting_int("NUMOF_TS", cfg.NUMOF_TS))
        self.ts_spin = tk.Spinbox(opt, from_=100, to=20000, increment=100,
                                  textvariable=self.ts_var, width=12)
        self.ts_spin.grid(row=3, column=1, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.ts_spin, self.ts_var, "NUMOF_TS", int)

        ttk.Label(opt, text="接收波形 (rx_*.txt):", style="Card.TLabel"
                  ).grid(row=4, column=0, sticky=tk.W, padx=(10, 2), pady=4)
        self.rxfile_var = tk.StringVar(value=_setting_str("RX_FILE", ""))
        self.rxfile_entry = ttk.Entry(opt, textvariable=self.rxfile_var)
        self.rxfile_entry.grid(row=4, column=1, columnspan=4, sticky=tk.EW,
                               padx=(2, 4), pady=4)
        self._bind_entry(self.rxfile_entry, self.rxfile_var, "RX_FILE")
        self.rxfile_btn = ttk.Button(opt, text="浏览…", command=self._browse_rx)
        self.rxfile_btn.grid(row=4, column=5, sticky=tk.EW, padx=(2, 12), pady=4)

        ttk.Label(opt, text="示波器地址:", style="Card.TLabel"
                  ).grid(row=5, column=0, sticky=tk.W, padx=(10, 2), pady=4)
        self.oscaddr_var = tk.StringVar(value=_setting_str("OSC_VISA_ADDR", cfg.OSC_VISA_ADDR))
        self.oscaddr_entry = ttk.Entry(opt, textvariable=self.oscaddr_var)
        self.oscaddr_entry.grid(row=5, column=1, columnspan=4, sticky=tk.EW,
                                padx=(2, 4), pady=4)
        self._bind_entry(self.oscaddr_entry, self.oscaddr_var, "OSC_VISA_ADDR",
                         on_save=lambda v: _persist_addresses(osc_addr=v))
        ttk.Button(opt, text="自动识别", command=self._auto_detect_scope
                   ).grid(row=5, column=5, sticky=tk.EW, padx=(2, 12), pady=4)

        ttk.Label(opt, text="示波器通道:", style="Card.TLabel"
                  ).grid(row=6, column=0, sticky=tk.W, padx=(10, 2), pady=4)
        self.oscchan_var = tk.StringVar(value=_setting_str("OSC_CHANNEL", cfg.OSC_CHANNEL))
        self.oscchan_combo = ttk.Combobox(
            opt, textvariable=self.oscchan_var,
            values=["CHAN1", "CHAN2", "CHAN3", "CHAN4"],
            state="readonly", width=10)
        self.oscchan_combo.grid(row=6, column=1, sticky=tk.EW, padx=(2, 12), pady=4)
        self.oscchan_combo.bind("<<ComboboxSelected>>",
                                lambda _e: _persist_addresses(osc_channel=self.oscchan_var.get()))

        self.oscdual_var = tk.BooleanVar(value=_setting_bool("OSC_DUAL", False))
        self.oscdual_check = ttk.Checkbutton(
            opt, text="双通道采集 (CH1+CH2)", variable=self.oscdual_var,
            command=self._on_oscdual_change)
        self.oscdual_check.grid(row=6, column=2, columnspan=2, sticky=tk.W,
                                padx=(10, 2), pady=4)

        ttk.Label(opt, text="示波器采样率 (MSa/s):", style="Card.TLabel"
                  ).grid(row=6, column=4, sticky=tk.W, padx=(10, 2), pady=4)
        self.oscsrate_var = tk.DoubleVar(value=_setting_float("OSC_SAMPLE", cfg.OSC_SAMPLE))
        self.oscsrate_spin = tk.Spinbox(opt, from_=100, to=10000, increment=100,
                                        textvariable=self.oscsrate_var, width=12)
        self.oscsrate_spin.grid(row=6, column=5, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.oscsrate_spin, self.oscsrate_var, "OSC_SAMPLE", float)

        btn_bar = tk.Frame(opt, bg=COLOR_CARD)
        btn_bar.grid(row=7, column=0, columnspan=6, sticky=tk.W,
                     padx=10, pady=(6, 10))
        self.run_btn = ttk.Button(btn_bar, text="▶  运行仿真",
                                  style="Accent.TButton",
                                  command=self.start_run)
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.save_params_btn = ttk.Button(btn_bar, text="💾 保存参数",
                                          command=self._save_all_parameters)
        self.save_params_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.quick_btn = ttk.Button(btn_bar, text="⚡ 快速绘图（仅发射）",
                                    command=self._quick_plot)
        self.quick_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.run_status = ttk.Label(btn_bar, text="就绪", style="DimCard.TLabel")
        self.run_status.pack(side=tk.LEFT, padx=16)

        # ── AWG520 card ──────────────────────────────────────────────
        awg_card = ttk.LabelFrame(scrollable_frame, text=" AWG520 波形下载 ")
        awg_card.pack(fill=tk.X, padx=2, pady=(2, 4))
        for c in (1, 3, 5):
            awg_card.columnconfigure(c, weight=1, uniform="awg_entry")

        ttk.Label(awg_card, text="AWG 地址:", style="Card.TLabel"
                  ).grid(row=0, column=0, sticky=tk.W, padx=(10, 2), pady=(8, 4))
        self.awgaddr_var = tk.StringVar(value=_setting_str("AWG_VISA_ADDR", cfg.AWG_VISA_ADDR))
        self.awgaddr_entry = ttk.Entry(awg_card, textvariable=self.awgaddr_var)
        self.awgaddr_entry.grid(row=0, column=1, columnspan=4, sticky=tk.EW,
                                padx=(2, 4), pady=(8, 4))
        self._bind_entry(self.awgaddr_entry, self.awgaddr_var, "AWG_VISA_ADDR",
                         on_save=lambda v: _persist_addresses(awg_addr=v))
        ttk.Button(awg_card, text="自动识别", command=self._auto_detect_awg
                   ).grid(row=0, column=5, sticky=tk.EW, padx=(2, 12), pady=(8, 4))

        ttk.Label(awg_card, text="采样率 (MSa/s):", style="Card.TLabel"
                  ).grid(row=1, column=0, sticky=tk.W, padx=(10, 2), pady=4)
        self.awgsrate_var = tk.DoubleVar(value=_setting_float("AWG_SAMPLE_RATE", cfg.AWG_SAMPLE))
        self.awgsrate_spin = tk.Spinbox(awg_card, from_=10, to=2000, increment=10,
                                        textvariable=self.awgsrate_var, width=8)
        self.awgsrate_spin.grid(row=1, column=1, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.awgsrate_spin, self.awgsrate_var, "AWG_SAMPLE_RATE", float)

        ttk.Label(awg_card, text="CH1 幅度 Vpp:", style="Card.TLabel"
                  ).grid(row=1, column=2, sticky=tk.W, padx=(10, 2), pady=4)
        self.awgvpp_ch1_var = tk.DoubleVar(value=_setting_float("AWG_VPP_CH1", cfg.AWG_VPP_CH1))
        self.awgvpp_ch1_spin = tk.Spinbox(awg_card, from_=0.02, to=2.0, increment=0.05,
                                          textvariable=self.awgvpp_ch1_var, width=8)
        self.awgvpp_ch1_spin.grid(row=1, column=3, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.awgvpp_ch1_spin, self.awgvpp_ch1_var, "AWG_VPP_CH1", float)

        ttk.Label(awg_card, text="CH2 幅度 Vpp:", style="Card.TLabel"
                  ).grid(row=1, column=4, sticky=tk.W, padx=(10, 2), pady=4)
        self.awgvpp_ch2_var = tk.DoubleVar(value=_setting_float("AWG_VPP_CH2", cfg.AWG_VPP_CH2))
        self.awgvpp_ch2_spin = tk.Spinbox(awg_card, from_=0.02, to=2.0, increment=0.05,
                                          textvariable=self.awgvpp_ch2_var, width=8)
        self.awgvpp_ch2_spin.grid(row=1, column=5, sticky=tk.EW, padx=(2, 12), pady=4)
        self._bind_spinbox(self.awgvpp_ch2_spin, self.awgvpp_ch2_var, "AWG_VPP_CH2", float)

        self.awg_combine_var = tk.BooleanVar(value=_setting_bool("AWG_COMBINE", False))
        self.awg_combine_check = ttk.Checkbutton(
            awg_card, text="叠加到单通道 (CH1)", variable=self.awg_combine_var,
            command=self._on_awg_combine_change)
        self.awg_combine_check.grid(row=2, column=0, columnspan=3, sticky=tk.W,
                                    padx=(10, 2), pady=4)

        self.awg_sync_var = tk.StringVar(value="请下载 AWG 波形")
        self.awg_sync_label = ttk.Label(
            awg_card, textvariable=self.awg_sync_var,
            style="DimCard.TLabel")
        self.awg_sync_label.grid(row=2, column=3, columnspan=3,
                                 sticky=tk.W, padx=(10, 2), pady=4)

        awg_btns = tk.Frame(awg_card, bg=COLOR_CARD)
        awg_btns.grid(row=3, column=0, columnspan=6, sticky=tk.W,
                      padx=10, pady=(4, 10))
        self.awg_dl_btn = ttk.Button(awg_btns, text="⬇ 生成并下载波形",
                                     style="Accent.TButton",
                                     command=self._start_awg_download)
        self.awg_dl_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.awg_start_btn = ttk.Button(awg_btns, text="▶ 开始输出",
                                        command=self._start_awg_output)
        self.awg_start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.awg_stop_btn = ttk.Button(awg_btns, text="停止输出",
                                       command=self._stop_awg)
        self.awg_stop_btn.pack(side=tk.LEFT, padx=(0, 6))

        awg_btns2 = tk.Frame(awg_card, bg=COLOR_CARD)
        awg_btns2.grid(row=4, column=0, columnspan=6, sticky=tk.W,
                       padx=10, pady=(0, 10))
        self.awg_apply_btn = ttk.Button(awg_btns2, text="⚙ 应用输出设置",
                                        command=self._apply_awg_output_settings)
        self.awg_apply_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.awg_clear_btn = ttk.Button(awg_btns2, text="🗑 清空 AWG",
                                        command=self._clear_awg)
        self.awg_clear_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.awg_gen_only_btn = ttk.Button(
            awg_btns2, text="⧉ 仅生成波形",
            command=self._generate_awg_waveform_only)
        self.awg_gen_only_btn.pack(side=tk.LEFT)

        self.awg_card = awg_card
        self._on_awg_combine_change()
        # 初始状态尚未下载，提示用户先下载；后续变更再由对应回调更新
        self.awg_sync_var.set("请下载 AWG 波形")

        # ── LMS 离线参数优化 card ─────────────────────────────────────
        sweep_card = ttk.LabelFrame(scrollable_frame, text=" 离线 LMS 参数优化（仅 file 数据源） ")
        sweep_card.pack(fill=tk.X, padx=2, pady=(2, 4))

        self.sweep_enabled_var = tk.BooleanVar(
            value=_setting_bool("SWEEP_ENABLED", True))
        self.sweep_enabled_check = ttk.Checkbutton(
            sweep_card, text="启用 LMS 参数扫参", variable=self.sweep_enabled_var,
            command=self._on_sweep_enable_change)
        self.sweep_enabled_check.pack(anchor=tk.W, padx=12, pady=(8, 4))

        # taps
        taps_frame = tk.Frame(sweep_card, bg=COLOR_CARD)
        taps_frame.pack(fill=tk.X, padx=12, pady=2)
        ttk.Label(taps_frame, text="抽头数 最小:", style="Card.TLabel"
                  ).pack(side=tk.LEFT)
        self.sweep_taps_min_var = tk.IntVar(value=_setting_int("SWEEP_TAPS_MIN", 3))
        self.sweep_taps_min_spin = tk.Spinbox(
            taps_frame, from_=1, to=51, increment=2,
            textvariable=self.sweep_taps_min_var, width=6)
        self.sweep_taps_min_spin.pack(side=tk.LEFT, padx=(4, 12))
        self._bind_spinbox(self.sweep_taps_min_spin, self.sweep_taps_min_var,
                           "SWEEP_TAPS_MIN", int)
        ttk.Label(taps_frame, text="最大:", style="Card.TLabel"
                  ).pack(side=tk.LEFT)
        self.sweep_taps_max_var = tk.IntVar(value=_setting_int("SWEEP_TAPS_MAX", 31))
        self.sweep_taps_max_spin = tk.Spinbox(
            taps_frame, from_=1, to=51, increment=2,
            textvariable=self.sweep_taps_max_var, width=6)
        self.sweep_taps_max_spin.pack(side=tk.LEFT, padx=(4, 12))
        self._bind_spinbox(self.sweep_taps_max_spin, self.sweep_taps_max_var,
                           "SWEEP_TAPS_MAX", int)
        ttk.Label(taps_frame, text="步进:", style="Card.TLabel"
                  ).pack(side=tk.LEFT)
        self.sweep_taps_step_var = tk.IntVar(value=_setting_int("SWEEP_TAPS_STEP", 4))
        self.sweep_taps_step_spin = tk.Spinbox(
            taps_frame, from_=2, to=10, increment=2,
            textvariable=self.sweep_taps_step_var, width=6)
        self.sweep_taps_step_spin.pack(side=tk.LEFT, padx=(4, 12))
        self._bind_spinbox(self.sweep_taps_step_spin, self.sweep_taps_step_var,
                           "SWEEP_TAPS_STEP", int)

        # mu1
        mu1_frame = tk.Frame(sweep_card, bg=COLOR_CARD)
        mu1_frame.pack(fill=tk.X, padx=12, pady=2)
        ttk.Label(mu1_frame, text="μ1 范围:", style="Card.TLabel"
                  ).pack(side=tk.LEFT)
        self.sweep_mu1_min_var = tk.DoubleVar(value=_setting_float("SWEEP_MU1_MIN", 1e-3))
        self.sweep_mu1_min_entry = ttk.Entry(mu1_frame, textvariable=self.sweep_mu1_min_var, width=10)
        self.sweep_mu1_min_entry.pack(side=tk.LEFT, padx=(4, 4))
        self._bind_entry(self.sweep_mu1_min_entry, self.sweep_mu1_min_var, "SWEEP_MU1_MIN")
        ttk.Label(mu1_frame, text="~", style="Card.TLabel").pack(side=tk.LEFT)
        self.sweep_mu1_max_var = tk.DoubleVar(value=_setting_float("SWEEP_MU1_MAX", 1e-1))
        self.sweep_mu1_max_entry = ttk.Entry(mu1_frame, textvariable=self.sweep_mu1_max_var, width=10)
        self.sweep_mu1_max_entry.pack(side=tk.LEFT, padx=(4, 12))
        self._bind_entry(self.sweep_mu1_max_entry, self.sweep_mu1_max_var, "SWEEP_MU1_MAX")
        ttk.Label(mu1_frame, text="点数:", style="Card.TLabel").pack(side=tk.LEFT)
        self.sweep_mu1_points_var = tk.IntVar(value=_setting_int("SWEEP_MU1_POINTS", 5))
        self.sweep_mu1_points_spin = tk.Spinbox(
            mu1_frame, from_=2, to=20, textvariable=self.sweep_mu1_points_var, width=5)
        self.sweep_mu1_points_spin.pack(side=tk.LEFT, padx=(4, 12))
        self._bind_spinbox(self.sweep_mu1_points_spin, self.sweep_mu1_points_var,
                           "SWEEP_MU1_POINTS", int)

        # mu2
        mu2_frame = tk.Frame(sweep_card, bg=COLOR_CARD)
        mu2_frame.pack(fill=tk.X, padx=12, pady=2)
        ttk.Label(mu2_frame, text="μ2 范围:", style="Card.TLabel"
                  ).pack(side=tk.LEFT)
        self.sweep_mu2_min_var = tk.DoubleVar(value=_setting_float("SWEEP_MU2_MIN", 1e-3))
        self.sweep_mu2_min_entry = ttk.Entry(mu2_frame, textvariable=self.sweep_mu2_min_var, width=10)
        self.sweep_mu2_min_entry.pack(side=tk.LEFT, padx=(4, 4))
        self._bind_entry(self.sweep_mu2_min_entry, self.sweep_mu2_min_var, "SWEEP_MU2_MIN")
        ttk.Label(mu2_frame, text="~", style="Card.TLabel").pack(side=tk.LEFT)
        self.sweep_mu2_max_var = tk.DoubleVar(value=_setting_float("SWEEP_MU2_MAX", 1e-1))
        self.sweep_mu2_max_entry = ttk.Entry(mu2_frame, textvariable=self.sweep_mu2_max_var, width=10)
        self.sweep_mu2_max_entry.pack(side=tk.LEFT, padx=(4, 12))
        self._bind_entry(self.sweep_mu2_max_entry, self.sweep_mu2_max_var, "SWEEP_MU2_MAX")
        ttk.Label(mu2_frame, text="点数:", style="Card.TLabel").pack(side=tk.LEFT)
        self.sweep_mu2_points_var = tk.IntVar(value=_setting_int("SWEEP_MU2_POINTS", 5))
        self.sweep_mu2_points_spin = tk.Spinbox(
            mu2_frame, from_=2, to=20, textvariable=self.sweep_mu2_points_var, width=5)
        self.sweep_mu2_points_spin.pack(side=tk.LEFT, padx=(4, 12))
        self._bind_spinbox(self.sweep_mu2_points_spin, self.sweep_mu2_points_var,
                           "SWEEP_MU2_POINTS", int)

        # scale + buttons
        btn_frame = tk.Frame(sweep_card, bg=COLOR_CARD)
        btn_frame.pack(fill=tk.X, padx=12, pady=(4, 8))
        ttk.Label(btn_frame, text="步长尺度:", style="Card.TLabel"
                  ).pack(side=tk.LEFT)
        self.sweep_mu_scale_var = tk.StringVar(value=_setting_str("SWEEP_MU_SCALE", "log"))
        self.sweep_mu_scale_combo = ttk.Combobox(
            btn_frame, textvariable=self.sweep_mu_scale_var,
            values=["log", "linear"], state="readonly", width=8)
        self.sweep_mu_scale_combo.pack(side=tk.LEFT, padx=(4, 16))
        self.sweep_mu_scale_combo.bind("<<ComboboxSelected>>",
                                       lambda _e: _persist_setting("SWEEP_MU_SCALE",
                                                                   self.sweep_mu_scale_var.get()))

        self.sweep_btn = ttk.Button(btn_frame, text="▶ 离线优化",
                                    style="Accent.TButton",
                                    command=self._start_sweep)
        self.sweep_btn.pack(side=tk.LEFT, padx=(0, 12))
        self.sweep_status_var = tk.StringVar(value="扫参未启用")
        self.sweep_status_label = ttk.Label(
            btn_frame, textvariable=self.sweep_status_var, style="DimCard.TLabel")
        self.sweep_status_label.pack(side=tk.LEFT)

        self.sweep_card = sweep_card
        self._update_sweep_state()

        # ── Right-side: log card ─────────────────────────────────────
        log_card = ttk.LabelFrame(right_frame, text=" 运行日志 ")
        log_card.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        log_frame = tk.Frame(log_card, bg=COLOR_CARD)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text = tk.Text(
            log_frame, wrap=tk.NONE, state=tk.DISABLED, width=60,
            font=(FONT_MONO, 10),
            spacing1=0, spacing2=0, spacing3=0,
            bg="#0F172A", fg="#E2E8F0", bd=0, highlightthickness=0,
            insertbackground="#E2E8F0")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lsb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                            command=self.log_text.yview)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.tag_configure("head", foreground="#22D3EE")
        self.log_text.tag_configure("err", foreground="#F87171")

        self._append_log(
            "提示：选择数据源与参数后点击对应按钮。\n"
            "virtual = 虚拟信道离线仿真；file = 读取已保存接收波形；scope = 在线采集示波器并运行。\n"
            "运行完成后会自动刷新并切换到最新实验。\n", "head")

        self._on_src_change()

    def _on_src_change(self, _event=None):
        src = self.src_var.get()
        _persist_setting("DATA_SOURCE", src)
        file_state = tk.NORMAL if src == "file" else tk.DISABLED
        scope_state = tk.NORMAL if src == "scope" else tk.DISABLED
        self.rxfile_entry.configure(state=file_state)
        self.rxfile_btn.configure(state=file_state)
        self.oscaddr_entry.configure(state=scope_state)
        self.oscdual_check.configure(state=scope_state)
        # SNR 输入仅在 virtual 仿真时有效；file/scope 使用均衡后估计的实际 SNR
        self.snr_spin.configure(state=tk.NORMAL if src == "virtual" else tk.DISABLED)
        self._update_oscchan_state()
        if src == "scope":
            self.run_btn.configure(text="▶  采集并运行")
        elif src == "file":
            self.run_btn.configure(text="▶  从文件运行")
        else:
            self.run_btn.configure(text="▶  运行仿真")
        self._update_sweep_state()

    def _update_oscchan_state(self):
        """双通道采集开启时禁用单通道下拉框。"""
        src = self.src_var.get()
        if src != "scope" or self.oscdual_var.get():
            self.oscchan_combo.configure(state=tk.DISABLED)
        else:
            self.oscchan_combo.configure(state="readonly")

    def _on_oscdual_change(self):
        self._update_oscchan_state()
        _persist_addresses(osc_dual=self.oscdual_var.get())

    def _browse_rx(self):
        path = filedialog.askopenfilename(
            title="选择原始接收波形文件",
            initialdir=str(cfg.RXDATA_DIR),
            filetypes=[("原始接收波形 rx_*.txt", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.rxfile_var.set(path)
            _persist_setting("RX_FILE", path)

    def _bind_spinbox(self, widget, var, key: str, converter):
        """监听 Spinbox 变量变化，变更即保存（覆盖手动输入、增减箭头等）。"""
        def _save(*_args):
            try:
                value = converter(var.get())
                _persist_setting(key, value)
            except Exception:
                pass
        var.trace_add("write", _save)

    def _bind_entry(self, widget, var, key: str, on_save=None):
        """给 Entry 绑定失焦 / 回车事件，变更即保存。"""
        def _save(_event=None):
            value = var.get()
            if on_save is not None:
                on_save(value)
            else:
                _persist_setting(key, value)
        widget.bind("<FocusOut>", _save)
        widget.bind("<Return>", _save)

    def _show_instruments_dialog(self, title: str, instruments):
        """弹出窗口显示识别到的仪器列表。"""
        text = instr_disc.format_instrument_list(instruments)
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("700x300")
        win.transient(self)
        txt = tk.Text(win, wrap=tk.NONE, font=(FONT_MONO, 9),
                      bg="#FAFAFA", fg=COLOR_TEXT, bd=0,
                      highlightbackground=COLOR_BORDER, highlightthickness=1)
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        txt.insert(tk.END, text)
        txt.configure(state=tk.DISABLED)
        ttk.Button(win, text="关闭", command=win.destroy
                   ).pack(side=tk.BOTTOM, pady=(0, 10))

    def _auto_detect_scope(self):
        """在后台线程自动识别示波器并填入地址。"""
        self._append_log("正在扫描示波器...")
        threading.Thread(target=self._auto_detect_scope_thread, daemon=True).start()

    def _auto_detect_scope_thread(self):
        try:
            extra = [self.oscaddr_var.get().strip()]
            candidates = instr_disc.detect_scope_candidates(extra_addrs=extra)
            if not candidates:
                self._queue.put(("log", "未识别到示波器，请检查连接与驱动。"))
                self._queue.put(("info", "未找到示波器。"))
                return
            addr = candidates[0]["address"]
            self._queue.put(("set_oscaddr", addr))
            self._queue.put(("log", f"已识别示波器: {addr}\n    {candidates[0]['idn']}"))
            self._queue.put(("show_dialog", ("识别到的示波器", candidates)))
        except Exception as exc:
            self._queue.put(("log", f"自动识别失败: {exc}"))
            self._queue.put(("error", f"自动识别失败: {exc}"))

    def _auto_detect_awg(self):
        """在后台线程自动识别 AWG 并填入地址。"""
        self._append_log("正在扫描 AWG...")
        threading.Thread(target=self._auto_detect_awg_thread, daemon=True).start()

    def _auto_detect_awg_thread(self):
        try:
            extra = [self.awgaddr_var.get().strip()]
            candidates = instr_disc.detect_awg_candidates(extra_addrs=extra)
            if not candidates:
                self._queue.put(("log", "未识别到 AWG，请检查连接与驱动。"))
                self._queue.put(("info", "未找到 AWG。"))
                return
            addr = candidates[0]["address"]
            self._queue.put(("set_awgaddr", addr))
            self._queue.put(("log", f"已识别 AWG: {addr}\n    {candidates[0]['idn']}"))
            self._queue.put(("show_dialog", ("识别到的 AWG", candidates)))
        except Exception as exc:
            self._queue.put(("log", f"自动识别失败: {exc}"))
            self._queue.put(("error", f"自动识别失败: {exc}"))

    def _on_awg_combine_change(self):
        """切换单通道叠加模式时更新界面状态并保存。"""
        combine = self.awg_combine_var.get()
        _persist_setting("AWG_COMBINE", combine)
        self._mark_awg_out_of_sync("叠加方式已更改，请重新下载 AWG 波形")
        if combine:
            self.awg_card.configure(text=" AWG520 波形下载（两路叠加到 CH1） ")
            self.awg_dl_btn.configure(text="⬇ 生成并下载单通道叠加波形")
            self.awgvpp_ch2_spin.configure(state=tk.DISABLED)
        else:
            self.awg_card.configure(text=" AWG520 波形下载（CH1 = I 路，CH2 = Q 路） ")
            self.awg_dl_btn.configure(text="⬇ 生成并下载双通道波形")
            self.awgvpp_ch2_spin.configure(state=tk.NORMAL)

    def _start_awg_download(self):
        if self._thread is not None and self._thread.is_alive():
            messagebox.showinfo("忙", "已有任务在运行，请等待完成。")
            return
        self.awg_dl_btn.configure(state=tk.DISABLED)
        if self.awg_combine_var.get():
            self._append_log("\n===== 开始生成并下载 AWG 单通道叠加波形 =====\n", "head")
        else:
            self._append_log("\n===== 开始生成并下载 AWG 双通道波形 =====\n", "head")
        self._thread = threading.Thread(target=self._awg_thread, daemon=True)
        self._thread.start()
        self.after(100, self._poll)

    def _awg_thread(self):
        try:
            datano = self.datano_var.get()
            seed = self.seed_var.get()
            mode = self.mod_var.get()
            upsampleno = self.upsampleno_var.get()
            v1, v2, _, _ = core.generate_symbols(
                mode, datano, seed, seed + 100, upsampleno=upsampleno)
            tx = core.generate_tx(v1, v2, upsampleno=upsampleno)
            self._queue.put(("log",
                             f"发射波形已生成: 调制={mode}, 符号数={datano}, 种子={seed}, "
                             f"上采样={upsampleno}, 采样率={self.awgsrate_var.get()} MSa/s"))
            if self.awg_combine_var.get():
                awg_m8190a.combine_and_download_single_channel(
                    tx["data1"], tx["data2"],
                    sample_rate=self.awgsrate_var.get() * 1e6,
                    vpp=self.awgvpp_ch1_var.get(),
                    channel=1,
                    visa_addr=self.awgaddr_var.get().strip(),
                    log=lambda msg: self._queue.put(("log", msg)),
                )
            else:
                awg_m8190a.download_two_channels(
                    tx["data1"], tx["data2"],
                    sample_rate=self.awgsrate_var.get() * 1e6,
                    vpp_ch1=self.awgvpp_ch1_var.get(),
                    vpp_ch2=self.awgvpp_ch2_var.get(),
                    visa_addr=self.awgaddr_var.get().strip(),
                    log=lambda msg: self._queue.put(("log", msg)),
                )
            self._queue.put(("awg_done", None))
        except Exception as exc:
            self._queue.put(("awg_error", exc))

    def _mark_awg_out_of_sync(self, reason: str = "请重新下载 AWG 波形"):
        """当发射参数变更时提示用户当前 AWG 波形可能已不同步。"""
        self.awg_sync_var.set(f"⚠ {reason}")

    def _mark_awg_synced(self):
        """下载完成后标记 AWG 波形与当前设置同步。"""
        self.awg_sync_var.set("✓ 波形已与当前设置同步")

    def _on_mod_change(self, _event=None):
        """调制模式变更时保存并提示重新下载。"""
        _persist_setting("MODULATION_MODE", self.mod_var.get())
        self._mark_awg_out_of_sync("调制模式已更改，请重新下载 AWG 波形")

    def _save_all_parameters(self):
        """把当前运行测试页所有参数显式保存到 settings.json。"""
        save_settings({
            "DATA_SOURCE": self.src_var.get(),
            "DATANO": self.datano_var.get(),
            "MODULATION_MODE": self.mod_var.get(),
            "SNR_DB": self.snr_var.get(),
            "SEED": self.seed_var.get(),
            "LMS_TAPS": self.taps_var.get(),
            "LMS_MU1": self.mu1_var.get(),
            "LMS_MU2": self.mu2_var.get(),
            "NUMOF_TS": self.ts_var.get(),
            "RX_FILE": self.rxfile_var.get(),
            "OSC_VISA_ADDR": self.oscaddr_var.get(),
            "OSC_CHANNEL": self.oscchan_var.get(),
            "OSC_DUAL": self.oscdual_var.get(),
            "OSC_SAMPLE": self.oscsrate_var.get(),
            "UPSAMPLENO": self.upsampleno_var.get(),
            "AWG_VISA_ADDR": self.awgaddr_var.get(),
            "AWG_SAMPLE_RATE": self.awgsrate_var.get(),
            "AWG_VPP_CH1": self.awgvpp_ch1_var.get(),
            "AWG_VPP_CH2": self.awgvpp_ch2_var.get(),
            "AWG_COMBINE": self.awg_combine_var.get(),
            "SWEEP_ENABLED": self.sweep_enabled_var.get(),
            "SWEEP_TAPS_MIN": self.sweep_taps_min_var.get(),
            "SWEEP_TAPS_MAX": self.sweep_taps_max_var.get(),
            "SWEEP_TAPS_STEP": self.sweep_taps_step_var.get(),
            "SWEEP_MU1_MIN": self.sweep_mu1_min_var.get(),
            "SWEEP_MU1_MAX": self.sweep_mu1_max_var.get(),
            "SWEEP_MU1_POINTS": self.sweep_mu1_points_var.get(),
            "SWEEP_MU2_MIN": self.sweep_mu2_min_var.get(),
            "SWEEP_MU2_MAX": self.sweep_mu2_max_var.get(),
            "SWEEP_MU2_POINTS": self.sweep_mu2_points_var.get(),
            "SWEEP_MU_SCALE": self.sweep_mu_scale_var.get(),
        })
        self._append_log("\n===== 当前运行参数已保存 =====\n", "head")

    def _on_sweep_enable_change(self):
        """启用/禁用扫参时更新状态并保存。"""
        _persist_setting("SWEEP_ENABLED", self.sweep_enabled_var.get())
        self._update_sweep_state()

    def _update_sweep_state(self):
        """根据数据源和是否启用扫参更新按钮与提示。"""
        enabled = self.sweep_enabled_var.get()
        src_ok = self.src_var.get() == "file"
        if not enabled:
            self.sweep_btn.configure(state=tk.DISABLED)
            self.sweep_status_var.set("扫参未启用")
        elif not src_ok:
            self.sweep_btn.configure(state=tk.DISABLED)
            self.sweep_status_var.set("仅数据源为 file 时可用")
        else:
            self.sweep_btn.configure(state=tk.NORMAL)
            self.sweep_status_var.set("就绪")

    def _start_sweep(self):
        """启动离线 LMS 参数扫描线程。"""
        if self._thread is not None and self._thread.is_alive():
            messagebox.showinfo("忙", "已有任务在运行，请等待完成。")
            return
        if self.src_var.get() != "file":
            messagebox.showwarning("数据源错误", "离线参数优化仅支持 file 数据源。")
            return
        rx_file = self.rxfile_var.get().strip()
        if not rx_file:
            messagebox.showwarning("缺少接收文件", "请在“接收波形”中选择 rx_*.txt 文件。")
            return
        if not Path(rx_file).is_file():
            messagebox.showerror("文件不存在", f"找不到接收文件:\n{rx_file}")
            return

        taps_min = self.sweep_taps_min_var.get()
        taps_max = self.sweep_taps_max_var.get()
        taps_step = self.sweep_taps_step_var.get()
        if taps_min > taps_max or taps_step <= 0:
            messagebox.showwarning("参数错误", "抽头数范围设置不正确。")
            return

        try:
            mu1_min = float(self.sweep_mu1_min_var.get())
            mu1_max = float(self.sweep_mu1_max_var.get())
            mu2_min = float(self.sweep_mu2_min_var.get())
            mu2_max = float(self.sweep_mu2_max_var.get())
        except Exception:
            messagebox.showwarning("参数错误", "μ1 / μ2 范围请输入有效数字。")
            return
        if mu1_min <= 0 or mu1_max <= 0 or mu2_min <= 0 or mu2_max <= 0:
            messagebox.showwarning("参数错误", "步长范围必须大于 0。")
            return
        if mu1_min > mu1_max or mu2_min > mu2_max:
            messagebox.showwarning("参数错误", "步长范围的最小值不能大于最大值。")
            return

        self.sweep_btn.configure(state=tk.DISABLED)
        self.sweep_status_var.set("扫描中…")
        self._append_log("\n========== Starting LMS Coordinate Descent ==========\n", "head")
        self.app.set_running(True)
        self._thread = threading.Thread(target=self._sweep_thread, daemon=True)
        self._thread.start()
        self.after(100, self._poll)

    def _sweep_thread(self):
        try:
            best_record, all_results = optimizer.run_lms_coordinate_search(
                rx_file=self.rxfile_var.get().strip(),
                datano=self.datano_var.get(),
                seed=self.seed_var.get(),
                snr_db=self.snr_var.get(),
                modulation_mode=self.mod_var.get(),
                numof_ts=self.ts_var.get(),
                awg_sample_rate_ms=self.awgsrate_var.get(),
                upsampleno=self.upsampleno_var.get(),
                taps_range=(
                    self.sweep_taps_min_var.get(),
                    self.sweep_taps_max_var.get(),
                    self.sweep_taps_step_var.get(),
                ),
                mu1_range=(
                    float(self.sweep_mu1_min_var.get()),
                    float(self.sweep_mu1_max_var.get()),
                    self.sweep_mu1_points_var.get(),
                ),
                mu2_range=(
                    float(self.sweep_mu2_min_var.get()),
                    float(self.sweep_mu2_max_var.get()),
                    self.sweep_mu2_points_var.get(),
                ),
                mu_scale=self.sweep_mu_scale_var.get(),
                initial_taps=self.taps_var.get(),
                initial_mu1=self.mu1_var.get(),
                initial_mu2=self.mu2_var.get(),
                log=lambda msg: self._queue.put(("log", msg)),
            )
            # 保存完整扫描结果 CSV
            import csv
            csv_path = cfg.RECORD_DIR / f"sweep_{best_record['run_id']}.csv"
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "taps", "mu1", "mu2", "ber_band1", "ber_band2", "ber_avg", "snr_db"])
                writer.writeheader()
                writer.writerows(all_results)

            save_record(best_record["run_id"], best_record, cfg.RECORD_DIR)
            self._queue.put(("sweep_done", {
                "record": best_record,
                "csv": str(csv_path),
                "count": len(all_results),
            }))
        except Exception as exc:
            self._queue.put(("sweep_error", exc))

    def _stop_awg(self):
        if self._thread is not None and self._thread.is_alive():
            messagebox.showinfo("忙", "下载任务进行中，请等待完成后再停止。")
            return

        def _do_stop():
            try:
                with awg_m8190a.AWG520Controller(
                        visa_addr=self.awgaddr_var.get().strip(),
                        log=lambda msg: self._queue.put(("log", msg))) as awg:
                    awg.stop()
            except Exception as exc:
                self._queue.put(("log", f"停止输出失败: {exc}"))

        self._append_log("\n===== 停止 AWG 输出 =====\n", "head")
        threading.Thread(target=_do_stop, daemon=True).start()
        self.after(100, self._poll)

    def _start_awg_output(self):
        if self._thread is not None and self._thread.is_alive():
            messagebox.showinfo("忙", "已有任务在运行，请等待完成。")
            return
        self.awg_start_btn.configure(state=tk.DISABLED)
        self._append_log("\n===== 开始 AWG 输出 =====\n", "head")
        self._thread = threading.Thread(target=self._start_awg_output_thread,
                                        daemon=True)
        self._thread.start()
        self.after(100, self._poll)

    def _start_awg_output_thread(self):
        try:
            awg_m8190a.start_awg_output(
                visa_addr=self.awgaddr_var.get().strip(),
                log=lambda msg: self._queue.put(("log", msg)),
            )
            self._queue.put(("start_done", None))
        except Exception as exc:
            self._queue.put(("start_error", exc))

    def _apply_awg_output_settings(self):
        if self._thread is not None and self._thread.is_alive():
            messagebox.showinfo("忙", "已有任务在运行，请等待完成。")
            return
        self.awg_apply_btn.configure(state=tk.DISABLED)
        self._append_log(
            f"\n===== 应用 AWG 输出设置: {self.awgsrate_var.get()} MSa/s, "
            f"Vpp=[{self.awgvpp_ch1_var.get():.2f}, {self.awgvpp_ch2_var.get():.2f}] =====\n",
            "head")
        self._thread = threading.Thread(target=self._apply_awg_output_settings_thread,
                                        daemon=True)
        self._thread.start()
        self.after(100, self._poll)

    def _apply_awg_output_settings_thread(self):
        try:
            awg_m8190a.apply_awg_output_settings(
                sample_rate=self.awgsrate_var.get() * 1e6,
                vpp_ch1=self.awgvpp_ch1_var.get(),
                vpp_ch2=self.awgvpp_ch2_var.get(),
                visa_addr=self.awgaddr_var.get().strip(),
                log=lambda msg: self._queue.put(("log", msg)),
            )
            self._queue.put(("apply_done", None))
        except Exception as exc:
            self._queue.put(("apply_error", exc))

    def _clear_awg(self):
        if self._thread is not None and self._thread.is_alive():
            messagebox.showinfo("忙", "已有任务在运行，请等待完成。")
            return
        if not messagebox.askyesno("确认清空",
                                   "确定要清空 AWG 中的波形并停止输出吗？"):
            return
        self.awg_clear_btn.configure(state=tk.DISABLED)
        self._append_log("\n===== 清空 AWG =====\n", "head")
        self._thread = threading.Thread(target=self._clear_awg_thread,
                                        daemon=True)
        self._thread.start()
        self.after(100, self._poll)

    def _clear_awg_thread(self):
        try:
            awg_m8190a.clear_awg(
                visa_addr=self.awgaddr_var.get().strip(),
                log=lambda msg: self._queue.put(("log", msg)),
            )
            self._queue.put(("clear_done", None))
        except Exception as exc:
            self._queue.put(("clear_error", exc))

    def _generate_awg_waveform_only(self):
        """根据当前参数生成发射波形文件（txt + wfm），但不连接 AWG。"""
        try:
            datano = self.datano_var.get()
            seed = self.seed_var.get()
            mode = self.mod_var.get()
            upsampleno = self.upsampleno_var.get()
            v1, v2, decimal1, decimal2 = core.generate_symbols(
                mode, datano, seed, seed + 100, upsampleno=upsampleno)
            tx = core.generate_tx(v1, v2, upsampleno=upsampleno)
            run_id = generate_run_id()
            tx1_path = cfg.TXDATA_DIR / f"txI_{run_id}.txt"
            tx2_path = cfg.TXDATA_DIR / f"txQ_{run_id}.txt"
            txsum_path = cfg.TXDATA_DIR / f"txsum_{run_id}.txt"
            v1_path = cfg.TXDATA_DIR / f"v1_{run_id}.txt"
            v2_path = cfg.TXDATA_DIR / f"v2_{run_id}.txt"
            dec1_path = cfg.TXDATA_DIR / f"txsym_1_{run_id}.txt"
            dec2_path = cfg.TXDATA_DIR / f"txsym_2_{run_id}.txt"
            from utils import save_txt, write_wfm
            save_txt(tx1_path, tx["data1"])
            save_txt(tx2_path, tx["data2"])
            save_txt(txsum_path, tx["tx_sum"])
            save_txt(v1_path, v1)
            save_txt(v2_path, v2)
            save_txt(dec1_path, decimal1, fmt="%d")
            save_txt(dec2_path, decimal2, fmt="%d")
            write_wfm(tx["data1"], cfg.TXDATA_DIR / f"SuperposedPAM6_Tx1_{run_id}.wfm")
            write_wfm(tx["data2"], cfg.TXDATA_DIR / f"SuperposedPAM6_Tx2_{run_id}.wfm")
            self._append_log(
                f"\n===== 仅生成波形: {run_id} =====\n"
                f"调制={mode}, 符号数={datano}, 种子={seed}\n"
                f"文件: txI_{run_id}.txt, txQ_{run_id}.txt (+ .wfm)\n",
                "head")
            self._mark_awg_out_of_sync("波形已重新生成，请下载到 AWG")
        except Exception as exc:
            self._append_log(f"\n生成波形失败: {exc}\n", "err")

    def _append_log(self, text, tag=None):
        self.log_text.configure(state=tk.NORMAL)
        if tag:
            self.log_text.insert(tk.END, text, tag)
        else:
            self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        if self._log_file is not None and not self._log_file.closed:
            try:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._log_file.write(f"[{timestamp}] {text}")
                self._log_file.flush()
            except Exception:
                pass

    def start_run(self):
        if self._thread is not None and self._thread.is_alive():
            return
        src = self.src_var.get()
        if src == "file" and not self.rxfile_var.get():
            messagebox.showwarning("缺少接收文件", "请在“接收文件”中选择波形文件。")
            return
        self.run_btn.configure(state=tk.DISABLED)
        self.run_status.configure(text="运行中…")
        self.app.set_running(True)
        self._append_log("\n========== Starting Simulation ==========\n", "head")
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()
        self.after(100, self._poll)

    def _run_thread(self):
        try:
            src = self.src_var.get()
            run_id = None
            old_record = None
            # file 数据源时尽量复用原 run_id（从 rx_<run_id>.txt 文件名提取），不生成新 ID
            if src == "file":
                rx_path = Path(self.rxfile_var.get())
                if rx_path.stem.startswith("rx_"):
                    run_id = rx_path.stem[3:]
                    old_json = cfg.RECORD_DIR / f"record_{run_id}.json"
                    if old_json.is_file():
                        try:
                            old_record = json.loads(old_json.read_text(encoding="utf-8"))
                        except Exception:
                            old_record = None

            record = main_flow.run_experiment(
                datano=self.datano_var.get(),
                seed=self.seed_var.get(),
                snr_db=self.snr_var.get(),
                lms_taps=self.taps_var.get(),
                lms_mu1=self.mu1_var.get(),
                lms_mu2=self.mu2_var.get(),
                numof_ts=self.ts_var.get(),
                data_source=src,
                rx_file=self.rxfile_var.get(),
                osc_addr=self.oscaddr_var.get(),
                osc_channel=self.oscchan_var.get(),
                osc_dual=self.oscdual_var.get(),
                osc_sample_rate_ms=self.oscsrate_var.get(),
                awg_sample_rate_ms=self.awgsrate_var.get(),
                modulation_mode=self.mod_var.get(),
                upsampleno=self.upsampleno_var.get(),
                run_id=run_id,
                log=lambda msg: self._queue.put(("log", msg)),
            )

            # file 数据源且存在旧记录时：BER 变差则不覆盖，并提示
            if src == "file" and old_record is not None:
                old_ber = old_record.get("ber_avg", float("inf"))
                if record["ber_avg"] > old_ber:
                    self._queue.put((
                        "log",
                        f"注意: 新 BER {record['ber_avg']:.3e} 比原记录 "
                        f"{old_ber:.3e} 差，未覆盖原记录 (run_id={run_id})。"
                    ))
                    self._queue.put(("info",
                                     f"未覆盖 {run_id}\n新 BER 比原记录差，"))
                    self._queue.put(("done", run_id))
                    return

            save_record(record["run_id"], record, cfg.RECORD_DIR)
            self._queue.put(("log",
                             f"完成: 平均 BER={record['ber_avg']:.4e} "
                             f"(带1={record['ber_band1']:.4e}, 带2={record['ber_band2']:.4e})"))
            self._queue.put(("done", record["run_id"]))
        except Exception as exc:
            self._queue.put(("error", exc))

    def _poll(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._append_log(payload + "\n")
                elif kind == "error":
                    self._append_log(f"\nError: {payload}\n", "err")
                    traceback.print_exc()
                    if self._thread is not None and not self._thread.is_alive():
                        self._on_done(None)
                elif kind == "info":
                    messagebox.showinfo("自动识别", payload)
                elif kind == "set_oscaddr":
                    self.oscaddr_var.set(payload)
                    _persist_addresses(osc_addr=payload)
                elif kind == "set_awgaddr":
                    self.awgaddr_var.set(payload)
                    _persist_addresses(awg_addr=payload)
                elif kind == "show_dialog":
                    title, instruments = payload
                    self._show_instruments_dialog(title, instruments)
                elif kind == "done":
                    self._append_log("\n========== Simulation Completed ==========\n", "head")
                    self._on_done(payload)
                    return
                elif kind == "awg_done":
                    self._append_log("\n===== AWG 下载完成 =====\n", "head")
                    self.awg_dl_btn.configure(state=tk.NORMAL)
                    self._mark_awg_synced()
                elif kind == "awg_error":
                    self._append_log(f"\nAWG 错误: {payload}\n", "err")
                    self.awg_dl_btn.configure(state=tk.NORMAL)
                elif kind == "start_done":
                    self._append_log("\n===== AWG 已开始输出 =====\n", "head")
                    self.awg_start_btn.configure(state=tk.NORMAL)
                elif kind == "start_error":
                    self._append_log(f"\nAWG 开始输出错误: {payload}\n", "err")
                    self.awg_start_btn.configure(state=tk.NORMAL)
                elif kind == "apply_done":
                    self._append_log("\n===== AWG 输出设置已应用 =====\n", "head")
                    self.awg_apply_btn.configure(state=tk.NORMAL)
                elif kind == "apply_error":
                    self._append_log(f"\nAWG 输出设置错误: {payload}\n", "err")
                    self.awg_apply_btn.configure(state=tk.NORMAL)
                elif kind == "sweep_done":
                    rec = payload["record"]
                    count = payload["count"]
                    csv_path = payload["csv"]
                    self._append_log(
                        f"\n===== LMS 坐标下降完成 =====\n"
                        f"共评估 {count} 组参数，最优:\n"
                        f"  taps={rec['lms_taps']}, μ1={rec['lms_mu1']:.4e}, "
                        f"μ2={rec['lms_mu2']:.4e}\n"
                        f"  平均 BER={rec['ber_avg']:.4e}\n"
                        f"  完整结果 CSV: {csv_path}\n"
                        f"  已把最优参数写入当前 LMS 设置。\n",
                        "head",
                    )
                    self.taps_var.set(int(rec["lms_taps"]))
                    self.mu1_var.set(float(rec["lms_mu1"]))
                    self.mu2_var.set(float(rec["lms_mu2"]))
                    _persist_setting("LMS_TAPS", int(rec["lms_taps"]))
                    _persist_setting("LMS_MU1", float(rec["lms_mu1"]))
                    _persist_setting("LMS_MU2", float(rec["lms_mu2"]))
                    self._on_done(rec["run_id"])
                    self.sweep_status_var.set(
                        f"最优 BER={rec['ber_avg']:.3e} (taps={rec['lms_taps']})")
                    return
                elif kind == "sweep_error":
                    self._append_log(f"\nLMS 参数扫描错误: {payload}\n", "err")
                    self.sweep_btn.configure(state=tk.NORMAL)
                    self.sweep_status_var.set("扫描失败")
                    self.app.set_running(False)
                elif kind == "clear_done":
                    self._append_log("\n===== AWG 已清空 =====\n", "head")
                    self.awg_clear_btn.configure(state=tk.NORMAL)
                    self._mark_awg_out_of_sync("AWG 已清空，请下载新波形")
                elif kind == "clear_error":
                    self._append_log(f"\nAWG 清空错误: {payload}\n", "err")
                    self.awg_clear_btn.configure(state=tk.NORMAL)
        except queue.Empty:
            pass
        if self._thread is not None and self._thread.is_alive():
            self.after(100, self._poll)

    def _on_done(self, run_id):
        self.run_btn.configure(state=tk.NORMAL)
        self.sweep_btn.configure(state=tk.NORMAL)
        self._update_sweep_state()
        self.app.set_running(False)
        if run_id:
            self.run_status.configure(text="测试完成 ✔")
            self.app.reload_data(select_latest=True)
        else:
            self.run_status.configure(text="测试失败")

    def _quick_plot(self):
        """Generate a TX-only preview without running a full simulation."""
        try:
            datano = min(self.datano_var.get(), 5000)
            seed = self.seed_var.get()
            mode = self.mod_var.get()
            upsampleno = self.upsampleno_var.get()
            v1, v2, _, _ = core.generate_symbols(
                mode, datano, seed, seed + 100, upsampleno=upsampleno)
            tx = core.generate_tx(v1, v2, upsampleno=upsampleno)
            data = {"tx": tx["tx_sum"], "sym": v1 + 1j * v2,
                    "fs": cfg.AWG_SAMPLE * 1e6}
            self.app.show_quick_plots(data)
            self._append_log("快速绘图（仅发射）已生成。")
        except Exception as exc:
            messagebox.showerror("绘图错误", str(exc))
            self._append_log(f"快速绘图错误: {exc}", "err")


class SuperpositionGuiApp(tk.Tk):
    def __init__(self):
        super().__init__()

        _set_windows_taskbar_icon()

        dpi = self.winfo_fpixels("1i")
        self.font_scale = max(dpi / 96.0, 1.0)
        self.tk.call("tk", "scaling", dpi / 72.0)

        self.title(f"{APP_EMOJI} Superposed / QAM Communication System Experiment Platform")
        self._icon, self._icon_ico = _create_emoji_icon(APP_EMOJI)
        if self._icon_ico is not None and sys.platform == "win32":
            try:
                self.iconbitmap(str(self._icon_ico))
            except Exception:
                if self._icon is not None:
                    self.iconphoto(True, self._icon)
        elif self._icon is not None:
            self.iconphoto(True, self._icon)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(int(sw * 0.82), int(1500 * self.font_scale))
        h = min(int(sh * 0.85), int(950 * self.font_scale))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        self.minsize(int(1050 * self.font_scale), int(680 * self.font_scale))
        self.configure(bg=COLOR_BG)

        apply_styles(self, self.font_scale)

        self.runs = []
        self.records = []
        self.current_run = None
        self._record_by_run = {}
        self._running = False

        header = tk.Frame(self, bg=COLOR_BG)
        header.pack(fill=tk.X, padx=16, pady=(14, 6))
        ttk.Label(header, text=f"{APP_EMOJI} Superposed / QAM Communication System Experiment Platform",
                  style="Title.TLabel").pack(side=tk.LEFT)
        self.pill_runs = ttk.Label(header, style="Pill.TLabel")
        self.pill_runs.pack(side=tk.RIGHT, padx=(8, 0))
        self.pill_records = ttk.Label(header, style="Pill.TLabel")
        self.pill_records.pack(side=tk.RIGHT)

        sel = make_card(self)
        sel.pack(fill=tk.X, padx=16, pady=(4, 8))
        inner = tk.Frame(sel, bg=COLOR_CARD)
        inner.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(inner, text="实验 ID (run_id)", style="Section.TLabel"
                  ).pack(side=tk.LEFT)
        self.run_var = tk.StringVar()
        self.run_combo = ttk.Combobox(
            inner, textvariable=self.run_var, state="readonly",
            width=30, font=(FONT_MONO, 10))
        self.run_combo.pack(side=tk.LEFT, padx=(8, 8))
        self.run_combo.bind("<<ComboboxSelected>>",
                            lambda _e: self.select_run(self.run_var.get()))
        ttk.Button(inner, text="⟳ 刷新数据", command=self.reload_data
                   ).pack(side=tk.LEFT)
        self.metrics_var = tk.StringVar(value="")
        metrics_row = tk.Frame(sel, bg=COLOR_CARD)
        metrics_row.pack(fill=tk.X, padx=12, pady=(0, 10))
        ttk.Label(metrics_row, textvariable=self.metrics_var, style="Metrics.TLabel"
                  ).pack(side=tk.LEFT)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

        tab1 = ttk.Frame(self.notebook)
        self.notebook.add(tab1, text="  📈 波形与频谱  ")
        self.panel_wave = PlotPanel(tab1, self, TAB_WAVEFORM)
        self.panel_wave.pack(fill=tk.BOTH, expand=True, padx=2, pady=6)

        tab2 = ttk.Frame(self.notebook)
        self.notebook.add(tab2, text="  🎛 叠加调制  ")
        self.panel_mod = PlotPanel(tab2, self, TAB_MODULATION)
        self.panel_mod.pack(fill=tk.BOTH, expand=True, padx=2, pady=6)

        tab3 = ResultsPanel(self.notebook, self)
        self.notebook.add(tab3, text="  📊 传输实验结果  ")
        self.results_panel = tab3

        tab4 = RunPanel(self.notebook, self)
        self.notebook.add(tab4, text="  ▶ 运行测试  ")
        self.panel_run = tab4

        tab5 = DualKeithley2400Panel(self.notebook, self)
        self.notebook.add(tab5, text="  ⚡ 源表 (3x2400)  ")
        self.panel_smu = tab5

        self.notebook.select(3)

        # 菜单：工具 -> 低代码编辑器
        menubar = tk.Menu(self)
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="低代码流程编辑器",
                               command=self._launch_lowcode)
        menubar.add_cascade(label="工具", menu=tools_menu)
        self.config(menu=menubar)

        self.status_var = tk.StringVar()
        status = tk.Label(self, textvariable=self.status_var, anchor=tk.W,
                          bg=COLOR_BG, fg=COLOR_TEXT_DIM, bd=0,
                          font=(FONT_FAMILY, 9))
        status.pack(fill=tk.X, side=tk.BOTTOM, padx=16, pady=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.reload_data()

    def _launch_lowcode(self):
        """启动低代码流程编辑器（独立窗口）。"""
        import subprocess
        import sys
        try:
            subprocess.Popen(
                [sys.executable, str(cfg.BASE_DIR / "lowcode_app.py")],
                cwd=str(cfg.BASE_DIR))
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))

    def reload_data(self, select_latest=False):
        self.records = list_records()
        self._record_by_run = {r.get("run_id"): r for r in self.records}
        set_record_by_run(self._record_by_run)
        seen = set()
        self.runs = []
        for rec in reversed(self.records):
            run_id = rec.get("run_id", "")
            if run_id and run_id not in seen:
                seen.add(run_id)
                self.runs.append(run_id)
        self.run_combo["values"] = self.runs
        self.pill_runs.configure(text=f"{len(self.runs)} experiments")
        self.pill_records.configure(text=f"{len(self.records)} records")
        if not self.runs:
            self.status_var.set(
                f"No experiment data found ({RECORDS_DIR}). Please run a test on the \"Run Test\" page first.")
            return
        latest = self.runs[0]
        if select_latest or self.current_run not in self.runs:
            self.select_run(latest)
        else:
            self.select_run(self.current_run)
        self.status_var.set(f"Data directory: {cfg.DATA_DIR}; Records directory: {RECORDS_DIR}")

    def select_run(self, run_id, source=None):
        if not run_id:
            return
        self.current_run = run_id
        if self.run_var.get() != run_id:
            self.run_var.set(run_id)
        rec = self._record_by_run.get(run_id)
        if rec:
            rate, awg, up, bw = _rate_info(rec)
            rate_str = (f"{rate:.0f} Mbps ({awg:.0f}/×{up}/{bw:.0f}M)"
                        if rate and awg and bw else
                        (f"{rate:.0f} Mbps" if rate else "N/A"))
            self.metrics_var.set(
                f"模式 {rec.get('modulation_mode', '36QAM')} | "
                f"符号数 {rec.get('datano', '-')} | "
                f"种子 {rec.get('seed', '-')} | "
                f"SNR {rec.get('snr_db', 0):.1f} dB | "
                f"速率 {rate_str} | "
                f"带1 BER {rec.get('ber_band1', 0):.3e} | "
                f"带2 BER {rec.get('ber_band2', 0):.3e} | "
                f"平均 BER {rec.get('ber_avg', 0):.3e} | "
                f"源 {rec.get('data_source', '-')}")
        else:
            self.metrics_var.set("(No record file for this experiment)")
        self.panel_wave.refresh()
        self.panel_mod.refresh()
        if source != "results":
            self.results_panel.refresh()
        else:
            self.results_panel.plot_panel.refresh()

    def show_quick_plots(self, data: dict):
        """Display TX-only quick plot data in a modal preview window."""
        win = tk.Toplevel(self)
        win.title("快速绘图（仅发射）")
        win.geometry("900x650")
        win.transient(self)
        fig = Figure(figsize=(8, 6), dpi=100)
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(canvas, win)
        toolbar.update()

        tx = data["tx"]
        sym = data["sym"]
        fs = data["fs"]

        ax_tx = fig.add_subplot(2, 2, 1)
        ax_spec = fig.add_subplot(2, 2, 3)
        ax_const = fig.add_subplot(2, 2, 4)

        sample_len = min(len(tx), 2000)
        t_axis = np.arange(sample_len) / fs

        ax_tx.plot(t_axis * 1e6, tx[:sample_len])
        ax_tx.set_title("发射波形（两路叠加，前 2000 个采样点）")
        ax_tx.set_xlabel("时间 (us)")
        ax_tx.set_ylabel("幅度")

        nfft = 2 ** int(np.ceil(np.log2(min(len(tx), 8192))))
        f = (np.arange(nfft) - nfft // 2) * fs / nfft / 1e6
        spec = 20 * np.log10(np.abs(np.fft.fftshift(np.fft.fft(tx[:nfft]))) + 1e-12)
        ax_spec.plot(f, spec)
        ax_spec.set_title("发射频谱")
        ax_spec.set_xlabel("频率 (MHz)")
        ax_spec.set_ylabel("幅度 (dB)")

        if sym is not None:
            ax_const.plot(sym.real, sym.imag, "b.", alpha=0.5)
            ax_const.set_title("发射星座图")
            ax_const.set_xlabel("同相 I")
            ax_const.set_ylabel("正交 Q")
            ax_const.grid(True)
            ax_const.axis("equal")

        fig.tight_layout()
        canvas.draw()

    def set_running(self, running):
        self._running = running
        self.run_combo.configure(state="readonly" if not running
                                 else tk.DISABLED)

    def _on_close(self):
        """Clean up the source-meter connections and persist addresses before exit."""
        try:
            _persist_addresses(
                osc_addr=self.panel_run.oscaddr_var.get(),
                awg_addr=self.panel_run.awgaddr_var.get(),
                osc_channel=self.panel_run.oscchan_var.get(),
                osc_dual=self.panel_run.oscdual_var.get(),
            )
        except Exception:
            pass
        try:
            if hasattr(self, "panel_smu"):
                self.panel_smu.on_close()
        except Exception:
            pass
        try:
            if hasattr(self, "panel_run") and self.panel_run._log_file is not None:
                self.panel_run._log_file.close()
        except Exception:
            pass
        self.destroy()


def main():
    enable_dpi_awareness()
    app = SuperpositionGuiApp()
    app.mainloop()


if __name__ == "__main__":
    main()
