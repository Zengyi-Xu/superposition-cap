# -*- coding: utf-8 -*-
"""Keithley 2400 SourceMeter control wrapper (RS-232 / GPIB).

This module ports the Keithley 2400 driver from the IVLab/DMT-NN project into
the superposition-cap experiment platform. It supports two instrument
communication interfaces:

    * RS-232 / USB-to-RS232  : classical serial (COM) port.
    * GPIB (IEEE-488)        : commonly used with NI GPIB-USB-HS adapters
                               and linux-gpib / NI-VISA backends.

The public entry point is the ``Keithley2400`` class. Its ``interface``
argument selects the transport. All SCPI command methods are shared, so
calling code does not need to know which transport is in use.

Typical usage::

    from keithley2400_controller import Keithley2400

    # RS-232 example
    k = Keithley2400(port="COM3", baudrate=9600, interface="rs232")

    # GPIB example (port can be an address or a full resource string)
    k = Keithley2400(port=22, interface="gpib")
    k = Keithley2400(port="GPIB0::22::INSTR", interface="gpib")

    k.connect()
    k.set_source_mode("voltage")      # voltage source
    k.set_compliance(0.1)             # 100 mA current limit
    k.set_output_level(1.0)           # 1 V
    k.output_on()
    print(k.measure())
    k.output_off()
    k.disconnect()

Note on "GPIO"
-------------
Some users colloquially refer to the GPIB/IEEE-488 port as "GPIO". For
robustness this driver accepts ``interface="gpio"`` and treats it as
``"gpib"``, logging a clarification message. The Keithley 2400 does not
provide a general-purpose GPIO data bus for SCPI communication; if you are
using the 6-pin Digital I/O port for triggering only, that is outside the
scope of this driver.
"""
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

try:
    import serial
    import serial.tools.list_ports
except Exception:  # pragma: no cover
    serial = None  # type: ignore


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class K2400Error(Exception):
    """Base exception for Keithley 2400 operations."""
    pass


class K2400ConnectionError(K2400Error):
    """Raised when the instrument connection cannot be established."""
    pass


class K2400CommandError(K2400Error):
    """Raised when a command returns an unexpected response."""
    pass


