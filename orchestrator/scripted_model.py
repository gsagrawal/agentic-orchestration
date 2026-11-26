"""FAKED: stands in for the LLM (llm-gateway).

A script is a list of actions:
    {"do": "tool", "tool": "<name>", "args": {...}}
    {"do": "wait"}      wait for a human
    {"do": "finish"}
The model is a pure function of the messages it is given, like a real one: its
position in the script is the number of assistant messages already in the history.
A script that runs out finishes, unless the agent has "loop": true, in which
case it starts over. That is the "poison" script that never ends by itself.
"""


class ScriptedModel:
    def decide(self, agent: dict, messages: list[dict]) -> dict:
        script = agent["script"]
        position = sum(1 for m in messages if m["role"] == "assistant")
        if position >= len(script):
            if not agent.get("loop") or not script:  # nothing to loop over: finish
                return {"do": "finish"}
            position %= len(script)
        return script[position]
