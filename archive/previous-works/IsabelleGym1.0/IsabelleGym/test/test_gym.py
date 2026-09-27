"""Tests for IsabelleGym."""

from test.common import test_constants
from test.common.utils import assert_program_has_expected_results, filter_non_ascii
from typing import Any

from expecttest import Expect, assert_expected_inline

from gym.isabelle_gym import IsabelleGym

GymProgramCommand = list[Any]
GymProgram = list[GymProgramCommand]


def formatted_result(result_type: str, content: str) -> str:
    """Format the output for display."""
    return f"{result_type}:\n{content}"


def formatted_object_string(actual_object: Any) -> str:
    """Formats object strings for comparison, avoiding over-escaping backslashes."""
    string_repr = repr(actual_object)
    over_backslash_escape_removed = string_repr.replace("\\" * 2, "\\")
    return over_backslash_escape_removed


def formatted_open_subgoals(open_subgoals: list[str]) -> str:
    """Formats object strings for comparison, avoiding over-escaping backslashes."""
    if not open_subgoals:
        return "[]"
    return "\n".join(map(formatted_object_string, open_subgoals))


def assert_gym_program_has_expected_results(
    gym: IsabelleGym,
    program: GymProgram,
    expected_results: Expect,
    auto_add_subgoals: bool = False,
) -> None:
    """Test the results of environment interaction via the gym match expectations."""

    def format_gym_command(command: GymProgramCommand) -> str:
        gym_f, *args = command
        arg_strings = [formatted_object_string(arg) for arg in args]
        return f">>> gym.{gym_f.__name__}({' ,'.join(arg_strings)})"

    def get_gym_command_result_parts(command: GymProgramCommand) -> list[str]:
        gym_f, *args = command
        result = gym_f(*args)
        separated_output = result.separated_output()
        out, err = map(
            filter_non_ascii, (separated_output.output(), separated_output.error())
        )

        command_result_parts = []
        if out:
            command_result_parts.append(formatted_result("Output", out))
        if err:
            command_result_parts.append(formatted_result("Error", err))
        if auto_add_subgoals:
            open_subgoals = gym.open_subgoals()
            if open_subgoals:
                assert len(open_subgoals) >= 1
                command_result_parts.append(
                    formatted_result("Subgoals", formatted_open_subgoals(open_subgoals))
                )
        return command_result_parts

    assert_program_has_expected_results(
        expected_results,
        program,
        format_gym_command,
        get_gym_command_result_parts,
    )


def assert_expected_gym_source(
    gym: IsabelleGym,
    expected_source: Expect,
) -> None:
    """Assert that the source code of the Isabelle environment matches expectations."""
    expected_source.assert_expected(gym.get_source().total_output())


def test_simple_state_saving_and_restoration(gym: IsabelleGym) -> None:
    """Test saving and restoring the state of the Isabelle environment."""
    gym.enter_thy(test_constants.TEST_THEORY)
    gym.step(test_constants.TEST_THEORY_HEADER)
    gym.step("lemma test1: sorry")
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)

    state1 = gym.save_state()
    gym.step("lemma test2: sorry")
    gym.step("lemma test3: sorry")
    state2 = gym.save_state()
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry
lemma test2: sorry
lemma test3: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)

    restore_successful = gym.restore_state(state1)
    assert restore_successful
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)

    restore_successful = gym.restore_state(state2)
    assert restore_successful
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry
lemma test2: sorry
lemma test3: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)


def test_multi_branch_state_saving_and_restoration(gym: IsabelleGym) -> None:
    """
    Test saving and restoring the state between multiple branches in the history tree.
    """
    gym.enter_thy(test_constants.TEST_THEORY)
    gym.step(test_constants.TEST_THEORY_HEADER)
    gym.step("lemma test1: sorry")
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)

    # We are going to create a fork from here
    fork_root_state = gym.save_state()

    gym.step("lemma test2: sorry")
    gym.step("lemma test3: sorry")
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry
lemma test2: sorry
lemma test3: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)
    branch1_state = gym.save_state()

    restore_successful = gym.restore_state(fork_root_state)
    assert restore_successful
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)

    # We create another branch from the fork root
    gym.step("lemma test4: sorry")
    gym.step("lemma test5: sorry")
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry
lemma test4: sorry
lemma test5: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)
    branch2_state = gym.save_state()

    # Now we go from the tip of branch 2 to the tip of branch 1
    restore_successful = gym.restore_state(branch1_state)
    assert restore_successful
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry
lemma test2: sorry
lemma test3: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)

    # Now we go from the tip of branch 1 to the tip of branch 2
    restore_successful = gym.restore_state(branch2_state)
    assert restore_successful
    expected_source = Expect(
        """\
theory Test imports Main begin
lemma test1: sorry
lemma test4: sorry
lemma test5: sorry"""
    )
    assert_expected_gym_source(gym, expected_source)


def test_minimal_proof_state_no_subgoals(gym: IsabelleGym) -> None:
    """Test list of open subgoals for non-proof state."""
    gym.enter_thy(test_constants.TEST_THEORY)
    gym.step(test_constants.TEST_THEORY_HEADER)
    subgoals = gym.open_subgoals()
    assert_expected_inline(formatted_open_subgoals(subgoals), """[]""")


