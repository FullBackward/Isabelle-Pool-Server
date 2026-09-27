import argparse
import csv
import sys
import time
from pathlib import Path

from tqdm import tqdm

from local_gym.isabelle_gym import IsabelleGym

from local_gym.success_checker import is_syntax_successful, get_error_message, get_output_message

from benchmark.afp_samples import AFP_SESSIONS

default_timings_save_location = Path(__file__).parent / "isabelle_gym_afp_timings.csv"

parser = argparse.ArgumentParser(description="Benchmark AFP theories using IsabelleGym")
parser.add_argument(
    "--afp_thys_dir", required=True, help="Path to AFP theory directory"
)
parser.add_argument(
    "--output",
    default=default_timings_save_location,
    help=f"Output CSV file path (default: {default_timings_save_location})",
)

args = parser.parse_args()
afp_thys_dir = Path(args.afp_thys_dir)
output = Path(args.output)


def time_gym_thy_file(thy_file: Path, n: int) -> float:
    """
    Benchmark a single theory file and return the elapsed time in seconds.
    """
    gym = IsabelleGym()
    # Run the file through IsabelleGym once to avoid counting dependency building time
    with open(thy_file) as f:
        thy_contents = f.read()
    gym.enter_thy(thy_file.stem)
    thy_result = gym.step(thy_contents)
    if(not is_syntax_successful(thy_result)):
        print(f"Error processing {thy_file} during initial run:", file=sys.stderr)
        print(get_error_message(thy_result), file=sys.stderr)
        gym.close()
        return float('inf')
    gym.rollback()

    start_time = time.time()
    lines = thy_contents.splitlines()
    for i in tqdm(range(0, len(lines), n)):
        batch = "\n".join(lines[i : i + n])
        result = gym.step(batch)
        if not is_syntax_successful(result):
            print(f"Error processing {thy_file}:", file=sys.stderr)
            print(f"Error batch: {batch}", file=sys.stderr)
            print(get_error_message(result), file=sys.stderr)
            gym.close()
            return float('inf')
        print(result.total_output())
    end_time = time.time() - start_time
    gym.close()
    return end_time


def time_gym_afp_session_processing(afp_session: str, n: int) -> float:
    """
    Benchmark all theory files in a session and write results to CSV
    """
    afp_session_dir = afp_thys_dir / afp_session
    if not afp_session_dir.is_dir():
        return None

    total_time = 0
    for thy_file in sorted(afp_session_dir.glob("*.thy")):
        total_time += time_gym_thy_file(thy_file, n)
    return total_time


def main():
    """
    Main function to run timing on all AFP sessions and write to CSV.
    """
    print(
        f"Starting AFP benchmark with {len(AFP_SESSIONS)} sessions using IsabelleGym..."
    )
    print(f"Results will be written to {output}")

    # Create and write to CSV file
    with open(output, "w", newline="") as csvfile:
        csvwriter = csv.writer(csvfile)
        csvwriter.writerow(["Session", "Processing Time (seconds)"])

        # Process each session
        for n in [1, 10, 100]:
            print(f"\n--- Benchmarking with batch size n={n} ---\n")
            csvwriter.writerow(["Batch Size", n])
            for i, session in enumerate(AFP_SESSIONS, 1):
                print(f"[{i}/{len(AFP_SESSIONS)}] Processing session: {session}")

                # Get timing for the session
                elapsed_time = time_gym_afp_session_processing(session, n)

                if elapsed_time is not None:
                    csvwriter.writerow([session, f"{elapsed_time:.2f}"])
                    print(f"  Completed {session} in {elapsed_time:.2f} seconds")
                else:
                    csvwriter.writerow([session, "ERROR"])
                    print(f"  Error processing {session} - directory not found")

                # Flush to write immediately to file
                csvfile.flush()
            csvfile.flush()
            time.sleep(0.5)

    print(f"Benchmark complete. Results written to {output}")


if __name__ == "__main__":
    main()
