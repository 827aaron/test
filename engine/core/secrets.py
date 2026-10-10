"""密钥存储。任何 Key 都不进仓库。

查找顺序：环境变量（AIQ_ 开头）→ Mac 钥匙串 / 系统 keyring → 加密文件（服务器）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets as pysecrets
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

SERVICE = "aiquant"

# 所有已知的密钥名
KEYS = {
    "binance.demo.api_key": "币安 Demo Trading API Key",
    "binance.demo.api_secret": "币安 Demo Trading API Secret",
    "binance.testnet.api_key": "币安旧版测试网 API Key",
    "binance.testnet.api_secret": "币安旧版测试网 API Secret",
    "binance.live.api_key": "币安实盘 API Key",
    "binance.live.api_secret": "币安实盘 API Secret",
    "telegram.bot_token": "Telegram Bot Token",
    "telegram.owner_chat_id": "你自己的 Telegram 用户 ID（只响应这个账号）",
    "engine.access_token": "App 连接引擎用的访问令牌（自动生成）",
}


def env_name(key: str) -> str:
    return "AIQ_" + key.upper().replace(".", "_")


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class KeyringStore:
    def __init__(self) -> None:
        import keyring

        self._kr = keyring

    def get(self, key: str) -> str | None:
        try:
            return self._kr.get_password(SERVICE, key)
        except Exception:
            log.debug("keyring 读取失败", exc_info=True)
            return None

    def set(self, key: str, value: str) -> None:
        self._kr.set_password(SERVICE, key, value)

    def delete(self, key: str) -> None:
        try:
            self._kr.delete_password(SERVICE, key)
        except Exception:
            pass


class EncryptedFileStore:
    """服务器用：Fernet 加密的 JSON 文件，密钥由主密码经 scrypt 派生。"""

    def __init__(self, path: Path, password: str) -> None:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        self.path = path
        salt_path = path.with_suffix(path.suffix + ".salt")
        if salt_path.exists():
            salt = salt_path.read_bytes()
        else:
            salt = pysecrets.token_bytes(16)
            path.parent.mkdir(parents=True, exist_ok=True)
            salt_path.write_bytes(salt)
            os.chmod(salt_path, 0o600)
        kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
        self._f = Fernet(base64.urlsafe_b64encode(kdf.derive(password.encode())))
        self._data: dict[str, str] = {}
        if path.exists():
            self._data = json.loads(self._f.decrypt(path.read_bytes()))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self._f.encrypt(json.dumps(self._data).encode()))
        os.chmod(self.path, 0o600)

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value
        self._save()

    def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._save()


class ChainStore:
    """先看环境变量，再看后端存储。写入只写后端。"""

    def __init__(self, backend: SecretStore | None) -> None:
        self.backend = backend

    def get(self, key: str) -> str | None:
        v = os.environ.get(env_name(key))
        if v:
            return v
        return self.backend.get(key) if self.backend else None

    def set(self, key: str, value: str) -> None:
        if self.backend is None:
            raise RuntimeError("没有可写的密钥存储")
        self.backend.set(key, value)

    def delete(self, key: str) -> None:
        if self.backend:
            self.backend.delete(key)

    def ensure_access_token(self) -> str:
        tok = self.get("engine.access_token")
        if not tok:
            tok = pysecrets.token_urlsafe(32)
            self.set("engine.access_token", tok)
        return tok


def default_store(data_dir: Path) -> ChainStore:
    pw = os.environ.get("AIQ_SECRETS_PASSWORD")
    if pw:  # 服务器模式
        return ChainStore(EncryptedFileStore(data_dir / "secrets.enc", pw))
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        if isinstance(keyring.get_keyring(), FailKeyring):
            raise RuntimeError("no keyring backend")
        return ChainStore(KeyringStore())
    except Exception:
        log.warning("系统钥匙串不可用，只能使用环境变量；服务器请设置 AIQ_SECRETS_PASSWORD")
        return ChainStore(None)
