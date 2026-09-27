"""
Fixed miniF2F Demo with proper theory imports
"""

from local_gym.isabelle_gym import IsabelleGym
from local_gym.isabelle_agent_interface import SimpleIsabelleAgent
from local_gym.success_checker import is_syntax_successful, get_error_message, get_output_message


class AgentProofDemo:
    def __init__(self):
        self.gym = IsabelleGym()
        
        self.gym.enter_thy("Demo")
        self.gym.step('theory Demo imports Main begin')
        
        self.agent = SimpleIsabelleAgent("BasicAgent")
        
    def run_demo(self):
        #test_data = miniF2F_init()
        #test_data = test_data.select(range(self.e_n))
        test_data = [
            'lemma conj_commute: "P ∧ Q ⟹ Q ∧ P"',
            'lemma part1_2: "(Q ∧ R) ∧ P ⟶ (P ∧ R) ∧ Q"',
            'lemma part1_3: "(A ⟶ B) ∧ (C ⟶ D) ∧ (A ∨ C) ⟶ B ∨ D"',
            'lemma Zoey_and_Mel: "⟦ Say 1 z = Mag m;Say 1 m = (¬ Mag z ∧ ¬ Mag m)⟧ ⟹ Spa z ∧ Mag m"',
            """definition Flower :: "'a ⇒ bool" where "Flower x ≡ (Spa x ∨ Mag x)" lemma Abel_and_Beatrice:
  "⟦ ((∃ x. Flower x) ⟶ (∀ y. Spa y)) = Say 1 a
    ;Say 1 b = (¬ (∀ x y. Flower x ⟶ Spa y))
    ;Say 2 a = Mag b
    ;Say 2 b = (Spa a ∧ Spa b)
   ⟧ ⟹ ¬ Flower a ∧ ¬ Flower b ∧ Spa a ∧ Mag b" """
            ]
        
        success_count = 0
        
        for idx, problem in enumerate(test_data):
            print(f"\n{'='*60}")
            print(f"Problem {idx+1}/{len(test_data)}: {problem}")
            print(f"{'='*60}")
            print(f"\n{'-'*60}")
            
            """
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
            result3 = self.gym.step("sledgehammer")
            if(not is_syntax_successful(result3)):
                print("Error running sledgehammer:")
                print(get_error_message(result3))
                print(f"{'-'*60}")
                break
            subgoals = self.gym.open_subgoals()
            print(subgoals)
            """
            # Try to prove
            result = self.agent.prove_theorem(
                gym=self.gym,
                theorem_statement=problem,
                max_steps=30,  # Increased for better chance
                timeout=15.0,  # Increased timeout
                verbose=False,
                enable_visualization=True
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
            self.agent.reset_session()
            print(f"{'-'*60}")
        
        # Summary
        print(f"\n{'='*60}")
        print(f"SUMMARY: {success_count}/{len(test_data)} proofs succeeded")
        print(f"Success rate: {success_count/len(test_data):.1%}")
        print(f"{'='*60}")
    
    def close(self):
        """Clean up resources"""
        print("\nClosing Isabelle session...")
        self.gym.close()


if __name__ == "__main__":
    # Run demo with 3 problems
    demo = AgentProofDemo()
    
    try:
        demo.run_demo()
    finally:
        demo.close()