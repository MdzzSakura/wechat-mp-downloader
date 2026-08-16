"""Windows 系统代理开关（微信 PC 版走系统代理）。仅在 Windows 上可用。"""
from __future__ import annotations

import sys

_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _refresh() -> None:
    import ctypes
    wininet = ctypes.windll.Wininet  # type: ignore[attr-defined]
    wininet.InternetSetOptionW(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
    wininet.InternetSetOptionW(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH


def get_proxy() -> dict:
    if sys.platform != "win32":
        return {}
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY) as k:
        out = {}
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
            try:
                out[name] = winreg.QueryValueEx(k, name)[0]
            except FileNotFoundError:
                out[name] = None
        return out


def set_proxy(server: str | None, override: str = "<local>") -> dict:
    """设置（server 非空）或关闭（server=None）系统代理，返回修改前的旧值便于恢复。"""
    if sys.platform != "win32":
        raise RuntimeError("仅支持 Windows")
    import winreg
    old = get_proxy()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_SET_VALUE) as k:
        if server:
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, override)
        else:
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    _refresh()
    return old


def restore_proxy(old: dict) -> None:
    if sys.platform != "win32" or not old:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, int(old.get("ProxyEnable") or 0))
        if old.get("ProxyServer") is not None:
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, old["ProxyServer"])
        if old.get("ProxyOverride") is not None:
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, old["ProxyOverride"])
    _refresh()
