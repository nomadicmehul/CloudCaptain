import logging
import os
import threading
import time

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)
time.sleep(float(os.environ.get("CC_INIT_SLEEP", "0")))
STATE = {"cold": True, "invocations": 0}


def start_ticker(path):
    def tick():
        while True:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("tick\n")
            time.sleep(0.05)

    threading.Thread(target=tick, daemon=True).start()


def handler(event, context):
    STATE["invocations"] += 1
    cold, STATE["cold"] = STATE["cold"], False
    action = event.get("action", "hello")
    LOGGER.info("action=%s cold=%s remaining_ms=%d", action, cold, context.get_remaining_time_in_millis())
    if action == "fail":
        raise ValueError(f"bad input: {event.get('reason', 'fail')}")
    if action == "sleep":
        time.sleep(float(event.get("seconds", 5)))
    if action == "exit":
        os._exit(int(event.get("code", 3)))
    if action == "big":
        return {"blob": "x" * int(event.get("bytes", 7000000))}
    if action == "unserializable":
        return {"tags": {"a", "b"}}
    if action == "background":
        start_ticker(event["path"])
    if action == "env":
        return {"name": event["name"], "value": os.environ.get(event["name"])}
    return {"message": f"hello {event.get('name', 'world')}", "cold": cold, "invocations": STATE["invocations"]}
