"""批量重跑 file 数据源记录，用当前代码更新实际 SNR 与 BER。

只处理 data_source == 'file' 且原平均 BER <= 阈值（默认 0.05）的记录。
如果重新跑后 BER 比原来差，则保留旧记录并报告。
"""
import json
import sys
from pathlib import Path
from typing import List, Tuple

import config as cfg
import main as main_flow
from record import save_record


BER_THRESHOLD = 0.05


def _list_records() -> List[dict]:
    """读取所有记录 JSON。"""
    records = []
    if not cfg.RECORD_DIR.is_dir():
        return records
    for p in sorted(cfg.RECORD_DIR.glob("record_*.json")):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return records


def reprocess(ber_threshold: float = BER_THRESHOLD, dry_run: bool = False) -> Tuple[int, int, List[str]]:
    """批量重跑 file 记录。

    Returns
    -------
    processed : int
        成功更新覆盖的记录数。
    skipped : int
        因 BER 变差而未覆盖的记录数。
    warnings : list[str]
        跳过的原因列表。
    """
    records = _list_records()
    processed = 0
    skipped = 0
    warnings: List[str] = []

    for rec in records:
        run_id = rec.get("run_id", "")
        src = rec.get("data_source", "")
        old_ber = rec.get("ber_avg", float("inf"))

        if src != "file":
            continue
        if old_ber > ber_threshold:
            warnings.append(f"{run_id}: 原 BER={old_ber:.3e} > {ber_threshold}，跳过")
            continue

        rx_file = rec.get("rx_file") or rec.get("rx_path", "")
        if not rx_file or not Path(rx_file).is_file():
            warnings.append(f"{run_id}: 找不到接收文件 {rx_file}，跳过")
            continue

        print(f"[{run_id}] 重新处理中... (原 BER={old_ber:.3e})")
        if dry_run:
            continue

        try:
            new_rec = main_flow.run_experiment(
                run_id=run_id,
                datano=int(rec.get("datano", cfg.DATANO)),
                seed=int(rec.get("seed", cfg.SEED_BAND1)),
                snr_db=float(rec.get("snr_db", cfg.SNR_DB)),
                lms_taps=int(rec.get("lms_taps", cfg.LMS_TAPS)),
                lms_mu1=float(rec.get("lms_mu1", cfg.LMS_MU1)),
                lms_mu2=float(rec.get("lms_mu2", cfg.LMS_MU2)),
                numof_ts=int(rec.get("numof_ts", cfg.NUMOF_TS)),
                data_source="file",
                rx_file=rx_file,
                modulation_mode=rec.get("modulation_mode", cfg.MODULATION_MODE),
                log=print,
            )
        except Exception as exc:
            warnings.append(f"{run_id}: 重跑失败: {exc}")
            continue

        new_ber = new_rec.get("ber_avg", float("inf"))
        if new_ber <= old_ber:
            save_record(run_id, new_rec, cfg.RECORD_DIR)
            print(f"  -> 已覆盖，新 BER={new_ber:.3e}, SNR={new_rec.get('snr_db', 0):.2f} dB")
            processed += 1
        else:
            warnings.append(
                f"{run_id}: BER 变差 ({old_ber:.3e} -> {new_ber:.3e})，保留旧记录"
            )
            print(f"  -> 未覆盖，BER 变差 ({old_ber:.3e} -> {new_ber:.3e})")
            skipped += 1

    return processed, skipped, warnings


def main():
    dry_run = "--dry-run" in sys.argv
    processed, skipped, warnings = reprocess(dry_run=dry_run)
    print("\n===== 批量重跑完成 =====")
    print(f"已覆盖: {processed}")
    print(f"因变差保留旧记录: {skipped}")
    if warnings:
        print("提示/跳过:")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
