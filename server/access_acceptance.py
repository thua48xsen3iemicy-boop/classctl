"""Exercise T06 over mutual TLS using disposable identities."""

import hashlib
import json
import sqlite3
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent / "contracts/examples"


def identities(tmp):
    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=tmp, check=True, capture_output=True)

    openssl(
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:P-256",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-subj",
        "/CN=test-CA",
        "-days",
        "1",
    )
    ids = {}
    contexts = {}
    for name in ("localhost", "teacher", "guest", "pc"):
        openssl(
            "req",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            name + ".key",
            "-out",
            "request",
            "-subj",
            "/CN=" + name,
        )
        openssl(
            "x509",
            "-req",
            "-in",
            "request",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            name + ".pem",
            "-days",
            "1",
        )
        cert = tmp / (name + ".pem")
        ids[name] = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(cert.read_text())
        ).hexdigest()
        ctx = ssl.create_default_context(cafile=tmp / "ca.pem")
        ctx.load_cert_chain(cert, tmp / (name + ".key"))
        contexts[name] = ctx
    policy = {
        "teachers": {
            ids["teacher"]: {"rooms": {"lab-a": ["view"]}},
            ids["guest"]: {"rooms": {"lab-b": ["view"]}},
        },
        "nodes": {ids["pc"]: "pc-01"},
    }
    path = tmp / "policy"
    path.write_text(json.dumps(policy))
    return contexts, ids


def test(tmp):
    contexts, ids = identities(tmp)
    policy = json.loads((tmp / "policy").read_text())
    path = tmp / "policy"
    with subprocess.Popen(
        [
            sys.executable,
            HERE / "secure.py",
            "--db",
            "inventory",
            "--port",
            "0",
            "--ca",
            "ca.pem",
            "--key",
            "localhost.key",
            "--cert",
            "localhost.pem",
            "--policy",
            "policy",
        ],
        cwd=tmp,
        stdout=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            port = int(proc.stdout.readline())

            def call(identity, route, data=None, expected=200):
                body = json.dumps(data).encode() if data is not None else None
                try:
                    with urlopen(
                        Request(f"https://localhost:{port}/{route}", data=body),
                        context=contexts[identity],
                        timeout=3,
                    ) as response:
                        assert expected == response.status
                        return json.load(response)
                except HTTPError as error:
                    assert error.code == expected, (route, error.code, expected)

            status, caps, access = [
                json.loads((EXAMPLES / (name + ".json")).read_text())
                for name in ("status", "capabilities", "access")
            ]
            call("pc", "nodes/pc-02/status", status, 403)
            call("pc", "nodes/pc-01/status", status)
            caps["features"].append("preview.jpeg.v1")
            call("pc", "nodes/pc-01/capabilities", caps)
            call("teacher", "room")
            call("guest", "room", expected=403)
            call("guest", "grant", access, 403)
            call("teacher", "grant", {**access, "mode": "control"}, 403)
            call("teacher", "grant", {**access, "layoutRevision": 2}, 403)
            sub = json.loads((EXAMPLES.parent / "preview/sequence.json").read_text())[0]
            call("guest", "subscribe", sub, 403)
            sub_ticket = call("teacher", "subscribe", sub)["ticket"]
            assert call("pc", "open", {"token": sub_ticket})["access"] == sub
            ticket = call("teacher", "grant", access)["ticket"]
            call("guest", "open", {"token": ticket}, 403)
            opened = call("pc", "open", {"token": ticket})
            assert opened["access"] == access
            call("pc", "open", {"token": ticket}, 403)
            lease = {"token": opened["handle"]}
            assert call("pc", "active", lease) == access
            policy["teachers"].clear()
            path.write_text(json.dumps(policy))
            call("pc", "active", lease, 403)
            with sqlite3.connect(tmp / "inventory") as db:
                assert db.execute(
                    "SELECT count(*) FROM audit WHERE subject=? AND outcome='revoked' AND target LIKE ?",
                    (ids["teacher"], "%DP-2%"),
                ).fetchone() == (2,)
        finally:
            proc.terminate()
    print(
        "T06: mutual TLS, room ACL, node binding, grant replay, revision, revocation and audit OK"
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        test(Path(directory))
