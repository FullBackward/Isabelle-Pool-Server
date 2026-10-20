# Memory and capacity with many sessions: IPS vs PIDE MCP (template — filled from results/REPORT_SNIPPET.md)

Status: experiment section built (`evaluation/memory_scaling/`, plan `claude-work/2026-10-20-plan-memory-scaling-experiments/`);
numbers below are placeholders until the server runs land. Everything here is deterministic (no LLM).

## Setup (Appendix D block — paste `scripts/00_check_env.sh`'s output)

- Host: [hostname, CPU, cores, RAM]; kernel [..]; cgroup v2; `systemd-run --user --scope` available.
- Isabelle: Isabelle2026-RC2, id [..]; one install for both tools; `ML_OPTIONS` [..] (the same per-process heap cap
  for every `poly` of both tools); `ISABELLE_TOOL_JAVA_OPTIONS` [..].
- IPS commit [..] (memory management ON: thresholds [..]); PIDE MCP commit [..] (`main`, pinned to RC2).
- Workloads: `CTL.thy` (HOL), `Termination.thy` (HOL-Library), `Sum_Topology.thy` (HOL-Analysis); sha256 in the manifests.
- Phases per cell: P0 60 s, P1 start k, P2 60 s, P3 load each, P4 60 s, P5 concurrent re-check, P6 stop; sampler 1 s;
  steady state = median of the last 30 s of a phase. Repeats: 3 (1 for k ≥ 16).

## What was measured

- Per process under each tool: RSS, **PSS** (the headline: heap images are file-backed and partly shared between
  `poly` processes of one heap), USS; per tool the sum by role (JVM / prover); host used = MemTotal − MemAvailable;
  the tool's cgroup `memory.current` / `memory.peak`.
- Per (tool, heap): least-squares marginal memory per session and fixed cost, on PSS and on host used; "sessions per
  100 GB" on the host-used slope; the JVM's growth over a series; start / load / re-check latencies (p50, p95).

## E1 — capacity curves

[Figure: `results/plots/host_settled_mb.png` — host used after the loads vs k, one panel per heap, three lines.]
[Figure: `results/plots/pss_peak_mb.png` — tool PSS at the concurrent re-check.]

[Table from REPORT_SNIPPET.md: marginal / fixed (host and PSS), peak marginal, sessions per 100 GB, latencies.]

Reading: [IPS: one gateway JVM (fixed cost F_ips) + m_ips per session; PIDE A: one MCP JVM + m_a per session; PIDE B:
one JVM per session, so its marginal cost carries a JVM (m_b = JVM + prover).]

## E2 — start latency

[Table: cold start p50/p95 per heap for the three tools (P1); IPS warm hit p50/p95 (`warm-ips-<heap>/manifest.json`).]

## E3 — pressure under a 48 GB limit (HOL-Analysis)

[IPS: sessions admitted before the first refusal, the 503 `memory_pressure` body, evictions, no kernel kill
(`oom_kill` stays 0), recovery time after releasing half the sessions. PIDE B: sessions before the first `oom_kill`,
which process the kernel killed, what the other sessions saw, whether the MCP processes survived.]

## E4 — isolation

[One row per tool: victim pid, how many of the other sessions answered within 30 s, processes alive.]

## Threats to validity

- Topology B is not PIDE MCP's intended deployment; it measures the cost of one process per agent, not a defect.
- PSS attribution of shared heap pages changes with k by construction; host used is reported alongside.
- One fixed theory per heap bounds "memory after work" from below; P5 is a burst, not a steady load.
- Every `poly` runs under IPS's heap cap (the same for both tools); uncapped runs are out of scope.
