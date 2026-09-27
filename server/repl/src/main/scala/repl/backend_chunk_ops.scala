package repl

import isabelle._

import scala.jdk.CollectionConverters._

/** Execution surface of [[ReplBackend]] consumed by the chunk-centric MCP via
 *  the HTTP server's /commands (`step`), /verify_chunk, /checkpoints
 *  (`save_state` / `restore_state`), and /rollback endpoints, plus the
 *  vectorised-environment ops (`vectorise` / `scalarise` / `vector_step`,
 *  IsabelleGym 1.0 lineage).
 *
 *  Unlike Backend_File_Ops, `verify_chunk` is TRANSACTIONAL: a failed or
 *  timed-out chunk is rolled back via `discard_last_edit`, leaving no trace in
 *  the theory node (see its Scaladoc). */
trait Backend_Chunk_Ops { this: ReplBackend =>

  /** Remove the most recent text edit and report the resulting node output;
   *  consumed via POST .../rollback. */
  def rollback(): Repl_Result = build_result {
    repl_session.rollback_last_text_edit()
    repl_session.output_current_node_results()
  }

  /** Insert one command (or chunk) as a single edit and report its output;
   *  consumed via POST .../commands. */
  def step(isar_string: String): Repl_Result = build_result {
    repl_session.send_edit(isar_string)
    repl_session.output_current_node_results()
  }

  /**
   * Verify a whole proof CHUNK in one shot: insert it as a single edit (parallel proof
   * checking stays on per parallel_proofs), then return a JSON per-command status report
   * under ONE wall budget (no per-command timeouts). On budget expiry the report is partial
   * and names the still-`running` line (the loop). Requires the theory to be begun.
   * Consumed via POST .../verify_chunk — the chunk-centric MCP's only execution tool.
   *
   * TRANSACTIONAL: the chunk is kept in the theory node only if it verifies fully
   * (`Chunk_Report.success`); on any failure OR timeout it is rolled back via
   * `discard_last_edit` so the attempt leaves no trace. This makes repeated attempts
   * independent: the next try can't hit "Duplicate fact declaration" (re-declaring the same
   * lemma) or "Bad context for command ... -- using reset state" (a still-running command
   * poisoning the node), and the removal edit cancels the obsolete (e.g. looping `metis`)
   * command instead of leaving it churning. Since the theory is begun, send_edit always
   * records the chunk as the last text edit, so discard removes exactly this chunk.
   *
   * The report carries `proof_open`: even a `success` chunk (no command errors) may leave an
   * UNCLOSED proof — e.g. `theorem ... using assms` or a trailing `have ...` with no `qed`.
   * Such a chunk is kept (so the caller can `sledgehammer` the open goal), but `proof_open`
   * is true so the caller knows the theorem is NOT actually proved and must close it (or
   * rollback) before starting a new `theorem`/`lemma` — declaring one while a proof is open
   * is exactly what triggers "Bad context for command -- using reset state". `proof_open` is
   * the ML `Toplevel.is_proof` predicate (block lifetime), NOT bare subgoal counting: a
   * successful terminal `show` leaves zero subgoals but the block still awaits `qed`, and
   * batch builds reject that with "Goal present in this block". In that situation the report
   * also carries `pending_qed`=true so the caller knows the only missing step is `qed`
   * (vs. real remaining goals, which need proof work). For a rolled-back (failed) chunk
   * both are false (nothing was kept).
   */
  def verify_chunk(isar_string: String, wall_budget_ms: Long): String = {
    Repl_Output.reset()
    if (!repl_session.current_thy_begun)
      Json_Reports.failed_chunk_report("theory not begun")
    else {
      repl_session.send_edit(isar_string)
      val report = repl_session.chunk_status_report(wall_budget_ms)
      val proof_open =
        if (!report.success) { repl_session.discard_last_edit(); false }
        else in_proof()  // kept chunk: block still open (goals pending OR qed pending)
      val pending_qed =
        report.success && proof_open && open_subgoals().isEmpty  // discharged, needs only qed
      JSON.Format(report.fields + ("proof_open" -> proof_open) + ("pending_qed" -> pending_qed))
    }
  }

  /** Checkpoint: snapshot every entered theory's status and return the new state
   *  id; consumed via the checkpoints endpoints. */
  def save_state(): EnvStateID = repl_session.save_state()

  /** Restore a checkpoint by id (diff-based edits against the current status);
   *  false if the id is unknown. Consumed via the checkpoints endpoints. */
  def restore_state(state_id: EnvStateID): Boolean = repl_session.restore_state(state_id)

  /** Fork the current theory into `size` duplicate environments for vector
   *  stepping (IsabelleGym 1.0 lineage). */
  def vectorise(size: Int): Unit =
    repl_session.vectorise(size)

  /** Collapse the vectorised environments, keeping duplicate `index_to_keep`. */
  def scalarise(index_to_keep: Int): Unit =
    repl_session.scalarise(index_to_keep)

  /** Apply one command per duplicate environment created by `vectorise`. */
  def vector_step(isar_strings: java.util.List[String]): Repl_Result = build_result {
    repl_session.send_vector_edit(isar_strings.asScala.toList)
    repl_session.output_current_node_results()
  }
}
