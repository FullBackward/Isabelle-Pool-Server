""" IsabelleGym Agent Interface for Isabelle/HOL, inspired by PyPantograph """

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple, Union
from enum import Enum


from local_gym.isabelle_gym import IsabelleGym
from local_gym.success_checker import is_tactic_successful, is_syntax_successful, get_error_message

"""
try:
    from gym.proof_visualizer import ProofVisualizer, TacticResult
    from gym.training_data_system import TrainingDataCollector
    VISUALIZATION_AVAILABLE = True
    TRAINING_DATA_AVAILABLE = True
except ImportError:
    ProofVisualizer = None
    TacticResult = None
    TrainingDataCollector = None
    VISUALIZATION_AVAILABLE = False
    TRAINING_DATA_AVAILABLE = False
"""

from local_gym.proof_visualizer import ProofVisualizer, TacticResult
#from gym.training_data_system import TrainingDataCollector
VISUALIZATION_AVAILABLE = True
#TRAINING_DATA_AVAILABLE = False


class ProofStrategy(Enum):
    """Proof strategy enumeration for different proving approaches"""
    DEPTH_FIRST = "dfs"
    BREADTH_FIRST = "bfs"
    PRIORITY = "priority"
    MCTS = "mcts"
    SLEDGEHAMMER = "sh"
    COLLABORATIVE = "collab"


