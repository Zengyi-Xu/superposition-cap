"""VISA 仪器自动发现工具。

提供示波器（Keysight/Agilent）与 AWG（Tektronix AWG520 等）的自动识别，
支持 GPIB / USB / TCPIP / ASRL 等多种 VISA 资源类型。
"""
import json
import subprocess
import sys
from typing import List, Optional, Tuple

import pyvisa

import config as cfg


# list_resources() 在某些系统（PyVISA-py + TCPIP 扫描）下可能挂起，
# 因此用独立子进程加超时保护。
_RESOURCE_DISCOVERY_TIMEOUT = 3.0  # 秒


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


def _configured_addresses() -> List[str]:
    """返回配置文件中硬编码的示波器 / AWG 地址，作为自动识别的保底候选。"""
    addrs = []
    for addr in (cfg.OSC_VISA_ADDR, cfg.AWG_VISA_ADDR):
        if addr and isinstance(addr, str):
            addrs.append(addr)
    return addrs


def _is_scope(info: dict) -> bool:
    text = f"{info['vendor']} {info['model']}".upper()
    return any(kw in text for kw in SCOPE_KEYWORDS)


def _is_awg(info: dict) -> bool:
    text = f"{info['vendor']} {info['model']}".upper()
    return any(kw in text for kw in AWG_KEYWORDS)


def _is_reachable(addr: str, timeout: float = 0.3) -> bool:
    """对 TCPIP 地址做快速 TCP 连通性探测，避免 VISA 打开不可达地址时长时间挂起。"""
    if not addr.upper().startswith("TCPIP"):
        return True
    try:
        parts = addr.split("::")
        # TCPIP[board]::host::port::SOCKET
        if len(parts) >= 4:
            host = parts[1]
            port = int(parts[2])
            import socket
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
    except Exception:
        pass
    return False


def _quick_identify(addrs: List[str], timeout_ms: int = 800) -> List[dict]:
    """对给定地址做快速识别，用于优先确认用户已填写/配置的地址。"""
    from concurrent.futures import ThreadPoolExecutor

    addrs = [a for a in addrs if a and _is_reachable(a)]
    if not addrs:
        return []
    found = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(identify_resource, addr, timeout_ms)
                   for addr in addrs]
        for future in futures:
            info = future.result()
            if info is not None:
                found.append(info)
    return found


def scan_instruments(timeout_ms: int = 1000,
                       max_workers: int = 4,
                       include_asrl: bool = False,
                       extra_addrs: Optional[List[str]] = None,
                       include_configured: bool = True) -> List[dict]:
    """扫描所有 VISA 资源并尝试识别，返回可连接仪器的列表（并行识别）。

    Parameters
    ----------
    include_asrl : bool
        是否包含串口（ASRL）资源。自动识别时默认排除，可显著加快速度。
    extra_addrs : list[str] | None
        额外需要尝试的 VISA 地址（例如用户当前在 GUI 输入框中填写的地址）。
    include_configured : bool
        是否把 config.py 中硬编码的 OSC/AWG 地址加入候选。
    """
    from concurrent.futures import ThreadPoolExecutor

    resources = list_visa_resources()

    # 把配置地址和 GUI 传入的地址也加入候选，避免 pyvisa 枚举不到 TCPIP 设备
    extras = list(extra_addrs or [])
    if include_configured:
        extras += _configured_addresses()
    seen = set(resources)
    for addr in extras:
        if addr and addr not in seen:
            resources.append(addr)
            seen.add(addr)

    # 默认跳过串口，避免打开不存在的 COM 口导致超时
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


def detect_scope_candidates(timeout_ms: int = 1000,
                            include_asrl: bool = False,
                            extra_addrs: Optional[List[str]] = None) -> List[dict]:
    """返回识别到的示波器候选列表。"""
    # 快速路径：优先尝试用户输入的地址 + 配置文件地址
    quick_addrs = list(extra_addrs or []) + _configured_addresses()
    quick_candidates = [info for info in _quick_identify(quick_addrs, timeout_ms=min(800, timeout_ms))
                        if _is_scope(info)]
    if quick_candidates:
        return quick_candidates

    # 兜底路径：完整扫描系统资源
    candidates = []
    for info in scan_instruments(timeout_ms, include_asrl=include_asrl,
                                  extra_addrs=extra_addrs,
                                  include_configured=False):
        if _is_scope(info):
            candidates.append(info)
    return candidates


def detect_awg_candidates(timeout_ms: int = 1000,
                          include_asrl: bool = False,
                          extra_addrs: Optional[List[str]] = None) -> List[dict]:
    """返回识别到的 AWG 候选列表。"""
    quick_addrs = list(extra_addrs or []) + _configured_addresses()
    quick_candidates = [info for info in _quick_identify(quick_addrs, timeout_ms=min(800, timeout_ms))
                        if _is_awg(info)]
    if quick_candidates:
        return quick_candidates

    candidates = []
    for info in scan_instruments(timeout_ms, include_asrl=include_asrl,
                                  extra_addrs=extra_addrs,
                                  include_configured=False):
        if _is_awg(info):
            candidates.append(info)
    return candidates


def auto_detect_scope(timeout_ms: int = 1000,
                      include_asrl: bool = False,
                      extra_addrs: Optional[List[str]] = None) -> Optional[str]:
    """自动识别示波器地址，返回最佳候选地址；未找到返回 None。"""
    candidates = detect_scope_candidates(timeout_ms, include_asrl=include_asrl,
                                          extra_addrs=extra_addrs)
    if not candidates:
        return None
    # 优先级：TCPIP > USB > GPIB > SERIAL
    priority = {"TCPIP": 0, "USB": 1, "GPIB": 2, "SERIAL": 3, "OTHER": 4}
    candidates.sort(key=lambda x: priority.get(x["interface"], 99))
    return candidates[0]["address"]


def auto_detect_awg(timeout_ms: int = 1000,
                    include_asrl: bool = False,
                    extra_addrs: Optional[List[str]] = None) -> Optional[str]:
    """自动识别 AWG 地址，返回最佳候选地址；未找到返回 None。"""
    candidates = detect_awg_candidates(timeout_ms, include_asrl=include_asrl,
                                        extra_addrs=extra_addrs)
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
