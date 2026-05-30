"""Tests for intercept-agent.py — PreToolUse:Agent hook."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / 'hooks' / 'intercept-agent.py'


def _payload(subagent_type='Explore', prompt='', description=''):
    return {'tool_input': {
        'subagent_type': subagent_type,
        'prompt': prompt,
        'description': description,
    }}


class TestExploreCodeStructureDenied:
    """Explore agents with code-structure prompts should be denied."""

    @pytest.mark.parametrize('prompt', [
        'Where is main defined in the codebase?',
        'What calls the cache_wrap function?',
        'Find the definition of intercept_read',
        'Find the function handle_deny in hooks/',
        'Explore the hook enforcement code structure',
        'Read all hook scripts and analyze them',
        'Read every python file in hooks/',
        'Callers of _check_symbol_grep',
        'Test coverage for cache-wrap module',
        'Understand how the intercept hooks enforce routing',
        'Analyze the codebase structure',
        'Locate the class CacheManager',
        'Which tests cover the edit helper?',
        'Inspect the routing enforcement logic',
    ])
    def test_denied(self, run_hook, prompt):
        rc, out, err = run_hook(HOOK, _payload(prompt=prompt))
        deny = out.get('hookSpecificOutput', {})
        assert deny.get('permissionDecision') == 'deny', f'Expected deny for: {prompt}'
        assert 'cairn-graph' in deny.get('permissionDecisionReason', '')


class TestExploreNonCodeAllowed:
    """Explore agents targeting non-code content should pass through."""

    @pytest.mark.parametrize('prompt', [
        'Search through docs for deployment instructions',
        'Find all markdown files mentioning installation',
        'Look in the README for configuration options',
        'Search config yaml files for threshold settings',
        'Find documentation files about the cache system',
        'Cross-repo search for similar patterns',
    ])
    def test_allowed(self, run_hook, prompt):
        rc, out, err = run_hook(HOOK, _payload(prompt=prompt))
        assert out == {}, f'Expected allow for: {prompt}'


class TestNonExploreAllowed:
    """Non-Explore agent types should always pass through."""

    @pytest.mark.parametrize('subagent_type', [
        'general-purpose', 'Plan', 'code-reviewer', '', 'claude-code-guide',
    ])
    def test_allowed(self, run_hook, subagent_type):
        rc, out, err = run_hook(HOOK, _payload(
            subagent_type=subagent_type,
            prompt='Where is main defined?',
        ))
        assert out == {}


class TestGuards:
    """Edge cases and safety guards."""

    def test_disable_env_var(self, run_hook):
        rc, out, err = run_hook(HOOK, _payload(prompt='Where is main defined?'),
                                env={'CCH_DISABLE': '1'})
        assert out == {}

    def test_no_cairn_graph_allows(self, run_hook, tmp_path):
        """When cairn-graph is not on PATH, allow through."""
        # Strip PATH to exclude cairn-graph (subprocess doesn't inherit monkeypatch)
        rc, out, err = run_hook(HOOK, _payload(prompt='Where is main defined?'),
                                env={'PATH': str(tmp_path)})
        assert out == {}

    def test_malformed_json_passes(self, run_hook, tmp_path):
        """Malformed stdin should fail-open."""
        import json, os, subprocess, sys
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=b'not json',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, 'HOME': str(tmp_path)},
            timeout=10,
        )
        out = json.loads(proc.stdout.decode().strip())
        assert out == {}

    def test_missing_tool_input_passes(self, run_hook):
        rc, out, err = run_hook(HOOK, {})
        assert out == {}

    def test_description_also_matched(self, run_hook):
        """Code-structure intent in description (not just prompt) should trigger deny."""
        rc, out, err = run_hook(HOOK, _payload(
            description='Explore hook enforcement code',
            prompt='Look at the hooks directory and report what each script does.',
        ))
        deny = out.get('hookSpecificOutput', {})
        assert deny.get('permissionDecision') == 'deny'
