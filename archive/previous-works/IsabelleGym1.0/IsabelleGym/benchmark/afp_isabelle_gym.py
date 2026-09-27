import argparse
import csv
import sys
import time
from pathlib import Path

from tqdm import tqdm

from gym.isabelle_gym import IsabelleGym

from .afp_samples import AFP_SESSIONS

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


def time_gym_thy_file(thy_file: Path) -> float:
    """
    Benchmark a single theory file and return the elapsed time in seconds.
    """
    gym = IsabelleGym()
    # Run the file through IsabelleGym once to avoid counting dependency building time
    with open(thy_file) as f:
        thy_contents = f.read()
    gym.enter_thy(thy_file.stem)
    gym.step(thy_contents)
    gym.rollback()

    n = 100
    start_time = time.time()
    lines = thy_contents.splitlines()
    for i in tqdm(range(0, len(lines), n)):
        batch = "\n".join(lines[i : i + n])
        gym.step(batch)

    return time.time() - start_time


def time_gym_afp_session_processing(afp_session: str) -> float:
    """
    Benchmark all theory files in a session and write results to CSV
    """
    afp_session_dir = afp_thys_dir / afp_session
    if not afp_session_dir.is_dir():
        return None

    total_time = 0
    for thy_file in sorted(afp_session_dir.glob("*.thy")):
        total_time += time_gym_thy_file(thy_file)
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
        for i, session in enumerate(AFP_SESSIONS, 1):
            print(f"[{i}/{len(AFP_SESSIONS)}] Processing session: {session}")

            # Get timing for the session
            elapsed_time = time_gym_afp_session_processing(session)

            if elapsed_time is not None:
                csvwriter.writerow([session, f"{elapsed_time:.2f}"])
                print(f"  Completed {session} in {elapsed_time:.2f} seconds")
            else:
                csvwriter.writerow([session, "ERROR"])
                print(f"  Error processing {session} - directory not found")

            # Flush to write immediately to file
            csvfile.flush()

    print(f"Benchmark complete. Results written to {output}")


if __name__ == "__main__":
    main()
