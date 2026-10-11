"""
An agent environment that runs commands in an Apptainer instance image.

The Apptainer counterpart of mini-swe-agent's DockerEnvironment, for the
twister-pipeline Zephyr instances (scripts/build_instances.py builds them as
.sif images under the work root). Same interface -- execute(), cleanup(),
get_template_vars(), serialize() -- so DefaultAgent uses it unchanged.

Every command runs in its own `apptainer exec` over one writable overlay
directory, so:

    - the agent's edits and builds persist from command to command, in the
      overlay, while the image itself is never modified;
    - nothing outlives a command: --containall gives each its own PID
      namespace, so a timed-out command takes its QEMU with it. (Under Docker
      the orphaned QEMU kept qemu.pid locked and broke every later run.)

mini-swe-agent ships a SingularityEnvironment, but it unpacks the whole image
into a sandbox directory in /tmp for every run -- several GB per run, into a
small node-local /tmp. The overlay holds only what the agent changes.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import apptainer_util as au  # noqa: E402

from minisweagent.exceptions import Submitted  # noqa: E402
from minisweagent.utils.serialize import recursive_merge  # noqa: E402

SUBMIT = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


class ApptainerEnvironment:
    def __init__(self, *, image: str | Path, name: str, cwd: str = "/testbed",
                 timeout: int = 900, setup_commands: list[str] | None = None,
                 logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("embedeval.apptainer")
        self.image = Path(image)
        if not self.image.exists():
            raise FileNotFoundError(f"instance image not built: {self.image}")
        self.cwd, self.timeout = cwd, timeout
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = au.path("runs", f"agent-{name}-{stamp}", "x").parent
        self.overlay = self.run_dir / "overlay"
        self.overlay.mkdir(parents=True, exist_ok=True)
        self.config = {"image": str(self.image), "cwd": cwd, "timeout": timeout,
                       "overlay": str(self.overlay)}
        for c in setup_commands or []:
            r = self.execute({"command": c}, timeout=300)
            if r["returncode"] != 0:
                raise RuntimeError(f"environment setup failed: {c}: {r['output'][-300:]}")

    # -- mini-swe-agent interface -------------------------------------------

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(dict(self.config), platform.uname()._asdict(), kwargs)

    def serialize(self) -> dict:
        return {"info": {"config": {
            "environment": dict(self.config),
            "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}"}}}

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        cmd = au.exec_args(self.image, overlay=self.overlay)
        cmd[2:2] = ["--pwd", cwd or self.cwd]
        cmd += ["bash", "-c", command]
        try:
            r = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout or self.timeout,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            output = {"output": r.stdout, "returncode": r.returncode, "exception_info": ""}
        except Exception as e:
            raw = getattr(e, "output", None)
            raw = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
            output = {"output": raw, "returncode": -1,
                      "exception_info": f"An error occurred while executing the command: {e}",
                      "extra": {"exception_type": type(e).__name__, "exception": str(e)}}
        self._check_finished(output)
        return output

    def _check_finished(self, output: dict) -> None:
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == SUBMIT and output["returncode"] == 0:
            submission = "".join(lines[1:])
            raise Submitted({"role": "exit", "content": submission,
                             "extra": {"exit_status": "Submitted", "submission": submission}})

    def cleanup(self) -> None:
        """Remove the overlay -- the only state the run created."""
        if getattr(self, "run_dir", None) and self.run_dir.exists():
            au.remove(self.run_dir)
