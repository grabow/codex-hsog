//! SSE stream processing for Chat Completions API.
//!
//! This module handles Server-Sent Events from the Chat Completions API,
//! converting them to Codex's internal `ResponseEvent` format for compatibility.

use crate::common::ResponseEvent;
use crate::common::ResponseStream;
use crate::error::ApiError;
use crate::rate_limits::parse_all_rate_limits;
use crate::telemetry::SseTelemetry;
use codex_client::ByteStream;
use codex_client::StreamResponse;
use codex_protocol::models::ContentItem;
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::TokenUsage;
use eventsource_stream::Eventsource;
use futures::StreamExt;
use serde::Deserialize;
use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::OnceLock;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::time::Instant;
use tokio::time::timeout;
use tracing::debug;
use tracing::trace;

/// Spawns a Chat Completions SSE stream processing task.
///
/// This function creates a new task that processes SSE events from the Chat
/// Completions API and converts them to `ResponseEvent` instances compatible
/// with Codex's internal event stream format.
///
/// # Arguments
///
/// * `stream_response` - The HTTP response containing the SSE stream
/// * `idle_timeout` - Maximum time to wait for new events before timing out
/// * `telemetry` - Optional telemetry reporter for SSE events
/// * `turn_state` - Optional turn state lock for sticky routing
///
/// # Returns
///
/// A `ResponseStream` that emits `ResponseEvent` instances
pub fn spawn_chat_stream(
    stream_response: StreamResponse,
    idle_timeout: Duration,
    telemetry: Option<Arc<dyn SseTelemetry>>,
    turn_state: Option<Arc<OnceLock<String>>>,
) -> ResponseStream {
    let rate_limit_snapshots = parse_all_rate_limits(&stream_response.headers);
    let models_etag = stream_response
        .headers
        .get("X-Models-Etag")
        .and_then(|v| v.to_str().ok())
        .map(ToString::to_string);

    if let Some(turn_state) = turn_state.as_ref()
        && let Some(header_value) = stream_response
            .headers
            .get("x-codex-turn-state")
            .and_then(|v| v.to_str().ok())
    {
        let _ = turn_state.set(header_value.to_string());
    }

    let (tx_event, rx_event) = mpsc::channel::<Result<ResponseEvent, ApiError>>(1600);
    tokio::spawn(async move {
        for snapshot in rate_limit_snapshots {
            let _ = tx_event.send(Ok(ResponseEvent::RateLimits(snapshot))).await;
        }
        if let Some(etag) = models_etag {
            let _ = tx_event.send(Ok(ResponseEvent::ModelsEtag(etag))).await;
        }
        process_chat_sse(
            stream_response.bytes,
            tx_event,
            idle_timeout,
            telemetry,
            turn_state,
        )
        .await;
    });

    ResponseStream { rx_event }
}

