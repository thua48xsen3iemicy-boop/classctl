"""Run from the checkout with python3 checks/verify.py."""
import json
import re
import runpy
import sys
from preview import check_preview
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def unique(items, context):
    require(len(items) == len(set(items)), f"Дубликат в {context}")


def check_input():
    schema=read(CONTRACTS/"input-v1.schema.json")
    Draft202012Validator.check_schema(schema)
    validator=Draft202012Validator(schema)
    keys=read(CONTRACTS/"input-keys.json")
    for code in keys:
        validator.validate({"kind":"key","code":code,"down":True})
    validator.validate({"kind":"button","u":0,"v":0.99,"down":False,"button":0})
    wheel = {"kind": "wheel", "steps": -2, "u": 0.5, "v": 0.5}
    validator.validate(wheel)
    for steps in (0, -9, 9, 1.5):
        require(not validator.is_valid(dict(wheel, steps=steps)), "Приняты неверные шаги колеса")
    require(not validator.is_valid(dict(wheel, u=1)), "Колесо принято за полем изображения")
    for event in [{"kind":"move","u":1,"v":0},{"kind":"key","down":True,"code":"Unknown"},
                  {"kind":"release","nodeId":"other"}]:
        require(not validator.is_valid(event),"Несовместимое событие ввода принято")


def check_session_identity():
    schema = read(CONTRACTS / "session-identity-v1.schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    legacy = dict(op="identity", sessionId="login-7", hostname="lab-pc", user="student")
    validator.validate(legacy)
    validator.validate(dict(legacy, user=None, displayName="Анна Иванова"))
    validator.validate(dict(legacy, displayName=None))
    for invalid_name in ("", "а" * 256, ["Анна Иванова"]):
        require(
            not validator.is_valid(dict(legacy, displayName=invalid_name)),
            "Принято неверное имя пользователя сессии",
        )


def check_power():
    common = read(CONTRACTS / "v1.schema.json")
    schema = read(CONTRACTS / "power-v1.schema.json")
    Draft202012Validator.check_schema(schema)
    portal = Draft202012Validator(schema, registry=Registry().with_resource(
        "urn:classctl:control:v1", Resource.from_contents(common),
    ))
    portal.validate(read(CONTRACTS / "power/portal.json"))
    require(not portal.is_valid(read(CONTRACTS / "power/invalid-portal.json")),
            "Портал принял произвольные параметры выключения")
    parameters = portal.evolve(schema=schema["$defs"]["parameters"])
    parameters.validate(read(CONTRACTS / "examples/power-request.json")["parameters"])
    require(not parameters.is_valid({"force": True}), "Служба принимает параметры выключения")
    print("Схема запроса питания: OK")


def check_examples():
    schema = read(CONTRACTS / "v1.schema.json")
    Draft202012Validator.check_schema(schema)
    formats = FormatChecker()
    require(not formats.conforms("invalid", "date-time"), "Недоступна проверка времени")
    validator = Draft202012Validator(schema, format_checker=formats)
    examples = {}
    for path in sorted((CONTRACTS / "examples").glob("*.json")):
        data = read(path)
        errors = list(validator.iter_errors(data))
        require(not errors, f"{path.name}: {errors[0].message if errors else ''}")
        examples[path.stem] = data

    cases = {
        "offline-session": ("status", ["session"], "type"),
        "missing-monitor": ("access", ["target"], "oneOf"),
        "duplicate-target": ("request", ["nodeIds"], "uniqueItems"),
    }
    invalid = CONTRACTS / "invalid"
    require(set(cases) == {p.stem for p in invalid.glob("*.json")}, "Неполный список отказов")
    for name, (branch, location, rule) in cases.items():
        data = read(invalid / f"{name}.json")
        require(not validator.is_valid(data), f"Принят {name}")
        sub = validator.evolve(schema=schema["$defs"][branch])
        errors = sub.iter_errors(data)
        require(any(list(e.path) == location and e.validator == rule for e in errors),
                f"{name}: ожидался отказ по {location} ({rule})")

    seats = examples["room"]["seats"]
    unique([s["seatId"] for s in seats], "seatId")
    unique([(s["row"], s["column"]) for s in seats], "позициях")
    nodes = [s["nodeId"] for s in seats if s["nodeId"] is not None]
    unique(nodes, "привязках")
    status, access = examples["status"], examples["access"]
    session = status["session"]
    monitors = [m["monitorId"] for m in session["monitors"]]
    unique(monitors, "мониторах")
    require(len(monitors) == 2, "Эталонное место содержит два монитора")
    require(status["nodeId"] == access["nodeId"] == examples["capabilities"]["nodeId"]
            and access["nodeId"] in nodes, "Несогласованный узел")
    require(access["sessionId"] == session["sessionId"]
            and access["layoutRevision"] == session["layoutRevision"]
            and access["target"]["monitorId"] in monitors, "Несогласованный выбор монитора")

    request, result = examples["request"], examples["result"]
    outcomes = result["outcomes"]
    targets = [o["nodeId"] for o in outcomes]
    unique(targets, "результатах")
    require(request["requestId"] == result["requestId"] and
            set(targets) == set(request["nodeIds"]) <= set(nodes), "Потеряна цель запроса")
    require({o["state"] for o in outcomes} == {"succeeded", "rejected"}, "Ожидался смешанный итог")
    action, = examples["capabilities"]["actions"]
    require(action["actionId"] == request["actionId"] == "demo.echo"
            and action["parameterSchema"] == "demo.echo.v1", "Неизвестное действие")
    ttl = datetime.fromisoformat(request["expiresAt"]) - datetime.fromisoformat(request["issuedAt"])
    require(0 < ttl.total_seconds() <= action["maxTtlSeconds"], "Недопустимый TTL")
    demo = ROOT / "actions/demo.echo"
    params = read(demo / "parameters.schema.json")
    Draft202012Validator.check_schema(params)
    parameters = Draft202012Validator(params)
    parameters.validate(request["parameters"])
    require(not parameters.is_valid({"label": "ok", "command": "extra"}), "Принят лишний параметр")
    output = runpy.run_path(str(demo / "demo.py"))["run"](request["parameters"])
    require(output == request["parameters"], "Демо вернуло другую метку")
    print(f"Контракты: {len(examples)} примеров, {len(cases)} ожидаемых отказа. Связи и demo.echo: OK")


def check_links():
    count = 0
    for doc in ROOT.rglob("*.md"):
        if any(part.startswith(".") for part in doc.relative_to(ROOT).parts):
            continue
        for dest in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", doc.read_text()):
            url = urlsplit(dest)
            if url.scheme or url.netloc:
                continue
            target = (doc.parent / unquote(url.path)).resolve() if url.path else doc
            require(target.is_relative_to(ROOT) and target.exists(), f"{doc.name}: {dest}")
            require(not url.fragment, f"{doc.name}: используйте ссылку на файл целиком ({dest})")
            count += 1
    print(f"Внутренние ссылки Markdown: {count} OK")


if __name__ == "__main__":
    try:
        check_examples()
        check_preview(ROOT)
        check_input()
        check_session_identity()
        check_power()
        check_links()
    except (ValueError, KeyError, OSError) as exc:
        sys.exit(f"Ошибка проверки: {exc}")
