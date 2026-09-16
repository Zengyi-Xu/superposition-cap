# Superposed 16QAM — Python Rewrite

把 MATLAB 代码（`SuperposedPS16QAM02` 文件夹，功率域 PAM4+PAM4 叠加发射、类 16QAM 接收）
用 Python 重写，并仿照 CAP_NN 项目提供同风格的 Tkinter GUI 实验平台。

## 原理

两路独立的 PAM4 比特流分别经"类部分响应"差分映射得到 PAM6 电平：

```
v(n) = dec(n) + floor(v(n-1)/2)      % dec ∈ {0,1,2,3}, v ∈ {0..5}
v = (v - 2.5) * 2                     % 缩放到 {-5,-3,-1,1,3,5}
```

两路 PAM6 分别用 cos/sin 子载波上变频（子载波间隔 BW = 1 + roll_off），
单路接收（MISO）时两路波形直接叠加，等效为一路 16QAM 信号；
接收端做 xcorr 同步、正交下变频、SRRC 匹配滤波、按 3 抽取，
再用 2x2 MIMO LMS 均衡分离 I/Q 两路，PAM6 最近邻判决后差分解码回 PAM4，逐比特算 BER。

## 与 MATLAB 文件的对应关系

| Python | MATLAB |
| --- | --- |
| `superposition_core.py` | A1_TX_MIMOPAM4toPAM_0723.m / A2_RX_MIMOPAM4toPAM_0826.m / PAM6_demodulation.m |
| `equalizer.py` | LMS_1DownS_Testnan.m / MIMOLMS_1DownS_Testnan.m |
| `utils.py` (`write_wfm`) | makewfm.m |
| `oscilloscope.py` | A2_RX 中的 tcpip/SCPI 采集段 |
| `channel.py` | （新增，复用 CAP_NN 的 VLC 虚拟信道，便于无硬件仿真） |

## 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

## 快速开始

### 命令行（离线虚拟信道）

```bash
python main.py --data-source virtual --snr 20
```

### GUI

```bash
python superposition_gui.py
```

GUI 共 5 个页签：波形与频谱 / 叠加调制 / 传输实验结果 / 运行测试 / 示波器。
在"运行测试"页选择数据源（virtual 虚拟信道、file 已存波形、scope 在线示波器），
设置符号数、SNR、种子与 LMS 参数后点击"运行仿真"，结果自动保存到 `data/` 并刷新图形。

### 在线采集（Keysight 示波器）

修改 `config.py` 中的地址（默认 `TCPIP0::169.254.140.83::5025::SOCKET`），
或直接在 GUI"示波器"页输入地址连接并采集。

## 目录结构

```
superposition cap/
├── config.py                # 参数（镜像 MATLAB 常量）与路径
├── superposition_core.py    # 叠加调制/解调核心 DSP
├── equalizer.py             # LMS 与 2x2 MIMO LMS 均衡器
├── channel.py               # 虚拟信道（VLC 模型 + AWGN）
├── oscilloscope.py          # Keysight 示波器 pyvisa 采集
├── utils.py                 # 文件 IO / 重采样 / xcorr 同步 / wfm 生成
├── record.py                # 实验记录（JSON + 文本摘要）
├── main.py                  # 命令行入口
├── superposition_gui.py     # GUI 入口（CAP_NN 设计语言）
├── data/
│   ├── txdata/              # 发射波形、PAM6 电平、PAM4 序列、.wfm
│   ├── rxdata/              # 接收波形、均衡后符号
│   ├── plots/               # 导出的图像/数据
│   └── records/             # 每次实验的 record_<run_id>.json/.txt
└── requirements.txt
```
