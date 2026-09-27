theory McpLspDemo imports Main begin

lemma hd_id: "hd [x] = x" by simp
lemma use_hd: "hd [1::nat] = 1"
  by (simp add: hd_id)

lemma two_goals: "\<lbrakk> A; B \<rbrakk> \<Longrightarrow> A \<and> B"
proof (rule conjI)
  show A by assumption
  show B by assumption
qed

end
