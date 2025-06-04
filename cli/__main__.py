import asyncio
import base64
import os
import urllib
import httpx

from uuid import uuid4

import asyncclick as click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.json import JSON
from rich.markdown import Markdown

from a2a.client import A2AClient, A2ACardResolver
from a2a.types import (
    Part,
    TextPart,
    FilePart,
    FileWithBytes,
    Task,
    TaskState,
    Message,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
    MessageSendConfiguration,
    SendMessageRequest,
    SendStreamingMessageRequest,
    MessageSendParams,
    GetTaskRequest,
    TaskQueryParams,
    JSONRPCErrorResponse,
)
from push_notification_auth import PushNotificationReceiverAuth

# Initialize Rich console
console = Console()

def extract_text_from_parts(parts):
    """Extract text content from message parts."""
    texts = []
    for part in parts:
        # Handle the nested Part structure: Part(root=TextPart(...))
        if hasattr(part, 'root') and hasattr(part.root, 'text'):
            texts.append(part.root.text)
        elif hasattr(part, 'text'):
            texts.append(part.text)
    return ' '.join(texts) if texts else ""

def print_agent_response(message_text: str):
    """Print agent response with nice formatting and colors."""
    if not message_text.strip():
        return
    
    # Create a panel with the agent's response
    panel = Panel(
        message_text,
        title="🤖 Agent Response",
        title_align="left",
        border_style="blue",
        padding=(0, 1)
    )
    console.print(panel)

def print_user_message(message_text: str):
    """Print user message with different styling."""
    text = Text(f"👤 You: {message_text}", style="bold white")
    console.print(text)

def print_status_update(status: str, message: str = ""):
    """Print status updates in a subtle way."""
    status_text = Text(f"📊 Status: {status}", style="dim cyan")
    if message:
        status_text.append(f" - {message}")
    console.print(status_text)

def print_agent_card(card):
    """Print agent card in a nice format."""
    console.print("\n🎯 [bold cyan]Agent Information[/bold cyan]")
    
    # Extract key information from the card
    card_data = card.model_dump(exclude_none=True)
    
    info_panel = Panel(
        f"[bold]Name:[/bold] {card_data.get('name', 'Unknown')}\n"
        f"[bold]Description:[/bold] {card_data.get('description', 'No description')}\n"
        f"[bold]Version:[/bold] {card_data.get('version', 'Unknown')}\n"
        f"[bold]URL:[/bold] {card_data.get('url', 'Unknown')}\n"
        f"[bold]Capabilities:[/bold] Streaming: {card_data.get('capabilities', {}).get('streaming', False)}, "
        f"Push Notifications: {card_data.get('capabilities', {}).get('pushNotifications', False)}",
        title="Agent Card",
        border_style="green"
    )
    console.print(info_panel)

@click.command()
@click.option("--agent", default="http://localhost:10000")
@click.option("--session", default=0)
@click.option("--history", default=False)
@click.option("--use_push_notifications", default=False)
@click.option("--push_notification_receiver", default="http://localhost:5000")
@click.option("--header", multiple=True)
async def cli(
    agent,
    session,
    history,
    use_push_notifications: bool,
    push_notification_receiver: str,
    header,
):
    headers = {h.split("=")[0]: h.split("=")[1] for h in header}
    console.print(f"[dim]Will use headers: {headers}[/dim]")
    async with httpx.AsyncClient(timeout=30, headers=headers) as httpx_client:
        card_resolver = A2ACardResolver(httpx_client, agent)
        card = await card_resolver.get_agent_card()

        print_agent_card(card)

        notif_receiver_parsed = urllib.parse.urlparse(push_notification_receiver)
        notification_receiver_host = notif_receiver_parsed.hostname
        notification_receiver_port = notif_receiver_parsed.port

        if use_push_notifications:
            from hosts.cli.push_notification_listener import (
                PushNotificationListener,
            )

            notification_receiver_auth = PushNotificationReceiverAuth()
            await notification_receiver_auth.load_jwks(f"{agent}/.well-known/jwks.json")

            push_notification_listener = PushNotificationListener(
                host=notification_receiver_host,
                port=notification_receiver_port,
                notification_receiver_auth=notification_receiver_auth,
            )
            push_notification_listener.start()

        client = A2AClient(httpx_client, agent_card=card)

        continue_loop = True
        streaming = card.capabilities.streaming
        context_id = session if session > 0 else uuid4().hex

        while continue_loop:
            console.print("\n" + "="*50)
            console.print("[bold green]Starting new conversation[/bold green]")
            console.print("="*50)
            continue_loop, _, taskId = await completeTask(
                client,
                streaming,
                use_push_notifications,
                notification_receiver_host,
                notification_receiver_port,
                None,
                context_id,
            )

            if history and continue_loop:
                console.print("\n[bold cyan]Conversation History[/bold cyan]")
                task_response = await client.get_task(
                    {"id": taskId, "historyLength": 10}
                )
                # Show history in a prettier format
                history_data = task_response.model_dump(include={"result": {"history": True}})
                console.print(JSON.from_data(history_data))

