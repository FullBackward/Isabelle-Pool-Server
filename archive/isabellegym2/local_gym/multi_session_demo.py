from local_gym.multi_session_agent_framework import MultiSessionAgentFramework
from local_gym.proof_visualizer import ProofVisualizer
from datasets import load_dataset

def miniF2F_init():
    dataset = load_dataset("miniF2F", "all")
    train_data = dataset["train"]
    test_data = dataset["test"]
    validation_data = dataset["validation"]
    return train_data, test_data, validation_data

class MultiSessionDemo:
    def __init__(self, example_count: int = 5):
        self.framework = MultiSessionAgentFramework(show_states=False, enable_cache=True, max_cache_size=50, enable_memory_management=True, shared_cache=True)
        self.visualizer = ProofVisualizer()
        self.e_n = example_count

    def run_demo(self):
        train_data, test_data, validation_data = miniF2F_init()
        train_data, test_data = train_data.select(range(self.e_n)), test_data.select(range(self.e_n))
        for idx, problem in enumerate(train_data):
            session_id = f"session_{idx}"
            self.framework.create_session(session_id, initial_thys=["HOL"])
            self.framework.switch_session(session_id)
            theory_name = problem['theory_name']
            lemma_statement = problem['lemma_statement']
            self.framework.enter_thy(theory_name)
            self.framework.step(f"lemma {lemma_statement}")
            # Here you would implement the proof search logic
            proof_found = False  # Placeholder for actual proof search result
            if proof_found:
                self.framework.step("end")
                source = self.framework.source()
                self.visualizer.visualize_proof(source)
            self.framework.close_session(session_id)
