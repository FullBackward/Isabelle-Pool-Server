import os
import sys
from typing import List, Optional
from pathlib import Path
from .operation import Success, Failure

DIR_ROOT = Path(__file__).parent.parent.parent.resolve()
thys_pool_dir = DIR_ROOT / "thys"
default_filepath = thys_pool_dir / "init.thy"

thys_pool = ["Analysis_init", "Complex_Main_init", "Pure_init"]

class ThyInit:
    def __init__(self, init_filepath: Path = default_filepath) -> None:
        self.filepath = init_filepath
        try:
            self.template = self.filepath.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise RuntimeError(f"init.thy file not found at {self.filepath}")
        except OSError as e:
            raise RuntimeError(f"Error reading init.thy file at {self.filepath}: {e}")

        self.created_files: set[str] = set()

    def format_thys(self, thys: List[str]) -> str:
        if not thys:
            return ""
        return " ".join(f'"{theory}"' for theory in thys)

    def generate_thy_file(self, thys: List[str], filename: str) -> str:
        imports_str = self.format_thys(thys)
        content = self.template.replace("[thys]", imports_str)
        content = content.replace("[filename]", filename)
        return content

    def gen_file(self, filename: str, thys: List[str]) -> Success | Failure:
        # if the target is already in the pool, no generation needed
        if filename in thys_pool:
            return Success(msg="No generation needed for pool theories.", data=filename)

        output_filepath = thys_pool_dir / f"{filename}.thy"
        content = self.generate_thy_file(thys, filename)

        try:
            output_filepath.write_text(content, encoding="utf-8")
        except OSError as e:
            return Failure(err=f"Failed to write to {output_filepath}: {e}")

        self.created_files.add(filename)
        return Success(msg=f"Generated {output_filepath}.", data=filename)

    def cleanup(self, filename: str) -> Success | Failure:
        if filename not in self.created_files:
            return Failure(err="No cleanup needed for untracked files.")
        if filename in thys_pool:
            return Success(msg="No cleanup needed for pool theories.")

        output_filepath = thys_pool_dir / f"{filename}.thy"
        try:
            if output_filepath.exists():
                output_filepath.unlink()
        except OSError as e:
            print(f"Error deleting {output_filepath}: {e}", file=sys.stderr)
            return Failure(err=f"Failed to delete {output_filepath}.")

        self.created_files.discard(filename)
        return Success(msg=f"Deleted {output_filepath}.")
