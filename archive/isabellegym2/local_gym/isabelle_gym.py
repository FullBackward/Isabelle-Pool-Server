"""Gym environment for Isabelle"""

from dataclasses import dataclass, field

from repl.src.python.isabelle_client import IsabelleClient
from repl.src.python.repl_backend_gateway import EnvStateID, ReplResult


@dataclass
class ProofState:
    """Class to store the proof state extracted from Isabelle."""

    open_subgoals: list[str] = field(default_factory=list)
    local_facts: list[str] = field(default_factory=list)
    global_facts: list[str] = field(default_factory=list)


class IsabelleGym:
    """Gym environment for reinforcement learning with Isabelle/HOL.

    Parameters
    ----------
    show_states : bool, default False
        If ``True`` the backend prints proof states after every Isar command.
        Useful for debugging / human-readable sessions, but slows things down
        during RL training.
    """

    def __init__(
        self,
        show_states: bool = False,
        enable_cache: bool = False,
        max_cache_size: int = 10,
        shared_cache: bool = False,
        initial_thys: list[str] = None,
        init_field: str = "HOL",
    ) -> None:
        """Initialize the Isabelle gym environment."""
        self.isabelle_client = IsabelleClient(
            show_states=show_states,
            enable_cache=enable_cache,
            max_cache_size=max_cache_size,
            shared_cache=shared_cache,
            initial_thys=initial_thys,
            field=init_field,
        )
        # Placeholder for gym spaces
        pass

    # Session management methods
    def create_session(self, session_id: str, initial_thys: list[str] = None, field:str = "HOL") -> str:
        """Create a new session."""
        return self.isabelle_client.create_session(session_id, initial_thys, field)
    
    def switch_session(self, session_id: str) -> bool:
        """Switch to the specified session."""
        return self.isabelle_client.switch_session(session_id)
    
    def list_sessions(self) -> dict:
        """List all sessions."""
        return self.isabelle_client.list_sessions()
    
    def close_session(self, session_id: str) -> bool:
        """Close the specified session."""
        return self.isabelle_client.close_session(session_id)
    
    def get_session_cache_stats(self, session_id: str = None) -> dict:
        """Get cache statistics for the specified session."""
        return self.isabelle_client.get_session_cache_stats(session_id)
    
    def get_session_cache_status(self, session_id: str = None) -> str:
        """Get cache status for the specified session."""
        return self.isabelle_client.get_session_cache_status(session_id)

    def enter_thy(self, thy_name: str) -> ReplResult:
        """Enters a given theory."""
        return self.isabelle_client.enter_thy(thy_name)

    @property
    def current_thy(self) -> str:
        """Returns the current theory name."""
        return self.isabelle_client.get_current_thy_name()

    def step(self, isar_string: str) -> ReplResult:
        """Adds the given Isar string to the current theory."""
        #if(isar_string.strip() == ""):
            #return ReplResult()
        result = self.isabelle_client.isar_snippet(isar_string)
        #print(result.separated_output, result.total_output)
        return result

    def open_subgoals(self) -> list[str]:
        """Returns the list of currently open subgoals."""
        return self.isabelle_client.open_subgoals()

    def proof_finished(self) -> bool:
        """
        Checks whether the current proof is finished (i.e. there are no open subgoals).
        """
        return not self.open_subgoals()

    def proof_state(
        self,
        subgoals: bool = False,
        local_facts: bool = False,
        global_facts: bool = False,
        global_facts_limit: int = 50,
    ) -> ProofState:
        """Returns the list of currently open subgoals."""
        proof_state = ProofState()
        if subgoals:
            proof_state.open_subgoals = self.isabelle_client.open_subgoals()
        if local_facts:
            proof_state.local_facts = self.isabelle_client.local_facts()
        if global_facts:
            proof_state.global_facts = self.isabelle_client.global_facts(
                global_facts_limit
            )
        return proof_state

    def get_source(self) -> ReplResult:
        """Returns the source code of the current theory."""
        return self.isabelle_client.get_source()

    def rollback(self) -> ReplResult:
        """Undoes the last step in the current theory."""
        return self.isabelle_client.rollback()

    def save_state(self) -> EnvStateID:
        """
        Saves the current state of all theories in the current Isabelle environment,
        providing a unique identifier for the state which can be used to later restore
        it.
        """
        return self.isabelle_client.save_state()

    def restore_state(self, state_id: EnvStateID) -> bool:
        """
        Restores the Isabelle environment to a previously saved state. A boolean output
        indicates whether the restoration was successful (using an invalid state ID
        will result in failure).
        """
        return self.isabelle_client.restore_state(state_id)

    def reset(self) -> None:
        """Resets the gym environment."""
        return self.isabelle_client.reset()
    
    def get_cache_stats(self) -> dict:
        """Return the cache statistics."""
        return self.isabelle_client.get_cache_stats()
        
    def get_cache_status(self) -> dict:
        """Return the cache status."""
        return self.isabelle_client.get_cache_status()

    # Vectorised environment support (multiple parallel proof states)

    def vectorise(self, size: int) -> None:
        """Switch to *vector mode* with ``size`` parallel environments."""
        self.isabelle_client.vectorise(size)

    def scalarise(self, index_to_keep: int) -> None:
        """Return from vector mode to a single environment, keeping ``index_to_keep``."""
        self.isabelle_client.scalarise(index_to_keep)

    def vector_step(self, isar_strings: list[str]) -> ReplResult:
        """Execute *size* Isar commands across all vectorised environments in parallel."""
        return self.isabelle_client.vector_step(isar_strings)

    def close(self) -> None:
        """Shut down the underlying Isabelle process and gateway cleanly."""
        self.isabelle_client.cleanup()
