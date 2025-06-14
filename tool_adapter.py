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
        plugin_logger.info("工具适配器：process_tool_response_from_history 函数开始执行。")

        if not hasattr(plugin_instance, 'processed_tool_call_ids'):
            plugin_logger.warning("工具适配器：插件实例上未找到 'processed_tool_call_ids' 属性。跳过处理。")
            return
        plugin_logger.debug(f"工具适配器：已处理的 tool_call_ids 集合: {plugin_instance.processed_tool_call_ids}")

        conversation_manager = plugin_instance.context.conversation_manager
        current_conversation_id = await conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        
        if not current_conversation_id:
            plugin_logger.info("工具适配器：未找到当前会话ID。跳过处理。")
            return
        plugin_logger.info(f"工具适配器：当前会话ID为: {current_conversation_id}")

        conversation = await conversation_manager.get_conversation(event.unified_msg_origin, current_conversation_id)
        if not conversation or not conversation.history:
            plugin_logger.info("工具适配器：未找到会话或会话历史为空。跳过处理。")
            return
        plugin_logger.debug(f"工具适配器：获取到会话历史的前200字符: {conversation.history[:200]}...")

        history_list = []
        try:
            history_list = json.loads(conversation.history)
            if not isinstance(history_list, list):
                plugin_logger.warning(f"工具适配器：会话历史不是一个列表。实际类型为: {type(history_list)}。历史内容: {conversation.history}")
                return
        except json.JSONDecodeError:
            plugin_logger.error(f"工具适配器：解析会话历史JSON失败。历史内容: {conversation.history}", exc_info=True)
            return
        
        plugin_logger.info(f"工具适配器：成功解析会话历史，共 {len(history_list)} 条记录。开始反向遍历。")
        
        found_and_processed_new = False
        for i, message_entry in enumerate(reversed(history_list)):
            plugin_logger.debug(f"工具适配器：检查历史记录条目索引 {len(history_list) - 1 - i}: {message_entry}")
            if isinstance(message_entry, dict) and \
               message_entry.get("role") == "tool" and \
               "tool_call_id" in message_entry and \
               "content" in message_entry:
                
                tool_call_id = message_entry.get("tool_call_id")
                plugin_logger.info(f"工具适配器：找到 role='tool' 的消息，其 tool_call_id 为: {tool_call_id}")
                
                if tool_call_id and tool_call_id.startswith(TARGET_TOOL_CALL_ID_PREFIX):
                    plugin_logger.info(f"工具适配器：tool_call_id '{tool_call_id}' 匹配目标前缀 '{TARGET_TOOL_CALL_ID_PREFIX}'。")
                    if tool_call_id not in plugin_instance.processed_tool_call_ids:
                        plugin_logger.info(f"工具适配器：发现新的工具响应，ID 为: '{tool_call_id}'。")
                        
                        tool_content_str = message_entry.get("content")
                        if tool_content_str and isinstance(tool_content_str, str):
                            plugin_logger.debug(f"工具适配器：工具响应内容 (字符串): {tool_content_str}")
                            try:
                                tool_content_json = json.loads(tool_content_str)
                                answer_text = tool_content_json.get("answerText")
                                
                                if answer_text:
                                    plugin_logger.info(f"工具适配器：成功从 '{tool_call_id}' 提取到 answerText。准备发送。")
                                    await event.send(event.plain_result(str(answer_text)))
                                    plugin_instance.processed_tool_call_ids.add(tool_call_id)
                                    plugin_logger.info(f"工具适配器：answerText 已发送，'{tool_call_id}' 已标记为已处理。")
                                    found_and_processed_new = True
                                    break 
                                else:
                                    plugin_logger.warning(f"工具适配器：在工具 '{tool_call_id}' 的响应内容中未找到 'answerText' 字段。内容: {tool_content_str}")
                            except json.JSONDecodeError:
                                plugin_logger.error(f"工具适配器：解析工具 '{tool_call_id}' 的响应内容JSON失败。内容: {tool_content_str}", exc_info=True)
                            except Exception as e:
                                plugin_logger.error(f"工具适配器：处理工具 '{tool_call_id}' 响应时发生未知错误: {e}", exc_info=True)
                        else:
                            plugin_logger.warning(f"工具适配器：工具 '{tool_call_id}' 的响应内容为空或不是字符串类型。")
                        
                        if not found_and_processed_new: 
                             plugin_instance.processed_tool_call_ids.add(tool_call_id)
                             plugin_logger.info(f"工具适配器：将 '{tool_call_id}' 标记为已处理（即使提取answerText失败或内容不符）。")
                        break 
                    else:
                        plugin_logger.info(f"工具适配器：工具响应 '{tool_call_id}' 已被处理过。停止查找更早的记录。")
                        break 
                else:
                    plugin_logger.debug(f"工具适配器：tool_call_id '{tool_call_id}' 不匹配目标前缀 '{TARGET_TOOL_CALL_ID_PREFIX}'。")

        if not found_and_processed_new:
            plugin_logger.info("工具适配器：在此次检查中未找到新的、符合条件的目标工具响应进行处理。")

    except Exception as e:
        plugin_logger.error(f"工具适配器：在 process_tool_response_from_history 函数中发生严重错误: {e}", exc_info=True)
