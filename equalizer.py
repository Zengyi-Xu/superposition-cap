"""LMS 与 2x2 MIMO LMS 均衡器。

移植自 MATLAB 函数（SuperposedPS16QAM02 文件夹）：
  - LMS_1DownS_Testnan.m         符号速率单路 LMS 线性均衡器
  - MIMOLMS_1DownS_Testnan.m     2x2 MIMO LMS（分离同相/正交两路基带串扰）
"""
from typing import Tuple

import numpy as np


def lms_equalizer(rxdata: np.ndarray,
                  txdata: np.ndarray,
                  taps_lms: int,
                  mu_lms: float,
                  numof_ts: int,
                  ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """符号速率 LMS 线性均衡器（单路，复数可用）。

    Returns
    -------
    k : np.ndarray
        均衡输出，与 rxdata 等长（首尾用输入样本填充）。
    y : np.ndarray
        训练阶段输出。
    E : np.ndarray
        训练阶段误差。
    W : np.ndarray
        收敛后的抽头权重。
    """
    rxdata = np.asarray(rxdata).flatten()
    txdata = np.asarray(txdata).flatten()

    rxdata = rxdata / np.sqrt(np.mean(np.abs(rxdata) ** 2))
    txdata = txdata / np.sqrt(np.mean(np.abs(txdata) ** 2))

    x = rxdata[:numof_ts]
    d = txdata[:numof_ts]

    compensation = taps_lms
    half = (taps_lms - 1) // 2
    W = np.zeros(taps_lms, dtype=complex)

    ntr = len(x)
    y = np.zeros(ntr, dtype=complex)
    E = np.zeros(ntr, dtype=complex)

    nn = 0
    n = compensation - 1
    while n < ntr:
        idx = n - half
        X_lms = x[idx - half: idx + half + 1][::-1]
        y[nn] = np.dot(W, X_lms)
        e = d[idx] - y[nn]
        W = W + mu_lms * e * np.conj(X_lms)
        E[nn] = e
        n += 1
        nn += 1

    k = np.zeros(len(rxdata), dtype=complex)
    n = compensation - 1
    mm = 0
    while n < len(rxdata):
        idx = n - half
        X_lms = rxdata[idx - half: idx + half + 1][::-1]
        k[mm] = np.dot(W, X_lms)
        n += 1
        mm += 1

    head = (compensation - 1) // 2
    tail = len(rxdata) - mm
    if tail > 0:
        k = np.concatenate([rxdata[:head], k[:mm], rxdata[-tail:]])
    else:
        k = np.concatenate([rxdata[:head], k[:mm]])
    k = k / np.sqrt(np.mean(np.abs(k) ** 2))
    return k, y, E, W


def mimo_lms_equalizer(taps_lms: int,
                       mu_lms: float,
                       mu_lms2: float,
                       numof_ts: int,
                       rxdata1: np.ndarray,
                       rxdata2: np.ndarray,
                       txdata1: np.ndarray,
                       txdata2: np.ndarray,
                       ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """2x2 MIMO LMS 均衡器（MIMOLMS_1DownS_Testnan.m 的逐行移植）。

    用两路接收信号 rxdata1/rxdata2 共同估计 txdata1（输出 k）；
    交换收发对即可得到另一路估计。输入输出均为实数序列。

    Returns
    -------
    k : np.ndarray
        均衡输出，与 rxdata1 等长（首尾用输入样本填充并归一化）。
    Ik : np.ndarray
        训练阶段输出。
    E : np.ndarray
        训练阶段误差。
    W1, W2 : np.ndarray
        收敛后的两路抽头权重。
    """
    rxdata1 = np.asarray(rxdata1, dtype=float).flatten()
    rxdata2 = np.asarray(rxdata2, dtype=float).flatten()
    txdata1 = np.asarray(txdata1, dtype=float).flatten()
    txdata2 = np.asarray(txdata2, dtype=float).flatten()

    rxdata1 = rxdata1 / np.sqrt(np.mean(np.abs(rxdata1) ** 2))
    rxdata2 = rxdata2 / np.sqrt(np.mean(np.abs(rxdata2) ** 2))
    txdata1 = txdata1 / np.sqrt(np.mean(np.abs(txdata1) ** 2))
    txdata2 = txdata2 / np.sqrt(np.mean(np.abs(txdata2) ** 2))

    y1 = rxdata1[:numof_ts]
    y2 = rxdata2[:numof_ts]
    d1 = txdata1[:numof_ts]
    d2 = txdata2[:numof_ts]

    ntr = len(y1)
    compensation = taps_lms
    half = (taps_lms - 1) // 2
    head = (compensation - 1) // 2

    Ik = np.zeros(ntr)
    E = np.zeros(ntr)
    W1 = np.zeros(taps_lms)
    W2 = np.zeros(taps_lms)

    # 训练阶段（窗口为因果 11 抽头：y(c), y(c-1) .. y(c-2*half)，期望 d(c-half)，
    # 与 MATLAB MIMOLMS_1DownS_Testnan.m 的索引完全一致）
    nn = 0
    c = compensation - 1
    while c < ntr:
        u1 = y1[c - 2 * half: c + 1][::-1]
        u2 = y2[c - 2 * half: c + 1][::-1]
        Ik[nn] = np.dot(W1, u1) + np.dot(W2, u2)
        e = d1[c - half] - Ik[nn]
        W1 = W1 + mu_lms * e * u1
        W2 = W2 + mu_lms2 * e * u2
        E[nn] = e
        c += 1
        nn += 1

    # 应用阶段
    n_total = len(rxdata1)
    out_len = n_total - (compensation - 1)
    k = np.zeros(out_len)
    c = compensation - 1
    mm = 0
    while c < n_total:
        r1 = rxdata1[c - 2 * half: c + 1][::-1]
        r2 = rxdata2[c - 2 * half: c + 1][::-1]
        k[mm] = np.dot(W1, r1) + np.dot(W2, r2)
        c += 1
        mm += 1

    # 首尾补偿抽头损失（与 MATLAB 一致）
    k = np.concatenate([rxdata1[:head], k, rxdata1[n_total - head:]])
    k = k / np.sqrt(np.mean(np.abs(k) ** 2))
    return k, Ik, E, W1, W2
