package repl

import isabelle._

import scala.collection.mutable

case class Session_Data(id: UUID.T, session: Headless.Session)

class Session_Manager(show_states: Boolean) {
  private val (server_info, server) = Server_Utils.start_server()
  private val running_sessions: Synchronized[mutable.Set[UUID.T]] =
    Synchronized(mutable.Set.empty)
  private val pending_removals: Synchronized[mutable.Set[Future[Unit]]] =
    Synchronized(mutable.Set.empty)

  def get_new_session(initial_thys: List[String]): Session_Data = {
    val session_delay_options_to_minimise =
      List("headless_consolidate_delay", "headless_check_delay", "headless_nodes_status_delay")
    val min_delay = "0.1"
    val session_option_pairs =
      ("show_states", show_states.toString) ::
        session_delay_options_to_minimise.map(option_name => (option_name, min_delay))
    val session_options = session_option_pairs.map { case (name, value) => s"${name}=${value}" }
    val session_id = Server_Utils.start_session(server_info, server, session_options)
    running_sessions.change(_ += session_id)
    val session = server.the_session(session_id)
    if (initial_thys.nonEmpty)
      session.use_theories(initial_thys)
    Session_Data(session_id, session)
  }

  private def remove_session_sync(session_id: UUID.T): Unit = {
    Server_Utils.stop_session(server_info, server, session_id)
    running_sessions.change(_ -= session_id)
  }

  def remove_session_async(session_id: UUID.T): Unit = {
    val removal_future = Future.fork[Unit](remove_session_sync(session_id))
    pending_removals.change(_ += removal_future)
    removal_future.map(_ => pending_removals.change(_ -= removal_future))
  }

  def remove_session_async(session_data: Session_Data): Unit =
    remove_session_async(session_data.id)

  def shutdown(): Unit = {
    def apply_foreach[A](mutable_set: Synchronized[mutable.Set[A]])(f: A => Unit): Unit =
      mutable_set.guarded_access(set => Some((set.toList, set))).foreach(f)

    apply_foreach(running_sessions)(remove_session_async)

    apply_foreach(pending_removals) { removal_future =>
      try removal_future.join
      catch {
        case e: Throwable =>
          Output.error_message(s"Error during session removal: ${Exn.message(e)}")
      }
    }
    Server_Utils.stop_server(server_info)
  }
}
