signature REPL =
sig

  val send_open_subgoals: Toplevel.state -> unit
  val send_local_facts: Toplevel.state -> unit
  val send_global_facts: Toplevel.state -> int -> unit

end;

structure Repl : REPL =
struct

fun open_subgoals state =
  if (Toplevel.is_proof state) then (
    let 
      val proof = Toplevel.proof_of state;
      val ({context, goal, ...}) = Proof.raw_goal proof;
      val num_goals = goal |> Thm.prop_of |> Logic.strip_horn |> #1 |> length;
      val pretty_goals = if num_goals > 0 then Goal_Display.pretty_goals context goal else []
    in
      List.map Pretty.string_of pretty_goals
    end
  )
  else
    []

fun send_open_subgoals state =
  open_subgoals state |> \<^scala>\<open>add_open_subgoals\<close> |> ignore

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
      [List.hd (map #1 (sort_by (#1 o #2) (map (`(Proof_Context.pretty_fact ctxt)) local_facts)))]
  end;

fun local_facts state =
  if (Toplevel.is_proof state) then 
    state
    |> Toplevel.proof_of
    |> Proof.context_of
    |> pretty_local_facts true
    |> List.map Pretty.string_of
  else
    []

fun send_local_facts state =
  state |> local_facts |> \<^scala>\<open>add_local_facts\<close> |> ignore

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
    in
      limited_facts
      |> map (Proof_Context.pretty_fact ctxt)
      |> List.map Pretty.string_of
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

end

val send_open_subgoals = Repl.send_open_subgoals;
val send_local_facts = Repl.send_local_facts;
val send_global_facts = Repl.send_global_facts;
