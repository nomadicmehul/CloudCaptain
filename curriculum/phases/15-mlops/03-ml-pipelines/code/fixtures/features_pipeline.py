from minipipe import dsl


@dsl.component(base_image="python:3.12-alpine")
def count(n: int, label: str = "x", strict: bool = False) -> int:
    return n


@dsl.component(base_image="python:3.12-alpine")
def scale(x: float, data: dsl.Output[dsl.Dataset]) -> str:
    with open(data.path, "w") as handle:
        handle.write(str(x))
    return "ok"


@dsl.component(base_image="python:3.12-alpine")
def check(data: dsl.Input[dsl.Dataset], tag: str):
    pass


@dsl.pipeline(name="features")
def features(k: int = 3, mode: str = "fast", threshold: float = 0.5):
    first = count(n=k)
    second = count(n=1)
    second.after(first)
    second.set_caching_options(False)
    with dsl.If(first.output == 1):
        scale(x=0.5)
    with dsl.If(mode == "fast", name="fast-path"):
        count(n=2, label=mode)
    with dsl.ParallelFor(items=[0.1, 0.2], parallelism=1) as x:
        scaled = scale(x=x)
        check(data=scaled.outputs["data"], tag=scaled.outputs["Output"])
    with dsl.ParallelFor(items=[1, 2]) as i:
        count(n=i, strict=True)
    with dsl.If(threshold >= second.output):
        count(n=k)
