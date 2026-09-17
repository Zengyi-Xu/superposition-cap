"""功率域叠加 PAM（Superposed 16QAM）调制核心。

移植自 MATLAB 代码（SuperposedPS16QAM02 文件夹）：
  - A1_TX_MIMOPAM4toPAM_0723.m    两路 PAM4 -> 差分映射为两路 PAM6 -> 正交叠加为类 16QAM
  - A2_RX_MIMOPAM4toPAM_0826.m    下变频 + 匹配滤波 + 抽取 + 2x2 MIMO LMS + PAM6 判决 + 差分解码
  - PAM6_demodulation.m           PAM6 最近邻判决
  - makewfm.m                     AWG 波形文件（见 utils.write_wfm）

发射信号：两路独立的 PAM4 比特流分别经"类部分响应"映射得到 PAM6 电平
（v(n) = dec(n) + floor(v(n-1)/2)），再分别用 cos/sin 子载波上变频；
单路接收（MISO）时两路波形直接叠加，等效为一路 16QAM 信号。
"""
from typing import Tuple

import numpy as np
import scipy.signal as sg

import config


# -----------------------------------------------------------------------------
# 发射机
# -----------------------------------------------------------------------------
def generate_pam4_streams(datano: int,
                          seed1: int = config.SEED_BAND1,
                          seed2: int = config.SEED_BAND2
                          ) -> Tuple[np.ndarray, np.ndarray]:
    """生成两路 [0, PAM_ORDER) 的随机 PAM4 十进制序列（对应 rng(100)/rng(200) + randi）。"""
    np.random.seed(seed1)
    decimal_pam4_1 = np.random.randint(0, config.PAM_ORDER, size=datano)
    np.random.seed(seed2)
    decimal_pam4_2 = np.random.randint(0, config.PAM_ORDER, size=datano)
    return decimal_pam4_1, decimal_pam4_2


def pam4_to_pam6(decimal_pam4: np.ndarray) -> np.ndarray:
    """PAM4 -> PAM6 差分映射，并缩放到 {-5, -3, -1, 1, 3, 5}（A1_TX 中的 v1/v2）。

    v(1) = 0；v(n) = dec(n) + floor(v(n-1)/2)，n = 2 .. N-1；v(N) = 0（与 MATLAB 一致）。
    """
    dec = np.asarray(decimal_pam4, dtype=int).ravel()
    n = len(dec)
    v = np.zeros(n, dtype=float)
    for i in range(1, n - 1):
        v[i] = dec[i] + np.floor(v[i - 1] / 2.0)
    return (v - 2.5) * 2.0


# -----------------------------------------------------------------------------
# 调制模式支持（普通 QAM / NLTCP-QAM / 叠加 PAM）
# -----------------------------------------------------------------------------
MODULATION_36QAM = "36QAM"
MODULATION_QAM4 = "4QAM"
MODULATION_QAM16 = "16QAM"
MODULATION_QAM32 = "32QAM"
MODULATION_QAM64 = "64QAM"
MODULATION_NLTCP36 = "36QAM_NLTCP"
SUPPORTED_MODULATIONS = [
    MODULATION_36QAM, MODULATION_QAM4,
    MODULATION_QAM16, MODULATION_QAM32, MODULATION_QAM64,
]


def _bits_for_order(order: int) -> int:
    """返回能编码 order 个电平所需的最少比特数（order 为 2 的幂时即 log2）。"""
    return int(np.ceil(np.log2(order)))


def _pam_levels(order: int) -> np.ndarray:
    """返回对称奇数 PAM 电平：order=2 -> [-1,1]；order=4 -> [-3,-1,1,3]；以此类推。"""
    return np.arange(order, dtype=float) * 2.0 - (order - 1.0)


