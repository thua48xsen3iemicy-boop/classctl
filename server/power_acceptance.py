"""Exercise Chromium, both listeners and the live node with a substitute for busctl."""

import asyncio
import json
import os
import shutil
import sqlite3
import ssl
import tempfile
from argparse import Namespace
from contextlib import AsyncExitStack, closing
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web
from playwright.async_api import async_playwright, expect

from access_acceptance import identities
from live import Relay, power, ui_app
from node.live import main as run_node


async def listener(stack, app, tls=None):
    runner = web.AppRunner(app)
    await runner.setup()
    stack.push_async_callback(runner.cleanup)
    await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls).start()
    return runner.addresses[0][1]


async def stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def wait_state(client, origin, predicate):
    async with asyncio.timeout(5):
        while True:
            async with client.get(origin + "/state") as response:
                state = await response.json()
            if predicate(state):
                return state
            await asyncio.sleep(0.05)


async def check_ui(origin):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=shutil.which("chromium"), args=["--no-sandbox"],
        )
        page = await browser.new_page()
        await page.goto(origin)
        await page.locator("#sidebar-handle").focus()
        await page.keyboard.press("Enter")
        await page.locator("#sidebar-pin").click()
        select = page.locator("#power-node")
        shutdown = page.get_by_role("button", name="Выключить компьютер", exact=True)
        reboot = page.get_by_role("button", name="Перезагрузить компьютер", exact=True)
        status = page.locator("#power-status")
        await expect(shutdown).to_be_disabled()
        await expect(reboot).to_be_disabled()
        await select.select_option("pc-01")

        dialogs = []

        async def dismiss(dialog):
            dialogs.append(dialog.message)
            await dialog.dismiss()

        async def accept(dialog):
            dialogs.append(dialog.message)
            await dialog.accept()

        page.once("dialog", dismiss)
        await shutdown.click()
        await expect(status).to_be_empty()
        page.once("dialog", accept)
        await shutdown.click()
        await expect(status).to_have_text("pc-01 · Команда передана системе")
        await select.select_option("pc-02")
        page.once("dialog", accept)
        await reboot.click()
        await expect(status).to_have_text("pc-02 · Команда передана системе")
        assert [msg.split("?")[0] for msg in dialogs] == [
            "Выключить компьютер pc-01", "Выключить компьютер pc-01",
            "Перезагрузить компьютер pc-02",
        ]
        await browser.close()


