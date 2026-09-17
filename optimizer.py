"""离线 LMS 参数网格扫描 / 优化。

针对 data_source='file' 的场景：用户已有一个原始接收波形 rx_*.txt，
指定 LMS taps、mu1、mu2 的搜索范围后，自动找出使平均 BER 最低的参数组合。
"""
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

import config as cfg
import superposition_core as core
from equalizer import mimo_lms_equalizer
from record import generate_run_id
from utils import save_txt, sync_by_xcorr, write_wfm

LogFn = Optional[Callable[[str], None]]


def _to_log(msg: str, log: LogFn) -> None:
    if log is not None:
        log(msg)


def build_param_grid(
    taps_range: Tuple[int, int, int],
    mu1_range: Tuple[float, float, int],
    mu2_range: Tuple[float, float, int],
    mu_scale: str = "log",
) -> List[Tuple[int, float, float]]:
    """构造 LMS 参数网格。

    Parameters
    ----------
    taps_range : (min, max, step)
        抽头数搜索范围，强制为奇数。
    mu1_range, mu2_range : (min, max, points)
        步长搜索范围；points 为采样点数。
    mu_scale : "log" | "linear"
        步长采样方式。通常步长跨数量级，默认 log。
    """
    t_min, t_max, t_step = taps_range
    taps = [int(t) for t in np.arange(t_min, t_max + 1, t_step) if int(t) % 2 == 1]
    if not taps:
        raise ValueError(f"抽头数范围 {taps_range} 未产生有效奇数")

    def _mu_grid(mu_range: Tuple[float, float, int]) -> np.ndarray:
        m_min, m_max, points = mu_range
        if mu_scale.lower() == "log":
            return np.logspace(np.log10(m_min), np.log10(m_max), points)
        return np.linspace(m_min, m_max, points)

    mu1_grid = _mu_grid(mu1_range)
    mu2_grid = _mu_grid(mu2_range)

    grid = []
    for t in taps:
        for m1 in mu1_grid:
            for m2 in mu2_grid:
                grid.append((int(t), float(m1), float(m2)))
    return grid


def evaluate_lms_params(
    datarx1: np.ndarray,
    datarx2: np.ndarray,
    v1: np.ndarray,
    v2: np.ndarray,
    decimal1: np.ndarray,
    decimal2: np.ndarray,
    modulation_mode: str,
    taps: int,
    mu1: float,
    mu2: float,
    numof_ts: int,
    datano: int,
) -> Dict[str, float]:
    """对一组 LMS 参数评估 BER（只返回数值结果，不保存文件）。"""
    equ_data1, _, _, _, _ = mimo_lms_equalizer(
        taps, mu1, mu2, numof_ts, datarx1, datarx2, v1, v2
    )
    equ_data2, _, _, _, _ = mimo_lms_equalizer(
        taps, mu1, mu2, numof_ts, datarx2, datarx1, v2, v1
    )
    avp1 = np.sqrt(np.mean(v1 ** 2))
    avp2 = np.sqrt(np.mean(v2 ** 2))
    recoverdata = equ_data1 * avp1 + 1j * equ_data2 * avp2

    rx_dec1, rx_dec2 = core.demodulate_symbols(recoverdata, modulation_mode)
    sl = slice(taps - 1, datano - taps)
    _, ber_band1 = core.biterr(rx_dec1[sl], decimal1[sl])
    _, ber_band2 = core.biterr(rx_dec2[sl], decimal2[sl])
    ber_avg = float(np.mean([ber_band1, ber_band2]))

    # 实际 SNR（离线文件场景）
    ref_symbols = v1 + 1j * v2
    err_symbols = recoverdata[sl] - ref_symbols[sl]
    signal_power = np.mean(np.abs(ref_symbols[sl]) ** 2)
    noise_power = np.mean(np.abs(err_symbols) ** 2)
    snr_db = float(10 * np.log10(signal_power / noise_power)) if noise_power > 0 else 99.0

    return {
        "ber_band1": float(ber_band1),
        "ber_band2": float(ber_band2),
        "ber_avg": ber_avg,
        "snr_db": snr_db,
    }


