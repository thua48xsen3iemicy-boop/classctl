"""Summarize measured samples. CPU percent is relative to one logical core."""

import json
import math
import sys


def percentile(values, quantile):
    if not values:
        return float("nan")
    values = sorted(values)
    return values[math.ceil(len(values) * quantile) - 1]


def summarize(data):
    cfg = data["config"]
    streams = cfg["visible"] * cfg["outputs"]
    elapsed = data["end"] - data["start"]
    captures = {(i, n): t for i, n, t in data["source"]["captures"]}
    drawn = {(i, n): t for i, n, t in data["draws"]}
    if not drawn or drawn.keys() - captures.keys():
        raise ValueError("Empty display or unknown decoded frame ID")
    ages = [t - captures[key] for key, t in drawn.items()]
    first, last = data["samples"][0], data["samples"][-1]
    resources = []
    for role in ("source", "browser", "relay"):
        cpu = (last[1][role][0] - first[1][role][0]) * 100 / (last[0] - first[0])
        rss = max(s[1][role][1] for s in data["samples"]) / (1024**2)
        if role == "relay" and "turn" in first[1]:
            cpu += (
                (last[1]["turn"][0] - first[1]["turn"][0]) * 100 / (last[0] - first[0])
            )
            rss = (
                max(s[1]["turn"][1] + s[1]["relay"][1] for s in data["samples"])
                / 1024**2
            )
        resources.append(f"{cpu:.0f}/{rss:.0f}")
    src, relay = data["source"], data["relay"]
    rtc = next((r for r in data.get("rtcReports", []) if r["type"] == "transport"), {})
    server_rx = relay["rx"] + src.get("rtcTx", 0) + rtc.get("bytesSent", 0)
    server_tx = relay["tx"] + src.get("rtcRx", 0) + rtc.get("bytesReceived", 0)
    rate = lambda n: round(n * 8 / elapsed / 1000)
    clock = min(data["clocks"], key=lambda p: p[2] - p[0])
    bound = max(abs(clock[1] - clock[0]), abs(clock[2] - clock[1])) + 1
    fps = min(sum(i == s for i, n in drawn) / elapsed for s in range(streams))
    stale = [s[2] for s in data["stalls"] if s[0] == "stale"]
    recovery = [s[1] for s in data["stalls"] if s[0] == "recovered"]
    after = [t for key, t in drawn.items() if recovery and captures[key] >= recovery[0]]
    recovery_ms = round(min(after) - recovery[0]) if after else "—"
    columns = [
        cfg["outputs"],
        cfg["scenario"],
        cfg["transport"],
        f"{fps:.1f}",
        f"{percentile(ages, 0.50):.0f}/{percentile(ages, 0.95):.0f}",
        len(captures) - len(drawn),
        *resources,
        f"{rate(src.get('rtcRx', 0))}/{rate(src['tx'] + src.get('rtcTx', 0))}",
        f"{rate(server_rx)}/{rate(server_tx)}",
        f"{max(src['queuePeak'], relay['queuePeak'])}/{src['replaced'] + relay['replaced']}",
        f"{stale[0] if stale else '—'}/{recovery_ms}",
        f"{bound:.1f}",
    ]
    return "| " + " | ".join(map(str, columns)) + " |"


print(
    "| Выходов | Сценарий | Путь | Мин. FPS | Возраст p50/p95, мс | Без показа | CPU/RSS источник | Браузер | Сервер | ПК ↓/↑ | Сервер ↓/↑ | Очередь/замены | stale/возврат, мс | Часы ±мс |"
)
print("|" + "---|" * 14)
with open(sys.argv[1]) as f:
    for line in f:
        print(summarize(json.loads(line)))