/// Processes SSE events from the Chat Completions API.
///
/// This function reads SSE events from the byte stream and converts them to
/// `ResponseEvent` instances. It handles the Chat Completions streaming format
/// which uses delta updates for content.
///
/// # Arguments
///
/// * `stream` - The byte stream containing SSE data
/// * `tx_event` - Channel sender for emitting events
/// * `idle_timeout` - Maximum time to wait for new events
/// * `telemetry` - Optional telemetry reporter
async fn process_chat_sse(
    stream: ByteStream,
    tx_event: mpsc::Sender<Result<ResponseEvent, ApiError>>,
    idle_timeout: Duration,
    telemetry: Option<Arc<dyn SseTelemetry>>,
    _turn_state: Option<Arc<OnceLock<String>>>,
) {
    let mut stream = stream.eventsource();
    let mut buffer = String::new();
    let mut response_id = String::new();
    let mut tool_calls = ChatToolCallsState::default();

    loop {
        let start = Instant::now();
        let response = timeout(idle_timeout, stream.next()).await;
        if let Some(t) = telemetry.as_ref() {
            t.on_sse_poll(&response, start.elapsed());
        }

        let sse = match response {
            Ok(Some(Ok(sse))) => sse,
            Ok(Some(Err(e))) => {
                debug!("SSE Error: {e:#}");
                let _ = tx_event.send(Err(ApiError::Stream(e.to_string()))).await;
                return;
            }
            Ok(None) => {
                // Stream ended - flush pending content or tool calls, then complete.
                if let Err(err) = emit_pending_output(
                    &tx_event,
                    &mut buffer,
                    &mut tool_calls,
                    response_id.as_str(),
                )
                .await
                {
                    let _ = tx_event.send(Err(err)).await;
                    return;
                }
                emit_completion(&tx_event, &response_id, None).await;
                return;
            }
            Err(_) => {
                let _ = tx_event
                    .send(Err(ApiError::Stream("idle timeout waiting for SSE".into())))
                    .await;
                return;
            }
        };

        trace!("SSE event: {}", &sse.data);

        // Parse Chat Completions chunk
        match parse_chat_chunk(&sse.data) {
            Ok(Some(chunk)) => {
                if !chunk.id.is_empty() {
                    response_id = chunk.id.clone();
                }

                let token_usage = chunk.usage.as_ref().map(|usage| TokenUsage {
                    input_tokens: usage.prompt_tokens as i64,
                    cached_input_tokens: 0,
                    output_tokens: usage.completion_tokens as i64,
                    reasoning_output_tokens: 0,
                    total_tokens: usage.total_tokens as i64,
                });

                // Accumulate streamed content and tool-call deltas. We only emit
                // text when the turn is known to be text-only, so provider
                // pseudo-tool text does not leak into user-visible output.
                for choice in &chunk.choices {
                    if let Some(ref content) = choice.delta.content {
                        buffer.push_str(content);
                    }
                    if let Some(delta_tool_calls) = choice.delta.tool_calls.as_ref() {
                        for delta_tool_call in delta_tool_calls {
                            tool_calls.push_tool_call_delta(delta_tool_call);
                        }
                    }
                    if let Some(legacy_function_call) = choice.delta.function_call.as_ref() {
                        tool_calls.push_legacy_function_call_delta(legacy_function_call);
                    }

                    // Check for completion (finish_reason set)
                    if choice.finish_reason.is_some() {
                        if let Err(err) = emit_pending_output(
                            &tx_event,
                            &mut buffer,
                            &mut tool_calls,
                            response_id.as_str(),
                        )
                        .await
                        {
                            let _ = tx_event.send(Err(err)).await;
                            return;
                        }
                        emit_completion(&tx_event, &response_id, token_usage).await;
                        return;
                    }
                }

                // Some providers emit usage in a final no-op chunk after all deltas.
                if token_usage.is_some() && !chunk.choices.is_empty() {
                    continue;
                }

                if token_usage.is_some() {
                    if let Err(err) = emit_pending_output(
                        &tx_event,
                        &mut buffer,
                        &mut tool_calls,
                        response_id.as_str(),
                    )
                    .await
                    {
                        let _ = tx_event.send(Err(err)).await;
                        return;
                    }
                    emit_completion(&tx_event, &response_id, token_usage).await;
                    return;
                }
            }
            Ok(None) => {
                // Empty or parseable line, continue
            }
            Err(e) => {
                debug!("Failed to parse chat chunk: {e}, data: {}", &sse.data);
                // Continue processing even if one chunk fails
            }
        }
    }
}

/// Emits a completed assistant message event and clears the buffer.
async fn emit_content_message(
    tx_event: &mpsc::Sender<Result<ResponseEvent, ApiError>>,
    buffer: &mut String,
) {
    if !buffer.is_empty() {
        let _ = tx_event
            .send(Ok(ResponseEvent::OutputItemAdded(ResponseItem::Message {
                id: None,
                role: "assistant".to_string(),
                content: vec![],
                end_turn: None,
                phase: None,
            })))
            .await;
        let _ = tx_event
            .send(Ok(ResponseEvent::OutputItemDone(ResponseItem::Message {
                id: None,
                role: "assistant".to_string(),
                content: vec![ContentItem::OutputText {
                    text: buffer.clone(),
                }],
                end_turn: None,
                phase: None,
            })))
            .await;
        buffer.clear();
    }
}

async fn emit_pending_output(
    tx_event: &mpsc::Sender<Result<ResponseEvent, ApiError>>,
    buffer: &mut String,
    tool_calls: &mut ChatToolCallsState,
    response_id: &str,
) -> Result<(), ApiError> {
    if tool_calls.has_pending() {
        // Some providers emit explanatory text content together with real
        // tool_calls. Drop text and treat the turn as a tool-call turn.
        buffer.clear();
        tool_calls.emit_tool_call_items(tx_event, response_id).await
    } else {
        emit_content_message(tx_event, buffer).await;
        Ok(())
    }
}

/// Emits a completion event.
async fn emit_completion(
    tx_event: &mpsc::Sender<Result<ResponseEvent, ApiError>>,
    response_id: &str,
    token_usage: Option<TokenUsage>,
) {
    let _ = tx_event
        .send(Ok(ResponseEvent::Completed {
            response_id: response_id.to_string(),
            token_usage,
            can_append: false, // Chat Completions doesn't support append
        }))
        .await;
}

