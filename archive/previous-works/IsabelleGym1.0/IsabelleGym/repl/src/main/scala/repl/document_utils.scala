package repl

import isabelle._

object Document_Utils {
  def thy_node_name(thy_name: String): Document.Node.Name = {
    val qualifier = Sessions.DRAFT
    Document.Node.Name(s"$qualifier.$thy_name", theory = thy_name)
  }

  private def stable_node_snapshot(
      session: Headless.Session,
      node_name: Document.Node.Name,
      wait_until_all_commands_processed: Boolean = true
  ): Document.Snapshot = {
    val node_snapshot =
      session.await_stable_snapshot().switch(node_name)
    val version = node_snapshot.version
    var commands_to_process = node_snapshot.node.commands

    def all_commands_processed = {
      val state = session.get_state()
      commands_to_process = commands_to_process.filterNot { command =>
        val states = state.command_states(version, command)
        states.exists(st => st.maybe_consolidated || st.consolidated)
      }
      commands_to_process.isEmpty
    }

    while (wait_until_all_commands_processed && !all_commands_processed)
      session.output_delay.sleep()

    node_snapshot
  }

  def output_node_results(
      session: Headless.Session,
      node_name: Document.Node.Name,
      last_insertion_start_line: Int
  ): Unit = {
    def pretty_print_results(
        command: Command,
        results: Command.Results,
        hide_state_messages: Boolean
    ): Unit = {

      def output_pretty_if_non_empty(body: XML.Body, output_f: String => Unit): Unit = {
        val pretty_string = Pretty.string_of(body)
        if (pretty_string.nonEmpty) output_f(pretty_string)
      }
      results.iterator
        .foreach(_._2 match {
          case XML.Elem(Markup(markup_type, _), body) =>
            markup_type match {
              case Markup.WRITELN_MESSAGE => ()
              case Markup.STATE_MESSAGE =>
                if (!(command.is_ignored || hide_state_messages))
                  output_pretty_if_non_empty(body, Repl_Output.add_output)
              case Markup.ERROR_MESSAGE | Markup.WARNING_MESSAGE =>
                output_pretty_if_non_empty(body, Repl_Output.add_error)
              case _ => output_pretty_if_non_empty(body, Repl_Output.add_output)
            }
        })
    }

    val node_snapshot = stable_node_snapshot(session, node_name)
    val node_commands = node_snapshot.node.commands

    node_commands.foreach { command =>
      val command_results = node_snapshot.command_results(command)
      val start_line = node_snapshot.node.command_start_line(command).getOrElse(1)
      val hide_state_messages = start_line < last_insertion_start_line
      pretty_print_results(
        command,
        command_results,
        hide_state_messages = hide_state_messages
      )
    }
  }

  def node_source(session: Headless.Session, node_name: Document.Node.Name) = stable_node_snapshot(
    session,
    node_name,
    wait_until_all_commands_processed = false
  ).node.source
}
