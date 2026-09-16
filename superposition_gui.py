# -*- coding: utf-8 -*-
"""Superposed 16QAM Communication System Experiment Platform GUI.

Five tabs:
    1. Waveform & Spectrum    —— TX/RX time-domain waveforms and spectra
    2. Superposition Modulation —— superposed constellation and density plots
    3. Transmission Results   —— experiment record table and run-history trend
    4. Run Test               —— run the transceiver from the GUI with live log output
    5. Oscilloscope           —— TCP/IP control of the Keysight oscilloscope

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
import main as main_flow
import superposition_core as core
from record import generate_run_id, save_record
from oscilloscope import KeysightScope, ScopeError

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
    """Regenerate the TX superposed constellation (v1 + 1j*v2) for a record."""
    seed = int(rec.get("seed", cfg.SEED_BAND1))
    datano = int(rec.get("datano", cfg.DATANO))
    dec1, dec2 = core.generate_pam4_streams(min(datano, max_count), seed, seed + 100)
    v1 = core.pam4_to_pam6(dec1)
    v2 = core.pam4_to_pam6(dec2)
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
    ax.set_xlabel("同相 I（带1 PAM6）")
    ax.set_ylabel("正交 Q（带2 PAM6）")
    ax.grid(True, alpha=0.3)
    ax.axis("equal")
    fig.tight_layout()


def build_rx_constellation(fig, run_id, title):
    rec = _get_record(run_id)
    iq = _load_eq_symbols(rec)
    ax = fig.add_subplot(111)
    if iq is not None and len(iq):
        ax.plot(iq.real, iq.imag, "b.", alpha=0.4, markersize=4)
        maxaxis = int(np.log2(cfg.PAM_ORDER)) * 2 + 2
        ax.set_xlim(-maxaxis, maxaxis)
        ax.set_ylim(-maxaxis, maxaxis)
    ax.set_title(title)
    ax.set_xlabel("同相 I（带1 PAM6）")
    ax.set_ylabel("正交 Q（带2 PAM6）")
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
    ax.set_xlabel("同相 I（带1 PAM6）")
    ax.set_ylabel("正交 Q（带2 PAM6）")
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
    "tx_constellation":      (build_tx_constellation,      "发射叠加星座图（PAM6 x PAM6）"),
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
    font_base = (FONT_FAMILY, 10)
    font_bold = (FONT_FAMILY, 10, "bold")
    font_tab = (FONT_FAMILY, 11)
    pad_x = int(round(14 * f))
    pad_y = int(round(8 * f))

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
                    font=(FONT_FAMILY, 17, "bold"))
    style.configure("Subtitle.TLabel", background=COLOR_BG,
                    foreground=COLOR_TEXT_DIM,
                    font=(FONT_FAMILY, 10))
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
                    rowheight=int(round(28 * f)), font=font_base,
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

    COLUMNS = ("run_id", "time", "src", "datano", "snr", "ber1", "ber2", "ber_avg")
    HEADINGS = {
        "run_id": ("Experiment ID", 170),
        "time": ("Time", 150),
        "src": ("Source", 80),
        "datano": ("Symbols", 80),
        "snr": ("SNR (dB)", 80),
        "ber1": ("BER 带1", 110),
        "ber2": ("BER 带2", 110),
        "ber_avg": ("平均 BER", 110),
    }

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

        top = ttk.LabelFrame(self, text=" 传输实验记录（点击行切换实验） ")
        top.pack(fill=tk.X, padx=2, pady=(2, 8))
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
        self.tree.bind("<<TreeviewSelect>>", self._on_row_select)

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


class RunPanel(ttk.Frame):
    """Tab 4: run the superposed transceiver from the GUI with live log output."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None

        # ── Parameter card ───────────────────────────────────────────
        opt = ttk.LabelFrame(self, text=" 实验参数 ")
        opt.pack(fill=tk.X, padx=2, pady=(2, 8))

        ttk.Label(opt, text="数据源:", style="Card.TLabel"
                  ).grid(row=0, column=0, sticky=tk.W, padx=12, pady=(10, 4))
        self.src_var = tk.StringVar(value=cfg.DATA_SOURCE)
        self.src_combo = ttk.Combobox(
            opt, textvariable=self.src_var,
            values=["virtual", "file", "scope"],
            state="readonly", width=16)
        self.src_combo.grid(row=0, column=1, sticky=tk.W, padx=(4, 16), pady=(10, 4))
        self.src_combo.bind("<<ComboboxSelected>>", self._on_src_change)

        ttk.Label(opt, text="符号数:", style="Card.TLabel"
                  ).grid(row=0, column=2, sticky=tk.W, padx=12, pady=(10, 4))
        self.datano_var = tk.IntVar(value=cfg.DATANO)
        tk.Spinbox(opt, from_=1024, to=1024 * 512, increment=1024,
                   textvariable=self.datano_var, width=16
                   ).grid(row=0, column=3, sticky=tk.W, padx=(4, 16), pady=(10, 4))

        ttk.Label(opt, text="信噪比 (dB):", style="Card.TLabel"
                  ).grid(row=1, column=0, sticky=tk.W, padx=12, pady=4)
        self.snr_var = tk.DoubleVar(value=cfg.SNR_DB)
        tk.Spinbox(opt, from_=0.0, to=50.0, increment=0.5,
                   textvariable=self.snr_var, width=16
                   ).grid(row=1, column=1, sticky=tk.W, padx=(4, 16), pady=4)

        ttk.Label(opt, text="随机种子:", style="Card.TLabel"
                  ).grid(row=1, column=2, sticky=tk.W, padx=12, pady=4)
        self.seed_var = tk.IntVar(value=cfg.SEED_BAND1)
        tk.Spinbox(opt, from_=0, to=10000, textvariable=self.seed_var, width=16
                   ).grid(row=1, column=3, sticky=tk.W, padx=(4, 16), pady=4)

        ttk.Label(opt, text="LMS 抽头数:", style="Card.TLabel"
                  ).grid(row=2, column=0, sticky=tk.W, padx=12, pady=4)
        self.taps_var = tk.IntVar(value=cfg.LMS_TAPS)
        tk.Spinbox(opt, from_=3, to=51, increment=2, textvariable=self.taps_var,
                   width=16).grid(row=2, column=1, sticky=tk.W, padx=(4, 16), pady=4)

        ttk.Label(opt, text="LMS 步长 μ1:", style="Card.TLabel"
                  ).grid(row=2, column=2, sticky=tk.W, padx=12, pady=4)
        self.mu1_var = tk.DoubleVar(value=cfg.LMS_MU1)
        tk.Spinbox(opt, from_=0.0001, to=1.0, increment=0.0005,
                   textvariable=self.mu1_var, width=16
                   ).grid(row=2, column=3, sticky=tk.W, padx=(4, 16), pady=4)

        ttk.Label(opt, text="LMS 步长 μ2:", style="Card.TLabel"
                  ).grid(row=3, column=0, sticky=tk.W, padx=12, pady=4)
        self.mu2_var = tk.DoubleVar(value=cfg.LMS_MU2)
        tk.Spinbox(opt, from_=0.0001, to=1.0, increment=0.0005,
                   textvariable=self.mu2_var, width=16
                   ).grid(row=3, column=1, sticky=tk.W, padx=(4, 16), pady=4)

        ttk.Label(opt, text="训练符号数:", style="Card.TLabel"
                  ).grid(row=3, column=2, sticky=tk.W, padx=12, pady=4)
        self.ts_var = tk.IntVar(value=cfg.NUMOF_TS)
        tk.Spinbox(opt, from_=100, to=20000, increment=100,
                   textvariable=self.ts_var, width=16
                   ).grid(row=3, column=3, sticky=tk.W, padx=(4, 16), pady=4)

        ttk.Label(opt, text="接收文件:", style="Card.TLabel"
                  ).grid(row=4, column=0, sticky=tk.W, padx=12, pady=4)
        self.rxfile_var = tk.StringVar(value="")
        self.rxfile_entry = ttk.Entry(opt, textvariable=self.rxfile_var, width=20)
        self.rxfile_entry.grid(row=4, column=1, sticky=tk.W, padx=(4, 4), pady=4)
        self.rxfile_btn = ttk.Button(opt, text="浏览…", command=self._browse_rx)
        self.rxfile_btn.grid(row=4, column=1, sticky=tk.E, padx=(4, 16), pady=4)

        ttk.Label(opt, text="示波器地址:", style="Card.TLabel"
                  ).grid(row=4, column=2, sticky=tk.W, padx=12, pady=4)
        self.oscaddr_var = tk.StringVar(value=cfg.OSC_VISA_ADDR)
        self.oscaddr_entry = ttk.Entry(opt, textvariable=self.oscaddr_var, width=24)
        self.oscaddr_entry.grid(row=4, column=3, sticky=tk.W, padx=(4, 16), pady=4)

        btn_bar = tk.Frame(opt, bg=COLOR_CARD)
        btn_bar.grid(row=5, column=0, columnspan=4, sticky=tk.W,
                     padx=12, pady=(4, 8))
        self.run_btn = ttk.Button(btn_bar, text="▶  运行仿真",
                                  style="Accent.TButton",
                                  command=self.start_run)
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.quick_btn = ttk.Button(btn_bar, text="⚡ 快速绘图（仅发射）",
                                    command=self._quick_plot)
        self.quick_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.run_status = ttk.Label(btn_bar, text="就绪", style="DimCard.TLabel")
        self.run_status.pack(side=tk.LEFT, padx=16)

        # ── Bottom: log card ─────────────────────────────────────────
        log_card = ttk.LabelFrame(self, text=" 运行日志 ")
        log_card.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        log_frame = tk.Frame(log_card, bg=COLOR_CARD)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text = tk.Text(
            log_frame, wrap=tk.NONE, state=tk.DISABLED, width=40,
            font=(FONT_MONO, 9),
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
            "提示：选择数据源与参数后点击“运行仿真”。\n"
            "virtual = 虚拟信道离线仿真；file = 读取已保存接收波形；scope = 在线采集示波器。\n"
            "运行完成后会自动刷新并切换到最新实验。\n", "head")

        self._on_src_change()

    def _on_src_change(self, _event=None):
        src = self.src_var.get()
        file_state = tk.NORMAL if src == "file" else tk.DISABLED
        scope_state = tk.NORMAL if src == "scope" else tk.DISABLED
        self.rxfile_entry.configure(state=file_state)
        self.rxfile_btn.configure(state=file_state)
        self.oscaddr_entry.configure(state=scope_state)

    def _browse_rx(self):
        path = filedialog.askopenfilename(
            initialdir=str(cfg.RXDATA_DIR),
            filetypes=[("Text 波形", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.rxfile_var.set(path)

    def _append_log(self, text, tag=None):
        self.log_text.configure(state=tk.NORMAL)
        if tag:
            self.log_text.insert(tk.END, text, tag)
        else:
            self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

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
        self._append_log("\n========== Starting Superposed 16QAM Simulation ==========\n", "head")
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()
        self.after(100, self._poll)

    def _run_thread(self):
        try:
            src = self.src_var.get()
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
                log=lambda msg: self._queue.put(("log", msg)),
            )
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
                    self._on_done(None)
                elif kind == "done":
                    self._append_log("\n========== Simulation Completed ==========\n", "head")
                    self._on_done(payload)
                    return
        except queue.Empty:
            pass
        if self._thread is not None and self._thread.is_alive():
            self.after(100, self._poll)

    def _on_done(self, run_id):
        self.run_btn.configure(state=tk.NORMAL)
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
            dec1, dec2 = core.generate_pam4_streams(datano, seed, seed + 100)
            v1 = core.pam4_to_pam6(dec1)
            v2 = core.pam4_to_pam6(dec2)
            tx = core.generate_tx(v1, v2)
            data = {"tx": tx["tx_sum"], "sym": v1 + 1j * v2,
                    "fs": cfg.AWG_SAMPLE * 1e6}
            self.app.show_quick_plots(data)
            self._append_log("快速绘图（仅发射）已生成。")
        except Exception as exc:
            messagebox.showerror("绘图错误", str(exc))
            self._append_log(f"快速绘图错误: {exc}", "err")


class ScopePanel(ttk.Frame):
    """Tab 5: control a Keysight oscilloscope over TCP/IP (acquire & save)."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._scope = KeysightScope()

        # ── Connection card ──────────────────────────────────────────
        conn = ttk.LabelFrame(self, text=" 连接 ")
        conn.pack(fill=tk.X, padx=2, pady=(2, 8))
        ttk.Label(conn, text="VISA 地址:", style="Card.TLabel"
                  ).grid(row=0, column=0, sticky=tk.W, padx=12, pady=(10, 4))
        self.addr_var = tk.StringVar(value=cfg.OSC_VISA_ADDR)
        ttk.Entry(conn, textvariable=self.addr_var, width=40
                  ).grid(row=0, column=1, sticky=tk.W, padx=(4, 16), pady=(10, 4))
        self.conn_btn = ttk.Button(conn, text="连接", style="Accent.TButton",
                                   command=self._toggle_connect)
        self.conn_btn.grid(row=0, column=2, sticky=tk.W, padx=(4, 12), pady=(10, 4))
        self.idn_var = tk.StringVar(value="未连接")
        ttk.Label(conn, textvariable=self.idn_var, style="DimCard.TLabel"
                  ).grid(row=0, column=3, sticky=tk.W, padx=8, pady=(10, 4))

        # ── Acquisition card ─────────────────────────────────────────
        acq = ttk.LabelFrame(self, text=" 采集设置 ")
        acq.pack(fill=tk.X, padx=2, pady=(2, 8))
        ttk.Label(acq, text="通道:", style="Card.TLabel"
                  ).grid(row=0, column=0, sticky=tk.W, padx=12, pady=(10, 4))
        self.chan_var = tk.StringVar(value=cfg.OSC_CHANNEL)
        ttk.Combobox(acq, textvariable=self.chan_var, state="readonly", width=10,
                     values=["CHAN1", "CHAN2", "CHAN3", "CHAN4"]
                     ).grid(row=0, column=1, sticky=tk.W, padx=(4, 16), pady=(10, 4))
        ttk.Label(acq, text="采样率 (MSa/s):", style="Card.TLabel"
                  ).grid(row=0, column=2, sticky=tk.W, padx=12, pady=(10, 4))
        self.srate_var = tk.DoubleVar(value=cfg.OSC_SAMPLE)
        tk.Spinbox(acq, from_=100, to=8000, increment=100,
                   textvariable=self.srate_var, width=10
                   ).grid(row=0, column=3, sticky=tk.W, padx=(4, 16), pady=(10, 4))
        ttk.Label(acq, text="时基 (us):", style="Card.TLabel"
                  ).grid(row=0, column=4, sticky=tk.W, padx=12, pady=(10, 4))
        self.tb_var = tk.DoubleVar(value=cfg.OSC_TIMEBASE_SCALE * 1e6)
        tk.Spinbox(acq, from_=1.0, to=1000.0, increment=5.0,
                   textvariable=self.tb_var, width=10
                   ).grid(row=0, column=5, sticky=tk.W, padx=(4, 16), pady=(10, 4))
        self.acq_btn = ttk.Button(acq, text="采集并保存", style="Accent.TButton",
                                  command=self._start_acquire)
        self.acq_btn.grid(row=0, column=6, sticky=tk.W, padx=(8, 12), pady=(10, 4))
        ttk.Button(acq, text="采集并入栈运行",
                   command=self._start_acquire_and_run
                   ).grid(row=0, column=7, sticky=tk.W, padx=(4, 12), pady=(10, 4))

        # ── Log card ─────────────────────────────────────────────────
        log_card = ttk.LabelFrame(self, text=" 运行日志 ")
        log_card.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        log_frame = tk.Frame(log_card, bg=COLOR_CARD)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.log_text = tk.Text(
            log_frame, wrap=tk.NONE, state=tk.DISABLED, width=40,
            font=(FONT_MONO, 9),
            bg="#0F172A", fg="#E2E8F0", bd=0, highlightthickness=0,
            insertbackground="#E2E8F0")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lsb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                            command=self.log_text.yview)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.tag_configure("head", foreground="#22D3EE")
        self.log_text.tag_configure("err", foreground="#F87171")
        self._append_log("提示：输入 VISA 地址（如 TCPIP0::169.254.140.83::5025::SOCKET）后点击“连接”。\n",
                         "head")

    # ------------------------------------------------------------------
    def _log(self, text: str, tag: Optional[str] = None):
        self.log_text.configure(state=tk.NORMAL)
        if tag:
            self.log_text.insert(tk.END, text, tag)
        else:
            self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _toggle_connect(self):
        if self._scope.is_connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        try:
            self._scope = KeysightScope(self.addr_var.get().strip())
            self._scope.connect()
            idn = self._scope.idn()
        except Exception as exc:
            self._log(f"连接失败: {exc}\n", "err")
            self._scope = KeysightScope()
            return
        self.idn_var.set(idn)
        self.conn_btn.configure(text="断开")
        self._log(f"已连接: {idn}\n", "head")

    def _disconnect(self):
        try:
            self._scope.disconnect()
        except Exception:
            pass
        self.idn_var.set("未连接")
        self.conn_btn.configure(text="连接")
        self._log("已断开。\n")

    def _start_acquire(self, then_run: bool = False):
        if self._thread is not None and self._thread.is_alive():
            return
        self.acq_btn.configure(state=tk.DISABLED)
        self._log("\n===== 开始采集 =====\n", "head")
        self._thread = threading.Thread(
            target=self._acquire_thread, args=(then_run,), daemon=True)
        self._thread.start()
        self.after(100, self._poll)

    def _start_acquire_and_run(self):
        self._start_acquire(then_run=True)

    def _acquire_thread(self, then_run: bool):
        try:
            if not self._scope.is_connected:
                self._queue.put(("log", "正在自动连接…"))
                self._scope = KeysightScope(self.addr_var.get().strip())
                self._scope.connect()
                self._queue.put(("idn", self._scope.idn()))
            self._scope.configure(self.srate_var.get() * 1e6,
                                  self.tb_var.get() * 1e-6)
            result = self._scope.acquire(self.chan_var.get())
            y = result["ydata"]
            from datetime import datetime
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = cfg.RXDATA_DIR / f"scope_{stamp}.txt"
            np.savetxt(path, y)
            self._queue.put(("log",
                             f"采集完成: {result['channel']}, {len(y)} 点, "
                             f"SRATE={1 / result['preamble']['x_increment'] / 1e6:.1f} MSa/s"))
            self._queue.put(("log", f"已保存: {path}"))
            self._queue.put(("acq_done", str(path)))
        except Exception as exc:
            self._queue.put(("error", exc))

    def _poll(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._log(payload + "\n")
                elif kind == "idn":
                    self.idn_var.set(payload)
                    self.conn_btn.configure(text="断开")
                    self._log(f"已连接: {payload}\n", "head")
                elif kind == "error":
                    self._log(f"\nError: {payload}\n", "err")
                    self.acq_btn.configure(state=tk.NORMAL)
                    return
                elif kind == "acq_done":
                    self._log("===== 采集结束 =====\n", "head")
                    self.acq_btn.configure(state=tk.NORMAL)
                    return
        except queue.Empty:
            pass
        if self._thread is not None and self._thread.is_alive():
            self.after(100, self._poll)

    def on_close(self):
        try:
            if self._scope is not None:
                self._scope.disconnect()
        except Exception:
            pass


class SuperpositionGuiApp(tk.Tk):
    def __init__(self):
        super().__init__()

        _set_windows_taskbar_icon()

        dpi = self.winfo_fpixels("1i")
        self.font_scale = max(dpi / 96.0, 1.0)
        self.tk.call("tk", "scaling", dpi / 72.0)

        self.title(f"{APP_EMOJI} Superposed 16QAM Communication System Experiment Platform")
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
        ttk.Label(header, text=f"{APP_EMOJI} Superposed 16QAM Communication System Experiment Platform",
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
        ttk.Label(inner, textvariable=self.metrics_var, style="Metrics.TLabel"
                  ).pack(side=tk.LEFT, padx=20)

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

        tab5 = ScopePanel(self.notebook, self)
        self.notebook.add(tab5, text="  📟 示波器  ")
        self.panel_scope = tab5

        self.notebook.select(3)

        self.status_var = tk.StringVar()
        status = tk.Label(self, textvariable=self.status_var, anchor=tk.W,
                          bg=COLOR_BG, fg=COLOR_TEXT_DIM, bd=0,
                          font=(FONT_FAMILY, 9))
        status.pack(fill=tk.X, side=tk.BOTTOM, padx=16, pady=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.reload_data()

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
            self.metrics_var.set(
                f"符号数 {rec.get('datano', '-')} | "
                f"种子 {rec.get('seed', '-')} | "
                f"SNR {rec.get('snr_db', 0):.1f} dB | "
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
            ax_const.set_title("发射叠加星座图（PAM6 x PAM6）")
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
        """Clean up the oscilloscope connection before exit."""
        try:
            if hasattr(self, "panel_scope"):
                self.panel_scope.on_close()
        except Exception:
            pass
        self.destroy()


def main():
    enable_dpi_awareness()
    app = SuperpositionGuiApp()
    app.mainloop()


if __name__ == "__main__":
    main()
