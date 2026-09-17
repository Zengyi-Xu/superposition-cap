"""传输记录管理。

为每次测试生成唯一 ID，并保存关键参数和结果（JSON + 文本摘要）。
"""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import config


def generate_run_id() -> str:
    """生成唯一的测试 ID：时间戳 + 短 UUID。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uid = uuid.uuid4().hex[:6]
    return f"{ts}_{short_uid}"


def save_record(run_id: str,
                record: Dict[str, Any],
                record_dir: Path = config.RECORD_DIR) -> Path:
    """将测试记录保存为 JSON 和文本摘要。

    Args:
        run_id: 本次测试的唯一 ID
        record: 记录内容字典
        record_dir: 保存记录的目录

    Returns:
        JSON 文件路径
    """
    record_dir = Path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)

    json_path = record_dir / f"record_{run_id}.json"
    txt_path = record_dir / f"record_{run_id}.txt"

    full_record = {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
    }
    full_record.update(record)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(full_record, f, indent=2, ensure_ascii=False, default=str)

    lines = [
        f"运行 ID: {run_id}",
        f"时间戳: {full_record['timestamp']}",
        f"调制模式: {full_record.get('modulation_mode', 'superposed')}",
        f"数据源: {full_record.get('data_source', 'N/A')}",
        f"符号数: {full_record.get('datano', 'N/A')}",
        f"随机种子: {full_record.get('seed', 'N/A')}",
        f"信噪比: {full_record.get('snr_db', 'N/A')} dB",
        f"LMS 抽头/步长: {full_record.get('lms_taps', 'N/A')} / "
        f"{full_record.get('lms_mu1', 'N/A')}, {full_record.get('lms_mu2', 'N/A')}",
        f"传输速率: {full_record.get('data_rate_mbps', 'N/A')} Mbps",
        f"PAM6 带1 误码率: {full_record.get('ber_pam6_1', 'N/A')}",
        f"PAM6 带2 误码率: {full_record.get('ber_pam6_2', 'N/A')}",
        f"PAM4 带1 误码率: {full_record.get('ber_band1', 'N/A')}",
        f"PAM4 带2 误码率: {full_record.get('ber_band2', 'N/A')}",
        f"平均误码率: {full_record.get('ber_avg', 'N/A')}",
    ]
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return json_path
