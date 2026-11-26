"""Desktop launcher for Windows (.exe) and Linux: runs the Web Dashboard, Bot Service, and System Tray."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn
from PIL import Image, ImageDraw

from app.config import load_desktop_settings, resolve_data_dir
from app.service_manager import BotServiceManager
from app.web_ui import create_web_app


def _setup_logging(data_dir: Path) -> None:
    """Ensure stdout/stderr are safe in --noconsole Windows PyInstaller builds and log to file."""
    log_file = data_dir / "lotterybot.log"
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(log_file, "a", encoding="utf-8")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def _create_tray_image() -> Image.Image:
    """Create a crisp 64x64 lottery icon image for the Windows system tray."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        ico_path = Path(sys._MEIPASS) / "app" / "lottery.ico"
    else:
        ico_path = Path(__file__).resolve().parent / "app" / "lottery.ico"
    if ico_path.exists():
        try:
            return Image.open(ico_path).convert("RGBA")
        except Exception:
            pass

    img = Image.new("RGBA", (64, 64), (15, 23, 42, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(29, 78, 216, 255))
    d.rounded_rectangle([14, 20, 50, 44], radius=6, fill=(16, 185, 129, 255))
    d.ellipse([10, 28, 18, 36], fill=(29, 78, 216, 255))
    d.ellipse([46, 28, 54, 36], fill=(29, 78, 216, 255))
    return img


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    for bind_host in (host, "0.0.0.0"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((bind_host, port))
            except OSError:
                return True
    return False


def _check_existing_instance(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("app") == "lotterybot"
    except Exception:
        return False


def _find_existing_instance(preferred: int, host: str = "127.0.0.1") -> int | None:
    for port in range(preferred, preferred + 20):
        if _port_in_use(port, host) and _check_existing_instance(port):
            return port
    return None


def _pick_free_port(preferred: int, host: str = "127.0.0.1") -> int:
    for port in range(preferred, preferred + 20):
        if not _port_in_use(port, host):
            return port
    return preferred


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LotteryBot Desktop Launcher & Web Control Center")
    parser.add_argument("--host", default=None, help="Host IP to bind (overrides settings/WEB_HOST)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind (overrides settings/WEB_PORT)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    parser.add_argument("--no-tray", action="store_true", help="Run without system tray icon")
    parser.add_argument("--data-dir", default=None, help="Custom persistent lottery_data directory")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.data_dir:
        os.environ["LOTTERY_DATA_DIR"] = str(Path(args.data_dir).resolve())

    data_dir = resolve_data_dir()
    _setup_logging(data_dir)
    log = logging.getLogger("launcher")

    settings = load_desktop_settings(data_dir)
    preferred_port = int(args.port or os.environ.get("WEB_PORT") or settings.web_port or 8000)

    # If already running on any candidate port, open the dashboard in browser and exit
    existing_port = _find_existing_instance(preferred_port)
    if existing_port is not None:
        if not args.no_browser and os.environ.get("LOTTERY_NO_BROWSER") != "1":
            webbrowser.open(f"http://127.0.0.1:{existing_port}")
        return

    bind_host = str(args.host or os.environ.get("WEB_HOST") or settings.web_host or "127.0.0.1")
    port = _pick_free_port(preferred_port, bind_host)
    url = f"http://127.0.0.1:{port}"

    manager = BotServiceManager(data_dir=data_dir, settings=settings)
    app = create_web_app(manager)

    # IMPORTANT: log_config=None avoids uvicorn tty crashes in --noconsole PyInstaller mode
    config = uvicorn.Config(app, host=bind_host, port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True, name="uvicorn-web")
    server_thread.start()

    # Wait until web server is up, then open browser
    for _ in range(50):
        if _check_existing_instance(port):
            break
        time.sleep(0.1)

    log.info("Web Control Center listening on %s (bind=%s:%s)", url, bind_host, port)
    if not args.no_browser and os.environ.get("LOTTERY_NO_BROWSER") != "1":
        webbrowser.open(url)

    def _run_coro(coro):
        loop = getattr(server, "loop", None)
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)

    # Run Windows System Tray icon if available, otherwise wait on server thread
    if args.no_tray or os.environ.get("LOTTERY_NO_TRAY") == "1":
        server_thread.join()
        return

    try:
        import pystray

        def on_open(_icon, _item):
            webbrowser.open(url)

        def on_start(_icon, _item):
            _run_coro(manager.start())

        def on_stop(_icon, _item):
            _run_coro(manager.stop())

        def on_exit(icon, _item):
            server.should_exit = True
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem("Open Web Dashboard", on_open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start Bot Service", on_start),
            pystray.MenuItem("Stop Bot Service", on_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit LotteryBot", on_exit),
        )
        icon = pystray.Icon("LotteryBot", _create_tray_image(), f"LotteryBot ({url})", menu)
        icon.run()
        server.should_exit = True
        server_thread.join(timeout=5)
    except Exception as e:
        log.warning("System tray unavailable (%s); running headless web server at %s", e, url)
        try:
            while server_thread.is_alive():
                server_thread.join(timeout=1.0)
        except KeyboardInterrupt:
            server.should_exit = True
            server_thread.join(timeout=5)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
