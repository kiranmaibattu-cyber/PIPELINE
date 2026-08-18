from __future__ import annotations

import argparse
import json
import os

import redis


def main(args):
    client = redis.Redis(
        host=args.host,
        port=args.port,
        password=args.password,
        decode_responses=True,
    )
    last_id = args.start_id
    while True:
        messages = client.xread({args.stream: last_id}, block=args.block_ms, count=args.count)
        if not messages:
            if args.once:
                return
            continue
        for _, entries in messages:
            for entry_id, fields in entries:
                last_id = entry_id
                payload = json.loads(fields.get("payload") or "{}")
                print(json.dumps({"id": entry_id, "payload": payload}, indent=2))
        if args.once:
            return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("ANALYTICS_REDIS_HOST") or os.getenv("REDIS_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ANALYTICS_REDIS_PORT") or os.getenv("REDIS_PORT", "6379")))
    parser.add_argument("--password", default=os.getenv("ANALYTICS_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD"))
    parser.add_argument("--stream", default=os.getenv("ANALYTICS_REDIS_STREAM", "traffic:analytics"))
    parser.add_argument("--start-id", default="$")
    parser.add_argument("--block-ms", type=int, default=5000)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    main(parser.parse_args())
