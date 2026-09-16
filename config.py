"""Superposed 16QAM（功率域 PAM4+PAM4 叠加）Python 收发机的配置参数。

镜像自以下 MATLAB 参数集（SuperposedPS16QAM02 文件夹）：
  - A1_TX_MIMOPAM4toPAM_0723.m    （发射机）
  - A2_RX_MIMOPAM4toPAM_0826.m    （接收机，900 MSa/s 版）
  - MIMOLMS_1DownS_Testnan.m      （2x2 MIMO LMS 均衡器）
"""
from pathlib import Path

# ---------------------------------------------------------------------------
# 项目路径
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TXDATA_DIR = DATA_DIR / "txdata"
RXDATA_DIR = DATA_DIR / "rxdata"
PLOT_DIR = DATA_DIR / "plots"
RECORD_DIR = DATA_DIR / "records"

for _d in (DATA_DIR, TXDATA_DIR, RXDATA_DIR, PLOT_DIR, RECORD_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 发射机参数（A1_TX_MIMOPAM4toPAM_0723.m）
# ---------------------------------------------------------------------------
PAM_ORDER = 4                     # 每路基带 PAM 阶数
DATANO = 1024 * 32                # 符号数
UPSAMPLENO = 3                    # 上采样倍数（每个符号 3 个采样点）
ROLLOFF = 0.205                   # SRRC 滚降系数
FILTER_SPAN = 10                  # 滤波器截断长度（符号数）
FILTER_SPS = 3                    # 每个符号周期的滤波器采样点数
START_FREQ = 5e6 / 150e6          # 起始频率偏移（归一化）
# BW = 1.2                        # 固定 spacing
BW = 1 + ROLLOFF                  # 可变 spacing
SUBCAR1 = BW / 2 + START_FREQ     # 子载波频率（rad / t1 单位）

SEED_BAND1 = 100                  # 第一路 PAM4 随机序列种子（rng(100)）
SEED_BAND2 = 200                  # 第二路 PAM4 随机序列种子（rng(200)）

# ---------------------------------------------------------------------------
# 采样率（A2_RX_MIMOPAM4toPAM_0826.m：AWG 900 MSa/s，示波器 2000 MSa/s）
# ---------------------------------------------------------------------------
AWG_SAMPLE = 900                  # AWG 采样率（MSa/s）
OSC_SAMPLE = 2000                 # 示波器采样率（MSa/s）

# ---------------------------------------------------------------------------
# 接收机 / LMS 均衡器参数
# ---------------------------------------------------------------------------
LMS_TAPS = 11                     # LMS 抽头数
LMS_MU1 = 0.0065                  # 同路 LMS 步长 u_LMS
LMS_MU2 = 0.0257                  # 异路 LMS 步长 u_LMS2（抑制另一路基带串扰）
NUMOF_TS = 2000                   # 训练符号数

# ---------------------------------------------------------------------------
# 运行模式
# ---------------------------------------------------------------------------
# data_source: "virtual"  = 虚拟信道（离线仿真）
#              "file"     = 读取 data/rxdata 下已保存的接收波形
#              "scope"    = 通过 pyvisa 从示波器在线采集
DATA_SOURCE = "virtual"
USE_VIRTUAL_CHANNEL = 1           # 1 = 虚拟信道，0 = 读取文件

# ---------------------------------------------------------------------------
# 虚拟信道参数（VLC 信道模型，复用 CAP_NN channel.vlc_channel 参数风格）
# ---------------------------------------------------------------------------
SNR_DB = 20.0
CHANNEL_FS = 100                  # 指数衰减带宽参数
CHANNEL_FACTOR = 40               # 衰减因子；越大 -> 带宽越宽 / 衰落越小
CHANNEL_NONLINEAR = False         # 是否启用弱 LED 非线性

# ---------------------------------------------------------------------------
# 硬件地址（Keysight 示波器，TCP/IP，与 MATLAB 中 tcpip('169.254.140.83',5025) 对应）
# ---------------------------------------------------------------------------
OSC_VISA_ADDR = "TCPIP0::169.254.140.83::5025::SOCKET"
OSC_CHANNEL = "CHAN1"
OSC_SAMPLE_RATE = 2000e6          # :ACQUIRE:SRATE
OSC_TIMEBASE_SCALE = 60e-6        # :TIMEBASE:SCALE

# ---------------------------------------------------------------------------
# M8190A 任意波形发生器（Keysight，TCP/IP 5025）
# ---------------------------------------------------------------------------
AWG_SAMPLE_RATE = AWG_SAMPLE * 1e6  # AWG 采样率（Hz），= 900 MSa/s
AWG_VPP = 0.5                     # 输出幅度（Vpp）
AWG_OUTPUT_ROUTE = "DAC"          # 输出路径: DC / AC / DAC
# VISA 地址（把 localhost 换成 AWG 实际 IP）：
AWG_VISA_ADDR = "TCPIP0::localhost::5025::SOCKET"

# ---------------------------------------------------------------------------
# 绘图 / 记录
# ---------------------------------------------------------------------------
PLOT_SHOW = False
PLOT_SAVE = True
PLOT_DPI = 300
