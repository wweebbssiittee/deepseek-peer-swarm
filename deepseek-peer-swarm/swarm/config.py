from __future__ import annotations

import ctypes
import json
import os
import secrets
from pathlib import Path

PEERS = [f"peer-{i:02}" for i in range(1, 11)]
DEFAULT_PROMPT = (Path(__file__).parent / "prompts" / "peer.md").read_text(encoding="utf-8")
SETTING_NAMES = {"model", "base_url", "thinking", "reasoning_effort", "max_output_tokens", "system_prompt"}


def data_directory() -> Path:
    return Path(os.environ.get("SWARM_DATA_DIR", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "DeepSeekPeerSwarm")))


def protect(data: bytes, decrypt: bool = False) -> bytes:
    """Windows account-bound encryption; never silently fall back to plaintext."""
    if os.name != "nt":
        raise ValueError("Saving keys requires Windows DPAPI; use DEEPSEEK_API_KEY_1..10 environment variables on other systems.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    lib = ctypes.WinDLL("crypt32", use_last_error=True)
    function = lib.CryptUnprotectData if decrypt else lib.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows credential encryption failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(target.data)


class Settings:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "settings.json"
        self.values = {"model": "deepseek-flash", "base_url": "https://api.deepseek.com", "thinking": True, "reasoning_effort": "high", "max_output_tokens": 8192, "system_prompt": DEFAULT_PROMPT}
        if self.path.exists():
            saved_settings = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved_settings, dict):
                raise ValueError("Settings must contain a JSON object")
            self.values.update({name: value for name, value in saved_settings.items() if name in SETTING_NAMES})
        self.keys = [os.environ.get(f"DEEPSEEK_API_KEY_{i}", "") for i in range(1, 11)]
        vault = self.home / "keys.enc"
        if vault.exists():
            saved = json.loads(protect(vault.read_bytes(), decrypt=True))
            self.keys = [self.keys[i] or saved[i] for i in range(10)]
        token_file = self.home / "access-token.txt"
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        self.token = token_file.read_text(encoding="utf-8").strip()
        self.values = self.redact_value(self.values)

    def public(self):
        return self.redact_value({**{name: value for name, value in self.values.items() if name in SETTING_NAMES},
                                  "configured_keys": [bool(k) for k in self.keys]})

    def update(self, data: dict):
        # Scrub with both the old and new key sets if credentials rotate in this
        # update. Otherwise an old key embedded in a prompt would stop matching.
        values = self.redact_value(self.values)
        for name in ("model", "thinking", "reasoning_effort", "max_output_tokens", "system_prompt"):
            if name in data:
                values[name] = self.redact_value(data[name])
        if data.get("keys") is not None:
            keys = data["keys"]
            if len(keys) != 10:
                raise ValueError("Provide exactly ten key slots; blank preserves a saved key")
            updated = [str(k).strip() or self.keys[i] for i, k in enumerate(keys)]
            present = [k for k in updated if k]
            if len(set(present)) != len(present):
                raise ValueError("Each peer needs a separate API key")
            if any(len(k) > 512 or '\n' in k or '\r' in k for k in updated):
                raise ValueError("Invalid key format")
            if updated != self.keys:
                sealed = protect(json.dumps(updated).encode())
                temp = self.home / "keys.enc.tmp"
                temp.write_bytes(sealed)
                temp.replace(self.home / "keys.enc")
                self.keys = updated
        self.values = self.redact_value({name: value for name, value in values.items() if name in SETTING_NAMES})
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def redact(self, value: str) -> str:
        # Replace longer secrets first when one configured value prefixes another.
        for key in sorted({*self.keys, self.token}, key=len, reverse=True):
            if key:
                value = value.replace(key, "[REDACTED]")
        return value

    def redact_value(self, value):
        """Copy public data, sanitizing strings before JSON escaping can hide secrets.

        Never mutate private histories or executable tool/approval arguments.
        Only known configured credentials and the local access token are removed;
        arbitrary personal information cannot be inferred from its contents.
        """
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {self.redact(key) if isinstance(key, str) else key: self.redact_value(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        return value
