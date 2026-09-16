"""可见光通信信道模型（虚拟信道，离线仿真用）。

复用自 CAP_NN 项目的 channel.py（VLC 信道：指数衰减频率响应 + AWGN，
可选弱 LED 非线性）。原 MATLAB 代码（SuperposedPS16QAM02）无虚拟信道，
此处沿用 CAP_NN Python 版的建模方式，便于无硬件时跑通完整链路。
"""
import numpy as np


def vlc_channel(data_in: np.ndarray,
                snr_db: float,
                fs_hz: float,
                factor: float,
                nonlinear: bool = False,
                vpp: float = 1.2,
                ) -> np.ndarray:
    """应用 VLC 信道模型，可选 LED 非线性和频率衰落。

    Parameters
    ----------
    data_in : np.ndarray
        输入波形（实数，此处为两路叠加后的发射波形）。
    snr_db : float
        信道之后的目标 SNR（dB）。
    fs_hz : float
        控制指数衰减带宽的参数（MATLAB vlc_channel.m 中的 Fs）。
    factor : float
        衰减因子；越大 -> 带宽越宽 / 衰落越小。
    nonlinear : bool
        是否应用弱 LED 非线性模型。
    vpp : float
        用于非线性缩放的峰峰值电压。

    Returns
    -------
    data_rx : np.ndarray
        加入 AWGN 后的实数接收波形。
    """
    data_tx = np.asarray(data_in, dtype=float).flatten()

    if nonlinear:
        x = data_tx / (np.max(data_tx) - np.min(data_tx)) * 2 * vpp
        data_tx = 4.412 / (1.0 + np.exp(-1.07 * x)) - 2.206
        data_tx = data_tx / np.sqrt(np.mean(data_tx ** 2))

    n = len(data_tx)
    k = np.arange(1, n // 2 + 1)
    df = 2 * fs_hz / n
    fsn = df * k
    ch1 = np.exp(-fsn / factor)
    ch = np.concatenate([ch1, ch1[::-1]])

    data_ch_fft = np.fft.fft(data_tx)
    data_ch_ifft = np.real(np.fft.ifft(data_ch_fft * ch))
    data_tx = data_ch_ifft - np.mean(data_ch_ifft)

    sig_power = np.mean(data_tx ** 2)
    noise_power = sig_power / (10 ** (snr_db / 10))
    noise = np.sqrt(noise_power) * np.random.randn(len(data_tx))
    return data_tx + noise
