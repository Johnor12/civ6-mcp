"""Pictures of the game window, and key presses sent to it (Windows, standard library only).

The game's Lua has no screenshot call, so the picture is taken from
outside: the window's own content through PrintWindow, which works while
other windows cover the game, saved as a JPEG by GDI+. Keys go to the
window's message queue, so the game need not have the keyboard focus.
"""

from __future__ import annotations

import ctypes
import logging
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

GAME_EXE_PREFIX = "civilizationvi"  # CivilizationVI.exe, CivilizationVI_DX12.exe
JPEG_QUALITY = 88
VK_ESCAPE = 0x1B

_PW_RENDERFULLCONTENT = 2  # what the compositor shows, so Direct3D windows are not black
_SRCCOPY = 0x00CC0020
_WM_KEYDOWN, _WM_KEYUP = 0x0100, 0x0101
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_JPEG_ENCODER = "{557CF401-1A04-11D3-9A73-0000F81EF32E}"
_QUALITY_PARAMETER = "{1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB}"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_HANDLE = ctypes.c_void_p
_user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
_user32.IsWindowVisible.argtypes = [_HANDLE]
_user32.IsIconic.argtypes = [_HANDLE]
_user32.GetWindowThreadProcessId.argtypes = [_HANDLE, ctypes.POINTER(wintypes.DWORD)]
_user32.GetClientRect.argtypes = [_HANDLE, ctypes.POINTER(wintypes.RECT)]
_user32.ClientToScreen.argtypes = [_HANDLE, ctypes.POINTER(wintypes.POINT)]
_user32.GetWindowDC.argtypes = [_HANDLE]
_user32.GetWindowDC.restype = _HANDLE
_user32.GetDC.argtypes = [_HANDLE]
_user32.GetDC.restype = _HANDLE
_user32.ReleaseDC.argtypes = [_HANDLE, _HANDLE]
_user32.PrintWindow.argtypes = [_HANDLE, _HANDLE, wintypes.UINT]
_user32.PostMessageW.argtypes = [_HANDLE, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
_gdi32.CreateCompatibleDC.argtypes = [_HANDLE]
_gdi32.CreateCompatibleDC.restype = _HANDLE
_gdi32.CreateCompatibleBitmap.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = _HANDLE
_gdi32.SelectObject.argtypes = [_HANDLE, _HANDLE]
_gdi32.SelectObject.restype = _HANDLE
_gdi32.BitBlt.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, _HANDLE, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_gdi32.GetPixel.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_int]
_gdi32.GetPixel.restype = wintypes.DWORD
_gdi32.DeleteObject.argtypes = [_HANDLE]
_gdi32.DeleteDC.argtypes = [_HANDLE]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = _HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [_HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [_HANDLE]


class ScreenError(RuntimeError):
    """The game window could not be found, pictured or saved."""


@dataclass(frozen=True)
class GameWindow:
    hwnd: int
    width: int  # client area, in real pixels
    height: int


def screenshots_dir() -> Path:
    """Where screenshots are written: outside the repo, and emptied at the start of every Slack run (clear_screenshots)."""
    return Path(tempfile.gettempdir()) / "civ6-mcp" / "screenshots"


# Sending a screenshot to its player is the Slack bot's job (it has the token
# and knows whose DM the run reports to). The MCP server's send_screenshot
# tool only answers with SENT_PREFIX and the file; scripts/claude_runs.py
# reads that answer off the run's output and uploads the file. The marker
# file says such a run is listening, so the tool never claims a delivery
# nobody will make.
SENT_PREFIX = "Screenshot handed to Slack: "
_DELIVERY_MARKER = "slack-delivery"


def clear_screenshots() -> None:
    """Delete every screenshot and the delivery marker: one player's pictures are not for the next run to read."""
    folder = screenshots_dir()
    if not folder.is_dir():
        return
    for path in folder.iterdir():
        if path.suffix == ".jpg" or path.name == _DELIVERY_MARKER:
            path.unlink(missing_ok=True)


def set_delivery(on: bool) -> None:
    """Say whether a Slack run is listening for send_screenshot answers."""
    marker = screenshots_dir() / _DELIVERY_MARKER
    if on:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
    else:
        marker.unlink(missing_ok=True)


def delivery_on() -> bool:
    return (screenshots_dir() / _DELIVERY_MARKER).exists()


def screenshot_named(name: str) -> Path | None:
    """The screenshot with this file name (or path: only its last part counts), or None when there is none.

    Only a .jpg inside screenshots_dir() is ever returned, whatever was asked for.
    """
    path = screenshots_dir() / Path(name.strip().strip("\"'")).name
    return path if path.suffix == ".jpg" and path.is_file() else None


def sent_screenshot(tool_result: str) -> Path | None:
    """The file a send_screenshot answer handed over, or None when the answer is a refusal."""
    first = tool_result.strip().split("\n", 1)[0]
    return screenshot_named(first.removeprefix(SENT_PREFIX)) if first.startswith(SENT_PREFIX) else None


_dpi_aware = False


def _see_real_pixels() -> None:
    """Without this a scaled display (150%) reports, and captures, a shrunken window."""
    global _dpi_aware
    if _dpi_aware:
        return
    _dpi_aware = True
    try:
        _user32.SetProcessDpiAwarenessContext.argtypes = [_HANDLE]
        if not _user32.SetProcessDpiAwarenessContext(_HANDLE(-4)):  # per monitor, v2
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (OSError, AttributeError):  # set already, or an older Windows
        log.debug("could not set DPI awareness", exc_info=True)


def _process_name(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name
    finally:
        _kernel32.CloseHandle(handle)


def find_game_window() -> GameWindow:
    """The game's main window: the largest visible window of a Civilization VI process."""
    _see_real_pixels()
    found: list[GameWindow] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, _HANDLE, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not _process_name(pid.value).lower().startswith(GAME_EXE_PREFIX):
            return True
        rect = wintypes.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(rect))
        found.append(GameWindow(hwnd, rect.right - rect.left, rect.bottom - rect.top))
        return True

    _user32.EnumWindows(visit, 0)
    if not found:
        raise ScreenError("No Civilization VI window found. Is the game running?")
    window = max(found, key=lambda w: w.width * w.height)
    if _user32.IsIconic(window.hwnd) or window.width < 200 or window.height < 200:
        raise ScreenError("The Civilization VI window is minimized: someone at the PC has to restore it.")
    return window


def _is_blank(dc, width: int, height: int) -> bool:
    """True when a grid of sample pixels is all one colour: nothing was drawn."""
    first = _gdi32.GetPixel(dc, width // 2, height // 2)
    for i in range(1, 8):
        for j in range(1, 8):
            if _gdi32.GetPixel(dc, width * i // 8, height * j // 8) != first:
                return False
    return True


def capture(path: Path, window: GameWindow | None = None, quality: int = JPEG_QUALITY) -> GameWindow:
    """Save a JPEG of the game window's content to `path`. Returns the window pictured."""
    window = window or find_game_window()
    w, h = window.width, window.height
    window_dc = _user32.GetWindowDC(window.hwnd)
    mem_dc = _gdi32.CreateCompatibleDC(window_dc)
    bitmap = _gdi32.CreateCompatibleBitmap(window_dc, w, h)
    old = _gdi32.SelectObject(mem_dc, bitmap)
    try:
        ok = _user32.PrintWindow(window.hwnd, mem_dc, _PW_RENDERFULLCONTENT)
        if not ok or _is_blank(mem_dc, w, h):
            # Fall back on what the screen shows where the window is (only right while nothing covers the game).
            origin = wintypes.POINT(0, 0)
            _user32.ClientToScreen(window.hwnd, ctypes.byref(origin))
            screen_dc = _user32.GetDC(None)
            try:
                _gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, origin.x, origin.y, _SRCCOPY)
            finally:
                _user32.ReleaseDC(None, screen_dc)
            if _is_blank(mem_dc, w, h):
                raise ScreenError(
                    "The picture of the game window came out blank. The PC may be locked or its display off."
                )
        _gdi32.SelectObject(mem_dc, old)
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_jpeg(bitmap, path, quality)
    finally:
        _gdi32.SelectObject(mem_dc, old)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(window.hwnd, window_dc)
    return window


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]

    @classmethod
    def parse(cls, text: str) -> _GUID:
        guid = cls()
        ctypes.WinDLL("ole32").CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
        return guid


class _EncoderParameter(ctypes.Structure):
    _fields_ = [("guid", _GUID), ("count", wintypes.ULONG), ("type", wintypes.ULONG), ("value", ctypes.c_void_p)]


class _EncoderParameters(ctypes.Structure):
    _fields_ = [("count", wintypes.UINT), ("parameter", _EncoderParameter * 1)]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("version", wintypes.UINT),
        ("callback", ctypes.c_void_p),
        ("no_thread", wintypes.BOOL),
        ("no_codecs", wintypes.BOOL),
    ]


