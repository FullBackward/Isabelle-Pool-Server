import argparse
import csv
import sys
import time
from pathlib import Path

from local_gym.isabelle_gym import IsabelleGym
from local_gym.session_pool import FastIsabelleGym

default_timings_save_location = Path(__file__).parent / "isabelle_fast_gym_timings.csv"

parser = argparse.ArgumentParser(description="Benchmark for comparing session pool vs regular IsabelleGym")
parser.add_argument(
    "--output",
    default=default_timings_save_location,
    help=f"Output CSV file path (default: {default_timings_save_location})",
)

args = parser.parse_args()
output = Path(args.output)

def time_regular_gym_session() -> float:
    """
    Benchmark a session using regular IsabelleGym and return the elapsed time in seconds.
    """
    start_time = time.time()
    gym1 = IsabelleGym()
    gym2 = IsabelleGym()
    gym3 = IsabelleGym()
    use_time = time.time() - start_time
    gym1.close()
    gym2.close()
    gym3.close()
    return use_time
    
def time_single_session_gym() -> float:
    """
    Benchmark a session using regular IsabelleGym and return the elapsed time in seconds.
    """
    start_time = time.time()
    gym = IsabelleGym()
    use_time = time.time() - start_time
    time.sleep(0.5)
    gym.close()
    return use_time

def time_session_pool_session() -> float:
    """
    Benchmark a session using IsabelleFastGym (session pool) and return the elapsed time in seconds.
    """
    start_time = time.time()
    gym1 = FastIsabelleGym()
    gym2 = FastIsabelleGym()
    gym3 = FastIsabelleGym()
    use_time = time.time() - start_time
    gym1.close()
    gym2.close()
    gym3.close()
    return use_time

def time_old_session_pool_session() -> float:
    """
    Benchmark a session using IsabelleFastGym (session pool) and return the elapsed time in seconds.
    """
    start_time = time.time()
    default_session = IsabelleGym(enable_cache=True, shared_cache=True)
    session1 = default_session.create_session("test_session_1")
    session2 = default_session.create_session("test_session_2")
    use_time = time.time() - start_time
    default_session.close()
    return use_time



with open(output, mode="w", newline="") as csvfile:
    fieldnames = ["method", "elapsed_time_seconds"]
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()
    time_single_session_gym_time = time_single_session_gym()
    print("single regular gym time: ", time_single_session_gym_time)
    print("================================")
    writer.writerow({"method": "single_regular_gym", "elapsed_time_seconds": time_single_session_gym_time})

    regular_gym_time = time_regular_gym_session()
    print("3 sessions regular gym time: ", regular_gym_time)
    print("================================")
    writer.writerow({"method": "3_sessions_regular_gym", "elapsed_time_seconds": regular_gym_time})

    session_pool_time = time_session_pool_session()
    print("3 sessions pool gym time: ", session_pool_time)
    print("================================")
    writer.writerow({"method": "3_sessions_pool", "elapsed_time_seconds": session_pool_time})

    old_session_pool_time = time_old_session_pool_session()
    print("3 LRU time: ", old_session_pool_time)
    print("================================")
    writer.writerow({"method": "3_old_session_pool", "elapsed_time_seconds": old_session_pool_time})

print(f"Benchmark results saved to {output}")
