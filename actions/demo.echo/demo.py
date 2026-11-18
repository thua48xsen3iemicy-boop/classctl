"""Pure demonstration action. Its caller validates parameters before dispatch."""


def run(parameters):
    return {"label": parameters["label"]}
