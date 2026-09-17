"""Tektronix AWG520 任意波形发生器控制（pyvisa + GPIB）。

参考 Qcodes 社区驱动（qcodes_contrib_drivers/drivers/Tektronix/AWG520）
的 MMEM:DATA / MAGIC 1000 波形下载协议：
  - 通过 GPIB 接口连接（AWG520 不支持以太网程控）
  - 设置采样时钟 SOUR:FREQ、输出幅度 SOURn:VOLT:LEV:IMM:AMPL
  - 将浮点波形打包为 MAGIC 1000 格式，通过 MMEM:DATA 写入仪器
  - 使用 SOURn:FUNC:USER 把波形文件分配给对应通道
  - AWGC:RUN / AWGC:STOP 控制播放

叠加调制需要两路不同波形：data1 -> CH1，data2 -> CH2（见 download_two_channels）。
两路幅度可分别调节。
"""
import struct
from typing import Callable, Dict, Optional, Tuple

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
                 vpp_ch1: float = None,
                 vpp_ch2: float = None,
                 timeout_ms: int = 30000,
                 log: LogFn = None):
        """
        Args:
            visa_addr: VISA 资源字符串，如 "GPIB0::1::INSTR"
            sample_rate: AWG 采样率（Hz），默认取 config.AWG_SAMPLE_RATE
            vpp: 两路共用输出幅度（Vpp）；vpp_ch1/vpp_ch2 留空时生效
            vpp_ch1: CH1 输出幅度（Vpp）
            vpp_ch2: CH2 输出幅度（Vpp）
            timeout_ms: 通信超时
            log: 日志回调（GUI 线程安全输出用）
        """
        if visa_addr is None or str(visa_addr).lower() == "auto":
            visa_addr = instr_disc.auto_detect_awg() or cfg.AWG_VISA_ADDR
        self.visa_addr = visa_addr or cfg.AWG_VISA_ADDR
        self.sample_rate = sample_rate or cfg.AWG_SAMPLE_RATE

        # 优先使用每通道独立幅度；未指定时回退到共用 vpp / 配置默认值
        base_vpp = vpp if vpp is not None else cfg.AWG_VPP
        self.vpp_ch1 = vpp_ch1 if vpp_ch1 is not None else base_vpp
        self.vpp_ch2 = vpp_ch2 if vpp_ch2 is not None else base_vpp

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
                  vpp_ch1: Optional[float] = None,
                  vpp_ch2: Optional[float] = None,
                  channels: Tuple[int, ...] = (1, 2)) -> None:
        """配置采样时钟与输出幅度（支持两路分别设幅度）。"""
        sample_rate = sample_rate or self.sample_rate

        # 每通道幅度：独立参数 > 默认实例值 > 共用 vpp
        vpp_map: Dict[int, float] = {1: self.vpp_ch1, 2: self.vpp_ch2}
        if vpp_ch1 is not None:
            vpp_map[1] = vpp_ch1
        if vpp_ch2 is not None:
            vpp_map[2] = vpp_ch2
        if vpp is not None:
            for ch in channels:
                vpp_map[ch] = vpp

        # 全局采样时钟（AWG520 单时钟驱动双通道）
        self.write(f"SOUR:FREQ {sample_rate:.15g}")

        for ch in channels:
            ch_vpp = vpp_map[ch]
            self.write(f"SOUR{ch}:VOLT:LEV:IMM:AMPL {ch_vpp:.15g}")
            self.write(f"SOUR{ch}:VOLT:LEV:IMM:OFFS 0")
            # 默认 marker 高低电平
            self.write(f"SOUR{ch}:MARK1:VOLT:LEV:IMM:HIGH 1.0")
            self.write(f"SOUR{ch}:MARK1:VOLT:LEV:IMM:LOW 0.0")
            self.write(f"SOUR{ch}:MARK2:VOLT:LEV:IMM:HIGH 1.0")
            self.write(f"SOUR{ch}:MARK2:VOLT:LEV:IMM:LOW 0.0")

        self.query("*OPC?")
        vpp_str = "/".join(f"{vpp_map[ch]:.2f}" for ch in channels)
        self._log(f"AWG 已配置: fs={sample_rate/1e6:.1f} MSa/s, Vpp=[{vpp_str}] V")

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

    def _loaded_channels(self) -> Tuple[int, ...]:
        """查询当前已分配波形文件的通道列表。"""
        loaded = []
        for ch in (1, 2):
            try:
                resp = self.query(f"SOUR{ch}:FUNC:USER?").strip()
                # 典型返回: "ch1.wfm","MAIN" 或 "NONE"
                filename = resp.split(",")[0].strip().strip('"').strip()
                if filename and filename.upper() != "NONE":
                    loaded.append(ch)
            except Exception:
                pass
        return tuple(loaded)

    def start(self, channels: Optional[Tuple[int, ...]] = None) -> None:
        """开始 AWG 输出（不下发波形，仅播放当前已加载的波形）。

        未指定通道时自动查询已加载波形的通道，避免对空通道发送 OUTP ON。
        """
        if channels is None:
            channels = self._loaded_channels()
        if not channels:
            self._log("AWG 没有已加载波形的通道，无法开始输出。请先下载波形。")
            return
        for ch in channels:
            self.write(f"OUTP{ch} ON")
        self.write("AWGC:RUN")
        self.query("*OPC?")
        self._log(f"AWG 输出已开始 ({', '.join(f'CH{ch}' for ch in channels)})。")

    def stop(self, channels: Tuple[int, ...] = (1, 2)) -> None:
        """停止 AWG 输出。"""
        self.write("AWGC:STOP")
        for ch in channels:
            self.write(f"OUTP{ch} OFF")
        self._log("AWG 输出已停止。")

    def set_sample_rate(self, sample_rate: float = None) -> None:
        """仅设置 AWG 全局采样时钟，不下发波形。"""
        sample_rate = sample_rate or self.sample_rate
        self.write(f"SOUR:FREQ {sample_rate:.15g}")
        self.query("*OPC?")
        self._log(f"AWG 采样率已设置为 {sample_rate/1e6:.1f} MSa/s")

    def apply_output_settings(self,
                              sample_rate: float = None,
                              vpp_ch1: float = None,
                              vpp_ch2: float = None,
                              channels: Optional[Tuple[int, ...]] = None,
                              run: bool = True) -> None:
        """不下发波形，仅更新采样率和输出幅度，并自动启动已加载波形的通道。"""
        sample_rate = sample_rate or self.sample_rate
        self.write(f"SOUR:FREQ {sample_rate:.15g}")

        if channels is None:
            channels = self._loaded_channels()

        vpp_map = {1: vpp_ch1, 2: vpp_ch2}
        for ch in channels:
            vpp = vpp_map.get(ch)
            if vpp is not None:
                self.write(f"SOUR{ch}:VOLT:LEV:IMM:AMPL {vpp:.15g}")

        self.query("*OPC?")
        if channels:
            active = ", ".join(f"CH{ch}={vpp_map[ch]:.2f}V" for ch in channels if vpp_map[ch] is not None)
            self._log(f"AWG 输出参数已更新: fs={sample_rate/1e6:.1f} MSa/s, "
                      f"已加载通道: {', '.join(f'CH{ch}' for ch in channels)}"
                      + (f" ({active})" if active else ""))
            if run:
                self.start(channels=channels)
        else:
            self._log(f"AWG 采样率已更新为 {sample_rate/1e6:.1f} MSa/s，但当前没有已加载波形的通道。")

    def clear_waveforms(self, filenames: Tuple[str, ...] = ("ch1.wfm", "ch2.wfm"),
                        channels: Tuple[int, ...] = (1, 2)) -> None:
        """停止输出并删除 AWG 内存中的指定波形文件。"""
        self.stop(channels=channels)
        for filename in filenames:
            try:
                self.write(f'MMEM:DEL "{filename}","MAIN"')
            except Exception as exc:
                self._log(f"删除 {filename} 时警告: {exc}")
        self.query("*OPC?")
        self._log("AWG 已清空。")


