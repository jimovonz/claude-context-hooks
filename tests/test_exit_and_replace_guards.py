"""Guards for two failures that shipped real damage.

Both were mechanically detectable at the moment they happened, and both were
missed because the signal was advisory or absent.
"""
from hooks.lib.guards import (check_pytest_exit_masked,
                              check_self_referential_replace)


# --- pytest exit status discarded by a pipeline ---

def test_pytest_piped_to_tail_is_blocked():
    """The real incident: a suite with 1 failure read as green because the
    pipeline reported tail's exit code, and a commit was made on it."""
    assert check_pytest_exit_masked("pytest tests/ -q 2>&1 | tail -2")


def test_pytest_piped_to_head_is_blocked():
    assert check_pytest_exit_masked("pytest -q | head -5")


def test_pipefail_makes_it_acceptable():
    """The guard asks for the status to survive, not for the pipe to go."""
    assert check_pytest_exit_masked("set -o pipefail; pytest -q | tail -3") is None


def test_bare_pytest_is_untouched():
    assert check_pytest_exit_masked("pytest tests/ -q") is None


def test_pytest_piped_to_grep_is_not_blocked():
    """Deliberately narrow: only head/tail truncation is the known footgun.
    A broad 'any pipe' rule would fire constantly and habituate."""
    assert check_pytest_exit_masked("pytest -q | grep FAILED") is None


def test_unrelated_tail_pipeline_is_untouched():
    assert check_pytest_exit_masked("cat log | tail -2") is None


# --- global replace that rewrites its own output ---

def test_replacement_containing_search_is_blocked():
    cmd = "cch-edit.py f.py 'log(x)' 'wrap(log(x))' --all"
    assert check_self_referential_replace(cmd)


def test_disjoint_replacement_is_allowed():
    cmd = "cch-edit.py f.py 'old_call()' 'new_call()' --all"
    assert check_self_referential_replace(cmd) is None


def test_without_all_flag_it_is_allowed():
    """A single-occurrence edit errors on non-uniqueness anyway, so the
    self-referential hazard needs --all to bite."""
    cmd = "cch-edit.py f.py 'log(x)' 'wrap(log(x))'"
    assert check_self_referential_replace(cmd) is None


def test_other_commands_are_untouched():
    assert check_self_referential_replace("rg --all 'a' 'ab'") is None


# --- bulk reads dressed up as interpreter one-liners ---

def test_python_read_oneliner_is_blocked():
    """Found by probing the guard with rewrites of a blocked command — the
    evasion was not hypothetical, it was the next thing to hand."""
    from hooks.lib.guards import check_interpreter_bulk_read
    assert check_interpreter_bulk_read('python3 -c "print(open(\'q.py\').read())"')


def test_perl_slurp_is_blocked():
    from hooks.lib.guards import check_interpreter_bulk_read
    assert check_interpreter_bulk_read("perl -ne 'print' cairn/query.py")


def test_python_without_read_is_allowed():
    from hooks.lib.guards import check_interpreter_bulk_read
    assert check_interpreter_bulk_read('python3 -c "print(1+1)"') is None


def test_awk_is_deliberately_not_blocked():
    """Recognising an awk program that happens to print most lines is a
    judgement call, and a guard that misfires trains reflexive overriding."""
    from hooks.lib.guards import check_interpreter_bulk_read
    assert check_interpreter_bulk_read("awk 'NR<600' cairn/query.py") is None


# --- circumvention is auditable ---

def test_override_is_recorded_with_guard_and_command(tmp_path, monkeypatch):
    """A bypass was countable but not attributable: digest-named marker files
    held nothing, so you could see six happened and never learn which guard."""
    import json
    from hooks.lib import guards
    log = tmp_path / "overrides.jsonl"
    monkeypatch.setattr(guards, "_OVERRIDE_LOG", log)
    guards.record_override("cat big_file.py", "Bulk read blocked. Use sed.")
    rec = json.loads(log.read_text().strip())
    assert rec["cmd"] == "cat big_file.py"
    assert "Bulk read blocked" in rec["guard"]
    assert rec["ts"]


def test_audit_never_breaks_the_command(monkeypatch):
    """Fail-soft: auditing a bypass must not become a way to fail a command."""
    from hooks.lib import guards
    monkeypatch.setattr(guards, "_OVERRIDE_LOG", None)
    guards.record_override("cat f.py", "reason")   # must not raise