/// Parses a Chat Completions SSE chunk.
///
/// # Arguments
///
/// * `data` - The SSE data string to parse
///
/// # Returns
///
/// `Some(ChatCompletionChunk)` if parsing succeeds, `None` for empty data
fn parse_chat_chunk(data: &str) -> Result<Option<ChatCompletionChunk>, String> {
    if data.trim().is_empty() || data == "[DONE]" {
        return Ok(None);
    }

    serde_json::from_str::<ChatCompletionChunk>(data)
        .map(Some)
        .map_err(|e| format!("failed to parse chat completion chunk: {e}"))
}

#[derive(Debug, Deserialize, PartialEq)]
struct ChatCompletionChunk {
    id: String,
    object: String,
    created: u64,
    model: String,
    choices: Vec<ChatChoiceDelta>,
    #[serde(default)]
    usage: Option<ChatUsage>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct ChatChoiceDelta {
    index: usize,
    delta: ChatDeltaContent,
    #[serde(default)]
    finish_reason: Option<String>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct ChatDeltaContent {
    #[serde(default)]
    role: Option<String>,
    #[serde(default)]
    content: Option<String>,
    #[serde(default)]
    tool_calls: Option<Vec<ChatToolCallDelta>>,
    #[serde(default)]
    function_call: Option<ChatFunctionCallDelta>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct ChatUsage {
    prompt_tokens: u32,
    completion_tokens: u32,
    total_tokens: u32,
}

#[derive(Debug, Deserialize, PartialEq)]
struct ChatToolCallDelta {
    index: usize,
    #[serde(default)]
    id: Option<String>,
    #[serde(default, rename = "type")]
    kind: Option<String>,
    #[serde(default)]
    function: Option<ChatFunctionCallDelta>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct ChatFunctionCallDelta {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    arguments: Option<String>,
}

#[derive(Debug, Default)]
struct ChatToolCallsState {
    tool_calls: BTreeMap<usize, AccumulatedToolCall>,
    legacy_function_call: Option<AccumulatedToolCall>,
}

impl ChatToolCallsState {
    fn push_tool_call_delta(&mut self, delta: &ChatToolCallDelta) {
        let state = self.tool_calls.entry(delta.index).or_default();
        state.apply(
            delta.id.as_deref(),
            delta.kind.as_deref(),
            delta.function.as_ref(),
        );
    }

    fn push_legacy_function_call_delta(&mut self, delta: &ChatFunctionCallDelta) {
        let state = self
            .legacy_function_call
            .get_or_insert_with(AccumulatedToolCall::default);
        state.apply(None, Some("function"), Some(delta));
    }

    fn has_pending(&self) -> bool {
        !self.tool_calls.is_empty() || self.legacy_function_call.is_some()
    }

    async fn emit_tool_call_items(
        &mut self,
        tx_event: &mpsc::Sender<Result<ResponseEvent, ApiError>>,
        response_id: &str,
    ) -> Result<(), ApiError> {
        if self.tool_calls.is_empty() && self.legacy_function_call.is_none() {
            return Err(ApiError::Stream(
                "chat completion finished with tool_calls but no tool call payload was received"
                    .to_string(),
            ));
        }

        for (index, state) in &self.tool_calls {
            let kind = state.kind.as_deref().unwrap_or("function");
            if kind != "function" {
                return Err(ApiError::Stream(format!(
                    "chat completion emitted unsupported tool call type `{kind}`"
                )));
            }

            let name = state.name.clone().ok_or_else(|| {
                ApiError::Stream("chat completion tool call is missing function name".to_string())
            })?;
            let call_id = state.call_id.clone().unwrap_or_else(|| {
                if response_id.is_empty() {
                    format!("chat-tool-call-{index}")
                } else {
                    format!("{response_id}-tool-{index}")
                }
            });
            let arguments = if state.arguments.is_empty() {
                "{}".to_string()
            } else {
                state.arguments.clone()
            };
            tx_event
                .send(Ok(ResponseEvent::OutputItemDone(
                    ResponseItem::FunctionCall {
                        id: None,
                        name,
                        arguments,
                        call_id,
                    },
                )))
                .await
                .map_err(|_| {
                    ApiError::Stream("chat fallback event channel is closed".to_string())
                })?;
        }

        if let Some(state) = self.legacy_function_call.as_ref() {
            let name = state.name.clone().ok_or_else(|| {
                ApiError::Stream(
                    "legacy function_call payload is missing function name".to_string(),
                )
            })?;
            let call_id = state.call_id.clone().unwrap_or_else(|| {
                if response_id.is_empty() {
                    "chat-tool-call-legacy-0".to_string()
                } else {
                    format!("{response_id}-tool-legacy-0")
                }
            });
            let arguments = if state.arguments.is_empty() {
                "{}".to_string()
            } else {
                state.arguments.clone()
            };
            tx_event
                .send(Ok(ResponseEvent::OutputItemDone(
                    ResponseItem::FunctionCall {
                        id: None,
                        name,
                        arguments,
                        call_id,
                    },
                )))
                .await
                .map_err(|_| {
                    ApiError::Stream("chat fallback event channel is closed".to_string())
                })?;
        }

        self.tool_calls.clear();
        self.legacy_function_call = None;
        Ok(())
    }
}

#[derive(Debug, Default)]
struct AccumulatedToolCall {
    call_id: Option<String>,
    kind: Option<String>,
    name: Option<String>,
    arguments: String,
}

impl AccumulatedToolCall {
    fn apply(
        &mut self,
        call_id: Option<&str>,
        kind: Option<&str>,
        function: Option<&ChatFunctionCallDelta>,
    ) {
        if self.call_id.is_none()
            && let Some(call_id) = call_id
        {
            self.call_id = Some(call_id.to_string());
        }
        if self.kind.is_none()
            && let Some(kind) = kind
        {
            self.kind = Some(kind.to_string());
        }
        if let Some(function) = function {
            if self.name.is_none()
                && let Some(name) = function.name.as_deref()
            {
                self.name = Some(name.to_string());
            }
            if let Some(arguments) = function.arguments.as_deref() {
                self.arguments.push_str(arguments);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_client::TransportError;
    use futures::TryStreamExt;
    use pretty_assertions::assert_eq;
    use std::io::Cursor;
    use tokio_util::io::ReaderStream;

    async fn collect_chat_events(chunks: &[&[u8]]) -> Vec<Result<ResponseEvent, ApiError>> {
        let mut body = String::new();
        for chunk in chunks {
            let s = std::str::from_utf8(chunk).unwrap();
            body.push_str(s);
        }

        let stream = ReaderStream::new(Cursor::new(body))
            .map_err(|err: std::io::Error| TransportError::Network(err.to_string()));
        let (tx, mut rx) = mpsc::channel::<Result<ResponseEvent, ApiError>>(16);
        tokio::spawn(process_chat_sse(
            Box::pin(stream),
            tx,
            Duration::from_millis(1000),
            None,
            None,
        ));

        let mut events = Vec::new();
        while let Some(ev) = rx.recv().await {
            events.push(ev);
        }
        events
    }

    #[tokio::test]
    async fn test_chat_sse_basic_stream() {
        let chunk1 = b"data: {\"id\":\"chatcmpl-123\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"Hello\"},\"finish_reason\":null}]}\n\n";
        let chunk2 = b"data: {\"id\":\"chatcmpl-123\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\" world\"},\"finish_reason\":null}]}\n\n";
        let chunk3 = b"data: {\"id\":\"chatcmpl-123\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n";
        let chunk4 = b"data: [DONE]\n\n";

        let events = collect_chat_events(&[chunk1, chunk2, chunk3, chunk4]).await;

        let message = events.iter().find_map(|event| match event {
            Ok(ResponseEvent::OutputItemDone(ResponseItem::Message { content, .. })) => {
                content.iter().find_map(|item| match item {
                    ContentItem::OutputText { text } => Some(text.clone()),
                    _ => None,
                })
            }
            _ => None,
        });
        assert_eq!(message, Some("Hello world".to_string()));
    }

    #[tokio::test]
    async fn test_chat_sse_with_usage() {
        let chunk1 = b"data: {\"id\":\"chatcmpl-456\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"Hi\"},\"finish_reason\":null}]}\n\n";
        let chunk2 = b"data: {\"id\":\"chatcmpl-456\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5,\"total_tokens\":15}}\n\n";

        let events = collect_chat_events(&[chunk1, chunk2]).await;

        // Should have completion with token usage
        let has_completion = events.iter().any(|e| {
            matches!(e, Ok(ResponseEvent::Completed { token_usage: Some(usage), .. }) if usage.input_tokens == 10 && usage.output_tokens == 5)
        });
        assert!(has_completion);
    }

    #[test]
    fn test_parse_chat_chunk_valid() {
        let data = r#"{"id":"test","object":"chat.completion.chunk","created":1234567890,"model":"gpt-4","choices":[{"index":0,"delta":{"content":"test"}}]}"#;
        let result = parse_chat_chunk(data);
        assert!(result.is_ok());
        assert!(result.unwrap().is_some());
    }

    #[test]
    fn test_parse_chat_chunk_empty() {
        assert_eq!(parse_chat_chunk(""), Ok(None));
        assert_eq!(parse_chat_chunk("[DONE]"), Ok(None));
    }

    #[test]
    fn test_parse_chat_chunk_invalid() {
        let data = r#"{"invalid json"#;
        let result = parse_chat_chunk(data);
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_chat_sse_tool_calls_stream() {
        let chunk1 = b"data: {\"id\":\"chatcmpl-tools\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"exec\",\"arguments\":\"{\\\"cmd\\\":\\\"\"}}]},\"finish_reason\":null}]}\n\n";
        let chunk2 = b"data: {\"id\":\"chatcmpl-tools\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"ls\\\"}\"}}]},\"finish_reason\":null}]}\n\n";
        let chunk3 = b"data: {\"id\":\"chatcmpl-tools\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n";
        let chunk4 = b"data: [DONE]\n\n";

        let events = collect_chat_events(&[chunk1, chunk2, chunk3, chunk4]).await;

        let function_call = events.iter().find_map(|event| match event {
            Ok(ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
                name,
                arguments,
                call_id,
                ..
            })) => Some((name.clone(), arguments.clone(), call_id.clone())),
            _ => None,
        });
        assert_eq!(
            function_call,
            Some((
                "exec".to_string(),
                "{\"cmd\":\"ls\"}".to_string(),
                "call_1".to_string(),
            ))
        );

        let completed_response_id = events.iter().find_map(|event| match event {
            Ok(ResponseEvent::Completed { response_id, .. }) => Some(response_id.clone()),
            _ => None,
        });
        assert_eq!(completed_response_id, Some("chatcmpl-tools".to_string()));
    }

    #[tokio::test]
    async fn test_chat_sse_tool_calls_with_stop_finish_reason_prefer_tool_call() {
        let chunk1 = b"data: {\"id\":\"chatcmpl-tools-stop\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"{\\\"command\\\":\\\"ls -la\\\"}\",\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"exec\",\"arguments\":\"{\\\"cmd\\\":\\\"\"}}]},\"finish_reason\":null}]}\n\n";
        let chunk2 = b"data: {\"id\":\"chatcmpl-tools-stop\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"ls -la\\\"}\"}}]},\"finish_reason\":\"stop\"}]}\n\n";

        let events = collect_chat_events(&[chunk1, chunk2]).await;

        let function_call = events.iter().find_map(|event| match event {
            Ok(ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
                name,
                arguments,
                call_id,
                ..
            })) => Some((name.clone(), arguments.clone(), call_id.clone())),
            _ => None,
        });
        assert_eq!(
            function_call,
            Some((
                "exec".to_string(),
                "{\"cmd\":\"ls -la\"}".to_string(),
                "call_1".to_string(),
            ))
        );

        let emitted_text = events.iter().any(|event| {
            matches!(
                event,
                Ok(ResponseEvent::OutputItemDone(ResponseItem::Message { .. }))
                    | Ok(ResponseEvent::OutputTextDelta(_))
            )
        });
        assert!(!emitted_text);
    }

    #[tokio::test]
    async fn test_chat_sse_legacy_function_call_stream() {
        let chunk1 = b"data: {\"id\":\"chatcmpl-legacy\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"function_call\":{\"name\":\"exec\",\"arguments\":\"{\\\"cmd\\\":\\\"echo\"}},\"finish_reason\":null}]}\n\n";
        let chunk2 = b"data: {\"id\":\"chatcmpl-legacy\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{\"function_call\":{\"arguments\":\" hi\\\"}\"}},\"finish_reason\":null}]}\n\n";
        let chunk3 = b"data: {\"id\":\"chatcmpl-legacy\",\"object\":\"chat.completion.chunk\",\"created\":1699000000,\"model\":\"gpt-4\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"function_call\"}]}\n\n";

        let events = collect_chat_events(&[chunk1, chunk2, chunk3]).await;

        let function_call = events.iter().find_map(|event| match event {
            Ok(ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
                name,
                arguments,
                call_id,
                ..
            })) => Some((name.clone(), arguments.clone(), call_id.clone())),
            _ => None,
        });
        assert_eq!(
            function_call,
            Some((
                "exec".to_string(),
                "{\"cmd\":\"echo hi\"}".to_string(),
                "chatcmpl-legacy-tool-legacy-0".to_string(),
            ))
        );
    }
}