# 保持与旧 M8190A 控制器的向下兼容：导入名不变
M8190AController = AWG520Controller


def download_two_channels(data1: np.ndarray,
                          data2: np.ndarray,
                          sample_rate: float = None,
                          vpp: float = None,
                          vpp_ch1: float = None,
                          vpp_ch2: float = None,
                          visa_addr: Optional[str] = None,
                          log: LogFn = None,
                          stop_first: bool = True) -> None:
    """便捷函数：连接 -> 配置 -> data1 下载到 CH1、data2 下载到 CH2 -> 同时播放。

    叠加调制两路基带（I 路 cos 子载波 / Q 路 sin 子载波）需要两路不同波形，
    分别下发到 AWG520 的两个通道。可分别设置 CH1/CH2 的 Vpp。
    """
    with AWG520Controller(visa_addr=visa_addr,
                          sample_rate=sample_rate,
                          vpp=vpp,
                          vpp_ch1=vpp_ch1,
                          vpp_ch2=vpp_ch2,
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


def download_single_channel(data: np.ndarray,
                            sample_rate: float = None,
                            vpp: float = None,
                            channel: int = 1,
                            visa_addr: Optional[str] = None,
                            log: LogFn = None,
                            stop_first: bool = True) -> None:
    """便捷函数：连接 -> 配置 -> 将单路波形下载到指定通道并播放。

    用于把叠加后的单路波形（例如 tx_sum）直接输出到 AWG 的一个通道，
    从而只需要使用一个 AWG 输出端口 + 一个示波器采集端口。
    """
    with AWG520Controller(visa_addr=visa_addr,
                          sample_rate=sample_rate,
                          vpp=vpp,
                          log=log) as awg:
        awg.configure(channels=(channel,))
        if stop_first:
            awg.stop(channels=(channel,))
        awg.download_waveform(data, channel=channel,
                              filename=f"ch{channel}.wfm", run=True)
        if log:
            log(f"CH{channel} 单通道波形已开始播放。")


def combine_and_download_single_channel(data1: np.ndarray,
                                        data2: np.ndarray,
                                        sample_rate: float = None,
                                        vpp: float = None,
                                        channel: int = 1,
                                        visa_addr: Optional[str] = None,
                                        log: LogFn = None,
                                        stop_first: bool = True) -> None:
    """便捷函数：将两路波形叠加并归一化后，下载到 AWG 单通道播放。

    两路信号在基带直接相加（等效于 generate_tx 中的 tx_sum），再整体归一化到
    [-1, 1] 范围，避免 DAC 削顶。最终只用 AWG 的 CH1 输出两路信号的叠加。
    """
    data_sum = np.asarray(data1, dtype=float) + np.asarray(data2, dtype=float)
    peak = np.max(np.abs(data_sum))
    if peak > 0:
        data_sum = data_sum / peak
    download_single_channel(
        data_sum,
        sample_rate=sample_rate,
        vpp=vpp,
        channel=channel,
        visa_addr=visa_addr,
        log=log,
        stop_first=stop_first,
    )


def start_awg_output(visa_addr: Optional[str] = None,
                     log: LogFn = None) -> None:
    """便捷函数：连接 AWG 后仅开始播放当前已加载的波形。"""
    with AWG520Controller(visa_addr=visa_addr, log=log) as awg:
        awg.start()
        if log:
            log("AWG 已开始输出当前波形。")


def set_awg_sample_rate(sample_rate: float,
                        visa_addr: Optional[str] = None,
                        log: LogFn = None) -> None:
    """便捷函数：连接 AWG 后仅设置采样率，不下发任何波形。

    适用于只改变 AWG 时钟而保持已下载波形不变的场景。
    """
    with AWG520Controller(visa_addr=visa_addr, sample_rate=sample_rate,
                          log=log) as awg:
        awg.set_sample_rate(sample_rate)
        if log:
            log(f"AWG 采样率已设置为 {sample_rate/1e6:.1f} MSa/s（未重新下载波形）")


def apply_awg_output_settings(sample_rate: float,
                              vpp_ch1: float,
                              vpp_ch2: float,
                              visa_addr: Optional[str] = None,
                              log: LogFn = None) -> None:
    """便捷函数：连接 AWG 后仅更新采样率和两路输出幅度，不下发波形。"""
    with AWG520Controller(visa_addr=visa_addr, sample_rate=sample_rate,
                          vpp_ch1=vpp_ch1, vpp_ch2=vpp_ch2,
                          log=log) as awg:
        awg.apply_output_settings(sample_rate, vpp_ch1, vpp_ch2)
        if log:
            log(f"AWG 输出参数已应用: fs={sample_rate/1e6:.1f} MSa/s, "
                f"Vpp=[{vpp_ch1:.2f}, {vpp_ch2:.2f}] V")


def clear_awg(visa_addr: Optional[str] = None,
              log: LogFn = None) -> None:
    """便捷函数：连接 AWG 后停止输出并删除已下载波形。"""
    with AWG520Controller(visa_addr=visa_addr, log=log) as awg:
        awg.clear_waveforms()
        if log:
            log("AWG 已清空，当前波形已删除。")
