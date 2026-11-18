import json
import subprocess

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


def check_preview(root):
    def read(path):
        return json.loads((root / path).read_text())

    schema = read("contracts/preview-v1.schema.json")
    Draft202012Validator.check_schema(schema)
    registry = Registry().with_resource(
        "urn:classctl:control:v1", Resource.from_contents(read("contracts/v1.schema.json"))
    )
    validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    sequence = read("contracts/preview/sequence.json")
    for msg in sequence:
        validator.validate(msg)
    for case in read("contracts/preview/rejections.json"):
        invalid = {**sequence[case["event"]], case["field"]: case["value"]}
        if validator.is_valid(invalid):
            raise ValueError(f"Принят несовместимый пример {case}")
    subprocess.run(["node", "--input-type=module", "-e", """
        import {readFileSync} from 'node:fs';
        import {exercise} from './checks/preview.mjs';
        const read = p => JSON.parse(readFileSync(p));
        await exercise(read('contracts/preview/sequence.json'), read('contracts/examples/status.json'));
    """], cwd=root, check=True)
    print("Preview v1: схемы, подтверждения и сброс позднего кадра OK")
