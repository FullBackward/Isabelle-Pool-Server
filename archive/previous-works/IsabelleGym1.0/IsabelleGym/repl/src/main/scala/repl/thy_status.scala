package repl

import isabelle._

import io.bullet.spliff.Diff

import scala.collection.mutable

case class Thy_Status(
    last_text_edit: Option[Text.Edit] = None,
    parent: Option[Thy_Status] = None,
    accumulated_thy_header_tokens: List[Token] = List(),
    input_thy_name_verified: Boolean = false,
    header_processed: Boolean = false,
    insertion_point: Int = 0,
    insertion_line: Int = 1
) {
  def add_thy_header_tokens(tokens_to_add: List[Token]): Thy_Status =
    copy(accumulated_thy_header_tokens = accumulated_thy_header_tokens ::: tokens_to_add)

  def input_thy_name: Option[String] =
    accumulated_thy_header_tokens match {
      case _ :: _ :: thy_tok :: _ => Some(thy_tok.content)
      case _                      => None
    }

  private def nearest_common_ancestor(other_status: Thy_Status): Option[Thy_Status] = {
    val visited_left = mutable.Set[Thy_Status]()
    val visited_right = mutable.Set[Thy_Status]()
    var current_left: Option[Thy_Status] = Some(this)
    var current_right: Option[Thy_Status] = Some(other_status)
    var nca: Option[Thy_Status] = None

    def get_parent_if_defined(current_opt: Option[Thy_Status]): Option[Thy_Status] =
      current_opt.flatMap(_.parent)

    def visit_one_branch_checking_other(
        current_opt: Option[Thy_Status],
        visited_this_branch: mutable.Set[Thy_Status],
        visited_other_branch: mutable.Set[Thy_Status]
    ): Unit =
      current_opt.foreach { current_status =>
        if (visited_other_branch.contains(current_status)) {
          nca = Some(current_status)
        }
        visited_this_branch += current_status
      }

    while (nca.isEmpty && (current_left.isDefined || current_right.isDefined)) {
      visit_one_branch_checking_other(current_left, visited_left, visited_right)
      current_left = get_parent_if_defined(current_left)
      visit_one_branch_checking_other(current_right, visited_right, visited_left)
      current_right = get_parent_if_defined(current_right)
    }
    nca
  }

  private def insertions_from_ancestor(
      descendant: Thy_Status,
      ancestor: Option[Thy_Status]
  ): String = {
    var edits = List.empty[Text.Edit]
    var current_state_opt: Option[Thy_Status] = Some(descendant)

    while (current_state_opt.isDefined && current_state_opt != ancestor) {
      val current_state = current_state_opt.get
      current_state.last_text_edit.foreach { edit =>
        require(edit.is_insert, "non-insert edit found in history.")
        edits = edit :: edits
      }
      current_state_opt = current_state.parent
    }
    edits.map(text_edit => text_edit.text).mkString("")
  }

  def difference_edits(base_status: Thy_Status): List[Text.Edit] =
    if (this == base_status) List()
    else {
      val nca = nearest_common_ancestor(base_status)
      val insertions_base = insertions_from_ancestor(base_status, nca)
      val insertions_target = insertions_from_ancestor(this, nca)
      val diff = Diff(insertions_base, insertions_target)
      var base_offset = nca.map(_.insertion_point).getOrElse(0)
      diff.delInsOpsSorted.map {
        case Diff.Op.Insert(baseIx, targetIx, count) =>
          val edit = Text.Edit.insert(
            base_offset + baseIx,
            insertions_target.substring(targetIx, targetIx + count)
          )
          base_offset += count
          edit
        case Diff.Op.Delete(baseIx, count) =>
          val edit = Text.Edit.remove(
            base_offset + baseIx,
            insertions_base.substring(baseIx, baseIx + count)
          )
          base_offset -= count
          edit
      }.toList
    }
}
