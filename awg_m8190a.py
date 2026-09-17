"""Tektronix AWG520 任意波形发生器控制（pyvisa + GPIB）。

参考 Qcodes 社区驱动（qcodes_contrib_drivers/drivers/Tektronix/AWG520）
的 MMEM:DATA / MAGIC 1000 波形下载协议：
  - 通过 GPIB 接口连接（AWG520 不支持以太网程控）
  - 设置采样时钟 SOUR:FREQ、输出幅度 SOURn:VOLT:LEV:IMM:AMPL
  - 将浮点波形打包为 MAGIC 1000 格式，通过 MMEM:DATA 写入仪器
  - 使用 SOURn:FUNC:USER 把波形文件分配给对应通道
  - AWGC:RUN / AWGC:STOP 控制播放

叠加调制需要两路不同波形：data1 -> CH1，data2 -> CH2（见 download_two_channels）。
"""
import struct
from typing import Callable, Optional, Tuple

import numpy as np
import pyvisa

import config as cfg
import instrument_discovery as instr_disc

LogFn = Optional[Callable[[str], None]]


class AWG520Controller:
    """Tektronix AWG520 AWG 控制器（GPIB）。"""

    def __init__(self,
                 visa_addr: Optional[str] = None,
                 sample_rate: float = None,
                 vpp: float = None,
                 timeout_ms: int = 30000,
                 log: LogFn = None):
        """
        Args:
            visa_addr: VISA 资源字符串，如 "GPIB0::1::INSTR"
            sample_rate: AWG 采样率（Hz），默认取 config.AWG_SAMPLE_RATE
            vpp: 输出幅度（Vpp）
            timeout_ms: 通信超时
            log: 日志回调（GUI 线程安全输出用）
        """
        if visa_addr is None or str(visa_addr).lower() == "auto":
            visa_addr = instr_disc.auto_detect_awg() or cfg.AWG_VISA_ADDR
        self.visa_addr = visa_addr or cfg.AWG_VISA_ADDR
        self.sample_rate = sample_rate or cfg.AWG_SAMPLE_RATE
        self.vpp = vpp if vpp is not None else cfg.AWG_VPP
        self.timeout_ms = timeout_ms
        self._log = log or (lambda msg: None)
        self.rm = pyvisa.ResourceManager()
        self.inst: Optional[pyvisa.Resource] = None

    # ------------------------------------------------------------------
    def connect(self) -> "AWG520Controller":
        """建立连接并验证设备。"""
        self._log(f"正在连接 AWG520: {self.visa_addr} ...")
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
                  channels: Tuple[int, ...] = (1, 2)) -> None:
        """配置采样时钟与输出幅度。"""
        sample_rate = sample_rate or self.sample_rate
        vpp = vpp if vpp is not None else self.vpp

        # 全局采样时钟（AWG520 单时钟驱动双通道）
        self.write(f"SOUR:FREQ {sample_rate:.15g}")

        for ch in channels:
            self.write(f"SOUR{ch}:VOLT:LEV:IMM:AMPL {vpp:.15g}")
            self.write(f"SOUR{ch}:VOLT:LEV:IMM:OFFS 0")
            # 默认 marker 高低电平
            self.write(f"SOUR{ch}:MARK1:VOLT:LEV:IMM:HIGH 1.0")
            self.write(f"SOUR{ch}:MARK1:VOLT:LEV:IMM:LOW 0.0")
            self.write(f"SOUR{ch}:MARK2:VOLT:LEV:IMM:HIGH 1.0")
            self.write(f"SOUR{ch}:MARK2:VOLT:LEV:IMM:LOW 0.0")

        self.query("*OPC?")
        self._log(f"AWG 已配置: fs={sample_rate/1e6:.1f} MSa/s, Vpp={vpp} V")

    # ------------------------------------------------------------------
    @staticmethod
    def _pack_magic1000(data: np.ndarray,
                        markers1: Optional[np.ndarray] = None,
                        markers2: Optional[np.ndarray] = None) -> bytes:
        """将 [-1, 1] 浮点波形打包为 AWG520 MAGIC 1000 数据体。

        每点 5 字节：4 字节小端 float + 1 字节 marker（m1 + 2*m2）。
        """
        data = np.asarray(data, dtype=float).ravel()
        n = len(data)
        if np.max(np.abs(data)) > 1.0:
            data = data / np.max(np.abs(data))

        if markers1 is None:
            markers1 = np.zeros(n, dtype=np.uint8)
        else:
            markers1 = np.asarray(markers1, dtype=np.uint8).ravel()
        if markers2 is None:
            markers2 = np.zeros(n, dtype=np.uint8)
        else:
            markers2 = np.asarray(markers2, dtype=np.uint8).ravel()

        if not (len(markers1) == n and len(markers2) == n):
            raise ValueError("marker 长度必须与波形长度一致")

        m = markers1 + 2 * markers2
        body = bytearray()
        for w, mk in zip(data, m):
            body += struct.pack("<f", float(w))
            body += struct.pack("B", int(mk))
        return bytes(body)

    def download_waveform(self,
                          data: np.ndarray,
                          channel: int = 1,
                          filename: str = "ch1.wfm",
                          run: bool = True,
                          markers1: Optional[np.ndarray] = None,
                          markers2: Optional[np.ndarray] = None) -> None:
        """下载波形到指定通道并可选立即播放。"""
        if self.inst is None:
            raise RuntimeError("AWG 未连接")

        sample_rate = self.sample_rate
        body = self._pack_magic1000(data, markers1, markers2)
        segm_len = len(data)

        # 构造 MAGIC 1000 文件块
        # 内层：#<lenlen><len>MAGIC 1000\n#<lenlen><len><body>CLOCK <clock>\n
        inner_header = b"MAGIC 1000\n"
        body_header = ("#" + str(len(str(len(body)))) + str(len(body))).encode("ascii")
        clock_line = f"CLOCK {sample_rate:.10e}\n".encode("ascii")
        inner = inner_header + body_header + body + clock_line

        outer_len_str = str(len(inner))
        outer_header = ("#" + str(len(outer_len_str)) + outer_len_str).encode("ascii")

        msg = f'MMEM:DATA "{filename}",'.encode("ascii") + outer_header + inner

        self.inst.write_raw(msg)
        self.query("*OPC?")

        # 分配给通道并开启输出
        self.write(f'SOUR{channel}:FUNC:USER "{filename}","MAIN"')
        self.write(f"OUTP{channel} ON")

        if run:
            self.write("AWGC:RUN")
            self.query("*OPC?")
        self._log(f"通道 {channel} 文件 {filename} 下载完成（{segm_len} 采样点）"
                  + ("，播放中" if run else ""))

    def stop(self, channels: Tuple[int, ...] = (1, 2)) -> None:
        """停止 AWG 输出。"""
        self.write("AWGC:STOP")
        for ch in channels:
            self.write(f"OUTP{ch} OFF")
        self._log("AWG 输出已停止。")


# 保持与旧 M8190A 控制器的向下兼容：导入名不变
M8190AController = AWG520Controller


def download_two_channels(data1: np.ndarray,
                          data2: np.ndarray,
                          sample_rate: float = None,
                          vpp: float = None,
                          visa_addr: Optional[str] = None,
                          log: LogFn = None,
                          stop_first: bool = True) -> None:
    """便捷函数：连接 -> 配置 -> data1 下载到 CH1、data2 下载到 CH2 -> 同时播放。

    叠加调制两路基带（I 路 cos 子载波 / Q 路 sin 子载波）需要两路不同波形，
    分别下发到 AWG520 的两个通道。
    """
    with AWG520Controller(visa_addr=visa_addr,
                          sample_rate=sample_rate,
                          vpp=vpp,
                          log=log) as awg:
        awg.configure(channels=(1, 2))
        if stop_first:
            awg.stop()
        # 先分别下载（不播放），再同时启动两通道，保证同步输出
        awg.download_waveform(data1, channel=1, filename="ch1.wfm", run=False)
        awg.download_waveform(data2, channel=2, filename="ch2.wfm", run=False)
        awg.write("AWGC:RUN")
        awg.query("*OPC?")
        if log:
            log("CH1 / CH2 已开始同步播放。")
