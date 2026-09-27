package repl

import isabelle._

/** Py4J entrypoint facade: one `ReplBackend` instance per HTTP-server session, all
 *  sharing ONE gateway JVM (see repl_backend_gateway.scala). Python addresses this
 *  exact class via Py4J and the `ReplBackend` Protocol in
 *  repl/src/python/repl_backend_gateway.py — the public method set and signatures
 *  ARE the wire contract and must stay in sync with that Protocol.
 *
 *  The class itself only holds the constructor state and the shared probe plumbing
 *  (`build_result`, `send_ml_command`, `with_probe_settle`). The public surface is
 *  split into one trait per consuming workflow, each in its own file:
 *    - Backend_Lifecycle (backend_lifecycle.scala) — session lifecycle and cache.
 *    - Backend_Probes    (backend_probes.scala)    — transient read-only probes (BOTH MCPs).
 *    - Backend_Chunk_Ops (backend_chunk_ops.scala) — chunk-centric execution surface.
 *    - Backend_File_Ops  (backend_file_ops.scala)  — LSP-like file-sync surface. */
class ReplBackend(show_states: Boolean, enable_cache: Boolean = false, max_cache_size: Int = 10, protected val initial_thys: List[String] = List("$ISABELLE_REPL_HOME/thys/IsabelleREPL"), session_manager: Option[Session_Manager] = None, protected val field: String = "HOL", protected val session_dirs: List[String] = Nil)
    extends Backend_Lifecycle
    with Backend_Probes
    with Backend_Chunk_Ops
    with Backend_File_Ops {
  // protected (not private) so the mixed-in traits can reach them via the
  // self-type; still off the Py4J-callable public surface.
  protected val session_manager_instance = session_manager.getOrElse(new Session_Manager(show_states, enable_cache, max_cache_size))
  protected var repl_session = new Repl_Session(session_manager_instance, initial_thys, field, session_dirs)

  /** Unique channel ID for this backend instance, used to isolate ML
   *  communication (subgoals, local facts, global facts) from other
   *  concurrent backends sharing the same JVM process. */
  val channel_id: String = java.util.UUID.randomUUID().toString.nn.replace("-", "").nn.take(16)

  /** Reset the per-thread result buffer, run `command_logic`, and return the
   *  accumulated Repl_Result. The standard wrapper for output-producing methods. */
  def build_result[A](command_logic: => A): Repl_Result = {
    Repl_Output.reset()
    command_logic
    Repl_Output.result
  }

  /** The `ML_val ‹…›` command text for an ML probe — shared by send_ml_command
   *  (append path) and the post-`end` insert path in Backend_Probes. */
  protected def ml_command_text(ml_text: String): String =
    s"ML_val ${Symbol.open} $ml_text ${Symbol.close}"

  protected def send_ml_command(ml_text: String): Unit = repl_session.send_edit(
    ml_command_text(ml_text)
  )

  /** Run a transient ML probe TRANSACTIONALLY: wait for the probe's channel
   *  reply, then for the probe command's evaluation to FINISH before the
   *  caller discards it — the reply arrives DURING evaluation, so discarding
   *  immediately can cancel the probe's remainder and race the NEXT probe into
   *  a stale or canceled document state (empty results / blind channel
   *  timeout, e.g. sledgehammer calls right after other probes). One retry:
   *  a canceled probe otherwise surfaces only as a queue timeout. */
  protected def with_probe_settle[T](probe: => T): T = {
    def attempt(): T = {
      val r = probe
      repl_session.await_current_node_settled()
      r
    }
    try attempt()
    catch { case _: Exception => attempt() }
  }
}