def test_minimal_proof_state_single_subgoal(gym: IsabelleGym) -> None:
    """Test list of open subgoals for proof state with single subgoal."""
    gym.enter_thy(test_constants.TEST_THEORY)
    gym.step(test_constants.TEST_THEORY_HEADER)
    gym.step(test_constants.EXAMPLE_THEOREM)
    gym.step("proof -")
    subgoals = gym.open_subgoals()
    assert_expected_inline(formatted_open_subgoals(subgoals), """'1. 1 + 2 = 3'""")


def test_minimal_proof_state_multiple_subgoals(gym: IsabelleGym) -> None:
    """Test list of open subgoals for proof state with multiple subgoals."""
    gym.enter_thy(test_constants.TEST_THEORY)
    gym.step(test_constants.TEST_THEORY_HEADER)
    gym.step('lemma conj_commute: "P ∧ Q ⟹ Q ∧ P"')
    gym.step("proof")
    gym.step('assume PQ: "P ∧ Q"')

    subgoals = gym.open_subgoals()
    assert_expected_inline(
        formatted_open_subgoals(subgoals),
        """\
'1. P \\<and> Q \\<Longrightarrow> Q'
'2. P \\<and> Q \\<Longrightarrow> P'""",
    )


def test_local_facts(gym: IsabelleGym) -> None:
    """
    Test list of local facts yields previously proved lemmas in the current proof
    context.
    """
    gym.enter_thy(test_constants.TEST_THEORY)
    gym.step(test_constants.TEST_THEORY_HEADER)
    gym.step('lemma "1+2=(3::int)"')
    gym.step("proof -")
    gym.step('have "1+1=(2::int)" sorry')
    gym.step('show "1+2=(3::int)"')
    proof_state = gym.proof_state(local_facts=True)
    local_facts = proof_state.local_facts

    assert_expected_inline(
        "\n".join(local_facts),
        """??.<unnamed>: 1 + 1 = 2""",
    )


def test_global_facts(gym: IsabelleGym) -> None:
    """
    Test list of local facts yields previously proved lemmas in the current proof
    context.
    """
    gym.enter_thy(test_constants.TEST_THEORY)
    gym.step(test_constants.TEST_THEORY_HEADER)
    gym.step(test_constants.EXAMPLE_THEOREM)
    proof_state = gym.proof_state(global_facts=True, global_facts_limit=20)
    global_facts = proof_state.global_facts

    assert_expected_inline(
        "\n".join(global_facts),
        """\
bijI: inj ?f \\<Longrightarrow> surj ?f \\<Longrightarrow> bij ?f
id_o: id \\<circ> ?g = ?g
injD: inj ?f \\<Longrightarrow> ?f ?x = ?f ?y \\<Longrightarrow> ?x = ?y
injI: (\\<And>x y. ?f x = ?f y \\<Longrightarrow> x = y) \\<Longrightarrow> inj ?f
o_id: ?f \\<circ> id = ?f
allE: \\<forall>x. ?P x \\<Longrightarrow> (?P ?x \\<Longrightarrow> ?R) \\<Longrightarrow> ?R
allI: (\\<And>x. ?P x) \\<Longrightarrow> \\<forall>x. ?P x
cong: ?f = ?g \\<Longrightarrow> ?x = ?y \\<Longrightarrow> ?f ?x = ?g ?y
ex1E: \\<exists>!x. ?P x \\<Longrightarrow> (\\<And>x. ?P x \\<Longrightarrow> \\<forall>y. ?P y \\<longrightarrow> y = x \\<Longrightarrow> ?R) \\<Longrightarrow> ?R
ex1I: ?P ?a \\<Longrightarrow> (\\<And>x. ?P x \\<Longrightarrow> x = ?a) \\<Longrightarrow> \\<exists>!x. ?P x
exCI: (\\<forall>x. \\<not> ?P x \\<Longrightarrow> ?P ?a) \\<Longrightarrow> \\<exists>x. ?P x
exE: \\<exists>x. ?P x \\<Longrightarrow> (\\<And>x. ?P x \\<Longrightarrow> ?Q) \\<Longrightarrow> ?Q
exI: ?P ?x \\<Longrightarrow> \\<exists>x. ?P x
ext: (\\<And>x. ?f x = ?g x) \\<Longrightarrow> ?f = ?g
if_P: ?P \\<Longrightarrow> (if ?P then ?x else ?y) = ?x
iffE: ?P = ?Q \\<Longrightarrow> (?P \\<longrightarrow> ?Q \\<Longrightarrow> ?Q \\<longrightarrow> ?P \\<Longrightarrow> ?R) \\<Longrightarrow> ?R
mp: ?P \\<longrightarrow> ?Q \\<Longrightarrow> ?P \\<Longrightarrow> ?Q
sym: ?s = ?t \\<Longrightarrow> ?t = ?s
le0: 0 \\<le> ?n
UnE: ?c \\<in> ?A \\<union> ?B \\<Longrightarrow> (?c \\<in> ?A \\<Longrightarrow> ?P) \\<Longrightarrow> (?c \\<in> ?B \\<Longrightarrow> ?P) \\<Longrightarrow> ?P""",
    )


def test_facts_outside_proof_context(gym: IsabelleGym) -> None:
    """Test that facts are only available within a proof context."""
    gym.enter_thy(test_constants.TEST_THEORY)
    gym.step(test_constants.TEST_THEORY_HEADER)

    # We are not in a proof context, so the fact retrievals should terminate gracefully
    # with empty lists.
    proof_state = gym.proof_state(subgoals=True, local_facts=True, global_facts=True)
    assert not proof_state.open_subgoals
    assert not proof_state.local_facts
    assert not proof_state.global_facts
