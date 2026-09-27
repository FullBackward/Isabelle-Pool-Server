from local_gym.isabelle_gym import IsabelleGym

from .multi_agent_isabelle_gym import MultiAgentIsabelleGym


class Agent:
    def act(self, gym: IsabelleGym, shared_state: dict):
        """
        Perform one turn in the shared environment.
        Must be overridden by subclasses.
        """
        raise NotImplementedError


class ConjectureAgent(Agent):
    """Agent that tries to complete the proof, repeatedly using 'auto'."""

    def act(self, gym: IsabelleGym, shared_state: dict):
        """Tries 'auto' for each of the open subgoals."""
        sugoals = gym.open_subgoals()
        for subgoal in sugoals:
            gym.step(f"by auto")


class CorrectorAgent(Agent):
    """Agent that corrects the proof if it fails."""

    def act(self, gym: IsabelleGym, shared_state: dict):
        """If the proof has failed, roll back to the original state and use Sledgehammer."""
        if gym.proof_finished():
            print("Proof completed! No corrections need to be made.")
            return

        gym.restore_state(shared_state["proof_start_state"])
        sledgehammer_proof = gym.sledgehammer()
        for step in sledgehammer_proof:
            gym.step(step)


class SimpleMultiAgentProver(MultiAgentIsabelleGym):
    """Simple demonstration of a multi-agent prover."""

    def __init__(self, isabelle_gym=None):
        MultiAgentIsabelleGym.__init__(self, isabelle_gym)
        self.register_agent("ConjectureAgent", ConjectureAgent())
        self.register_agent("CorrectorAgent", CorrectorAgent())

    def prove(self, theory: str, lemma: str):
        self.gym.enter_thy(theory)
        self.gym.step(f"lemma {lemma}")
        shared_state = {"proof_start_state": self.gym.save_state()}

        self.run_agent_env_cycle(("ConjectureAgent", "CorrectorAgent"), shared_state)
        assert self.gym.proof_finished(), "Multi-agent proof failed!"

        # End the theory and print the proof source
        self.gym.step("end")
        print(f"Proof of lemma {lemma} was completed. Theory source:")
        print(self.gym.source())
