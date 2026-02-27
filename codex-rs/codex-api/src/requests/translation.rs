//! Translation between Responses API and Chat Completions API formats.
//!
//! This module provides functions to convert between the OpenAI Responses API
//! format and the legacy Chat Completions API format, enabling fallback support.

use crate::common::ResponsesApiRequest;
use crate::endpoint::chat_completions::ChatCompletionsRequest;
use crate::endpoint::chat_completions::ChatCompletionsTool;
use crate::endpoint::chat_completions::ChatMessage;
use crate::endpoint::chat_completions::ChatToolCall;
use crate::endpoint::chat_completions::ChatToolFunction;
use crate::endpoint::chat_completions::ChatToolFunctionCall;
use codex_protocol::models::ContentItem;
use codex_protocol::models::FunctionCallOutputPayload;
use codex_protocol::models::ResponseItem;
use serde_json::Value;
use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChatTranslationError {
    InvalidFunctionTool(String),
}

impl fmt::Display for ChatTranslationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ChatTranslationError::InvalidFunctionTool(message) => {
                write!(f, "invalid function tool in chat fallback: {message}")
            }
        }
    }
}

impl std::error::Error for ChatTranslationError {}

/// Translates a Responses API request to a Chat Completions request.
///
/// This function converts the Responses API format (which uses `instructions`
/// and `input` fields) to the Chat Completions format (which uses `messages`).
///
/// # Translation Rules
///
/// - The `instructions` field becomes a `system` message (or is omitted if empty)
/// - Each `input` item is converted to a message based on its type:
///   - `user` role items become `user` messages
///   - `assistant` role items become `assistant` messages
///   - `developer` role items are mapped to `system` messages
///   - `function_call` items become assistant `tool_calls`
///   - `function_call_output` items become `tool` messages
/// - Only `function` tools are translated for chat fallback mode
/// - Non-function tools are ignored
/// - Streaming is always enabled
/// - Reasoning parameters are not passed to the chat API
///
/// # Arguments
///
/// * `responses_req` - The Responses API request to translate
///
/// # Returns
///
/// A `ChatCompletionsRequest` that can be sent to the Chat Completions API
pub fn responses_to_chat_request(
    responses_req: &ResponsesApiRequest,
) -> Result<ChatCompletionsRequest, ChatTranslationError> {
    let mut messages = Vec::new();

    // Convert instructions to a system message if present and non-empty
    if !responses_req.instructions.is_empty() {
        messages.push(ChatMessage {
            role: "system".to_string(),
            content: Some(responses_req.instructions.clone()),
            tool_calls: None,
            tool_call_id: None,
        });
    }

    // Convert input items to messages
    messages.extend(input_to_messages(&responses_req.input));
    let tools = translate_tools(&responses_req.tools)?;
    let include_tool_controls = !tools.is_empty();

    Ok(ChatCompletionsRequest {
        model: responses_req.model.clone(),
        messages,
        stream: true,
        temperature: None, // Use model defaults
        max_tokens: None,  // Use model defaults
        stop: None,        // No custom stop sequences
        tools: include_tool_controls.then_some(tools),
        tool_choice: include_tool_controls
            .then(|| Value::String(responses_req.tool_choice.clone())),
        parallel_tool_calls: include_tool_controls.then_some(responses_req.parallel_tool_calls),
    })
}

/// Converts ResponseItems to ChatMessages.
///
/// This function processes the input items and converts them to the appropriate
/// message format for the Chat Completions API.
///
/// # Arguments
///
/// * `input` - The input items to convert
///
/// # Returns
///
/// A vector of `ChatMessage` instances
fn input_to_messages(input: &[ResponseItem]) -> Vec<ChatMessage> {
    input.iter().filter_map(response_item_to_message).collect()
}

/// Converts a single ResponseItem to a ChatMessage.
///
/// # Arguments
///
/// * `item` - The response item to convert
///
/// # Returns
///
/// `Some(ChatMessage)` if the item can be converted, `None` otherwise
fn response_item_to_message(item: &ResponseItem) -> Option<ChatMessage> {
    match item {
        ResponseItem::Message { role, content, .. } => {
            // Extract text content from the content array
            let text_content = extract_text_content(content)?;
            // Map developer role to system for Chat API compatibility
            let mapped_role = if role == "developer" {
                "system".to_string()
            } else {
                role.clone()
            };
            Some(ChatMessage {
                role: mapped_role,
                content: Some(text_content),
                tool_calls: None,
                tool_call_id: None,
            })
        }
        ResponseItem::FunctionCall {
            name,
            arguments,
            call_id,
            ..
        } => Some(ChatMessage {
            role: "assistant".to_string(),
            content: None,
            tool_calls: Some(vec![ChatToolCall {
                id: Some(call_id.clone()),
                kind: Some("function".to_string()),
                function: ChatToolFunctionCall {
                    name: Some(name.clone()),
                    arguments: Some(arguments.clone()),
                },
            }]),
            tool_call_id: None,
        }),
        ResponseItem::FunctionCallOutput { call_id, output } => Some(ChatMessage {
            role: "tool".to_string(),
            content: Some(function_call_output_to_text(output)),
            tool_calls: None,
            tool_call_id: Some(call_id.clone()),
        }),
        // For other item types, we skip them in MVP
        _ => None,
    }
}

