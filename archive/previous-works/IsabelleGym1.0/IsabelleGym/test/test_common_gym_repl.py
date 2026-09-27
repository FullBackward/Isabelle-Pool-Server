"""Tests for common functionality between the IsabelleGym and the Isabelle REPL."""

from dataclasses import dataclass
from test.common import test_constants
from test.common.utils import IsarSnippet
from test.test_gym import GymProgram, assert_gym_program_has_expected_results
from test.test_repl import ReplProgram, assert_repl_program_has_expected_results

from expecttest import Expect
from pytest import CaptureFixture

from gym.isabelle_gym import IsabelleGym
from repl.src.python.isabelle_repl import IsabelleRepl, ReplMetaCommand

CommonProgramCommand = list[str]
CommonProgram = list[CommonProgramCommand]


def translate_common_to_repl_program(program: CommonProgram) -> ReplProgram:
    """Translate the program in the common format to Isabelle REPL interactions."""
    return [" ".join(command_parts) for command_parts in program]


def translate_common_to_gym_program(
    gym: IsabelleGym, program: CommonProgram
) -> GymProgram:
    """Translate the program in the common format to IsabelleGym interactions."""
    gym_program = []
    repl_to_gym = {
        ReplMetaCommand.ENTER: gym.enter_thy,
        ReplMetaCommand.SOURCE: gym.get_source,
        ReplMetaCommand.ROLLBACK: gym.rollback,
    }
    for command_parts in program:
        command, *args = command_parts
        if command in repl_to_gym:
            gym_command = repl_to_gym[command]
            gym_program.append([gym_command, *args])
        elif not args:
            # Isar snippet
            gym_program.append([gym.step, command])
        else:
            raise ValueError(f"Invalid common program step: {command_parts}")
    return gym_program


@dataclass
class ExpectedGymReplResults:
    """Container for the expected results for the IsabelleGym and Isabelle REPL."""

    gym: Expect
    repl: Expect


def assert_expected_results_for_gym_and_repl(
    gym: IsabelleGym,
    repl: IsabelleRepl,
    capsys: CaptureFixture[str],
    program: CommonProgram,
    expected_results: ExpectedGymReplResults,
) -> None:
    """
    Assert that the results of the IsabelleGym and Isabelle REPL are as expected. The
    program is delivered as a list of REPL subcommands and arguments. These programs
    are then transformed into the equivalent interactions for the gym and REPL."""

    repl_program = translate_common_to_repl_program(program)
    assert_repl_program_has_expected_results(
        repl, capsys, repl_program, expected_results.repl
    )

    gym_program = translate_common_to_gym_program(gym, program)
    assert_gym_program_has_expected_results(
        gym, gym_program, expected_results.gym, auto_add_subgoals=True
    )


def enter_theory(theory_name: str) -> CommonProgramCommand:
    """Format an enter theory command."""
    return [ReplMetaCommand.ENTER, theory_name]


ENTER_TEST_THEORY = enter_theory(test_constants.TEST_THEORY)


