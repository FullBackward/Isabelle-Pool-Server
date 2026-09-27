"""
Fixed miniF2F Demo with proper theory imports
"""

from datasets import load_dataset
from local_gym.isabelle_gym import IsabelleGym
from local_gym.isabelle_agent_interface import SimpleIsabelleAgent
from local_gym.success_checker import is_syntax_successful, get_error_message, get_output_message
from pathlib import Path

DIR_ROOT = Path(__file__).parent.parent
thys = DIR_ROOT / "repl" / "thys"

def miniF2F_init():
    """Load miniF2F dataset"""
    dataset = load_dataset("wellecks/minif2f_isabelle")
    return dataset["test"]


class AgentProofDemo:
    def __init__(self, example_count: int = 5):
        #print(thys.__str__() + "/Complex_Main_init")
        self.gym = IsabelleGym(initial_thys=["Complex_Main_init"])
        self.e_n = example_count
        
        self.gym.enter_thy("Demo")
        print("Initializing Isabelle with Complex_Main...")
        self.gym.step('theory Demo imports Complex_Main begin')
        print("Initialization complete.")
        self.agent = SimpleIsabelleAgent("BasicAgent")
        print("Agent initialized.")
        
    def run_demo(self):
        test_data = miniF2F_init()
        test_data = test_data.select(range(self.e_n))
        
        success_count = 0
        
        for idx, problem in enumerate(test_data):
            print(f"\n{'='*60}")
            print(f"Problem {idx+1}/{self.e_n}: {problem['problem_name']}")
            print(f"{'='*60}")
            print(f"Informal: {problem['informal_statement']}")
            print(f"\nFormal statement:")
            print(f"{problem['formal_statement']}")
            print(f"\n{'-'*60}")
            
            
            result1 = self.gym.step(problem["formal_statement"])
            if(not is_syntax_successful(result1)):
                print("Error in formal statement:")
                print(get_error_message(result1))
                print(f"{'-'*60}")
                break
            result2 = self.gym.step("proof -")
            if(not is_syntax_successful(result2)):
                print("Error starting proof:")
                print(get_error_message(result2))
                print(f"{'-'*60}")
                break
            subgoals = self.gym.open_subgoals()
            print(subgoals)
            result3 = self.gym.step("apply sledgehammer")
            subgoals = self.gym.open_subgoals()
            print(subgoals)
            """
            # Try to prove
            result = self.agent.prove_theorem(
                gym=self.gym,
                theorem_statement=problem["formal_statement"],
                max_steps=30,  # Increased for better chance
                timeout=15.0,  # Increased timeout
                verbose=False
            )
            
            if result.success:
                print(f"✓ PROOF SUCCEEDED!")
                print(f"  Duration: {result.duration:.2f}s")
                print(f"  Steps: {result.proof_steps}")
                print(f"  Tactics used: {result.tactic_attempts}")
                print(f"  Success rate: {result.success_rate:.1%}")
                success_count += 1
            else:
                print(f"✗ PROOF FAILED")
                print(f"  Error: {result.error_message}")
                print(f"  Steps attempted: {result.proof_steps}")
            """
            
            print(f"{'-'*60}")
        
        # Summary
        print(f"\n{'='*60}")
        print(f"SUMMARY: {success_count}/{self.e_n} proofs succeeded")
        print(f"Success rate: {success_count/self.e_n:.1%}")
        print(f"{'='*60}")
    
    def close(self):
        """Clean up resources"""
        print("\nClosing Isabelle session...")
        self.gym.close()


if __name__ == "__main__":
    # Run demo with 3 problems
    demo = AgentProofDemo(example_count=1)
    
    try:
        demo.run_demo()
    finally:
        demo.close()