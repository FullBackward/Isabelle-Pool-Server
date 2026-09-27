package repl

import isabelle.*
import XML.Encode.*
import repl.Repl_ML_Communication.Open_Subgoals_Function

import java.util.concurrent.{ BlockingQueue, LinkedBlockingQueue, TimeUnit }
import scala.concurrent.Await

object Repl_ML_Communication {
  private val SUBGOALS_TIMEOUT_SECONDS = 20
  private val subgoals: Synchronized[Option[List[String]]] = Synchronized.apply(None)

  object Open_Subgoals_Function extends Scala.Fun_Strings("add_open_subgoals") {
    val here = Scala_Project.here

    def apply(open_subgoals: List[String]): List[String] = {
      subgoals.change {
        case Some(_) => error("more subgoal messages arrived than requested")
        case None    => Some(open_subgoals.map(Output.clean_yxml))
      }
      List()
    }
  }

  def waiting_for_subgoals_message[T](block: => T): List[String] = {
    subgoals.change(_ => None)
    block
    subgoals
      .timed_access(
        _ => Some(Time.now() + Time.seconds(SUBGOALS_TIMEOUT_SECONDS)),
        {
          case None        => None
          case Some(goals) => Some((goals, None))
        }
      )
      .getOrElse(error("Timeout waiting for subgoals message"))
  }

  private val LOCAL_FACTS_TIMEOUT_SECONDS = 20
  private val local_facts: Synchronized[Option[List[String]]] = Synchronized.apply(None)

  object Local_Facts_Function extends Scala.Fun_Strings("add_local_facts") {
    val here = Scala_Project.here

    def apply(received_local_facts: List[String]): List[String] = {
      local_facts.change {
        case Some(_) => error("more local facts messages arrived than requested")
        case None    => Some(received_local_facts.map(Output.clean_yxml))
      }
      List()
    }
  }

  def waiting_for_local_facts_message[T](block: => T): List[String] = {
    local_facts.change(_ => None)
    block
    local_facts
      .timed_access(
        _ => Some(Time.now() + Time.seconds(LOCAL_FACTS_TIMEOUT_SECONDS)),
        {
          case None        => None
          case Some(facts) => Some((facts, None))
        }
      )
      .getOrElse(error("Timeout waiting for local facts message"))
  }

  private val GLOBAL_FACTS_TIMEOUT_MINUTES = 5
  private val global_facts: Synchronized[Option[List[String]]] = Synchronized.apply(None)

  object Global_Facts_Function extends Scala.Fun_Strings("add_global_facts") {
    val here = Scala_Project.here

    def apply(received_global_facts: List[String]): List[String] = {
      global_facts.change {
        case Some(_) => error("more global facts messages arrived than requested")
        case None    => Some(received_global_facts.map(Output.clean_yxml))
      }
      List()
    }
  }

  def waiting_for_global_facts_message[T](block: => T): List[String] = {
    global_facts.change(_ => None)
    block
    global_facts
      .timed_access(
        _ => Some(Time.now() + Time.minutes(GLOBAL_FACTS_TIMEOUT_MINUTES)),
        {
          case None        => None
          case Some(facts) => Some((facts, None))
        }
      )
      .getOrElse(error("Timeout waiting for global facts message"))
  }
}

class Scala_Functions
    extends Scala.Functions(
      Repl_ML_Communication.Open_Subgoals_Function,
      Repl_ML_Communication.Local_Facts_Function,
      Repl_ML_Communication.Global_Facts_Function
    )
