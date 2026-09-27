package repl

import isabelle.Output

import scala.collection.mutable

/** Result buffer for one backend call (`Repl_Result`, returned over Py4J) and
 *  its thread-local accumulator ([[Repl_Output]]). Shared infrastructure:
 *  every output-producing method on [[ReplBackend]] in both workflows funnels
 *  through `build_result` → this buffer. */
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

/**
 * Thread-safe wrapper around Repl_Result using ThreadLocal storage.
 *
 * RATIONALE (P0 fix): The previous implementation used a single shared
 * `var current_result` across the entire JVM.  When two Python sessions
 * called into the Scala gateway concurrently (via separate ThreadedBackend
 * worker threads), one session's `reset()` would wipe the result being
 * accumulated by another session, causing silent output corruption.
 *
 * By storing the current result in a ThreadLocal, each worker thread gets
 * its own isolated Repl_Result instance, which matches the 1-thread-per-
 * session model enforced by ThreadedBackend on the Python side.
 */
object Repl_Output {
  private val thread_local_result: ThreadLocal[Repl_Result] =
    new ThreadLocal[Repl_Result]()

  def result: Repl_Result = {
    val r = thread_local_result.get()
    if (r == null)
      throw new IllegalStateException("Result not initialised. Call build_result first.")
    r
  }

  def reset(): Unit =
    thread_local_result.set(new Repl_Result())

  def add_output(message: String): Unit = {
    val r = thread_local_result.get()
    if (r == null)
      throw new IllegalStateException("Result not initialised. Call build_result first.")
    r.add_output(message)
  }

  def add_error(message: String): Unit = {
    val r = thread_local_result.get()
    if (r == null)
      throw new IllegalStateException("Result not initialised. Call build_result first.")
    r.add_error(message)
  }
}
