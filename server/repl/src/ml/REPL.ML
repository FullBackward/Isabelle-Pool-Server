(* REPL.ML — the Isabelle/ML side of the transient read-only probes AND the
   overlay query operations.

   Loaded into every session via the generated wrapper theories (repl/thys/,
   see thy_init.py). Consumed by the probes in Backend_Probes
   (backend_probes.scala), which insert `ML_val ‹Repl.send_*_tagged ...›`
   commands; the functions here extract subgoals/facts/proof-status from the
   toplevel state and push the results back to Scala through the tagged
   channel callbacks in Repl_ML_Communication (repl_ml_communication.scala).
   Also registers the "isabellegym_sledgehammer" overlay query operation used
   by Backend_File_Ops.sledgehammer_at (bottom of file). Serves BOTH MCP
   workflows, since both use the probes. *)

signature REPL =
sig

  val send_open_subgoals: Toplevel.state -> unit
  val send_local_facts: Toplevel.state -> unit
  val send_global_facts: Toplevel.state -> int -> unit

  (* Tagged variants: prepend "CH:<channel_id>" so the Scala side can
     route messages to per-backend queues, enabling safe concurrency. *)
  val send_open_subgoals_tagged: string -> Toplevel.state -> unit
  val send_local_facts_tagged: string -> Toplevel.state -> unit
  val send_global_facts_tagged: string -> Toplevel.state -> int -> unit
  val send_sledgehammer_tagged: string -> int -> Toplevel.state -> unit
  val send_in_proof_tagged: string -> Toplevel.state -> unit

end;

structure Repl : REPL =
struct

(* === Subgoal extraction and channel senders === *)

fun open_subgoals state =
  if (Toplevel.is_proof state) then (
    let 
      val proof = Toplevel.proof_of state;
      val ({context, goal, ...}) = Proof.raw_goal proof;
      val num_goals = goal |> Thm.prop_of |> Logic.strip_horn |> #1 |> length;
      val pretty_goals = if num_goals > 0 then Goal_Display.pretty_goals context goal else []
      fun clean_string s = 
        let val parsed = YXML.parse_body s
        in XML.content_of parsed end
    in
      List.map (clean_string o Pretty.string_of) pretty_goals
    end
  )
  else
    []

fun send_open_subgoals state =
  open_subgoals state |> \<^scala>\<open>add_open_subgoals\<close> |> ignore

fun send_open_subgoals_tagged channel_id state =
  let val goals = open_subgoals state
  in ("CH:" ^ channel_id) :: goals |> \<^scala>\<open>add_open_subgoals\<close> |> ignore end

(* === In-proof predicate (block lifetime, incl. pending qed) === *)

(* Whether the toplevel is still inside a proof block.  True for the whole
   block lifetime — INCLUDING after a successful terminal `show`, while `qed`
   is still pending — and false once `qed` lands (or for theory-level text).
   This is the "proof not registered yet" signal that bare subgoal counting
   misses (discharged-but-unclosed blocks fail batch builds with
   "Goal present in this block"). *)
fun send_in_proof_tagged channel_id state =
  ("CH:" ^ channel_id) :: [if Toplevel.is_proof state then "1" else "0"]
    |> \<^scala>\<open>add_in_proof\<close> |> ignore

(* === Local fact extraction and channel senders === *)

fun pretty_local_facts verbose ctxt =
  let
    val facts = Proof_Context.facts_of ctxt;
    val props = map #1 (Facts.props facts);
    val local_facts =
      (if null props then [] else [("<unnamed>", props)]) @
      Facts.dest_static verbose [Global_Theory.facts_of (Proof_Context.theory_of ctxt)] facts;
    fun clean_string s = 
      let val parsed = YXML.parse_body s
      in XML.content_of parsed end
  in
    if null local_facts then []
    else
      map #1 (sort_by (#1 o #2) (map (`(Proof_Context.pretty_fact ctxt)) local_facts))
      |> List.map (clean_string o Pretty.string_of)
  end;

fun local_facts state =
  if (Toplevel.is_proof state) then 
    state
    |> Toplevel.proof_of
    |> Proof.context_of
    |> pretty_local_facts true
  else
    []

fun send_local_facts state =
  state |> local_facts |> \<^scala>\<open>add_local_facts\<close> |> ignore

fun send_local_facts_tagged channel_id state =
  let val facts = local_facts state
  in ("CH:" ^ channel_id) :: facts |> \<^scala>\<open>add_local_facts\<close> |> ignore end

(* === Global fact extraction and channel senders === *)

