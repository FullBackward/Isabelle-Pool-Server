"""Utilities associated with the process that runs the Scala REPL gateway."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

import py4j
import py4j.java_collections
from py4j.java_gateway import GatewayParameters, JavaGateway

REPO_ROOT = Path(__file__).parent.parent.parent.parent
repl_gateway_path = REPO_ROOT / "repl/src/main/scala/repl/repl_backend_gateway.scala"
isabelle_executable = REPO_ROOT / "isabelle" / "bin" / "isabelle"

EnvStateID = int


class Outputs(Protocol):
    # pylint: disable=missing-docstring
    """Protocol for separated outputs from the Scala backend."""

    def output(self) -> str: ...
    def error(self) -> str: ...


class ReplResult(Protocol):
    # pylint: disable=missing-docstring
    """Protocol for results from the Scala backend."""

    def separated_output(self) -> Outputs: ...
    def total_output(self) -> str: ...


# pylint: disable=missing-docstring
class ReplBackend(Protocol):
    """Protocol for results for the Scala backend."""

    def current_thy_name_string(self) -> str: ...
    def enter_thy(self, input_thy_name: str) -> ReplResult: ...
    def open_subgoals(self) -> py4j.java_collections.JavaList[str]: ...
    def local_facts(self) -> py4j.java_collections.JavaList[str]: ...
    def global_facts(self, limit: int) -> py4j.java_collections.JavaList[str]: ...
    def get_proof_state(self) -> ReplResult: ...
    def get_source(self) -> ReplResult: ...
    def rollback(self) -> ReplResult: ...
    def step(self, isar_string: str) -> ReplResult: ...
    def save_state(self) -> EnvStateID: ...
    def restore_state(self, state_id: EnvStateID) -> bool: ...
    def reset(self) -> ReplResult: ...
    def exit(self) -> None: ...


class ReplBackendGatewayProcess:
    """
    Class to manage an instance of the Scala REPL gateway process.
    """

    def __init__(self) -> None:
        # pylint: disable=consider-using-with, subprocess-popen-preexec-fn
        self.process = subprocess.Popen(
            [isabelle_executable, "scala", str(repl_gateway_path)],
            # Isabelle uses many child processes, so we start a new process group
            preexec_fn=os.setsid,
            # Port number will be passed via stdout
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
        )
        if self.process.stdout is None:
            raise RuntimeError(
                "Scala REPL gateway process failed to start (stdout is None)."
            )
        port = int(self.process.stdout.readline().strip())
        # Redirect stdout to the console once the port number is read
        self.process.stdout = sys.stdout
        self.gateway = JavaGateway(gateway_parameters=GatewayParameters(port=port))

    def has_terminated(self) -> bool:
        """Check if the Scala REPL gateway process has terminated."""
        return self.process.poll() is not None

    def get_repl_backend(self, show_states: bool) -> ReplBackend:
        """Get the Isabelle REPL object from the Scala gateway."""
        poll_interval = 0.1
        poll_timeout = 20
        start_time = time.time()
        while time.time() - start_time < poll_timeout:
            if self.has_terminated():
                raise RuntimeError("Scala REPL gateway process has terminated.")
            try:
                repl_backend: ReplBackend = (
                    self.gateway.jvm.repl.ReplBackendGateway.get_repl_backend(
                        show_states
                    )
                )
                return repl_backend
            except py4j.protocol.Py4JNetworkError:
                time.sleep(poll_interval)
        raise RuntimeError(
            "Failed to get the Scala REPL backend from the gateway process."
        )

    def terminate(self) -> None:
        """
        Terminate the Scala REPL gateway and the gateway process, ensuring child
        processes are also killed.
        """
        self.gateway.shutdown()

        pgid = os.getpgid(self.process.pid)
        os.killpg(pgid, signal.SIGTERM)

        wait_start_time = time.time()
        while time.time() - wait_start_time < 1:
            if self.process.poll() is not None:
                return
            time.sleep(0.1)

        print("Gateway process refused to terminate. Forcefully killing...")
        os.killpg(pgid, signal.SIGKILL)
        self.process.wait(timeout=1)
