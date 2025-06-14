import json
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Star # Star 通常在主插件类继承

# 尝试从 AstrBot 内部获取 logger
try:
    from astrbot.api import logger as plugin_logger
except ImportError:
    import logging
    plugin_logger = logging.getLogger("ToolPromptsToolAdapter")
    if not plugin_logger.hasHandlers():
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        plugin_logger.addHandler(handler)
        plugin_logger.setLevel(logging.INFO)

TARGET_TOOL_CALL_ID_PREFIX = "gemini_integrator_mcp-gemini_web_search"

async def process_tool_response_from_history(plugin_instance: Star, event: AstrMessageEvent):
    """
    在消息发送后，检查会话历史记录中是否有来自特定工具的、尚未处理的响应。
    如果找到，则提取 answerText 并单独发送。
    """
    try:
        if not hasattr(plugin_instance, 'processed_tool_call_ids'):
            plugin_logger.warning("ToolAdapter: 'processed_tool_call_ids' not found on plugin instance. Skipping.")
            return

        # 获取当前会话的上下文历史
        # self.context is plugin_instance.context
        conversation_manager = plugin_instance.context.conversation_manager
        
        # event.unified_msg_origin 是会话的唯一标识
        current_conversation_id = await conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        if not current_conversation_id:
            plugin_logger.debug("ToolAdapter: No current conversation ID found. Skipping.")
            return

        conversation = await conversation_manager.get_conversation(event.unified_msg_origin, current_conversation_id)
        if not conversation or not conversation.history:
            plugin_logger.debug("ToolAdapter: No conversation history found. Skipping.")
            return

        history_list = []
        try:
            history_list = json.loads(conversation.history)
            if not isinstance(history_list, list):
                plugin_logger.warning(f"ToolAdapter: Conversation history is not a list. History: {conversation.history}")
                return
        except json.JSONDecodeError:
            plugin_logger.error(f"ToolAdapter: Failed to parse conversation history JSON. History: {conversation.history}")
            return
        
        # 从后向前遍历历史记录，查找最新的未处理的目标工具响应
        for message_entry in reversed(history_list):
            if isinstance(message_entry, dict) and \
               message_entry.get("role") == "tool" and \
               "tool_call_id" in message_entry and \
               "content" in message_entry:
                
                tool_call_id = message_entry.get("tool_call_id")
                
                if tool_call_id and tool_call_id.startswith(TARGET_TOOL_CALL_ID_PREFIX):
                    if tool_call_id not in plugin_instance.processed_tool_call_ids:
                        plugin_logger.info(f"ToolAdapter: Found new tool response for '{tool_call_id}' in history.")
                        
                        tool_content_str = message_entry.get("content")
                        if tool_content_str and isinstance(tool_content_str, str):
                            try:
                                tool_content_json = json.loads(tool_content_str)
                                answer_text = tool_content_json.get("answerText")
                                
                                if answer_text:
                                    plugin_logger.info(f"ToolAdapter: Extracting and sending answerText for '{tool_call_id}'.")
                                    await event.send(event.plain_result(str(answer_text)))
                                    plugin_instance.processed_tool_call_ids.add(tool_call_id)
                                    plugin_logger.info(f"ToolAdapter: answerText sent and '{tool_call_id}' marked as processed.")
                                    # 找到了最新的未处理的，处理完就返回，避免处理更早的同名工具调用（如果逻辑是只处理最新的一个）
                                    # 或者，如果希望一次性处理所有新的，则不在这里 return，但要注意可能的多条消息发送。
                                    # 当前逻辑：找到最新的一个未处理的，处理并返回。
                                    return
                                else:
                                    plugin_logger.warning(f"ToolAdapter: 'answerText' not found in content for '{tool_call_id}'. Content: {tool_content_str}")
                            except json.JSONDecodeError:
                                plugin_logger.error(f"ToolAdapter: Failed to parse tool content JSON for '{tool_call_id}'. Content: {tool_content_str}")
                            except Exception as e:
                                plugin_logger.error(f"ToolAdapter: Error processing tool content for '{tool_call_id}': {e}", exc_info=True)
                        else:
                            plugin_logger.warning(f"ToolAdapter: Tool content is missing or not a string for '{tool_call_id}'.")
                        
                        # 即使处理失败或内容不符合预期，也标记为已处理，避免反复尝试
                        plugin_instance.processed_tool_call_ids.add(tool_call_id)
                        return # 处理完一个（无论成功与否）就退出，等待下一次钩子触发
                    else:
                        # plugin_logger.debug(f"ToolAdapter: Tool response for '{tool_call_id}' already processed. Skipping.")
                        # 如果已经处理过，并且我们是从后向前找最新的，那么更早的同名工具调用也应该被处理过了（或不应再处理）
                        # 如果只关心最新的一个，可以在这里 break 或者 return
                        return # 假设我们只关心最新的一个未处理的，如果最新的已经被处理，则停止
            
            # 如果当前消息不是我们要找的 role:tool，或者不是目标工具，继续往前找
            # 但如果这条消息是 assistant 发出的包含 tool_calls 的消息，说明工具调用刚发生，结果还没回来
            # if isinstance(message_entry, dict) and message_entry.get("role") == "assistant" and message_entry.get("tool_calls"):
            #     plugin_logger.debug(f"ToolAdapter: Found assistant message with tool_calls, tool results might be next. History length: {len(history_list)}")
            #     # 通常工具结果会紧跟在 assistant 的 tool_calls 消息之后

    except Exception as e:
        plugin_logger.error(f"ToolAdapter: Error in process_tool_response_from_history: {e}", exc_info=True)
