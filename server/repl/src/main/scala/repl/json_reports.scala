package repl

import isabelle._

/** Centralized builders for the CANNED JSON responses the backend returns over
 *  Py4J as raw strings. The Python server parses these (session.py / router.py),
 *  so the field names, order, and shapes here are a WIRE CONTRACT — keep them
 *  byte-identical when editing. Reports built field-by-field from live document
 *  state (e.g. node_status_report's per-command objects) stay inline at their
 *  call sites; only fixed canned responses live here. */
object Json_Reports {

  /** The failed chunk report returned when no chunk could be attempted at all
   *  (theory not begun / no theory entered). Shape shared by verify_chunk and
   *  step_chunk_report. */
  def failed_chunk_report(error: String): String =
    JSON.Format(JSON.Object(
      "timed_out" -> false,
      "success" -> false,
      "proof_open" -> false,
      "pending_qed" -> false,
      "used_sorry" -> false,
      "elapsed_ms" -> 0,
      "commands" -> List.empty[JSON.T],
      "error" -> error))

  /** The empty chunk-status fields used when no theory has been entered
   *  (Repl_Session.chunk_status_report's None branch; `proof_open`/`pending_qed`
   *  are added later by the backend callers). */
  def empty_chunk_fields(): JSON.Object.T =
    JSON.Object(
      "timed_out" -> false,
      "success" -> false,
      "used_sorry" -> false,
      "elapsed_ms" -> 0,
      "commands" -> List.empty[JSON.T])

  /** The {found:false, error} reply of the read-only line queries
   *  (command_at_line / goals_at_line): no theory entered, or line out of range. */
  def line_query_not_found(error: String): String =
    JSON.Format(JSON.Object("found" -> false, "error" -> error))

  /** The FALLBACK reply of sync_document (incremental file sync): no
   *  incremental edit was attempted and the caller MUST use the reset path.
   *  Keeps the chunk-report field set (as a failing report) so the server's
   *  report parser sees a well-formed shape; the `"fallback"` marker is what
   *  the server keys on. Reasons: "no_begun_theory" | "header_changed". */
  def sync_fallback_report(reason: String): String =
    JSON.Format(JSON.Object(
      "fallback" -> reason,
      "timed_out" -> false,
      "success" -> false,
      "proof_open" -> false,
      "pending_qed" -> false,
      "used_sorry" -> false,
      "elapsed_ms" -> 0,
      "commands" -> List.empty[JSON.T]))
}
