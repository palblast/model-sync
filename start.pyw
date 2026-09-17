#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy model-sync 启动器（无窗口版）。

双击此文件即可启动后端服务并打开浏览器。
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(BASE_DIR, "server.py")
PORT = "7788"


def kill_port(port: str) -> None:
    """杀掉占用指定端口的进程。"""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        pids = set()
        for line in (result.stdout or "").splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                if parts:
                    pids.add(parts[-1])
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/f", "/pid", pid],
                               capture_output=True, timeout=3,
                               encoding="utf-8", errors="replace")
            except Exception:
                pass
    except Exception:
        pass


def wait_for_port(port: str, timeout: int = 8) -> bool:
    """等待端口可连接。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    return False


def main():
    # 1. 清理端口
    kill_port(PORT)

    # 2. 启动 server.py
    #    DEVNULL 丢弃所有输出，避免管道读取导致的编码问题
    #    CREATE_NO_WINDOW 确保不弹任何窗口
    subprocess.Popen(
        [sys.executable, SERVER_PY, "--port", PORT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000,
    )

    # 3. 等待服务就绪
    ready = wait_for_port(PORT, timeout=8)

    # 4. 打开浏览器
    webbrowser.open(f"http://127.0.0.1:{PORT}/")

    if not ready:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, f"后端服务启动失败，请检查端口 {PORT} 是否被占用",
            "WorkBuddy Model Sync", 0x10,
        )


if __name__ == "__main__":
    main()