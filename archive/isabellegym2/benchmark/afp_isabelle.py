import argparse
import csv
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from benchmark.afp_samples import AFP_SESSIONS

default_timings_save_location = Path(__file__).parent / "isabelle_afp_timings.csv"
parser = argparse.ArgumentParser(description="AFP Isabelle benchmark tool")
parser.add_argument(
    "--afp_thys_dir",
    required=True,
    help="Path to AFP theory directory",
)
parser.add_argument(
    "--isabelle_path",
    required=True,
    help="Path to Isabelle executable",
)
parser.add_argument(
    "--output",
    default=default_timings_save_location,
    help=f"Output CSV file path (default: {default_timings_save_location})",
)

args = parser.parse_args()
afp_thys_dir = args.afp_thys_dir
isabelle_path = args.isabelle_path
output_file = args.output


def time_build_afp_session(afp_session: str) -> float:
    """
    Time the build of a specific AFP session.
    Returns the elapsed time in seconds for the specific session.
    """
    isabelle_build_command = (
        f"{isabelle_path} build -j 1 -o threads=1 -d {afp_thys_dir} {afp_session}"
    )

    # Execute the command and capture output
    process = subprocess.Popen(
        isabelle_build_command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Initialize variable to store elapsed time
    elapsed_time_seconds = None

    # Read output line by line
    for line in process.stdout:
        # Look for lines that indicate the session has finished
        match = re.search(
            rf"Finished {re.escape(afp_session)} \((\d+):(\d+):(\d+) elapsed time", line
        )
        if match:
            hours, minutes, seconds = map(int, match.groups())
            elapsed_time_seconds = (hours * 60 + minutes) * 60 + seconds

    # Wait for process to complete
    process.wait()

    return elapsed_time_seconds


def main():
    """
    Main function to run timing on all AFP sessions and write to CSV.
    These tests assume that the AFP sessions have not yet been built.
    """
    print(f"Starting AFP benchmark with {len(AFP_SESSIONS)} sessions...")
    print(f"Results will be written to {output_file}")

    # Create and write to CSV file
    with open(output_file, "w", newline="") as csvfile:
        csvwriter = csv.writer(csvfile)
        csvwriter.writerow(["Session", "Time"])

        # Process each session
        for i, session in enumerate(AFP_SESSIONS, 1):
            print(f"[{i}/{len(AFP_SESSIONS)}] Processing session: {session}")
            elapsed_time = time_build_afp_session(session)

            if elapsed_time is not None:
                csvwriter.writerow([session, elapsed_time])
                print(f"  Completed {session} in {elapsed_time} seconds")
            else:
                csvwriter.writerow([session, "ERROR"])
                print(f"  Error processing {session}")

            # Flush to write immediately to file
            csvfile.flush()

    print(f"Benchmark complete. Results written to {output_file}")


if __name__ == "__main__":
    main()
