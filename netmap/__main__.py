"""netmap command line.

  python -m netmap [-c config.yaml] serve [--replay dump.json] [--bind IP] [--port N] [--no-tls]
  python -m netmap [-c config.yaml] dump OUT.json        save raw router tables once
  python -m netmap [-c config.yaml] snapshot [--replay dump.json]   print one snapshot as JSON
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import config, model, server
from .collector import RouterClient


def load_replay(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("routers", data) if isinstance(data, dict) else data


def main():
    p = argparse.ArgumentParser(prog="netmap")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--replay")
    s.add_argument("--bind")
    s.add_argument("--port", type=int)
    s.add_argument("--no-tls", action="store_true")
    d = sub.add_parser("dump")
    d.add_argument("out")
    n = sub.add_parser("snapshot")
    n.add_argument("--replay")
    args = p.parse_args()
    cmd = args.cmd or "serve"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)

    if cmd == "dump":
        out = {"collected": time.time(), "routers": {}, "errors": {}}
        for rc in cfg["routers"]:
            client = RouterClient(rc)
            try:
                out["routers"][rc["host"]] = client.fetch()
            except Exception as e:
                out["errors"][rc["host"]] = f"{type(e).__name__}: {e}"
            finally:
                client.close()
        Path(args.out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        print(f"wrote {args.out}" + (f" (errors: {out['errors']})" if out["errors"] else ""))
    elif cmd == "snapshot":
        if args.replay:
            snap = model.build(cfg, load_replay(args.replay))
        else:
            poller = server.Poller(cfg)
            poller.poll_once()
            snap = json.loads(poller.snapshot)
        sys.stdout.reconfigure(encoding="utf-8")
        json.dump(snap, sys.stdout, indent=1, default=str, ensure_ascii=False)
    else:
        srv = cfg["server"]
        if getattr(args, "bind", None):
            srv["bind"] = args.bind
        if getattr(args, "port", None):
            srv["port"] = args.port
        if getattr(args, "no_tls", False):
            srv["tls"] = False
        replay = load_replay(args.replay) if getattr(args, "replay", None) else None
        server.serve(cfg, replay)


if __name__ == "__main__":
    main()
