"""vLLM tool parser for Qwen2.5-Coder: the Hermes parser, also accepting <tools>.

Qwen2.5-Coder-32B-Instruct writes its tool call as
    <tools>{"name": "bash", "arguments": {"command": "ls /tmp"}}</tools>
instead of the <tool_call>...</tool_call> its chat template asks for: the JSON
is right, the tag is the one the template uses to list the tools. The Hermes
parser looks only for <tool_call>, so every such reply would reach the agent
as text with no tool call (a format error). This parser renames the tag and
leaves the rest to the Hermes parser. Its SWE-bench bash-only score was
measured with mini-swe-agent 1.0, whose actions are text, not tool calls.

Loaded with --tool-parser-plugin <this file> --tool-call-parser qwen25_coder.
"""

from vllm.tool_parsers.abstract_tool_parser import ToolParserManager
from vllm.tool_parsers.hermes_tool_parser import Hermes2ProToolParser


def _retag(text: str) -> str:
    return text.replace("<tools>", "<tool_call>").replace("</tools>", "</tool_call>")


@ToolParserManager.register_module("qwen25_coder")
class Qwen25CoderToolParser(Hermes2ProToolParser):

    def extract_tool_calls(self, model_output, request):
        return super().extract_tool_calls(_retag(model_output), request)

    def extract_tool_calls_streaming(self, previous_text, current_text, delta_text,
                                     previous_token_ids, current_token_ids, delta_token_ids,
                                     request):
        return super().extract_tool_calls_streaming(
            _retag(previous_text), _retag(current_text), _retag(delta_text),
            previous_token_ids, current_token_ids, delta_token_ids, request)
