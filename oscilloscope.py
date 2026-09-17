"""Keysight/Agilent 示波器采集（TCP/IP，pyvisa）。

移植自 MATLAB A2_RX_MIMOPAM4toPAM_0826.m 中的 SCPI 指令序列：
  :RUN / :ACQUIRE:SRATE / :TIMEBASE:SCALE / *TRG /
  :WAVEFORM:FORMAT WORD / :WAVEFORM:BYTEORDER LSBFirst /
  :WAVEFORM:SOURCE CHANx / :WAVEFORM:PREAMBLE? / :WAV:DATA?

波形数据按 preamble 换算为伏特：Y = YIncrement * (Raw - YReference) + YOrigin。
"""
from typing import Optional

import numpy as np

import config
import instrument_discovery as instr_disc


class ScopeError(RuntimeError):
    pass


class KeysightScope:
    """Keysight/Agilent InfiniiVision 示波器（TCP/IP SOCKET VISA 地址）。"""

    def __init__(self, visa_addr: str = config.OSC_VISA_ADDR,
                 timeout_ms: int = 10_000):
        if visa_addr is None or visa_addr.lower() == "auto":
            visa_addr = instr_disc.auto_detect_scope() or config.OSC_VISA_ADDR
        self.visa_addr = visa_addr
        self.timeout_ms = timeout_ms
        self._rm = None
        self._inst = None

    # ------------------------------------------------------------------
    def connect(self) -> None:
        if self._inst is not None:
            return
        try:
            import pyvisa
        except ImportError as exc:
            raise ScopeError("缺少 pyvisa，请先: pip install pyvisa PyVISA-py") from exc
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self.visa_addr)
        self._inst.timeout = self.timeout_ms
        try:
            self._inst.write("*CLS")
        except Exception:
            pass

    def disconnect(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
            self._inst = None
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            self._rm = None

    @property
    def is_connected(self) -> bool:
        return self._inst is not None

    def idn(self) -> str:
        self._require()
        return self._inst.query("*IDN?").strip()

    # ------------------------------------------------------------------
    def configure(self,
                  sample_rate: float = config.OSC_SAMPLE_RATE,
                  timebase_scale: float = config.OSC_TIMEBASE_SCALE) -> None:
        """与 MATLAB 一致：设置采样率、时基并触发采集。"""
        self._require()
        self._inst.write(":RUN")
        self._inst.write(f":ACQUIRE:SRATE {sample_rate:.6e}")
        self._inst.write(f":TIMEBASE:SCALE {timebase_scale:.6e}")
        self._inst.write("*TRG")
        self._inst.write(":WAVEFORM:FORMAT WORD")
        self._inst.write(":WAVEFORM:BYTEORDER LSBFirst")

    def acquire(self, channel: str = config.OSC_CHANNEL) -> dict:
        """采集指定通道波形，返回含 YData（伏特）与 preamble 的字典。"""
        self._require()
        self._inst.write(f":WAVEFORM:SOURCE {channel}")
        preamble = self._inst.query(":WAVEFORM:PREAMBLE?").strip()
        self._inst.write(":WAV:DATA?")
        raw = self._inst.read_raw()
        data = _parse_ieee_block(raw)

        fields = [float(x) for x in preamble.split(",")]
        if len(fields) < 10:
            raise ScopeError(f"无法解析 preamble: {preamble!r}")
        (fmt, wtype, points, count, x_incr, x_orig, x_ref,
         y_incr, y_orig, y_ref) = fields[:10]

        raw_i16 = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")
        ydata = y_incr * (raw_i16 - y_ref) + y_orig
        return {
            "channel": channel,
            "preamble": {
                "format": fmt, "type": wtype, "points": points, "count": count,
                "x_increment": x_incr, "x_origin": x_orig, "x_reference": x_ref,
                "y_increment": y_incr, "y_origin": y_orig, "y_reference": y_ref,
            },
            "raw": raw_i16,
            "ydata": ydata,
        }

    # ------------------------------------------------------------------
    def _require(self) -> None:
        if self._inst is None:
            raise ScopeError("示波器未连接")


def _parse_ieee_block(raw: bytes) -> bytes:
    """解析 '#<n><len><payload>' 形式的 IEEE 488.2 二进制块。"""
    if not raw.startswith(b"#"):
        raise ScopeError("示波器返回的不是二进制块")
    ndigits = int(chr(raw[1]))
    header_len = 2 + ndigits
    payload_len = int(raw[2:header_len].decode("ascii"))
    return raw[header_len: header_len + payload_len]


def acquire_waveform(visa_addr: str = config.OSC_VISA_ADDR,
                     channel: str = config.OSC_CHANNEL,
                     sample_rate: float = config.OSC_SAMPLE_RATE,
                     timebase_scale: float = config.OSC_TIMEBASE_SCALE) -> dict:
    """一次性连接 -> 配置 -> 采集 -> 断开，返回 acquire() 的字典。"""
    scope = KeysightScope(visa_addr)
    try:
        scope.connect()
        scope.configure(sample_rate, timebase_scale)
        return scope.acquire(channel)
    finally:
        scope.disconnect()
