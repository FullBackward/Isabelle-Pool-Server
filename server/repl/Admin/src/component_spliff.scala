/* Build Isabelle spliff component from official download. */

package repl

import isabelle._

object Component_Spliff {
  /* build Spliff */

  val default_download_url =
    "https://repo1.maven.org/maven2/io/bullet/spliff_2.13/0.8.0/spliff_2.13-0.8.0.jar"

  val default_target_dir = Path.explode("$ISABELLE_HOME_USER/contrib")

  def build_spliff(
      download_url: String = default_download_url,
      progress: Progress = new Progress,
      target_dir: Path = default_target_dir
  ): Unit = {
    val Download_Name = """^.*/([^/]+)\.jar""".r
    val download_name =
      download_url match {
        case Download_Name(download_name) => download_name
        case _ => error("Malformed jar download URL: " + quote(download_url))
      }

    /* component */
    Output.writeln(target_dir.toString)
    val component_dir =
      Components.Directory(target_dir + Path.basic(download_name)).create(progress = progress)

    File.write(
      component_dir.LICENSE,
      Url.read("https://raw.githubusercontent.com/sirthias/spliff/master/LICENSE")
    )

    /* README */

    File.write(
      component_dir.README,
      "This is " + download_name + " from\n" + download_url +
        "\n\nSee also https://github.com/sirthias/spliff\n\n" +
        Url.read("https://raw.githubusercontent.com/sirthias/spliff/master/README.md")
    )

    /* settings */

    component_dir.write_settings("""
ISABELLE_SPLIFF_HOME="$COMPONENT"

classpath "$ISABELLE_SPLIFF_HOME/lib/""" + download_name + """.jar"
""")

    /* jar */

    val jar = component_dir.lib + Path.basic(download_name).jar
    Isabelle_System.make_directory(jar.dir)
    Isabelle_System.download_file(download_url, jar, progress = progress)
    Components.update_components(true, component_dir.path)
  }

  /* Isabelle tool wrapper */

  val isabelle_tool =
    Isabelle_Tool(
      "component_spliff",
      "build Isabelle spliff component from official download",
      Scala_Project.here,
      { args =>
        var target_dir = default_target_dir
        var download_url = default_download_url

        val getopts = Getopts(
          s"""
Usage: isabelle component_spliff [OPTIONS] DOWNLOAD

  Options are:
    -D DIR       target directory (default ${default_target_dir})
    -U URL       download URL
                 (default: ${default_download_url})

  Build Spliff component from the specified download URL (JAR).
""",
          "D:" -> (arg => target_dir = Path.explode(arg)),
          "U:" -> (arg => download_url = arg)
        )

        val more_args = getopts(args)
        if (more_args.nonEmpty) getopts.usage()

        val progress = new Console_Progress()

        build_spliff(download_url = download_url, progress = progress, target_dir = target_dir)
      }
    )
}
