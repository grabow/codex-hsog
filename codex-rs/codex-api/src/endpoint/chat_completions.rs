//! Chat Completions API client for fallback support.
//!
//! This module provides a client for the legacy OpenAI Chat Completions API
//! (`/v1/chat/completions`), used as a fallback when the Responses API
//! returns a 404 error.

use crate::auth::AuthProvider;
use crate::common::ResponseStream;
use crate::endpoint::session::EndpointSession;
use crate::error::ApiError;
use crate::provider::Provider;
use crate::requests::headers::build_conversation_headers;
use crate::requests::headers::insert_header;
use crate::requests::headers::subagent_header;
use crate::sse::chat::spawn_chat_stream;
use crate::telemetry::SseTelemetry;
use codex_client::HttpTransport;
use codex_client::RequestTelemetry;
use codex_protocol::protocol::SessionSource;
use http::HeaderMap;
use http::HeaderValue;
use http::Method;
use serde::Deserialize;
use serde::Serialize;
use serde_json::Value;
use std::sync::Arc;
use std::sync::OnceLock;

/// Chat Completions API client.
///
/// This client is used as a fallback when the Responses API returns a 404 error,
/// allowing compatibility with providers that only support the legacy Chat Completions API.
pub struct ChatCompletionsClient<T: HttpTransport, A: AuthProvider> {
    session: EndpointSession<T, A>,
    sse_telemetry: Option<Arc<dyn SseTelemetry>>,
    /// Custom path for the Chat Completions endpoint.
    /// Defaults to "/v1/chat/completions" if not specified.
    chat_path: String,
}

#[derive(Default)]
pub struct ChatCompletionsOptions {
    pub conversation_id: Option<String>,
    pub session_source: Option<SessionSource>,
    pub extra_headers: HeaderMap,
    pub turn_state: Option<Arc<OnceLock<String>>>,
}

impl<T: HttpTransport, A: AuthProvider> ChatCompletionsClient<T, A> {
    /// Creates a new ChatCompletionsClient.
    ///
    /// # Arguments
    ///
    /// * `transport` - HTTP transport implementation
    /// * `provider` - API provider configuration
    /// * `auth` - Authentication provider
    /// * `chat_path` - Optional custom path for the Chat Completions endpoint
    pub fn new(
        transport: T,
        provider: Provider,
        auth: A,
        chat_path: Option<String>,
    ) -> Self {
        Self {
            session: EndpointSession::new(transport, provider, auth),
            sse_telemetry: None,
            chat_path: chat_path.unwrap_or_else(|| "/v1/chat/completions".to_string()),
        }
    }

    /// Sets telemetry for this client.
    pub fn with_telemetry(
        self,
        request: Option<Arc<dyn RequestTelemetry>>,
        sse: Option<Arc<dyn SseTelemetry>>,
    ) -> Self {
        Self {
            session: self.session.with_request_telemetry(request),
            sse_telemetry: sse,
            chat_path: self.chat_path,
        }
    }

    /// Streams a chat completions request.
    ///
    /// # Arguments
    ///
    /// * `request` - Chat completions request payload
    /// * `options` - Request options including conversation tracking
    pub async fn stream_request(
        &self,
        request: ChatCompletionsRequest,
        options: ChatCompletionsOptions,
    ) -> Result<ResponseStream, ApiError> {
        let ChatCompletionsOptions {
            conversation_id,
            session_source,
            extra_headers,
            turn_state,
        } = options;

        let body = serde_json::to_value(&request)
            .map_err(|e| ApiError::Stream(format!("failed to encode chat request: {e}")))?;

        let mut headers = extra_headers;
        headers.extend(build_conversation_headers(conversation_id));
        if let Some(subagent) = subagent_header(&session_source) {
            insert_header(&mut headers, "x-openai-subagent", &subagent);
        }

        self.stream(body, headers, turn_state).await
    }

