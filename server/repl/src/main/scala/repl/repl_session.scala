package repl

import scala.language.unsafeNulls

import isabelle._

/** Per-session document state and execution engine beneath [[ReplBackend]]:
 *  text edits into the PIDE document, output extraction, the rollback chain
 *  (`rollback_last_text_edit` / `discard_last_edit`), checkpoints
 *  (`save_state` / `restore_state`), wall-bounded chunk status reports, and
 *  vectorised environments. Shared infrastructure consumed by BOTH workflows
 *  (chunk-centric and LSP-like file-sync) — all access is serialized through
 *  the session's single worker thread on the Python side. */
type EnvStateID = Long

class Repl_Session(session_manager: Session_Manager, initial_thys: List[String] = List("$ISABELLE_REPL_HOME/thys/IsabelleREPL"), field: String = "HOL", session_dirs: List[String] = Nil) {
  //private val helper_thy = "$ISABELLE_REPL_HOME/thys/IsabelleREPL"
  private val helper_thy = initial_thys.headOption.getOrElse("$ISABELLE_REPL_HOME/thys/IsabelleREPL")

  private var session_thys: Map[String, Thy_Info] = Map.empty

  private var current_field: String = field

  private var session_data: Session_Data =
    session_manager.get_new_session(initial_thys, field, session_dirs)
  private var current_thy_info: Option[Thy_Info] = None

  private var initial_theories: List[String] = initial_thys

  private var current_state_id_counter: Long = 0

  def vector_current_duplicates: Option[List[Thy_Info]] = vector_env.flatMap(_.current_duplicates)
  private var vector_env: Option[Vector_Env] = None

  private class StateIDManager {
    private var count: EnvStateID = 0L

    def generate(): EnvStateID = {
      require(count < Long.MaxValue, "state id counter overflow")
      val result = count
      count += 1
      result
    }

    def valid(state_id: EnvStateID): Boolean = state_id >= 0 && state_id <= count
  }

  private val state_id_manager = new StateIDManager

  private def session: Headless.Session = session_data.session

  def entered_some_thy: Boolean = current_thy_info.isDefined

  def current_thy_begun: Boolean = current_thy_info match {
    case Some(thy_info) if thy_info.header_processed => true
    case _                                           => false
  }

  def current_thy_name_string: String = current_thy_info.map(_.name).getOrElse("")

  private def current_thy_node_name = {
    val thy_info = current_thy_info.getOrElse(
      error("Cannot retrieve current node name if there is no current theory.")
    )
    Document_Utils.thy_node_name(thy_info.name)
  }

  private def update_session_with_edits(
      edits: List[Edit],
      node_name: Option[Document.Node.Name] = None
  ): Unit =
    session.update(
      Document.Blobs.empty,
      edits.map(edit => (node_name.getOrElse(current_thy_node_name), edit))
    )

  def enter_thy(thy_name: String): Unit = {
    if (current_thy_info.isDefined) set_current_required(false)
    val thy_info = session_thys.getOrElse(
      thy_name, {
        val thy_info = new Thy_Info(thy_name)
        session_thys = session_thys + (thy_name -> thy_info)
        thy_info
      }
    )
    current_thy_info = Some(thy_info)
    set_current_required(true)
  }

  private def set_current_required(required: Boolean): Unit = {
    val required_edit = Edit_Utils.set_required_edit(required)
    update_session_with_edits(List(required_edit))
  }

  def output_current_node_results(): Unit =
    current_thy_info.foreach(thy_info =>
      Document_Utils.output_node_results(
        session,
        current_thy_node_name,
        thy_info.last_insertion_line
      )
    )

  /** Sequencing barrier for transient ML probes: wait until every command in the
   *  current node (including the just-evaluated probe) is consolidated. Call
   *  after a probe's channel reply arrives and BEFORE discard_last_edit, so the
   *  discard cannot cancel the probe's remaining evaluation and race the next
   *  probe (see Document_Utils.await_all_processed). */
  def await_current_node_settled(): Unit =
    current_thy_info.foreach(_ =>
      Document_Utils.await_all_processed(session, current_thy_node_name))

