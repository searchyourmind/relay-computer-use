from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .engine import Run
from .models import Capability


async def main_async(args):
    capability = Capability.model_validate_json(Path(args.artifact).read_text()) if args.mode == "replay" else None
    run = Run(mode=args.mode, goal=args.goal, inputs={"member_id": args.member_id},
              target_url=args.target, capability=capability, evidence_root=Path(args.evidence),
              operator_timeout=180 if args.operator else 1, headed=args.headed)
    task = asyncio.create_task(run.execute())
    while not task.done():
        if run.status == "awaiting_operator" and args.operator:
            print(f"Intervention: {run.intervention['reason']}; same live session {run.surface.session_id}.")
            command = await asyncio.to_thread(input, "Type restore, retry, or abort: ")
            if command in {"restore", "retry"}:
                await run.claim()
                await run.operator_action("restore_session" if command == "restore" else "retry")
                await run.resume()
            else:
                await run.abort()
        await asyncio.sleep(.1)
    result = await task
    print(json.dumps({**result, "evidence_directory": str(run.evidence_dir)}, indent=2))
    if run.capability and args.save and run.status == "success":
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(run.capability.model_dump_json(indent=2) + "\n")
    return 0 if run.status in {"success", "business_outcome"} else 1


def main():
    parser = argparse.ArgumentParser(description="Discover and replay a browser capability")
    parser.add_argument("mode", choices=["discover", "replay"])
    parser.add_argument("--goal", default="Find the current savings balance for the supplied member.")
    parser.add_argument("--target", default="http://127.0.0.1:4311/")
    parser.add_argument("--member-id", default="1001")
    parser.add_argument("--artifact", default="evidence/capability.json")
    parser.add_argument("--save")
    parser.add_argument("--evidence", default="runtime")
    parser.add_argument("--operator", action="store_true", help="Pause for real operator input in this terminal")
    parser.add_argument("--headed", action="store_true")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
