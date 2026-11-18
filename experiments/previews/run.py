"""Run one synthetic classroom series and preserve raw observations."""

import argparse
import asyncio
import json
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

import psutil
from aiohttp import ClientSession
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def process(stack, args):
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop():
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        if p.returncode not in (0, -15):
            print(p.stderr.read().decode()[-2000:], file=sys.stderr)
        p.stderr.close()

    stack.callback(stop)
    return p


async def ready(client, url, proc):
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError("Child exited during startup")
        try:
            async with client.get(url, ssl=False) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        await asyncio.sleep(0.05)
    raise TimeoutError(url)


def sample(pid):
    parent = psutil.Process(pid)
    cpu = rss = 0
    for child in [parent, *parent.children(recursive=True)]:
        try:
            times = child.cpu_times()
            cpu += times.user + times.system
            rss += child.memory_info().rss
        except psutil.NoSuchProcess:
            pass
    return [cpu, rss]


async def subscribe(page, cfg, client):
    await page.goto(cfg["relay"])
    offer = await page.evaluate("init", cfg)
    if offer:
        async with client.post(cfg["sourceUrl"] + "/offer", json=offer, ssl=False) as r:
            answer = await r.json()
        await page.evaluate("answer=>peer.setRemoteDescription(answer)", answer)
        await page.wait_for_function("peer.connectionState==='connected'")


async def run(c, tmp, client, pw):
    with ExitStack() as stack:
        ports = {name: port() for name in ("sourcePort", "relayPort", "debugPort")}
        cfg = {**c, **ports, "cert": str(tmp / "cert"), "key": str(tmp / "key")}
        for role in ("relay", "source"):
            cfg[role + "Url"] = f"https://127.0.0.1:{cfg[role + 'Port']}"
        cfg["relay"] = cfg["relayUrl"]
        examples = HERE.parents[1] / "contracts/examples"
        status = json.loads((examples / "status.json").read_text())
        room = json.loads((examples / "room.json").read_text())
        seats = [
            dict(seatId=f"desk-{n + 1}", nodeId=f"pc-{n + 1:02}", row=1, column=n + 1)
            for n in range(c["seats"])
        ]
        room["seats"] = seats
        streams = [
            dict(
                seatId=seat["seatId"],
                nodeId=seat["nodeId"],
                sessionId=status["session"]["sessionId"],
                layoutRevision=status["session"]["layoutRevision"],
                monitorId=monitor["monitorId"],
            )
            for seat in seats[: c["visible"]]
            for monitor in status["session"]["monitors"][: c["outputs"]]
        ]
        config = tmp / "config.json"
        config.write_text(json.dumps(cfg))
        processes = {}
        if c["transport"] == "rtc":
            cfg["turnPort"] = port()
            cfg["turnPassword"] = secrets.token_hex(16)
            args = [
                "turnserver",
                "-n",
                "--no-cli",
                "--no-tls",
                "--no-dtls",
                "--no-udp",
                "--no-multicast-peers",
                "--allow-loopback-peers",
                "--lt-cred-mech",
                "--realm=localhost",
                "--listening-ip=127.0.0.1",
                "--relay-ip=127.0.0.1",
                "--relay-threads=1",
                "--log-file=stdout",
                f"--pidfile={tmp}/turn.pid",
                f"--listening-port={cfg['turnPort']}",
                "--user=pupil:" + cfg["turnPassword"],
            ]
            processes["turn"] = process(stack, args).pid

        for role in ("relay", "source"):
            p = process(stack, [sys.executable, HERE / "peer.py", role, config])
            processes[role] = p.pid
            await ready(client, cfg[role + "Url"] + "/metrics", p)
        profile = stack.enter_context(tempfile.TemporaryDirectory(dir=tmp))
        p = process(
            stack,
            [
                shutil.which("chromium"),
                "--headless",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--ignore-certificate-errors",
                "--disable-background-networking",
                "--allow-loopback-in-peer-connection",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={ports['debugPort']}",
            ],
        )
        processes["browser"] = p.pid
        debug = f"http://127.0.0.1:{ports['debugPort']}"
        await ready(client, debug + "/json/version", p)
        browser = await pw.chromium.connect_over_cdp(debug)
        page = await browser.contexts[0].new_page()
        await subscribe(page, cfg, client)
        probes = []
        for _ in range(7):
            before = time.time_ns() / 1e6
            clock = await page.evaluate("Date.now()")
            after = time.time_ns() / 1e6
            probes.append([before, clock, after])
        async with client.post(cfg["sourceUrl"] + "/start", ssl=False) as r:
            r.raise_for_status()
        start = time.time()
        samples = []
        while time.time() - start < c["duration"]:
            samples.append(
                [time.time(), {role: sample(pid) for role, pid in processes.items()}]
            )
            await asyncio.sleep(0.2)
        end = time.time()
        data = await page.evaluate("({draws,checks,stalls})")
        for role in ("source", "relay"):
            async with client.get(cfg[role + "Url"] + "/metrics", ssl=False) as r:
                data[role] = await r.json()
        if c["transport"] == "rtc":
            data[
                "rtcReports"
            ] = await page.evaluate("""async()=>Array.from((await peer.getStats()).values())
                .filter(x=>['candidate-pair','codec','inbound-rtp','transport','local-candidate'].includes(x.type))
                .map(({address,ip,port,usernameFragment,...metrics})=>metrics)""")
        data.update(
            room=room,
            streams=streams,
            clocks=probes,
            config=c,
            start=start,
            end=end,
            samples=samples,
            browser=browser.version,
            host=platform.platform(),
        )
        if c["scenario"] == "outage":
            await page.evaluate("()=>{socket.close();peer?.close()}")
            await asyncio.sleep(0.7)
            started = time.time_ns() / 1e6
            await subscribe(page, cfg, client)
            await page.wait_for_function(
                "new Set(draws.map(row=>row[0])).size===surfaces.length"
            )
            data["resubscribe"] = dict(
                started=started, draws=await page.evaluate("draws")
            )
        await page.close()
        await browser.close()
        return data