class K2400ConfigError(K2400Error):
    """Raised when an invalid configuration value is supplied."""
    pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logger(name: str = "k2400", level: int = logging.DEBUG) -> logging.Logger:
    """Configure a logger writing to the project log directory."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File (inside project log/)
    log_dir = Path(__file__).resolve().parent / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / "keithley2400.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Interface normalisation
# ---------------------------------------------------------------------------
_SUPPORTED_INTERFACES = ("rs232", "gpib")


def _normalize_interface(interface: str) -> str:
    """Validate and normalise the communication interface name.

    ``"gpio"`` is accepted as a user-friendly alias for GPIB because the two
    abbreviations are often confused, but a clarification message is logged.
    """
    value = str(interface).strip().lower().replace("-", "").replace(" ", "")
    if value == "usb":
        value = "rs232"  # USB-to-RS232 adapters are still serial transports
    if value == "gpio":
        logger = logging.getLogger("k2400")
        logger.warning(
            '[K2400] interface="gpio" 被识别为 GPIB (IEEE-488)。'
            'Keithley 2400 的 SCPI 通信通常使用 RS-232 或 GPIB，'
            '如需 Digital I/O 触发请使用其它专用代码。'
        )
        value = "gpib"
    if value not in _SUPPORTED_INTERFACES:
        raise K2400ConfigError(
            f"不支持的通信接口: {interface!r}。请使用 "
            f"'rs232' (串口/USB-RS232) 或 'gpib' (IEEE-488/GPIB)。"
        )
    return value


# ---------------------------------------------------------------------------
# Port / resource discovery
# ---------------------------------------------------------------------------
def _check_serial_available() -> None:
    if serial is None:
        raise ImportError(
            "使用 RS-232 接口需要 pyserial。安装命令：pip install pyserial>=3.5"
        )


def _check_pyvisa_available():
    """Lazy pyvisa importer so the module can be imported without pyvisa."""
    try:
        import pyvisa
        return pyvisa
    except Exception as exc:  # pragma: no cover
        raise ImportError(
            "使用 GPIB 接口需要 pyvisa。安装命令：pip install pyvisa>=1.13"
        ) from exc


def list_com_ports() -> List[Dict]:
    """Return a list of available COM ports with metadata."""
    _check_serial_available()
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "port": p.device,
            "description": p.description,
            "hwid": p.hwid,
            "vid": p.vid,
            "pid": p.pid,
        })
    return ports


def find_instrument_ports(keywords: Optional[List[str]] = None) -> List[Dict]:
    """Find COM ports whose description matches common USB-serial adapter names."""
    _check_serial_available()
    if keywords is None:
        keywords = ["USB Serial", "FTDI", "Prolific", "CH340", "CP210", "Keithley"]
    all_ports = list_com_ports()
    matches = []
    for p in all_ports:
        desc = p["description"].upper()
        if any(kw.upper() in desc for kw in keywords):
            matches.append(p)
    return matches


def list_gpib_resources() -> List[str]:
    """Return a list of GPIB resource strings found by PyVISA."""
    pyvisa = _check_pyvisa_available()
    try:
        rm = pyvisa.ResourceManager()
        resources = list(rm.list_resources())
        rm.close()
    except Exception as exc:
        raise K2400ConnectionError(f"枚举 GPIB 资源失败: {exc}") from exc
    return [r for r in resources if r.upper().startswith("GPIB")]


def _build_gpib_resource(port: Union[str, int]) -> str:
    """Convert a user-supplied GPIB identifier into a full VISA resource string."""
    if isinstance(port, str):
        s = port.strip()
        if "::" in s.upper():
            return s
        try:
            addr = int(s)
        except ValueError:
            raise K2400ConfigError(
                f"GPIB 端口/地址格式无效: {port!r}。请填写主地址数字（如 22）"
                f"或完整资源字符串（如 GPIB0::22::INSTR）。"
            )
    else:
        addr = int(port)
    return f"GPIB0::{addr}::INSTR"


# ---------------------------------------------------------------------------
# Common SCPI command logic
# ---------------------------------------------------------------------------
class _Keithley2400Common:
    """Shared SCPI command set and instrument state.

    Transport subclasses must implement ``write``, ``read_line``, ``connect``
    and ``disconnect``.
    """

    def __init__(self, timeout: float, logger: Optional[logging.Logger]):
        self.timeout = timeout
        self.connected = False
        self._source_mode = "voltage"   # "voltage" or "current"
        self._measure_func = "current"  # "current" or "voltage"
        self._debug = False
        self.logger = logger or _setup_logger("k2400")

    # Transport interface (implemented by subclasses)
    def write(self, cmd: str):
        raise NotImplementedError

    def read_line(self) -> str:
        raise NotImplementedError

    # Shared helpers
    def query(self, cmd: str) -> str:
        self.write(cmd)
        time.sleep(0.02)
        return self.read_line()

    def reset(self):
        self.write("*RST")
        self.logger.info("[K2400] 仪器复位")

    def idn(self) -> str:
        """Query the instrument identification string."""
        return self.query("*IDN?")

    # Source / measure configuration
    def set_source_mode(self, mode: str):
        """设置源模式，测量功能自动切换为互补端（电压源测电流，电流源测电压）。"""
        mode = mode.lower()
        if mode == "voltage":
            self.write(":SOUR:FUNC VOLT")
            self._source_mode = "voltage"
            self._measure_func = "current"
        elif mode == "current":
            self.write(":SOUR:FUNC CURR")
            self._source_mode = "current"
            self._measure_func = "voltage"
        else:
            raise K2400ConfigError(f"不支持的源模式: {mode}")
        self.write(f':SENS:FUNC "{self._measure_func.upper()}"')
        self.logger.info(f"[K2400] 源模式设为 {mode}")

    def get_source_mode(self) -> str:
        return self._source_mode

    def set_measure_function(self, func: str):
        """设置测量功能（必须与源模式互补，不允许测量与源相同的物理量）。"""
        func = func.lower()
        if func == self._source_mode:
            raise K2400ConfigError(
                f"测量功能不能等于源模式: 当前为{self._source_mode}源，"
                f"只能测量{'电压' if self._source_mode == 'current' else '电流'}"
            )
        if func == "current":
            self.write(':SENS:FUNC "CURR"')
            self._measure_func = "current"
        elif func == "voltage":
            self.write(':SENS:FUNC "VOLT"')
            self._measure_func = "voltage"
        else:
            raise K2400ConfigError(f"不支持的测量功能: {func}")

    def set_compliance(self, value: float):
        if self._source_mode == "voltage":
            self.write(f":SENS:CURR:PROT {value}")
        else:
            self.write(f":SENS:VOLT:PROT {value}")
        self.logger.info(f"[K2400] 合规限值设为 {value}")

    def set_nplc(self, nplc: float):
        func = "CURR" if self._measure_func == "current" else "VOLT"
        self.write(f":SENS:{func}:NPLC {nplc}")
        self.logger.info(f"[K2400] NPLC设为 {nplc}")

    def set_range(self, auto: bool = True, fixed_value: Optional[float] = None):
        func = "CURR" if self._measure_func == "current" else "VOLT"
        if auto:
            self.write(f":SENS:{func}:RANG:AUTO ON")
        elif fixed_value is not None:
            self.write(f":SENS:{func}:RANG {fixed_value}")
            self.write(f":SENS:{func}:RANG:AUTO OFF")

    # Output control
    def set_output_level(self, level: float):
        if self._source_mode == "voltage":
            self.write(f":SOUR:VOLT:LEV {level}")
        else:
            self.write(f":SOUR:CURR:LEV {level}")
        self.logger.info(f"[K2400] 输出电平设为 {level}")

    def output_on(self):
        self.write(":OUTP ON")
        self.logger.info("[K2400] 输出开启")

    def output_off(self):
        self.write(":OUTP OFF")
        self.logger.info("[K2400] 输出关闭")

    def measure(self) -> Dict[str, float]:
        resp = self.query(":READ?")
        parts = resp.split(",")
        if len(parts) >= 4:
            return {
                "voltage": float(parts[0]),
                "current": float(parts[1]),
                "resistance": float(parts[2]),
                "timestamp": float(parts[3]),
                "status": parts[4] if len(parts) > 4 else "",
            }
        raise K2400CommandError(f"测量返回格式异常: {resp}")

    def check_errors(self) -> List[str]:
        errors = []
        for _ in range(10):
            resp = self.query(":SYST:ERR?")
            if "+0" in resp or "No error" in resp:
                break
            errors.append(resp)
        return errors


# ---------------------------------------------------------------------------
# RS-232 backend
# ---------------------------------------------------------------------------
class _Keithley2400Serial(_Keithley2400Common):
    """Keithley 2400 controlled over RS-232 or USB-to-RS232."""

    def __init__(self, port: str, baudrate: int, timeout: float,
                 logger: Optional[logging.Logger]):
        super().__init__(timeout, logger)
        self.port = port
        self.baudrate = baudrate
        self.ser: Optional[serial.Serial] = None

    def connect(self, retries: int = 3) -> bool:
        _check_serial_available()
        for attempt in range(1, retries + 1):
            try:
                self.logger.info(f"[K2400] 尝试 RS-232 连接 {self.port} (第{attempt}次)")
                self.ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=self.timeout,
                    write_timeout=self.timeout,
                    xonxoff=False,
                    rtscts=False,
                )
                # Keithley RS-232 口依赖 RTS/CTS、DTR/DSR 握手，
                # 必须显式拉高，否则仪器不发送数据
                self.ser.setRTS(True)
                self.ser.setDTR(True)
                time.sleep(0.5)
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
                self.write(":SYST:BEEP:STAT OFF")
                self.connected = True
                idn = self.idn()
                self.logger.info(f"[K2400] RS-232 连接成功，IDN={idn.strip()}")
                return True
            except Exception as exc:
                self.logger.warning(f"[K2400] 连接尝试 {attempt} 失败: {exc}")
                if attempt < retries:
                    time.sleep(0.5)
                else:
                    self.connected = False
                    raise K2400ConnectionError(
                        f"无法通过 RS-232 连接 Keithley 2400 "
                        f"(端口={self.port}, 波特率={self.baudrate})。\n"
                        f"原始错误: {exc}\n"
                        f"常见原因：1) 串口号错误；2) 线缆未接好；3) 仪器未开机；"
                        f"4) 仪器通信接口未设为 RS-232。"
                    ) from exc
        return False

    def disconnect(self):
        if self.ser is not None:
            try:
                if self.ser.is_open:
                    try:
                        self.write(":OUTP OFF")
                    except Exception:
                        pass
                    self.ser.close()
            except Exception as e:
                self.logger.warning(f"[K2400] 关闭串口时警告: {e}")
            finally:
                self.ser = None
        self.connected = False
        self.logger.info("[K2400] RS-232 已断开")

    def write(self, cmd: str):
        if self.ser is None or not self.ser.is_open:
            raise K2400ConnectionError("RS-232 串口未打开")
        data = (cmd + "\n").encode("ascii")
        self.ser.write(data)
        if self._debug:
            self.logger.debug(f">>> {cmd}")

    def query(self, cmd: str) -> str:
        """发送指令并读取响应，自动跳过命令回显。"""
        self.write(cmd)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise K2400ConnectionError("RS-232 query 超时")
            resp = self.read_line(timeout=remaining)
            if resp.strip() == cmd.strip():
                continue
            return resp

    def read_line(self, timeout: Optional[float] = None) -> str:
        if self.ser is None or not self.ser.is_open:
            raise K2400ConnectionError("RS-232 串口未打开")
        old_timeout = self.ser.timeout
        if timeout is not None:
            self.ser.timeout = timeout
        try:
            raw = self.ser.readline()
            if not raw:
                raise K2400ConnectionError("RS-232 读取超时，请检查端口和波特率")
            line = raw.decode("ascii", errors="replace").strip()
            if self._debug:
                self.logger.debug(f"<<< {line}")
            return line
        finally:
            if timeout is not None:
                self.ser.timeout = old_timeout


# ---------------------------------------------------------------------------
# GPIB backend
# ---------------------------------------------------------------------------
class _Keithley2400GPIB(_Keithley2400Common):
    """Keithley 2400 controlled over GPIB (IEEE-488) via PyVISA."""

    def __init__(self, port: Union[str, int], timeout: float,
                 logger: Optional[logging.Logger]):
        super().__init__(timeout, logger)
        self.port = str(port)
        self.gpib_resource = _build_gpib_resource(port)
        self._rm = None
        self.inst = None

    def connect(self, retries: int = 3) -> bool:
        pyvisa = _check_pyvisa_available()
        for attempt in range(1, retries + 1):
            try:
                self.logger.info(
                    f"[K2400] 尝试 GPIB 连接 {self.gpib_resource} (第{attempt}次)"
                )
                self._rm = pyvisa.ResourceManager()
                self.inst = self._rm.open_resource(self.gpib_resource)
                self.inst.timeout = int(self.timeout * 1000)
                self.inst.write_termination = "\n"
                self.inst.read_termination = "\n"
                self.write(":SYST:BEEP:STAT OFF")
                self.connected = True
                idn = self.idn()
                self.logger.info(f"[K2400] GPIB 连接成功，IDN={idn.strip()}")
                return True
            except Exception as exc:
                self.logger.warning(f"[K2400] 连接尝试 {attempt} 失败: {exc}")
                if attempt < retries:
                    time.sleep(0.5)
                else:
                    self.connected = False
                    self._cleanup_resources()
                    raise K2400ConnectionError(
                        f"无法通过 GPIB 连接 Keithley 2400 "
                        f"(资源={self.gpib_resource})。\n"
                        f"原始错误: {exc}\n"
                        f"常见原因：1) GPIB 地址错误；2) 线缆未接好；"
                        f"3) 仪器未开机或地址不匹配；4) linux-gpib/NI-VISA 未安装；"
                        f"5) 当前用户无 /dev/gpib* 访问权限。"
                    ) from exc
        return False

    def disconnect(self):
        if self.inst is not None:
            try:
                try:
                    self.write(":OUTP OFF")
                except Exception:
                    pass
                self.inst.close()
            except Exception as e:
                self.logger.warning(f"[K2400] 关闭 GPIB 资源时警告: {e}")
            finally:
                self.inst = None
        self._cleanup_resources()
        self.connected = False
        self.logger.info("[K2400] GPIB 已断开")

    def _cleanup_resources(self):
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            self._rm = None

    def write(self, cmd: str):
        if self.inst is None:
            raise K2400ConnectionError("GPIB 资源未打开")
        self.inst.write(cmd)
        if self._debug:
            self.logger.debug(f">>> {cmd}")

    def read_line(self) -> str:
        if self.inst is None:
            raise K2400ConnectionError("GPIB 资源未打开")
        line = self.inst.read().strip()
        if self._debug:
            self.logger.debug(f"<<< {line}")
        return line


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------
class Keithley2400:
    """Public facade that selects RS-232 or GPIB transport automatically.

    Parameters
    ----------
    port : str or int
        RS-232: COM port name, e.g. "COM3" or "/dev/ttyUSB0".
        GPIB: primary address (int or str, e.g. 22) or full resource string
        (e.g. "GPIB0::22::INSTR").
    baudrate : int
        RS-232 baud rate. Ignored for GPIB.
    timeout : float
        Communication timeout in seconds.
    interface : {"rs232", "gpib", "gpio"}
        Communication interface. ``"gpio"`` is accepted as an alias for
        ``"gpib"`` and a clarification is logged.
    logger : logging.Logger, optional
        Custom logger instance.
    """

    def __init__(self, port: Union[str, int] = "COM1", baudrate: int = 9600,
                 timeout: float = 5.0, interface: str = "rs232",
                 logger: Optional[logging.Logger] = None):
        self.interface = _normalize_interface(interface)
        if self.interface == "rs232":
            self._backend: _Keithley2400Common = _Keithley2400Serial(
                port=str(port), baudrate=baudrate, timeout=timeout, logger=logger
            )
        else:  # gpib
            self._backend = _Keithley2400GPIB(
                port=port, timeout=timeout, logger=logger
            )

    @property
    def connected(self) -> bool:
        return self._backend.connected

    @property
    def port(self) -> str:
        return self._backend.port

    @property
    def baudrate(self) -> Optional[int]:
        return getattr(self._backend, "baudrate", None)

    def __getattr__(self, name: str):
        return getattr(self._backend, name)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


# ---------------------------------------------------------------------------
# Convenience helpers for the GUI
# ---------------------------------------------------------------------------
def refresh_serial_port_list() -> List[str]:
    """Return a list of strings like 'COM3 - USB-SERIAL CH340'."""
    items = []
    for p in list_com_ports():
        desc = p["description"] or "Unknown"
        items.append(f"{p['port']} - {desc}")
    return items


def refresh_gpib_resource_list() -> List[str]:
    """Return a list of GPIB resource strings with a description prefix."""
    resources = list_gpib_resources()
    return [f"{r} - GPIB" for r in resources]


def refresh_port_list(interface: str = "rs232") -> List[str]:
    """Return available ports/resources for the selected interface."""
    interface = _normalize_interface(interface)
    if interface == "gpib":
        return refresh_gpib_resource_list()
    return refresh_serial_port_list()


def parse_port_entry(entry: str) -> str:
    """Extract the port name or GPIB resource from a dropdown string."""
    entry = entry.strip()
    if entry.upper().startswith("GPIB"):
        # "GPIB0::22::INSTR - GPIB" -> "GPIB0::22::INSTR"
        return entry.split(" - ", 1)[0].strip()
    return entry.split(" - ", 1)[0].strip() if " - " in entry else entry
