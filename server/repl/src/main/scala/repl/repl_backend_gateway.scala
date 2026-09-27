package repl

import isabelle._
import py4j.GatewayServer
import scala.jdk.CollectionConverters._

/** JVM entrypoint of the shared gateway: `Gateway_App.main` starts the Py4J
 *  GatewayServer (launched by repl_backend_gateway.py via `isabelle scala`),
 *  and [[ReplBackendGateway]] is the factory object Python calls to obtain
 *  [[ReplBackend]] instances — one per HTTP-server session, all in this ONE
 *  JVM. Shared infrastructure serving both MCP workflows. */
object ReplBackendGateway {
  /** Functional liveness probe for the "Timer already cancelled" wedge
   *  (docs/ISSUES.md Bug 9): schedule a no-op on the JVM-global Event_Timer.
   *  A wedged timer throws IllegalStateException at schedule time, so this
   *  answers false instead of 500ing — letting the Python side distinguish
   *  a wedged-but-alive JVM from a healthy one and trigger crash recovery. */
  def alive(): Boolean =
    try { Event_Timer.request(new Console_Logger(), isabelle.Time.now()) { () }; true }
    catch { case _: IllegalStateException => false }

  def get_repl_backend(show_states: Boolean): ReplBackend = new ReplBackend(show_states)
  def get_repl_backend_with_cache(show_states: Boolean, enable_cache: Boolean): ReplBackend = 
    new ReplBackend(show_states, enable_cache)
    
  def get_repl_backend_with_full_cache_config(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, field: String = "HOL"): ReplBackend = 
    new ReplBackend(show_states, enable_cache, max_cache_size, field = field)

  def get_repl_backend_with_memory_management(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, field: String = "HOL"): ReplBackend =
    new ReplBackend(show_states, enable_cache, max_cache_size, field = field)

  def get_repl_backend_with_initial_theories(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, initial_thys: java.util.List[String], field: String = "HOL", session_dirs: java.util.List[String] = java.util.List.of[String]().nn): ReplBackend =
    new ReplBackend(show_states, enable_cache, max_cache_size, initial_thys.asScala.toList, field = field, session_dirs = session_dirs.asScala.toList)

  private var shared_session_manager: Option[Session_Manager] = None

  def get_shared_session_manager(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int): Session_Manager = {
    if (shared_session_manager.isEmpty) {
      shared_session_manager = Some(new Session_Manager(show_states, enable_cache, max_cache_size))
    }
    shared_session_manager.get
  }

  def get_repl_backend_with_shared_cache(show_states: Boolean, enable_cache: Boolean, max_cache_size: Int, initial_thys: java.util.List[String], field: String = "HOL"): ReplBackend = {
    val session_manager = get_shared_session_manager(show_states, enable_cache, max_cache_size)
    new ReplBackend(show_states, enable_cache, max_cache_size, initial_thys.asScala.toList, Some(session_manager), field)
  }
  
  def clear_shared_session_manager(): Unit = {
    shared_session_manager.foreach(_.shutdown())
    shared_session_manager = None
  }
}

object Gateway_App {
  def main(args: Array[String]): Unit = {
    val gateway = new GatewayServer(ReplBackendGateway, 0)
    gateway.start()
    val port = gateway.getListeningPort
    Output.writeln(port.toString, stdout = true)
  }
}
