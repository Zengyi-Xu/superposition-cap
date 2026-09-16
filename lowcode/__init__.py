"""Superposed 16QAM 低代码编程平台。

把叠加调制收发链路（PAM4 序列 / PAM6 差分映射 / 发射成形 / 信道 /
同步 / 下变频 / MIMO LMS 均衡 / PAM6 判决 / PAM4 解码 / BER /
AWG 双通道下载 / 示波器采集 / 画图 / 变量存取）封装为可视化节点，
像搭积木一样连线编程：

    python -m lowcode            # 打开低代码编辑器
    python -m lowcode.flowchart  # 导出叠加调制标准收发框图 PNG

模块结构：
- graph_model.py  节点图数据模型（Node / Edge / FlowGraph，JSON 序列化）
- nodes.py        叠加调制节点库（每个节点封装本项目核心函数）
- executor.py     拓扑执行引擎（按连线传递变量）
- panel.py        节点编辑器画布（拖拽 / 连线 / 缩放 / 属性面板）
- flowchart.py    标准收发框图模板 + PNG 导出
- app.py          主窗口（编辑器 + 日志 + 运行控制）
"""

__version__ = "0.1.0"
