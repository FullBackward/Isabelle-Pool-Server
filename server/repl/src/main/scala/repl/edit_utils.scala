package repl

import isabelle._

import io.bullet.spliff.Diff

/** Construction of PIDE document edits: text insert/remove edits, node
 *  perspective edits, and theory-header processing (parsing the accumulated
 *  header, importing its dependencies, emulating the REPL helper import).
 *  Shared infrastructure used by [[Repl_Session]] for every edit path in both
 *  workflows. */
type Edit = Document.Node.Edit[Text.Edit, Text.Perspective]

object Edit_Utils {
  def insert_text_edit(
      string: String,
      thy_info: Thy_Info
  ): Text.Edit = {
    val formatted_string = string + "\n"
    Text.Edit.insert(thy_info.insertion_point, formatted_string)
  }

  def remove_insert_edit(insert_edit: Text.Edit): Text.Edit =
    Text.Edit.remove(insert_edit.start, insert_edit.text)

  def edit_from_text_edit(text_edit: Text.Edit): Edit =
    edit_from_text_edits(List(text_edit))

  def edit_from_text_edits(text_edits: List[Text.Edit]): Edit =
    Document.Node.Edits[Text.Edit, Text.Perspective](
      text_edits
    )

  /** Minimal insert/remove edit sequence transforming `old_text` into
   *  `new_text`, computed with the bundled spliff diff — the SAME mechanism as
   *  Thy_Status.difference_edits (offsets are adjusted cumulatively, so the
   *  edits apply in list order against the evolving text). Also returns the
   *  base offset of the FIRST change (None when the texts are identical); ops
   *  are emitted in ascending offset order, so this is the minimum offset any
   *  edit touches. (The trailing `.nn` mirrors Thy_Status.difference_edits:
   *  the RC0 track's compiler flags type String.substring results as
   *  nullable.) Backs Repl_Session.replace_document (incremental PIDE
   *  document sync). */
  def text_diff_edits(
      old_text: String,
      new_text: String
  ): (List[Text.Edit], Option[Text.Offset]) =
    if (old_text == new_text) (List(), None)
    else {
      val diff = Diff(old_text, new_text)
      var base_offset = 0
      var first_change: Option[Text.Offset] = None
      val edits = diff.delInsOpsSorted.map {
        case Diff.Op.Insert(baseIx, targetIx, count) =>
          val offset = base_offset + baseIx
          if (first_change.isEmpty) first_change = Some(offset)
          base_offset += count
          Text.Edit.insert(offset, new_text.substring(targetIx, targetIx + count).nn)
        case Diff.Op.Delete(baseIx, count) =>
          val offset = base_offset + baseIx
          if (first_change.isEmpty) first_change = Some(offset)
          base_offset -= count
          Text.Edit.remove(offset, old_text.substring(baseIx, baseIx + count).nn)
      }.toList
      (edits, first_change)
    }

  def set_required_edit(required: Boolean): Edit =
    Document.Node.Perspective[Text.Edit, Text.Perspective](
      required,
      Text.Perspective.empty,
      Document.Node.Overlays.empty
    )

  private def dependencies_edit(
      session: Headless.Session,
      node_name: Document.Node.Name,
      thy_header: Thy_Header
  ): Option[Edit] = {
    val imports = thy_header.imports.map { case (s, pos) =>
      val name = session.resources.import_name(node_name, s)
      (name, pos)
    }
    val illegal_imports =
      imports.filter { case (name, _) =>
        Sessions.illegal_theory(name.theory_base_name)
      }
    illegal_imports.foreach { case (name, pos) =>
      Repl_Output.add_error(
        "Illegal theory name " + quote(name.theory_base_name) + Position
          .here(pos)
      )
    }
    if (illegal_imports.nonEmpty) None
    else {
      val node_header =
        // Isabelle 2026: Header carries an `options` field between imports and
        // keywords — pass named args so the fields land correctly.
        Document.Node.Header(
          imports = imports,
          keywords = thy_header.keywords,
          abbrevs = thy_header.abbrevs,
        )
      Some(Document.Node.Deps(node_header))
    }
  }

  private def import_all_theories(
      session: Headless.Session,
      all_import_names: List[String]
  ): Boolean =
    all_import_names.forall { local_import_name =>
      val error_msg = s"Failed to import theory: $local_import_name"
      val import_successful =
        try {
          val result = session.use_theories(List(local_import_name))
          if (!result.ok) Repl_Output.add_error(error_msg)
          result.ok
        } catch {
          case e: Exception =>
            Repl_Output.add_error(s"$error_msg\n${e.getMessage}")
            false
        }
      import_successful
    }

  def get_thy_header_edits(
      thy_info: Thy_Info,
      session: Headless.Session,
      node_name: Document.Node.Name,
      additional_imports_to_emulate: List[String] = Nil
  ): Option[List[Edit]] =
    Thy_Parsing.extract_thy_header_from_tokens(thy_info.accumulated_thy_header_tokens) match {
      case None =>
        Repl_Output.add_error("Invalid theory header."); None
      case Some(thy_header) =>
        val all_imports_successful = import_all_theories(
          session,
          thy_header.imports.map(_._1)
        )
        if (!all_imports_successful) return None

        val first_import = thy_header.imports.headOption.getOrElse(
          error("Cannot have theory header with no imports.")
        )
        def emulate_thy_import(thy: String): Edit = {
          val emulated_import = (thy, first_import._2)
          val emulated_import_header =
            thy_header.copy(imports = List(emulated_import))
          dependencies_edit(
            session,
            node_name,
            emulated_import_header
          ).getOrElse(error(s"Could not emulate header for import of theory $thy."))
        }
        val emulated_imports_edits = additional_imports_to_emulate.map(emulate_thy_import)

        dependencies_edit(session, node_name, thy_header).map { true_deps_edit =>
          thy_info.set_header_processed(true)
          // LOAD-BEARING (heap-pool model, Stage 3): the emulated wrapper-import
          // Deps edit is applied AFTER the true-deps edit and REPLACES the header
          // (the PIDE keeps the last Deps edit). Consequence: the document's own
          // import list only gates LOADING (import_all_theories above); the visible
          // parent context comes from the wrapper alone. Therefore the wrapper must
          // state every import whose facts/ML environment the document needs.
          // Do NOT merge the two Deps edits — the design relies on the overwrite
          // (claude-work/2026-8-12(2)-research-heap-pool/FINDINGS.md, spike 3).
          true_deps_edit :: emulated_imports_edits
        }
    }
}