def get_modulation_params(mode: str) -> dict:
    """返回指定调制模式的参数字典。"""
    if mode == MODULATION_36QAM:
        return {
            "mode": mode,
            "is_superposed": True,
            "order_per_dim": 6,
            "levels": _pam_levels(6),
            "bits_per_dim": 2,
            "shaped": False,
        }
    if mode == MODULATION_QAM4:
        return {
            "mode": mode, "is_superposed": False,
            "order_per_dim": 2, "levels": _pam_levels(2),
            "bits_per_dim": 1, "shaped": False,
        }
    if mode == MODULATION_QAM16:
        return {
            "mode": mode, "is_superposed": False,
            "order_per_dim": 4, "levels": _pam_levels(4),
            "bits_per_dim": 2, "shaped": False,
        }
    if mode == MODULATION_QAM32:
        # 32QAM：I 路 4 电平(2bit)，Q 路 8 电平(3bit)，共 32 点、5 bit/符号
        return {
            "mode": mode, "is_superposed": False,
            "order_i": 4, "order_q": 8,
            "levels_i": _pam_levels(4), "levels_q": _pam_levels(8),
            "bits_i": 2, "bits_q": 3,
            "shaped": False,
        }
    if mode == MODULATION_QAM64:
        return {
            "mode": mode, "is_superposed": False,
            "order_per_dim": 8, "levels": _pam_levels(8),
            "bits_per_dim": 3, "shaped": False,
        }
    if mode == MODULATION_NLTCP36:
        # 保留对旧记录/旧流程的兼容，但不再在 GUI 下拉中提供
        return {
            "mode": mode, "is_superposed": False,
            "order_per_dim": 6, "levels": _pam_levels(6),
            "bits_per_dim": 3, "shaped": True,
        }
    raise ValueError(f"不支持的调制模式: {mode!r}，可选: {SUPPORTED_MODULATIONS}")


def generate_pam_streams_uniform(datano: int,
                                 order: int,
                                 seed1: int = config.SEED_BAND1,
                                 seed2: int = config.SEED_BAND2
                                 ) -> Tuple[np.ndarray, np.ndarray]:
    """生成两路 [0, order) 的均匀随机 PAM 十进制索引序列。"""
    np.random.seed(seed1)
    d1 = np.random.randint(0, order, size=datano)
    np.random.seed(seed2)
    d2 = np.random.randint(0, order, size=datano)
    return d1, d2


def _mb_probs(levels: np.ndarray, lambda_: float) -> np.ndarray:
    """Maxwell-Boltzmann 概率分布：P(x) ∝ exp(-λ·x²)。"""
    levels = np.asarray(levels, dtype=float)
    probs = np.exp(-lambda_ * levels ** 2)
    return probs / np.sum(probs)


def generate_pam_streams_shaped(datano: int,
                                order: int,
                                seed1: int = config.SEED_BAND1,
                                seed2: int = config.SEED_BAND2,
                                lambda_: float = config.NLTCP_SHAPING_FACTOR
                                ) -> Tuple[np.ndarray, np.ndarray]:
    """生成两路概率整形 PAM 十进制索引序列（内圈点概率更高）。"""
    probs = _mb_probs(_pam_levels(order), lambda_)
    np.random.seed(seed1)
    d1 = np.random.choice(order, size=datano, p=probs)
    np.random.seed(seed2)
    d2 = np.random.choice(order, size=datano, p=probs)
    return d1, d2


def pam_indices_to_levels(indices: np.ndarray, order: int) -> np.ndarray:
    """把 PAM 十进制索引映射为实际电平值。"""
    return _pam_levels(order)[np.asarray(indices, dtype=int)]


