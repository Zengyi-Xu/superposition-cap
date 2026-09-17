"""Superposed 16QAM 低代码节点库。

把本项目的收发链路（PAM4 序列生成 / PAM6 差分映射 / 发射成形 / 信道 /
同步 / 下变频 / MIMO LMS 均衡 / PAM6 判决 / PAM4 解码 / BER / AWG 下载 /
示波器采集 / 画图 / 变量存取）封装为可拖拽的功能节点：

- 输入端口：接收上游节点传递过来的变量
- 输出端口：把计算结果传递给下游节点
- 参数：在属性面板中设置（对应函数的关键字参数）

节点函数签名统一为 func(inputs, params, ctx) -> dict，
inputs/返回值均以端口名称为键。算法实现直接复用 superposition_core /
channel / equalizer / awg_m8190a / oscilloscope / utils 中的函数，
保证与 main.py 流程一致。
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

import config
import superposition_core as core
from equalizer import mimo_lms_equalizer
from utils import db_spectrum, load_txt, save_txt, sync_by_xcorr


# ----------------------------------------------------------------------
# 节点定义结构
# ----------------------------------------------------------------------
@dataclass
class PortDef:
    name: str
    label: str
    data_type: str = "any"
    required: bool = True   # 仅对输入端口有意义


@dataclass
class ParamDef:
    name: str
    label: str
    type: str = "str"       # float | int | str | bool | choice
    default: Any = ""
    choices: List[str] = field(default_factory=list)
    minimum: Optional[float] = None
    maximum: Optional[float] = None


@dataclass
class NodeDef:
    type_id: str
    label: str
    category: str
    description: str
    inputs: List[PortDef]
    outputs: List[PortDef]
    params: List[ParamDef]
    func: Optional[Callable[[Dict[str, Any], Dict[str, Any], Any], Dict[str, Any]]]


# 节点类别配色（供编辑器使用）
CATEGORY_COLORS = {
    "数据源":   "#3B82F6",
    "发射":     "#06B6D4",
    "信道":     "#F59E0B",
    "硬件":     "#B45309",
    "接收":     "#8B5CF6",
    "解调":     "#0891B2",
    "分析":     "#EA580C",
    "画图":     "#65A30D",
    "变量存取": "#475569",
    "标注":     "#94A3B8",
}

NODES: Dict[str, NodeDef] = {}


def register(node_def: NodeDef) -> NodeDef:
    NODES[node_def.type_id] = node_def
    return node_def


def get_node_def(type_id: str) -> Optional[NodeDef]:
    return NODES.get(type_id)


def node_types_by_category() -> Dict[str, List[NodeDef]]:
    cats: Dict[str, List[NodeDef]] = {}
    for nd in NODES.values():
        cats.setdefault(nd.category, []).append(nd)
    return cats


def _out_path(ctx, filename: str) -> Path:
    """把相对文件名解析到本次运行目录。"""
    p = Path(filename)
    if p.is_absolute():
        return p
    return Path(ctx.run_dir) / p


def _plot_show(params: Dict[str, Any]) -> bool:
    return bool(params.get("show", False))


def _latest_file(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime,
                     reverse=True)
    if not matches:
        raise FileNotFoundError(f"{directory} 下没有 {pattern} 文件")
    return matches[0]


# ======================================================================
# 数据源
# ======================================================================
def _fn_const(inputs, params, ctx):
    """常量节点：把文本解析为 Python 数值/数组。"""
    text = str(params.get("value", "0"))
    try:
        value = eval(text, {"__builtins__": {}}, {"np": np})  # noqa: S307
    except Exception as exc:
        raise ValueError(f"常量表达式无法解析: {text!r} ({exc})")
    ctx.log(f"常量 = {value!r}")
    return {"value": value}


def _fn_pam4_source(inputs, params, ctx):
    """生成两路随机 PAM4 十进制序列（rng(seed)/rng(seed+100)，与 main.py 一致）。"""
    datano = int(params.get("datano", config.DATANO))
    seed = int(params.get("seed", config.SEED_BAND1))
    dec1, dec2 = core.generate_pam4_streams(datano, seed, seed + 100)
    ctx.log(f"PAM4 序列: 2 路 x {datano} 符号, seed={seed}/{seed + 100}")
    return {"dec1": dec1, "dec2": dec2}


def _fn_symbol_source(inputs, params, ctx):
    """通用符号序列源：支持 superposed/4QAM/16QAM/64QAM/36QAM_NLTCP。"""
    datano = int(params.get("datano", config.DATANO))
    seed = int(params.get("seed", config.SEED_BAND1))
    mode = str(params.get("modulation", config.MODULATION_MODE))
    lambda_ = float(params.get("shaping_lambda", config.NLTCP_SHAPING_FACTOR))
    v1, v2, dec1, dec2 = core.generate_symbols(mode, datano, seed, seed + 100, lambda_)
    ctx.log(f"符号序列 ({mode}): 2 路 x {datano} 符号, seed={seed}/{seed + 100}")
    return {"dec1": dec1, "dec2": dec2, "v1": v1, "v2": v2}


def _fn_load_var(inputs, params, ctx):
    """从文件读取变量（txt / npy）。"""
    path = Path(str(params.get("path", "")))
    if not str(path):
        raise ValueError("请在参数中填写文件路径")
    if not path.is_absolute():
        path = config.BASE_DIR / path
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    fmt = params.get("format", "auto")
    if fmt == "auto":
        fmt = path.suffix.lstrip(".").lower()
    if fmt == "txt":
        value = load_txt(path)
    elif fmt == "npy":
        value = np.load(path, allow_pickle=True)
    else:
        raise ValueError(f"不支持的格式: {fmt}")
    arr = np.asarray(value) if not isinstance(value, dict) else None
    ctx.log(f"读取变量 <- {path}"
            + (f" shape={arr.shape} dtype={arr.dtype}" if arr is not None else ""))
    return {"value": value}


def _fn_load_rx_file(inputs, params, ctx):
    """读取接收波形文件；路径留空时自动取 rxdata 目录下最新的 rx_*.txt。"""
    path_str = str(params.get("path", "")).strip()
    if path_str:
        path = Path(path_str)
        if not path.is_absolute():
            path = config.BASE_DIR / path
    else:
        path = _latest_file(config.RXDATA_DIR, "rx_*.txt")
    rx = load_txt(path)
    ctx.log(f"读取 RX 波形 <- {path} (len={len(rx)})")
    return {"waveform": rx}


# ======================================================================
# 发射
# ======================================================================
def _fn_pam4_to_pam6(inputs, params, ctx):
    """PAM4 -> PAM6 差分映射，缩放到 {-5,-3,-1,1,3,5}。"""
    v1 = core.pam4_to_pam6(np.asarray(inputs["dec1"]).ravel().astype(int))
    v2 = core.pam4_to_pam6(np.asarray(inputs["dec2"]).ravel().astype(int))
    ctx.log(f"PAM6 映射: {len(v1)} + {len(v2)} 符号")
    return {"v1": v1, "v2": v2}


def _fn_tx_generate(inputs, params, ctx):
    """发射机：上采样 -> SRRC 成形 -> cos/sin 子载波上变频 -> 功率归一化。"""
    v1 = np.asarray(inputs["v1"]).ravel()
    v2 = np.asarray(inputs["v2"]).ravel()
    tx = core.generate_tx(v1, v2)
    ctx.log(f"发射成形: data1/data2 各 {len(tx['data1'])} 点, "
            f"tx_sum {len(tx['tx_sum'])} 点")
    aux = {"cos1": tx["cos1"], "sin1": tx["sin1"],
           "filter_cos": tx["filter_cos"]}
    return {"data1": tx["data1"], "data2": tx["data2"],
            "tx_sum": tx["tx_sum"], "aux": aux}


# ======================================================================
# 信道
# ======================================================================
def _fn_virtual_channel(inputs, params, ctx):
    """虚拟 VLC 信道：指数衰减频率响应 + AWGN（可选 LED 非线性）。"""
    import channel
    x = np.asarray(inputs["waveform_in"]).ravel()
    rx = channel.vlc_channel(
        x,
        snr_db=float(params.get("snr_db", config.SNR_DB)),
        fs_hz=float(params.get("fs_hz", config.CHANNEL_FS)),
        factor=float(params.get("factor", config.CHANNEL_FACTOR)),
        nonlinear=bool(params.get("nonlinear", config.CHANNEL_NONLINEAR)),
    )
    ctx.log(f"虚拟信道: SNR={float(params.get('snr_db', config.SNR_DB)):.1f} dB, "
            f"{len(x)} -> {len(rx)} 点")
    return {"waveform": rx}


# ======================================================================
# 硬件
# ======================================================================
def _fn_awg_download_dual(inputs, params, ctx):
    """把两路波形分别下载到 AWG520 的 CH1 / CH2 并同步播放（需硬件）。"""
    import awg_m8190a
    w1 = np.asarray(inputs["wave1"]).ravel()
    w2 = np.asarray(inputs["wave2"]).ravel()
    awg_m8190a.download_two_channels(
        w1, w2,
        vpp=float(params.get("vpp", config.AWG_VPP)),
        visa_addr=str(params.get("visa_addr", config.AWG_VISA_ADDR)),
        log=ctx.log,
    )
    ctx.log(f"AWG520 双通道下载完成: {len(w1)}/{len(w2)} 点")
    return {}


def _fn_scope_capture(inputs, params, ctx):
    """从 Keysight 示波器采集波形并重采样到 AWG 速率（需硬件）。"""
    from oscilloscope import acquire_waveform
    from utils import resample_ratio
    addr = str(params.get("visa_addr", config.OSC_VISA_ADDR))
    result = acquire_waveform(
        visa_addr=addr,
        channel=str(params.get("channel", config.OSC_CHANNEL)),
        sample_rate=float(params.get("sample_rate", config.OSC_SAMPLE_RATE)),
        timebase_scale=float(params.get("timebase_scale", config.OSC_TIMEBASE_SCALE)),
    )
    rx = result["ydata"]
    rx = resample_ratio(rx, config.OSC_SAMPLE, config.AWG_SAMPLE)
    ctx.log(f"示波器采集完成: {result['channel']}, {len(rx)} 点（已重采样到 "
            f"{config.AWG_SAMPLE} MSa/s）")
    return {"waveform": rx}


# ======================================================================
# 接收
# ======================================================================
def _fn_sync_xcorr(inputs, params, ctx):
    """互相关同步：截取与 TX 等长的 RX 片段（与 A2_RX 一致）。"""
    rx = np.asarray(inputs["rx"]).ravel()
    tx = np.asarray(inputs["tx_ref"]).ravel()
    datarx, offset, _, _ = sync_by_xcorr(rx, tx)
    ctx.log(f"xcorr 同步: 偏移 {offset} 点")
    return {"datarx": datarx, "offset": int(offset)}


def _fn_downconvert(inputs, params, ctx):
    """正交下变频 + 匹配滤波 + 按上采样倍数抽取，输出复数符号流。"""
    aux = inputs["aux"]
    data_recover = core.downconvert_to_symbols(
        np.asarray(inputs["datarx"]).ravel(),
        aux["cos1"], aux["sin1"], aux["filter_cos"],
        int(params.get("upsampleno", config.UPSAMPLENO)),
        int(params.get("offset", 0)),
    )
    ctx.log(f"下变频: 符号流 {len(data_recover)} 点")
    return {"symbols": data_recover}


def _fn_mimo_lms(inputs, params, ctx):
    """2x2 MIMO LMS 均衡：分离 I/Q 两路，输出复数恢复符号流。

    与 main.py 一致：e1 由 (r1, r2) 估计 v1，e2 由 (r2, r1) 估计 v2，
    recover = e1*rms(v1) + j * e2*rms(v2)。
    """
    symbols = np.asarray(inputs["symbols"])
    r1, r2 = np.real(symbols), np.imag(symbols)
    v1 = np.asarray(inputs["v1"]).ravel()
    v2 = np.asarray(inputs["v2"]).ravel()
    taps = int(params.get("taps", config.LMS_TAPS))
    mu1 = float(params.get("mu1", config.LMS_MU1))
    mu2 = float(params.get("mu2", config.LMS_MU2))
    nts = int(params.get("numof_ts", config.NUMOF_TS))
    e1, _, _, _, _ = mimo_lms_equalizer(taps, mu1, mu2, nts, r1, r2, v1, v2)
    e2, _, _, _, _ = mimo_lms_equalizer(taps, mu1, mu2, nts, r2, r1, v2, v1)
    recover = e1 * np.sqrt(np.mean(v1 ** 2)) + 1j * e2 * np.sqrt(np.mean(v2 ** 2))
    ctx.log(f"MIMO LMS 均衡: taps={taps}, mu=({mu1}, {mu2}), 训练 {nts} 符号")
    return {"recover": recover}


# ======================================================================
# 解调
# ======================================================================
def _fn_pam6_decide(inputs, params, ctx):
    """PAM6 最近邻判决：recover = e1*avp1 + j*e2*avp2 -> 两路 0..5 判决。

    与 main.py 一致：real(recover)/2 + 2.5 后判决到 0..5。
    """
    recover = np.asarray(inputs["recover"])
    dec1 = core.pam6_demodulate(np.real(recover) / 2.0 + 2.5)
    dec2 = core.pam6_demodulate(np.imag(recover) / 2.0 + 2.5)
    ctx.log(f"PAM6 判决: {len(dec1)} + {len(dec2)} 符号")
    return {"dec1": dec1, "dec2": dec2}


def _fn_symbol_decide(inputs, params, ctx):
    """通用判决：按调制模式对恢复符号流进行判决（superposed 会差分解码回 PAM4）。"""
    recover = np.asarray(inputs["recover"])
    mode = str(params.get("modulation", config.MODULATION_MODE))
    dec1, dec2 = core.demodulate_symbols(recover, mode)
    ctx.log(f"{mode} 判决: {len(dec1)} + {len(dec2)} 符号")
    return {"dec1": dec1, "dec2": dec2}


def _fn_pam4_decode(inputs, params, ctx):
    """PAM6 差分解码回 PAM4（0..3）。"""
    d1 = core.pam6_to_pam4(np.asarray(inputs["dec6_1"]).ravel().astype(int))
    d2 = core.pam6_to_pam4(np.asarray(inputs["dec6_2"]).ravel().astype(int))
    return {"dec1": d1.astype(int), "dec2": d2.astype(int)}


def _fn_ber(inputs, params, ctx):
    """对比发送/接收十进制符号，统计误码率（自然二进制逐比特，与 biterr 一致）。"""
    tx_dec = np.asarray(inputs["tx_dec"]).ravel().astype(int)
    rx_dec = np.asarray(inputs["rx_dec"]).ravel().astype(int)
    n = min(len(tx_dec), len(rx_dec))
    tx_dec, rx_dec = tx_dec[:n], rx_dec[:n]
    errs, ber = core.biterr(rx_dec, tx_dec)
    ctx.log(f"误码统计: {errs}/{n * 2} 比特, BER={ber:.6e}")
    return {"ber": float(ber), "errors": int(errs), "count": int(n)}


def _fn_ber_report(inputs, params, ctx):
    """一次性统计两路 BER（跳过 LMS 抽头边缘，与 main.py 一致）。

    superposed 模式下输出 PAM6/PAM4 双层指标；其它模式输出单层 I/Q 支路指标。
    """
    recover = np.asarray(inputs["recover"])
    decimal1 = np.asarray(inputs["dec1_tx"]).ravel().astype(int)
    decimal2 = np.asarray(inputs["dec2_tx"]).ravel().astype(int)
    v1 = np.asarray(inputs["v1"]).ravel()
    v2 = np.asarray(inputs["v2"]).ravel()
    taps = int(params.get("taps", config.LMS_TAPS))
    mode = str(params.get("modulation", config.MODULATION_MODE))
    datano = len(decimal1)

    rx_dec1, rx_dec2 = core.demodulate_symbols(recover, mode)
    sl = slice(taps - 1, datano - taps)
    _, ber1 = core.biterr(rx_dec1[sl], decimal1[sl])
    _, ber2 = core.biterr(rx_dec2[sl], decimal2[sl])
    ber_avg = float(np.mean([ber1, ber2]))

    if mode == core.MODULATION_SUPERPOSED:
        params6 = core.get_modulation_params(mode)
        dec6_1 = core.pam_demodulate(np.real(recover), params6["levels"])
        dec6_2 = core.pam_demodulate(np.imag(recover), params6["levels"])
        _, ber6_1 = core.biterr(dec6_1[sl], (v1 / 2.0 + 2.5)[sl])
        _, ber6_2 = core.biterr(dec6_2[sl], (v2 / 2.0 + 2.5)[sl])
        ctx.log(f"PAM6 层 BER: 带1={ber6_1:.4e}, 带2={ber6_2:.4e}")
        ctx.log(f"PAM4 层 BER: 带1={ber1:.4e}, 带2={ber2:.4e}")
    else:
        ber6_1, ber6_2 = ber1, ber2
        ctx.log(f"I/Q 支路 BER: 带1={ber1:.4e}, 带2={ber2:.4e}")
    ctx.log(f"平均 BER = {ber_avg:.4e}")
    return {"ber_pam6_1": float(ber6_1), "ber_pam6_2": float(ber6_2),
            "ber_band1": float(ber1), "ber_band2": float(ber2),
            "ber_avg": ber_avg}


# ======================================================================
# 分析
# ======================================================================
def _fn_full_experiment(inputs, params, ctx):
    """运行一次完整收发实验（等价于 python main.py 的参数集），返回记录字典。"""
    import main as main_flow
    record = main_flow.run_experiment(
        datano=int(params.get("datano", config.DATANO)),
        seed=int(params.get("seed", config.SEED_BAND1)),
        snr_db=float(params.get("snr_db", config.SNR_DB)),
        lms_taps=int(params.get("lms_taps", config.LMS_TAPS)),
        lms_mu1=float(params.get("lms_mu1", config.LMS_MU1)),
        lms_mu2=float(params.get("lms_mu2", config.LMS_MU2)),
        numof_ts=int(params.get("numof_ts", config.NUMOF_TS)),
        data_source=str(params.get("data_source", "virtual")),
        rx_file=str(params.get("rx_file", "")),
        osc_addr=str(params.get("osc_addr", config.OSC_VISA_ADDR)),
        modulation_mode=str(params.get("modulation_mode", config.MODULATION_MODE)),
        log=ctx.log,
    )
    return {"record": record, "ber_avg": record["ber_avg"]}


# ======================================================================
# 画图
# ======================================================================
def _plot_impl(kind, inputs, params, ctx):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.font_manager as fm
    import matplotlib.pyplot as plt

    for _name in ["Microsoft YaHei", "SimHei", "SimSun", "STSong",
                  "WenQuanYi Micro Hei", "Noto Sans CJK SC"]:
        try:
            fm.findfont(_name, fallback_to_default=False)
            matplotlib.rcParams["font.sans-serif"] = [_name, "DejaVu Sans"]
            break
        except Exception:
            continue
    matplotlib.rcParams["axes.unicode_minus"] = False

    title = str(params.get("title", kind))
    show = _plot_show(params)
    out = _out_path(ctx, f"{params.get('filename', kind)}.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(8, 4))
    if kind == "plot_time":
        sig = np.asarray(inputs["waveform"]).ravel()
        n = min(len(sig), int(params.get("max_points", 4000)))
        ax.plot(np.arange(n), sig[:n], lw=0.6)
        ax.set_xlabel("sample")
        ax.set_ylabel("amplitude")
    elif kind == "plot_spectrum":
        sig = np.asarray(inputs["waveform"]).ravel()
        fs = float(params.get("fs", config.AWG_SAMPLE * 1e6))
        spec = db_spectrum(sig)
        freqs = np.linspace(-fs / 2, fs / 2, len(spec))
        ax.plot(freqs / 1e6, spec, lw=0.6)
        ax.set_xlabel("frequency (MHz)")
        ax.set_ylabel("magnitude (dB)")
    elif kind == "plot_constellation":
        iq = np.asarray(inputs["symbols"]).ravel()
        n = min(len(iq), int(params.get("max_points", 8000)))
        ax.scatter(np.real(iq[:n]), np.imag(iq[:n]), s=2, alpha=0.4)
        ax.set_xlabel("I")
        ax.set_ylabel("Q")
        ax.axis("equal")
    else:
        plt.close(fig)
        raise ValueError(f"未知画图类型: {kind}")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=int(params.get("dpi", 150)))
    if show:
        plt.show()
    plt.close(fig)
    ctx.log(f"图像已保存: {out}")
    return {"path": str(out)}


def _make_plot_func(kind: str):
    def _func(inputs, params, ctx):
        return _plot_impl(kind, inputs, params, ctx)
    return _func


# ======================================================================
# 变量存取
# ======================================================================
def _fn_save_var(inputs, params, ctx):
    """把变量保存到文件（txt / npy）。"""
    value = inputs["value"]
    filename = str(params.get("filename", "variable.npy"))
    fmt = params.get("format", "auto")
    if fmt == "auto":
        fmt = Path(filename).suffix.lstrip(".").lower() or "npy"
    out = _out_path(ctx, filename)
    out.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "txt":
        arr = np.atleast_1d(np.asarray(value))
        if np.iscomplexobj(arr):
            flat = arr.ravel()
            np.savetxt(out, np.column_stack([flat.real, flat.imag]), fmt="%.6f")
        else:
            save_txt(out, arr)
    elif fmt == "npy":
        np.save(out, value)
    else:
        raise ValueError(f"不支持的格式: {fmt}")
    ctx.log(f"变量已保存 -> {out}")
    return {"path": str(out)}


def _fn_print_var(inputs, params, ctx):
    """查看变量内容：弹窗显示形状/类型/统计信息（不刷运行日志）。"""
    value = inputs["value"]
    name = str(params.get("name", "value"))
    if isinstance(value, np.ndarray):
        finite = np.asarray(value)[np.isfinite(np.real(value))] if value.size else value
        stats = ""
        if np.asarray(value).size and np.issubdtype(value.dtype, np.number):
            stats = (f"\nmin={np.min(finite):.4g}, max={np.max(finite):.4g}, "
                     f"mean={np.mean(finite):.4g}")
        text = f"{name}: ndarray shape={value.shape} dtype={value.dtype}{stats}"
    elif isinstance(value, dict):
        text = f"{name}: dict keys={list(value.keys())}"
    else:
        text = f"{name}: {type(value).__name__} = {value!r}"
    ctx.popup(f"打印变量: {name}", text)
    return {"value_out": value}


def _fn_note(inputs, params, ctx):
    return {}


# ======================================================================
# 节点注册
# ======================================================================
def _register_all():
    # ---- 数据源 ----
    register(NodeDef(
        "pam4_source", "PAM4 序列源", "数据源",
        "生成两路随机 PAM4 十进制序列（rng(seed)/rng(seed+100)）",
        [], [PortDef("dec1", "PAM4-1", "pam4"), PortDef("dec2", "PAM4-2", "pam4")],
        [ParamDef("datano", "符号数", "int", config.DATANO),
         ParamDef("seed", "种子", "int", config.SEED_BAND1)],
        _fn_pam4_source))

    register(NodeDef(
        "symbol_source", "通用符号源", "数据源",
        "生成 superposed/4QAM/16QAM/64QAM/36QAM_NLTCP 的 I/Q 符号（同时输出索引与电平）",
        [], [PortDef("dec1", "I 索引", "pam"),
             PortDef("dec2", "Q 索引", "pam"),
             PortDef("v1", "I 电平", "pam"),
             PortDef("v2", "Q 电平", "pam")],
        [ParamDef("datano", "符号数", "int", config.DATANO),
         ParamDef("seed", "种子", "int", config.SEED_BAND1),
         ParamDef("modulation", "调制模式", "choice", config.MODULATION_MODE,
                  core.SUPPORTED_MODULATIONS),
         ParamDef("shaping_lambda", "整形系数 λ", "float", config.NLTCP_SHAPING_FACTOR)],
        _fn_symbol_source))

    register(NodeDef(
        "load_rx_file", "读取接收文件", "数据源",
        "读取接收波形 txt；路径留空取 rxdata 下最新的 rx_*.txt",
        [], [PortDef("waveform", "波形", "waveform")],
        [ParamDef("path", "文件路径（空=最新）", "str", "")],
        _fn_load_rx_file))

    register(NodeDef(
        "const", "常量", "变量存取",
        "把文本解析为 Python 数值/数组（可用 np）",
        [], [PortDef("value", "值", "any")],
        [ParamDef("value", "表达式", "str", "0")],
        _fn_const))

    register(NodeDef(
        "load_var", "读取变量", "变量存取",
        "从文件读取变量（txt / npy）",
        [], [PortDef("value", "值", "any")],
        [ParamDef("path", "文件路径", "str", ""),
         ParamDef("format", "格式", "choice", "auto",
                  ["auto", "txt", "npy"])],
        _fn_load_var))

    # ---- 发射 ----
    register(NodeDef(
        "pam4_to_pam6", "PAM6 差分映射", "发射",
        "PAM4 -> PAM6 差分映射，缩放到 {-5,-3,-1,1,3,5}",
        [PortDef("dec1", "PAM4-1", "pam4"), PortDef("dec2", "PAM4-2", "pam4")],
        [PortDef("v1", "PAM6-1", "pam6"), PortDef("v2", "PAM6-2", "pam6")],
        [], _fn_pam4_to_pam6))

    register(NodeDef(
        "tx_generate", "发射成形", "发射",
        "上采样 x3 -> SRRC 成形 -> cos/sin 子载波上变频 -> 功率归一化",
        [PortDef("v1", "PAM6-1", "pam6"), PortDef("v2", "PAM6-2", "pam6")],
        [PortDef("data1", "I 路波形", "waveform"),
         PortDef("data2", "Q 路波形", "waveform"),
         PortDef("tx_sum", "叠加波形", "waveform"),
         PortDef("aux", "接收辅助", "dict", False)],
        [], _fn_tx_generate))

    # ---- 信道 ----
    register(NodeDef(
        "virtual_channel", "虚拟信道", "信道",
        "指数衰减频率响应 + AWGN（可选弱 LED 非线性）",
        [PortDef("waveform_in", "输入波形", "waveform")],
        [PortDef("waveform", "输出波形", "waveform")],
        [ParamDef("snr_db", "SNR (dB)", "float", config.SNR_DB),
         ParamDef("fs_hz", "带宽参数 fs", "float", config.CHANNEL_FS),
         ParamDef("factor", "衰减因子", "float", config.CHANNEL_FACTOR),
         ParamDef("nonlinear", "LED 非线性", "bool", config.CHANNEL_NONLINEAR)],
        _fn_virtual_channel))

    # ---- 硬件 ----
    register(NodeDef(
        "awg_download_dual", "AWG 双通道下载", "硬件",
        "两路波形分别下载到 AWG520 CH1 / CH2 并同步播放",
        [PortDef("wave1", "CH1 波形", "waveform"),
         PortDef("wave2", "CH2 波形", "waveform")],
        [],
        [ParamDef("visa_addr", "VISA 地址", "str", config.AWG_VISA_ADDR),
         ParamDef("vpp", "幅度 Vpp", "float", config.AWG_VPP)],
        _fn_awg_download_dual))

    register(NodeDef(
        "scope_capture", "示波器采集", "硬件",
        "Keysight 示波器采集波形并重采样到 AWG 速率",
        [], [PortDef("waveform", "波形", "waveform")],
        [ParamDef("visa_addr", "VISA 地址", "str", config.OSC_VISA_ADDR),
         ParamDef("channel", "通道", "choice", config.OSC_CHANNEL,
                  ["CHAN1", "CHAN2", "CHAN3", "CHAN4"]),
         ParamDef("sample_rate", "采样率 (Hz)", "float", config.OSC_SAMPLE_RATE),
         ParamDef("timebase_scale", "时基 (s)", "float", config.OSC_TIMEBASE_SCALE)],
        _fn_scope_capture))

    # ---- 接收 ----
    register(NodeDef(
        "sync_xcorr", "互相关同步", "接收",
        "xcorr 同步，截取与 TX 等长的 RX 片段",
        [PortDef("rx", "接收波形", "waveform"),
         PortDef("tx_ref", "TX 参考", "waveform")],
        [PortDef("datarx", "同步后波形", "waveform"),
         PortDef("offset", "偏移", "scalar", False)],
        [], _fn_sync_xcorr))

    register(NodeDef(
        "downconvert", "下变频抽取", "接收",
        "正交下变频 + SRRC 匹配滤波 + 按 3 抽取，输出复数符号流",
        [PortDef("datarx", "同步后波形", "waveform"),
         PortDef("aux", "接收辅助", "dict", False)],
        [PortDef("symbols", "复数符号流", "symbols")],
        [ParamDef("upsampleno", "上采样倍数", "int", config.UPSAMPLENO),
         ParamDef("offset", "抽取偏移", "int", 0)],
        _fn_downconvert))

    register(NodeDef(
        "mimo_lms", "MIMO LMS 均衡", "接收",
        "2x2 MIMO LMS 均衡分离 I/Q 两路，输出复数恢复符号流",
        [PortDef("symbols", "复数符号流", "symbols"),
         PortDef("v1", "PAM6-1 参考", "pam6"),
         PortDef("v2", "PAM6-2 参考", "pam6")],
        [PortDef("recover", "恢复符号流", "symbols")],
        [ParamDef("taps", "抽头数", "int", config.LMS_TAPS),
         ParamDef("mu1", "步长 μ1", "float", config.LMS_MU1),
         ParamDef("mu2", "步长 μ2", "float", config.LMS_MU2),
         ParamDef("numof_ts", "训练符号数", "int", config.NUMOF_TS)],
        _fn_mimo_lms))

    # ---- 解调 ----
    register(NodeDef(
        "pam6_decide", "PAM6 判决", "解调",
        "复数恢复符号流 -> 两路 0..5 最近邻判决",
        [PortDef("recover", "恢复符号流", "symbols")],
        [PortDef("dec1", "判决-1", "pam6"), PortDef("dec2", "判决-2", "pam6")],
        [], _fn_pam6_decide))

    register(NodeDef(
        "symbol_decide", "通用判决", "解调",
        "按调制模式对恢复符号流判决（superposed 会解码回 PAM4）",
        [PortDef("recover", "恢复符号流", "symbols")],
        [PortDef("dec1", "判决-1", "pam"), PortDef("dec2", "判决-2", "pam")],
        [ParamDef("modulation", "调制模式", "choice", config.MODULATION_MODE,
                  core.SUPPORTED_MODULATIONS)],
        _fn_symbol_decide))

    register(NodeDef(
        "pam4_decode", "PAM4 解码", "解调",
        "PAM6 差分解码回 PAM4（0..3）",
        [PortDef("dec6_1", "PAM6-1", "pam6"), PortDef("dec6_2", "PAM6-2", "pam6")],
        [PortDef("dec1", "PAM4-1", "pam4"), PortDef("dec2", "PAM4-2", "pam4")],
        [], _fn_pam4_decode))

    register(NodeDef(
        "ber", "误码统计", "解调",
        "对比发送/接收符号，输出 BER（自然二进制逐比特）",
        [PortDef("rx_dec", "接收符号", "any"),
         PortDef("tx_dec", "发送符号", "any")],
        [PortDef("ber", "BER", "scalar"),
         PortDef("errors", "错误比特数", "scalar", False),
         PortDef("count", "符号数", "scalar", False)],
        [], _fn_ber))

    register(NodeDef(
        "ber_report", "BER 汇总报告", "解调",
        "两路 BER 汇总（跳过 LMS 抽头边缘，与 main.py 一致）",
        [PortDef("recover", "恢复符号流", "symbols"),
         PortDef("dec1_tx", "发送-1", "pam"),
         PortDef("dec2_tx", "发送-2", "pam"),
         PortDef("v1", "I 电平参考", "pam"),
         PortDef("v2", "Q 电平参考", "pam")],
        [PortDef("ber_avg", "平均 BER", "scalar"),
         PortDef("ber_band1", "带1 BER", "scalar", False),
         PortDef("ber_band2", "带2 BER", "scalar", False),
         PortDef("ber_pam6_1", "PAM6-1 BER", "scalar", False),
         PortDef("ber_pam6_2", "PAM6-2 BER", "scalar", False)],
        [ParamDef("taps", "LMS 抽头数", "int", config.LMS_TAPS),
         ParamDef("modulation", "调制模式", "choice", config.MODULATION_MODE,
                  core.SUPPORTED_MODULATIONS)],
        _fn_ber_report))

    # ---- 分析 ----
    register(NodeDef(
        "full_experiment", "完整实验", "分析",
        "等价于 python main.py：一次跑完收发链路并返回记录字典",
        [],
        [PortDef("record", "实验记录", "dict"),
         PortDef("ber_avg", "平均 BER", "scalar")],
        [ParamDef("data_source", "数据源", "choice", "virtual",
                  ["virtual", "file", "scope"]),
         ParamDef("datano", "符号数", "int", config.DATANO),
         ParamDef("seed", "种子", "int", config.SEED_BAND1),
         ParamDef("snr_db", "SNR (dB)", "float", config.SNR_DB),
         ParamDef("lms_taps", "LMS 抽头数", "int", config.LMS_TAPS),
         ParamDef("lms_mu1", "LMS μ1", "float", config.LMS_MU1),
         ParamDef("lms_mu2", "LMS μ2", "float", config.LMS_MU2),
         ParamDef("numof_ts", "训练符号数", "int", config.NUMOF_TS),
         ParamDef("modulation_mode", "调制模式", "choice", config.MODULATION_MODE,
                  core.SUPPORTED_MODULATIONS),
         ParamDef("rx_file", "接收文件", "str", ""),
         ParamDef("osc_addr", "示波器地址", "str", config.OSC_VISA_ADDR)],
        _fn_full_experiment))

    # ---- 画图 ----
    for kind, label, desc, ins in [
        ("plot_time", "画时域波形", "绘制波形前 N 个点",
         [PortDef("waveform", "波形", "waveform")]),
        ("plot_spectrum", "画频谱", "绘制双边幅度谱（dB）",
         [PortDef("waveform", "波形", "waveform")]),
        ("plot_constellation", "画星座图", "绘制复数符号星座",
         [PortDef("symbols", "复数符号", "symbols")]),
    ]:
        params = [ParamDef("title", "标题", "str", label),
                  ParamDef("filename", "文件名", "str", kind),
                  ParamDef("max_points", "最大点数", "int", 4000),
                  ParamDef("show", "弹出显示", "bool", False)]
        if kind == "plot_spectrum":
            params.insert(2, ParamDef("fs", "采样率 (Hz)", "float",
                                      config.AWG_SAMPLE * 1e6))
        register(NodeDef(kind, label, "画图", desc, ins,
                         [PortDef("path", "PNG 路径", "any", False)],
                         params, _make_plot_func(kind)))

    # ---- 变量存取 / 标注 ----
    register(NodeDef(
        "save_var", "保存变量", "变量存取",
        "把变量保存到文件（txt / npy）",
        [PortDef("value", "值", "any")],
        [PortDef("path", "文件路径", "any", False)],
        [ParamDef("filename", "文件名", "str", "variable.npy"),
         ParamDef("format", "格式", "choice", "auto", ["auto", "txt", "npy"])],
        _fn_save_var))

    register(NodeDef(
        "print_var", "打印变量", "变量存取",
        "弹窗查看变量形状/类型/统计信息",
        [PortDef("value", "值", "any")],
        [PortDef("value_out", "值(直通)", "any", False)],
        [ParamDef("name", "变量名", "str", "value")],
        _fn_print_var))

    register(NodeDef(
        "note", "注释", "标注", "备注说明（不参与计算）", [], [],
        [ParamDef("text", "内容", "str", "备注...")], _fn_note))


_register_all()
