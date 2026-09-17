# 开发日志

## 2026-09-18（第二轮）

### 修正：传输速率计算与记录
- `main.py` / `optimizer.py`：记录中新增 `upsampleno`、`bandwidth_mhz` 字段，
  速率 = 带宽 × bits/symbol（带宽 = AWG 采样率 ÷ 上采样倍数，与 DSP 共用 `cfg.UPSAMPLENO`）。
  例：750 MSa/s ÷ 3 = 250 MHz 带宽，16QAM 速率 = 1 Gbps。
- 日志同时打印“带宽: 250.0 MHz (AWG 750 MSa/s ÷ 3 倍上采样)”。
- `record.py` 文本摘要加入“AWG 采样率 / 上采样 / 带宽”行。
- `batch_reprocess.py`：重跑时传入原记录的 `awg_sample_rate_ms`，避免被重置为默认值。
- 94 条历史记录已补 `upsampleno` / `bandwidth_mhz` 字段。
- `superposition_gui.py`：结果表格“速率”列与顶部指标栏改为同一列展示
  `速率 (采样率/×上采样/带宽)`，如 `1200 (900/×3/300M)`。

### 改进：运行测试页布局
- 左右分栏改为可拖动 PanedWindow（初始 60/40），左栏不再被挤窄。
- 实验参数卡片重排为 3 组/行，输入框随窗宽伸展；接收波形/示波器地址跨列显示完整路径。
- AWG 卡片按钮收拢为两行按钮栏（下载/开始/停止 + 应用输出设置/清空/仅生成），不再截断。
- 顶部指标栏独立成行，完整显示所有指标。
- 运行日志字体 9→10pt，行距收紧（spacing 置 0）。

## 2026-09-18

### 新增：离线 LMS 参数自动优化
- 新模块 `optimizer.py`：实现三轮坐标下降 LMS 优化。
  - 第一轮：全范围坐标下降（taps → μ1 → μ2）。
  - 第二轮：在已收敛 μ 上重新选择 taps。
  - 第三轮：在第二轮最优值附近 1/3 ~ 3 倍 log 范围细化 μ。
  - 评估次数远低于穷举网格，且能找到更优参数。
- `superposition_gui.py`：Run Test 页新增“离线 LMS 参数优化”卡片，
  支持设置 taps / μ1 / μ2 范围、log/linear 采样，优化完成后自动把最优参数写回当前设置。
- 默认勾选“启用 LMS 参数扫参”。

### 新增：批量重跑与记录清理脚本
- `batch_reprocess.py`：批量重跑 `data_source=file` 的历史记录，复用原 run_id，
  用当前代码更新实际 SNR 与 BER；BER 变差时保留旧记录并提示。
- `cleanup_records.py`：删除仅有实验 ID 但对应波形文件（tx/rx/eq）已丢失的记录。

### 改进：实际 SNR 估计与文件数据源 ID 复用
- `main.py`：file / scope 数据源下，LMS 均衡后用 `recoverdata` 与参考符号计算实际 SNR，
  覆盖原记录中的 `snr_db`；virtual 模式仍使用输入 SNR。
- `superposition_gui.py`：file 数据源手动运行时，从 `rx_<run_id>.txt` 提取原 run_id，
  不生成新 ID；BER 变差时不覆盖原记录。
- SNR 输入框仅在 virtual 模式下可用。

### 改进：AWG 控制与单通道叠加
- `awg_m8190a.py`：
  - `start()` 只启动已加载波形的通道，避免空通道导致无法输出。
  - `apply_output_settings()` 更新采样率/Vpp 后自动运行已加载通道。
  - 新增 `clear_waveforms()` 清空 AWG 波形并停止输出。
- `superposition_gui.py`：AWG 卡片新增“开始输出”“应用输出设置”“清空 AWG”“仅生成波形”按钮，
  并在调制方式/叠加方式变更后提示重新下载。
- 新增 `settings.py` 持久化用户地址与运行参数。

### 新增：结果表格“传输速率”列
- `main.py` / `optimizer.py`：计算并记录 `data_rate_mbps`。
- `superposition_gui.py`：Results 表格与顶部指标栏显示传输速率。
- 所有历史记录已补填速率字段。

