(* REPL.ML — the Isabelle/ML side of Isabelle Pool Server's read-only state queries.

   Loaded into every session via the generated wrapper theories
   (server/repl/thys/, see thy_init.py). Every query is a Query_Operation
   (Pure/PIDE/query_operation.ML): the Scala side (Backend_Probes via
   Document_Utils.overlay_query) attaches the registered print function as a
   temporary document OVERLAY on a host command, the function runs against
   that command's result Toplevel.state, and every `writeln_result` becomes
   one instance-tagged RESULT in the host's command_results. No text edit is
   ever made, so the document, rollback chain and command history stay
   untouched. (Until 2026-09-30 the state queries were `ML_val` probe
   commands inserted at the document tip whose replies came back through
   Scala.Fun_Strings channels — a probe leaked into the script whenever its
   reply timed out; see docs/DESIGN_CHOICES.md 1.10 and docs/ISSUES.md
   tracker REPL-1.)

   Registered operations (Scala passes "<name>_query" as the print function):
     isabelle_pool_server_goals         []                  one line per open subgoal
     isabelle_pool_server_in_proof      []                  "1" | "0" (Toplevel.is_proof)
     isabelle_pool_server_local_facts   []                  one line per proof-context fact
     isabelle_pool_server_global_facts  [limit]             one line per global fact, bounded
     isabelle_pool_server_state         []                  rendered proof state, one line per
                                                   chunk; empty outside a proof
     isabelle_pool_server_sledgehammer  [timeout_s, subgoal] every line sledgehammer prints;
                                                   error "Unknown proof context"
                                                   outside a proof
   Serves BOTH MCP workflows: the chunk-centric one queries the document tip,
   the LSP-like one hosts sledgehammer on the command at a given line. *)

signature REPL =
sig
  val open_subgoals: Toplevel.state -> string list
  val local_facts: Toplevel.state -> string list
  val global_facts: Toplevel.state -> int -> string list
  val proof_state: Toplevel.state -> string list
  val sledgehammer_results: int -> int -> Toplevel.state -> string list
end;

structure Repl : REPL =
struct

(* YXML -> plain text: strip sendback/break/block markup so callers get plain
   strings (every query applies this before handing lines to Scala). *)
fun clean_string s = XML.content_of (YXML.parse_body s);

(* === Open subgoals === *)

fun open_subgoals state =
  if Toplevel.is_proof state then
    let
      val proof = Toplevel.proof_of state;
      val {context, goal, ...} = Proof.raw_goal proof;
      val num_goals = goal |> Thm.prop_of |> Logic.strip_horn |> #1 |> length;
      val pretty_goals = if num_goals > 0 then Goal_Display.pretty_goals context goal else [];
    in
      List.map (clean_string o Pretty.string_of) pretty_goals
    end
  else
    [];

(* === Local facts (facts visible in the proof context) === *)

fun pretty_local_facts verbose ctxt =
  let
    val facts = Proof_Context.facts_of ctxt;
    val props = map #1 (Facts.props facts);
    val local_facts =
      (if null props then [] else [("<unnamed>", props)]) @
      Facts.dest_static verbose [Global_Theory.facts_of (Proof_Context.theory_of ctxt)] facts;
  in
    if null local_facts then []
    else
      map #1 (sort_by (#1 o #2) (map (`(Proof_Context.pretty_fact ctxt)) local_facts))
      |> List.map (clean_string o Pretty.string_of)
  end;

fun local_facts state =
  if Toplevel.is_proof state then
    state |> Toplevel.proof_of |> Proof.context_of |> pretty_local_facts true
  else
    [];

(* === Global facts of the current theory, bounded by `limit` === *)

fun global_facts state limit =
  if Toplevel.is_proof state then
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
    in
      limited_facts
      |> map (Proof_Context.pretty_fact ctxt)
      |> List.map (clean_string o Pretty.string_of)
    end
  else
    [];

