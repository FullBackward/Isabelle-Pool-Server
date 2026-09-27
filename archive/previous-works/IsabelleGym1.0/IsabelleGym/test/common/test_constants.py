"""Common constants for testing."""

from test.common.utils import IsarSnippet

LONG_DUMMY_TEXT = """\
Lorem Ipsum is simply dummy text of the printing and typesetting industry.
Lorem Ipsum has been the industry's standard dummy text ever since the 1500s, 
when an unknown printer took a galley of type and scrambled it to make a type specimen 
book."""
SHORT_DUMMY_TEXT = "Lorem Ipsum"

TEST_THEORY = "Test"
TEST_THEORY_HEADER = IsarSnippet.theory_header(TEST_THEORY, ["Main"])
EXAMPLE_THEOREM = IsarSnippet.theorem("1+2=3")