### 修复与 UI 调整
- `superposition_gui.py`：
  - Run Test 页左侧参数/AWG/优化卡片改为可滚动区域，支持鼠标滚轮；右侧日志固定。
  - 整体字体放大（基础 11pt），卡片间距收紧，1080P 下更整齐。
  - 修复离线 file 模式误选 `eq_*.txt` 导致的 shape 广播错误，增加清晰提示。
- `record.py`：文本摘要加入传输速率。

## 2026-09-17

### 改写：AWG 控制从 Keysight M8190A 改为 Tektronix AWG520（GPIB）
- `awg_m8190a.py`：整体替换为 AWG520 控制器，使用 GPIB 地址连接，
  通过 `MMEM:DATA` + MAGIC 1000 格式下载波形，命令集参考 Qcodes 社区驱动。
- `download_two_channels(data1, data2)` 保持签名兼容，去掉 M8190A 专用
  `output_route` 参数。
- `config.py`：`AWG_VISA_ADDR` 改为 `GPIB0::1::INSTR`，移除 `AWG_OUTPUT_ROUTE`。
- `superposition_gui.py`：AWG 卡片改为 AWG520，去掉输出路径选择。
- `lowcode/nodes.py`：AWG 双通道下载节点描述与参数同步更新。

## 2026-09-16

### 修复：BER 恒为 0 的严重 bug
- `superposition_core.py` `biterr()`：原实现用 `uint64.view(uint8)` 后取 `[:, -width:]`，
  小端机器上最后一列是恒为 0 的最高位字节，导致任意误码下 BER 都输出 0。
  改为移位实现 `(a[:, None] >> shifts) & 1`。
- 验证：SNR 扫描（-5/0/4/8/12/20/30 dB）BER 随信噪比单调变化，30 dB 归零。

### 修复：GUI 启动即崩溃
- `superposition_gui.py` `ScopePanel.__init__` 调用了不存在的方法 `_append_log`
  （实际名为 `_log`），打开 GUI 立刻 AttributeError。

### 修复：GUI 布局
- “浏览…”按钮与接收文件 Entry 放在同一网格格（sticky=E 遮挡输入框）。
  按钮拆到独立格，示波器地址下移一行。

### 新增：M8190A AWG 控制
- 新模块 `awg_m8190a.py`（移植自 DMT_PY_NN，对应 MATLAB AWG_transmit/download_M8190A）：
  pyvisa 连接、900 MSa/s、输出路径 DC/AC/DAC、12-bit WSP 模式 int16 分块下载。
- `download_two_channels(data1, data2)`：叠加调制两路不同波形分别下发
  CH1（I 路 cos 子载波）/ CH2（Q 路 sin 子载波），两通道下载完后同时
  `:INIT:IMM` 保证同步播放。
- `config.py` 新增 AWG_VISA_ADDR / AWG_VPP / AWG_OUTPUT_ROUTE / AWG_SAMPLE_RATE。
- GUI“运行测试”页新增 M8190A 卡片：地址/Vpp/输出路径 + 双通道下载 + 停止输出，
  后台线程执行不卡 UI。

### 新增：低代码编程平台（镜像 DMT_PY_NN 引擎）
- 新包 `lowcode/`：
  - `graph_model.py` / `panel.py` / `executor.py` 移植自 DMT（节点图模型、
    拖拽连线画布、拓扑执行引擎；文件后缀 `.spflow.json`）。
  - `nodes.py` 本项目节点库（19 种）：PAM4 序列源、PAM6 差分映射、发射成形、
    虚拟信道、AWG 双通道下载、示波器采集、互相关同步、下变频抽取、
    MIMO LMS 均衡、PAM6 判决、PAM4 解码、误码统计/BER 汇总、完整实验、
    时域/频谱/星座图、变量存取、注释。
  - `flowchart.py` 标准收发框图模板 + PNG 导出；硬件节点默认 `enabled=False`
    禁用（执行引擎新增跳过逻辑）。
  - `app.py` 主窗口；入口 `python lowcode_app.py` 或 `python -m lowcode`。
- 主 GUI 新增菜单“工具 → 低代码流程编辑器”。
- 验证：模板图 15 节点端到端无头执行通过（SNR=20dB 时两路 BER ≈ 6e-4/5e-4，
  与 main.py 一致），编辑器与主 GUI 冒烟测试通过。
