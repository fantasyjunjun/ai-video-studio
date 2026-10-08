"""OS 凭据库封装（P-1b）。

设计目标：**内联密钥不再明文落盘 providers.yaml**，改为：
  - 优先存进 OS 凭据库（keyring）：Windows 凭据管理器 / macOS Keychain / Linux libsecret；
  - 当运行环境没有可用的 OS 凭据库时（无桌面会话、CI、headless 服务器），
    自动回退到**本地 Fernet 加密文件**（应用数据目录内 `.secret.key` + `.secrets.enc`），
    密钥仍以加密形态静态存放，绝不明文。

这层封装对上层透明：config.py 的 resolve_* 只调用 get_secret / set_secret / delete_secret，
不关心底层到底是 keyring 还是加密文件。对"自由配置任意 API"的硬约束零影响——
存储位置只是落盘方式，抽象层（base_url + key + 多 key/fallback）一个字都不动。

注意：本模块文件名刻意叫 `secret_store` 而非 `secrets`，避免遮蔽 Python 标准库 `secrets`。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

# 与 providers.yaml 同目录（backend 目录）；用 __file__ 推导避免与 deps 形成导入环。
BASE = Path(__file__).resolve().parents[2]
SERVICE = "ai-video-studio"

_lock = threading.Lock()


def _account(slot: str, pid: str) -> str:
    return f"{slot}:{pid}"


# ------------------------------------------------------------------ keyring 探测


def _keyring():
    """返回 (keyring 模块, 后端名)；若没有可用后端则返回 (None, "")。"""
    try:
        import keyring  # 延迟导入，未安装时不影响其他功能
    except Exception:
        return None, ""
    try:
        kr = keyring.get_keyring()
    except Exception:
        return None, ""
    name = (getattr(kr, "name", "") or "") if kr is not None else ""
    # keyring 在找不到任何可用后端时会返回一个 fail 后端，名字里带 "fail" / "fail"，
    # 这种"假后端"不能写也不能读，必须当作不可用
    if kr is None or "fail" in name.lower():
        return None, ""
    return keyring, name


def secret_backend_name() -> str:
    """当前实际使用的密钥后端，便于排查与在 UI 上展示。"""
    kr, name = _keyring()
    if kr is not None:
        return f"keyring:{name}"
    return "encrypted-file"


# ------------------------------------------------------------------ 加密文件回退


def _key_path() -> Path:
    return BASE / ".secret.key"


def _store_path() -> Path:
    return BASE / ".secrets.enc"


def _restrict(path: Path) -> None:
    """尽力把文件权限收窄到当前用户（Windows 上 POSIX 权限无效，改用 icacls）。"""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or "*S-1-5-32-544"  # 退化为 Administrators
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:R"],
                check=False, capture_output=True,
            )
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass


def _fernet():
    from cryptography.fernet import Fernet

    kp = _key_path()
    if kp.exists():
        key = kp.read_bytes()
    else:
        key = Fernet.generate_key()
        kp.write_bytes(key)  # 截断覆盖（托管环境禁止 os.remove）
        _restrict(kp)
    return Fernet(key)


def _load_file() -> dict:
    p = _store_path()
    if not p.exists():
        return {}
    try:
        f = _fernet()
        data = json.loads(f.decrypt(p.read_bytes()).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_file(data: dict) -> None:
    f = _fernet()
    token = f.encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    _store_path().write_bytes(token)  # 截断覆盖
    _restrict(_store_path())


# ------------------------------------------------------------------ 公开 API


def set_secret(slot: str, pid: str, value: str) -> None:
    acc = _account(slot, pid)
    kr, _ = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, acc, value)
            return
        except Exception:
            pass
    # 回退：本地加密文件
    with _lock:
        data = _load_file()
        data[acc] = value
        _save_file(data)


def get_secret(slot: str, pid: str) -> Optional[str]:
    acc = _account(slot, pid)
    kr, _ = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(SERVICE, acc)
            if v:
                return v
        except Exception:
            pass
    with _lock:
        return _load_file().get(acc)


def delete_secret(slot: str, pid: str) -> None:
    acc = _account(slot, pid)
    kr, _ = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, acc)
        except Exception:
            pass
    with _lock:
        data = _load_file()
        if acc in data:
            del data[acc]
            _save_file(data)


def has_secret(slot: str, pid: str) -> bool:
    return get_secret(slot, pid) is not None
