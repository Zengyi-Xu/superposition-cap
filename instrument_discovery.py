"""VISA 仪器自动发现工具。

提供示波器（Keysight/Agilent）与 AWG（Tektronix AWG520 等）的自动识别，
支持 GPIB / USB / TCPIP / ASRL 等多种 VISA 资源类型。
"""
import json
import subprocess
import sys
from typing import List, Optional, Tuple

import pyvisa


# list_resources() 在某些系统（PyVISA-py + TCPIP 扫描）下可能挂起，
# 因此用独立子进程加超时保护。
_RESOURCE_DISCOVERY_TIMEOUT = 6.0  # 秒


# 厂商关键字 / ID（用于资源字符串或 *IDN? 返回）
SCOPE_KEYWORDS = ["KEYSIGHT", "AGILENT", "TEKTRONIX", "RIGOL", "SIGLENT"]
AWG_KEYWORDS = ["TEKTRONIX", "AWG", "M8190", "M8195", "M9336"]


def _list_resources_for_backend(backend: Optional[str]) -> List[str]:
    """单个 backend 的资源枚举；在独立子进程中运行以便加超时。"""
    rm = pyvisa.ResourceManager(backend)
    try:
        return list(rm.list_resources())
    finally:
        try:
            rm.close()
        except Exception:
            pass


