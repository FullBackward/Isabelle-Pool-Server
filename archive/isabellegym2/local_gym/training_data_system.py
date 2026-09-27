""" Training Data Extraction System for IsabelleGym """
import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Set, Union
from local_gym.isabelle_agent_interface import IsabelleAgent, ProofContext, ProofResult
from local_gym.isabelle_gym import IsabelleGym
from local_gym.success_checker import is_syntax_successful, get_error_message

@dataclass
class TacticInvocation:
    """
    Data structure recording complete information for each tactic call.
    Used for machine learning training data extraction.
    """
    # basic information
    before: str                    
    after: str                     
    tactic: str                    
    success: bool                  
    
    goal_id: int                   
    subgoals_before: List[str]     
    subgoals_after: List[str]      
    used_constants: List[str]      
    execution_time: float          
    
    # context information
    theorem_statement: str         
    proof_step: int               
    agent_name: str               
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    proof_depth: int = 0          
    total_subgoals: int = 0       
    subgoals_reduced: int = 0    
    
    def to_ml_format(self) -> Dict[str, Any]:
        """convert to machine learning training format"""
        return {
            "input": {
                "goal": self.before,
                "all_subgoals": self.subgoals_before,
                "goal_id": self.goal_id,
                "theorem": self.theorem_statement,
                "context": self.used_constants,
                "proof_step": self.proof_step,
                "depth": self.proof_depth
            },
            "target": self.tactic,
            "success": self.success,
            "metadata": {
                "execution_time": self.execution_time,
                "subgoals_change": f"{len(self.subgoals_before)} → {len(self.subgoals_after)}",
                "agent": self.agent_name,
                "timestamp": self.timestamp
            }
        }

@dataclass  
class ProofSession:
    """
    Complete proof session record containing all session data.
    """
    theorem_statement: str
    agent_name: str
    session_id: str
    start_time: str
    end_time: Optional[str] = None
    
    success: bool = False
    total_steps: int = 0
    total_duration: float = 0.0
    error_message: Optional[str] = None
    
    tactic_invocations: List[TacticInvocation] = field(default_factory=list)
    final_proof: Optional[str] = None
    
    successful_tactics: int = 0
    failed_tactics: int = 0
    unique_tactics_used: Set[str] = field(default_factory=set)
    
    isabelle_version: Optional[str] = None
    theory_context: Optional[str] = None
    
    def add_tactic_invocation(self, invocation: TacticInvocation):
        """add tactic invocation record"""
        self.tactic_invocations.append(invocation)
        self.total_steps += 1
        
        if invocation.success:
            self.successful_tactics += 1
        else:
            self.failed_tactics += 1
            
        self.unique_tactics_used.add(invocation.tactic)
    
    @property
    def success_rate(self) -> float:
        """calculate success rate"""
        if self.total_steps == 0:
            return 0.0
        return self.successful_tactics / self.total_steps
    
    def to_summary(self) -> Dict[str, Any]:
        """generate session summary"""
        return {
            "session_id": self.session_id,
            "theorem": self.theorem_statement,
            "agent": self.agent_name,
            "success": self.success,
            "duration": self.total_duration,
            "steps": self.total_steps,
            "success_rate": self.success_rate,
            "unique_tactics": len(self.unique_tactics_used),
            "start_time": self.start_time,
            "end_time": self.end_time
        }