def test_proof_state(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """Test the outputting of the proof state."""
    program = [
        ENTER_TEST_THEORY,
        [test_constants.TEST_THEORY_HEADER],
        [test_constants.EXAMPLE_THEOREM],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('Test')
>>> gym.step('theory Test imports Main begin')
>>> gym.step('theorem "1+2=3"')
Subgoals:
'1. 1 + 2 = 3'"""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter Test
[theory = Test] >>> theory Test imports Main begin
[theory = Test] >>> theorem "1+2=3"
proof (prove)
goal (1 subgoal):
 1. 1 + 2 = 3"""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_quick_proof(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """Test the output indicating a quick proof has been completed."""
    program = [
        ENTER_TEST_THEORY,
        [test_constants.TEST_THEORY_HEADER],
        [test_constants.EXAMPLE_THEOREM],
        ["by auto"],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('Test')
>>> gym.step('theory Test imports Main begin')
>>> gym.step('theorem "1+2=3"')
Subgoals:
'1. 1 + 2 = 3'
>>> gym.step('by auto')"""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter Test
[theory = Test] >>> theory Test imports Main begin
[theory = Test] >>> theorem "1+2=3"
proof (prove)
goal (1 subgoal):
 1. 1 + 2 = 3
[theory = Test] >>> by auto"""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_long_proof(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """Test the output indicating a long proof has been completed."""
    program = [
        ENTER_TEST_THEORY,
        [test_constants.TEST_THEORY_HEADER],
        [test_constants.EXAMPLE_THEOREM],
        ["proof -"],
        ["show ?thesis"],
        ["by auto"],
        ["qed"],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('Test')
>>> gym.step('theory Test imports Main begin')
>>> gym.step('theorem "1+2=3"')
Subgoals:
'1. 1 + 2 = 3'
>>> gym.step('proof -')
Subgoals:
'1. 1 + 2 = 3'
>>> gym.step('show ?thesis')
Subgoals:
'1. 1 + 2 = 3'
>>> gym.step('by auto')
Output:
show 1 + 2 = 3
Successful attempt to solve goal by exported rule:
  1 + 2 = 3
>>> gym.step('qed')"""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter Test
[theory = Test] >>> theory Test imports Main begin
[theory = Test] >>> theorem "1+2=3"
proof (prove)
goal (1 subgoal):
 1. 1 + 2 = 3
[theory = Test] >>> proof -
proof (state)
goal (1 subgoal):
 1. 1 + 2 = 3
[theory = Test] >>> show ?thesis
proof (prove)
goal (1 subgoal):
 1. 1 + 2 = 3
[theory = Test] >>> by auto
show 1 + 2 = 3
Successful attempt to solve goal by exported rule:
  1 + 2 = 3
proof (state)
this:
  1 + 2 = 3

goal:
No subgoals!
[theory = Test] >>> qed"""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_sending_edits_without_entering_theory(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """Test that sending edits without entering a theory is forbidden."""
    program = [
        [test_constants.TEST_THEORY_HEADER],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.step('theory Test imports Main begin')
Error:
Cannot make edits without entering theory."""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> theory Test imports Main begin
*** Cannot make edits without entering theory."""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_sending_edits_for_theory_different_to_one_entered(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """
    Test that starting a theory which is not the one that has been entered is forbidden.
    """
    program = [
        enter_theory("TheoryNameThatIsNotTest"),
        [test_constants.TEST_THEORY_HEADER],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('TheoryNameThatIsNotTest')
>>> gym.step('theory Test imports Main begin')
Error:
Name of theory in header must match name of current theory."""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter TheoryNameThatIsNotTest
[theory = TheoryNameThatIsNotTest] >>> theory Test imports Main begin
*** Name of theory in header must match name of current theory."""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_rollback_empty(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """
    Test that the rollback operation is graciously handled when no edits have been made.
    """
    program = [
        ENTER_TEST_THEORY,
        [ReplMetaCommand.ROLLBACK],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('Test')
>>> gym.rollback()
Error:
No text edits have been made to rollback."""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter Test
[theory = Test] >>> rollback
*** No text edits have been made to rollback."""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_rollback_non_empty(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """
    Test that the rollback operation undoes the last edit when there are non-zero edits.
    """
    program = [
        ENTER_TEST_THEORY,
        [test_constants.TEST_THEORY_HEADER],
        [ReplMetaCommand.SOURCE],
        [ReplMetaCommand.ROLLBACK],
        [ReplMetaCommand.SOURCE],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('Test')
>>> gym.step('theory Test imports Main begin')
>>> gym.get_source()
Output:
theory Test imports Main begin
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
>>> gym.rollback()
>>> gym.get_source()
Output:
theory Test imports Main begin
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>"""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter Test
[theory = Test] >>> theory Test imports Main begin
[theory = Test] >>> source
theory Test imports Main begin
[theory = Test] >>> rollback
[theory = Test] >>> source"""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_entering_multiple_theories(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """Test switching between multiple theories."""
    theory1 = "Test1"
    theory2 = "Test2"
    theorem1 = IsarSnippet.theorem("1+2=3")
    theorem2 = IsarSnippet.theorem("2+2=4")

    program = [
        enter_theory(theory1),
        [IsarSnippet.theory_header(theory1, ["Main"])],
        [theorem1],
        enter_theory(theory2),
        [IsarSnippet.theory_header(theory1, ["Main"])],
        [IsarSnippet.theory_header(theory2, ["Main"])],
        [theorem2],
        [ReplMetaCommand.SOURCE],
        enter_theory(theory1),
        [ReplMetaCommand.SOURCE],
        [ReplMetaCommand.ROLLBACK],
        enter_theory(theory2),
        [ReplMetaCommand.SOURCE],
        enter_theory(theory1),
        [ReplMetaCommand.SOURCE],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('Test1')
>>> gym.step('theory Test1 imports Main begin')
>>> gym.step('theorem "1+2=3"')
Subgoals:
'1. 1 + 2 = 3'
>>> gym.enter_thy('Test2')
>>> gym.step('theory Test1 imports Main begin')
Error:
Name of theory in header must match name of current theory.
>>> gym.step('theory Test2 imports Main begin')
>>> gym.step('theorem "2+2=4"')
Subgoals:
'1. 2 + 2 = 4'
>>> gym.get_source()
Output:
theory Test2 imports Main begin
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
theorem "2+2=4"
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
Subgoals:
'1. 2 + 2 = 4'
>>> gym.enter_thy('Test1')
Subgoals:
'1. 1 + 2 = 3'
>>> gym.get_source()
Output:
theory Test1 imports Main begin
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
theorem "1+2=3"
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
Subgoals:
'1. 1 + 2 = 3'
>>> gym.rollback()
Subgoals:
'1. 1 + 2 = 3'
>>> gym.enter_thy('Test2')
Subgoals:
'1. 2 + 2 = 4'
>>> gym.get_source()
Output:
theory Test2 imports Main begin
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
theorem "2+2=4"
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
Subgoals:
'1. 2 + 2 = 4'
>>> gym.enter_thy('Test1')
Subgoals:
'1. 1 + 2 = 3'
>>> gym.get_source()
Output:
theory Test1 imports Main begin
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
theorem "1+2=3"
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>
Subgoals:
'1. 1 + 2 = 3'"""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter Test1
[theory = Test1] >>> theory Test1 imports Main begin
[theory = Test1] >>> theorem "1+2=3"
proof (prove)
goal (1 subgoal):
 1. 1 + 2 = 3
[theory = Test1] >>> enter Test2
[theory = Test2] >>> theory Test1 imports Main begin
*** Name of theory in header must match name of current theory.
[theory = Test2] >>> theory Test2 imports Main begin
[theory = Test2] >>> theorem "2+2=4"
proof (prove)
goal (1 subgoal):
 1. 2 + 2 = 4
[theory = Test2] >>> source
theory Test2 imports Main begin
theorem "2+2=4"
[theory = Test2] >>> enter Test1
[theory = Test1] >>> source
theory Test1 imports Main begin
theorem "1+2=3"
[theory = Test1] >>> rollback
[theory = Test1] >>> enter Test2
[theory = Test2] >>> source
theory Test2 imports Main begin
theorem "2+2=4"
[theory = Test2] >>> enter Test1
[theory = Test1] >>> source
theory Test1 imports Main begin"""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def assert_theory_header_processed(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """
    Given the repl is in a state where the theory header should have been processed,
    test that it is.
    """
    program = [
        # Checks that the helper theory has been imported
        [test_constants.EXAMPLE_THEOREM]
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.step('theorem "1+2=3"')
Subgoals:
'1. 1 + 2 = 3'"""
        ),
        repl=Expect(
            """\
[theory = Test] >>> theorem "1+2=3"
proof (prove)
goal (1 subgoal):
 1. 1 + 2 = 3"""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_preamble_error_reporting(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """
    Test error reporting actually happens in the preamble as a proxy to check it is
    being processed before the theory header is provided.
    """
    program = [
        ENTER_TEST_THEORY,
        [IsarSnippet.section("Test section")],
        # Remove the first character of the command to create an error
        [IsarSnippet.text(test_constants.SHORT_DUMMY_TEXT)[1:]],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('Test')
>>> gym.step('section \\<open> Test section \\<close>')
>>> gym.step('ext \\<open> Lorem Ipsum \\<close>')
Error:
Outer syntax error: command expected,
but identifier ext was found"""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter Test
[theory = Test] >>> section \\<open> Test section \\<close>
[theory = Test] >>> ext \\<open> Lorem Ipsum \\<close>
*** Outer syntax error: command expected,
*** but identifier ext was found"""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )


def test_preamble(
    gym: IsabelleGym, repl: IsabelleRepl, capsys: CaptureFixture[str]
) -> None:
    """
    Test preamble can be added before theory without affecting remaining processing.
    """
    program = [
        ENTER_TEST_THEORY,
        [IsarSnippet.section("Test section")],
        [IsarSnippet.text(test_constants.LONG_DUMMY_TEXT)],
        [test_constants.TEST_THEORY_HEADER],
        [ReplMetaCommand.SOURCE],
    ]
    expected_results = ExpectedGymReplResults(
        gym=Expect(
            """\
>>> gym.enter_thy('Test')
>>> gym.step('section \\<open> Test section \\<close>')
>>> gym.step("text \\<open> Lorem Ipsum is simply dummy text of the printing and typesetting industry.\\nLorem Ipsum has been the industry's standard dummy text ever since the 1500s, \\nwhen an unknown printer took a galley of type and scrambled it to make a type specimen \\nbook. \\<close>")
>>> gym.step('theory Test imports Main begin')
>>> gym.get_source()
Output:
section \\<open> Test section \\<close>
text \\<open> Lorem Ipsum is simply dummy text of the printing and typesetting industry.
Lorem Ipsum has been the industry's standard dummy text ever since the 1500s, 
when an unknown printer took a galley of type and scrambled it to make a type specimen 
book. \\<close>
theory Test imports Main begin
ML_val \\<open> Repl.send_open_subgoals @{Isar.state} \\<close>"""
        ),
        repl=Expect(
            """\
[no theory entered (use 'enter <theory>')] >>> enter Test
[theory = Test] >>> section \\<open> Test section \\<close>
[theory = Test] >>> text \\<open> Lorem Ipsum is simply dummy text of the printing and typesetting industry.
Lorem Ipsum has been the industry's standard dummy text ever since the 1500s, 
when an unknown printer took a galley of type and scrambled it to make a type specimen 
book. \\<close>
[theory = Test] >>> theory Test imports Main begin
[theory = Test] >>> source
section \\<open> Test section \\<close>
text \\<open> Lorem Ipsum is simply dummy text of the printing and typesetting industry.
Lorem Ipsum has been the industry's standard dummy text ever since the 1500s, 
when an unknown printer took a galley of type and scrambled it to make a type specimen 
book. \\<close>
theory Test imports Main begin"""
        ),
    )
    assert_expected_results_for_gym_and_repl(
        gym, repl, capsys, program, expected_results
    )
    assert_theory_header_processed(gym, repl, capsys)
