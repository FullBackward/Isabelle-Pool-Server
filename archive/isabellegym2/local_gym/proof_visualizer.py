"""Proof State Visualizer providing structured proof state display and feedback."""

import json
import time
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class TacticResult(Enum):
    """Tactic execution result types."""
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    PARTIAL = "PARTIAL"
    PENDING = "PENDING"


@dataclass
class TacticExecution:
    """Single tactic execution record."""
    tactic: str
    timestamp: float
    result: TacticResult
    before_subgoals: List[str]
    after_subgoals: List[str]
    error_message: Optional[str] = None
    execution_time: float = 0.0
    
    @property
    def subgoals_changed(self) -> bool:
        """Check if subgoals have changed."""
        return self.before_subgoals != self.after_subgoals
    
    @property
    def progress_made(self) -> bool:
        """Check if progress was made (subgoals reduced or content changed)."""
        return (len(self.after_subgoals) < len(self.before_subgoals) or 
                self.subgoals_changed)


@dataclass
class ProofSession:
    """Complete proof session record."""
    theorem_statement: str
    start_time: float
    agent_name: str
    executions: List[TacticExecution] = field(default_factory=list)
    final_success: bool = False
    total_steps: int = 0
    
    def add_execution(self, execution: TacticExecution):
        """add tactic execution"""
        self.executions.append(execution)
        self.total_steps += 1
    
    @property
    def duration(self) -> float:
        """get total duration"""
        if not self.executions:
            return 0.0
        return self.executions[-1].timestamp - self.start_time
    
    @property
    def success_rate(self) -> float:
        """get success rate"""
        if not self.executions:
            return 0.0
        successful = sum(1 for ex in self.executions 
                        if ex.result == TacticResult.SUCCESS)
        return successful / len(self.executions)


