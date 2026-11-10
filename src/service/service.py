import inspect
import json
import logging
import warnings
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse  # type: ignore[import-untyped]
from langfuse.langchain import (
    CallbackHandler,  # type: ignore[import-untyped]
)
from langgraph.types import Command, Interrupt
from langsmith import Client as LangsmithClient
from langsmith import uuid7

from agents import DEFAULT_AGENT, AgentGraph, get_agent, get_all_agent_info, load_agent
from core import settings
from memory import initialize_database, initialize_store
from schema import (
    ChatHistory,
    ChatHistoryInput,
    ChatMessage,
    Feedback,
    FeedbackResponse,
    ServiceMetadata,
    StreamInput,
    UserInput,
    UserThreads,
    UserThreadsInput,
)
from service.agui import router as agui_router
from service.threads import list_user_threads
from service.utils import (
    convert_message_content_to_string,
    ensure_model_available,
    langchain_to_chat_message,
    messages_from_checkpoint,
    remove_tool_calls,
)

warnings.filterwarnings("ignore", category=LangChainBetaWarning)
logger = logging.getLogger(__name__)


def custom_generate_unique_id(route: APIRoute) -> str:
    """为 OpenAPI 客户端生成符合惯例的操作 ID。"""
    return route.name

"""单一共享密钥
"""
def verify_bearer(
    http_auth: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(HTTPBearer(description="Please provide AUTH_SECRET api key.", auto_error=False)),
    ],
) -> None:
    if not settings.AUTH_SECRET:
        return
    auth_secret = settings.AUTH_SECRET.get_secret_value()
    if not http_auth or http_auth.credentials != auth_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    可配置的生命周期，用于初始化对应的数据库 checkpointer、store，
    并以异步方式加载 agent——例如用于启动 MCP 客户端。
    """
    try:
        # 同时初始化 checkpointer（用于短期记忆）和 store（用于长期记忆）
        async with initialize_database() as saver, initialize_store() as store:
            # 配置这两个组件
            if hasattr(saver, "setup"):  # ignore: union-attr
                await saver.setup()
            # 只有 Postgres 的 store 需要 setup，InMemoryStore 不需要
            if hasattr(store, "setup"):  # ignore: union-attr
                await store.setup()

            if not settings.AUTH_SECRET:
                logger.warning(
                    "AUTH_SECRET is not configured — all API endpoints are unauthenticated. "
                    "Set AUTH_SECRET in your environment to enable bearer token authentication."
                )

            # 为 agent 配置两个记忆组件，并采用异步加载
            agents = get_all_agent_info()
            for a in agents:
                try:
                    await load_agent(a.key)
                    logger.info(f"Agent loaded: {a.key}")
                except Exception as e:
                    logger.error(f"Failed to load agent {a.key}: {e}")
                    # 继续加载其他 agent，而不是让启动失败

                agent = get_agent(a.key)
                # 为线程范围的记忆（对话历史）设置 checkpointer
                agent.checkpointer = saver
                # 为长期记忆（跨对话知识）设置 store
                agent.store = store
            yield
    except Exception as e:
        logger.error(f"Error during database/store/agents initialization: {e}")
        raise


app = FastAPI(lifespan=lifespan, generate_unique_id_function=custom_generate_unique_id)
router = APIRouter(dependencies=[Depends(verify_bearer)])
# AG-UI 协议端点沿用相同的 bearer 认证——参见 service/agui.py
router.include_router(agui_router)


@router.get("/info")
async def info() -> ServiceMetadata:
    models = list(settings.AVAILABLE_MODELS)
    models.sort()
    return ServiceMetadata(
        agents=get_all_agent_info(),
        models=models,
        default_agent=DEFAULT_AGENT,
        default_model=settings.DEFAULT_MODEL,
    )


async def _handle_input(
    user_input: UserInput, agent: AgentGraph, agent_id: str
) -> tuple[dict[str, Any], UUID]:
    """
    解析用户输入，并处理所需的中断恢复。
    返回用于调用 agent 的 kwargs 以及 run_id。
    """
    run_id = uuid7()
    thread_id = user_input.thread_id or str(uuid4())
    user_id = user_input.user_id or str(uuid4())

    configurable = {"thread_id": thread_id, "user_id": user_id}
    if user_input.model is not None:
        ensure_model_available(user_input.model)
        configurable["model"] = user_input.model

    callbacks: list[Any] = []
    if settings.LANGFUSE_TRACING:
        # 为 Langchain 初始化 Langfuse CallbackHandler（用于追踪）
        langfuse_handler = CallbackHandler()

        callbacks.append(langfuse_handler)

    if user_input.agent_config:
        # 检查保留键（即使不在 configurable 中也包含 'model'）
        reserved_keys = {"thread_id", "user_id", "model"}
        if overlap := reserved_keys & user_input.agent_config.keys():
            raise HTTPException(
                status_code=422,
                detail=f"agent_config contains reserved keys: {overlap}",
            )
        configurable.update(user_input.agent_config)

    config = RunnableConfig(
        configurable=configurable,
        metadata={"user_id": user_id, "agent_id": agent_id},
        run_id=run_id,
        callbacks=callbacks,
    )

    # 检查是否存在需要恢复的中断
    state = await agent.aget_state(config=config)

    interrupted_tasks = [
        task for task in state.tasks if hasattr(task, "interrupts") and task.interrupts
    ]

    input: Command | dict[str, Any]
    if interrupted_tasks:
        # 假定用户输入是对中断恢复执行的响应
        input = Command(resume=user_input.message)
    else:
        input = {"messages": [HumanMessage(content=user_input.message)]}

    kwargs = {
        "input": input,
        "config": config,
    }

    return kwargs, run_id


@router.post("/{agent_id}/invoke", operation_id="invoke_with_agent_id")
@router.post("/invoke")
async def invoke(user_input: UserInput, agent_id: str = DEFAULT_AGENT) -> ChatMessage:
    """
    使用用户输入调用 agent，以获取最终响应。

    如果未提供 agent_id，则使用默认 agent。
    通过 thread_id 可以持久化并继续多轮对话。消息中还会附带 run_id kwarg，
    用于记录反馈。
    通过 user_id 可以跨多个线程持久化并延续同一段对话。
    """
    # 注意：目前这里只返回最后一条消息或中断。
    # 当 agent 输出多条 AIMessage 时（例如 interrupt-agent 中的后台步骤，
    # 或 research-assistant 中的工具步骤），这些消息会被省略。按理说，
    # 可能希望把它们包含进来。那样的话就需要把 API 改为返回 ChatMessage 列表。
    agent: AgentGraph = get_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, agent_id)

    try:
        response_events: list[tuple[str, Any]] = await agent.ainvoke(**kwargs, stream_mode=["updates", "values"])  # type: ignore # fmt: skip
        response_type, response = response_events[-1]
        # 在中断处停止的运行会在任一 stream 模式的最新事件中上报该中断，
        # 因此在回退到最后一条消息之前，先检查是否存在中断。
        if "__interrupt__" in response:
            # 将第一个中断的值作为 AIMessage 返回
            output = langchain_to_chat_message(
                AIMessage(content=response["__interrupt__"][0].value)
            )
        elif response_type == "values":
            # 正常响应，agent 已成功完成
            output = langchain_to_chat_message(response["messages"][-1])
        else:
            raise ValueError(f"Unexpected response type: {response_type}")

        output.run_id = str(run_id)
        return output
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")


async def message_generator(
    user_input: StreamInput, agent_id: str = DEFAULT_AGENT
) -> AsyncGenerator[str, None]:
    """
    从 agent 生成消息流。

    这是 /stream 端点的核心方法。
    """
    agent: AgentGraph = get_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, agent_id)

    try:
        # 处理图输出的流式事件，并通过 SSE 流逐条推送消息。
        async for stream_event in agent.astream(  # type: ignore[no-matching-overload]
            **kwargs, stream_mode=["updates", "messages", "custom"], subgraphs=True
        ):
            if not isinstance(stream_event, tuple):
                continue
            # 根据子图设置处理不同的流事件结构
            if len(stream_event) == 3:
                # 启用 subgraphs=True 时：(node_path, stream_mode, event)
                _, stream_mode, event = stream_event
            else:
                # 未启用 subgraphs 时：(stream_mode, event)
                stream_mode, event = stream_event
            new_messages: list[Any] = []
            if stream_mode == "updates":
                for node, updates in event.items():
                    # 处理 agent 中断的一种简单方式。
                    # 在更完善的实现中，可以增加
                    # 某种结构化的 ChatMessage 类型来返回中断值。
                    if node == "__interrupt__":
                        interrupt: Interrupt
                        for interrupt in updates:
                            new_messages.append(AIMessage(content=interrupt.value))
                        continue
                    updates = updates or {}
                    update_messages = updates.get("messages", [])
                    # 使用 langgraph-supervisor 库时的特殊处理
                    if "supervisor" in node or "sub-agent" in node:
                        # 真正来自该 agent 的工具只有 handoff 和 handback
                        if isinstance(update_messages[-1], ToolMessage):
                            if "sub-agent" in node and len(update_messages) > 1:
                                # 如果是 sub-agent，则保留最后 2 条消息——handback 工具及其结果
                                update_messages = update_messages[-2:]
                            else:
                                # 如果是 supervisor，则只保留最后一条消息——handoff 的结果。工具消息来自 'agent' 节点。
                                update_messages = [update_messages[-1]]
                        else:
                            update_messages = []
                    new_messages.extend(update_messages)

            if stream_mode == "custom":
                new_messages = [event]

            # LangGraph 的流式输出可能产生元组：(field_name, field_value)
            # 例如 ('content', <str>)、('tool_calls', [ToolCall,...])、('additional_kwargs', {...}) 等。
            # 我们只把受支持的字段累加到 `parts` 中，并跳过不受支持的元数据。
            # 更多信息参见：https://langchain-ai.github.io/langgraph/cloud/how-tos/stream_messages/
            processed_messages = []
            current_message: dict[str, Any] = {}
            for message in new_messages:
                if isinstance(message, tuple):
                    key, value = message
                    # 将各字段暂存到临时字典中
                    current_message[key] = value
                else:
                    # 如果当前有正在拼接的消息，则先把它加入
                    if current_message:
                        processed_messages.append(_create_ai_message(current_message))
                        current_message = {}
                    processed_messages.append(message)

            # 补充剩余的消息片段
            if current_message:
                processed_messages.append(_create_ai_message(current_message))

            for message in processed_messages:
                try:
                    chat_message = langchain_to_chat_message(message)
                    chat_message.run_id = str(run_id)
                except Exception as e:
                    logger.error(f"Error parsing message: {e}")
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Unexpected error'})}\n\n"
                    continue
                # LangGraph 会重新发送输入消息，观感很奇怪，因此将其丢弃
                if chat_message.type == "human" and chat_message.content == user_input.message:
                    continue
                yield f"data: {json.dumps({'type': 'message', 'content': chat_message.model_dump()})}\n\n"

            if stream_mode == "messages":
                if not user_input.stream_tokens:
                    continue
                msg, metadata = event
                if "skip_stream" in metadata.get("tags", []):
                    continue
                # 某种情况下，astream("messages") 会让非 LLM 节点发送额外的消息。
                # 直接丢弃这些消息。
                if not isinstance(msg, AIMessageChunk):
                    continue
                content = remove_tool_calls(msg.content)
                if content:
                    # 在 OpenAI 的语境下，空内容通常意味着
                    # 模型正在请求调用某个工具。
                    # 因此只输出非空内容。
                    yield f"data: {json.dumps({'type': 'token', 'content': convert_message_content_to_string(content)})}\n\n"
    except Exception as e:
        logger.error(f"Error in message generator: {e}")
        yield f"data: {json.dumps({'type': 'error', 'content': 'Internal server error'})}\n\n"
    finally:
        yield "data: [DONE]\n\n"


def _create_ai_message(parts: dict) -> AIMessage:
    sig = inspect.signature(AIMessage)
    valid_keys = set(sig.parameters)
    filtered = {k: v for k, v in parts.items() if k in valid_keys}
    return AIMessage(**filtered)


def _sse_response_example() -> dict[int | str, Any]:
    return {
        status.HTTP_200_OK: {
            "description": "Server Sent Event Response",
            "content": {
                "text/event-stream": {
                    "example": "data: {'type': 'token', 'content': 'Hello'}\n\ndata: {'type': 'token', 'content': ' World'}\n\ndata: [DONE]\n\n",
                    "schema": {"type": "string"},
                }
            },
        }
    }


@router.post(
    "/{agent_id}/stream",
    response_class=StreamingResponse,
    responses=_sse_response_example(),
    operation_id="stream_with_agent_id",
)
@router.post("/stream", response_class=StreamingResponse, responses=_sse_response_example())
async def stream(user_input: StreamInput, agent_id: str = DEFAULT_AGENT) -> StreamingResponse:
    """
    以流式方式返回 agent 对用户输入的响应，包含中间消息和 token。

    如果未提供 agent_id，则使用默认 agent。
    通过 thread_id 可以持久化并继续多轮对话。所有消息中还会附带 run_id kwarg，
    用于记录反馈。
    通过 user_id 可以跨多个线程持久化并延续同一段对话。

    设置 `stream_tokens=false` 可返回中间消息，但不逐 token 返回。
    """
    return StreamingResponse(
        message_generator(user_input, agent_id),
        media_type="text/event-stream",
    )


@router.post("/feedback")
async def feedback(feedback: Feedback) -> FeedbackResponse:
    """
    将某次运行的反馈记录到 LangSmith。

    这是 LangSmith create_feedback API 的一层简单封装，使凭据可以
    保存在服务端统一管理，而不必放在客户端。
    参见：https://api.smith.langchain.com/redoc#tag/feedback/operation/create_feedback_api_v1_feedback_post
    """
    client = LangsmithClient()
    kwargs = feedback.kwargs or {}
    client.create_feedback(
        run_id=feedback.run_id,
        key=feedback.key,
        score=feedback.score,
        **kwargs,
    )
    return FeedbackResponse()


@router.post("/{agent_id}/history", operation_id="history_with_agent_id")
@router.post("/history")
async def history(input: ChatHistoryInput, agent_id: str = DEFAULT_AGENT) -> ChatHistory:
    """
    获取某个线程和 agent 的对话历史。

    如果未提供 agent_id，则使用默认 agent。
    """
    agent: AgentGraph = get_agent(agent_id)
    config = RunnableConfig(configurable={"thread_id": input.thread_id})
    try:
        messages: list[BaseMessage] = []
        # Functional-API 类型的 agent 会把对话保存在 `__previous__` 中，而 aget_state
        # 不会返回它，因此先读取原始 checkpoint，只有在图模式下才回退。
        checkpointer = getattr(agent, "checkpointer", None)
        if checkpointer:
            tup = await checkpointer.aget_tuple(config)
            if tup and "__previous__" in (tup.checkpoint.get("channel_values") or {}):
                messages = messages_from_checkpoint(tup.checkpoint)
        if not messages:
            state_snapshot = await agent.aget_state(config=config)
            messages = state_snapshot.values["messages"]
        chat_messages: list[ChatMessage] = [langchain_to_chat_message(m) for m in messages]
        return ChatHistory(messages=chat_messages)
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")


@router.get("/{agent_id}/threads", operation_id="threads_with_agent_id")
@router.get("/threads")
async def threads(
    input: UserThreadsInput = Depends(), agent_id: str = DEFAULT_AGENT
) -> UserThreads:
    """
    列出某个 agent 下用户的会话线程，按最近更新时间倒序排列。

    `user_id` 由调用方自行声明，不会与请求携带的凭据做校验，
    因此任何持有 bearer token 的人都可以列出任意用户的线程——这与 /history
    采用相同的信任模型。在终端用户能够访问之前，请在此之上自行实现授权。
    """
    agent: AgentGraph = get_agent(agent_id)
    checkpointer = getattr(agent, "checkpointer", None)
    if not checkpointer:
        return UserThreads(threads=[])

    try:
        summaries = await list_user_threads(checkpointer, input.user_id, agent_id, input.limit)
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")

    return UserThreads(threads=summaries)


@app.get("/health")
async def health_check():
    """健康检查端点。"""

    health_status = {"status": "ok"}

    if settings.LANGFUSE_TRACING:
        try:
            langfuse = Langfuse()
            health_status["langfuse"] = "connected" if langfuse.auth_check() else "disconnected"
        except Exception as e:
            logger.error(f"Langfuse connection error: {e}")
            health_status["langfuse"] = "disconnected"

    return health_status


app.include_router(router)
