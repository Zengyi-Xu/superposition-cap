"""清理没有对应波形文件的实验记录。

检查每条 record_*.json 中引用的 tx/rx/eq 文件是否存在；
若关键文件缺失，则删除该记录的 json 和 txt 摘要。
"""
import json
import sys
from pathlib import Path

import config as cfg


REQUIRED_FILE_KEYS = ["tx1_path", "tx2_path", "txsum_path", "rx_path", "eq_path"]


def cleanup(dry_run: bool = True) -> int:
    """返回实际删除（或待删除）的记录数。"""
    deleted = 0
    for json_path in sorted(cfg.RECORD_DIR.glob("record_*.json")):
        try:
            rec = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"{json_path.name}: JSON 损坏，建议手动检查")
            continue

        run_id = rec.get("run_id", json_path.stem[7:])
        missing = []
        for key in REQUIRED_FILE_KEYS:
            path_str = rec.get(key)
            if not path_str or not Path(path_str).is_file():
                missing.append(key)

        if missing:
            print(f"{run_id}: 缺失 {', '.join(missing)}")
            if not dry_run:
                json_path.unlink()
                txt_path = json_path.with_suffix(".txt")
                if txt_path.is_file():
                    txt_path.unlink()
                sweep_csv = cfg.RECORD_DIR / f"sweep_{run_id}.csv"
                if sweep_csv.is_file():
                    sweep_csv.unlink()
                deleted += 1
    return deleted


def main():
    dry_run = "--execute" not in sys.argv
    if dry_run:
        print("=====  dry-run: 以下记录将被删除（加 --execute 正式执行） =====")
    else:
        print("===== 正式清理 =====")
    deleted = cleanup(dry_run=dry_run)
    print(f"\n{'待删除' if dry_run else '已删除'}记录数: {deleted}")


if __name__ == "__main__":
    main()
