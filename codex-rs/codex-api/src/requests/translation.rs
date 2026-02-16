//! Translation between Responses API and Chat Completions API formats.
//!
//! This module provides functions to convert between the OpenAI Responses API
//! format and the legacy Chat Completions API format, enabling fallback support.

use crate::common::ResponsesApiRequest;
use crate::endpoint::chat_completions::{ChatCompletionsRequest, ChatMessage};
use codex_protocol::models::ContentItem;
use codex_protocol::models::ResponseItem;

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
/// - Tools are disabled in fallback mode (MVP limitation)
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
pub fn responses_to_chat_request(responses_req: &ResponsesApiRequest) -> ChatCompletionsRequest {
    let mut messages = Vec::new();

    // Convert instructions to a system message if present and non-empty
    if !responses_req.instructions.is_empty() {
        messages.push(ChatMessage {
            role: "system".to_string(),
            content: responses_req.instructions.clone(),
        });
    }

    // Convert input items to messages
    messages.extend(input_to_messages(&responses_req.input));

    ChatCompletionsRequest {
        model: responses_req.model.clone(),
        messages,
        stream: true,
        temperature: None,    // Use model defaults
        max_tokens: None,     // Use model defaults
        stop: None,           // No custom stop sequences
    }
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
    input
        .iter()
        .filter_map(|item| response_item_to_message(item))
        .collect()
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
                content: text_content,
            })
        }
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

#[cfg(test)]
mod tests {
    use super::*;
    use codex_protocol::models::ContentItem;
    use codex_protocol::models::ResponseItem;

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

        let chat_req = responses_to_chat_request(&responses_req);
        assert_eq!(chat_req.model, "gpt-4");
        assert_eq!(chat_req.messages.len(), 1);
        assert_eq!(chat_req.messages[0].role, "system");
        assert_eq!(chat_req.messages[0].content, "You are a helpful assistant.");
        assert!(chat_req.stream);
    }

    #[test]
    fn test_responses_to_chat_with_user_input() {
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: "You are a helpful assistant.".to_string(),
            input: vec![ResponseItem::Message {
                id: None,
                role: "user".to_string(),
                content: vec![ContentItem::InputText { text: "Hello!".to_string() }],
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

        let chat_req = responses_to_chat_request(&responses_req);
        assert_eq!(chat_req.messages.len(), 2);
        assert_eq!(chat_req.messages[0].role, "system");
        assert_eq!(chat_req.messages[1].role, "user");
        assert_eq!(chat_req.messages[1].content, "Hello!");
    }

    #[test]
    fn test_developer_role_mapped_to_system() {
        let responses_req = ResponsesApiRequest {
            model: "gpt-4".to_string(),
            instructions: String::new(),
            input: vec![ResponseItem::Message {
                id: None,
                role: "developer".to_string(),
                content: vec![ContentItem::InputText { text: "Special instructions".to_string() }],
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

        let chat_req = responses_to_chat_request(&responses_req);
        assert_eq!(chat_req.messages.len(), 1);
        assert_eq!(chat_req.messages[0].role, "system");
        assert_eq!(chat_req.messages[0].content, "Special instructions");
    }

    #[test]
    fn test_extract_text_content() {
        let content = vec![
            ContentItem::InputText { text: "Hello".to_string() },
            ContentItem::OutputText { text: "World".to_string() },
        ];
        let text = extract_text_content(&content);
        assert_eq!(text, Some("Hello\nWorld".to_string()));
    }

    #[test]
    fn test_extract_text_content_empty() {
        let content = vec![ContentItem::InputImage { image_url: "http://example.com".to_string() }];
        let text = extract_text_content(&content);
        assert_eq!(text, None);
    }
}
