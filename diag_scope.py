"""MSO22 示波器 SCPI 命令诊断脚本。"""
import pyvisa

ADDR = "USB::0x0699::0x0105::C012762::INSTR"

rm = pyvisa.ResourceManager()
inst = rm.open_resource(ADDR)
inst.timeout = 10000

print("*IDN? ->", inst.query("*IDN?").strip())
print()

print("=== 通道选择命令 ===")
for cmd in [":WAVEFORM:SOURCE CH1", ":DATA:SOURCE CH1", ":SELECT:CH1 ON"]:
    try:
        inst.write(cmd)
        print(f"{cmd}: OK")
    except Exception as e:
        print(f"{cmd}: {type(e).__name__}: {e}")
print()

print("=== Preamble 命令 ===")
for src in [":WAVEFORM:SOURCE CH1", ":DATA:SOURCE CH1"]:
    try:
        inst.write(src)
    except Exception as e:
        print(f"{src}: {e}")
        continue
    for pre in [":WAVEFORM:PREAMBLE?", ":WFMOUTPRE?", ":WFMPRE?"]:
        try:
            resp = inst.query(pre)
            print(f"{src} + {pre} -> [{repr(resp)}]")
        except Exception as e:
            print(f"{src} + {pre}: {type(e).__name__}: {e}")
print()

print("=== 波形数据命令 ===")
for enc in [":DATA:ENCdg RIBINARY", ":WAVEFORM:ENCODING BINARY"]:
    try:
        inst.write(enc)
        print(f"{enc}: OK")
    except Exception as e:
        print(f"{enc}: {e}")

for data_cmd in [":WAV:DATA?", ":CURVE?"]:
    try:
        inst.write(":DATA:SOURCE CH1")
        inst.write(data_cmd)
        raw = inst.read_raw()[:100]
        print(f"{data_cmd} -> first 100 bytes: {raw}")
    except Exception as e:
        print(f"{data_cmd}: {type(e).__name__}: {e}")

inst.close()
rm.close()
print("\n诊断完成。")
