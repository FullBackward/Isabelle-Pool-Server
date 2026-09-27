"""Puzzle ennvironment for the sliding puzzle game built on IsabelleGym."""

import random
from enum import Enum
from pathlib import Path
from re import S
from typing import Optional

from gym.isabelle_gym import IsabelleGym

# 0 represents the empty tile
IntBoard = list[int]

IsabelleSlot = str
IsabelleBoard = str
IsabelleMove = int

SLIDING_PUZZLE_THEORY = Path(__file__).parent / "formalisation" / "SlidingPuzzle"


class Move(Enum):
    """Enum for the possible moves in the sliding puzzle."""

    UP = 0
    DOWN = 1
    LEFT = 2
    RIGHT = 3


MOVE_DIRECTIONS = {
    Move.UP: (-1, 0),
    Move.DOWN: (1, 0),
    Move.LEFT: (0, -1),
    Move.RIGHT: (0, 1),
}

OPPOSITE_MOVE = {
    Move.UP: Move.DOWN,
    Move.DOWN: Move.UP,
    Move.LEFT: Move.RIGHT,
    Move.RIGHT: Move.LEFT,
}


def goal_board(size: int) -> IntBoard:
    """
    Generate the goal state for a sliding puzzle of given size (where board is size x
    size).
    """
    return list(range(1, size**2)) + [0]


def gap_index(board: IntBoard) -> int:
    """Get the index of the gap (0) in the board."""
    return board.index(0)


def gap_row_col(board: IntBoard, size: int) -> int:
    """Get the row and column of the gap (0) in the board."""
    return divmod(gap_index(board), size)


def row_col_to_index(row: int, col: int, size: int) -> int:
    """Get the tile at the given row and column in the board."""
    return row * size + col


def possible_moves(board: IntBoard, size: int, last_move: Optional[Move]) -> list[Move]:
    """
    Determine the possible moves from the current board state.
    """
    gap_row, gap_col = gap_row_col(board, size)
    moves = set()
    if gap_row > 0:
        moves.add(Move.UP)
    if gap_row < size - 1:
        moves.add(Move.DOWN)
    if gap_col > 0:
        moves.add(Move.LEFT)
    if gap_col < size - 1:
        moves.add(Move.RIGHT)
    if last_move is not None:
        moves.discard(OPPOSITE_MOVE[last_move])
    return list(moves)


def move_to_swap(move: Move, board: IntBoard, size: int) -> int:
    gap_row, gap_col = gap_row_col(board, size)
    row_offset, col_offset = MOVE_DIRECTIONS[move]
    new_row = gap_row + row_offset
    new_col = gap_col + col_offset
    return (
        row_col_to_index(gap_row, gap_col, size),
        row_col_to_index(new_row, new_col, size),
    )


def do_move(move: Move, board: IntBoard, size: int) -> None:
    """Perform the given move on the board."""
    gap_i, tile_i = move_to_swap(move, board, size)
    board[gap_i], board[tile_i] = (
        board[tile_i],
        board[gap_i],
    )


def move_to_isabelle_move(move: Move, board: IntBoard, size: int) -> IsabelleMove:
    _, tile_index = move_to_swap(move, board, size)
    return board[tile_index]


def python_to_isabelle_slot(slot: int) -> IsabelleSlot:
    """Convert a Python slot representation to formalisation slot representation."""
    return "Gap" if slot == 0 else f"Tile {slot}"


def python_to_isabelle_board(board: IntBoard) -> IsabelleBoard:
    """Convert a Python board representation to formalisation board representation."""
    isabelle_list = ", ".join(map(python_to_isabelle_slot, board))
    return f"[{isabelle_list}]"


def isabelle_to_python_slot(slot: IsabelleSlot) -> int:
    """Convert an Isabelle slot representation to the Python slot representation."""
    if slot == "Gap":
        return 0
    slot = slot.replace("(Suc 0)", "1")
    return int(slot.split()[1])


def isabelle_to_python_board(board: IsabelleBoard) -> IntBoard:
    """Convert a formalised board representation to the Python board representation."""
    board_parts = board.strip("[]").split(", ")
    return [isabelle_to_python_slot(slot) for slot in board_parts]


def generate_scrambled_board(size: int, num_scrambles: int) -> IntBoard:
    """
    Generate a random scrambled board of the given size and with the given number of
    scrambling moves (i.e. the new board is at most 'num_scambles' away from the goal
    board).
    """
    board = goal_board(size)
    last_move = None
    for _ in range(num_scrambles):
        next_move = random.choice(possible_moves(board, size, last_move))
        do_move(next_move, board, size)
        last_move = next_move
    return board


class PuzzleEnv:
    """Sliding puzzle environment."""

    def __init__(self, puzzle_size: int = 3):
        """Initialise the sliding puzzle environment."""
        self.puzzle_size = puzzle_size
        self.goal_board = goal_board(puzzle_size)
        self.gym = IsabelleGym()
        self.gym.enter_thy("SlidingPuzzleSolution")
        self.gym.step(
            f'theory SlidingPuzzleSolution imports "{SLIDING_PUZZLE_THEORY}" begin'
        )
        self.solution_start_state = self.gym.save_state()
        self.last_move = None

    def begin_proof_for_start_state(self, start_board: IntBoard):
        """Begin a proof for the given start state."""
        self.gym.restore_state(self.solution_start_state)
        scrambled_board_def_parts = [
            "definition scrambled_board :: board where",
            f'"scrambled_board = {python_to_isabelle_board(start_board)}"',
        ]
        self.gym.step("\n".join(scrambled_board_def_parts)).total_output()
        self.gym.step(
            f'theorem scrambled_solvable: "solves_n_by_n_puzzle {self.puzzle_size} scrambled_board"'
        )
        self.gym.step("unfolding scrambled_board_def")
        self.gym.step("unfolding solves_n_by_n_puzzle_def")
        self.last_move = None

    def make_move(self, move: Move, board: IntBoard = None):
        """Make a move in the proof."""
        if board is None:
            board = self.get_current_board()
        self.gym.step(
            f"apply (move {move_to_isabelle_move(move, board, self.puzzle_size)})"
        )
        self.last_move = move

    def undo_last_move(self):
        """Rollback the last move in the proof."""
        self.gym.rollback()

    def get_current_board(self) -> IntBoard:
        """Get the current board state within a scrambled board solvable proof."""
        open_subgoals = self.gym.open_subgoals()
        assert (
            len(open_subgoals) == 1
        ), "there should be exactly one open subgoal throughout board solving proof"

        solves_board_subgoal = open_subgoals[0]
        isabelle_board = solves_board_subgoal.removeprefix(
            f"1. solves {self.puzzle_size} "
        ).removesuffix(f" (goal_board {self.puzzle_size})")
        return isabelle_to_python_board(isabelle_board)

    def get_possible_next_boards(self) -> dict[Move, IntBoard]:
        """Get the possible moves from the current board state."""
        board = self.get_current_board()
        moves = possible_moves(board, self.puzzle_size, self.last_move)

        possible_next_boards = {}
        for move in moves:
            new_board = board.copy()
            do_move(move, new_board, self.puzzle_size)
            possible_next_boards[move] = new_board
        return possible_next_boards

    def is_goal_board(self, board: IntBoard) -> bool:
        """Check if the given board is the goal board."""
        return board == self.goal_board

    def goal_board_reached(self) -> bool:
        """Check if the goal board has been reached in the proof."""
        return self.get_current_board() == self.goal_board
