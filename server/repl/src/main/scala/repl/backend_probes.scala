package repl

import scala.jdk.CollectionConverters._

/** Transient read-only probes of [[ReplBackend]]: open subgoals, in-proof
 *  predicate, local/global facts, sledgehammer, rendered proof state, source
 *  text, and the generic `probe_transient` diagnostic primitive. These serve
 *  BOTH MCP servers — the chunk-centric one (state / subgoals / facts /
 *  sledgehammer / diagnostic tools) and the LSP-like read-only one.
 *
 *  Transient-probe contract: insert the probe command, read its output (or its
 *  per-channel ML reply), then `discard_last_edit` — so the proof script,
 *  rollback chain, and command history stay untouched. Channel probes are
 *  wrapped in `with_probe_settle` so the probe's evaluation finishes before
 *  the discard (see repl_backend.scala).
 *
 *  FINISHED THEORIES (document ends with theory `end`): a probe appended past
 *  `end` never executes — it is parsed without a theory context and fails with
 *  "missing theory context", so the ML channel would time out (docs/ISSUES.md
 *  work log 2026-08-08). Handling, keyed on [[Repl_Session.current_thy_ended]]:
 *  `open_subgoals`/`in_proof`/`sledgehammer` short-circuit to their definitional
 *  answers (a successful `end` means no proof is open); `local_facts`/
 *  `global_facts`/`probe_transient` are re-routed through
 *  [[Repl_Session.with_probe_before_end]], which inserts the probe BEFORE the
 *  `end` command and always removes it again (byte-identical document). */
trait Backend_Probes { this: ReplBackend =>

  /** Pretty-printed open subgoals of the current proof state ([] when not in a
   *  proof — including after a trailing theory `end`, which closes the theory
   *  with no proof open by definition). Consumed via GET .../subgoals by both MCPs. */
  def open_subgoals(): java.util.List[String] = {
    val subgoals =
      if (!repl_session.current_thy_begun) List()
      else if (repl_session.current_thy_ended) List()  // post-`end`: no proof can be open
      else {
        val message = with_probe_settle(
          Repl_ML_Communication.waiting_for_subgoals_message(
            {
              // The ML side prepends "CH:<channel_id>" so the Scala callback
              // can route the response to the correct per-backend queue.
              send_ml_command(
                s"""Repl.send_open_subgoals_tagged "${channel_id}" @{Isar.state}"""
              )
            },
            channel_id
          )
        )
        repl_session.discard_last_edit()  // probe is transient: no doc/rollback pollution
        message
      }
    subgoals.asJava
  }

  /** True while the toplevel is inside a proof block (the ML `Toplevel.is_proof`
   *  predicate: true for the whole block lifetime, including after a terminal
   *  `show` while `qed` is still pending). Definitionally false after a trailing
   *  theory `end` (a successful `end` means no proof is open). */
  def in_proof(): Boolean = {
    if (!repl_session.current_thy_begun) false
    else if (repl_session.current_thy_ended) false  // post-`end`: no proof can be open
    else {
      val message = with_probe_settle(
        Repl_ML_Communication.waiting_for_in_proof_message(
          {
            // The ML side prepends "CH:<channel_id>" so the Scala callback
            // can route the response to the correct per-backend queue.
            send_ml_command(
              s"""Repl.send_in_proof_tagged "${channel_id}" @{Isar.state}"""
            )
          },
          channel_id
        )
      )
      repl_session.discard_last_edit()  // probe is transient: no doc/rollback pollution
      message == List("1")
    }
  }

  /** Facts visible in the current proof context; consumed via
   *  GET .../facts/local by both MCPs. On a finished theory the probe is placed
   *  BEFORE the trailing `end` (an appended probe would never execute); the ML
   *  side still gates on `Toplevel.is_proof`, so an empty list there is correct. */
  def local_facts(): java.util.List[String] = {
    val local_facts =
      if (!repl_session.current_thy_begun) List()
      else if (repl_session.current_thy_ended) {
        with_probe_settle(
          repl_session.with_probe_before_end { insert =>
            Repl_ML_Communication.waiting_for_local_facts_message(
              {
                insert(
                  ml_command_text(s"""Repl.send_local_facts_tagged "${channel_id}" @{Isar.state}""")
                )
              },
              channel_id
            )
          }
        )
      }
      else {
        val message = with_probe_settle(
          Repl_ML_Communication.waiting_for_local_facts_message(
            {
              send_ml_command(
                s"""Repl.send_local_facts_tagged "${channel_id}" @{Isar.state}"""
              )
            },
            channel_id
          )
        )
        repl_session.discard_last_edit()  // probe is transient
        message
      }
    local_facts.asJava
  }

