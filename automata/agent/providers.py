import logging
from abc import ABC, abstractmethod
from typing import Dict, Final, List, Sequence

from automata.agent.agent import Agent, AgentToolkitBuilder
from automata.agent.error import (
    AgentDatabaseError,
    AgentGeneralError,
    AgentMaxIterError,
    AgentResultError,
    AgentStopIteration,
)
from automata.config.base import ConfigCategory
from automata.config.openai_agent import OpenAIAutomataAgentConfig
from automata.core.utils import format_text, load_config
from automata.llm.foundation import (
    LLMChatMessage,
    LLMConversationDatabaseProvider,
    LLMIterationResult,
)
from automata.llm.providers.openai import (
    FunctionCall,
    OpenAIChatCompletionProvider,
    OpenAIChatMessage,
    OpenAIConversation,
    OpenAIFunction,
    OpenAITool,
)

logger = logging.getLogger(__name__)


class OpenAIAutomataAgent(Agent):
    """
    OpenAIAutomataAgent is an autonomous agent designed to execute instructions and report
    the results back to the main system. It communicates with the OpenAI API to generate
    responses based on given instructions and manages interactions with various tools.
    """

    CONTINUE_PREFIX: Final = f"Continue..."
    EXECUTION_PREFIX: Final = "Execution Result:"
    _initialized = False
    GENERAL_SUFFIX: Final = "NOTE - you are at iteration {iteration_count} out of a maximum of {max_iterations}. Please return a result with call_termination when ready."
    STOPPING_SUFFIX: Final = "NOTE - YOU HAVE EXCEEDED YOUR MAXIMUM ALLOWABLE ITERATIONS, RETURN A RESULT NOW WITH call_termination."

    def __init__(self, instructions: str, config: OpenAIAutomataAgentConfig) -> None:
        super().__init__(instructions)
        self.config = config
        self.iteration_count = 0
        self.agent_conversation_database = OpenAIConversation()
        self.completed = False
        self._setup()

    def __iter__(self):
        return self

    def __next__(self) -> LLMIterationResult:
        """
        Executes a single iteration of the task and returns the latest assistant and user messages.

        Raises:
            AgentError: If the agent has already completed its task or exceeded the maximum number of iterations.

        Returns:
            LLMIterationResult Latest assistant and user messages, or None if the task is completed.

        TODO:
            - Add support for multiple assistants.
            - Can we cleanup the logging?
        """
        if self.completed or self.iteration_count >= self.config.max_iterations:
            raise AgentStopIteration

        logging.debug(f"\n{('-' * 120)}\nLatest Assistant Message -- \n")
        assistant_message = self.chat_provider.get_next_assistant_completion()
        self.chat_provider.add_message(assistant_message)
        if not self.config.stream:
            logger.debug(f"{assistant_message}\n")
        logging.debug(f"\n{('-' * 120)}")

        self.iteration_count += 1

        user_message = self._get_next_user_response(assistant_message)
        logger.debug(f"Latest User Message -- \n{user_message}\n")
        self.chat_provider.add_message(user_message)
        logging.debug(f"\n{('-' * 120)}")

        return (assistant_message, user_message)

    @property
    def tools(self) -> List[OpenAITool]:
        """
        Returns:
            List[OpenAITool]: The tools for the agent.
        """
        tools = []
        for tool in self.config.tools:
            if not isinstance(tool, OpenAITool):
                raise ValueError(f"Invalid tool type: {type(tool)}")
            tools.append(tool)
        tools.append(self._get_termination_tool())
        return tools

    @property
    def functions(self) -> List[OpenAIFunction]:
        """
        Returns:
            List[OpenAIFunction]: The available functions for the agent.
        """
        return [ele.openai_function for ele in self.tools]

    def run(self) -> str:
        """
        Runs the agent and iterates through the tasks until a result is produced
        or the max iterations are exceeded.

        The agent must be setup before running.
        This implementation calls next() on self until a AgentStopIteration exception is raised,
        at which point it will break out of the loop and return the final result.

        Returns:
            str: The final agent output or an error message if the result wasn't found before an exception.

        Raises:
            AgentError:
                If the agent exceeds the maximum number of iterations.
                If the agent does not produce a result.
        """
        if not self._initialized:
            raise AgentGeneralError("The agent has not been initialized.")

        while True:
            try:
                next(self)
            except AgentStopIteration:
                break

        last_message = self.agent_conversation_database.get_latest_message()
        if not self.completed and self.iteration_count >= self.config.max_iterations:
            raise AgentMaxIterError("The agent exceeded the maximum number of iterations.")
        elif not self.completed or not isinstance(last_message, OpenAIChatMessage):
            raise AgentResultError("The agent did not produce a result.")
        elif not last_message.content:
            raise AgentResultError("The agent produced an empty result.")
        return last_message.content

    def set_database_provider(self, provider: LLMConversationDatabaseProvider) -> None:
        if not isinstance(provider, LLMConversationDatabaseProvider):
            raise AgentDatabaseError(f"Invalid database provider type: {type(provider)}")
        if self.database_provider:
            raise AgentDatabaseError("The database provider has already been set.")
        self.database_provider = provider
        self.agent_conversation_database.register_observer(provider)

    def _build_initial_messages(
        self, instruction_formatter: Dict[str, str]
    ) -> Sequence[LLMChatMessage]:
        """
        Builds the initial messages for the agent's conversation.
        The messages are built from the initial messages in the instruction config.
        All messages are formatted using the given instruction_formatter.

        TODO - Consider moving this logic to the conversation provider
        """
        assert "user_input_instructions" in instruction_formatter

        messages_config = load_config(
            ConfigCategory.INSTRUCTION.value, self.config.instruction_version.value
        )
        initial_messages = messages_config["initial_messages"]

        input_messages = []
        for message in initial_messages:
            input_message = (
                format_text(instruction_formatter, message["content"])
                if "content" in message
                else None
            )
            function_call = message.get("function_call")
            input_messages.append(
                OpenAIChatMessage(
                    role=message["role"],
                    content=input_message,
                    function_call=FunctionCall(
                        name=function_call["name"], arguments=function_call["arguments"]
                    )
                    if function_call
                    else None,
                )
            )

        return input_messages

    def _get_next_user_response(self, assistant_message: OpenAIChatMessage) -> OpenAIChatMessage:
        """
        Generates a user message based on the assistant's message.
        This is done by checking if the assistant's message contains a function call.
        If it does, then the corresponding tool is run and the result is returned.
        Otherwise, the user is prompted to continue the conversation.
        """
        if self.iteration_count != self.config.max_iterations - 1:
            iteration_message = OpenAIAutomataAgent.GENERAL_SUFFIX.format(
                iteration_count=self.iteration_count, max_iterations=self.config.max_iterations
            )
        else:
            iteration_message = OpenAIAutomataAgent.STOPPING_SUFFIX

        if assistant_message.function_call:
            for tool in self.tools:
                if assistant_message.function_call.name == tool.openai_function.name:
                    result = tool.run(assistant_message.function_call.arguments)
                    # Completion can occur from running `call_terminate` in the block above.
                    function_iteration_message = (
                        "" if self.completed else f"\n\n{iteration_message}"
                    )
                    return OpenAIChatMessage(
                        role="user",
                        content=f"{OpenAIAutomataAgent.EXECUTION_PREFIX}\n\n{result}{function_iteration_message}",
                    )
        return OpenAIChatMessage(
            role="user",
            content=f"{OpenAIAutomataAgent.CONTINUE_PREFIX}{iteration_message}",
        )

    def _setup(self) -> None:
        """
        Setup the agent by initializing the conversation and chat provider.

        Note:
            This should be called before running the agent.

        Raises:
            AgentError: If the agent fails to initialize.
        """
        logger.debug(f"Setting up agent with tools = {self.config.tools}")
        self.agent_conversation_database.add_message(
            OpenAIChatMessage(role="system", content=self.config.system_instruction)
        )
        for message in list(
            self._build_initial_messages({"user_input_instructions": self.instructions})
        ):
            logger.debug(f"Adding the following initial mesasge to the conversation {message}")
            self.agent_conversation_database.add_message(message)
            logging.debug(f"\n{('-' * 120)}")

        self.chat_provider = OpenAIChatCompletionProvider(
            model=self.config.model,
            temperature=self.config.temperature,
            stream=self.config.stream,
            conversation=self.agent_conversation_database,
            functions=self.functions,
        )
        self._initialized = True

        logger.debug(
            f"Initializing with System Instruction -- \n\n{self.config.system_instruction}\n\n"
        )
        logger.debug(f"\n{('-' * 60)}\nSession ID: {self.config.session_id}\n{'-'* 60}\n\n")

    def _get_termination_tool(self) -> OpenAITool:
        """Gets the tool responsible for terminating the OpenAI agent."""

        def terminate(result: str):
            self.completed = True
            return result

        return OpenAITool(
            name="call_termination",
            description="Terminates the conversation.",
            properties={
                "result": {
                    "type": "string",
                    "description": "The final result of the conversation.",
                }
            },
            required=["result"],
            function=terminate,
        )


class OpenAIAgentToolkitBuilder(AgentToolkitBuilder, ABC):
    """OpenAIAgentToolkitBuilder is an abstract class for building OpenAI agent tools."""

    @abstractmethod
    def build_for_open_ai(self) -> List[OpenAITool]:
        """Builds an OpenAITool to be used by the associated agent.
        TODO - Automate as much of this as possible, and modularize
        """
        pass

    @classmethod
    def can_handle(cls, tool_manager):
        return cls.TOOL_TYPE == tool_manager
