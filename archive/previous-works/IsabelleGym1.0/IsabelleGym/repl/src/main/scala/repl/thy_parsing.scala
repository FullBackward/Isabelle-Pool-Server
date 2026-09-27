package repl

import isabelle._

import scala.util.parsing.input.Reader

object Thy_Parsing {
  private def get_thy_tokens(reader: Reader[Char]): LazyList[Token] = {
    val token = Token.Parsers.token(Thy_Header.bootstrap_keywords)

    def make_tokens(in: Reader[Char]): LazyList[Token] =
      token(in) match {
        case Token.Parsers.Success(tok, rest) => tok #:: make_tokens(rest)
        case _                                => LazyList.empty
      }

    make_tokens(reader)
  }

  def get_thy_header_tokens(
      thy_header_reader: Reader[Char],
      drop_tokens_before_thy_command: Boolean = true
  ): List[Token] = {
    val all_tokens = get_thy_tokens(thy_header_reader)
    val processed_tokens =
      if (drop_tokens_before_thy_command)
        all_tokens.dropWhile(tok => !tok.is_command(Thy_Header.THEORY))
      else all_tokens
    processed_tokens.toList
  }

  private object Parsers extends Thy_Header.Parsers {
    def parse_header(tokens: List[Token]): Option[Thy_Header] =
      parse(commit(header), Token.reader(tokens, Token.Pos.command)) match {
        case Success(result, _) => Some(result)
        case _                  => None
      }
  }

  def extract_thy_header_from_tokens(
      thy_header_tokens: List[Token]
  ): Option[Thy_Header] =
    Parsers.parse_header(thy_header_tokens)

  def extract_thy_name(thy_name_string: String): Option[String] =
    get_thy_tokens(Scan.char_reader(thy_name_string)).toList match {
      case List(tok) if tok.is_ident || tok.is_name => Some(tok.content)
      case _                                        => None
    }
}
