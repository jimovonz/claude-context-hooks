"""Guards must not depend on how the model happened to quote its argv.

Two bugs found 2026-07-30 while adopting `sed -n 'A,Bp;Bq'` (early quit):

  1. `cat 'foo.py'` tokenised to `'foo.py'`, whose Path suffix is `.py'` and
     matched no code extension — so a single quote character bypassed the
     bulk-read block outright. Paths with spaces need quoting, so this was
     reachable by accident, not only by intent.
  2. `segments()` split on separators inside quotes, fragmenting
     `sed -n '1,200p;200q' f.py` into two useless halves and silently losing
     warn_bulk_sed — on the very form we now tell the model to prefer.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))

from lib.guards import (  # noqa: E402
    check_bulk_read, segments, warn_bulk_sed, _extract_code_file,
)
from lib.cairn_graph_footer import _extract_line_range  # noqa: E402


class TestQuotedPathsStillBlock:
    """A quoted path must not evade the bulk-read block."""

    def test_bare_path_blocks(self):
        assert check_bulk_read('cat big.py')

    def test_single_quoted_path_blocks(self):
        assert check_bulk_read("cat 'big.py'")

    def test_double_quoted_path_blocks(self):
        assert check_bulk_read('cat "big.py"')

    def test_quoted_path_blocks_with_trailing_segment(self):
        assert check_bulk_read("cat 'big.py' ;true")

    def test_extract_code_file_strips_quotes(self):
        assert _extract_code_file(" 'big.py'") == 'big.py'
        assert _extract_code_file(' "big.py"') == 'big.py'
        assert _extract_code_file(' big.py') == 'big.py'

    def test_non_code_extension_still_ignored_when_quoted(self):
        assert _extract_code_file(" 'notes.txt'") is None


class TestSegmentsAreQuoteAware:
    """A separator inside quotes is data, not a separator."""

    def test_semicolon_inside_quotes_is_not_a_split(self):
        assert segments("sed -n '1,200p;200q' f.py") == ["sed -n '1,200p;200q' f.py"]

    def test_pipe_inside_quotes_is_not_a_split(self):
        assert segments("rg 'a|b' f.py") == ["rg 'a|b' f.py"]

    def test_real_separators_still_split(self):
        assert segments('cat a.py | head -5') == ['cat a.py', 'head -5']
        assert segments('cat a.py && true') == ['cat a.py', 'true']
        assert segments('cat a.py ; true') == ['cat a.py', 'true']

    def test_unquoted_semicolon_after_sed_is_genuinely_two_commands(self):
        # Not a bug: without quotes the shell really does end the sed here.
        assert segments('sed -n 1,200p;200q f.py') == ['sed -n 1,200p', '200q f.py']


class TestEarlyQuitFormIsUnderstood:
    """`;Bq` is what we now suggest, so every parser must still read it."""

    def test_warn_bulk_sed_fires_on_early_quit_form(self):
        assert warn_bulk_sed("sed -n '1,200p;200q' foo.py")

    def test_warn_bulk_sed_fires_without_early_quit(self):
        assert warn_bulk_sed("sed -n '1,200p' foo.py")

    def test_small_early_quit_read_is_not_warned(self):
        assert warn_bulk_sed("sed -n '10,25p;25q' foo.py") is None

    def test_line_range_extracted_from_early_quit_form(self):
        assert _extract_line_range("sed -n '10,25p;25q' foo.py") == (10, 25)
        assert _extract_line_range('sed -n "10,25p;25q" foo.py') == (10, 25)

    def test_line_range_still_extracted_without_early_quit(self):
        assert _extract_line_range("sed -n '10,25p' foo.py") == (10, 25)

    def test_suggested_form_carries_the_quit(self):
        from lib.guards import _BULK_READ_REDIRECT
        assert "A,Bp;Bq" in _BULK_READ_REDIRECT
