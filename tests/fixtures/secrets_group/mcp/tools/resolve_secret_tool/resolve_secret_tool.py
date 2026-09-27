from ramen_runtime.secrets import resolve


def resolve_literal(var: str) -> str:
    return resolve("value=" + "{{$demo." + var + "}}")
