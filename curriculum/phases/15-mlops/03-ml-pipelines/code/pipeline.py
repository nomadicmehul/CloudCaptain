from minipipe import dsl

IMAGE = "python:3.12-alpine"


@dsl.component(base_image=IMAGE)
def make_dataset(rows: int, seed: int, noise: float, train_data: dsl.Output[dsl.Dataset], test_data: dsl.Output[dsl.Dataset]):
    import json
    import random

    rng = random.Random(seed)
    points = []
    for _ in range(rows):
        x = rng.uniform(0.0, 10.0)
        y = 3.0 * x + 2.0 + (rng.random() - 0.5) * noise
        points.append([round(x, 6), round(y, 6)])
    cut = rows * 4 // 5
    with open(train_data.path, "w") as handle:
        json.dump(points[:cut], handle)
    with open(test_data.path, "w") as handle:
        json.dump(points[cut:], handle)


@dsl.component(base_image=IMAGE)
def train_model(train_data: dsl.Input[dsl.Dataset], lr: float, epochs: int, model: dsl.Output[dsl.Model]) -> float:
    import json
    import math

    with open(train_data.path) as handle:
        points = json.load(handle)
    w, b = 0.0, 0.0
    n = len(points)
    for _ in range(epochs):
        grad_w, grad_b = 0.0, 0.0
        for x, y in points:
            error = w * x / 10.0 + b - y
            grad_w += 2.0 * error * x / 10.0 / n
            grad_b += 2.0 * error / n
        w -= lr * grad_w
        b -= lr * grad_b
    residuals = [w * x / 10.0 + b - y for x, y in points]
    mse = sum(r * r for r in residuals) / n
    with open(model.path, "w") as handle:
        json.dump({"slope": w / 10.0, "intercept": b, "epochs": epochs, "lr": lr}, handle, sort_keys=True)
    return round(math.sqrt(mse), 6)


@dsl.component(base_image=IMAGE)
def evaluate_model(model: dsl.Input[dsl.Model], test_data: dsl.Input[dsl.Dataset]) -> float:
    import json
    import math

    with open(model.path) as handle:
        weights = json.load(handle)
    with open(test_data.path) as handle:
        points = json.load(handle)
    residuals = [weights["slope"] * x + weights["intercept"] - y for x, y in points]
    return round(math.sqrt(sum(r * r for r in residuals) / len(residuals)), 6)


@dsl.component(base_image=IMAGE)
def register_model(model: dsl.Input[dsl.Model], rmse: float, name: str, card: dsl.Output[dsl.Artifact]):
    import json

    with open(model.path) as handle:
        weights = json.load(handle)
    with open(card.path, "w") as handle:
        json.dump({"name": name, "rmse": rmse, "weights": weights}, handle, sort_keys=True)


@dsl.component(base_image=IMAGE)
def score_slice(model: dsl.Input[dsl.Model], test_data: dsl.Input[dsl.Dataset], segment: str) -> float:
    import json
    import math

    bounds = {"low": (0.0, 10.0 / 3), "mid": (10.0 / 3, 20.0 / 3), "high": (20.0 / 3, 10.0)}[segment]
    with open(model.path) as handle:
        weights = json.load(handle)
    with open(test_data.path) as handle:
        points = [p for p in json.load(handle) if bounds[0] <= p[0] < bounds[1]]
    if not points:
        return 0.0
    residuals = [weights["slope"] * x + weights["intercept"] - y for x, y in points]
    return round(math.sqrt(sum(r * r for r in residuals) / len(residuals)), 6)


@dsl.pipeline(name="house-prices")
def train_pipeline(rows: int = 200, seed: int = 7, noise: float = 1.0, lr: float = 0.5, epochs: int = 300, max_rmse: float = 0.5):
    data = make_dataset(rows=rows, seed=seed, noise=noise)
    train = train_model(train_data=data.outputs["train_data"], lr=lr, epochs=epochs)
    train.set_retry(num_retries=2)
    evaluation = evaluate_model(model=train.outputs["model"], test_data=data.outputs["test_data"])
    with dsl.If(evaluation.output < max_rmse, name="good-enough"):
        register_model(model=train.outputs["model"], rmse=evaluation.output, name="house-prices")
    with dsl.ParallelFor(items=["low", "mid", "high"], parallelism=2) as segment:
        score_slice(model=train.outputs["model"], test_data=data.outputs["test_data"], segment=segment)