  /** Facts of the current theory's global context, up to `limit`; consumed via
   *  GET .../facts/global by both MCPs. Same post-`end` re-routing as local_facts. */
  def global_facts(limit: Int): java.util.List[String] = {
    require(limit > 0, "limit must be positive")
    val global_facts =
      if (!repl_session.current_thy_begun) List()
      else if (repl_session.current_thy_ended) {
        with_probe_settle(
          repl_session.with_probe_before_end { insert =>
            Repl_ML_Communication.waiting_for_global_facts_message(
              {
                insert(
                  ml_command_text(s"""Repl.send_global_facts_tagged "${channel_id}" @{Isar.state} ${limit}""")
                )
              },
              channel_id
            )
          }
        )
      }
      else {
        val message = with_probe_settle(
          Repl_ML_Communication.waiting_for_global_facts_message(
            {
              send_ml_command(
                s"""Repl.send_global_facts_tagged "${channel_id}" @{Isar.state} ${limit}"""
              )
            },
            channel_id
          )
        )
        repl_session.discard_last_edit()  // probe is transient
        message
      }
    global_facts.asJava
  }

  /** Run sledgehammer on the first subgoal with an Isabelle-level budget of
   *  `timeout_s` seconds; returns the collected suggestion lines. Consumed via
   *  the sledgehammer endpoint/tool by both MCPs. Meaningless after a trailing
   *  theory `end` (no open goal) — returns [] immediately rather than waiting
   *  out the channel timeout. */
  def sledgehammer(timeout_s: Int): java.util.List[String] = {
    val suggestions =
      if (!repl_session.current_thy_begun) List()
      else if (repl_session.current_thy_ended) List()  // post-`end`: no open goal
      else {
        try {
          val message = with_probe_settle(
            Repl_ML_Communication.waiting_for_sledgehammer_message(
              {
                send_ml_command(
                  s"""Repl.send_sledgehammer_tagged "${channel_id}" ${timeout_s} @{Isar.state}"""
                )
              },
              channel_id,
              timeout_s
            )
          )
          message
        } finally {
          repl_session.discard_last_edit()  // ALWAYS discard, even on timeout
        }
      }
    suggestions.asJava
  }

  /** Rendered current proof state; consumed via the state endpoint (MCP
   *  `proof_state` tool) by both MCPs. Errors immediately on a finished theory
   *  instead of timing out (the legacy ML probe cannot run past `end`). */
  def get_proof_state(): Repl_Result = build_result {
    if (!repl_session.current_thy_begun)
      Repl_Output.add_error(
        "Cannot retrieve proof state without beginning theory."
      )
    else if (repl_session.current_thy_ended)
      Repl_Output.add_error(
        "Cannot retrieve proof state after theory end (theory is closed)."
      )
    else {
      send_ml_command("Repl.get_proof_state @{Isar.state}")
      repl_session.output_current_node_results()  // read the probe's output first
      repl_session.discard_last_edit()             // then drop the transient probe
    }
  }

  /** Execute a command TRANSIENTLY: insert it, capture its writeln/state output, then
   *  discard the edit so the theory node and rollback chain are untouched. This is the
   *  low-level probe primitive; higher-level transient helpers (`sledgehammer`,
   *  `open_subgoals`, etc.) delegate to this.
   *
   *  Use this for ANY read-only query (diagnostic, ML probe, search, etc.) where you
   *  need the command's output but do NOT want it to persist in the proof script.
   *  Keyword allowlist/denylist gatekeeping is enforced upstream on the server side.
   *  Consumed via POST .../diagnostic by both MCP servers.
   *
   *  On a FINISHED theory (trailing `end`) the command is inserted BEFORE the `end`
   *  (an appended command would never execute) and removed again afterwards, leaving
   *  the document byte-identical. */
  def probe_transient(isar_string: String): Repl_Result = build_result {
    if (!repl_session.current_thy_begun)
      Repl_Output.add_error("Cannot run probe without beginning theory.")
    else if (repl_session.current_thy_ended) {
      repl_session.with_probe_before_end { insert =>
        insert(isar_string).foreach { offset =>
          repl_session.output_command_at_offset(offset)  // read the probe's output
        }
      }  // bracket removes the probe afterwards (try/finally)
    }
    else {
      repl_session.send_edit(isar_string)
      repl_session.output_current_node_results()  // read the probe's output first
      repl_session.discard_last_edit()             // then drop the transient command
    }
  }

  /** Current theory source text; consumed via GET .../source by both MCPs. */
  def get_source(): Repl_Result = build_result {
    Repl_Output.add_output(repl_session.current_source)
  }
}
