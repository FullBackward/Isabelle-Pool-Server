package repl

import isabelle._
import py4j.GatewayServer

object ReplBackendGateway {
  def get_repl_backend(show_states: Boolean): ReplBackend = new ReplBackend(show_states)
}

object Gateway_App {
  def main(args: Array[String]): Unit = {
    val gateway = new GatewayServer(ReplBackendGateway, 0)
    gateway.start()
    val port = gateway.getListeningPort
    Output.writeln(port.toString, stdout = true)
  }
}
