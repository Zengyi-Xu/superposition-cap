"""Keysight/Agilent 示波器采集（TCP/IP，pyvisa）。

参考 DMT_PY_NN/oscilloscope.py 的连续 RUN + read_binary_values 方案：
  - 让示波器保持在 RUN 状态，直接读取当前波形缓冲区
  - 设置固定采集点数与 RAW 模式，确保 preamble 为 10 字段标准格式
  - 用 read_binary_values 读取 :WAV:DATA? 返回的 WORD 数据

波形数据按 preamble 换算为伏特：Y = YIncrement * (Raw - YReference) + YOrigin。
"""
import atexit
from typing import Optional, Tuple

import numpy as np

import config
import instrument_discovery as instr_disc


class ScopeError(RuntimeError):
    pass


class KeysightScope:
    """Keysight/Agilent InfiniiVision 示波器（TCP/IP SOCKET VISA 地址）。"""

    def __init__(self, visa_addr: str = config.OSC_VISA_ADDR,
                 timeout_ms: int = 10_000):
        if visa_addr is not None:
            visa_addr = str(visa_addr).strip()
        if visa_addr is None or visa_addr.lower() == "auto":
            visa_addr = instr_disc.auto_detect_scope() or config.OSC_VISA_ADDR
        self.visa_addr = visa_addr
        self.timeout_ms = timeout_ms
        self._rm = None
        self._inst = None
        self._atexit_registered = False

    # ------------------------------------------------------------------
    def connect(self, _retry_with_auto_detect: bool = True) -> None:
        if self._inst is not None:
            return
        try:
            import pyvisa
        except ImportError as exc:
            raise ScopeError("缺少 pyvisa，请先: pip install pyvisa PyVISA-py") from exc
        try:
            self._rm = pyvisa.ResourceManager()
            # 优先在 open_resource 时传入 termination，某些 backend 对此更稳定
            try:
                self._inst = self._rm.open_resource(
                    self.visa_addr,
                    read_termination="\n",
                    write_termination="\n",
                )
            except Exception:
                self._inst = self._rm.open_resource(self.visa_addr)
                try:
                    self._inst.write_termination = "\n"
                except Exception:
                    pass
                try:
                    self._inst.read_termination = "\n"
                except Exception:
                    pass
            self._inst.timeout = self.timeout_ms
            try:
                self._inst.write("*CLS")
            except Exception:
                pass
            self._register_atexit()
        except Exception:
            # 如果指定地址连接失败，尝试自动识别一次再重连
            self.disconnect()
            if _retry_with_auto_detect and self.visa_addr and self.visa_addr.lower() not in ("auto", ""):
                try:
                    detected = instr_disc.auto_detect_scope()
                    if detected and detected != self.visa_addr:
                        self.visa_addr = detected
                        self.connect(_retry_with_auto_detect=False)
                        return
                except Exception:
                    pass
            raise

    def _register_atexit(self) -> None:
        if not self._atexit_registered:
            try:
                atexit.register(self.disconnect)
                self._atexit_registered = True
            except Exception:
                pass

    def disconnect(self) -> None:
        if self._inst is not None:
            try:
                # 关闭前恢复 RUN，避免示波器停留在 STOP 状态
                self._inst.write(":RUN")
            except Exception:
                pass
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
        if self._atexit_registered:
            try:
                atexit.unregister(self.disconnect)
            except Exception:
                pass
            self._atexit_registered = False

    def close(self) -> None:
        """disconnect() 的别名，方便统一使用 .close() 清理。"""
        self.disconnect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    def __del__(self):
        try:
            self.disconnect()
        except Exception:
            pass

    @property
    def is_connected(self) -> bool:
        return self._inst is not None

    def idn(self) -> str:
        self._require()
        return self._inst.query("*IDN?").strip()

    @property
    def is_tektronix(self) -> bool:
        """根据 *IDN? 判断是否为 Tektronix 示波器。"""
        try:
            return "TEKTRONIX" in self.idn().upper()
        except Exception:
            return False

    def _query_ascii(self, cmd: str, timeout_ms: int = None) -> str:
        """发送查询命令并返回 ASCII 响应。"""
        self._require()
        old_timeout = self._inst.timeout
        if timeout_ms is not None:
            self._inst.timeout = timeout_ms
        try:
            return self._inst.query(cmd).strip()
        finally:
            self._inst.timeout = old_timeout

    # ------------------------------------------------------------------
    def configure(self,
                  sample_rate: float = config.OSC_SAMPLE_RATE,
                  timebase_scale: float = config.OSC_TIMEBASE_SCALE) -> None:
        """配置采样率、时基、采集点数与波形格式。

        对每条 SCPI 命令单独容错：不同厂商（Keysight/Tektronix 等）支持的命令集合不同，
        非关键命令失败时继续尝试后续配置。
        """
        self._require()

        # 扩大 pyvisa 读写缓冲区，确保大波形能一次读完
        try:
            import pyvisa
            self._inst.set_buffer(pyvisa.constants.VI_READ_BUF, 40_000_000)
            self._inst.set_buffer(pyvisa.constants.VI_WRITE_BUF, 1_000_000)
        except Exception:
            pass

        def _write_safe(cmd: str) -> None:
            try:
                self._inst.write(cmd)
            except Exception:
                pass

        # Keysight 风格命令；Tektronix 等可能不支持，失败即跳过
        _write_safe(f":ACQUIRE:SRATE {sample_rate:.15g}")
        _write_safe(f":TIMEBASE:SCALE {timebase_scale:.15g}")
        _write_safe(f":HORIZONTAL:SAMPLERATE {sample_rate:.15g}")
        _write_safe(f":HORIZONTAL:SCALE {timebase_scale:.15g}")
        # 固定采集点数，避免返回扩展 preamble 或 x_increment=0
        _write_safe(":ACQUIRE:POINTS:AUTO OFF")
        _write_safe(":WAVEFORM:POINTS:MODE RAW")
        _write_safe(":WAVEFORM:POINTS 4000000")
        _write_safe(":WAVEFORM:FORMAT WORD")
        _write_safe(":WAVEFORM:BYTEORDER LSBFirst")
        # Tektronix 风格波形数据格式
        _write_safe(":DATA:ENCdg RIBINARY")
        _write_safe(":DATA:WIDTH 2")
        _write_safe(":WAVEFORM:ENCODING BINARY")
        _write_safe(":WAVEFORM:BYTEORDER LSBFIRST")

    def _alternate_channel(self, channel: str) -> str:
        """Keysight 用 CHANx，Tektronix 用 CHx，做一键互换。"""
        ch = channel.upper()
        if ch.startswith("CHAN"):
            return "CH" + ch[4:]
        if ch.startswith("CH") and not ch.startswith("CHA"):
            return "CHAN" + ch[2:]
        return channel

    def _read_preamble(self, channel: str, fallback: bool = True) -> dict:
        """读取并解析 preamble，兼容 Keysight 与 Tektronix 命令集。"""
        self._require()
        errors = []
        channels = (channel, self._alternate_channel(channel)) if fallback else (channel,)
        source_cmds = [":WAVEFORM:SOURCE", ":DATA:SOURCE"]
        # Tektronix 用 :WFMOUTPRE?，Keysight 用 :WAVEFORM:PREAMBLE?
        if self.is_tektronix:
            preamble_cmds = [":WFMOUTPRE?"]
        else:
            preamble_cmds = [":WAVEFORM:PREAMBLE?", ":WFMOUTPRE?"]
        for ch in channels:
            for src_cmd in source_cmds:
                try:
                    self._inst.write(f"{src_cmd} {ch}")
                except Exception as exc:
                    errors.append(f"{ch}/{src_cmd}: {exc}")
                    continue
                for cmd in preamble_cmds:
                    try:
                        preamble_str = self._query_ascii(cmd)
                        return self._parse_preamble(preamble_str)
                    except Exception as exc:
                        errors.append(f"{ch}/{cmd}: {exc}")
        raise ScopeError(f"无法读取通道 {channel} 的 preamble: {'; '.join(errors)}")

    @staticmethod
    def _parse_preamble(preamble_str: str) -> dict:
        """解析 Keysight 或 Tektronix 的 preamble，统一为 Keysight 风格。"""
        preamble_str = preamble_str.strip()
        if ";" in preamble_str:
            # Tektronix WFMOUTPRE/WFMPRE: 分号分隔，字段位置在不同子命令下略有差异。
            # 用 XUNIT="s"、YUNIT="V" 做锚点来定位，避免硬编码索引。
            parts = [p.strip() for p in preamble_str.split(";")]

            def _float_at(idx):
                if 0 <= idx < len(parts):
                    try:
                        return float(parts[idx])
                    except ValueError:
                        return 0.0
                return 0.0

            # 定位 XUNIT="s" 和 YUNIT="V"
            xunit_idx = next((i for i, p in enumerate(parts) if p.strip('"') == "s"), -1)
            yunit_idx = next((i for i, p in enumerate(parts) if p.strip('"') == "V"), -1)

            x_incr = _float_at(xunit_idx + 1) if xunit_idx >= 0 else 0.0
            x_origin = _float_at(xunit_idx + 2) if xunit_idx >= 0 else 0.0
            x_ref = _float_at(xunit_idx + 3) if xunit_idx >= 0 else 0.0

            y_mult = _float_at(yunit_idx + 1) if yunit_idx >= 0 else 0.0
            y_zero = _float_at(yunit_idx + 2) if yunit_idx >= 0 else 0.0
            y_off = _float_at(yunit_idx + 3) if yunit_idx >= 0 else 0.0

            # 字节序：找 MSB/LSB（通常在 WFID 字符串之前）
            byte_order = "MSB"
            for p in parts[:8]:
                p_clean = p.strip('"').upper()
                if p_clean in ("MSB", "LSB"):
                    byte_order = p_clean
                    break

            # 点数：取第一个遇到的较大整数值（NR_PT 通常 > 1000）
            points = 0
            for p in parts:
                try:
                    v = float(p)
                    if v > 1000:
                        points = int(v)
                        break
                except ValueError:
                    continue

            return {
                "format": 1,
                "type": 1,
                "points": points,
                "count": 1,
                "x_increment": x_incr,
                "x_origin": x_origin,
                "x_reference": x_ref,
                "y_increment": y_mult,
                "y_origin": y_zero,
                "y_reference": y_off,
                "byte_order": byte_order,
            }
        # Keysight 标准 10 字段逗号分隔
        parts = preamble_str.split(",")
        keys = [
            "format", "type", "points", "count",
            "x_increment", "x_origin", "x_reference",
            "y_increment", "y_origin", "y_reference",
        ]
        preamble = {}
        for k, v in zip(keys, parts):
            try:
                preamble[k] = float(v)
            except ValueError:
                preamble[k] = v
        preamble["byte_order"] = "LSB"  # Keysight 默认 LSBFirst
        return preamble

    def acquire(self, channel: str = config.OSC_CHANNEL) -> dict:
        """采集指定通道波形，返回含 YData（伏特）与 preamble 的字典。"""
        self._require()
        # 让示波器保持 RUN，直接读取当前波形缓冲区；不停止、不 digitize
        self._inst.write(":RUN")
        preamble = self._read_preamble(channel)

        required_keys = {"x_increment", "y_increment", "y_origin", "y_reference"}
        missing = required_keys - set(preamble.keys())
        if missing:
            raise ScopeError(f"preamble 缺少字段: {missing} (preamble={preamble})")

        x_incr = preamble.get("x_increment", 0)
        if x_incr == 0:
            raise ScopeError(
                f"示波器返回的 x_increment 为 0，通道 {channel} 未捕获到有效波形。"
                f"请检查通道是否已接信号、触发是否正常，以及时基/采样率设置是否合理。"
            )

        # 读取二进制波形数据；Tektronix 用 :CURVE?，Keysight 用 :WAV:DATA?
        is_big_endian = str(preamble.get("byte_order", "LSB")).upper() == "MSB"
        if self.is_tektronix:
            data_cmds = [":CURVE?"]
        else:
            data_cmds = [":WAV:DATA?", ":CURVE?"]
        raw = None
        last_exc = None
        for data_cmd in data_cmds:
            try:
                self._inst.write(data_cmd)
                raw = self._inst.read_binary_values(
                    datatype="h",
                    is_big_endian=is_big_endian,
                    header_fmt="ieee",
                    expect_termination=True,
                )
                break
            except Exception as exc:
                last_exc = exc
        if raw is None:
            raise ScopeError(f"读取波形数据失败: {last_exc}") from last_exc

        # 某些固件会在 binblock 后再跟一个换行符；read_binary_values 已处理大部分情况，
        # 残留则忽略
        try:
            self._inst.read_bytes(1)
        except Exception:
            pass

        raw_i16 = np.asarray(raw, dtype=np.int16)
        y_incr = preamble["y_increment"]
        y_orig = preamble["y_origin"]
        y_ref = preamble["y_reference"]
        ydata = y_incr * (raw_i16 - y_ref) + y_orig

        return {
            "channel": channel,
            "preamble": preamble,
            "raw": raw_i16,
            "ydata": ydata,
        }

    def acquire_dual(self, channels: Tuple[str, str] = ("CHAN1", "CHAN2")) -> dict:
        """同时采集两个通道，返回各自波形以及两路时域相加结果。

        保持示波器在 RUN 状态，分别读取每个通道的 preamble 与波形数据。
        两路长度不一致时取最短长度相加。
        """
        self._require()
        results = {}
        for ch in channels:
            results[ch] = self.acquire(ch)

        ydata_list = [results[ch]["ydata"] for ch in channels]
        min_len = min(len(y) for y in ydata_list)
        ydata_sum = np.sum([y[:min_len] for y in ydata_list], axis=0)

        return {
            "channels": channels,
            "results": results,
            "ydata": ydata_sum,
            "ydata_sum": ydata_sum,
            "preamble": results[channels[0]]["preamble"],
        }

    # ------------------------------------------------------------------
    def _require(self) -> None:
        if self._inst is None:
            raise ScopeError("示波器未连接")


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


def acquire_two_channels(visa_addr: str = config.OSC_VISA_ADDR,
                         channels: Tuple[str, str] = ("CHAN1", "CHAN2"),
                         sample_rate: float = config.OSC_SAMPLE_RATE,
                         timebase_scale: float = config.OSC_TIMEBASE_SCALE) -> dict:
    """一次性连接 -> 配置 -> 双通道采集 -> 断开，返回 acquire_dual() 的字典。"""
    scope = KeysightScope(visa_addr)
    try:
        scope.connect()
        scope.configure(sample_rate, timebase_scale)
        return scope.acquire_dual(channels)
    finally:
        scope.disconnect()
