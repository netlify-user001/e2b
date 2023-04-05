import traceback

from typing import Optional, Tuple, List, Dict, Union, cast
from langchain.agents.tools import InvalidTool
from langchain.agents import AgentExecutor
from langchain.agents.chat.base import ChatAgent
from langchain.schema import AgentAction, AgentFinish
from langchain.tools.base import BaseTool

from codegen.agent.parsing import parse_thoughts_and_actions, ToolLog

# FINAL_ANSWER_TRIGGERS = ["Final Answer:", "FinalAnswer", "Final Answer"]

FINAL_ANSWER_ACTION = "Final Answer:"
FINAL_ANSWER_ACTION_NO_WHITESPACE = "FinalAnswer"

ACTIONS_QUEUE = "actions_queue"
MALFORMED_ANSWER = "malformed_answer"


class CodegenAgent(ChatAgent):
    def _extract_tool_and_input(self, text: str) -> Optional[Tuple[str, str | None]]:
        # if any(trigger in text for trigger in FINAL_ANSWER_TRIGGERS):
        if FINAL_ANSWER_ACTION in text:
            return "Final Answer", text.split(FINAL_ANSWER_ACTION)[-1].strip()
        try:
            # `ACTIONS_QUEUE` is not a real tool. The original implementation of `extract_tool_and_input` parses LLM's output. Our override doesn't do that.
            # Instead, we parse the LLM's output inside the `_atake_next_step` of the AgentExecutor. We do that because sometimes the LLM malforms
            # the desired format we specified in the prompt and puts <action> tags in the wrong place.
            # We say "the raw LLM's output is an input of ACTIONS_QUEUE tool" and parse the ACTIONS_QUEUE "tool" in the `_atake_next_step` while trying to be more resilient towards
            # malforrmed input and <action> tags in unexpected places.
            return ACTIONS_QUEUE, text
        except Exception as e:
            print(traceback.format_exc())
            print("Got exception in `_extract_tool_and_input:\n", e)
            # Sometimes the agent just completely messed up the output format.
            # We want to remind it that the last answer was wrong and it should
            # follow the format.
            # TODO: Improve, it doesn't always need to be malformed answer.
            return (
                MALFORMED_ANSWER,
                f"Wrong format! Follow the format! Reminder to ALWAYS use the exact the action `Final Answer` when you know the final answer. I just tried to parse your last reponse with `xml.etree.ElementTree.fromstring()` and received this error:\n{e}Reminder, that you should follow the format I told you!",
            )


class CodegenAgentExecutor(AgentExecutor):
    async def _arun_tool(
        self,
        tool_name: str,
        tool_input: str,
        color_mapping: Dict[str, str],
        name_to_tool_map: Dict[str, BaseTool],
    ) -> str:
        if tool_name not in name_to_tool_map:
            return await InvalidTool().arun(  # type: ignore
                tool_name,
                verbose=self.verbose,
                color=None,
                llm_prefix="",
                observation_prefix=self.agent.observation_prefix,
            )
        tool = name_to_tool_map[tool_name]
        return_direct = tool.return_direct
        color = color_mapping[tool_name]
        llm_prefix = "" if return_direct else self.agent.llm_prefix
            # We then call the tool on the tool input to get an observation
        return await tool.arun(
            tool_input,
            verbose=self.verbose,
            color=color,
            llm_prefix=llm_prefix,
            observation_prefix=self.agent.observation_prefix,
        )

    async def _atake_next_step(
        self,
        name_to_tool_map: Dict[str, BaseTool],
        color_mapping: Dict[str, str],
        inputs: Dict[str, str],
        intermediate_steps: List[Tuple[AgentAction, str]],
    ) -> Union[AgentFinish, Tuple[AgentAction, str]]:
        """Take a single step in the thought-action-observation loop.

        Override this to take control of how the agent makes and acts on choices.
        """
        # Call the LLM to see what to do.
        output = await self.agent.aplan(intermediate_steps, **inputs)
        # If the tool chosen is the finishing tool, then we end and return.
        if isinstance(output, AgentFinish):
            return output
        elif output.tool in [FINAL_ANSWER_ACTION, FINAL_ANSWER_ACTION_NO_WHITESPACE]:
            return AgentFinish({"output": output.tool_input}, output.log)

        # Sometimes the agent just completely messed up the output format.
        # We want to remind it that the last answer was wrong and it should
        # follow the format.
        if output.tool == MALFORMED_ANSWER:
            return output, output.tool_input

        if self.callback_manager.is_async:
            await self.callback_manager.on_agent_action(
                output, verbose=self.verbose, color="green"
            )
        else:
            self.callback_manager.on_agent_action(
                output, verbose=self.verbose, color="green"
            )

        if output.tool != ACTIONS_QUEUE:
            raise ValueError("Unknown tool:", output.tool)
        # TODO: Assign observations to each tool separately. Currently we return observation only from the last tool in the list.
        observation = ""
        # Go through each action and run it.
        # Collect outputs from each action.
        for action in (
            cast(ToolLog, action)
            for action in parse_thoughts_and_actions(output.tool_input)
            if action["type"] == "tool"
        ):
            observation = await self._arun_tool(
                tool_name=action["tool_name"],
                tool_input=action["tool_input"],
                name_to_tool_map=name_to_tool_map,
                color_mapping=color_mapping,
            )
            # observation = (
            #     observation
            #     + f"Output of action '{tool_name}':\n"
            #     + self._run_tool(
            #         tool_name=tool_name,
            #         tool_input=tool_input,
            #         name_to_tool_map=name_to_tool_map,
            #         color_mapping=color_mapping,
            #     )
            #     + "==="
            # )
        return output, observation