def generate_symbols(mode: str,
                     datano: int,
                     seed1: int = config.SEED_BAND1,
                     seed2: int = config.SEED_BAND2,
                     shaping_lambda: float = config.NLTCP_SHAPING_FACTOR,
                     upsampleno: int = config.UPSAMPLENO,
                     ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """按指定调制模式生成发射符号。

    Returns
    -------
    v1, v2 : np.ndarray
        I/Q 两路实际电平值（直接输入 generate_tx）。
    tx_dec1, tx_dec2 : np.ndarray
        用于 BER 比对的十进制索引：superposed 模式下为 PAM4 索引，
        其它模式下为每维 PAM 索引（0..order_per_dim-1）。
    """
    if mode == MODULATION_36QAM:
        decimal1, decimal2 = generate_pam4_streams(datano, seed1, seed2)
        v1 = pam4_to_pam6(decimal1)
        v2 = pam4_to_pam6(decimal2)
        return v1, v2, decimal1, decimal2

    params = get_modulation_params(mode)
    if "order_i" in params and "order_q" in params:
        # 非对称 I/Q 阶数（如 32QAM: 4×8）
        decimal1, _ = generate_pam_streams_uniform(datano, params["order_i"], seed1, seed2)
        _, decimal2 = generate_pam_streams_uniform(datano, params["order_q"], seed1, seed2)
        v1 = pam_indices_to_levels(decimal1, params["order_i"])
        v2 = pam_indices_to_levels(decimal2, params["order_q"])
        return v1, v2, decimal1, decimal2

    order = int(params["order_per_dim"])
    if params["shaped"]:
        decimal1, decimal2 = generate_pam_streams_shaped(
            datano, order, seed1, seed2, shaping_lambda)
    else:
        decimal1, decimal2 = generate_pam_streams_uniform(
            datano, order, seed1, seed2)
    v1 = pam_indices_to_levels(decimal1, order)
    v2 = pam_indices_to_levels(decimal2, order)
    return v1, v2, decimal1, decimal2


def generate_symbols_for_tx(mode: str,
                            datano: int,
                            seed1: int = config.SEED_BAND1,
                            seed2: int = config.SEED_BAND2,
                            shaping_lambda: float = config.NLTCP_SHAPING_FACTOR,
                            upsampleno: int = config.UPSAMPLENO,
                            ) -> Tuple[Dict, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """生成符号并直接产生发射波形（供 main.py / optimizer.py 使用）。

    Returns
    -------
    tx : dict
        generate_tx 返回的波形字典（含 cos1/sin1/filter_cos 等）。
    v1, v2, decimal1, decimal2 : 与 generate_symbols 一致。
    """
    v1, v2, decimal1, decimal2 = generate_symbols(
        mode, datano, seed1, seed2, shaping_lambda, upsampleno)
    tx = generate_tx(v1, v2, upsampleno=upsampleno)
    return tx, v1, v2, decimal1, decimal2


def pam_demodulate(rxdata: np.ndarray, order_or_levels) -> np.ndarray:
    """PAM 最近邻判决，返回索引 0..order-1。

    Parameters
    ----------
    order_or_levels : int 或 array-like
        整数时表示 PAM 阶数；数组时直接使用给定电平集合。
    """
    x = np.asarray(rxdata, dtype=float).ravel()
    if isinstance(order_or_levels, int):
        cons = _pam_levels(order_or_levels)
    else:
        cons = np.asarray(order_or_levels, dtype=float)
    idx = np.argmin(np.abs(x[:, None] - cons[None, :]), axis=1)
    return idx


def demodulate_symbols(recoverdata: np.ndarray,
                       mode: str = MODULATION_36QAM
                       ) -> Tuple[np.ndarray, np.ndarray]:
    """按调制模式对接收回的复数符号流进行判决。

    Returns
    -------
    rx_dec1, rx_dec2 : 用于 BER 比对的十进制索引。
    """
    rx1 = np.real(np.asarray(recoverdata))
    rx2 = np.imag(np.asarray(recoverdata))
    params = get_modulation_params(mode)
    if mode == MODULATION_36QAM:
        return pam6_to_pam4(dec1), pam6_to_pam4(dec2)
    if "levels_i" in params and "levels_q" in params:
        dec1 = pam_demodulate(rx1, params["levels_i"])
        dec2 = pam_demodulate(rx2, params["levels_q"])
        return dec1, dec2
    dec1 = pam_demodulate(rx1, params["levels"])
    dec2 = pam_demodulate(rx2, params["levels"])
    return dec1, dec2


def srrc_filter(rolloff: float, span: int, sps: int) -> np.ndarray:
    """平方根升余弦（SRRC）滤波器，等价于 MATLAB rcosdesign(rolloff, span, sps, 'sqrt')。"""
    n_taps = span * sps + 1
    t = (np.arange(n_taps) - n_taps // 2).astype(float)
    h = np.zeros(n_taps, dtype=float)
    for i, ti in enumerate(t):
        ti_norm = ti / sps
        if np.isclose(ti_norm, 0.0):
            h[i] = 1.0 - rolloff + 4 * rolloff / np.pi
        elif np.isclose(np.abs(4 * rolloff * ti_norm), 1.0):
            h[i] = (rolloff / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * rolloff))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * rolloff))
            )
        else:
            num = np.sin(np.pi * ti_norm * (1 - rolloff)) + \
                4 * rolloff * ti_norm * np.cos(np.pi * ti_norm * (1 + rolloff))
            den = np.pi * ti_norm * (1 - (4 * rolloff * ti_norm) ** 2)
            h[i] = num / den
    return h / np.sqrt(np.sum(h ** 2))


