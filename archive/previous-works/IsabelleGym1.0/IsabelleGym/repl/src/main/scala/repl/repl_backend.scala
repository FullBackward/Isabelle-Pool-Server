package repl

import isabelle._
import scala.jdk.CollectionConverters._

import scala.collection.mutable

class ReplBackend(show_states: Boolean) {
  private val session_manager = new Session_Manager(show_states)
  private var repl_session = new Repl_Session(session_manager)

  def current_thy_name_string: String = repl_session.current_thy_name_string

  def build_result[A](command_logic: => A): Repl_Result = {
    Repl_Output.reset()
    command_logic
    Repl_Output.result
  }

  def enter_thy(input_thy_name: String): Repl_Result = build_result {
    Thy_Parsing.extract_thy_name(input_thy_name) match {
      case None           => Repl_Output.add_error(s"Invalid theory name: $input_thy_name")
      case Some(thy_name) => repl_session.enter_thy(thy_name)
    }
  }

  private def send_ml_command(ml_text: String): Unit = repl_session.send_edit(
    s"ML_val ${Symbol.open} $ml_text ${Symbol.close}"
  )

  def open_subgoals(): java.util.List[String] = {
    val subgoals =
      if (!repl_session.current_thy_begun) List()
      else {
        val message = Repl_ML_Communication.waiting_for_subgoals_message {
          send_ml_command("Repl.send_open_subgoals @{Isar.state}")
        }
        repl_session.rollback_last_text_edit()
        message
      }
    subgoals.asJava
  }

  def local_facts(): java.util.List[String] = {
    val local_facts =
      if (!repl_session.current_thy_begun) List()
      else {
        val message = Repl_ML_Communication.waiting_for_local_facts_message {
          send_ml_command("Repl.send_local_facts @{Isar.state}")
        }
        repl_session.rollback_last_text_edit()
        message
      }
    local_facts.asJava
  }

  def global_facts(limit: Int): java.util.List[String] = {
    require(limit > 0, "limit must be positive")
    val global_facts =
      if (!repl_session.current_thy_begun) List()
      else {
        val message = Repl_ML_Communication.waiting_for_global_facts_message {
          send_ml_command(s"Repl.send_global_facts @{Isar.state} ${limit}")
        }
        repl_session.rollback_last_text_edit()
        message
      }
    global_facts.asJava
  }

  def get_proof_state(): Repl_Result = build_result {
    if (!repl_session.current_thy_begun)
      Repl_Output.add_error(
        "Cannot retrieve proof state without beginning theory."
      )
    else {
      send_ml_command("Repl.get_proof_state @{Isar.state}")
      repl_session.output_current_node_results()
      repl_session.rollback_last_text_edit()
    }
  }

  def get_source(): Repl_Result = build_result {
    Repl_Output.add_output(repl_session.current_source)
  }

  def rollback(): Repl_Result = build_result {
    repl_session.rollback_last_text_edit()
    repl_session.output_current_node_results()
  }

  def step(isar_string: String): Repl_Result = build_result {
    repl_session.send_edit(isar_string)
    repl_session.output_current_node_results()
  }

  def vector_step(isar_strings: java.util.List[String]): Repl_Result = {
    repl_session.send_edit(isar_string)
    repl_session.output_current_node_results()
  }

  def reset(): Repl_Result = build_result {
    repl_session.stop()
    repl_session = new Repl_Session(session_manager)
  }

  def exit(): Unit =
    session_manager.shutdown()

  def save_state(): EnvStateID = repl_session.save_state()

  def restore_state(state_id: EnvStateID): Boolean = repl_session.restore_state(state_id)
}
