package repl

import scala.language.unsafeNulls

import isabelle.*
import java.util.concurrent.{ConcurrentHashMap, LinkedBlockingQueue, TimeUnit}

/**
 * Per-session ML communication channels — the Scala half of the transient
 * read-only probes in Backend_Probes (backend_probes.scala): per-channel
 * blocking queues plus the `Scala.Fun_Strings` callbacks the ML side
 * (repl/src/ml/REPL.ML) pushes replies into. Consumed by BOTH MCP workflows,
 * since both use the probes.
 *
 * The original implementation used a single global `Synchronized[Option[…]]`
 * slot for subgoals/local-facts/global-facts.  When multiple ReplBackend
 * instances call `open_subgoals()` concurrently the messages collide:
 *
 *   - "more subgoal messages arrived than requested" – a second session's
 *     response lands in the slot before the first has consumed it.
 *   - "Timeout waiting for subgoals message" – a session's response was
 *     stolen by another session.
 *
 * Fix: every message now carries a `channel_id` tag (the hex session hash
 * that the ML side includes).  A `ConcurrentHashMap` of per-channel queues
 * replaces the single global slot, so sessions can no longer interfere
 * with each other.
 *
 * COMPATIBILITY: If the ML side does NOT send a tagged message, we fall
 * back to a global default channel ("__default__") so that unmodified ML
 * code still works for single-session usage.
 */
object Repl_ML_Communication {
  // -----------------------------------------------------------------------
  // Per-channel infrastructure
  // -----------------------------------------------------------------------

  private val SUBGOALS_TIMEOUT_SECONDS: Int =
    sys.env.get("ISABELLE_REPL_SUBGOALS_TIMEOUT").flatMap(_.toIntOption).getOrElse(20)
  private val LOCAL_FACTS_TIMEOUT_SECONDS: Int =
    sys.env.get("ISABELLE_REPL_LOCAL_FACTS_TIMEOUT").flatMap(_.toIntOption).getOrElse(20)
  private val GLOBAL_FACTS_TIMEOUT_MINUTES: Int =
    sys.env.get("ISABELLE_REPL_GLOBAL_FACTS_TIMEOUT_MINUTES").flatMap(_.toIntOption).getOrElse(5)
  private val SLEDGEHAMMER_TIMEOUT_SECONDS: Int =
    sys.env.get("ISABELLE_REPL_SLEDGEHAMMER_TIMEOUT").flatMap(_.toIntOption).getOrElse(30)

  private val DEFAULT_CHANNEL = "__default__"

