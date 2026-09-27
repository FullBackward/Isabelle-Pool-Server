"""Read-eval-print-loop (REPL) for Isabelle, communicating via Isabelle/Scala."""

from code import InteractiveConsole
from types import ModuleType
from typing import Optional

import py4j

from repl.src.python.repl_backend_gateway import ReplResult

readline: Optional[ModuleType]
try:
    # pylint: disable=unused-import
    import readline
except ImportError:
    readline = None

from .isabelle_client import IsabelleClient


# pylint: disable=too-few-public-methods
class ReplMetaCommand:
    """Meta commands (non Isar snippets) for the Isabelle REPL."""

    SOURCE = "source"
    ROLLBACK = "rollback"
    ENTER = "enter"
    ENTER_THEORY_PREFIX = ENTER + " "
    EXIT = "exit"
    HELP = "help"


COMMAND_HELP = {
    "<isar_snippet>": "Append <isar_snippet> to the current theory.",
    ReplMetaCommand.SOURCE: "Show the source code of the current theory.",
    ReplMetaCommand.ROLLBACK: "Undo the last Isar command.",
    ReplMetaCommand.ENTER_THEORY_PREFIX + "<name>": "Enter or switch to theory <name>.",
    f"{ReplMetaCommand.EXIT} / <EOF> (usually <Ctrl>+D)": "Exit the REPL.",
    ReplMetaCommand.HELP: "Show this help message.",
}


class IsabelleRepl(InteractiveConsole):
    """Interactive console for interacting with Isabelle/HOL."""

    def __init__(self) -> None:
        super().__init__(None, "<console>")
        self.isabelle_client = IsabelleClient(show_states=True)
        self.exited = False

    def print_help(self) -> None:
        """Prints the help message for the REPL."""
        print("Available commands:")
        max_len_command = max(len(cmd) for cmd in COMMAND_HELP)
        for cmd, desc in COMMAND_HELP.items():
            print(f"  {cmd:<{max_len_command + 4}} {desc}")

    def step(self, code: str) -> Optional[ReplResult]:
        """Executes a single step of code in the REPL."""
        result = None
        match code.strip():
            case ReplMetaCommand.EXIT:
                raise SystemExit
            case ReplMetaCommand.HELP:
                self.print_help()
                result = None
            case ReplMetaCommand.SOURCE:
                result = self.isabelle_client.get_source()
            case ReplMetaCommand.ROLLBACK:
                result = self.isabelle_client.rollback()
            case cmd if cmd.startswith(ReplMetaCommand.ENTER_THEORY_PREFIX):
                thy_name = cmd.removeprefix(ReplMetaCommand.ENTER_THEORY_PREFIX)
                result = self.isabelle_client.enter_thy(thy_name)
            case _:
                result = self.isabelle_client.isar_snippet(code)
        return result

    def get_prompt_string(self) -> str:
        """
        Returns the current prompt for the REPL. The prompt includes the currently
        entered theory name, if any.
        """
        thy_name = self.isabelle_client.get_current_thy_name()
        prompt_end = ">>>"
        if thy_name:
            prompt_start = f"[theory = {thy_name}]"
        else:
            prompt_start = "[no theory entered (use 'enter <theory>')]"
        return f"{prompt_start} {prompt_end} "

    def raw_input(self, prompt: str = "") -> str:
        user_input = input(self.get_prompt_string())
        if user_input == ReplMetaCommand.EXIT:
            raise EOFError
        return user_input

    def runsource(
        self, source: str, filename: str = "<input>", symbol: str = "single"
    ) -> bool:
        try:
            result = self.step(source)
            if result is not None:
                output = result.total_output()
                if output:
                    print(output)
        except py4j.protocol.Py4JJavaError as e:
            print(e.java_exception.getMessage())
            raise
        need_more = False
        return need_more

    def reset(self) -> None:
        """Resets the REPL environment."""
        self.isabelle_client.reset()

    def start_interaction_loop(self) -> None:
        """Starts the REPL interaction loop."""
        banner = """\
====================================
 Welcome to the Isabelle/HOL REPL!
 Type 'help' for available commands.
===================================="""
        self.interact(
            banner=banner,
            exitmsg="",
        )


if __name__ == "__main__":
    repl = IsabelleRepl()
    repl.start_interaction_loop()
