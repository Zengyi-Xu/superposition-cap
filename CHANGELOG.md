# 开发日志

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
