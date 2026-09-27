package repl

import isabelle._

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
        Document.Node.Header(imports, thy_header.keywords, thy_header.abbrevs)
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
          true_deps_edit :: emulated_imports_edits
        }
    }
}
