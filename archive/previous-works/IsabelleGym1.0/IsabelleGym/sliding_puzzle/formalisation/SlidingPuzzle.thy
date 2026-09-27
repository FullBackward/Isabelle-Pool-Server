theory SlidingPuzzle
  imports Main "HOL-Eisbach.Eisbach"
begin

datatype slot = Tile nat | Gap
type_synonym board = "slot list"

fun gap_index :: "board \<Rightarrow> nat option" where
  "gap_index [] = None"
| "gap_index (x # xs) =
     (if x = Gap then Some 0 else map_option Suc (gap_index xs))"

fun tile_index :: "nat \<Rightarrow> board \<Rightarrow> nat option" where
  "tile_index _ [] = None"
| "tile_index n (Tile m # xs) =
     (if n = m then Some 0 else map_option Suc (tile_index n xs))"
| "tile_index n (_ # xs) =
     map_option Suc (tile_index n xs)"

fun swap :: "nat \<Rightarrow> nat \<Rightarrow> board \<Rightarrow> board" where
  "swap i j b =
     (if i < length b \<and> j < length b then
        let bi = b ! i; bj = b ! j in
        b[i := bj, j := bi]
      else b)"

fun index_to_coord :: "nat \<Rightarrow> nat \<Rightarrow> nat \<times> nat" where
  "index_to_coord width i =
     (i div width, i mod width)"

fun is_adjacent :: "nat \<Rightarrow> nat \<Rightarrow> nat \<Rightarrow> bool" where
  "is_adjacent width i j =
     (let (row_i, col_i) = index_to_coord width i;
          (row_j, col_j) = index_to_coord width j
      in ((row_i = row_j \<and> abs (int col_i - int col_j) = 1) \<or>
          (col_i = col_j \<and> abs (int row_i - int row_j) = 1)))"

inductive slide :: "nat \<Rightarrow> board \<Rightarrow> nat \<Rightarrow> board \<Rightarrow> bool" where
  slide_move:
    "gap_index b = Some i \<Longrightarrow>
     tile_index n b = Some j \<Longrightarrow>
     is_adjacent width i j \<Longrightarrow>
     swap i j b = b' \<Longrightarrow>
     slide width b n b'"

fun equal_board :: "board \<Rightarrow> board \<Rightarrow> bool" where
  "equal_board [] [] = True"
| "equal_board (Tile x # xs) (Tile y # ys) =
     (x = y \<and> equal_board xs ys)"
| "equal_board (Gap # xs) (Gap # ys) =
     equal_board xs ys"
| "equal_board _ _ = False"

inductive solves :: "nat \<Rightarrow> board \<Rightarrow> board \<Rightarrow> bool" where
  refl: "equal_board b1 b2 \<Longrightarrow> solves width b1 b2"
| step: "slide width b1 n b2 \<Longrightarrow> solves width b2 b3 \<Longrightarrow> solves width b1 b3"

definition goal_board :: "nat \<Rightarrow> board" where
  "goal_board width = map Tile [1..<width^2] @ [Gap]"

definition solves_n_by_n_puzzle :: "nat \<Rightarrow> board \<Rightarrow> bool" where
  "solves_n_by_n_puzzle width b = solves width b (goal_board width)"

method move for n :: nat =
  ( rule solves.step,
    rule slide.slide_move[where n = n],
    auto simp: map_option_case )

method check_equal =
  (rule solves.refl, eval)

end
