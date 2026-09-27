""" MCTS Agent Implementation for IsabelleGym """

import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from local_gym.isabelle_agent_interface import IsabelleAgent, ProofContext, ProofResult
from local_gym.isabelle_gym import IsabelleGym
from local_gym.success_checker import is_syntax_successful, get_error_message

@dataclass
class MCTSProofContext(ProofContext):
    """ Extends base ProofContext to support MCTS tree search functionality """
    visit_count: int = 1
    total_value: float = 0.0
    children: List['MCTSProofContext'] = field(default_factory=list)
    parent: Optional['MCTSProofContext'] = None
    parent_goal_id: Optional[int] = None
    
    exhausted: bool = False
    subtree_exhausted: bool = False
    tactic_feedback: Optional[str] = None
    trials: List[int] = field(default_factory=list)
    
    def __post_init__(self):

        super().__post_init__()
        if len(self.trials) != len(self.subgoals):
            self.trials = [0] * len(self.subgoals)
    
    def get_mean_value(self) -> float:

        if self.visit_count == 0:
            return 0.0
        return self.total_value / self.visit_count
    
    def is_expanded(self) -> bool:

        return len(self.children) > 0
    
    @property
    def is_root(self) -> bool:

        return self.parent is None


class MCTSIsabelleAgent(IsabelleAgent, ABC):
    """ MCTS theorem proving agent implementation """
    
    def __init__(self, agent_name: str = "MCTSAgent", c_puct: float = 1.0):
        super().__init__(agent_name)
        self.c_puct = c_puct  
        
    @abstractmethod
    def estimate(self, state: MCTSProofContext) -> MCTSProofContext:

        pass
    
    @abstractmethod  
    def select(self, state: MCTSProofContext) -> List[MCTSProofContext]:
        """ Select search path from root to leaf. """
        pass
    
    def backup(self, states: List[MCTSProofContext], value: float):
        """ Backpropagate value updates. """
        for state in states:
            state.total_value += value
            state.visit_count += 1
            
            # Update subtree exhaustion state
            if state.children:
                state.subtree_exhausted = (
                    all(child.subtree_exhausted for child in state.children) 
                    and state.exhausted
                )
    
    def prove_theorem(self, 
               gym: IsabelleGym, 
               theorem_statement: str,
               max_steps: int = 100,
               max_trials_per_goal: int = 5,
               verbose: bool = False,
               **kwargs) -> ProofResult:
        
        start_time = time.time()
        
        self.reset_session()
        

        current_thy = gym.current_thy
        subgoals = gym.open_subgoals()
        
        if verbose:
            print(f"Current theory state: {current_thy}")
            print(f"Current subgoals count: {len(subgoals)}")
        
        if subgoals:
            if verbose:
                print("Detected existing subgoals, starting MCTS search directly")
            

            initial_priorities = [0.0] * len(subgoals)
            search_root = MCTSProofContext(
                subgoals=subgoals,
                priorities=initial_priorities,
                current_theorem=theorem_statement,
                proof_depth=0
            )
            search_root = self.estimate(search_root)
            
        # If no subgoals, need to start theorem
        else:
            if verbose:
                print("No subgoals detected, attempting to start theorem")
            
            theorem_result = gym.step(theorem_statement)
            if not is_syntax_successful(theorem_result):
                return ProofResult(
                    success=False, duration=time.time() - start_time,
                    proof_steps=0, final_proof_length=0, final_state="",
                    error_message=f"Failed to start theorem: {get_error_message(theorem_result)}"
                )
            
            # Check subgoals
            subgoals = gym.open_subgoals()
            if not subgoals:
                return ProofResult(
                    success=False, duration=time.time() - start_time,
                    proof_steps=0, final_proof_length=0, final_state="",
                    error_message="Failed to start theorem: No subgoals generated after theorem statement"
                )
            
            # initialize root node
            n_goals_root = len(subgoals)
            initial_priorities = [0.0] * len(subgoals)
            
            search_root = MCTSProofContext(
                subgoals=subgoals,
                priorities=initial_priorities,
                current_theorem=theorem_statement,
                proof_depth=0
            )
            search_root = self.estimate(search_root)
            
            if verbose:
                print(f"MCTS search started: {theorem_statement}")
                print(f"Initial goals: {n_goals_root}, root node value: {search_root.total_value:.3f}")
        
        # MCTS search loop
        for i_step in range(max_steps):
            # Selection 
            search_trajectory = self.select(search_root)
            search_state = search_trajectory[-1]
            
            if verbose and i_step % 10 == 0:
                print(f"Step {i_step}: trajectory length={len(search_trajectory)}, "
                      f"leaf value={search_state.get_mean_value():.3f}")
            
            
            if search_state.is_complete:
                if verbose:
                    print(f"MCTS proof successful! Steps: {i_step}")
                
                return ProofResult(
                    success=True, duration=time.time() - start_time,
                    proof_steps=i_step, final_proof_length=search_state.proof_depth,
                    final_state=gym.get_source().total_output(),
                    guidance_calls=self.session_priority_calls,
                    tactic_recommendations=self.session_tactic_history
                )
            
            # tactic selection
            target_goal_id = search_state.active_goal_id
            if target_goal_id < 0:
                search_state.exhausted = True
                search_state.subtree_exhausted = True
                continue
            
            # check trial limit
            if search_state.trials[target_goal_id] > max_trials_per_goal:
                tactic = None
            else:
                tactic = self.recommend_tactic(gym, search_state, target_goal_id)
            
            if verbose and tactic:
                print(f"Goal {target_goal_id}: {tactic}")
            
            if not tactic:
                search_state.tactic_feedback = None
                search_state.exhausted = True
                search_state.subtree_exhausted = all(
                    child.subtree_exhausted for child in search_state.children
                )
                continue
            
            # record tested tactic
            if tactic not in search_state.attempted_tactics:
                search_state.attempted_tactics.append(tactic)
            
            # expansion & simulation
            try:
                search_state.trials[target_goal_id] += 1
                
                if verbose:
                    print(f"Executing: {tactic}")
                
                # save current state
                old_subgoals = gym.open_subgoals().copy()
                
                tactic_result = gym.step(tactic)
                
                if is_syntax_successful(tactic_result):

                    new_subgoals = gym.open_subgoals()
                    
                    # generate priorities
                    new_priorities = (
                        [0.0] * len(new_subgoals) if len(new_subgoals) <= 1 
                        else self.evaluate_priorities(gym, search_state)
                    )
                    if len(new_subgoals) > 1:
                        self.session_priority_calls += 1
                    
                    child_state = MCTSProofContext(
                        subgoals=new_subgoals,
                        priorities=new_priorities,
                        current_theorem=search_state.current_theorem + f"\n{tactic}",
                        proof_depth=search_state.proof_depth + 1,
                        parent=search_state,
                        parent_goal_id=target_goal_id
                    )
                    
                    child_state = self.estimate(child_state)
                    search_state.children.append(child_state)
                    
                    # record tactic recommendation
                    self.session_tactic_history.append(f"Goal {target_goal_id}: {tactic}")
                    
                    # backpropagation
                    self.backup(search_trajectory, child_state.total_value)
                    
                    if verbose:
                        progress = f"{len(old_subgoals)} → {len(new_subgoals)}"
                        print(f"   Success! subgoals: {progress}, child node value: {child_state.total_value:.3f}")
                    
                else:
                    # tactic failed
                    search_state.tactic_feedback = str(tactic_result)
                    if verbose:
                        print(f"   Tactic failed: {tactic_result}")
                    
            except Exception as e:
                # server error
                if verbose:
                    print(f"   Execution error: {e}")
                search_state.tactic_feedback = str(e)
                continue
        
        # search completed
        if verbose:
            print(f"MCTS search reached maximum steps {max_steps}")
            print(f"Root node statistics: visits={search_root.visit_count}, value={search_root.get_mean_value():.3f}")
        
        return ProofResult(
            success=False, duration=time.time() - start_time,
            proof_steps=max_steps, final_proof_length=search_root.proof_depth,
            final_state=gym.get_source().total_output(),
            error_message="MCTS search steps exhausted",
            guidance_calls=self.session_priority_calls,
            tactic_recommendations=self.session_tactic_history
        )


