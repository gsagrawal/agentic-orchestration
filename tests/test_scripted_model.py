"""The faked model: a pure function of the messages it is given."""

from orchestrator.scripted_model import ScriptedModel

TOOL = {"do": "tool", "tool": "lookup", "args": {}}


def assistant(n):
    return [{"role": "assistant", "content": TOOL}] * n


def test_follows_the_script_by_counting_its_own_earlier_messages():
    agent = {"script": [TOOL, {"do": "wait"}]}
    assert ScriptedModel().decide(agent, []) == TOOL
    assert ScriptedModel().decide(agent, assistant(1)) == {"do": "wait"}


def test_a_script_that_runs_out_finishes():
    assert ScriptedModel().decide({"script": [TOOL]}, assistant(1)) == {"do": "finish"}


def test_a_looping_script_starts_over():
    assert ScriptedModel().decide({"script": [TOOL, {"do": "wait"}], "loop": True}, assistant(2)) == TOOL


def test_an_empty_script_finishes_even_if_it_loops():
    assert ScriptedModel().decide({"script": [], "loop": True}, []) == {"do": "finish"}