/// Extracts text content from a ResponseItem's content array.
///
/// # Arguments
///
/// * `content` - The content array from a ResponseItem
///
/// # Returns
///
/// The concatenated text content, or `None` if no text content is found
fn extract_text_content(content: &[ContentItem]) -> Option<String> {
    let mut text_parts = Vec::new();
    for item in content {
        match item {
            ContentItem::InputText { text } => text_parts.push(text.clone()),
            ContentItem::OutputText { text } => text_parts.push(text.clone()),
            ContentItem::InputImage { .. } => {} // Skip images in chat format
        }
    }
    if text_parts.is_empty() {
        None
    } else {
        Some(text_parts.join("\n"))
    }
}

fn function_call_output_to_text(output: &FunctionCallOutputPayload) -> String {
    output.body.to_text().unwrap_or_default()
}

fn translate_tools(tools: &[Value]) -> Result<Vec<ChatCompletionsTool>, ChatTranslationError> {
    let mut translated = Vec::with_capacity(tools.len());
    for tool in tools {
        let tool_type = tool.get("type").and_then(Value::as_str).ok_or_else(|| {
            ChatTranslationError::InvalidFunctionTool("missing `type`".to_string())
        })?;
        if tool_type != "function" {
            continue;
        }

        let name = tool.get("name").and_then(Value::as_str).ok_or_else(|| {
            ChatTranslationError::InvalidFunctionTool("missing `name`".to_string())
        })?;
        let parameters = tool.get("parameters").cloned().ok_or_else(|| {
            ChatTranslationError::InvalidFunctionTool("missing `parameters`".to_string())
        })?;
        let description = tool
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let strict = tool.get("strict").and_then(Value::as_bool);

        translated.push(ChatCompletionsTool {
            kind: "function".to_string(),
            function: ChatToolFunction {
                name: name.to_string(),
                description,
                parameters,
                strict,
            },
        });
    }
    Ok(translated)
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_protocol::models::ContentItem;
    use codex_protocol::models::FunctionCallOutputPayload;
    use codex_protocol::models::ResponseInputItem;
    use codex_protocol::models::ResponseItem;
    use pretty_assertions::assert_eq;
    use serde_json::json;

    #[test]
    fn test_responses_to_chat_basic() {
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: "You are a helpful assistant.".to_string(),
            input: vec![],
            tools: vec![],
            tool_choice: "auto".to_string(),
            parallel_tool_calls: false,
            reasoning: None,
            store: false,
            stream: true,
            include: vec![],
            prompt_cache_key: None,
            text: None,
        };

        let chat_req = responses_to_chat_request(&responses_req).expect("translation should work");
        assert_eq!(chat_req.model, "gpt-4");
        assert_eq!(chat_req.messages.len(), 1);
        assert_eq!(chat_req.messages[0].role, "system");
        assert_eq!(
            chat_req.messages[0].content,
            Some("You are a helpful assistant.".to_string())
        );
        assert!(chat_req.stream);
        assert!(chat_req.tools.is_none());
        assert!(chat_req.tool_choice.is_none());
        assert!(chat_req.parallel_tool_calls.is_none());
    }

    #[test]
    fn test_responses_to_chat_with_user_input() {
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: "You are a helpful assistant.".to_string(),
            input: vec![ResponseItem::Message {
                id: None,
                role: "user".to_string(),
                content: vec![ContentItem::InputText {
                    text: "Hello!".to_string(),
                }],
                end_turn: None,
                phase: None,
            }],
            tools: vec![],
            tool_choice: "auto".to_string(),
            parallel_tool_calls: false,
            reasoning: None,
            store: false,
            stream: true,
            include: vec![],
            prompt_cache_key: None,
            text: None,
        };

        let chat_req = responses_to_chat_request(&responses_req).expect("translation should work");
        assert_eq!(chat_req.messages.len(), 2);
        assert_eq!(chat_req.messages[0].role, "system");
        assert_eq!(chat_req.messages[1].role, "user");
        assert_eq!(chat_req.messages[1].content, Some("Hello!".to_string()));
    }

    #[test]
    fn test_developer_role_mapped_to_system() {
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: String::new(),
            input: vec![ResponseItem::Message {
                id: None,
                role: "developer".to_string(),
                content: vec![ContentItem::InputText {
                    text: "Special instructions".to_string(),
                }],
                end_turn: None,
                phase: None,
            }],
            tools: vec![],
            tool_choice: "auto".to_string(),
            parallel_tool_calls: false,
            reasoning: None,
            store: false,
            stream: true,
            include: vec![],
            prompt_cache_key: None,
            text: None,
        };

        let chat_req = responses_to_chat_request(&responses_req).expect("translation should work");
        assert_eq!(chat_req.messages.len(), 1);
        assert_eq!(chat_req.messages[0].role, "system");
        assert_eq!(
            chat_req.messages[0].content,
            Some("Special instructions".to_string())
        );
    }

    #[test]
    fn test_extract_text_content() {
        let content = vec![
            ContentItem::InputText {
                text: "Hello".to_string(),
            },
            ContentItem::OutputText {
                text: "World".to_string(),
            },
        ];
        let text = extract_text_content(&content);
        assert_eq!(text, Some("Hello\nWorld".to_string()));
    }

    #[test]
    fn test_extract_text_content_empty() {
        let content = vec![ContentItem::InputImage {
            image_url: "http://example.com".to_string(),
        }];
        let text = extract_text_content(&content);
        assert_eq!(text, None);
    }

    #[test]
    fn translates_function_tools_and_tool_controls() {
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: String::new(),
            input: vec![],
            tools: vec![json!({
                "type": "function",
                "name": "echo",
                "description": "Echo text",
                "strict": false,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"}
                    },
                    "required": ["value"]
                }
            })],
            tool_choice: "auto".to_string(),
            parallel_tool_calls: true,
            reasoning: None,
            store: false,
            stream: true,
            include: vec![],
            prompt_cache_key: None,
            text: None,
        };

        let chat_req = responses_to_chat_request(&responses_req).expect("translation should work");
        assert_eq!(chat_req.tools.as_ref().map(Vec::len), Some(1));
        assert_eq!(chat_req.tool_choice, Some(json!("auto")));
        assert_eq!(chat_req.parallel_tool_calls, Some(true));
    }

    #[test]
    fn ignores_non_function_tools_in_chat_fallback() {
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: String::new(),
            input: vec![],
            tools: vec![json!({
                "type": "web_search"
            })],
            tool_choice: "auto".to_string(),
            parallel_tool_calls: true,
            reasoning: None,
            store: false,
            stream: true,
            include: vec![],
            prompt_cache_key: None,
            text: None,
        };

        let chat_req = responses_to_chat_request(&responses_req).expect("translation should work");
        assert!(chat_req.tools.is_none());
        assert!(chat_req.tool_choice.is_none());
        assert!(chat_req.parallel_tool_calls.is_none());
    }

    #[test]
    fn keeps_function_tools_and_ignores_non_function_tools() {
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: String::new(),
            input: vec![],
            tools: vec![
                json!({
                    "type": "web_search"
                }),
                json!({
                    "type": "function",
                    "name": "echo",
                    "description": "Echo text",
                    "strict": false,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"}
                        },
                        "required": ["value"]
                    }
                }),
            ],
            tool_choice: "auto".to_string(),
            parallel_tool_calls: true,
            reasoning: None,
            store: false,
            stream: true,
            include: vec![],
            prompt_cache_key: None,
            text: None,
        };

        let chat_req = responses_to_chat_request(&responses_req).expect("translation should work");
        assert_eq!(chat_req.tools.as_ref().map(Vec::len), Some(1));
        assert_eq!(chat_req.tool_choice, Some(json!("auto")));
        assert_eq!(chat_req.parallel_tool_calls, Some(true));
    }

    #[test]
    fn translates_function_call_history_items() {
        let output_payload = FunctionCallOutputPayload::from_text("done".to_string());
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: String::new(),
            input: vec![
                ResponseItem::FunctionCall {
                    id: None,
                    name: "echo".to_string(),
                    arguments: "{\"value\":\"hello\"}".to_string(),
                    call_id: "call-1".to_string(),
                },
                ResponseItem::from(ResponseInputItem::FunctionCallOutput {
                    call_id: "call-1".to_string(),
                    output: output_payload,
                }),
            ],
            tools: vec![],
            tool_choice: "auto".to_string(),
            parallel_tool_calls: false,
            reasoning: None,
            store: false,
            stream: true,
            include: vec![],
            prompt_cache_key: None,
            text: None,
        };

        let chat_req = responses_to_chat_request(&responses_req).expect("translation should work");
        assert_eq!(chat_req.messages.len(), 2);
        assert_eq!(chat_req.messages[0].role, "assistant");
        assert_eq!(
            chat_req.messages[0]
                .tool_calls
                .as_ref()
                .and_then(|calls| calls.first())
                .and_then(|call| call.function.name.as_deref()),
            Some("echo")
        );
        assert_eq!(chat_req.messages[1].role, "tool");
        assert_eq!(chat_req.messages[1].tool_call_id.as_deref(), Some("call-1"));
        assert_eq!(chat_req.messages[1].content.as_deref(), Some("done"));
    }
}