  // Each channel ID maps to a BlockingQueue that holds at most one message.
  private val subgoal_channels =
    new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()
  private val local_fact_channels =
    new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()
  private val global_fact_channels =
    new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()
  private val sledgehammer_channels =
    new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()
  private val in_proof_channels =
    new ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]]()

  private def get_or_create_queue(
    map: ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]],
    channel: String
  ): LinkedBlockingQueue[List[String]] =
    map.computeIfAbsent(channel, _ => new LinkedBlockingQueue[List[String]](1).nn)

  /** Remove the channel's queue so stale data cannot leak across reuses. */
  def clear_channel(channel: String): Unit = {
    subgoal_channels.remove(channel)
    local_fact_channels.remove(channel)
    global_fact_channels.remove(channel)
    sledgehammer_channels.remove(channel)
    in_proof_channels.remove(channel)
  }

  // -----------------------------------------------------------------------
  // Scala functions called FROM Isabelle/ML  (via Scala.Fun_Strings)
  // -----------------------------------------------------------------------

  /** Extract a channel tag from the first element if it starts with "CH:" */
  private def extract_channel(msgs: List[String]): (String, List[String]) =
    msgs match {
      case head :: tail if head.startsWith("CH:") => (head.stripPrefix("CH:"), tail)
      case _ => (DEFAULT_CHANNEL, msgs)
    }

  /** Offer a reply to the channel's queue. A full queue means a LATE or DUPLICATE
   *  reply (e.g. a probe that timed out, was retried, and eventually answered
   *  anyway) — drop it with a warning instead of raising `error` INSIDE the
   *  ML→Scala callback, where throwing can poison the document execution. */
  private def offer_reply(
    map: ConcurrentHashMap[String, LinkedBlockingQueue[List[String]]],
    channel: String, reply: List[String], kind: String
  ): Unit = {
    val q = get_or_create_queue(map, channel)
    if (!q.offer(reply))
      Output.writeln(s"I/Q REPL: dropping late/duplicate $kind reply (channel=$channel)")
  }

  object Open_Subgoals_Function extends Scala.Fun_Strings("add_open_subgoals") {
    val here = Scala_Project.here

    def apply(open_subgoals: List[String]): List[String] = {
      val (channel, goals) = extract_channel(open_subgoals)
      offer_reply(subgoal_channels, channel, goals, "subgoals")
      List()
    }
  }

  object Local_Facts_Function extends Scala.Fun_Strings("add_local_facts") {
    val here = Scala_Project.here

    def apply(received_local_facts: List[String]): List[String] = {
      val (channel, facts) = extract_channel(received_local_facts)
      offer_reply(local_fact_channels, channel, facts, "local facts")
      List()
    }
  }

  object Global_Facts_Function extends Scala.Fun_Strings("add_global_facts") {
    val here = Scala_Project.here

    def apply(received_global_facts: List[String]): List[String] = {
      val (channel, facts) = extract_channel(received_global_facts)
      offer_reply(global_fact_channels, channel, facts, "global facts")
      List()
    }
  }

  object Sledgehammer_Results_Function extends Scala.Fun_Strings("add_sledgehammer_results") {
    val here = Scala_Project.here

    def apply(received_results: List[String]): List[String] = {
      val (channel, results) = extract_channel(received_results)
      offer_reply(sledgehammer_channels, channel, results, "sledgehammer")
      List()
    }
  }

  object In_Proof_Function extends Scala.Fun_Strings("add_in_proof") {
    val here = Scala_Project.here

    def apply(received_in_proof: List[String]): List[String] = {
      val (channel, in_proof) = extract_channel(received_in_proof)
      offer_reply(in_proof_channels, channel, in_proof, "in_proof")
      List()
    }
  }
  // -----------------------------------------------------------------------
  // Blocking receive helpers (called from ReplBackend on the Scala side)
  // -----------------------------------------------------------------------

  def waiting_for_subgoals_message[T](block: => T, channel: String = DEFAULT_CHANNEL): List[String] = {
    val q = get_or_create_queue(subgoal_channels, channel)
    q.clear()   // discard any stale message
    block
    val result = q.poll(SUBGOALS_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    if (result == null) error(s"Timeout waiting for subgoals message (channel=$channel)")
    result
  }

  def waiting_for_local_facts_message[T](block: => T, channel: String = DEFAULT_CHANNEL): List[String] = {
    val q = get_or_create_queue(local_fact_channels, channel)
    q.clear()
    block
    val result = q.poll(LOCAL_FACTS_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    if (result == null) error(s"Timeout waiting for local facts message (channel=$channel)")
    result
  }

  def waiting_for_global_facts_message[T](block: => T, channel: String = DEFAULT_CHANNEL): List[String] = {
    val q = get_or_create_queue(global_fact_channels, channel)
    q.clear()
    block
    val result = q.poll(GLOBAL_FACTS_TIMEOUT_MINUTES * 60, TimeUnit.SECONDS)
    if (result == null) error(s"Timeout waiting for global facts message (channel=$channel)")
    result
  }

  def waiting_for_sledgehammer_message[T](block: => T, channel: String = DEFAULT_CHANNEL, timeout_s: Int = SLEDGEHAMMER_TIMEOUT_SECONDS ): List[String] = {
    val q = get_or_create_queue(sledgehammer_channels, channel)
    q.clear()   // discard any stale message from a previous call
    block
    // give the ML side `timeout_s` (the Isabelle-level timeout) plus a
    // 10-second grace period for overhead before declaring a Scala-side timeout
    val result = q.poll((timeout_s + 10).toLong, TimeUnit.SECONDS)
    if (result == null) error(s"Timeout waiting for sledgehammer message (channel=$channel)")
    result
  }
  def waiting_for_in_proof_message[T](block: => T, channel: String = DEFAULT_CHANNEL): List[String] = {
    val q = get_or_create_queue(in_proof_channels, channel)
    q.clear()   // discard any stale message
    block
    val result = q.poll(SUBGOALS_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    if (result == null) error(s"Timeout waiting for in_proof message (channel=$channel)")
    result
  }
}


class Scala_Functions
    extends Scala.Functions(
      Repl_ML_Communication.Open_Subgoals_Function,
      Repl_ML_Communication.Local_Facts_Function,
      Repl_ML_Communication.Global_Facts_Function,
      Repl_ML_Communication.Sledgehammer_Results_Function,
      Repl_ML_Communication.In_Proof_Function
    )
