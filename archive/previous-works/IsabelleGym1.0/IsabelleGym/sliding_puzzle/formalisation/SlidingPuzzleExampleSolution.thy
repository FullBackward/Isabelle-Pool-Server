theory SlidingPuzzleExampleSolution
  imports SlidingPuzzle
begin

definition scrambled_board :: board where
  "scrambled_board = [
     Tile 1, Tile 2, Tile 3,
     Tile 4, Tile 5, Tile 6,
     Gap,    Tile 7, Tile 8]"

theorem scrambled_solvable: "solves_n_by_n_puzzle 3 scrambled_board"
  unfolding scrambled_board_def
            solves_n_by_n_puzzle_def
  (* 1. solves 3 [Tile 1, Tile 2, Tile 3, Tile 4, Tile 5,
                  Tile 6, Gap, Tile 7, Tile 8] (goal_board 3) *)
  apply (move 7)
  (* 1. solves 3 [Tile 1, Tile 2, Tile 3, Tile 4, Tile 5,
                  Tile 6, Tile 7, Gap, Tile 8] (goal_board 3) *)
  apply (move 8)
  (* 1. solves 3 [Tile 1, Tile 2, Tile 3, Tile 4, Tile 5,
                  Tile 6, Tile 7, Tile 8, Gap] (goal_board 3) *)
  apply (check_equal)
  done
end
