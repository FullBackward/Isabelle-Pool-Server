"""Common utility functions for testing."""

from typing import Callable, TypeVar

from expecttest import Expect


def filter_non_ascii(txt: str) -> str:
    """Filter out non-ASCII characters from the text."""
    return txt.encode("ascii", "ignore").decode("ascii")


ProgramCommand = TypeVar("ProgramCommand")


def assert_program_has_expected_results(
    expected_results: Expect,
    program: list[ProgramCommand],
    format_command: Callable[[ProgramCommand], str],
    get_command_result_parts: Callable[[ProgramCommand], list[str]],
) -> None:
    """
    Given functions to generate formatted command and result strings, check that the
    actuall output of the program matches the expected results.
    """
    result_contents = []

    for command in program:
        result_contents.append(format_command(command))
        command_result_parts = get_command_result_parts(command)
        if command_result_parts:
            command_results = "\n".join(command_result_parts)
            result_contents.append(command_results)

    actual_output = "\n".join(result_contents)
    expected_results.assert_expected(actual_output)


class IsarSnippet:
    """Utilities for creating Isar snippets."""

    OPEN_BLOCK = "\\<open>"
    CLOSE_BLOCK = "\\<close>"

    @staticmethod
    def theory_header(theory_name: str, imports: list[str]) -> str:
        """Format a theory header command."""
        return f"theory {theory_name} imports {" ".join(imports)} begin"

    @staticmethod
    def theorem(x: str) -> str:
        """Format a string as a theorem."""
        return f'theorem "{x}"'

    @staticmethod
    def open_close_block(block_name: str, block_content: str) -> str:
        """
        Format a block that is opened and closed (e.g. text \\<open> Example \\<close>).
        """
        return f"{block_name} {IsarSnippet.OPEN_BLOCK} {block_content} {IsarSnippet.CLOSE_BLOCK}"

    @staticmethod
    def section(content: str) -> str:
        """Format a section block."""
        return IsarSnippet.open_close_block("section", content)

    @staticmethod
    def text(content: str) -> str:
        """Format a text block."""
        return IsarSnippet.open_close_block("text", content)
