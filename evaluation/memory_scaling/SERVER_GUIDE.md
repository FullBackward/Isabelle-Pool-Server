# Memory-scaling experiments — Ubuntu server guide (ASD-STE100 style)

## 1. Scope

This guide tells you how to run the memory-scaling experiments on the Ubuntu server. The experiments
compare IPS and PIDE MCP when one host serves many Isabelle sessions. The scripts are in the folder
`evaluation/memory_scaling/` of the repository. Do the steps in the order given. Do not start a step
before the previous step is complete.

## 2. Requirements

Make sure that the server has these items before you start:

- Ubuntu 22.04 or 24.04 with cgroup version 2 and systemd.
- 128 GB of memory and 60 GB of free disk space.
- Python 3.12 or later, and git.
- No other user who runs Isabelle on the server during the experiments.

WARNING: The pressure experiment (E3) sets a memory limit with systemd. If systemd cannot start a
user scope, the limit does not apply and the result is not valid.

## 3. Install the software (one time, about 30 minutes)

1. Clone the repository and go into it:
   ```bash
   git clone <this repository> ~/Isabelle-Pool-Server
   cd ~/Isabelle-Pool-Server
   git checkout Impl-memory-robustness
   ```
2. Install Isabelle and IPS:
   ```bash
   ./deploy/native_setup.sh --system
   ```
   This installs Isabelle2026-RC2 in `/opt/isabelle`, makes the Python environment, and registers
   the IPS component.
3. Activate the Python environment and install the packages:
   ```bash
   source .venv/bin/activate
   pip install -e ".[mcp,eval]" -e ./client
   export PATH=/opt/isabelle/bin:$PATH
   ```
4. Install PIDE MCP as an Isabelle component. Use the same Isabelle and the same user:
   ```bash
   git clone https://github.com/kappelmann/isabelle-pide-mcp ~/isabelle-pide-mcp
   isabelle components -u ~/isabelle-pide-mcp
   isabelle pide_mcp -?
   ```
   The last command builds PIDE MCP and shows its usage text.
5. Permit systemd user scopes for your user. Do this one time:
   ```bash
   loginctl enable-linger $USER
   ```

NOTE: IPS writes a memory cap for the prover process into
`~/.isabelle/Isabelle2026-RC2/etc/settings`. The cap applies to both tools. This is correct. The
experiment records the cap.

## 4. Check the environment

1. Go to the experiment folder:
   ```bash
   cd evaluation/memory_scaling
   ```
2. Run the check:
   ```bash
   scripts/00_check_env.sh
   ```
3. Read the output. Each line must show PASS.
4. If a line shows FAIL, do the correction that the line gives. Then run the check again.
5. Keep the output. The report uses it as the environment description.

## 5. Build the heaps (one time)

1. Run the build:
   ```bash
   scripts/01_build_heaps.sh
   ```
2. Wait until the build is complete. HOL takes some minutes. HOL-Library takes about 15 minutes.
   HOL-Analysis takes about 45 minutes.
3. The script writes the build times to `results/heap_build_times.txt`.

## 6. Run the smoke test (about 10 minutes)

1. Run the smoke test:
   ```bash
   scripts/02_smoke.sh
   ```
2. The script runs six small cells: one and two sessions on HOL, for each of the three tools.
3. Each cell must show `outcome ok`. If a cell does not show `ok`, do not continue. Read the log of
   that cell in `results/smoke/`.
4. The script prints an estimate of the time for the full matrix. Use it to plan the server time.

## 7. Run experiment E1, the capacity matrix (many hours)

1. Start the matrix in a session that survives a disconnection:
   ```bash
   nohup scripts/10_run_e1.sh > results/e1.log 2>&1 &
   ```
2. Monitor the progress:
   ```bash
   tail -f results/e1.log
   ```
   The log shows one line for each cell.
3. If the run stops, start the same command again. The runner skips the cells that are complete.
4. To run a part of the matrix, add options. Example:
   ```bash
   scripts/10_run_e1.sh --tools ips,pide-b --heaps HOL --ks 1,2,4 --reps 1
   ```

CAUTION: The runner stops a series when the host uses more than 110 GB of memory. This protects the
server. Do not increase this limit.

## 8. Run experiments E2, E3 and E4

1. E2, the warm-hit latency of IPS:
   ```bash
   scripts/11_run_e2_latency.sh
   ```
2. E3, the behaviour under a memory limit of 48 GB on HOL-Analysis:
   ```bash
   LIMIT=48G HEAP=HOL-Analysis scripts/12_run_e3_pressure.sh
   ```
   IPS runs in its own scope with the limit. PIDE MCP runs under a scope around the whole experiment.
   Expect IPS to refuse new sessions. Expect the kernel to stop a PIDE MCP process.
3. E4, the isolation test:
   ```bash
   scripts/13_run_e4_kill.sh
   ```
   The script starts eight sessions, stops one prover process, and asks the other sessions for an
   answer.

## 9. Analyse the results

1. Run the analysis:
   ```bash
   scripts/20_analyze.sh
   ```
2. The script writes these files:
   - `results/tables/cells.csv` — one row for each cell.
   - `results/tables/series.csv` — the memory cost for each session, the fixed cost, and the number
     of sessions per 100 GB, for each tool and heap.
   - `results/plots/*.png` — memory against the number of sessions.
   - `results/REPORT_SNIPPET.md` — the tables for the report.
3. Run the analysis again at any time. It reads all the cells that have a manifest.

## 10. Hand over the results

1. Pack the results:
   ```bash
   tar czf memory-scaling-results.tgz results/
   ```
2. Copy the archive to your computer.
3. Put the tables from `REPORT_SNIPPET.md` and the plots into the report section. The template is in
   `docs/experiments/2026-10-20-memory-scaling-ips-vs-pide-mcp.md`.

## 11. Rules that the scripts apply

- Both tools use the same Isabelle build, the same user, the same heaps, and the same prover memory
  cap.
- Each cell starts a new IPS server in its own systemd scope.
- Each cell uses the same theory file. The manifest records its checksum.
- The sampler is the same for both tools. It reads `/proc` one time per second.
- The analysis reports two slopes: the memory of the tool's processes (PSS) and the memory that the
  host loses. The capacity number uses the host value.
- The manifest records the session options of each tool. The options are not the same by default.
  The report must say this.
