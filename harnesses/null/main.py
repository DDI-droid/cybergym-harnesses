import base64
import time
from pathlib import Path

from cg_harness import Trajectory, chat, save_final, settings, submit

trajectory = Trajectory()
options = settings()
trajectory.log("start", settings=options)

if options.get("ping_model"):
    reply = chat([{"role": "user", "content": "Reply with exactly: ok"}], max_tokens=8)
    trajectory.log("model", content=reply["choices"][0]["message"]["content"], usage=reply.get("usage"))

if options.get("sleep_seconds"):
    time.sleep(float(options["sleep_seconds"]))

if options.get("fail"):
    trajectory.log("error", message="asked to fail")
    raise SystemExit(3)

poc = Path("/tmp/candidate")
if options.get("poc_b64"):
    poc.write_bytes(base64.b64decode(options["poc_b64"]))
else:
    poc.write_bytes(str(options.get("poc_text", "cybergym-null-harness")).encode())
result = submit(poc)
trajectory.log("submit", exit_code=result.get("exit_code"), poc_id=result.get("poc_id"), error=result.get("error"))

if not options.get("skip_final"):
    save_final(poc)
trajectory.log("end")