def _save_tx_files(run_id: str, v1, v2, decimal1, decimal2, tx: dict) -> Dict[str, str]:
    """保存当前测试 ID 对应的发射文件，返回路径字典。"""
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

    return {
        "tx1_path": str(tx1_path),
        "tx2_path": str(tx2_path),
        "txsum_path": str(txsum_path),
        "v1_path": str(v1_path),
        "v2_path": str(v2_path),
        "txsym1_path": str(dec1_path),
        "txsym2_path": str(dec2_path),
        "dec1_path": str(dec1_path),
        "dec2_path": str(dec2_path),
    }


def run_lms_sweep(
    rx_file: str,
    datano: int = cfg.DATANO,
    seed: int = cfg.SEED_BAND1,
    snr_db: float = cfg.SNR_DB,
    modulation_mode: str = cfg.MODULATION_MODE,
    numof_ts: int = cfg.NUMOF_TS,
    taps_range: Tuple[int, int, int] = (3, 31, 4),
    mu1_range: Tuple[float, float, int] = (1e-4, 1e-1, 5),
    mu2_range: Tuple[float, float, int] = (1e-4, 1e-1, 5),
    mu_scale: str = "log",
    log: LogFn = print,
) -> Tuple[Dict, List[Dict]]:
    """离线 LMS 参数网格扫描。

    Returns
    -------
    best_record : dict
        可被 `save_record` 直接保存的记录（包含最优参数、BER、路径等）。
    all_results : list[dict]
        所有参数组合及其 BER，可用于输出 CSV。
    """
    if modulation_mode not in core.SUPPORTED_MODULATIONS:
        raise ValueError(f"不支持的调制模式: {modulation_mode!r}")

    run_id = generate_run_id()
    _to_log(f"[{run_id}] 开始 LMS 参数扫描: {modulation_mode}, datano={datano}, seed={seed}", log)

    # ---- 生成发射信号（只做一次）----
    v1, v2, decimal1, decimal2 = core.generate_symbols(modulation_mode, datano, seed, seed + 100)
    tx = core.generate_tx(v1, v2)
    tx_paths = _save_tx_files(run_id, v1, v2, decimal1, decimal2, tx)
    _to_log(f"发射波形已保存: txI_{run_id}.txt, txQ_{run_id}.txt", log)

    # ---- 加载并校验接收文件 ----
    rx_raw = np.loadtxt(rx_file)
    if rx_raw.ndim > 1:
        raise ValueError(
            f"接收文件 {Path(rx_file).name} 是多列数据，看起来是均衡结果 (eq_*.txt)。"
            f"离线优化需要选择原始接收波形文件 (rx_*.txt)。"
        )
    expected_len = datano * cfg.UPSAMPLENO
    if len(rx_raw) < expected_len:
        raise ValueError(
            f"接收文件 {Path(rx_file).name} 长度 {len(rx_raw)} 小于期望的原始波形长度 "
            f"{expected_len}。请选择 rx_*.txt 原始波形。"
        )
    _to_log(f"从文件加载接收波形: {rx_file} ({len(rx_raw)} 点)", log)

    # ---- 同步 / 下变频 / 抽取（只做一次）----
    datarx, offset, _, _ = sync_by_xcorr(rx_raw, tx["tx_sum"])
    _to_log(f"xcorr 同步: 偏移 {offset} 点", log)

    data_recover = core.downconvert_to_symbols(
        datarx, tx["cos1"], tx["sin1"], tx["filter_cos"], cfg.UPSAMPLENO, 0
    )
    datarx1 = np.real(data_recover)
    datarx2 = np.imag(data_recover)

    # ---- 参数网格 ----
    grid = build_param_grid(taps_range, mu1_range, mu2_range, mu_scale)
    total = len(grid)
    _to_log(f"参数网格: {total} 组 (taps×μ1×μ2)", log)

    all_results: List[Dict] = []
    best_record: Optional[Dict] = None
    best_ber = float("inf")

    for i, (taps, mu1, mu2) in enumerate(grid, start=1):
        try:
            res = evaluate_lms_params(
                datarx1, datarx2, v1, v2, decimal1, decimal2,
                modulation_mode, taps, mu1, mu2, numof_ts, datano,
            )
        except Exception as exc:
            _to_log(f"  [{i}/{total}] taps={taps}, mu1={mu1:.2e}, mu2={mu2:.2e} 失败: {exc}", log)
            all_results.append({
                "taps": taps, "mu1": mu1, "mu2": mu2,
                "ber_band1": np.nan, "ber_band2": np.nan, "ber_avg": np.nan,
            })
            continue

        all_results.append({
            "taps": taps, "mu1": mu1, "mu2": mu2,
            **res,
        })

        if not np.isnan(res["ber_avg"]) and res["ber_avg"] < best_ber:
            best_ber = res["ber_avg"]
            best_record = {
                "run_id": run_id,
                "mode": "superposed",
                "modulation_mode": modulation_mode,
                "data_source": "file",
                "datano": datano,
                "seed": seed,
                "snr_db": snr_db,
                "lms_taps": int(taps),
                "lms_mu1": float(mu1),
                "lms_mu2": float(mu2),
                "numof_ts": numof_ts,
                "sync_offset": int(offset),
                **res,
                **tx_paths,
            }

        if i == 1 or i == total or i % max(1, total // 10) == 0:
            _to_log(
                f"  [{i}/{total}] taps={taps}, mu1={mu1:.2e}, mu2={mu2:.2e} => "
                f"avg BER={res['ber_avg']:.4e} (best={best_ber:.4e})", log
            )

    if best_record is None:
        raise RuntimeError("所有参数组合均失败，未找到有效结果。")

    # ---- 保存最优结果的接收/均衡文件 ----
    rx_path = cfg.RXDATA_DIR / f"rx_{run_id}.txt"
    eq_path = cfg.RXDATA_DIR / f"eq_{run_id}.txt"
    save_txt(rx_path, datarx)

    # 用最优参数重新计算一次，得到均衡后的星座用于绘图
    equ_data1, _, _, _, _ = mimo_lms_equalizer(
        best_record["lms_taps"], best_record["lms_mu1"], best_record["lms_mu2"],
        numof_ts, datarx1, datarx2, v1, v2
    )
    equ_data2, _, _, _, _ = mimo_lms_equalizer(
        best_record["lms_taps"], best_record["lms_mu1"], best_record["lms_mu2"],
        numof_ts, datarx2, datarx1, v2, v1
    )
    avp1 = np.sqrt(np.mean(v1 ** 2))
    avp2 = np.sqrt(np.mean(v2 ** 2))
    recoverdata = equ_data1 * avp1 + 1j * equ_data2 * avp2
    np.savetxt(eq_path, np.column_stack([np.real(recoverdata), np.imag(recoverdata)]))

    best_record["rx_path"] = str(rx_path)
    best_record["eq_path"] = str(eq_path)
    best_record["rx_file"] = str(rx_file)

    _to_log(
        f"扫描完成。最优参数: taps={best_record['lms_taps']}, "
        f"mu1={best_record['lms_mu1']:.4e}, mu2={best_record['lms_mu2']:.4e}, "
        f"平均 BER={best_record['ber_avg']:.4e}", log
    )
    return best_record, all_results


def _taps_candidates(taps_range: Tuple[int, int, int]) -> List[int]:
    """按 (min, max, step) 生成奇数抽头数候选列表。"""
    t_min, t_max, t_step = taps_range
    return [int(t) for t in np.arange(t_min, t_max + 1, t_step) if int(t) % 2 == 1]


def _mu_candidates(mu_range: Tuple[float, float, int], mu_scale: str) -> np.ndarray:
    """按 (min, max, points) 生成 μ 候选数组（log 或 linear）。"""
    m_min, m_max, points = mu_range
    if mu_scale.lower() == "log":
        return np.logspace(np.log10(m_min), np.log10(m_max), points)
    return np.linspace(m_min, m_max, points)


def run_lms_coordinate_search(
    rx_file: str,
    datano: int = cfg.DATANO,
    seed: int = cfg.SEED_BAND1,
    snr_db: float = cfg.SNR_DB,
    modulation_mode: str = cfg.MODULATION_MODE,
    numof_ts: int = cfg.NUMOF_TS,
    taps_range: Tuple[int, int, int] = (3, 31, 4),
    mu1_range: Tuple[float, float, int] = (1e-4, 1e-1, 5),
    mu2_range: Tuple[float, float, int] = (1e-4, 1e-1, 5),
    mu_scale: str = "log",
    initial_taps: Optional[int] = None,
    initial_mu1: Optional[float] = None,
    initial_mu2: Optional[float] = None,
    log: LogFn = print,
) -> Tuple[Dict, List[Dict]]:
    """离线 LMS 三轮坐标下降优化。

    第一轮：全范围坐标下降（taps → μ1 → μ2）。
    第二轮：在已收敛的 μ 上重新选择 taps。
    第三轮：在第二轮最优值附近的 1/3 ~ 3 倍 log 范围内细化 μ。

    每维搜索时其它维固定为当前最优值，因此总评估次数远低于穷举网格。

    Returns
    -------
    best_record : dict
        可被 `save_record` 直接保存的记录。
    all_results : list[dict]
        所有尝试过的参数点及其 BER，可用于输出 CSV。
    """
    if modulation_mode not in core.SUPPORTED_MODULATIONS:
        raise ValueError(f"不支持的调制模式: {modulation_mode!r}")

    # 初始值：未指定时取各自范围中点（taps 取最接近的奇数）
    taps_cand = _taps_candidates(taps_range)
    mu1_cand = _mu_candidates(mu1_range, mu_scale)
    mu2_cand = _mu_candidates(mu2_range, mu_scale)
    if not taps_cand or not len(mu1_cand) or not len(mu2_cand):
        raise ValueError("参数范围设置不正确，未产生任何候选值。")

    cur_taps = initial_taps if initial_taps is not None else int(taps_cand[len(taps_cand) // 2])
    cur_mu1 = initial_mu1 if initial_mu1 is not None else float(mu1_cand[len(mu1_cand) // 2])
    cur_mu2 = initial_mu2 if initial_mu2 is not None else float(mu2_cand[len(mu2_cand) // 2])

    run_id = generate_run_id()
    _to_log(
        f"[{run_id}] 开始 LMS 坐标下降优化: {modulation_mode}, "
        f"datano={datano}, seed={seed}", log
    )
    _to_log(
        f"初始参数: taps={cur_taps}, μ1={cur_mu1:.4e}, μ2={cur_mu2:.4e}", log
    )

    # ---- 生成发射信号（只做一次）----
    v1, v2, decimal1, decimal2 = core.generate_symbols(modulation_mode, datano, seed, seed + 100)
    tx = core.generate_tx(v1, v2)
    tx_paths = _save_tx_files(run_id, v1, v2, decimal1, decimal2, tx)
    _to_log(f"发射波形已保存: txI_{run_id}.txt, txQ_{run_id}.txt", log)

    mod_params = core.get_modulation_params(modulation_mode)
    bits_per_symbol = 2 * int(mod_params["bits_per_dim"])
    symbol_rate_mhz = (awg_sample_rate_ms or cfg.AWG_SAMPLE) / cfg.UPSAMPLENO
    data_rate_mbps = symbol_rate_mhz * bits_per_symbol
    _to_log(f"传输速率: {data_rate_mbps:.1f} Mbps "
            f"({symbol_rate_mhz:.1f} Msymbol/s × {bits_per_symbol} bits/symbol)", log)

    # ---- 加载并校验接收文件 ----
    rx_raw = np.loadtxt(rx_file)
    if rx_raw.ndim > 1:
        raise ValueError(
            f"接收文件 {Path(rx_file).name} 是多列数据，看起来是均衡结果 (eq_*.txt)。"
            f"离线优化需要选择原始接收波形文件 (rx_*.txt)。"
        )
    expected_len = datano * cfg.UPSAMPLENO
    if len(rx_raw) < expected_len:
        raise ValueError(
            f"接收文件 {Path(rx_file).name} 长度 {len(rx_raw)} 小于期望的原始波形长度 "
            f"{expected_len}。请选择 rx_*.txt 原始波形。"
        )
    _to_log(f"从文件加载接收波形: {rx_file} ({len(rx_raw)} 点)", log)

    # ---- 同步 / 下变频 / 抽取（只做一次）----
    datarx, offset, _, _ = sync_by_xcorr(rx_raw, tx["tx_sum"])
    _to_log(f"xcorr 同步: 偏移 {offset} 点", log)

    data_recover = core.downconvert_to_symbols(
        datarx, tx["cos1"], tx["sin1"], tx["filter_cos"], cfg.UPSAMPLENO, 0
    )
    datarx1 = np.real(data_recover)
    datarx2 = np.imag(data_recover)

    def _eval(taps: int, mu1: float, mu2: float) -> Dict:
        return evaluate_lms_params(
            datarx1, datarx2, v1, v2, decimal1, decimal2,
            modulation_mode, taps, mu1, mu2, numof_ts, datano,
        )

    def _make_record(taps: int, mu1: float, mu2: float, res: Dict) -> Dict:
        return {
            "run_id": run_id,
            "mode": "superposed",
            "modulation_mode": modulation_mode,
            "data_source": "file",
            "datano": datano,
            "seed": seed,
            "snr_db": snr_db,
            "lms_taps": int(taps),
            "lms_mu1": float(mu1),
            "lms_mu2": float(mu2),
            "numof_ts": numof_ts,
            "sync_offset": int(offset),
            "awg_sample_rate_ms": awg_sample_rate_ms or cfg.AWG_SAMPLE,
            "bits_per_symbol": bits_per_symbol,
            "data_rate_mbps": data_rate_mbps,
            **res,
            **tx_paths,
        }

    all_results: List[Dict] = []
    best_record: Optional[Dict] = None
    best_ber = float("inf")

    def _optimize_dimension(dim_name, candidates, apply, current):
        nonlocal best_ber, best_record
        best_dim_val = current[dim_name]
        best_dim_ber = best_ber
        _to_log(f"开始优化 {dim_name}，当前最优 BER={best_ber:.4e}", log)
        for i, val in enumerate(candidates, start=1):
            taps, mu1, mu2 = apply(current, val)
            try:
                res = _eval(taps, mu1, mu2)
            except Exception as exc:
                _to_log(f"  [{dim_name} {i}/{len(candidates)}] {val} 失败: {exc}", log)
                all_results.append({
                    "taps": taps, "mu1": mu1, "mu2": mu2,
                    "ber_band1": np.nan, "ber_band2": np.nan,
                    "ber_avg": np.nan, "snr_db": np.nan,
                })
                continue
            all_results.append({"taps": taps, "mu1": mu1, "mu2": mu2, **res})
            if not np.isnan(res["ber_avg"]) and res["ber_avg"] < best_dim_ber:
                best_dim_ber = res["ber_avg"]
                best_dim_val = val
            if not np.isnan(res["ber_avg"]) and res["ber_avg"] < best_ber:
                best_ber = res["ber_avg"]
                best_record = _make_record(taps, mu1, mu2, res)
            _to_log(
                f"  [{dim_name} {i}/{len(candidates)}] {val} => "
                f"avg BER={res['ber_avg']:.4e} (best={best_dim_ber:.4e})", log
            )
        current[dim_name] = best_dim_val
        _to_log(
            f"{dim_name} 优化完成: 最优={best_dim_val}, "
            f"该维 BER={best_dim_ber:.4e}, 全局 best={best_ber:.4e}", log
        )

    # 评估初始点
    try:
        init_res = _eval(cur_taps, cur_mu1, cur_mu2)
    except Exception as exc:
        raise RuntimeError(f"初始参数评估失败: {exc}")
    all_results.append({"taps": cur_taps, "mu1": cur_mu1, "mu2": cur_mu2, **init_res})
    best_record = _make_record(cur_taps, cur_mu1, cur_mu2, init_res)
    best_ber = init_res["ber_avg"]
    current = {"taps": cur_taps, "mu1": cur_mu1, "mu2": cur_mu2}

    # ---- 第一轮：全范围坐标下降 taps → μ1 → μ2 ----
    _to_log("===== 第一轮：全范围坐标下降 =====", log)
    round1_dims = [
        ("taps", taps_cand, lambda t, v: (int(v), t["mu1"], t["mu2"])),
        ("mu1", mu1_cand, lambda t, v: (t["taps"], float(v), t["mu2"])),
        ("mu2", mu2_cand, lambda t, v: (t["taps"], t["mu1"], float(v))),
    ]
    for dim_name, candidates, apply in round1_dims:
        _optimize_dimension(dim_name, candidates, apply, current)

    # ---- 第二轮：固定 μ，重新选择 taps ----
    _to_log("===== 第二轮：在已收敛 μ 上重新选择 taps =====", log)
    _optimize_dimension(
        "taps", taps_cand,
        lambda t, v: (int(v), t["mu1"], t["mu2"]), current
    )

    # ---- 第三轮：在第二轮最优值附近 1/3 ~ 3 倍范围内细化 μ ----
    _to_log("===== 第三轮：μ 邻域 log 细化 =====", log)
    mu1_refine = _mu_candidates(
        (max(current["mu1"] / 3.0, 1e-6), current["mu1"] * 3.0, len(mu1_cand)), "log"
    )
    mu2_refine = _mu_candidates(
        (max(current["mu2"] / 3.0, 1e-6), current["mu2"] * 3.0, len(mu2_cand)), "log"
    )
    round3_dims = [
        ("mu1", mu1_refine, lambda t, v: (t["taps"], float(v), t["mu2"])),
        ("mu2", mu2_refine, lambda t, v: (t["taps"], t["mu1"], float(v))),
    ]
    for dim_name, candidates, apply in round3_dims:
        _optimize_dimension(dim_name, candidates, apply, current)

    # ---- 保存最优结果的接收/均衡文件 ----
    rx_path = cfg.RXDATA_DIR / f"rx_{run_id}.txt"
    eq_path = cfg.RXDATA_DIR / f"eq_{run_id}.txt"
    save_txt(rx_path, datarx)

    equ_data1, _, _, _, _ = mimo_lms_equalizer(
        best_record["lms_taps"], best_record["lms_mu1"], best_record["lms_mu2"],
        numof_ts, datarx1, datarx2, v1, v2
    )
    equ_data2, _, _, _, _ = mimo_lms_equalizer(
        best_record["lms_taps"], best_record["lms_mu1"], best_record["lms_mu2"],
        numof_ts, datarx2, datarx1, v2, v1
    )
    avp1 = np.sqrt(np.mean(v1 ** 2))
    avp2 = np.sqrt(np.mean(v2 ** 2))
    recoverdata = equ_data1 * avp1 + 1j * equ_data2 * avp2
    np.savetxt(eq_path, np.column_stack([np.real(recoverdata), np.imag(recoverdata)]))

    best_record["rx_path"] = str(rx_path)
    best_record["eq_path"] = str(eq_path)
    best_record["rx_file"] = str(rx_file)

    _to_log(
        f"三轮优化完成。最优参数: taps={best_record['lms_taps']}, "
        f"μ1={best_record['lms_mu1']:.4e}, μ2={best_record['lms_mu2']:.4e}, "
        f"平均 BER={best_record['ber_avg']:.4e}", log
    )
    return best_record, all_results
