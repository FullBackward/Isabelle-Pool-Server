theory LspDemo imports Main begin

lemma ok1: True by simp
lemma bad: False by simp  (* still wrong, but now it fails differently *)
