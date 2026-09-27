from typing import Iterable

from .isabelle_gym import IsabelleGym


class Agent:
    """Base class for agents in the multi-agent environment."""

    def act(self, gym: IsabelleGym, additional_info: dict) -> None:
        """
        Allows the agent to perform its desired actions given the current multi-agent
        environment state.
        """
        raise NotImplementedError("Agents must implement the act method")


class MultiAgentIsabelleGym:
    """
    Multi-agent adaptation of IsabelleGym implementing the Agent Environment Cycle
    (AEC).
    """

    def __init__(self, isabelle_gym: IsabelleGym):
        """
        Initialise the multi-agent environment.
        """
        self.gym = isabelle_gym
        self.agents: dict[str, Agent] = {}

    def register_agent(self, agent_name: str, agent: Agent) -> None:
        """
        Adds the given agent to the multi-agent environment.
        This makes it available in the AEC.
        """
        self.agents[agent_name] = agent

    def unregister_agent(self, agent_name: str) -> None:
        """
        Removes agent from those available in the AEC.

        Args:
            agent_name: The name of the agent to remove
        """
        if agent_name in self.agents:
            del self.agents[agent_name]

    def run_agent_env_cycle(
        self, agent_iter: Iterable[str], shared_state: dict
    ) -> None:
        """
        Conducts the AEC, constantly selecting an agent (based on "agent_iter")
        and allowing that agent to perform actions before selecting the next agent.
        This terminates in line with agent_iter.
        """
        for agent_name in agent_iter:
            assert agent_name in self.agents, f"Agent {agent_name} not registered"
            self.agents[agent_name].act(self.gym, shared_state)