class ConstantExtractor:
    """ Constant extractor for mathematical constants and symbols in Isabelle tactics. """
    
    def __init__(self):

        self.isabelle_keywords = {
            "by", "apply", "simp", "auto", "blast", "arith", "force",
            "intro", "elim", "cases", "induction", "rule", "erule",
            "assumption", "trivial", "clarify", "safe", "fast",
            "where", "using", "have", "show", "fix", "assume",
            "obtain", "consider", "define", "let", "moreover",
            "ultimately", "hence", "thus", "then", "finally"
        }
        
        self.logic_symbols = {
            "∀", "∃", "∧", "∨", "¬", "⟹", "⟷", "∈", "∉", "⊆", "⊇",
            "∩", "∪", "∅", "=", "≠", "≤", "≥", "<", ">", "∼", "≈"
        }
    
    def extract_from_tactic(self, tactic: str) -> List[str]:
        """ extract used constants from tactic """
        constants = set()
        
        theorem_refs = self._extract_theorem_references(tactic)
        constants.update(theorem_refs)
        
        math_constants = self._extract_math_constants(tactic)
        constants.update(math_constants)
        
        type_constructors = self._extract_type_constructors(tactic)
        constants.update(type_constructors)
        
        function_names = self._extract_function_names(tactic)
        constants.update(function_names)
        
        return sorted(list(constants))
    
    def _extract_theorem_references(self, tactic: str) -> Set[str]:
        """extract theorem and lemma references"""
        constants = set()
        
        rule_pattern = r'(?:rule|erule|drule)\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)'
        for match in re.finditer(rule_pattern, tactic):
            ref = match.group(1)
            if ref not in self.isabelle_keywords:
                constants.add(ref)
        
        using_pattern = r'using\s+([^)]+?)(?:\s|$|by|apply)'
        for match in re.finditer(using_pattern, tactic):
            refs = match.group(1).split()
            for ref in refs:
                clean_ref = ref.strip('(),[]')
                if clean_ref and clean_ref not in self.isabelle_keywords:
                    constants.add(clean_ref)
        
        return constants
    
    def _extract_math_constants(self, tactic: str) -> Set[str]:
        """extract mathematical constants"""
        constants = set()
    
        number_pattern = r'\b\d+\b'
        for match in re.finditer(number_pattern, tactic):
            constants.add(match.group())
        
        for symbol in self.logic_symbols:
            if symbol in tactic:
                constants.add(symbol)
        
        return constants
    
    def _extract_type_constructors(self, tactic: str) -> Set[str]:
        """extract type constructors"""
        constants = set()
        
        type_pattern = r'\b([A-Z][a-zA-Z0-9_]*(?:\.[A-Z][a-zA-Z0-9_]*)*)\b'
        for match in re.finditer(type_pattern, tactic):
            const = match.group(1)
            if const not in self.isabelle_keywords:
                constants.add(const)
        
        return constants
    
    def _extract_function_names(self, tactic: str) -> Set[str]:
        """extract function names and variable names"""
        constants = set()
        
        identifier_pattern = r'\b([a-z_][a-zA-Z0-9_]*)\b'
        for match in re.finditer(identifier_pattern, tactic):
            name = match.group(1)
            
            if (name not in self.isabelle_keywords and 
                len(name) > 1 and 
                not name.isdigit()):
                constants.add(name)
        
        return constants

