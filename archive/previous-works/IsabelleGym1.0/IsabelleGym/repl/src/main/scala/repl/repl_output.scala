package repl

import isabelle.Output

import scala.collection.mutable

class Repl_Result() {

  sealed trait Message { def message: String }
  private case class OutputMessage(message: String) extends Message
  private case class ErrorMessage(message: String) extends Message

  private val message_buffer = new mutable.ListBuffer[Message]()

  def add_output(message: String): Unit =
    if (message.nonEmpty) message_buffer.append(OutputMessage(message))

  def add_error(message: String): Unit =
    if (message.nonEmpty) message_buffer.append(ErrorMessage(message))

  def clear(): Unit =
    message_buffer.clear()

  case class Outputs(output: String, error: String)

  def separated_output(): Outputs = {
    def strip_trailing_new_line(s: String) = s.stripSuffix("\n")
    val output = new mutable.StringBuilder()
    val error = new mutable.StringBuilder()
    message_buffer.foreach {
      case ErrorMessage(msg)  => error.append(msg).append("\n")
      case OutputMessage(msg) => output.append(msg).append("\n")
    }
    Outputs(strip_trailing_new_line(output.toString), strip_trailing_new_line(error.toString))
  }

  def total_output(): String = {
    val output_lines = message_buffer.map {
      case OutputMessage(msg) => Output.writeln_text(msg)
      case ErrorMessage(msg)  => Output.error_message_text(msg)
    }
    output_lines.mkString("\n")
  }
}

object Repl_Output {
  private var current_result: Repl_Result = _

  def result: Repl_Result = current_result

  def reset(): Unit =
    current_result = new Repl_Result()

  def add_output(message: String): Unit = {
    if (current_result == null)
      throw new IllegalStateException("Result not initialised. Call build_result first.")
    current_result.add_output(message)
  }

  def add_error(message: String): Unit = {
    if (current_result == null)
      throw new IllegalStateException("Result not initialised. Call build_result first.")
    current_result.add_error(message)
  }
}