def subcarrier_waves(num_samples: int,
                     upsampleno: int = config.UPSAMPLENO,
                     subcar: float = config.SUBCAR1
                     ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """生成与 A1_TX 一致的子载波：t1 = 0 : 2π/sps : (N-1)·2π/sps。"""
    t1 = np.arange(num_samples) * (2 * np.pi / upsampleno)
    return t1, np.cos(subcar * t1), np.sin(subcar * t1)


def generate_tx(v1: np.ndarray,
                v2: np.ndarray,
                rolloff: float = config.ROLLOFF,
                filter_span: int = config.FILTER_SPAN,
                filter_sps: int = config.FILTER_SPS,
                upsampleno: int = config.UPSAMPLENO
                ) -> dict:
    """发射机：上采样 -> SRRC 成形 -> cos/sin 子载波上变频 -> 功率归一化。

    Returns
    -------
    dict，包含 data1（I 路）、data2（Q 路）、tx_sum（两路叠加，用作同步参考）、
    cos1/sin1/filter_cos/t1 等接收机所需波形。
    """
    v1 = np.asarray(v1, dtype=float).ravel()
    v2 = np.asarray(v2, dtype=float).ravel()
    datano = len(v1)

    t1, cos1, sin1 = subcarrier_waves(datano * upsampleno, upsampleno)
    filter_cos = srrc_filter(rolloff, filter_span, filter_sps)

    def _one(v: np.ndarray, carrier: np.ndarray) -> np.ndarray:
        up = np.zeros(len(v) * upsampleno)
        up[::upsampleno] = v
        shaped = sg.convolve(up, filter_cos, mode="same")
        data = np.real(shaped) * carrier
        return data / np.sqrt(np.mean(np.abs(data) ** 2))

    data1 = _one(v1, cos1)
    data2 = _one(v2, sin1)

    return {
        "data1": data1,
        "data2": data2,
        "tx_sum": data1 + data2,
        "cos1": cos1,
        "sin1": sin1,
        "filter_cos": filter_cos,
        "t1": t1,
    }


# -----------------------------------------------------------------------------
# 接收机
# -----------------------------------------------------------------------------
def downconvert_to_symbols(datarx: np.ndarray,
                           cos1: np.ndarray,
                           sin1: np.ndarray,
                           filter_cos: np.ndarray,
                           upsampleno: int = config.UPSAMPLENO,
                           offset: int = 0) -> np.ndarray:
    """正交下变频 + SRRC 匹配滤波 + 按 upsampleno 抽取（A2_RX 中的 MISO Rx 段）。

    返回长度为 len(datarx)/upsampleno 的复数符号流，并已按平均功率归一化。
    """
    data_rx = np.asarray(datarx, dtype=float).ravel() * cos1 + \
        1j * np.asarray(datarx, dtype=float).ravel() * sin1
    data_recover1 = sg.convolve(data_rx, filter_cos, mode="same")
    data_recover = data_recover1[offset::upsampleno]
    return data_recover / np.sqrt(np.mean(np.abs(data_recover) ** 2))


def pam6_demodulate(rxdata: np.ndarray) -> np.ndarray:
    """PAM6 最近邻判决（PAM6_demodulation.m）：判决到电平 0..5。"""
    x = np.asarray(rxdata, dtype=float).ravel()
    cons = np.arange(6, dtype=float)
    idx = np.argmin(np.abs(x[:, None] - cons[None, :]), axis=1)
    return cons[idx]


def pam6_to_pam4(dec_pam6: np.ndarray) -> np.ndarray:
    """PAM6 差分解码回 PAM4（A2_RX 的 decoding 段）。

    out(1) = 0；out(n) = |dec(n) - floor(dec(n-1)/2)|，n = 2 .. N。
    """
    dec = np.asarray(dec_pam6, dtype=float).ravel()
    out = np.zeros(len(dec))
    for n in range(1, len(dec)):
        out[n] = np.abs(dec[n] - np.floor(dec[n - 1] / 2.0))
    return out


def biterr(a: np.ndarray, b: np.ndarray) -> Tuple[int, float]:
    """MATLAB biterr 的等价实现：按自然二进制逐比特比较。

    位宽取 max(a,b) 所需的最小比特数（PAM4 判决值为 0..3 -> 2 比特）。
    返回 (错误比特数, 误码率)。
    """
    a = np.asarray(a).astype(np.int64).ravel()
    b = np.asarray(b).astype(np.int64).ravel()
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    width = int(np.floor(np.log2(max(np.max(a), np.max(b))))) + 1 if n else 1
    width = max(width, 1)
    shifts = np.arange(width - 1, -1, -1)
    ba = (a[:, None] >> shifts) & 1
    bb = (b[:, None] >> shifts) & 1
    err = int(np.count_nonzero(ba != bb))
    return err, err / (n * width) if n else 0.0