class SimpleMCTSIsabelleAgent(MCTSIsabelleAgent):
    """ Simple MCTS implementation with basic functionality """
    
    def __init__(self, agent_name: str = "SimpleMCTS", c_puct: float = 0.6):
        super().__init__(agent_name, c_puct)
        # ensure name attribute exists for training data system compatibility
        self.name = agent_name
        
        # Tactic collection for goal-based selection
        self.goal_tactic_id_map = {}
        
        # Primary automated tactics
        self.primary_tactics = [
            "by auto", 
            "by simp",      
            "by blast"     
        ]
        
        self.secondary_tactics = [ ] # TODO: develop corresponding tactics in specific environments

        self.specialized_tactics = [ ] # TODO: develop corresponding tactics in specific environments
    
    def estimate(self, state: MCTSProofContext) -> MCTSProofContext:
        """ Simple value estimation """

        if state.is_complete:
            state.total_value = 1.0
        else:
            # mix heuristic and randomness
            heuristic = 1.0 / (1.0 + len(state.subgoals))
            noise = random.random() * 0.3
            state.total_value = heuristic + noise
            
        return state
    
    def select(self, state: MCTSProofContext) -> List[MCTSProofContext]:
        """ UCB selection implementation """
        state_trajectory = [state]
        current_state = state
        
        # calculate current state's UCB score
        if current_state.visit_count > 0:
            current_state_ucb = (
                (current_state.total_value / current_state.visit_count) + 
                self.c_puct * math.sqrt(math.log(current_state.visit_count) / current_state.visit_count)
            )
        else:
            current_state_ucb = float('inf')
        
        # select along the tree
        while current_state.children:
            child_ucbs = []
            for child in current_state.children:
                if child.visit_count > 0 and current_state.visit_count > 0:
                    avg_val = child.total_value / child.visit_count
                    exploration = math.sqrt(math.log(current_state.visit_count) / child.visit_count)
                    ucb = avg_val + self.c_puct * exploration
                else:
                    ucb = float('inf')
                child_ucbs.append(ucb)
            
            # select non-exhausted best child
            available_children = [
                i for i, child in enumerate(current_state.children) 
                if not child.subtree_exhausted
            ]
            
            if not available_children:
                return state_trajectory
            
            # select UCB highest child
            best_child_idx = max(available_children, key=lambda i: child_ucbs[i])
            

            if (child_ucbs[best_child_idx] < current_state_ucb and 
                not current_state.exhausted):
                return state_trajectory
            
            current_state_ucb = child_ucbs[best_child_idx]
            current_state = current_state.children[best_child_idx]
            state_trajectory.append(current_state)
        
        return state_trajectory
    
    def recommend_tactic(self, gym: IsabelleGym, context: MCTSProofContext, target_goal_id: int) -> Optional[str]:
        """
        Tactic recommendation
        """
        if target_goal_id >= len(context.subgoals):
            return None
        
        target = context.subgoals[target_goal_id]
        
        # use same key generation method as PyPantograph
        key = (tuple(context.subgoals), target_goal_id)
        tactic_index = self.goal_tactic_id_map.get(key, 0)
        
        if "True" in target:
            tactics = self.primary_tactics
        elif len(target.split()) <= 3:
            tactics = self.primary_tactics + self.secondary_tactics
        else:
            tactics = self.primary_tactics + self.secondary_tactics + self.specialized_tactics
        
        while tactic_index < len(tactics):
            tactic = tactics[tactic_index]
            if tactic not in context.attempted_tactics:
                self.goal_tactic_id_map[key] = tactic_index + 1
                return tactic
            tactic_index += 1
        
        return None
    
    def evaluate_priorities(self, gym: IsabelleGym, context: MCTSProofContext) -> List[float]:
        """ Simple zero priority """
        return [0.0 for _ in context.subgoals]
