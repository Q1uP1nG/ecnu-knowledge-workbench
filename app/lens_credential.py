import os
from pathlib import Path


def windows_saved_token():
    """Read the current Windows user's DPAPI-protected Lens token."""
    local_data = os.getenv("LOCALAPPDATA")
    if os.name != "nt" or not local_data:
        return ""
    path = Path(local_data) / "ECNUWorkbench" / "lens-api-token.dpapi"
    try:
        encrypted = bytes.fromhex(path.read_text(encoding="ascii"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        raise ValueError("无法读取本机保存的 Lens Token，请重新配置 LENS_API_TOKEN。") from None

    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    buffer = ctypes.create_string_buffer(encrypted)
    incoming = DataBlob(len(encrypted), ctypes.addressof(buffer))
    outgoing = DataBlob()
    crypt = ctypes.WinDLL("crypt32").CryptUnprotectData
    if not crypt(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ValueError("当前 Windows 用户无法解密 Lens Token，请重新配置 LENS_API_TOKEN。")
    try:
        try:
            return ctypes.string_at(outgoing.data, outgoing.size).decode("utf-16-le")
        except UnicodeError:
            raise ValueError("本机保存的 Lens Token 格式无效，请重新配置 LENS_API_TOKEN。") from None
    finally:
        ctypes.memset(outgoing.data, 0, outgoing.size)
        ctypes.WinDLL("kernel32").LocalFree(ctypes.c_void_p(outgoing.data))
