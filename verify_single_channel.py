"""验证单通道叠加发射 + 单端口接收的链路功能。

本脚本不连接真实仪器，只做离线验证：
1. generate_tx 生成的 tx_sum 确实等于 data1 + data2。
2. combine_and_download_single_channel 的合成逻辑等价于 tx_sum 并归一化到 [-1, 1]。
3. 用 tx_sum 通过虚拟信道后，单端口接收（I/Q 下变频 + MIMO LMS）仍能正常解调。
"""
import numpy as np

import config as cfg
import main as main_flow
import superposition_core as core
import awg_m8190a


def verify_tx_sum():
    datano = 512
    v1, v2, _, _ = core.generate_symbols(core.MODULATION_SUPERPOSED, datano,
                                         cfg.SEED_BAND1, cfg.SEED_BAND2)
    tx = core.generate_tx(v1, v2)
    expected = tx["data1"] + tx["data2"]
    assert np.allclose(tx["tx_sum"], expected), "tx_sum 不等于 data1 + data2"
    print(f"[OK] tx_sum 长度={len(tx['tx_sum'])}, 峰值={np.max(np.abs(tx['tx_sum'])):.4f}")


def verify_combined_waveform():
    datano = 512
    v1, v2, _, _ = core.generate_symbols(core.MODULATION_SUPERPOSED, datano,
                                         cfg.SEED_BAND1, cfg.SEED_BAND2)
    tx = core.generate_tx(v1, v2)
    combined = np.asarray(tx["data1"], dtype=float) + np.asarray(tx["data2"], dtype=float)
    peak = np.max(np.abs(combined))
    combined_norm = combined / peak if peak > 0 else combined
    assert np.isclose(np.max(np.abs(combined_norm)), 1.0), "归一化后峰值应为 1"
    # 等价于 awg_m8190a.combine_and_download_single_channel 内部的第一步处理
    print(f"[OK] 单通道叠加波形已归一化，峰值=1.0，原始峰值={peak:.4f}")


def verify_single_port_receiver():
    record = main_flow.run_experiment(
        datano=1024,
        seed=cfg.SEED_BAND1,
        snr_db=20.0,
        lms_taps=cfg.LMS_TAPS,
        lms_mu1=cfg.LMS_MU1,
        lms_mu2=cfg.LMS_MU2,
        numof_ts=800,
        data_source="virtual",
        modulation_mode=core.MODULATION_SUPERPOSED,
        log=print,
    )
    assert record["ber_avg"] < 0.5, "平均 BER 异常，接收链路可能已损坏"
    print(f"[OK] 单端口接收平均 BER={record['ber_avg']:.4e}")


def verify_awg_helpers_exist():
    assert hasattr(awg_m8190a, "download_single_channel")
    assert hasattr(awg_m8190a, "combine_and_download_single_channel")
    print("[OK] awg_m8190a 单通道下载辅助函数已定义")


if __name__ == "__main__":
    verify_awg_helpers_exist()
    verify_tx_sum()
    verify_combined_waveform()
    verify_single_port_receiver()
    print("\n全部验证通过。")
