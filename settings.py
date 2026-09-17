"""用户设置持久化（示波器 / AWG 地址等）。

设置保存在 data/settings.json，优先级高于 config.py 中的硬编码默认值，
但仅作为默认值使用，不会修改版本控制中的 config.py。
"""
import json
import sys
from pathlib import Path
from typing import Optional

import config as cfg

_SETTINGS_PATH: Path = cfg.DATA_DIR / "settings.json"

DEFAULTS = {
    "OSC_VISA_ADDR": cfg.OSC_VISA_ADDR,
    "AWG_VISA_ADDR": cfg.AWG_VISA_ADDR,
    "OSC_DUAL": False,
}


def load_settings() -> dict:
    """读取已保存设置；不存在或损坏时返回默认值。"""
    if not _SETTINGS_PATH.exists():
        return dict(DEFAULTS)
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULTS)
        settings = dict(DEFAULTS)
        settings.update(data)
        return settings
    except Exception:
        return dict(DEFAULTS)


def save_settings(updates: dict) -> None:
    """增量保存设置到 data/settings.json。"""
    try:
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        current = load_settings()
        for key, value in updates.items():
            if value is None:
                continue
            if isinstance(value, str):
                current[key] = value.strip()
            else:
                current[key] = value
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"保存设置失败: {exc}", file=sys.stderr)


def get_setting(key: str, default: Optional[str] = None) -> str:
    """读取单个设置项。"""
    return str(load_settings().get(key, default or DEFAULTS.get(key, "")))
