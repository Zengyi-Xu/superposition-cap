"""Superposed 16QAM 收发机的命令行入口。

完整链路（镜像 A1_TX_MIMOPAM4toPAM_0723.m + A2_RX_MIMOPAM4toPAM_0826.m）：
  1. 两路随机 PAM4 序列（rng(100)/rng(200)）
  2. PAM4 -> PAM6 差分映射，缩放到 {-5,-3,-1,1,3,5}
  3. 上采样 x3 -> SRRC 成形 -> cos/sin 子载波上变频 -> 功率归一化
  4. 信道：虚拟信道 / 已保存接收文件 / 在线示波器采集（重采样到 AWG 速率）
  5. xcorr 同步 -> 正交下变频 + 匹配滤波 + 按 3 抽取 -> 复数符号流
  6. 2x2 MIMO LMS 均衡分离 I/Q 两路
  7. PAM6 判决 -> 差分解码回 PAM4 -> biterr 计算两路 BER 及平均
"""
import argparse
from pathlib import Path

import numpy as np

import channel
import config as cfg
import superposition_core as core
from equalizer import mimo_lms_equalizer
from record import generate_run_id, save_record
from utils import resample_ratio, save_txt, sync_by_xcorr, write_wfm


def run_experiment(datano: int = cfg.DATANO,
                   seed: int = cfg.SEED_BAND1,
                   snr_db: float = cfg.SNR_DB,
                   lms_taps: int = cfg.LMS_TAPS,
                   lms_mu1: float = cfg.LMS_MU1,
                   lms_mu2: float = cfg.LMS_MU2,
                   numof_ts: int = cfg.NUMOF_TS,
                   data_source: str = cfg.DATA_SOURCE,
                   rx_file: str = "",
                   osc_addr: str = cfg.OSC_VISA_ADDR,
                   osc_channel: str = cfg.OSC_CHANNEL,
                   osc_dual: bool = False,
                   osc_sample_rate_ms: float = cfg.OSC_SAMPLE,
                   awg_sample_rate_ms: float = cfg.AWG_SAMPLE,
                   modulation_mode: str = cfg.MODULATION_MODE,
                   run_id: str = None,
                   log=print) -> dict:
    """运行一次完整收发实验，返回记录字典（调用方负责 save_record）。"""
    if modulation_mode not in core.SUPPORTED_MODULATIONS:
        raise ValueError(f"不支持的调制模式: {modulation_mode!r}")
    run_id = run_id or generate_run_id()
    log(f"[{run_id}] 调制模式: {modulation_mode}  数据源: {data_source} "
        f" 符号数: {datano}  种子: {seed}")

    # ---- 发射（TX）----
    v1, v2, decimal1, decimal2 = core.generate_symbols(
        modulation_mode, datano, seed, seed + 100)
    tx = core.generate_tx(v1, v2)

    tx1_path = cfg.TXDATA_DIR / f"txI_{run_id}.txt"
    tx2_path = cfg.TXDATA_DIR / f"txQ_{run_id}.txt"
    txsum_path = cfg.TXDATA_DIR / f"txsum_{run_id}.txt"
    v1_path = cfg.TXDATA_DIR / f"v1_{run_id}.txt"
    v2_path = cfg.TXDATA_DIR / f"v2_{run_id}.txt"
    dec1_path = cfg.TXDATA_DIR / f"txsym_1_{run_id}.txt"
    dec2_path = cfg.TXDATA_DIR / f"txsym_2_{run_id}.txt"
    save_txt(tx1_path, tx["data1"])
    save_txt(tx2_path, tx["data2"])
    save_txt(txsum_path, tx["tx_sum"])
    save_txt(v1_path, v1)
    save_txt(v2_path, v2)
    save_txt(dec1_path, decimal1, fmt="%d")
    save_txt(dec2_path, decimal2, fmt="%d")
    write_wfm(tx["data1"], cfg.TXDATA_DIR / f"SuperposedPAM6_Tx1_{run_id}.wfm")
    write_wfm(tx["data2"], cfg.TXDATA_DIR / f"SuperposedPAM6_Tx2_{run_id}.wfm")
    log(f"发射波形已保存: {tx1_path.name}, {tx2_path.name} (+ .wfm)")

    # ---- 传输速率 ----
    mod_params = core.get_modulation_params(modulation_mode)
    bits_per_symbol = 2 * int(mod_params["bits_per_dim"])
    upsampleno = cfg.UPSAMPLENO
    bandwidth_mhz = awg_sample_rate_ms / upsampleno
    data_rate_mbps = bandwidth_mhz * bits_per_symbol
    log(f"带宽: {bandwidth_mhz:.1f} MHz "
        f"(AWG {awg_sample_rate_ms:.0f} MSa/s ÷ {upsampleno} 倍上采样)")
    log(f"传输速率: {data_rate_mbps:.1f} Mbps "
        f"({bandwidth_mhz:.1f} Msymbol/s × {bits_per_symbol} bits/symbol)")

    # ---- 信道 / 接收波形获取 ----
    if data_source == "virtual":
        rx_raw = channel.vlc_channel(
            tx["tx_sum"], snr_db=snr_db, fs_hz=cfg.CHANNEL_FS,
            factor=cfg.CHANNEL_FACTOR, nonlinear=cfg.CHANNEL_NONLINEAR)
        log(f"虚拟信道: SNR={snr_db:.1f} dB, factor={cfg.CHANNEL_FACTOR}, "
            f"nonlinear={cfg.CHANNEL_NONLINEAR}")
    elif data_source == "file":
        rx_raw = np.loadtxt(rx_file)
        if rx_raw.ndim > 1:
            raise ValueError(
                f"接收文件 {Path(rx_file).name} 是多列数据，看起来是均衡后的符号文件 "
                f"(eq_*.txt)。离线处理需要选择原始接收波形文件 (rx_*.txt)。"
            )
        expected_len = datano * cfg.UPSAMPLENO
        if len(rx_raw) < expected_len:
            raise ValueError(
                f"接收文件 {Path(rx_file).name} 长度 {len(rx_raw)} 远小于期望的原始波形长度 "
                f"{expected_len}。请确认选择的是原始接收波形 (rx_*.txt)，而不是均衡结果 "
                f"(eq_*.txt) 或符号文件。"
            )
        log(f"从文件加载接收波形: {rx_file} ({len(rx_raw)} 点)")
    elif data_source == "scope":
        osc_srate_hz = osc_sample_rate_ms * 1e6
        if osc_dual:
            from oscilloscope import acquire_two_channels
            result = acquire_two_channels(visa_addr=osc_addr, sample_rate=osc_srate_hz)
            rx_raw = result["ydata_sum"]
            ch_info = ", ".join(result["channels"])
            log(f"示波器双通道采集完成: {ch_info}, 相加后 {len(rx_raw)} 点, "
                f"srate={1 / result['preamble']['x_increment'] / 1e6:.0f} MSa/s")
        else:
            from oscilloscope import acquire_waveform
            result = acquire_waveform(visa_addr=osc_addr, channel=osc_channel,
                                      sample_rate=osc_srate_hz)
            rx_raw = result["ydata"]
            log(f"示波器采集完成: {result['channel']}, {len(rx_raw)} 点, "
                f"srate={1 / result['preamble']['x_increment'] / 1e6:.0f} MSa/s")
        rx_raw = resample_ratio(rx_raw, int(awg_sample_rate_ms), int(osc_sample_rate_ms))
        log(f"重采样 {osc_sample_rate_ms:.0f}->{awg_sample_rate_ms:.0f} MSa/s: {len(rx_raw)} 点")
    else:
        raise ValueError(f"未知数据源: {data_source}")

    # ---- 同步（xcorr，与 A2_RX 一致）----
    datarx, offset, _, _ = sync_by_xcorr(rx_raw, tx["tx_sum"])
    log(f"xcorr 同步: 偏移 {offset} 点")

    # ---- 下变频 + 匹配滤波 + 抽取 ----
    data_recover = core.downconvert_to_symbols(
        datarx, tx["cos1"], tx["sin1"], tx["filter_cos"], cfg.UPSAMPLENO, 0)
    datarx1 = np.real(data_recover)
    datarx2 = np.imag(data_recover)

    # ---- 2x2 MIMO LMS 均衡 ----
    equ_data1, _, _, w11, w12 = mimo_lms_equalizer(
        lms_taps, lms_mu1, lms_mu2, numof_ts, datarx1, datarx2, v1, v2)
    equ_data2, _, _, w21, w22 = mimo_lms_equalizer(
        lms_taps, lms_mu1, lms_mu2, numof_ts, datarx2, datarx1, v2, v1)
    avp1 = np.sqrt(np.mean(v1 ** 2))
    avp2 = np.sqrt(np.mean(v2 ** 2))
    recoverdata = equ_data1 * avp1 + 1j * equ_data2 * avp2
    log(f"MIMO LMS 完成: taps={lms_taps}, mu=({lms_mu1}, {lms_mu2}), 训练 {numof_ts} 符号")

    # ---- 实际 SNR 估计（file / scope 时使用均衡后符号计算）----
    sl = slice(lms_taps - 1, datano - lms_taps)
    if data_source != "virtual":
        ref_symbols = v1 + 1j * v2
        err_symbols = recoverdata[sl] - ref_symbols[sl]
        signal_power = np.mean(np.abs(ref_symbols[sl]) ** 2)
        noise_power = np.mean(np.abs(err_symbols) ** 2)
        if noise_power > 0:
            snr_db = float(10 * np.log10(signal_power / noise_power))
        else:
            snr_db = 99.0
        log(f"实际 SNR: {snr_db:.2f} dB")

    # ---- 判决与解码 ----
    rx_dec1, rx_dec2 = core.demodulate_symbols(recoverdata, modulation_mode)

    _, ber_band1 = core.biterr(rx_dec1[sl], decimal1[sl])
    _, ber_band2 = core.biterr(rx_dec2[sl], decimal2[sl])
    ber_avg = float(np.mean([ber_band1, ber_band2]))

    if modulation_mode == core.MODULATION_SUPERPOSED:
        # 叠加模式保留 PAM6 / PAM4 双层指标
        params = core.get_modulation_params(modulation_mode)
        dec6_1 = core.pam_demodulate(np.real(recoverdata), params["levels"])
        dec6_2 = core.pam_demodulate(np.imag(recoverdata), params["levels"])
        v1_unscaled = v1 / 2.0 + 2.5
        v2_unscaled = v2 / 2.0 + 2.5
        _, ber_pam6_1 = core.biterr(dec6_1[sl], v1_unscaled[sl])
        _, ber_pam6_2 = core.biterr(dec6_2[sl], v2_unscaled[sl])
        log(f"PAM6 层 BER: 带1={ber_pam6_1:.4e}, 带2={ber_pam6_2:.4e}")
        log(f"PAM4 层 BER: 带1={ber_band1:.4e}, 带2={ber_band2:.4e}")
    else:
        # 普通 QAM / NLTCP-QAM 只有一层判决
        ber_pam6_1 = ber_band1
        ber_pam6_2 = ber_band2
        log(f"I/Q 支路 BER: 带1={ber_band1:.4e}, 带2={ber_band2:.4e}")
    log(f"平均 BER = {ber_avg:.4e}")

    # ---- 保存接收/均衡结果 ----
    rx_path = cfg.RXDATA_DIR / f"rx_{run_id}.txt"
    eq_path = cfg.RXDATA_DIR / f"eq_{run_id}.txt"
    save_txt(rx_path, datarx)
    np.savetxt(eq_path, np.column_stack([np.real(recoverdata), np.imag(recoverdata)]))

    return {
        "run_id": run_id,
        "mode": "superposed",
        "modulation_mode": modulation_mode,
        "data_source": data_source,
        "datano": datano,
        "seed": seed,
        "snr_db": snr_db,
        "lms_taps": lms_taps,
        "lms_mu1": lms_mu1,
        "lms_mu2": lms_mu2,
        "numof_ts": numof_ts,
        "sync_offset": int(offset),
        "awg_sample_rate_ms": awg_sample_rate_ms,
        "upsampleno": upsampleno,
        "bandwidth_mhz": bandwidth_mhz,
        "bits_per_symbol": bits_per_symbol,
        "data_rate_mbps": data_rate_mbps,
        "ber_pam6_1": ber_pam6_1,
        "ber_pam6_2": ber_pam6_2,
        "ber_band1": ber_band1,
        "ber_band2": ber_band2,
        "ber_avg": ber_avg,
        "tx1_path": str(tx1_path),
        "tx2_path": str(tx2_path),
        "txsum_path": str(txsum_path),
        "v1_path": str(v1_path),
        "v2_path": str(v2_path),
        "txsym1_path": str(dec1_path),
        "txsym2_path": str(dec2_path),
        # 保留旧字段名，兼容旧记录 / 旧 GUI 引用
        "dec1_path": str(dec1_path),
        "dec2_path": str(dec2_path),
        "rx_path": str(rx_path),
        "eq_path": str(eq_path),
        "osc_addr": osc_addr if data_source == "scope" else "",
    }


