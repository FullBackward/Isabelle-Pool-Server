package repl

import isabelle.Isabelle_Scala_Tools

class Repl_Admin_Tools
    extends Isabelle_Scala_Tools(
      Component_Py4J.isabelle_tool,
      Component_Spliff.isabelle_tool,
    )
