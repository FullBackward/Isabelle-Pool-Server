"""Provides a Python interface to interact with Isabelle."""

import atexit

from .repl_backend_gateway import EnvStateID, ReplBackendGatewayProcess, ReplResult


class IsabelleClient:
    """Provides an interface to interact with Isabelle/HOL via the Scala REPL."""

    def __init__(self, show_states: bool) -> None:
        self.repl_backend_init = self.repl_backend_gateway_process_init = False
        atexit.register(self.cleanup)

        self.repl_backend_gateway_process = ReplBackendGatewayProcess()
        self.repl_backend_gateway_process_init = True
        self.repl_backend = self.repl_backend_gateway_process.get_repl_backend(
            show_states
        )
        self.repl_backend_init = True

    def enter_thy(self, thy_name: str) -> ReplResult:
        """Enters a given theory."""
        return self.repl_backend.enter_thy(thy_name)

    def isar_snippet(self, isar_string: str) -> ReplResult:
        """Adds the given Isar string to the current theory."""
        return self.repl_backend.step(isar_string)

    def get_current_thy_name(self) -> str:
        """Returns the current theory name."""
        return self.repl_backend.current_thy_name_string()

    def open_subgoals(self) -> list[str]:
        """Returns the list of currently open subgoals."""
        return [subgoal.strip() for subgoal in self.repl_backend.open_subgoals()]

    def local_facts(self) -> list[str]:
        """Returns the list of local facts in the current proof context."""
        return list(self.repl_backend.local_facts())

    def global_facts(self, limit: int) -> list[str]:
        """Returns the list of global facts in the current proof context."""
        return list(self.repl_backend.global_facts(limit))

    def get_source(self) -> ReplResult:
        """Returns the source code of the current theory (if any)."""
        return self.repl_backend.get_source()

    def rollback(self) -> ReplResult:
        """Undoes the last step in the current theory."""
        return self.repl_backend.rollback()

    def save_state(self) -> EnvStateID:
        """Saves the current state of the environment, returning an ID for the state."""
        return self.repl_backend.save_state()

    def restore_state(self, state_id: EnvStateID) -> bool:
        """Restores a previously saved state by the state ID."""
        return self.repl_backend.restore_state(state_id)

    def reset(self) -> None:
        """Resets the Scala REPL."""
        self.repl_backend.reset()

    def vectorise(self, size: int) -> None:
        """
        Switches to vector mode with the specified number of environments.
        """
        return self.repl_backend.vectorize(size)

    def scalarise(self, index_to_keep: int):
        """
        Switches from vector mode back to scalar mode, keeping only the
        environment at the specified index.
        """
        return self.repl_backend.scalarise(index_to_keep)

    def vector_step(self, isar_strings: list[str]) -> ReplResult:
        """
        Executes Isar commands in parallel across all environments.
        Empty strings in the list indicate no action for that environment.
        """
        return self.repl_backend.vector_step(isar_strings)

    def cleanup(self) -> None:
        """
        Cleans up the Scala REPL by shutting down the server behind the REPL, the REPL
        gateway and the associated processes.
        """
        if (
            self.repl_backend_gateway_process_init
            and not self.repl_backend_gateway_process.has_terminated()
        ):
            if self.repl_backend_init:
                self.repl_backend.exit()
            self.repl_backend_gateway_process.terminate()