def _save_jpeg(bitmap, path: Path, quality: int) -> None:
    gdiplus = ctypes.WinDLL("gdiplus")
    gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [_HANDLE, _HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
    token = ctypes.c_void_p()
    status = gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(_GdiplusStartupInput(1, None, False, False)), None)
    if status != 0:
        raise ScreenError(f"GDI+ did not start (status {status}).")
    try:
        image = ctypes.c_void_p()
        status = gdiplus.GdipCreateBitmapFromHBITMAP(bitmap, None, ctypes.byref(image))
        if status != 0:
            raise ScreenError(f"GDI+ could not read the captured bitmap (status {status}).")
        try:
            level = wintypes.ULONG(quality)
            params = _EncoderParameters()
            params.count = 1
            params.parameter[0] = _EncoderParameter(
                _GUID.parse(_QUALITY_PARAMETER), 1, 4, ctypes.cast(ctypes.pointer(level), ctypes.c_void_p)  # 4 = ULONG
            )
            status = gdiplus.GdipSaveImageToFile(
                image, str(path), ctypes.byref(_GUID.parse(_JPEG_ENCODER)), ctypes.byref(params)
            )
            if status != 0:
                raise ScreenError(f"GDI+ could not write {path} (status {status}).")
        finally:
            gdiplus.GdipDisposeImage(image)
    finally:
        gdiplus.GdiplusShutdown(token)


def press_key(vk: int = VK_ESCAPE, window: GameWindow | None = None) -> None:
    """Press and release a key in the game window, whether or not it has the focus."""
    window = window or find_game_window()
    scan = _user32.MapVirtualKeyW(vk, 0)
    down = 1 | (scan << 16)
    _user32.PostMessageW(window.hwnd, _WM_KEYDOWN, vk, down)
    time.sleep(0.05)
    _user32.PostMessageW(window.hwnd, _WM_KEYUP, vk, down | (0xC0 << 24))