class ProofVisualizer:
    """Proof state visualizer with session management and display capabilities."""
    
    def __init__(self, max_subgoal_display: int = 3, 
                 max_history_display: int = 10):
        self.max_subgoal_display = max_subgoal_display
        self.max_history_display = max_history_display
        self.current_session: Optional[ProofSession] = None
        
    def start_session(self, theorem_statement: str, agent_name: str):
        """start new proof session"""
        self.current_session = ProofSession(
            theorem_statement=theorem_statement,
            start_time=time.time(),
            agent_name=agent_name
        )
        
        print(f"Start")
        print(f"Theorem: {theorem_statement}")
        print(f"Agent: {agent_name}")
        print(f"Started: {time.strftime('%H:%M:%S')}")
    
    def record_tactic_execution(self, 
                              tactic: str,
                              before_subgoals: List[str],
                              after_subgoals: List[str],
                              success: bool,
                              error_message: Optional[str] = None,
                              execution_time: float = 0.0):
        """record tactic execution"""
        if not self.current_session:
            return
        
        # determine execution result
        if success:
            if len(after_subgoals) == 0:
                result = TacticResult.SUCCESS  
            elif len(after_subgoals) < len(before_subgoals):
                result = TacticResult.SUCCESS  
            elif before_subgoals != after_subgoals:
                result = TacticResult.SUCCESS  
            else:
                result = TacticResult.FAILURE  
        else:
            result = TacticResult.FAILURE
        
        execution = TacticExecution(
            tactic=tactic,
            timestamp=time.time(),
            result=result,
            before_subgoals=before_subgoals.copy(),
            after_subgoals=after_subgoals.copy(),
            error_message=error_message,
            execution_time=execution_time
        )
        
        self.current_session.add_execution(execution)
        self._display_tactic_result(execution)
    
    def _display_tactic_result(self, execution: TacticExecution):
        """display single tactic execution result"""
        step_num = len(self.current_session.executions)
        
        print(f"\nStep {step_num}: {execution.tactic}")
        print(f"   Result: {execution.result.value} "
              f"({execution.execution_time:.3f}s)")
        
        # display subgoals change
        before_count = len(execution.before_subgoals)
        after_count = len(execution.after_subgoals)
        
        if execution.result == TacticResult.SUCCESS:
            if after_count == 0:
                print(f"   PROOF COMPLETED! ({before_count} → 0 subgoals)")
            elif after_count < before_count:
                print(f"   Progress made: {before_count} → {after_count} subgoals")
            elif execution.subgoals_changed:
                print(f"   Subgoals transformed: {before_count} → {after_count}")
        else:
            print(f"   No progress: {before_count} subgoals remain")
            if execution.error_message:
                print(f"   Error: {execution.error_message}")
        
        # show current subgoals
        if execution.after_subgoals:
            print(f"   Current subgoals:")
            for i, subgoal in enumerate(execution.after_subgoals[:self.max_subgoal_display]):
                # simplify subgoal display
                simplified = self._simplify_subgoal(subgoal)
                print(f"      {i+1}. {simplified}")
            
            if len(execution.after_subgoals) > self.max_subgoal_display:
                remaining = len(execution.after_subgoals) - self.max_subgoal_display
                print(f"      ... and {remaining} more subgoals")
    
    def _simplify_subgoal(self, subgoal: str) -> str:
        """simplify subgoal display"""
        simplified = ' '.join(subgoal.split())
        
        if len(simplified) > 100:
            simplified = simplified[:97] + "..."
        
        return simplified
    
    def display_current_state(self, subgoals: List[str], 
                            proof_depth: int = 0,
                            tested_tactics: List[str] = None):
        """display current proof state"""
        print(f"\n====================== Current proof state ======================")
        
        if not subgoals:
            print(f"Proof completed")
            return
        
        print(f"Subgoals ({len(subgoals)}):")
        for i, subgoal in enumerate(subgoals[:self.max_subgoal_display]):
            simplified = self._simplify_subgoal(subgoal)
            print(f"   {i+1}. {simplified}")
        
        if len(subgoals) > self.max_subgoal_display:
            remaining = len(subgoals) - self.max_subgoal_display
            print(f"   ... and {remaining} more subgoals")
        
        print(f"Proof depth: {proof_depth}")
        
        if tested_tactics:
            print(f"Recently tested tactics:")
            for tactic in tested_tactics[-3:]: 
                print(f"   - {tactic}")
        
        print(f"{'────────────────────────────────────────────────────'}")
    
    def display_tactics_history(self, limit: int = None):
        """display tactics execution history"""
        if not self.current_session or not self.current_session.executions:
            print("No tactics history available")
            return
        
        display_limit = limit or self.max_history_display
        recent_executions = self.current_session.executions[-display_limit:]
        
        print(f"\ntactics history (last {len(recent_executions)} steps)")
        print(f"{'='*60}")
        
        for i, execution in enumerate(recent_executions, 1):
            step_num = len(self.current_session.executions) - len(recent_executions) + i
            progress_indicator = "[PROGRESS]" if execution.progress_made else "[STATIC]"
            
            print(f"{step_num:2d}. {execution.result.value} {progress_indicator} "
                  f"{execution.tactic} ({execution.execution_time:.3f}s)")
            
            if execution.error_message:
                print(f"     {execution.error_message}")
        
        print(f"{'='*60}")
    
    def display_session_summary(self):
        """display session summary"""
        if not self.current_session:
            print("No active session")
            return
        
        session = self.current_session
        
        print(f"\n====================== Proof Session Summary ======================")
        print(f"Theorem: {session.theorem_statement}")
        print(f"Agent: {session.agent_name}")
        print(f"Duration: {session.duration:.2f}s")
        print(f"Total steps: {session.total_steps}")
        print(f"Success rate: {session.success_rate:.1%}")
        
        if session.executions:
            successful_tactics = [ex.tactic for ex in session.executions 
                                if ex.result == TacticResult.SUCCESS]
            if successful_tactics:
                print(f"Successful tactics:")
                for tactic in set(successful_tactics):
                    count = successful_tactics.count(tactic)
                    print(f"   - {tactic} ({count}x)")
        
        print(f"Final result: {'SUCCESS' if session.final_success else 'INCOMPLETE'}")
        print(f"{'='*80}")
    
    def end_session(self, success: bool = False):
        """end current session"""
        if self.current_session:
            self.current_session.final_success = success
            self.display_session_summary()
            self.current_session = None
    
    def export_session_data(self, filepath: str):
        """export session data to JSON file"""
        if not self.current_session:
            print("No active session to export")
            return
        
        # prepare export data
        export_data = {
            "theorem_statement": self.current_session.theorem_statement,
            "agent_name": self.current_session.agent_name,
            "start_time": self.current_session.start_time,
            "duration": self.current_session.duration,
            "total_steps": self.current_session.total_steps,
            "success_rate": self.current_session.success_rate,
            "final_success": self.current_session.final_success,
            "executions": [
                {
                    "step": i + 1,
                    "tactic": ex.tactic,
                    "timestamp": ex.timestamp,
                    "result": ex.result.name,
                    "before_subgoals_count": len(ex.before_subgoals),
                    "after_subgoals_count": len(ex.after_subgoals),
                    "progress_made": ex.progress_made,
                    "error_message": ex.error_message,
                    "execution_time": ex.execution_time
                }
                for i, ex in enumerate(self.current_session.executions)
            ]
        }
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
            print(f"Session data exported to: {filepath}")
        except Exception as e:
            print(f"Failed to export session data: {e}")


def create_visualizer(max_subgoal_display: int = 3, 
                     max_history_display: int = 10) -> ProofVisualizer:
    """create proof visualizer instance"""
    return ProofVisualizer(max_subgoal_display, max_history_display)
