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
        process_chat_sse(stream_response.bytes, tx_event, idle_timeout, telemetry, turn_state).await;
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
    turn_state: Option<Arc<OnceLock<String>>>,
) {
    let mut stream = stream.eventsource();
    let mut buffer = String::new();
    let mut response_id = String::new();
    let mut finished = false;
    let mut item_added = false; // Track if OutputItemAdded was emitted

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
                // Stream ended - emit completion if not already done
                if !finished && !buffer.is_empty() {
                    emit_content_delta(&tx_event, &mut buffer).await;
                    emit_completion(&tx_event, &response_id, None).await;
                } else if !finished {
                    emit_completion(&tx_event, &response_id, None).await;
                }
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
                // Check for usage information first (might come in final chunk)
                if let Some(ref usage) = chunk.usage {
                    let token_usage = TokenUsage {
                        input_tokens: usage.prompt_tokens as i64,
                        cached_input_tokens: 0,
                        output_tokens: usage.completion_tokens as i64,
                        reasoning_output_tokens: 0,
                        total_tokens: usage.total_tokens as i64,
                    };
                    // Emit completion with usage if we have content
                    if item_added {
                        emit_content_delta(&tx_event, &mut buffer).await;
                    }
                    emit_completion(&tx_event, &chunk.id, Some(token_usage)).await;
                    return;
                }

                // Extract content deltas and emit them
                for choice in &chunk.choices {
                    if let Some(ref content) = choice.delta.content {
                        buffer.push_str(content);

                        // First time we receive content, emit OutputItemAdded to create the active item
                        if !item_added {
                            let _ = tx_event
                                .send(Ok(ResponseEvent::OutputItemAdded(ResponseItem::Message {
                                    id: None,
                                    role: "assistant".to_string(),
                                    content: vec![],
                                    end_turn: None,
                                    phase: None,
                                })))
                                .await;
                            item_added = true;
                        }

                        // Emit as OutputTextDelta for real-time display
                        if tx_event
                            .send(Ok(ResponseEvent::OutputTextDelta(content.to_string())))
                            .await
                            .is_err()
                        {
                            return;
                        }
                    }

                    // Check for completion (finish_reason set)
                    if choice.finish_reason.is_some() {
                        // Emit completion when finish_reason is received
                        if item_added {
                            emit_content_delta(&tx_event, &mut buffer).await;
                        }
                        emit_completion(&tx_event, &chunk.id, None).await;
                        finished = true;
                        return;
                    }
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

/// Emits a content delta event and clears the buffer.
async fn emit_content_delta(
    tx_event: &mpsc::Sender<Result<ResponseEvent, ApiError>>,
    buffer: &mut String,
) {
    if !buffer.is_empty() {
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
}

#[derive(Debug, Deserialize, PartialEq)]
struct ChatUsage {
    prompt_tokens: u32,
    completion_tokens: u32,
    total_tokens: u32,
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_client::TransportError;
    use futures::TryStreamExt;
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

        // Should have at least text deltas and completion
        assert!(events.len() >= 2);

        // Check for text delta
        let has_hello = events.iter().any(|e| {
            matches!(e, Ok(ResponseEvent::OutputTextDelta(text)) if text.contains("Hello"))
        });
        assert!(has_hello);
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
}