async def run(tmp):
    contexts, ids = identities(tmp)
    path = tmp / "policy"
    policy = json.loads(path.read_text())
    policy["nodes"][ids["guest"]] = "pc-02"

    def authorize(rights):
        policy["teachers"] = {"local": {"rooms": {"lab-a": rights}}}
        new_policy = path.with_suffix(".new")
        new_policy.write_text(json.dumps(policy))
        new_policy.replace(path)

    authorize(["view", "control"])
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=tmp / "ca.pem")
    tls.load_cert_chain(tmp / "localhost.pem", tmp / "localhost.key")
    tls.verify_mode = ssl.CERT_REQUIRED
    calls = []

    def busctl(command, **kwargs):
        assert command[:6] == [
            "busctl", "--system", "call", "org.freedesktop.login1",
            "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
        ]
        assert command[7:] == ["b", "false"]
        assert kwargs["check"]
        calls.append(command[6])

    async with AsyncExitStack() as stack, ClientSession() as client:
        stack.enter_context(patch("node.power.subprocess.run", side_effect=busctl))
        relay = Relay(Namespace(db=tmp / "db", policy=path, ice=None))
        stack.callback(relay.db.close)
        sources = web.Application()
        sources.router.add_get("/source", relay.source)
        agent_port = await listener(stack, sources, tls)
        origin = f"http://127.0.0.1:{await listener(stack, ui_app(relay))}"
        agent_origin = f"https://localhost:{agent_port}"
        tasks = {}
        with patch.object(os, "geteuid", return_value=0):
            for node, cert in [("pc-01", "pc"), ("pc-02", "guest")]:
                task = asyncio.create_task(run_node(Namespace(
                    node=node, cert=tmp / f"{cert}.pem", key=tmp / f"{cert}.key",
                    server=agent_origin, socket=str(tmp / f"{node}.sock"),
                    power_db=tmp / f"{node}.sqlite", ca=tmp / "ca.pem",
                    test_session=False, uid=None,
                )))
                tasks[node] = task
                stack.push_async_callback(stop, task)
            state = await wait_state(client, origin, lambda state: all(
                node["availability"] == "online" for node in state["nodes"].values()
            ))
        assert all(node["session"] is None for node in state["nodes"].values())
        assert state["powerNodes"] is None

        command = dict(actionId="system.shutdown", nodeId="pc-01")
        endpoint = origin + "/power"
        async with client.post(endpoint, json=command, headers={"Origin": origin}) as response:
            assert response.status == 403
        authorize(["view", "system.power"])
        await wait_state(client, origin, lambda state: state["powerNodes"] == ["pc-01", "pc-02"])
        async with client.post(endpoint, json=command, headers={"Origin": "https://other.example"}) as response:
            assert response.status == 403
        async with client.post(endpoint, json=dict(command, parameters={}), headers={"Origin": origin}) as response:
            assert response.status == 400
        async with client.post(agent_origin + "/power", json=command, ssl=contexts["pc"]) as response:
            assert response.status == 404
        assert calls == []

        await check_ui(origin)
        assert calls == ["PowerOff", "Reboot"]
        for node, action in [("pc-01", "system.shutdown"), ("pc-02", "system.reboot")]:
            with closing(sqlite3.connect(tmp / f"{node}.sqlite")) as db:
                saved, = db.execute("SELECT request FROM power").fetchall()
            request = json.loads(saved[0])
            assert request["nodeIds"] == [node] and request["actionId"] == action
        rows = relay.db.execute("SELECT target, outcome FROM audit WHERE event = 'power'").fetchall()
        assert [(json.loads(target)["nodeId"], outcome) for target, outcome in rows] == [
            ("pc-01", "issued"), ("pc-01", "succeeded"),
            ("pc-02", "issued"), ("pc-02", "succeeded"),
        ]
        await stop(tasks["pc-02"])
        await wait_state(client, origin, lambda state: state["powerNodes"] == ["pc-01"])

        async def send_to_pc02():
            async with client.post(endpoint, json=dict(command, nodeId="pc-02"),
                                   headers={"Origin": origin}) as response:
                assert response.status == 200
                outcome, = (await response.json())["outcomes"]
                return outcome

        assert await send_to_pc02() == dict(nodeId="pc-02", state="rejected", reason="offline")
        async with client.ws_connect(agent_origin + "/source", ssl=contexts["guest"]) as source:
            await source.send_json(state["nodes"]["pc-02"])
            await wait_state(client, origin, lambda state: state["nodes"]["pc-02"]["availability"] == "online")
            assert (await send_to_pc02())["reason"] == "unsupported"
            await source.send_json(dict(version=1, kind="capabilities", nodeId="pc-02",
                                        features=[], actions=power.CAPABILITIES))
            await wait_state(client, origin, lambda state: state["powerNodes"] == ["pc-01", "pc-02"])
            pending = asyncio.create_task(send_to_pc02())
            stack.push_async_callback(stop, pending)
            dispatched = await source.receive_json(timeout=3)
            assert dispatched["nodeIds"] == ["pc-02"]
            assert (await send_to_pc02())["reason"] == "busy"
        assert await pending == dict(nodeId="pc-02", state="unknown", reason="unconfirmed")
        assert calls == ["PowerOff", "Reboot"]
    print("T15: Chromium, отмена, права, mTLS, два адресных действия, аудит и потеря связи OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(run(Path(directory)))
