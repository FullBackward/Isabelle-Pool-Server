""" Correct success evaluation helper functions. """
from typing import List, Optional
from repl.src.python.repl_backend_gateway import ReplResult
from local_gym.isabelle_gym import IsabelleGym

def has_error_output(result: ReplResult) -> bool:
    """ check if ReplResult has error output """
    try:
        separated = result.separated_output()
        error_output = separated.error()

        if error_output is None:
            return False

        error_output = error_output.strip()

        if len(error_output) == 0:
            return False
        suspicious_lines = []
        for line in error_output.splitlines():
            stripped = line.strip()
            if (
                stripped == ""
                or stripped.startswith("###")
                or stripped.startswith("Warning:")
                or stripped.startswith("note:")
                or stripped.startswith("Warning —")
            ):

                continue
            suspicious_lines.append(stripped)


        return len(suspicious_lines) > 0
    except Exception:

        return True

def is_syntax_successful(result: ReplResult) -> bool:
    """ check if syntax execution is successful """
    return not has_error_output(result)

def is_proof_progress(before_subgoals: List[str], after_subgoals: List[str]) -> bool:
    """ check if there is real proof progress """
    # if subgoals number reduced
    if len(after_subgoals) < len(before_subgoals):
        return True
    
    # if subgoals content changed
    if len(after_subgoals) == len(before_subgoals):
        return after_subgoals != before_subgoals
    
    # if subgoals number increased
    if len(after_subgoals) > len(before_subgoals):

        # if there were subgoals before
        return len(before_subgoals) > 0
    
    return False

def is_tactic_successful(gym: IsabelleGym, 
                        result: ReplResult, 
                        before_subgoals: Optional[List[str]] = None) -> bool:
    """ check if tactic execution is truly successful """

    if not is_syntax_successful(result):
        return False
    
    if before_subgoals is None:
        return True

    after_subgoals = gym.open_subgoals()
    return is_proof_progress(before_subgoals, after_subgoals)


def get_error_message(result: ReplResult) -> str:
    """ get error message from ReplResult """
    try:
        separated = result.separated_output()
        return separated.error().strip()
    except Exception:
        return "Unknown error occurred"


def get_output_message(result: ReplResult) -> str:
    """ get output message from ReplResult """
    try:
        separated = result.separated_output()
        return separated.output().strip()
    except Exception:
        return ""

class SuccessResult:
    """ wrap ReplResult """
    def __init__(self, result: ReplResult, gym: IsabelleGym, before_subgoals: Optional[List[str]] = None):
        self.result = result
        self.gym = gym
        self.before_subgoals = before_subgoals
        
    @property
    def success(self) -> bool:
        return is_tactic_successful(self.gym, self.result, self.before_subgoals)
    
    def separated_output(self):
        """proxy separated_output method"""
        return self.result.separated_output()
    
    def total_output(self):
        """proxy total_output method"""
        return self.result.total_output()
    
    def __str__(self):
        return str(self.result) 