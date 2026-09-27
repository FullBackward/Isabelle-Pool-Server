"""Tests for the Isabelle REPL"""

from test.common.utils import assert_program_has_expected_results, filter_non_ascii

from expecttest import Expect
from pytest import CaptureFixture

from repl.src.python.isabelle_repl import IsabelleRepl, ReplMetaCommand


def banner_message(header: str, message: str) -> str:
    """Print a banner with the given header and message."""
    return f"--- {header} --- \n{message}"


ReplProgramCommand = str
ReplProgram = list[ReplProgramCommand]


def assert_repl_program_has_expected_results(
    repl: IsabelleRepl,
    capsys: CaptureFixture[str],
    program: ReplProgram,
    expected_results: Expect,
) -> None:
    """Test the Repl output for a given program by simulating interaction."""

    def format_repl_command(command: ReplProgramCommand) -> str:
        prompt = repl.get_prompt_string()
        return f"{prompt}{command}"

    def get_repl_command_result_parts(command: ReplProgramCommand) -> list[str]:
        repl.runsource(command)

        captured = capsys.readouterr()
        captured_out = filter_non_ascii(captured.out.strip())
        captured_err = filter_non_ascii(captured.err.strip())

        command_output_parts = []

        if captured_out:
            command_output_parts.append(captured_out)

        if captured_err:
            command_output_parts.append(banner_message("STDERR", captured_err))

        return command_output_parts

    assert_program_has_expected_results(
        expected_results,
        program,
        format_repl_command,
        get_repl_command_result_parts,
    )


def test_help_command(repl: IsabelleRepl, capsys: CaptureFixture[str]) -> None:
    """Test the help command output."""
    program = [
        ReplMetaCommand.HELP,
    ]
    expected_output = Expect(
        """\
[no theory entered (use 'enter <theory>')] >>> help
Available commands:
  <isar_snippet>                      Append <isar_snippet> to the current theory.
  source                              Show the source code of the current theory.
  rollback                            Undo the last Isar command.
  enter <name>                        Enter or switch to theory <name>.
  exit / <EOF> (usually <Ctrl>+D)     Exit the REPL.
  help                                Show this help message."""
    )
    assert_repl_program_has_expected_results(repl, capsys, program, expected_output)
