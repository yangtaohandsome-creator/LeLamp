"""Run the isolated local console demo: python -m lelamp.web --demo."""
import argparse
import asyncio
from pathlib import Path
import tempfile

from aiohttp import web

from .api import WebConsole
from .demo import DemoApp


def main():
    parser = argparse.ArgumentParser(description="LeLamp 本机网页模拟，不连接硬件")
    parser.add_argument("--demo", required=True, action="store_true")
    parser.add_argument("--port", type=int, default=18793)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="lelamp-web-demo-")
    async def build():
        app = DemoApp(Path(temporary.name))
        console = WebConsole(app, maintenance=app.maintenance)
        server = web.Application(client_max_size=32 * 1024)
        console.routes(server.router)
        async def lifecycle(_):
            await app.announcements.start()
            await app.alarms.start()
            await console.start()
            try:
                yield
            finally:
                await console.close()
                await app.close()
        server.cleanup_ctx.append(lifecycle)
        return server
    try:
        web.run_app(build(), host="127.0.0.1", port=args.port)
    finally:
        temporary.cleanup()


if __name__ == "__main__":
    main()
