"""最小化示波器测试。"""
import pyvisa
from oscilloscope import KeysightScope

addr = "USB::0x0699::0x0105::C012762::INSTR"
scope = KeysightScope(addr, timeout_ms=10000)
scope.connect()
print("IDN:", scope.idn())
scope.configure()
print("Configured")

# 手动测试 preamble 读取
for ch in ["CH1", "CH2"]:
    print(f"\n--- Testing {ch} ---")
    for src_cmd in [":WAVEFORM:SOURCE", ":DATA:SOURCE"]:
        try:
            scope._inst.write(f"{src_cmd} {ch}")
            print(f"{src_cmd} {ch}: OK")
        except Exception as e:
            print(f"{src_cmd} {ch}: {e}")
            continue
        for pre_cmd in [":WAVEFORM:PREAMBLE?", ":WFMOUTPRE?"]:
            try:
                resp = scope._inst.query(pre_cmd)
                print(f"  {pre_cmd} -> {repr(resp[:100])}")
            except Exception as e:
                print(f"  {pre_cmd}: {type(e).__name__}: {e}")

scope.disconnect()