async def completeTask(
    client: A2AClient,
    streaming,
    use_push_notifications: bool,
    notification_receiver_host: str,
    notification_receiver_port: int,
    taskId,
    contextId,
):
    prompt = click.prompt(
        "\n💬 What do you want to send to the agent? (:q or quit to exit)",
        type=str
    )
    if prompt == ":q" or prompt == "quit":
        return False, None, None

    # Show what the user typed
    print_user_message(prompt)

    message = Message(
        role="user",
        parts=[TextPart(text=prompt)],
        messageId=str(uuid4()),
        taskId=taskId,
        contextId=contextId,
    )

    payload = MessageSendParams(
        id=str(uuid4()),
        message=message,
        configuration=MessageSendConfiguration(
            acceptedOutputModes=["text"],
        ),
    )

    if use_push_notifications:
        payload["pushNotification"] = {
            "url": f"http://{notification_receiver_host}:{notification_receiver_port}/notify",
            "authentication": {
                "schemes": ["bearer"],
            },
        }

    taskResult = None
    message = None
    if streaming:
        response_stream = client.send_message_streaming(
            SendStreamingMessageRequest(
                id=str(uuid4()),
                params=payload,
            )
        )
        async for result in response_stream:
            if isinstance(result.root, JSONRPCErrorResponse):
                console.print(f"[red]❌ Error: {result.root.error}[/red]")
                return False, contextId, taskId
            event = result.root.result
            contextId = event.contextId
            if isinstance(event, Task):
                taskId = event.id
                print_status_update("Task Created", f"ID: {taskId}")
            elif isinstance(event, TaskStatusUpdateEvent):
                taskId = event.taskId
                # Extract meaningful status information
                status = event.status
                
                # Show status updates
                if hasattr(status, 'state'):
                    if status.state == 'working':
                        print_status_update("Working", "Agent is processing...")
                    elif status.state == 'input-required':
                        # Only show the agent's message when we actually need input
                        if hasattr(status, 'message') and status.message:
                            message_text = extract_text_from_parts(status.message.parts)
                            if message_text:
                                print_agent_response(message_text)
                        print_status_update("Input Required", "Please respond to the question above")
                    elif status.state == 'completed':
                        print_status_update("Completed", "Task finished")
                    else:
                        print_status_update("Status Update", f"State: {status.state}")
            elif isinstance(event, TaskArtifactUpdateEvent):
                taskId = event.taskId
                print_status_update("Artifact Update", "New content available")
            elif isinstance(event, Message):
                message = event
                # Extract and display the message text immediately
                message_text = extract_text_from_parts(message.parts)
                if message_text:
                    print_agent_response(message_text)
        # Upon completion of the stream. Retrieve the full task if one was made.
        if taskId:
            taskResult = await client.get_task(
                GetTaskRequest(
                    id=str(uuid4()),
                    params=TaskQueryParams(id=taskId),
                )
            )
            taskResult = taskResult.root.result
    else:
        try:
            # For non-streaming, assume the response is a task or message.
            event = await client.send_message(
                SendMessageRequest(
                    id=str(uuid4()),
                    params=payload,
                )
            )
            event = event.root.result
        except Exception as e:
            console.print(f"[red]❌ Failed to complete the call: {e}[/red]")
        if not contextId:
            contextId = event.contextId
        if isinstance(event, Task):
            if not taskId:
                taskId = event.id
            taskResult = event
        elif isinstance(event, Message):
            message = event

    if message:
        message_text = extract_text_from_parts(message.parts)
        if message_text:
            print_agent_response(message_text)
        return True, contextId, taskId
    if taskResult:
        # Check if we need to show the task result
        state = TaskState(taskResult.status.state)
        if state.name == TaskState.input_required.name:
            # For input required state, extract and show the question nicely
            if hasattr(taskResult.status, 'message') and taskResult.status.message:
                question_text = extract_text_from_parts(taskResult.status.message.parts)
                if question_text:
                    print_agent_response(question_text)
            
            return (
                await completeTask(
                    client,
                    streaming,
                    use_push_notifications,
                    notification_receiver_host,
                    notification_receiver_port,
                    taskId,
                    contextId,
                ),
                contextId,
                taskId,
            )
        ## task is complete
        return True, contextId, taskId
    ## Failure case, shouldn't reach
    return True, contextId, taskId


if __name__ == "__main__":
    asyncio.run(cli())