  /** Wall-bounded per-command status report for the just-inserted chunk: JSON + success. */
  def chunk_status_report(wall_budget_ms: Long): Chunk_Report =
    current_thy_info match {
      case Some(thy_info) =>
        Document_Utils.node_status_report(
          session,
          current_thy_node_name,
          thy_info.last_insertion_line,
          wall_budget_ms
        )
      case None =>
        Chunk_Report(false, Json_Reports.empty_chunk_fields())
    }

  /** Read-only jEdit-style line queries on a fresh stable snapshot (no edits, no ML
   *  probes). Consumed by Backend_File_Ops for the LSP-like file-sync mode. */
  def command_at_line(line: Int): String =
    current_thy_info match {
      case Some(_) => Document_Utils.command_at_line_json(session, current_thy_node_name, line)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  def goals_at_line(line: Int): String =
    current_thy_info match {
      case Some(_) => Document_Utils.goals_at_line_json(session, current_thy_node_name, line)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  /** Snapshot/Rendering-based position queries (hover / go-to-definition) and the
   *  overlay-based position-explicit sledgehammer. Read-only; consumed by
   *  Backend_File_Ops for the LSP-like file-sync mode. */
  def hover_at(line: Int, col: Int): String =
    current_thy_info match {
      case Some(_) => Document_Utils.hover_at_json(session, current_thy_node_name, line, col)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  def definition_at(line: Int, col: Int): String =
    current_thy_info match {
      case Some(_) => Document_Utils.definition_at_json(session, current_thy_node_name, line, col)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  def sledgehammer_at(line: Int, subgoal: Int, timeout_s: Int): String =
    current_thy_info match {
      case Some(_) =>
        Document_Utils.sledgehammer_at_json(session, current_thy_node_name, line, subgoal, timeout_s)
      case None    => Json_Reports.line_query_not_found("no theory entered")
    }

  /** True when the current document ends with theory `end` (last non-ignored command
   *  has span `end`). A successful `end` implies NO proof is open, and probe commands
   *  appended past it never execute (parsed without a theory context — they fail with
   *  "missing theory context" and the ML channel never replies). Backend_Probes uses
   *  this to short-circuit trivial post-`end` answers and reroute the rest through
   *  with_probe_before_end. */
  def current_thy_ended: Boolean =
    current_thy_info.exists(_ => Document_Utils.node_ends_with_end(session, current_thy_node_name))

  /** Insert `isar_string` immediately BEFORE the trailing theory `end` command,
   *  bypassing Thy_Info's append-only insertion_point bookkeeping (the document tip
   *  logically stays at `end`). A trailing newline is appended so the probe span does
   *  not glue to the `end` command. Returns the insertion offset and the exact
   *  inserted text; None when the node has no `end` command. */
  private def insert_before_end(isar_string: String): Option[(Text.Offset, String)] =
    if (!entered_some_thy) None
    else
      Document_Utils.last_end_offset(session, current_thy_node_name).map { end_offset =>
        val text = isar_string + "\n"
        update_session_with_edits(
          List(Edit_Utils.edit_from_text_edit(Text.Edit.insert(end_offset, text)))
        )
        (end_offset, text)
      }

  /** Symmetric remove for insert_before_end. */
  private def remove_before_end(offset: Text.Offset, text: String): Unit =
    if (entered_some_thy)
      update_session_with_edits(
        List(Edit_Utils.edit_from_text_edit(Text.Edit.remove(offset, text)))
      )

  /** Bracket for transient probes on a FINISHED theory: `body` receives an inserter
   *  that places the probe command immediately before the trailing `end` and returns
   *  its offset. The probe's evaluation is awaited BEFORE removal (same settle
   *  rationale as ReplBackend.with_probe_settle), and the insert is ALWAYS removed
   *  (try/finally, including on channel timeout), leaving the document byte-identical.
   *  Thy_Info bookkeeping is bypassed, so discard_last_edit must NOT be called for
   *  these probes. */
  def with_probe_before_end[T](body: (String => Option[Text.Offset]) => T): T = {
    var inserted: Option[(Text.Offset, String)] = None
    try {
      val result = body { text =>
        inserted = insert_before_end(text)
        inserted.map(_._1)
      }
      await_current_node_settled()
      result
    } finally {
      inserted.foreach { case (offset, text) => remove_before_end(offset, text) }
    }
  }

  /** Output the results of the single command starting at `offset` — for mid-document
   *  probes (with_probe_before_end), where output_current_node_results'
   *  last-insertion-line filter would hide them. */
  def output_command_at_offset(offset: Text.Offset): Unit =
    current_thy_info.foreach(_ =>
      Document_Utils.output_command_at_offset(session, current_thy_node_name, offset))

  /** INCREMENTAL whole-document replacement (jEdit-style file sync, Phase B1):
   *  diff `new_text` against the node's CURRENT source (spliff, the same
   *  mechanism as Thy_Status.difference_edits / restore_state) and submit the
   *  difference as ONE PIDE edit, so PIDE re-processes only from the first
   *  changed command onward — instead of reset + re-elaboration from line 1.
   *
   *  Returns Left(reason) when no incremental edit may be attempted — the
   *  caller MUST fall back to the reset path:
   *    - "no_begun_theory": no theory entered, or the header was never
   *      processed (there is no stable document base to diff against);
   *    - "header_changed": the FIRST diff hunk touches the theory header
   *      (the `theory … begin` command span of the OLD text) — includes a
   *      theory-name change. Header edits are not applied incrementally.
   *
   *  Otherwise the node text becomes exactly `new_text + "\n"` (mirroring
   *  send_edit's trailing newline, so the fast path and the reset path
   *  converge to the same node text), the Thy_Info bookkeeping is re-based to
   *  a fresh append-only base (Thy_Info.reset_to_fresh_base — the old
   *  rollback/checkpoint chain is CUT: pre-replace checkpoints are unsound),
   *  and Right(report) carries a wall-bounded status report covering only the
   *  re-processed tail (commands starting at/after first_changed_line - 1)
   *  with ABSOLUTE node lines. On budget expiry the replace edit is NOT
   *  discarded (a replace is an interleaved insert/remove sequence with no
   *  single insert to discard) — timed_out=true and the partial state stays
   *  for inspection (LSP-style, intentionally unlike verify_chunk). */
  def replace_document(new_text: String, wall_budget_ms: Long): Either[String, Chunk_Report] =
    current_thy_info match {
      case Some(thy_info) if thy_info.header_processed =>
        val node_name = current_thy_node_name
        val old_text = Document_Utils.node_source(session, node_name)
        Document_Utils.header_end_offset(session, node_name) match {
          case None => Left("no_begun_theory")
          case Some(header_end) =>
            val target_text = new_text + "\n"
            val (text_edits, first_change) =
              Edit_Utils.text_diff_edits(old_text, target_text)
            if (first_change.exists(_ < header_end)) Left("header_changed")
            else {
              if (text_edits.nonEmpty)
                update_session_with_edits(List(Edit_Utils.edit_from_text_edits(text_edits)))
              thy_info.reset_to_fresh_base(target_text)
              // Lines at/above the first change are byte-identical in old and
              // new text, so the OLD-text line of the first change is also its
              // line in the new node. since_line = first_changed_line - 1 also
              // catches a multi-line command whose span starts one line above
              // the change; an empty diff reports the whole (unchanged) node.
              val first_changed_line = first_change match {
                case Some(offset) => old_text.take(offset).count(_ == '\n') + 1
                case None         => 1
              }
              val since_line = if (first_change.isDefined) first_changed_line - 1 else 1
              Right(Document_Utils.node_status_report(
                session, node_name, since_line, wall_budget_ms, absolute_lines = true))
            }
        }
      case _ => Left("no_begun_theory")
    }

  def send_edit(isar_string: String, node: Option[Document.Node.Name] = None): Unit = {
    val edits = current_thy_info match {
      case None =>
        Repl_Output.add_error("Cannot make edits without entering theory.")
        List()
      case Some(thy_info) =>
        val text_edit = Edit_Utils.insert_text_edit(isar_string, thy_info)
        val edit_ok = thy_info.update_given_text_edit(text_edit)
        if (!edit_ok) List()
        else {
          val additional_edits: List[Edit] =
            if (thy_info.need_header_processing)
              Edit_Utils
                .get_thy_header_edits(
                  thy_info,
                  session,
                  current_thy_node_name,
                  List(helper_thy)
                )
                .getOrElse(List())
            else List()
          Edit_Utils.edit_from_text_edit(text_edit) :: additional_edits
        }
    }
    if (edits.nonEmpty) update_session_with_edits(edits, node)
  }

  def send_vector_edit(isar_strings: List[String]): Unit =
    vector_current_duplicates.foreach { thy_infos =>
      thy_infos.zip(isar_strings).foreach { case (thy_info, isar_string) =>
        send_edit(isar_string, node = Some(Document_Utils.thy_node_name(thy_info.name)))
      }
    }

  def current_source: String =
    if (entered_some_thy) Document_Utils.node_source(session, current_thy_node_name).nn.strip()
    else ""

  def rollback_last_text_edit(): Unit =
    current_thy_info match {
      case None =>
        Repl_Output.add_error("Cannot rollback without entering theory.")
      case Some(thy_info) =>
        thy_info.last_text_edit match {
          case None => Repl_Output.add_error("No text edits have been made to rollback.")
          case Some(last_edit) =>
            val remove_edit = Edit_Utils.remove_insert_edit(last_edit)
            update_session_with_edits(List(Edit_Utils.edit_from_text_edit(remove_edit)))
            thy_info.rollback_last_text_edit_if_exists()
        }
    }

  /** Silently remove the most recent text edit, if any. Used to make proof-state ML
   *  probes (open_subgoals / facts / sledgehammer / get_proof_state) TRANSIENT: the
   *  `ML_val ‹…›` probe is inserted and evaluated, its result is read, then this drops
   *  it — so probing leaves no trace in the document or the rollback chain. Without this,
   *  every command left a trailing probe edit, so user `rollback` peeled the probe first
   *  and needed two calls to undo one line. Unlike `rollback_last_text_edit`, this never
   *  writes to Repl_Output (safe to call outside `build_result`). */
  def discard_last_edit(): Unit =
    current_thy_info.foreach { thy_info =>
      thy_info.last_text_edit.foreach { last_edit =>
        update_session_with_edits(
          List(Edit_Utils.edit_from_text_edit(Edit_Utils.remove_insert_edit(last_edit)))
        )
        thy_info.rollback_last_text_edit_if_exists()
      }
    }

  def save_state(): EnvStateID = {
    val env_state_id = state_id_manager.generate()
    session_thys.foreach { case (_, thy_info) => thy_info.save_current_state(env_state_id) }
    env_state_id
  }

  def restore_state(state_id: EnvStateID): Boolean =
    if (!state_id_manager.valid(state_id)) false
    else {
      session_thys.foreach { case (_, thy_info) =>
        val thy_node_name = Document_Utils.thy_node_name(thy_info.name)
        val text_edits_required = thy_info.restore_state(state_id)
        val edits = List(Edit_Utils.edit_from_text_edits(text_edits_required))
        update_session_with_edits(edits, node_name = Some(thy_node_name))
      }
      true
    }

  def reset_with_cache(): Unit = {
    val new_session_data = session_manager.get_new_session(initial_theories, current_field, session_dirs)
    
    session_thys = Map.empty
    current_thy_info = None
    vector_env = None
    
    session_data = new_session_data
  }
  def stop(): Unit = session_manager.remove_session_async(session_data)
  

  def stop_with_cache(): Unit = {

    session_manager.release_session_to_cache(session_data, initial_theories)
  }
  def vectorise(size: Int): Unit = {
    require(size > 0, "Vector size must be positive")
    vector_env = Some(Vector_Env(size, current_thy_info))
  }

  def scalarise(env_to_keep: Int): Unit = {
    vector_env
      .getOrElse(error("can't scalarise when already in scalar mode"))
      .scalarise(env_to_keep)
  }

}