def _subprocess_list_resources(backend: Optional[str]) -> List[str]:
    """启动独立 Python 子进程枚举资源；父进程带超时等待。"""
    cmd = [
        sys.executable, "-c",
        "import json, pyvisa; "
        "be = __import__('sys').argv[1] or None; "
        "rm = pyvisa.ResourceManager(be); "
        "print(json.dumps(list(rm.list_resources()))); "
        "rm.close()",
        backend if backend else "",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8")
    try:
        stdout, stderr = proc.communicate(timeout=_RESOURCE_DISCOVERY_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError(f"backend {backend!r} 资源枚举超时（"
                           f"{_RESOURCE_DISCOVERY_TIMEOUT}s）")
    if proc.returncode != 0:
        raise RuntimeError(stderr.strip() or "子进程枚举资源失败")
    return json.loads(stdout.strip() or "[]")


def list_visa_resources(backend: Optional[str] = None,
                        timeout: float = _RESOURCE_DISCOVERY_TIMEOUT) -> List[str]:
    """列出当前系统可用的所有 VISA 资源字符串（带超时保护）。"""
    backends = [backend] if backend else ["@py", ""]
    last_exc = None
    for be in backends:
        try:
            return _subprocess_list_resources(be if be else None)
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"无法枚举 VISA 资源: {last_exc}") from last_exc


def _parse_idn(idn: str) -> Tuple[str, str, str, str]:
    """解析 *IDN? 返回字符串：厂商, 型号, 序列号, 固件版本。"""
    parts = [p.strip() for p in idn.split(",")]
    while len(parts) < 4:
        parts.append("")
    return tuple(parts[:4])


def _resource_type(addr: str) -> str:
    """根据 VISA 地址前缀判断接口类型。"""
    addr_u = addr.upper()
    if addr_u.startswith("GPIB"):
        return "GPIB"
    if addr_u.startswith("TCPIP"):
        return "TCPIP"
    if addr_u.startswith("USB"):
        return "USB"
    if addr_u.startswith("ASRL"):
        return "SERIAL"
    return "OTHER"


def query_idn(addr: str, timeout_ms: int = 3000) -> Optional[str]:
    """尝试连接指定地址并查询 *IDN?，失败返回 None（带超时保护）。"""
    cmd = [
        sys.executable, "-c",
        "import pyvisa, json, sys; "
        "addr = sys.argv[1]; timeout = int(sys.argv[2]); "
        "rm = pyvisa.ResourceManager(); "
        "inst = rm.open_resource(addr); "
        "inst.timeout = timeout; "
        "print(json.dumps(inst.query('*IDN?').strip())); "
        "inst.close(); rm.close()",
        addr, str(timeout_ms),
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8")
    try:
        stdout, _ = proc.communicate(timeout=(timeout_ms / 1000.0 + 1.0))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(stdout.strip())
    except Exception:
        return None


def identify_resource(addr: str, timeout_ms: int = 1500) -> Optional[dict]:
    """识别单个 VISA 资源，返回包含地址、类型、*IDN? 信息的字典。"""
    idn = query_idn(addr, timeout_ms)
    if idn is None:
        return None
    vendor, model, serial, version = _parse_idn(idn)
    return {
        "address": addr,
        "interface": _resource_type(addr),
        "idn": idn,
        "vendor": vendor,
        "model": model,
        "serial": serial,
        "version": version,
    }


def scan_instruments(timeout_ms: int = 1500,
                       max_workers: int = 4,
                       include_asrl: bool = True) -> List[dict]:
    """扫描所有 VISA 资源并尝试识别，返回可连接仪器的列表（并行识别）。

    Parameters
    ----------
    include_asrl : bool
        是否包含串口（ASRL）资源。自动识别时设为 False 可显著加快速度。
    """
    from concurrent.futures import ThreadPoolExecutor

    resources = list_visa_resources()
    if not include_asrl:
        resources = [r for r in resources if not r.upper().startswith("ASRL")]
    found = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(identify_resource, addr, timeout_ms)
                   for addr in resources]
        for future in futures:
            info = future.result()
            if info is not None:
                found.append(info)
    return found


def detect_scope_candidates(timeout_ms: int = 1500,
                            include_asrl: bool = False) -> List[dict]:
    """返回识别到的示波器候选列表。"""
    candidates = []
    for info in scan_instruments(timeout_ms, include_asrl=include_asrl):
        text = f"{info['vendor']} {info['model']}".upper()
        if any(kw in text for kw in SCOPE_KEYWORDS):
            candidates.append(info)
    return candidates


def detect_awg_candidates(timeout_ms: int = 1500,
                          include_asrl: bool = False) -> List[dict]:
    """返回识别到的 AWG 候选列表。"""
    candidates = []
    for info in scan_instruments(timeout_ms, include_asrl=include_asrl):
        text = f"{info['vendor']} {info['model']}".upper()
        if any(kw in text for kw in AWG_KEYWORDS):
            candidates.append(info)
    return candidates


def auto_detect_scope(timeout_ms: int = 1500,
                      include_asrl: bool = False) -> Optional[str]:
    """自动识别示波器地址，返回最佳候选地址；未找到返回 None。"""
    candidates = detect_scope_candidates(timeout_ms, include_asrl=include_asrl)
    if not candidates:
        return None
    # 优先级：TCPIP > USB > GPIB > SERIAL
    priority = {"TCPIP": 0, "USB": 1, "GPIB": 2, "SERIAL": 3, "OTHER": 4}
    candidates.sort(key=lambda x: priority.get(x["interface"], 99))
    return candidates[0]["address"]


def auto_detect_awg(timeout_ms: int = 1500,
                    include_asrl: bool = False) -> Optional[str]:
    """自动识别 AWG 地址，返回最佳候选地址；未找到返回 None。"""
    candidates = detect_awg_candidates(timeout_ms, include_asrl=include_asrl)
    if not candidates:
        return None
    # 对 AWG520 优先 GPIB；其它优先 TCPIP/USB
    def _sort_key(info):
        interface = info["interface"]
        if "AWG520" in info["model"].upper() and interface == "GPIB":
            return 0
        priority = {"GPIB": 1, "TCPIP": 2, "USB": 3, "SERIAL": 4, "OTHER": 5}
        return priority.get(interface, 99)
    candidates.sort(key=_sort_key)
    return candidates[0]["address"]


def format_instrument_list(instruments: List[dict]) -> str:
    """把识别结果格式化为可读的文本。"""
    if not instruments:
        return "未识别到任何仪器。"
    lines = []
    for info in instruments:
        lines.append(
            f"[{info['interface']}] {info['address']}\n"
            f"    {info['idn']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    print("正在扫描 VISA 资源...")
    try:
        resources = list_visa_resources()
        print(f"发现 {len(resources)} 个资源:")
        for r in resources:
            print(f"  {r}")
        print()
        print("正在识别仪器...")
        instruments = scan_instruments()
        print(format_instrument_list(instruments))
        print()
        scopes = detect_scope_candidates()
        awgs = detect_awg_candidates()
        print(f"示波器候选: {len(scopes)} 个")
        print(f"AWG 候选:   {len(awgs)} 个")
    except Exception as exc:
        print(f"扫描失败: {exc}")