(* === Rendered proof state (what jEdit's Output panel shows) === *)

fun proof_state state =
  if Toplevel.is_proof state then
    Toplevel.pretty_state state |> List.map (clean_string o Pretty.string_of)
  else
    [];

(* === Sledgehammer on subgoal `i` with an Isabelle-level budget of `timeout_s`
   seconds. Collects every line sledgehammer would normally print (e.g. "metis
   found a proof...", "Try this: by (metis ...)", "No proof found") via the
   output callback — the return value of run_sledgehammer is "" on success.
   Proof.enter_backward normalizes Forward ("state")-mode proof states (the
   state after an initial `proof (method)`), which run_sledgehammer rejects
   (Proof.assert_backward in sledgehammer.ML) — safe here because the query is
   read-only. Runs SYNCHRONOUSLY in the print task; the Scala overlay bracket
   bounds the wait and removes the overlay (cancelling the task) on expiry.
   Raises "Unknown proof context" outside a proof. === *)

fun sledgehammer_results timeout_s i state =
  (case try Toplevel.proof_of state of
    NONE => error "Unknown proof context"
  | SOME proof_state =>
      let
        val bwd_state = Proof.enter_backward proof_state;
        val ctxt = Proof.context_of bwd_state;
        val thy = Proof_Context.theory_of ctxt;
        (* default_params takes a theory; "timeout" is the Isabelle-level budget
           in seconds. ("blocking", _) was removed in recent Isabelle. *)
        val params = Sledgehammer_Commands.default_params thy
          [("timeout", string_of_int timeout_s)];
        val collected = Unsynchronized.ref ([] : string list);
        fun on_output s = collected := ! collected @ [clean_string s];
        val (_, (_, message)) =
          Sledgehammer.run_sledgehammer params Sledgehammer_Prover.Normal
            (SOME on_output) i Sledgehammer_Fact.no_fact_override bwd_state;
        val extra = if message = "" then [] else [clean_string message];
      in ! collected @ extra end);

end;

(* === Query_Operation registrations ===

   Registered when this file is loaded (the wrapper's ML_file), so every
   session — draft-wrapper and (Stage 3) heap-backed alike — has them. One
   `writeln_result` per line: each call is a separate instance-tagged RESULT,
   which the Scala side collects into a List[String] in order (a single
   `writelns_result` would glue every line into one result body). The print
   function receives the HOST command's result state, i.e. the current
   toplevel state when hosted on the document's last command. *)

val _ =
  Query_Operation.register {name = "isabelle_pool_server_goals", pri = 0}
    (fn {state = st, writeln_result, ...} =>
      List.app writeln_result (Repl.open_subgoals st));

val _ =
  Query_Operation.register {name = "isabelle_pool_server_in_proof", pri = 0}
    (fn {state = st, writeln_result, ...} =>
      writeln_result (if Toplevel.is_proof st then "1" else "0"));

val _ =
  Query_Operation.register {name = "isabelle_pool_server_local_facts", pri = 0}
    (fn {state = st, writeln_result, ...} =>
      List.app writeln_result (Repl.local_facts st));

val _ =
  Query_Operation.register {name = "isabelle_pool_server_global_facts", pri = 0}
    (fn {state = st, args, writeln_result, ...} =>
      let
        val limit =
          (case args of
            a :: _ => the_default 100 (Int.fromString a)
          | [] => 100);
      in List.app writeln_result (Repl.global_facts st limit) end);

val _ =
  Query_Operation.register {name = "isabelle_pool_server_state", pri = 0}
    (fn {state = st, writeln_result, ...} =>
      List.app writeln_result (Repl.proof_state st));

val _ =
  Query_Operation.register {name = "isabelle_pool_server_sledgehammer", pri = 0}
    (fn {state = st, args, writeln_result, ...} =>
      let
        val timeout_s = the_default 30 (Int.fromString (nth args 0));
        val i = the_default 1 (Int.fromString (nth args 1));
      in List.app writeln_result (Repl.sledgehammer_results timeout_s i st) end);
