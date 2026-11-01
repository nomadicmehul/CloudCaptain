import os

DATABASE_URL = os.environ["DATABASE_URL"]


def handler(event, context):
    return {"database": DATABASE_URL.rsplit("@", 1)[-1]}
