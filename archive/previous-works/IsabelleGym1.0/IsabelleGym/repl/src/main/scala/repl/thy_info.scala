package repl

import isabelle._
import scala.collection.mutable

class Thy_Info(val name: String, status: Option[Thy_Status] = None) {
  private var current_status: Thy_Status = status.getOrElse(Thy_Status())
  private val saved_states: mutable.Map[EnvStateID, Thy_Status] = mutable.Map.empty

  def header_processed: Boolean = current_status.header_processed
  def insertion_point: Int = current_status.insertion_point
  def accumulated_thy_header_tokens: List[Token] = current_status.accumulated_thy_header_tokens

  private def previous_status: Option[Thy_Status] = current_status.parent
  def last_insertion_line: Int = previous_status.map(_.insertion_line).getOrElse(1)
  def last_text_edit: Option[Text.Edit] = current_status.last_text_edit

  private def has_accumulated_full_thy_header: Boolean =
    current_status.accumulated_thy_header_tokens.exists(_.is_begin)

  def need_header_processing: Boolean =
    !header_processed && has_accumulated_full_thy_header

  def set_header_processed(header_processed: Boolean): Unit =
    current_status = current_status.copy(header_processed = header_processed)

  def update_given_text_edit(text_edit: Text.Edit): Boolean = {
    var new_status = current_status.copy(parent = Some(current_status))
    if (!header_processed) {
      val tokens_to_add =
        Thy_Parsing.get_thy_header_tokens(
          Scan.char_reader(text_edit.text),
          drop_tokens_before_thy_command = current_status.accumulated_thy_header_tokens.isEmpty
        )
      new_status = new_status.add_thy_header_tokens(tokens_to_add)
      if (!new_status.input_thy_name_verified) {
        val header_name_does_not_match_entered_name =
          new_status.input_thy_name match {
            case Some(input_thy_name) =>
              new_status = new_status.copy(input_thy_name_verified = true)
              name != input_thy_name
            case None => false
          }
        if (header_name_does_not_match_entered_name) {
          Repl_Output.add_error(
            "Name of theory in header must match name of current theory."
          )
          return false
        }
      }
    }
    val new_insertion_point = current_status.insertion_point + text_edit.text.length
    val new_insertion_line = current_status.insertion_line + text_edit.text.count(_ == '\n')
    new_status = new_status.copy(
      last_text_edit = Some(text_edit),
      insertion_point = new_insertion_point,
      insertion_line = new_insertion_line
    )
    current_status = new_status
    true
  }

  def rollback_last_text_edit_if_exists(): Unit =
    current_status = current_status.parent.getOrElse(current_status)

  def save_current_state(state_id: EnvStateID): Unit =
    saved_states.addOne((state_id, current_status))

  def restore_state(state_id: EnvStateID): List[Text.Edit] =
    saved_states.get(state_id) match {
      case None => List()
      case Some(status_to_restore) =>
        val edits_required = status_to_restore.difference_edits(current_status)
        current_status = status_to_restore
        edits_required
    }

  def duplicate(duplicate_name: String): Thy_Info = new Thy_Info(duplicate_name, status)
}