def main():
    parser = argparse.ArgumentParser(description="Superposed 16QAM Python 收发机")
    parser.add_argument("--data-source", choices=["virtual", "file", "scope"],
                        default=cfg.DATA_SOURCE)
    parser.add_argument("--rx-file", default="", help="data_source=file 时的接收波形路径")
    parser.add_argument("--osc-addr", default=cfg.OSC_VISA_ADDR)
    parser.add_argument("--osc-channel", default=cfg.OSC_CHANNEL)
    parser.add_argument("--osc-dual", action="store_true",
                        help="同时采集示波器 CH1 和 CH2 并相加")
    parser.add_argument("--osc-sample-rate", type=float, default=cfg.OSC_SAMPLE,
                        help="示波器采样率（MSa/s）")
    parser.add_argument("--awg-sample-rate", type=float, default=cfg.AWG_SAMPLE,
                        help="AWG 采样率（MSa/s），也用于重采样")
    parser.add_argument("--snr", type=float, default=cfg.SNR_DB)
    parser.add_argument("--seed", type=int, default=cfg.SEED_BAND1)
    parser.add_argument("--datano", type=int, default=cfg.DATANO)
    parser.add_argument("--lms-taps", type=int, default=cfg.LMS_TAPS)
    parser.add_argument("--lms-mu1", type=float, default=cfg.LMS_MU1)
    parser.add_argument("--lms-mu2", type=float, default=cfg.LMS_MU2)
    parser.add_argument("--numof-ts", type=int, default=cfg.NUMOF_TS)
    parser.add_argument("--modulation", choices=core.SUPPORTED_MODULATIONS,
                        default=cfg.MODULATION_MODE,
                        help="调制模式: superposed/4QAM/16QAM/64QAM/36QAM_NLTCP")
    args = parser.parse_args()

    record = run_experiment(
        datano=args.datano, seed=args.seed, snr_db=args.snr,
        lms_taps=args.lms_taps, lms_mu1=args.lms_mu1, lms_mu2=args.lms_mu2,
        numof_ts=args.numof_ts, data_source=args.data_source,
        rx_file=args.rx_file, osc_addr=args.osc_addr,
        osc_channel=args.osc_channel, osc_dual=args.osc_dual,
        osc_sample_rate_ms=args.osc_sample_rate,
        awg_sample_rate_ms=args.awg_sample_rate,
        modulation_mode=args.modulation)
    save_record(record["run_id"], record, cfg.RECORD_DIR)


if __name__ == "__main__":
    main()
