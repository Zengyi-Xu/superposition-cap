"""M8190A 任意波形发生器控制（pyvisa）。

移植自 DMT_PY_NN 项目的 awg_m8190a.py（其本身对应 MATLAB 的
AWG_transmit / download_M8190A 流程），适配本项目的 900 MSa/s 采样率：
  - 通过 TCPIP socket（或 VISA）连接 M8190A
  - 设置采样率、输出幅度、输出路径（DC/AC/DAC）
  - 下载波形到指定通道/段并播放

叠加调制需要两路不同波形：data1 -> CH1，data2 -> CH2（见 download_two_channels）。
"""
from typing import Callable, Optional, Tuple

import numpy as np
import pyvisa

import config as cfg

LogFn = Optional[Callable[[str], None]]


class M8190AController:
    """M8190A AWG 控制器。"""

    def __init__(self,
                 visa_addr: Optional[str] = None,
                 sample_rate: float = None,
                 vpp: float = None,
                 output_route: str = None,
                 timeout_ms: int = 30000,
                 log: LogFn = None):
        """
        Args:
            visa_addr: VISA 资源字符串，如 "TCPIP0::192.168.1.10::5025::SOCKET"
            sample_rate: AWG 采样率（Hz），默认取 config.AWG_SAMPLE（900 MSa/s）
            vpp: 输出幅度（Vpp）
            output_route: 输出路径，"DC" / "AC" / "DAC"
                - "DC": 直流耦合放大输出（基带常用）
                - "AC": 交流耦合放大输出（RF/IF 常用）
                - "DAC": DAC 直连输出（未放大，幅度最小）
            timeout_ms: 通信超时
            log: 日志回调（GUI 线程安全输出用）
        """
        self.visa_addr = visa_addr or cfg.AWG_VISA_ADDR
        self.sample_rate = sample_rate or cfg.AWG_SAMPLE_RATE
        self.vpp = vpp if vpp is not None else cfg.AWG_VPP
        self.output_route = (output_route or cfg.AWG_OUTPUT_ROUTE).upper()
        self.timeout_ms = timeout_ms
        self._log = log or (lambda msg: None)
        self.rm = pyvisa.ResourceManager()
        self.inst: Optional[pyvisa.Resource] = None

    # ------------------------------------------------------------------
    def connect(self) -> "M8190AController":
        """建立连接并验证设备。"""
        self._log(f"正在连接 M8190A: {self.visa_addr} ...")
        self.inst = self.rm.open_resource(self.visa_addr)
        self.inst.timeout = self.timeout_ms
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"

        idn = self.query("*IDN?")
        self._log(f"*IDN = {idn}")
        return self

    def close(self) -> None:
        if self.inst is not None:
            try:
                self.inst.close()
            except Exception as exc:
                self._log(f"关闭连接时警告: {exc}")
            self.inst = None
        self.rm.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ------------------------------------------------------------------
    def write(self, cmd: str) -> None:
        if self.inst is None:
            raise RuntimeError("AWG 未连接")
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        if self.inst is None:
            raise RuntimeError("AWG 未连接")
        return self.inst.query(cmd).strip()

    # ------------------------------------------------------------------
    def configure(self,
                  sample_rate: Optional[float] = None,
                  vpp: Optional[float] = None,
                  channels: Tuple[int, ...] = (1, 2),
                  output_route: Optional[str] = None) -> None:
        """配置采样率、输出路径与幅度。"""
        sample_rate = sample_rate or self.sample_rate
        vpp = vpp if vpp is not None else self.vpp
        route = (output_route or self.output_route).upper()

        if route not in ("DC", "AC", "DAC"):
            raise ValueError(f"不支持的 M8190A 输出路径: {route}，应为 DC/AC/DAC")

        # 参考时钟
        self.write(":ROSC:FREQ 1e7")
        self.write(":ROSC:SOUR EXT")

        for ch in channels:
            self.write(f":FREQ:RAST {sample_rate:.15g}")
            # 12-bit 宽带模式
            self.write(f":TRACe{ch}:DWIDth WSP")
            self.write(f":OUTP{ch}:ROUT {route}")
            self.write(f":{route}{ch}:VOLT:AMPL {vpp:.15g}")
            if route in ("DC", "AC"):
                self.write(f":DC{ch}:FORM NRZ")
            self.write(f":OUTP{ch}:NORM ON")
            self.write(f":OUTP{ch}:COMP ON")
            self.write(f":TRAC{ch}:SEL 1")

        self.query("*OPC?")
        self._log(f"AWG 已配置: fs={sample_rate/1e9:.3f} GHz, Vpp={vpp} V, route={route}")

    # ------------------------------------------------------------------
    @staticmethod
    def _scale_to_int16(data: np.ndarray) -> np.ndarray:
        """将 [-1, 1] 浮点波形缩放到 M8190A 12-bit DAC（int16，左移 4 位）。"""
        data = np.asarray(data).ravel()
        if np.max(np.abs(data)) > 1.0:
            data = data / np.max(np.abs(data))
        return np.int16(np.round(8191 * data) * 4)

    def download_waveform(self,
                          data: np.ndarray,
                          channel: int = 1,
                          segment: int = 1,
                          run: bool = True) -> None:
        """下载波形到指定通道/段，并可选立即播放。"""
        if self.inst is None:
            raise RuntimeError("AWG 未连接")

        data = self._scale_to_int16(data)
        segm_len = len(data)

        # 删除旧段并定义新段；ABORt / INIT:IMM 以通道号为参数，不拼接在命令名后
        self.write(f":ABORt {channel}")
        self.write(f":TRACe{channel}:DELete {segment}")
        self.write(f":TRACe{channel}:DEFine {segment},{segm_len}")

        # 分块下载（Keysight 建议每次不超过 523200 个 int16）
        chunk = 523200
        offset = 0
        while offset < segm_len:
            block = data[offset:offset + chunk]
            cmd = f":TRACe{channel}:DATA {segment},{offset},"
            self.inst.write_binary_values(cmd, block, datatype="h",
                                          is_big_endian=False,
                                          header_fmt="ieee",
                                          termination=None,
                                          final_termination="\n")
            offset += chunk

        self.query("*OPC?")

        self.write(f":TRACe{channel}:SELect {segment}")
        self.write(f":FUNCtion{channel}:MODE ARBitrary")
        self.write(f":OUTPut{channel}:STATe ON")

        if run:
            self.write(f":INIT:IMM {channel}")
            self.query("*OPC?")
        self._log(f"通道 {channel} 段 {segment} 下载完成（{segm_len} 采样点）"
                  + ("，播放中" if run else ""))

    def stop(self, channels: Tuple[int, ...] = (1, 2)) -> None:
        """停止指定通道输出。"""
        for ch in channels:
            self.write(f":ABORt {ch}")
        self._log("AWG 输出已停止。")


def download_two_channels(data1: np.ndarray,
                          data2: np.ndarray,
                          sample_rate: float = None,
                          vpp: float = None,
                          visa_addr: Optional[str] = None,
                          output_route: str = None,
                          log: LogFn = None,
                          stop_first: bool = True) -> None:
    """便捷函数：连接 -> 配置 -> data1 下载到 CH1、data2 下载到 CH2 -> 同时播放。

    叠加调制两路基带（I 路 cos 子载波 / Q 路 sin 子载波）需要两路不同波形，
    分别下发到 M8190A 的两个通道。
    """
    with M8190AController(visa_addr=visa_addr,
                          sample_rate=sample_rate,
                          vpp=vpp,
                          output_route=output_route,
                          log=log) as awg:
        awg.configure(channels=(1, 2))
        if stop_first:
            awg.stop()
        # 先分别下载（不播放），再同时启动两通道，保证同步输出
        awg.download_waveform(data1, channel=1, segment=1, run=False)
        awg.download_waveform(data2, channel=2, segment=1, run=False)
        awg.write(":INIT:IMM 1")
        awg.write(":INIT:IMM 2")
        awg.query("*OPC?")
        if log:
            log("CH1 / CH2 已开始同步播放。")