fun global_facts state limit =
  if (Toplevel.is_proof state) then (
    let
      val ctxt = state |> Toplevel.proof_of |> Proof.context_of;
      val thy = Proof_Context.theory_of ctxt;
      val global_facts = Global_Theory.facts_of thy;
      
      fun collect_facts (name, thms) (count, acc) =
        if count >= limit then (count, acc)
        else (count + 1, (name, thms) :: acc);
      
      val (_, limited_facts) = 
        Facts.fold_static collect_facts global_facts (0, [])
        |> (fn (count, facts) => (count, sort_by #1 facts));
      
      fun clean_string s = 
        let val parsed = YXML.parse_body s
        in XML.content_of parsed end
    in
      limited_facts
      |> map (Proof_Context.pretty_fact ctxt)
      |> List.map (clean_string o Pretty.string_of)
    end
  )
  else
    []

fun send_global_facts state limit =
  let
    val global_facts = global_facts state limit;
  in
    \<^scala>\<open>add_global_facts\<close> global_facts |> ignore
  end

fun send_global_facts_tagged channel_id state limit =
  let
    val facts = global_facts state limit;
  in
    ("CH:" ^ channel_id) :: facts |> \<^scala>\<open>add_global_facts\<close> |> ignore
  end

(* === Sledgehammer: run + non-blocking channel sender === *)

(* Run sledgehammer on the first subgoal and collect every line it would
   normally print (e.g. "metis found a proof...", "Try this: by (metis ...)",
   "No proof found").  Runs SYNCHRONOUSLY in the caller's thread, so this must
   only ever be invoked from a forked thread (see send_sledgehammer_tagged)
   to avoid blocking the document node. *)
fun sledgehammer_results timeout_s state =
  if Toplevel.is_proof state then
    let
      val proof_state = Toplevel.proof_of state;
      val ctxt        = Proof.context_of proof_state;
      val thy         = Proof_Context.theory_of ctxt;
      (* default_params takes a theory; "timeout" is the Isabelle-level budget
         in seconds.  ("blocking", _) was removed in recent Isabelle and is no
         longer a valid parameter. *)
      val params      = Sledgehammer_Commands.default_params thy
                          [("timeout", string_of_int timeout_s)];
      (* Collect every message via the output callback instead of letting it
         go to the PIDE buffer.  The actual proof methods ("Try this: ...")
         arrive here, not in the return value (which is "" on success).
         Strip the YXML markup (sendback/break/block control codes) so the
         caller gets plain text, consistent with the other channels. *)
      val collected   = Unsynchronized.ref ([] : string list);
      fun clean_string s = XML.content_of (YXML.parse_body s);
      fun on_output s = collected := (clean_string s :: !collected);
      val (_, (_, message)) =
        Sledgehammer.run_sledgehammer
          params
          Sledgehammer_Prover.Normal
          (SOME on_output)                   (* route output here, not to PIDE buffer *)
          1                                  (* first subgoal *)
          Sledgehammer_Fact.no_fact_override (* use all available facts *)
          proof_state;
      val extra = if message = "" then [] else [clean_string message]
    in
      List.rev (!collected) @ extra
    end
  else
    []

(* Standalone, non-blocking sledgehammer channel.

   The ML command that calls this returns immediately after forking, so the
   document node is freed and other ML channels (open_subgoals, local_facts,
   ...) keep working while sledgehammer runs.  When the forked thread finishes
   it pushes the results back through the tagged Scala callback, where the
   Scala side is blocking on the matching per-channel queue. *)
fun send_sledgehammer_tagged channel_id timeout_s state =
  let
    fun run () =
      let
        (* Failure-safe: ANY exception in the ATP run must still answer the
           channel — otherwise the Scala side waits out the full timeout and
           the failure surfaces as an opaque 500 instead of the real cause.
           Uses Exn.capture: a bare `handle exn =>` is rejected by Isabelle/ML
           ("Handler catches all exceptions"); interrupts are still re-raised. *)
        val results =
          (case Exn.capture (fn () => sledgehammer_results timeout_s state) () of
            Exn.Res rs => rs
          | Exn.Exn exn =>
              if Exn.is_interrupt exn then Exn.reraise exn
              else ["ERROR sledgehammer: " ^ General.exnMessage exn])
      in ("CH:" ^ channel_id) :: results |> \<^scala>\<open>add_sledgehammer_results\<close> |> ignore end
  in
    ignore (Isabelle_Thread.fork (Isabelle_Thread.params "repl_sledgehammer") run)
  end

end

(* Re-export the channel senders at top level for the wrapper theories. *)

val send_open_subgoals = Repl.send_open_subgoals;
val send_local_facts = Repl.send_local_facts;
val send_global_facts = Repl.send_global_facts;
val send_open_subgoals_tagged = Repl.send_open_subgoals_tagged;
val send_local_facts_tagged = Repl.send_local_facts_tagged;
val send_global_facts_tagged = Repl.send_global_facts_tagged;
val send_sledgehammer_tagged = Repl.send_sledgehammer_tagged;
val send_in_proof_tagged = Repl.send_in_proof_tagged;

(* === Overlay query operation: position-explicit sledgehammer ===

   Registered when this file is loaded (the wrapper's ML_file), so every
   session — draft-wrapper and (Stage 3) heap-backed alike — has the
   "isabellegym_sledgehammer_query" print function. Args: [timeout_s, subgoal_i].
   Proof.enter_backward normalizes Forward ("state")-mode proof states (the
   state after an initial `proof (method)`), which run_sledgehammer rejects
   (Proof.assert_backward in sledgehammer.ML) — safe here because the query is
   read-only. Consumed via overlay by Backend_File_Ops.sledgehammer_at. *)
val _ =
  Query_Operation.register {name = "isabellegym_sledgehammer", pri = 0}
    (fn {state = st, args, writelns_result, ...} =>
      (case try Toplevel.proof_of st of
        SOME proof_state =>
          let
            val timeout_s = the_default 30 (Int.fromString (nth args 0));
            val i = the_default 1 (Int.fromString (nth args 1));
            val bwd_state = Proof.enter_backward proof_state;
            val ctxt = Proof.context_of bwd_state;
            val thy = Proof_Context.theory_of ctxt;
            val params = Sledgehammer_Commands.default_params thy
              [("timeout", string_of_int timeout_s)];
            val collected = Unsynchronized.ref ([] : string list);
            fun clean s = XML.content_of (YXML.parse_body s);
            fun on_output s = collected := ! collected @ [clean s];
            val (_, (_, message)) =
              Sledgehammer.run_sledgehammer params Sledgehammer_Prover.Normal
                (SOME on_output) i Sledgehammer_Fact.no_fact_override bwd_state;
            val extra = if message = "" then [] else [clean message];
          in writelns_result (! collected @ extra) end
      | NONE => error "Unknown proof context"));