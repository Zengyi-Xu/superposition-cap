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
    ba = np.unpackbits(a.astype(np.uint64)[:, None].view(np.uint8), axis=1)[:, -width:]
    bb = np.unpackbits(b.astype(np.uint64)[:, None].view(np.uint8), axis=1)[:, -width:]
    err = int(np.count_nonzero(ba != bb))
    return err, err / (n * width) if n else 0.0
