import copy
import pytest

from src.project_pipeline import validate_pipeline, model_flags, check_pipeline_tasks
from src.jarvis_desktop.agent_jobs import command_for
from tests.test_orchestration import orch, _until
from tests.test_planner_routing import _post, _approve_and_finish

PIPELINE = {
    'planning': {'agent': 'antigravity', 'model': 'gemini-4-argon', 'effort': 'high'},
    'ui': {'agent': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high', 'enabled': True},
    'coding': {'agent': 'claude', 'model': 'sonnet', 'effort': 'medium'},
}


def test_validations_and_real_cli_arguments(tmp_path):
    value = validate_pipeline(PIPELINE)
    assert model_flags('antigravity', 'gemini-4-argon', 'high') == ['--model', 'gemini-4-argon-high', '--effort', 'high']
    args = command_for({'kind': 'execute', 'agent': 'codex', 'prompt': 'test', 'model': 'gpt-6.1-sol', 'effort': 'high'}, tmp_path, tmp_path)
    assert args[1:6] == ['exec', '--model', 'gpt-6.1-sol', '-c', 'model_reasoning_effort="high"']
    bad = copy.deepcopy(PIPELINE)
    bad['coding']['model'] = '--shell;echo'
    with pytest.raises(ValueError): validate_pipeline(bad)
    value['ui']['enabled'] = False
    assert check_pipeline_tasks({'tasks': [{'key': 'x', 'stage': 'ui', 'agent': 'codex'}]}, value)


async def test_three_stages_persist_and_dispatch_exact_settings(orch):
    status, conv = await _post(orch, '/v1/conversations', {'text': 'UNIT_PIPELINE', 'routing_version': 1, 'planning_agent': 'antigravity', 'execution_agent': 'claude', 'pipeline': PIPELINE})
    assert status == 202
    conv = await _until(orch, conv['id'], lambda c: c['state'] == 'awaiting_approval')
    plan = conv['plans'][-1]
    assert plan['body']['pipeline']['ui']['model'] == 'gpt-6.1-sol'
    tasks = plan['body']['tasks']
    assert tasks[0]['key'] in tasks[1]['depends_on']
    conv = await _approve_and_finish(orch, conv['id'], conv)
    jobs = orch['jobs']._all()
    planner = next(j for j in jobs if j['kind'] == 'coordinate' and j['model'])
    assert (planner['model'], planner['effort']) == ('gemini-4-argon', 'high')
    executed = sorted((j for j in jobs if j['kind'] == 'execute'), key=lambda j: j['created_at'])
    assert [(j['agent'], j['model'], j['effort']) for j in executed] == [('codex', 'gpt-6.1-sol', 'high'), ('claude', 'sonnet', 'medium')]
