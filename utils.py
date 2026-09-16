"""通用工具函数：文件读写、重采样、同步、AWG 波形文件生成等。"""
import struct
from pathlib import Path

import numpy as np
import scipy.signal as sg

import config


# -----------------------------------------------------------------------------
# 文件读写
# -----------------------------------------------------------------------------
def load_txt(path, dtype=float) -> np.ndarray:
    """将文本文件读取为 numpy 数组。"""
    if isinstance(path, str):
        path = Path(path)
    return np.loadtxt(path, dtype=dtype)


def save_txt(path, data: np.ndarray, fmt="%.6f") -> None:
    """将 numpy 数组保存到文本文件。"""
    if isinstance(path, str):
        path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savetxt(path, np.atleast_1d(data), fmt=fmt)


# -----------------------------------------------------------------------------
# 波形处理
# -----------------------------------------------------------------------------
def resample_ratio(data: np.ndarray, up: int, down: int) -> np.ndarray:
    """按 up/down 有理数因子重采样，等价于 MATLAB resample(data, p, q)。"""
    return sg.resample_poly(np.asarray(data, dtype=float), up, down)


def sync_by_xcorr(rx: np.ndarray, tx_ref: np.ndarray, start_index: int = 1):
    """与 MATLAB A2_RX 中基于 xcorr 的同步一致。

    在 rx 中定位 tx_ref 的起始位置，返回切片 rx[start : start + len(tx_ref)]。

    Parameters
    ----------
    rx : np.ndarray
        接收波形。
    tx_ref : np.ndarray
        同步参考（两路发射波形之和）。
    start_index : int
        MATLAB 中的 h（从匹配位置再向后偏移的样本数），默认为 1。

    Returns
    -------
    sliced : np.ndarray
        与发射等长的接收片段。
    offset : int
        匹配峰值对应的延迟（样本数）。
    corr : np.ndarray
        互相关序列（诊断用）。
    lags : np.ndarray
        互相关延迟轴。
    """
    rx = np.asarray(rx).ravel()
    tx_ref = np.asarray(tx_ref).ravel()
    corr = sg.correlate(np.real(rx), np.real(tx_ref), mode="full")
    lags = sg.correlation_lags(len(rx), len(tx_ref), mode="full")
    idx = int(np.argmax(np.abs(corr)))
    # 若匹配位置超出接收长度，则取次大峰值（与 MATLAB 一致）
    if lags[idx] + len(tx_ref) + start_index - 1 > len(rx):
        corr2 = corr.copy()
        corr2[idx] = 0
        idx = int(np.argmax(np.abs(corr2)))
    offset = int(lags[idx])
    start = offset + start_index  # MATLAB: data1(lag+h : lag+len+h-1)，1-based
    sliced = rx[start - 1 : start - 1 + len(tx_ref)]
    return sliced, offset, corr, lags


def db_spectrum(sig: np.ndarray) -> np.ndarray:
    """fftshift 后的 10*log10 幅度谱（与 MATLAB plot(fftshift(10*log10(abs(fft(x))))) 一致）。"""
    spec = np.abs(np.fft.fftshift(np.fft.fft(np.asarray(sig))))
    return 10 * np.log10(np.maximum(spec, 1e-12))


# -----------------------------------------------------------------------------
# AWG 波形文件（移植自 makewfm.m：Agilent/Keysight "MAGIC 1000" 格式）
# -----------------------------------------------------------------------------
def write_wfm(data: np.ndarray, filename, clock_str: str = "1.10e+08") -> Path:
    """生成 AWG 波形文件（makewfm.m 的 Python 版）。

    文件结构：MAGIC 1000 + '#' 头 + 第一个 float + 其后每个 float 间隔 1 个
    标记字节 + CLOCK 行。
    """
    if isinstance(filename, str):
        filename = Path(filename)
    wave = np.asarray(data, dtype=float).ravel()
    a = (np.abs(np.max(wave)) - np.abs(np.min(wave))) / 2.0
    wave = wave - a
    m = np.max(np.abs(wave))
    if m > 0:
        wave = wave / m

    samples = len(wave)
    sample_bytes = str(samples * 5)          # 每个样本 4 字节数据 + 1 字节标记
    header = ("#" + str(len(sample_bytes)) + sample_bytes).encode("ascii")

    body = bytearray()
    body += struct.pack("<f", float(wave[0]))
    for w in wave[1:]:
        body += struct.pack("<f", float(w))
        body += b"\x00"                      # 标记字节（对应 MATLAB fwrite(..., 'float', 1)）

    crlf = b"\x0d\x0a"
    with open(filename, "wb") as fid:
        fid.write(b"MAGIC 1000")
        fid.write(crlf)
        fid.write(header)
        fid.write(bytes(body))
        fid.write(f"CLOCK {clock_str}".encode("ascii"))
        fid.write(crlf)
    return filename
