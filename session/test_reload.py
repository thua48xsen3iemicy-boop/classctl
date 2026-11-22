"""Check the agent's exec handoff on a disposable local Unix socket."""

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]


async def stop(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


class ReloadAcceptance(unittest.IsolatedAsyncioTestCase):
    async def test_switch_release_in_same_process(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        first, second = (directory / name for name in ("first", "second"))
        for release in (first, second):
            (release / "session").mkdir(parents=True)
            for name in ("live.py", "remote_input.py"):
                shutil.copyfile(ROOT / "session" / name, release / "session" / name)
            (release / "contracts").symlink_to(ROOT / "contracts", target_is_directory=True)
            (release / ".venv").symlink_to(sys.prefix, target_is_directory=True)

        current = directory / "current-session"
        current.symlink_to(first, target_is_directory=True)
        socket_path = directory / "seat0.sock"
        events = asyncio.Queue()

        async def session(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            status = await ws.receive_json(timeout=5)
            session_id = status["session"]["sessionId"]
            events.put_nowait(("opened", session_id))
            try:
                async for _ in ws:
                    pass
            finally:
                events.put_nowait(("closed", session_id))
            return ws

        app = web.Application()
        app.router.add_get("/session", session)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.UnixSite(runner, str(socket_path)).start()
        process = await asyncio.create_subprocess_exec(
            first / ".venv/bin/python", first / "session/live.py",
            "--socket", socket_path, "--node", "pc-01", "--synthetic",
            "--release-link", current,
        )
        self.addAsyncCleanup(stop, process)
        cmdline = Path("/proc") / str(process.pid) / "cmdline"

        async with asyncio.timeout(10):
            event, first_id = await events.get()
            self.assertEqual(event, "opened")
            self.assertIn(bytes(first / "session/live.py"), cmdline.read_bytes().split(b"\0"))
            next_link = directory / "next-session"
            next_link.symlink_to(second, target_is_directory=True)
            next_link.replace(current)
            self.assertEqual(await events.get(), ("closed", first_id))
            event, second_id = await events.get()
            self.assertEqual(event, "opened")
            self.assertNotEqual(first_id, second_id)
            self.assertIn(bytes(second / "session/live.py"), cmdline.read_bytes().split(b"\0"))


if __name__ == "__main__":
    unittest.main()