class TrainingDataCollector:
    """ training data collector """
    def __init__(self, output_dir: str = "training_data", auto_save: bool = True):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.auto_save = auto_save
        
        self.current_session: Optional[ProofSession] = None
        self.collected_sessions: List[ProofSession] = []
        
        self.constant_extractor = ConstantExtractor()
        
        self.total_invocations = 0
        self.total_successful_invocations = 0
    
    def start_session(self, theorem_statement: str, agent_name: str) -> str:
        """ start new proof session """
        session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        
        self.current_session = ProofSession(
            theorem_statement=theorem_statement,
            agent_name=agent_name,
            session_id=session_id,
            start_time=datetime.now().isoformat()
        )
        
        return session_id
    
    def record_tactic_invocation(self,
                                before_state: ProofContext,
                                after_state: ProofContext,
                                tactic: str,
                                goal_id: int,
                                success: bool,
                                execution_time: float,
                                gym: Optional[IsabelleGym] = None):
        """ record single tactic invocation """
        if not self.current_session:
            raise RuntimeError("No active session. Call start_session() first.")
        
        used_constants = self.constant_extractor.extract_from_tactic(tactic)
        
        before_goal = ""
        after_goal = ""
        
        if goal_id >= 0 and goal_id < len(before_state.subgoals):
            before_goal = before_state.subgoals[goal_id]
        
        if goal_id >= 0 and goal_id < len(after_state.subgoals):
            after_goal = after_state.subgoals[goal_id]
        elif len(after_state.subgoals) == 0:
            after_goal = "⊢ True"  # proof completed
        
        subgoals_reduced = len(before_state.subgoals) - len(after_state.subgoals)
        
        invocation = TacticInvocation(
            before=before_goal,
            after=after_goal,
            tactic=tactic,
            success=success,
            goal_id=goal_id,
            subgoals_before=before_state.subgoals.copy(),
            subgoals_after=after_state.subgoals.copy(),
            used_constants=used_constants,
            execution_time=execution_time * 1000,  # milliseconds
            theorem_statement=self.current_session.theorem_statement,
            proof_step=len(self.current_session.tactic_invocations),
            agent_name=self.current_session.agent_name,
            proof_depth=before_state.proof_depth,
            total_subgoals=len(before_state.subgoals),
            subgoals_reduced=subgoals_reduced
        )
        
        self.current_session.add_tactic_invocation(invocation)
        
        self.total_invocations += 1
        if success:
            self.total_successful_invocations += 1
    
    def end_session(self, result: ProofResult):
        """ end current session and save data """
        if not self.current_session:
            return
        
        self.current_session.end_time = datetime.now().isoformat()
        self.current_session.success = result.success
        self.current_session.total_duration = result.duration
        self.current_session.final_proof = result.final_state
        if hasattr(result, 'error_message') and result.error_message:
            self.current_session.error_message = result.error_message
        
        self.collected_sessions.append(self.current_session)
        
        if self.auto_save:
            self._save_session(self.current_session)
        
        self.current_session = None
    
    def _save_session(self, session: ProofSession):
        """ save session to JSON file """
        filename = f"{session.session_id}.json"
        filepath = self.output_dir / filename
        
        session_dict = {
            "session_info": {
                "session_id": session.session_id,
                "theorem_statement": session.theorem_statement,
                "agent_name": session.agent_name,
                "start_time": session.start_time,
                "end_time": session.end_time,
                "success": session.success,
                "total_duration": session.total_duration,
                "error_message": session.error_message
            },
            "statistics": {
                "total_steps": session.total_steps,
                "successful_tactics": session.successful_tactics,
                "failed_tactics": session.failed_tactics,
                "success_rate": session.success_rate,
                "unique_tactics": len(session.unique_tactics_used),
                "tactics_used": list(session.unique_tactics_used)
            },
            "proof_data": {
                "final_proof": session.final_proof,
                "theory_context": session.theory_context
            },
            "tactic_invocations": [
                {
                    "before": inv.before,
                    "after": inv.after,
                    "tactic": inv.tactic,
                    "success": inv.success,
                    "goal_id": inv.goal_id,
                    "subgoals_before": inv.subgoals_before,
                    "subgoals_after": inv.subgoals_after,
                    "used_constants": inv.used_constants,
                    "execution_time": inv.execution_time,
                    "proof_step": inv.proof_step,
                    "timestamp": inv.timestamp,
                    "proof_depth": inv.proof_depth,
                    "total_subgoals": inv.total_subgoals,
                    "subgoals_reduced": inv.subgoals_reduced
                }
                for inv in session.tactic_invocations
            ]
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(session_dict, f, indent=2, ensure_ascii=False)
    
    def get_training_dataset(self, 
                           filter_successful: bool = True,
                           filter_successful_sessions: bool = True) -> List[TacticInvocation]:
        """ get training dataset """
        dataset = []
        
        for session in self.collected_sessions:
            if filter_successful_sessions and not session.success:
                continue
                
            for invocation in session.tactic_invocations:
                if filter_successful and not invocation.success:
                    continue
                    
                dataset.append(invocation)
        
        return dataset
    
    def export_for_ml_training(self, 
                              output_file: str = "ml_training_data.jsonl",
                              format_type: str = "jsonl",
                              **filter_kwargs) -> str:
        """ export machine learning training format data """
        dataset = self.get_training_dataset(**filter_kwargs)
        output_path = self.output_dir / output_file
        
        if format_type == "jsonl":
            with open(output_path, 'w', encoding='utf-8') as f:
                for inv in dataset:
                    ml_record = inv.to_ml_format()
                    f.write(json.dumps(ml_record, ensure_ascii=False) + '\n')
                    
        elif format_type == "csv":
            import csv
            with open(output_path.with_suffix('.csv'), 'w', newline='', encoding='utf-8') as f:
                if dataset:
                    writer = csv.DictWriter(f, fieldnames=[
                        'goal', 'tactic', 'success', 'execution_time', 
                        'subgoals_before', 'subgoals_after', 'agent'
                    ])
                    writer.writeheader()
                    for inv in dataset:
                        writer.writerow({
                            'goal': inv.before,
                            'tactic': inv.tactic,
                            'success': inv.success,
                            'execution_time': inv.execution_time,
                            'subgoals_before': len(inv.subgoals_before),
                            'subgoals_after': len(inv.subgoals_after),
                            'agent': inv.agent_name
                        })
        else:
            raise ValueError(f"Unsupported format: {format_type}")
        
        return str(output_path)
    
    def generate_report(self) -> Dict[str, Any]:
        """generate training data collection report"""
        if not self.collected_sessions:
            return {"error": "No sessions collected"}
        
        successful_sessions = sum(1 for s in self.collected_sessions if s.success)
        total_tactics = sum(len(s.tactic_invocations) for s in self.collected_sessions)
        successful_tactics = sum(s.successful_tactics for s in self.collected_sessions)
        
        tactic_counts = {}
        for session in self.collected_sessions:
            for inv in session.tactic_invocations:
                tactic_counts[inv.tactic] = tactic_counts.get(inv.tactic, 0) + 1
        
        top_tactics = sorted(tactic_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        
        return {
            "summary": {
                "total_sessions": len(self.collected_sessions),
                "successful_sessions": successful_sessions,
                "session_success_rate": successful_sessions / len(self.collected_sessions),
                "total_tactic_invocations": total_tactics,
                "successful_tactics": successful_tactics,
                "tactic_success_rate": successful_tactics / total_tactics if total_tactics > 0 else 0
            },
            "top_tactics": top_tactics,
            "session_summaries": [s.to_summary() for s in self.collected_sessions]
        }


class TrainingDataAgent(IsabelleAgent):
    """ Agent wrapper with integrated training data collection """
    
    def __init__(self, base_agent: IsabelleAgent, collector: TrainingDataCollector):
        super().__init__(f"TrainingData_{base_agent.agent_name}")
        self.base_agent = base_agent
        self.collector = collector
    
    def recommend_tactic(self, gym: IsabelleGym, state: ProofContext, goal_id: int) -> Optional[str]:
        """agent recommend_tactic call"""
        return self.base_agent.recommend_tactic(gym, state, goal_id)
    
    def evaluate_priorities(self, gym: IsabelleGym, state: ProofContext) -> List[float]:
        """agent evaluate_priorities call"""
        return self.base_agent.evaluate_priorities(gym, state)
    
    def prove_theorem(self, gym: IsabelleGym, theorem_statement: str, **kwargs) -> ProofResult:
        """ enhanced prove_theorem method with automatic training data collection """
        session_id = self.collector.start_session(theorem_statement, self.base_agent.agent_name)

        try:

            result = self._search_with_data_collection(gym, theorem_statement, **kwargs)
            
            self.collector.end_session(result)
            
            return result
            
        except Exception as e:
            error_result = ProofResult(
                success=False, duration=0.0, proof_steps=0, final_proof_length=0,
                final_state="", error_message=str(e)
            )
            self.collector.end_session(error_result)
            raise

    def _search_with_data_collection(self, gym: IsabelleGym, theorem_statement: str, **kwargs) -> ProofResult:
        """ implementation of search with data collection """
        import time
        start_time = time.time()
        
        self.reset_session()
        
        try:
            result = gym.step(theorem_statement)
            if not is_syntax_successful(result):
                return ProofResult(
                    success=False, duration=time.time() - start_time,
                    proof_steps=0, final_proof_length=0, final_state="",
                    error_message=f"Failed to start theorem: {get_error_message(result)}"
                )
        except Exception as e:
            return ProofResult(
                success=False, duration=time.time() - start_time,
                proof_steps=0, final_proof_length=0, final_state="",
                error_message=f"Failed to start theorem: {str(e)}"
            )
        
        initial_subgoals = gym.open_subgoals()
        if not initial_subgoals:
            return ProofResult(
                success=False, duration=time.time() - start_time,
                proof_steps=0, final_proof_length=0, final_state="",
                error_message="Failed to start theorem: No subgoals found after theorem statement"
            )
        
        initial_priorities = [0.0] * len(initial_subgoals)
        
        current_state = ProofContext(
            subgoals=initial_subgoals,
            priorities=initial_priorities,
            current_theorem=theorem_statement,
            proof_depth=0
        )
        
        max_steps = kwargs.get('max_steps', 50)
        max_depth = kwargs.get('max_depth', 20)
        timeout = kwargs.get('timeout', 30.0)
        verbose = kwargs.get('verbose', False)
        # main search loop
        for step in range(max_steps):
            if time.time() - start_time > timeout:
                return ProofResult(
                    success=False, duration=time.time() - start_time,
                    proof_steps=step, final_proof_length=current_state.proof_depth,
                    final_state=gym.get_source().total_output(),
                    error_message=f"Search timeout after {timeout}s"
                )
            
            if current_state.is_complete:
                return ProofResult(
                    success=True, duration=time.time() - start_time,
                    proof_steps=step, final_proof_length=current_state.proof_depth,
                    final_state=gym.get_source().total_output()
                )
            
            if current_state.proof_depth >= max_depth:
                return ProofResult(
                    success=False, duration=time.time() - start_time,
                    proof_steps=step, final_proof_length=current_state.proof_depth,
                    final_state=gym.get_source().total_output(),
                    error_message=f"Maximum depth {max_depth} reached"
                )
            
            if len(current_state.subgoals) > 1:
                priorities = self.base_agent.evaluate_priorities(gym, current_state)
                current_state.priorities = priorities
            
            goal_id = current_state.active_goal_id
            if goal_id < 0:
                return ProofResult(
                    success=False, duration=time.time() - start_time,
                    proof_steps=step, final_proof_length=current_state.proof_depth,
                    final_state=gym.get_source().total_output(),
                    error_message="No available goals"
                )
            
            tactic = self.base_agent.recommend_tactic(gym, current_state, goal_id)
            if not tactic:
                return ProofResult(
                    success=False, duration=time.time() - start_time,
                    proof_steps=step, final_proof_length=current_state.proof_depth,
                    final_state=gym.get_source().total_output(),
                    error_message="No more tactics available"
                )
            
            before_state = ProofContext(
                subgoals=current_state.subgoals.copy(),
                priorities=current_state.priorities.copy(),
                current_theorem=current_state.current_theorem,
                proof_depth=current_state.proof_depth
            )
            
            tactic_start_time = time.time()
            tactic_result = gym.step(tactic)
            tactic_execution_time = time.time() - tactic_start_time
            
            after_subgoals = gym.open_subgoals()
            after_state = ProofContext(
                subgoals=after_subgoals,
                priorities=[0.0] * len(after_subgoals),
                current_theorem=current_state.current_theorem + f"\n{tactic}",
                proof_depth=current_state.proof_depth + (1 if is_syntax_successful(tactic_result) else 0)
            )
            
            syntax_success = is_syntax_successful(tactic_result)
            real_progress = (len(after_subgoals) != len(before_state.subgoals) or 
                           after_subgoals != before_state.subgoals)
            actual_success = syntax_success and real_progress
            
            # record tactic call to training data
            self.collector.record_tactic_invocation(
                before_state=before_state,
                after_state=after_state,
                tactic=tactic,
                goal_id=goal_id,
                success=actual_success,
                execution_time=tactic_execution_time,
                gym=gym
            )
            
            if verbose:
                status = "SUCCESS" if actual_success else "FAILURE"
                print(f"[{status}] Step {step+1}: {tactic} "
                      f"({len(before_state.subgoals)} → {len(after_subgoals)} subgoals)")
            
            if actual_success:
                # update search state
                current_state = after_state
            else:
                # record but do not update state
                current_state.attempted_tactics.append(tactic)
        
        # search steps exhausted
        return ProofResult(
            success=False, duration=time.time() - start_time,
            proof_steps=max_steps, final_proof_length=current_state.proof_depth,
            final_state=gym.get_source().total_output(),
            error_message=f"Maximum steps {max_steps} reached"
        )


def create_training_data_agent(base_agent: IsabelleAgent, 
                              output_dir: str = "training_data") -> TrainingDataAgent:
    
    """ create agent with integrated training data collection """

    collector = TrainingDataCollector(output_dir)
    return TrainingDataAgent(base_agent, collector)


 