@dataclass
class ProofContext:
    """
    Isabelle proof context containing subgoals, priorities, and proof state
    Supports multi-objective priorities and Isabelle-specific features
    """

    subgoals: List[str] = field(default_factory=list)
    priorities: List[float] = field(default_factory=list)
    current_theorem: str = ""
    proof_depth: int = 0
    solved: List[bool] = field(default_factory=list)  # Track individual subgoal resolution status
    
    attempted_tactics: List[str] = field(default_factory=list)
    successful_tactics: List[str] = field(default_factory=list)
    proof_strategy: ProofStrategy = ProofStrategy.PRIORITY
    
    available_lemmas: List[str] = field(default_factory=list)
    sledgehammer_suggestions: List[str] = field(default_factory=list)
    theory_context: Dict[str, Any] = field(default_factory=dict)
    
    session_id: Optional[str] = None
    session_shared: bool = False
    
    # Training data
    training_metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Ensure priority and solved lists match subgoals"""
        if len(self.priorities) != len(self.subgoals):
            self.priorities = [1.0] * len(self.subgoals)
        if len(self.solved) != len(self.subgoals):
            self.solved = [False] * len(self.subgoals)  
    
    @property
    def active_goal_id(self) -> int:
        """Get current active goal ID - highest priority unsolved goal"""
        if not self.subgoals:
            return -1
        
        # Find the highest priority unsolved goal
        unsolved_goals = [(i, prio) for i, prio in enumerate(self.priorities) 
                         if i < len(self.solved) and not self.solved[i]]
        
        if not unsolved_goals:
            return -1
        
        goal_id, _ = max(unsolved_goals, key=lambda x: x[1])
        return goal_id
    
    @property
    def current_subgoal(self) -> Optional[str]:
        """Get current subgoal"""
        goal_id = self.active_goal_id
        if 0 <= goal_id < len(self.subgoals):
            return self.subgoals[goal_id]
        return None
    
    @property
    def is_complete(self) -> bool:
        """Check if proof is complete"""
        return len(self.subgoals) == 0 or all(self.solved)
    
    @property
    def num_subgoals(self) -> int:
        """Get the number of remaining subgoals"""
        return len(self.subgoals)
    
    @property
    def progress_ratio(self) -> float:
        """Proof progress ratio"""
        if not self.successful_tactics:
            return 0.0
        total_attempts = len(self.attempted_tactics)
        if total_attempts == 0:
            return 0.0
        return len(self.successful_tactics) / total_attempts
    
    def update_subgoals(self, new_subgoals: List[str], new_priorities: List[float] = None):
        """Update subgoals, priorities, and solved status"""
        self.subgoals = new_subgoals
        if new_priorities:
            self.priorities = new_priorities
        else:
            self.priorities = [1.0] * len(new_subgoals)
        self.solved = [False] * len(new_subgoals)
    
    def record_tactic_attempt(self, tactic: str, success: bool):
        """Record tactic attempt"""
        self.attempted_tactics.append(tactic)
        if success:
            self.successful_tactics.append(tactic)
    

@dataclass
class ProofResult:
    """
    Isabelle proof result
    Contains detailed information and performance metrics
    """
    # Basic results
    success: bool
    duration: float
    proof_steps: int
    final_proof_length: int
    final_state: str
    
    tactic_attempts: int = 0
    successful_tactics: int = 0
    failed_tactics: int = 0
    priority_updates: int = 0

    session_reuse_count: int = 0
    cache_hit_rate: float = 0.0
    memory_usage_mb: float = 0.0

    sledgehammer_calls: int = 0
    lemma_applications: int = 0
    visualization_enabled: bool = False
    
    training_data_collected: bool = False
    reward_signal: float = 0.0
    
    guidance_calls: int = 0         
    tactic_recommendations: List[str] = field(default_factory=list) 
    
    error_message: Optional[str] = None
    failure_analysis: Dict[str, Any] = field(default_factory=dict)
    
    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        efficiency = f"{self.successful_tactics}/{self.tactic_attempts}" if self.tactic_attempts > 0 else "0/0"
        return (f"ProofResult({status}, "
                f"steps={self.proof_steps}, "
                f"duration={self.duration:.2f}s, "
                f"efficiency={efficiency})")
    
    @property
    def success_rate(self) -> float:
        """Tactic success rate"""
        if self.tactic_attempts == 0:
            return 0.0
        return self.successful_tactics / self.tactic_attempts
    
    @property
    def performance_score(self) -> float:
        """Comprehensive performance score, can be edited by developers"""
        if not self.success:
            return 0.0
        
        # base score
        base_score = 100.0
        
        # efficiency bonus
        if self.tactic_attempts > 0:
            efficiency_bonus = (self.successful_tactics / self.tactic_attempts) * 20
            base_score += efficiency_bonus
        
        # speed bonus
        if self.duration > 0:
            speed_bonus = max(0, 10 - self.duration) 
            base_score += speed_bonus
        
        # cache usage bonus
        cache_bonus = self.cache_hit_rate * 5
        base_score += cache_bonus
        
        return min(base_score, 150.0)
    
class IsabelleAgent(ABC):
    """
    IsabelleGym unified agent interface

    """
    
    def __init__(self, agent_name: str = "UnnamedIsabelleAgent", strategy: ProofStrategy = ProofStrategy.PRIORITY):
        self.agent_name = agent_name
        self.strategy = strategy
        self.reset_session()
        
        # performance statistics
        self.total_proofs_attempted = 0
        self.total_proofs_succeeded = 0
        self.total_tactics_tried = 0
        self.total_tactics_succeeded = 0
        
        # training data collector
        self.training_collector = None
        #if TRAINING_DATA_AVAILABLE:
        #    self.training_collector = TrainingDataCollector(agent_name)
    
    @abstractmethod
    def recommend_tactic(self, 
                        gym: IsabelleGym, 
                        context: ProofContext,
                        target_goal_id: int) -> Optional[str]:
        """ recommend next tactic """
        pass
    
    @abstractmethod
    def evaluate_priorities(self, 
                           gym: IsabelleGym, 
                           context: ProofContext) -> List[float]:
        """ evaluate subgoal priorities """
        pass
    
    def reset_session(self):
        """Reset agent session state"""
        self.session_step_count = 0
        self.session_tactic_history = []
        self.session_priority_calls = 0
        self.session_start_time = None
    
    def reset(self):
        """Backward compatibility: alias for reset_session"""
        self.reset_session()
    
    def prove_theorem(self, 
                     gym: IsabelleGym, 
                     theorem_statement: str,
                     max_steps: int = 100,
                     max_depth: int = 50,
                     timeout: float = 60.0,
                     enable_visualization: bool = False,
                     collect_training_data: bool = False,
                     verbose: bool = False,
                     enable_sledgehammer: bool = False,
                     session_reuse: bool = True) -> ProofResult:
        """ detailed proof result """
        start_time = time.time()
        self.session_start_time = start_time
        self.reset_session()
        self.total_proofs_attempted += 1
        
        visualizer = None
        if enable_visualization and VISUALIZATION_AVAILABLE:
            visualizer = ProofVisualizer()
            visualizer.start_session(theorem_statement, self.agent_name)
        

        session_info = {}
        if hasattr(gym, 'get_session_info'):
            session_info = gym.get_session_info()
        
        try:
            # start the theorem proof
            initial_result = gym.step(theorem_statement)
            if not is_syntax_successful(initial_result):
                error_msg = f"Failed to start theorem: {get_error_message(initial_result)}"
                if visualizer:
                    visualizer.display_tactics_history()
                    visualizer.end_session(success=False)
                return self._create_failure_result(start_time, error_msg, 0, 0)
            
            initial_subgoals = gym.open_subgoals()
            if not initial_subgoals:
                error_msg = "No subgoals found after theorem statement"
                if visualizer:
                    visualizer.display_tactics_history()
                    visualizer.end_session(success=False)
                return self._create_failure_result(start_time, error_msg, 0, 0)
            
            # initialize the proof context
            context = ProofContext(
                subgoals=initial_subgoals,
                current_theorem=theorem_statement,
                proof_strategy=self.strategy,
                session_id=session_info.get('session_id'),
                session_shared=session_reuse,
                training_metadata={
                    'agent_name': self.agent_name,
                    'start_time': start_time,
                    'collect_data': collect_training_data
                }
            )
            
            for step in range(max_steps):
                if time.time() - start_time > timeout:
                    error_msg = f"Proof timeout after {timeout}s"
                    if visualizer:
                        visualizer.display_tactics_history()
                        visualizer.end_session(success=False)
                    return self._create_timeout_result(start_time, step, context, error_msg)
                
                if context.is_complete:
                    self.total_proofs_succeeded += 1
                    if visualizer:
                        visualizer.display_tactics_history()
                        visualizer.end_session(success=True)
                    return self._create_success_result(start_time, step, context, session_info)
                
                if context.proof_depth >= max_depth:
                    error_msg = f"Maximum proof depth {max_depth} reached"
                    if visualizer:
                        visualizer.display_tactics_history()
                        visualizer.end_session(success=False)
                    return self._create_failure_result(start_time, error_msg, step, context.proof_depth)
                
                if len(context.subgoals) > 1:
                    priorities = self.evaluate_priorities(gym, context)
                    context.priorities = priorities
                    self.session_priority_calls += 1
                
                # select the target and recommend the tactic
                target_goal_id = context.active_goal_id
                if target_goal_id < 0:
                    error_msg = "No available goals to work on"
                    if visualizer:
                        visualizer.display_tactics_history()
                        visualizer.end_session(success=False)
                    return self._create_failure_result(start_time, error_msg, step, context.proof_depth)
                
                if enable_sledgehammer and hasattr(gym, 'sledgehammer'):
                    sledgehammer_suggestions = gym.sledgehammer(context.current_subgoal)
                    context.sledgehammer_suggestions = sledgehammer_suggestions
                
             
                tactic = self.recommend_tactic(gym, context, target_goal_id)
                if not tactic:
                    error_msg = "No more tactics available"
                    if visualizer:
                        visualizer.display_tactics_history()
                        visualizer.end_session(success=False)
                    return self._create_failure_result(start_time, error_msg, step, context.proof_depth)
                
                self.session_tactic_history.append(f"Goal {target_goal_id}: {tactic}")
                
                tactic_start_time = time.time()
                before_subgoals = context.subgoals.copy()
                
                tactic_result = gym.step(tactic)
                tactic_execution_time = time.time() - tactic_start_time
                
                after_subgoals = gym.open_subgoals()
                syntax_success = is_syntax_successful(tactic_result)
                progress_made = (len(after_subgoals) != len(before_subgoals) or 
                               after_subgoals != before_subgoals)
                actual_success = syntax_success and progress_made
                
                self.total_tactics_tried += 1
                self.session_step_count += 1
                context.record_tactic_attempt(tactic, actual_success)
                
                if actual_success:
                    self.total_tactics_succeeded += 1
                    context.update_subgoals(after_subgoals)
                    context.proof_depth += 1
                    self.session_tactic_history.append(tactic)
                
                if visualizer:
                    visualizer.record_tactic_execution(
                        tactic=tactic,
                        before_subgoals=before_subgoals,
                        after_subgoals=after_subgoals,
                        success=actual_success,
                        error_message=None if syntax_success else str(tactic_result),
                        execution_time=tactic_execution_time
                    )
                
                # collect the training data
                if collect_training_data and self.training_collector:
                    try:
                        if hasattr(self.training_collector, 'record_tactic_execution'):
                            self.training_collector.record_tactic_execution(
                                context=context,
                                tactic=tactic,
                                success=actual_success,
                                reward=1.0 if actual_success else -0.1
                            )
                    except Exception:
                        pass  # training collector failed can not affect the proof process
            
            error_msg = f"Maximum steps {max_steps} reached"
            if visualizer:
                visualizer.end_session(success=False)
            return self._create_failure_result(start_time, error_msg, max_steps, context.proof_depth)
            
        except Exception as e:
            error_msg = f"Unexpected error during proof: {str(e)}"
            if visualizer:
                visualizer.display_tactics_history()
                visualizer.end_session(success=False)
            return self._create_failure_result(start_time, error_msg, 0, 0)
    
    def _create_success_result(self, start_time: float, steps: int, context: ProofContext, session_info: Dict) -> ProofResult:
        """create the success result"""
        duration = time.time() - start_time
        return ProofResult(
            success=True,
            duration=duration,
            proof_steps=steps,
            final_proof_length=context.proof_depth,
            final_state="Proof completed",
            tactic_attempts=len(context.attempted_tactics),
            successful_tactics=len(context.successful_tactics),
            failed_tactics=len(context.attempted_tactics) - len(context.successful_tactics),
            priority_updates=self.session_priority_calls,
            session_reuse_count=session_info.get('reuse_count', 0),
            cache_hit_rate=session_info.get('cache_hit_rate', 0.0),
            memory_usage_mb=session_info.get('memory_mb', 0.0),
            training_data_collected=self.training_collector is not None,
            reward_signal=100.0,  

            guidance_calls=self.session_priority_calls,
            tactic_recommendations=self.session_tactic_history
        )
    
    def _create_failure_result(self, start_time: float, error_msg: str, steps: int, depth: int) -> ProofResult:
        """create the failure result"""
        duration = time.time() - start_time
        return ProofResult(
            success=False,
            duration=duration,
            proof_steps=steps,
            final_proof_length=depth,
            final_state="Proof failed",
            error_message=error_msg,
            reward_signal=-10.0,  

            guidance_calls=self.session_priority_calls,
            tactic_recommendations=self.session_tactic_history
        )
    
    def _create_timeout_result(self, start_time: float, steps: int, context: ProofContext, error_msg: str) -> ProofResult:
        """create the timeout result"""
        duration = time.time() - start_time
        return ProofResult(
            success=False,
            duration=duration,
            proof_steps=steps,
            final_proof_length=context.proof_depth,
            final_state="Proof timeout",
            tactic_attempts=len(context.attempted_tactics),
            successful_tactics=len(context.successful_tactics),
            failed_tactics=len(context.attempted_tactics) - len(context.successful_tactics),
            error_message=error_msg,
            reward_signal=-5.0, 

            guidance_calls=self.session_priority_calls,
            tactic_recommendations=self.session_tactic_history
        )
    
    @property
    def success_rate(self) -> float:
        """overall success rate"""
        if self.total_proofs_attempted == 0:
            return 0.0
        return self.total_proofs_succeeded / self.total_proofs_attempted
    
    @property
    def tactic_efficiency(self) -> float:
        """tactic efficiency"""
        if self.total_tactics_tried == 0:
            return 0.0
        return self.total_tactics_succeeded / self.total_tactics_tried
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """get the performance summary"""
        return {
            'agent_name': self.agent_name,
            'strategy': self.strategy.value,
            'proofs_attempted': self.total_proofs_attempted,
            'proofs_succeeded': self.total_proofs_succeeded,
            'success_rate': self.success_rate,
            'tactics_tried': self.total_tactics_tried,
            'tactics_succeeded': self.total_tactics_succeeded,
            'tactic_efficiency': self.tactic_efficiency,
            'training_data_available': self.training_collector is not None
        }


class SimpleIsabelleAgent(IsabelleAgent):
    """
    simple Isabelle agent implementation
    comparable to PyPantograph's DumbAgent
    """
    
    def __init__(self, agent_name: str = "SimpleIsabelle"):
        super().__init__(agent_name, ProofStrategy.PRIORITY)
        
        # Isabelle tactics library
        self.basic_tactics = [
            "by auto",
            "by simp", 
            "by blast"
        ]
        
        self.intro_tactics = [
            "apply (rule impI)",
            "apply (rule allI)",
            "apply (rule conjI)"
        ]
        
        self.elimination_tactics = [
            "apply (erule conjE)",
            "apply (erule disjE)",
            "apply (erule exE)"
        ]
        
        self.goal_tactic_counters = {}
    
    def recommend_tactic(self, gym: IsabelleGym, context: ProofContext, target_goal_id: int) -> Optional[str]:
        """simple tactic recommendation logic"""
        if target_goal_id >= len(context.subgoals):
            return None
        
        target_subgoal = context.subgoals[target_goal_id]
        
        # generate the unique key
        key = (tuple(context.subgoals), target_goal_id)
        counter = self.goal_tactic_counters.get(key, 0)
        
        if target_subgoal.startswith('∀') or "∀" in target_subgoal:
            tactics = self.intro_tactics
        elif '∧' in target_subgoal or '∨' in target_subgoal:
            tactics = self.elimination_tactics
        else:
            tactics = self.basic_tactics
        
        if counter >= len(tactics):
            return None
        
        self.goal_tactic_counters[key] = counter + 1
        return tactics[counter]
    
    def evaluate_priorities(self, gym: IsabelleGym, context: ProofContext) -> List[float]:
        priorities = [1.0] + [0.5] * (len(context.subgoals) - 1)
        return priorities


def create_isabelle_agent(agent_type: str = "simple", 
                         agent_name: str = None,
                         strategy: ProofStrategy = ProofStrategy.PRIORITY) -> IsabelleAgent:
    """ create Isabelle agent instance """
    if agent_name is None:
        agent_name = f"{agent_type.capitalize()}IsabelleAgent"
    
    if agent_type == "simple":
        return SimpleIsabelleAgent(agent_name)
    else:
        raise ValueError(f"Unknown agent type: {agent_type}. Available: simple")


def quick_prove(gym: IsabelleGym, 
                theorem_statement: str,
                agent_type: str = "simple",
                **kwargs) -> ProofResult:
    """ quick proof function - IsabelleGym convenient interface """
    agent = create_isabelle_agent(agent_type)
    return agent.prove_theorem(gym, theorem_statement, **kwargs)