    async fn stream(
        &self,
        body: Value,
        extra_headers: HeaderMap,
        turn_state: Option<Arc<OnceLock<String>>>,
    ) -> Result<ResponseStream, ApiError> {
        // Remove leading slash from path if present (stream_with expects no leading slash)
        let path = self.chat_path.strip_prefix('/').unwrap_or(&self.chat_path);

        let stream_response = self
            .session
            .stream_with(
                Method::POST,
                path,
                extra_headers,
                Some(body),
                |req| {
                    req.headers.insert(
                        http::header::ACCEPT,
                        HeaderValue::from_static("text/event-stream"),
                    );
                },
            )
            .await?;

        Ok(spawn_chat_stream(
            stream_response,
            self.session.provider().stream_idle_timeout,
            self.sse_telemetry.clone(),
            turn_state,
        ))
    }
}

/// Chat Completions API request payload.
///
/// This represents the standard OpenAI Chat Completions API request format.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatCompletionsRequest {
    /// The model to use for completion.
    pub model: String,

    /// The messages to send to the model.
    pub messages: Vec<ChatMessage>,

    /// Whether to stream the response.
    #[serde(default = "default_stream")]
    pub stream: bool,

    /// Sampling temperature.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temperature: Option<f32>,

    /// Maximum tokens to generate.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<u32>,

    /// Stop sequences.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stop: Option<Vec<String>>,
}

const fn default_stream() -> bool {
    true
}

/// A message in the chat completion request.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    /// The role of the message author.
    pub role: String,

    /// The content of the message.
    pub content: String,
}

/// Chat Completions API response (non-streaming).
#[derive(Debug, Clone, Deserialize)]
pub struct ChatCompletionResponse {
    /// The unique identifier for the completion.
    pub id: String,

    /// The object type, always "chat.completion".
    pub object: String,

    /// The Unix timestamp (in seconds) of when the completion was created.
    pub created: u64,

    /// The model used for the completion.
    pub model: String,

    /// The list of completion choices.
    pub choices: Vec<ChatChoice>,

    /// Usage information for the completion.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<ChatUsage>,
}

/// A choice in the chat completion response.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatChoice {
    /// The index of the choice in the list.
    pub index: usize,

    /// The message generated by the model.
    pub message: ChatMessage,

    /// The reason the completion stopped.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
}

/// Token usage information for the completion.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatUsage {
    /// Number of tokens in the prompt.
    pub prompt_tokens: u32,

    /// Number of tokens in the completion.
    pub completion_tokens: u32,

    /// Total number of tokens used.
    pub total_tokens: u32,
}

/// Chat Completions streaming chunk.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatCompletionChunk {
    /// The unique identifier for the completion.
    pub id: String,

    /// The object type, always "chat.completion.chunk".
    pub object: String,

    /// The Unix timestamp (in seconds) of when the chunk was created.
    pub created: u64,

    /// The model used for the completion.
    pub model: String,

    /// The list of completion choices in this chunk.
    pub choices: Vec<ChatChoiceDelta>,

    /// Usage information (only in the final chunk).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub usage: Option<ChatUsage>,
}

/// A choice delta in a streaming chunk.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatChoiceDelta {
    /// The index of the choice in the list.
    pub index: usize,

    /// The delta content for this choice.
    pub delta: ChatDelta,

    /// The reason the completion stopped (only in final chunk).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
}

/// The delta content for a streaming choice.
#[derive(Debug, Clone, Deserialize)]
pub struct ChatDelta {
    /// The role of the message author (only in first chunk).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,

    /// The content delta.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_chat_completions_request_serialization() {
        let request = ChatCompletionsRequest {
            model: "gpt-4".to_string(),
            messages: vec![
                ChatMessage {
                    role: "system".to_string(),
                    content: "You are a helpful assistant.".to_string(),
                },
                ChatMessage {
                    role: "user".to_string(),
                    content: "Hello!".to_string(),
                },
            ],
            stream: true,
            temperature: Some(0.7),
            max_tokens: Some(100),
            stop: None,
        };

        let json = serde_json::to_string(&request).unwrap();
        assert!(json.contains("\"model\":\"gpt-4\""));
        assert!(json.contains("\"role\":\"system\""));
        assert!(json.contains("\"stream\":true"));
        assert!(json.contains("\"temperature\":0.7"));
    }
}
