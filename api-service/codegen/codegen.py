from typing import (
    List,
    List,
    Any,
    Dict,
    ClassVar,
)

from langchain.agents import AgentExecutor
from pydantic import BaseModel, PrivateAttr
from langchain.chat_models import ChatOpenAI
from langchain.callbacks.base import (
    AsyncCallbackManager,
    BaseCallbackManager,
)
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain.tools import BaseTool

from session.env import EnvVar
from database import Database
from codegen.agent import CodegenAgent, CodegenAgentExecutor
from codegen.callbacks.logs import LogsCallbackHandler
from codegen.prompt import (
    SYSTEM_PREFIX,
    SYSTEM_SUFFIX,
    SYSTEM_FORMAT_INSTRUCTIONS,
    HUMAN_INSTRUCTIONS_SUFFIX,
    get_human_instructions_prefix,
)

# class OutputFinalCode(BaseTool):
#     name = "OutputFinalCode"
#     description = "This is the last tool you would use. You use it when you know the final server code and you want to output it. The input should be the final server code that does what the user required."

#     def _run(self, final_code: str) -> str:
#         return final_code

#     async def _arun(self, final_code: str) -> str:
#         raise NotImplementedError("OutputFinalCode does not support async")


#     testing_instructions = """Here are your instructions:
# 1. Extract `email` from the incoming POST request.
# 2. If there's no email, respond back with an error.
# 3. Otherwise, respond back with the part of the email before the '@' sign.
# 4. Generate the full required server code and make sure it starts without any errors.
# 5. Test that the generated server from the previous step behaves as is required by making mock `curl` requests to the server.
# 6. Once all works without any bugs and errors, write the code to the file.
# 7. Deploy the code.
# """


class Codegen(BaseModel):
    input_variables: ClassVar[List[str]] = ["input", "agent_scratchpad", "method"]
    _agent: CodegenAgent = PrivateAttr()
    _agent_executor: AgentExecutor = PrivateAttr()
    _tools: List[BaseTool] = PrivateAttr()
    _llm: ChatOpenAI = PrivateAttr()
    _database: Database = PrivateAttr()
    _callback_manager: BaseCallbackManager = PrivateAttr()

    def __init__(
        self,
        database: Database,
        callback_manager: BaseCallbackManager,
        tools: List[BaseTool],
        llm: ChatOpenAI,
        agent: CodegenAgent,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._database = database
        self._callback_manager = callback_manager
        self._tools = tools
        self._llm = llm
        self._agent = agent

        self._agent_executor = CodegenAgentExecutor.from_agent_and_tools(
            agent=self._agent,
            tools=self._tools,
            verbose=True,
            callback_manager=self._callback_manager,
        )

    def tool_names(self):
        return [tool.name for tool in self._tools]

    @classmethod
    def from_tools_and_database(
        cls,
        custom_tools: List[BaseTool],
        database: Database,
    ):
        callback_manager = AsyncCallbackManager(
            [
                StreamingStdOutCallbackHandler(),
            ]
        )

        # Assign custom callback manager to custom tools
        for tool in custom_tools:
            tool.callback_manager = callback_manager

        # Create the LLM
        llm = ChatOpenAI(
            model_name="gpt-4",
            streaming=True,
            temperature=0,
            max_tokens=2056,
            verbose=True,
            callback_manager=callback_manager,
        )

        # Create CodegenAgent
        agent = CodegenAgent.from_llm_and_tools(
            llm=llm,
            tools=custom_tools,
            prefix=SYSTEM_PREFIX,
            suffix=SYSTEM_SUFFIX,
            format_instructions=SYSTEM_FORMAT_INSTRUCTIONS,
            input_variables=Codegen.input_variables,
            callback_manager=callback_manager,
        )

        return cls(
            database=database,
            callback_manager=callback_manager,
            tools=custom_tools,
            llm=llm,
            agent=agent,
        )

    async def generate(
        self,
        run_id: str,
        route: str,
        method: str,
        blocks: List[Dict],
    ):
        self._callback_manager.add_handler(
            LogsCallbackHandler(
                database=self._database, run_id=run_id, tool_names=self.tool_names()
            )
        )

        # Retrieve the description block.
        description_block: Dict[str, str] = next(
            b for b in blocks if b.get("type") == "Description"
        )

        # Retrueve the block describing the incoming request payload.
        incoming_request_block: Dict[str, str] | None = next(
            (b for b in blocks if b.get("type") == "RequestBody" and b.get("content")),
            None,
        )

        # Retrieve the instructions block.
        instructions_block: Dict[str, str] | None = next(
            (b for b in blocks if b.get("type") == "Instructions" and b.get("content")),
            None,
        )

        input_vars = {
            "description": description_block["content"],
            "request_body": f"{{\n{incoming_request_block['content']}\n}}"
            if incoming_request_block
            else None,
            "route": route,
            "method": method,
        }
        instructions = "Here are the instructions:"
        # inst_idx = 0

        # Append the premade prefix instructions.
        for instruction in get_human_instructions_prefix(
            has_request_body=bool(incoming_request_block)
        ):
            values = [v for k, v in input_vars.items() if k in instruction["variables"]]
            # Use the values to format the instruction string.
            inst = instruction["content"].format(*values)
            # instructions = instructions + "\n" + f"{inst_idx}. {inst}"
            instructions = instructions + "\n" + f"- {inst}"

        # Append the use instructions
        if instructions_block:
            instructions = (
                instructions
                + "\nHere are the required implementation instructions:\n"
                + instructions_block["content"]
            )

        print("Instructions:\n", instructions)

        ######## +++++ OLD
        # print("+++ BLOCKS")
        # print(blocks)
        # print("--- BLOCKS")
        # for block in blocks:
        #     if block.get("type") == "Basic":
        #         inst_idx += 1
        #         instructions = instructions + "\n" + f"{inst_idx}. " + block["prompt"]

        # Append the premade suffix instructions.
        for inst in HUMAN_INSTRUCTIONS_SUFFIX:
            instructions = f"{instructions}\n{inst}"

        # # instructions += "\nThought: Here is the plan of how I will go about solving this based on the instructions I got:\n1."
        # # instructions += "\nThought:"
        # print("Instructions:\n", instructions)
        ######## ----- OLD

        print("Running executor...")
        await self._agent_executor.arun(
            agent_scratchpad="",
            # input=testing_instructions
            input=instructions,
            method=method,
        )