async def main():
    parser = argparse.ArgumentParser()
    for key, default in [("seats", 2), ("visible", 2), ("fps", 8), ("duration", 5)]:
        parser.add_argument("--" + key, type=int, default=default)
    parser.add_argument(
        "--outputs", type=int, nargs="+", choices=[1, 2], default=[1, 2]
    )
    parser.add_argument("--source", type=int, nargs=2, default=[1280, 720])
    parser.add_argument("--thumb", type=int, nargs=2, default=[320, 180])
    parser.add_argument("--mbps", type=float, default=0)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=("moving", "static", "slow", "outage"),
        default=["moving", "static", "slow", "outage"],
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not (
        1 <= args.visible <= args.seats
        and args.duration >= 4
        and args.fps > 0
        and args.duration * args.fps < 65536
        and args.thumb[0] >= 128
        and args.thumb[1] >= 16
        and min(args.source) > 0
        and args.mbps >= 0
    ):
        parser.error("Invalid geometry, timing, subscription or bandwidth")
    config = vars(args).copy()
    out = config.pop("out")
    outputs = config.pop("outputs")
    scenarios = config.pop("scenarios")
    with tempfile.TemporaryDirectory() as directory, out.open("x") as result:
        tmp = Path(directory)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-keyout",
                tmp / "key",
                "-out",
                tmp / "cert",
            ],
            check=True,
            stderr=subprocess.DEVNULL,
        )
        async with ClientSession() as client, async_playwright() as pw:
            for n in outputs:
                for scenario in scenarios:
                    for transport in ("rtc", "jpeg"):
                        c = dict(
                            config, outputs=n, scenario=scenario, transport=transport
                        )
                        data = await run(c, tmp, client, pw)
                        print(
                            json.dumps(data, separators=(",", ":")),
                            file=result,
                            flush=True,
                        )
                        print(n, scenario, transport, len(data["draws"]), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
