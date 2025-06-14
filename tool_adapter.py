import json
import os
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Star
import astrbot.api.message_components as Comp

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

from .utils import store_media_in_plugin_data # 导入新的辅助函数

# 目标工具ID常量化
GEMINI_WEB_SEARCH_PREFIX = "gemini_integrator_mcp-gemini_web_search"
SD_IMAGE_GEN_PREFIX = "sd_image_gen-generate_sd_image"
OPENAPI_SPEECH_PREFIX = "openapi_integrator_mcp-generate_speech"

async def _handle_gemini_web_search(event: AstrMessageEvent, tool_content_str: str, tool_call_id: str, plugin_instance: Star):
    try:
        tool_content_json = json.loads(tool_content_str)
        answer_text = tool_content_json.get("answerText")
        if answer_text:
            plugin_logger.info(f"工具适配器：[{tool_call_id}] 提取到 answerText，准备发送。")
            await event.send(event.plain_result(str(answer_text)))
            plugin_instance.processed_tool_call_ids.add(tool_call_id)
            plugin_logger.info(f"工具适配器：[{tool_call_id}] answerText 已发送并标记为已处理。")
            return True
        else:
            plugin_logger.warning(f"工具适配器：[{tool_call_id}] 内容中未找到 'answerText'。")
    except json.JSONDecodeError:
        plugin_logger.error(f"工具适配器：[{tool_call_id}] 解析内容JSON失败。内容: {tool_content_str}", exc_info=True)
    except Exception as e:
        plugin_logger.error(f"工具适配器：[{tool_call_id}] 处理时发生错误: {e}", exc_info=True)
    return False

async def _handle_sd_image_gen(event: AstrMessageEvent, tool_content_str: str, tool_call_id: str, plugin_instance: Star):
    try:
        tool_content_list = json.loads(tool_content_str)
        if not isinstance(tool_content_list, list) or not tool_content_list:
            plugin_logger.warning(f"工具适配器：[{tool_call_id}] 内容不是有效列表或列表为空。")
            return False
        
        media_item = tool_content_list[0]
        path = media_item.get("path")
        url = media_item.get("url")
        media_segment = None

        if path and os.path.exists(path):
            plugin_logger.info(f"工具适配器：[{tool_call_id}] 原始本地路径 '{path}'。")
            if hasattr(plugin_instance, 'plugin_base_data_path') and plugin_instance.plugin_base_data_path:
                stored_path = await store_media_in_plugin_data(path, plugin_instance.plugin_base_data_path)
                if stored_path:
                    plugin_logger.info(f"工具适配器：[{tool_call_id}] 图片已存至插件数据目录 '{stored_path}'，将使用此路径发送。")
                    media_segment = Comp.Image.fromFileSystem(str(stored_path))
                else:
                    plugin_logger.warning(f"工具适配器：[{tool_call_id}] 存储图片到插件数据目录失败，尝试使用原始路径 '{path}'。")
                    media_segment = Comp.Image.fromFileSystem(path) # 回退到原始路径
            else:
                plugin_logger.warning(f"工具适配器：[{tool_call_id}] 插件实例缺少 plugin_base_data_path，直接使用原始路径 '{path}'。")
                media_segment = Comp.Image.fromFileSystem(path)
        elif url:
            plugin_logger.info(f"工具适配器：[{tool_call_id}] 本地路径无效或未提供，尝试使用 URL '{url}' 发送图片。")
            media_segment = Comp.Image.fromURL(url)
        
        if media_segment:
            await event.send(event.chain_result([media_segment]))
            plugin_instance.processed_tool_call_ids.add(tool_call_id)
            plugin_logger.info(f"工具适配器：[{tool_call_id}] 图片已发送并标记为已处理。")
            return True
        else:
            plugin_logger.warning(f"工具适配器：[{tool_call_id}] 未能从 path 或 url 创建图片消息段。路径: {path}, URL: {url}")
            
    except json.JSONDecodeError:
        plugin_logger.error(f"工具适配器：[{tool_call_id}] 解析内容JSON失败。内容: {tool_content_str}", exc_info=True)
    except Exception as e:
        plugin_logger.error(f"工具适配器：[{tool_call_id}] 处理时发生错误: {e}", exc_info=True)
    return False

async def _handle_openapi_speech(event: AstrMessageEvent, tool_content_str: str, tool_call_id: str, plugin_instance: Star):
    try:
        tool_content_list = json.loads(tool_content_str)
        if not isinstance(tool_content_list, list) or not tool_content_list:
            plugin_logger.warning(f"工具适配器：[{tool_call_id}] 内容不是有效列表或列表为空。")
            return False

        media_item = tool_content_list[0]
        path = media_item.get("path")
        media_segment = None

        if path and os.path.exists(path):
            plugin_logger.info(f"工具适配器：[{tool_call_id}] 原始本地路径 '{path}'。")
            if hasattr(plugin_instance, 'plugin_base_data_path') and plugin_instance.plugin_base_data_path:
                stored_path = await store_media_in_plugin_data(path, plugin_instance.plugin_base_data_path)
                if stored_path:
                    plugin_logger.info(f"工具适配器：[{tool_call_id}] 语音已存至插件数据目录 '{stored_path}'，将使用此路径发送。")
                    media_segment = Comp.Record(file=str(stored_path))
                else:
                    plugin_logger.warning(f"工具适配器：[{tool_call_id}] 存储语音到插件数据目录失败，尝试使用原始路径 '{path}'。")
                    media_segment = Comp.Record(file=path) # 回退到原始路径
            else:
                plugin_logger.warning(f"工具适配器：[{tool_call_id}] 插件实例缺少 plugin_base_data_path，直接使用原始路径 '{path}'。")
                media_segment = Comp.Record(file=path)
        
        if media_segment:
            await event.send(event.chain_result([media_segment]))
            plugin_instance.processed_tool_call_ids.add(tool_call_id)
            plugin_logger.info(f"工具适配器：[{tool_call_id}] 语音已发送并标记为已处理。")
            return True
        else:
            plugin_logger.warning(f"工具适配器：[{tool_call_id}] 未能从 path 创建语音消息段或路径无效。路径: {path}")

    except json.JSONDecodeError:
        plugin_logger.error(f"工具适配器：[{tool_call_id}] 解析内容JSON失败。内容: {tool_content_str}", exc_info=True)
    except Exception as e:
        plugin_logger.error(f"工具适配器：[{tool_call_id}] 处理时发生错误: {e}", exc_info=True)
    return False


async def process_tool_response_from_history(plugin_instance: Star, event: AstrMessageEvent):
    """
    在消息发送后，检查会话历史记录中是否有来自特定工具的、尚未处理的响应。
    如果找到，则根据工具类型提取内容并单独发送。
    """
    try:
        if not hasattr(plugin_instance, 'processed_tool_call_ids'):
            plugin_logger.warning("工具适配器：插件实例上未找到 'processed_tool_call_ids' 属性。")
            return
        
        conversation_manager = plugin_instance.context.conversation_manager
        current_conversation_id = await conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        
        if not current_conversation_id:
            return # 静默返回，避免过多日志
        
        conversation = await conversation_manager.get_conversation(event.unified_msg_origin, current_conversation_id)
        if not conversation or not conversation.history:
            return # 静默返回

        plugin_logger.debug(f"工具适配器：检查会话历史。当前会话ID: {current_conversation_id}，已处理ID: {plugin_instance.processed_tool_call_ids}")

        history_list = []
        try:
            history_list = json.loads(conversation.history)
            if not isinstance(history_list, list):
                plugin_logger.warning(f"工具适配器：会话历史非列表格式。")
                return
        except json.JSONDecodeError:
            plugin_logger.error(f"工具适配器：解析会话历史JSON失败。", exc_info=True)
            return
        
        if not history_list:
            return

        # 从后向前遍历，找到最新的未处理的目标工具响应
        # for message_entry in reversed(history_list): # 原遍历方式
        for i in range(len(history_list)):
            original_index = len(history_list) - 1 - i
            message_entry = history_list[original_index] # 从后向前取元素
            
            plugin_logger.debug(f"工具适配器：检查历史记录条目索引 {original_index}: {message_entry}")

            if isinstance(message_entry, dict) and \
               message_entry.get("role") == "tool" and \
               "tool_call_id" in message_entry and \
               "content" in message_entry:
                
                tool_call_id = message_entry.get("tool_call_id")
                tool_content_str = message_entry.get("content")

                if not tool_call_id or not tool_content_str or not isinstance(tool_content_str, str):
                    plugin_logger.debug(f"工具适配器：跳过不完整的工具消息条目: {message_entry}")
                    continue

                # 如果最新的 role:tool 消息已经被处理，则停止，不再检查更早的
                if tool_call_id in plugin_instance.processed_tool_call_ids:
                    plugin_logger.debug(f"工具适配器：工具响应 '{tool_call_id}' 已处理，停止本次检查。")
                    return 

                handler_function = None
                if tool_call_id.startswith(GEMINI_WEB_SEARCH_PREFIX):
                    handler_function = _handle_gemini_web_search
                elif tool_call_id.startswith(SD_IMAGE_GEN_PREFIX):
                    handler_function = _handle_sd_image_gen
                elif tool_call_id.startswith(OPENAPI_SPEECH_PREFIX):
                    handler_function = _handle_openapi_speech
                
                if handler_function:
                    plugin_logger.info(f"工具适配器：发现新的 '{tool_call_id}' 工具响应，准备处理。")
                    processed_successfully = await handler_function(event, tool_content_str, tool_call_id, plugin_instance)
                    
                    if not processed_successfully:
                        # 如果处理函数返回False（表示处理失败但不是致命错误，例如内容不符合预期）
                        # 仍然标记为已处理，以避免对同一错误数据反复尝试
                        plugin_instance.processed_tool_call_ids.add(tool_call_id)
                        plugin_logger.info(f"工具适配器：[{tool_call_id}] 处理未成功，已标记避免重试。")
                    # 处理完一个（无论成功与否）就立即返回，等待下一次 after_message_sent 触发
                    return
                else:
                    # 这条 role:tool 消息不是我们关心的，继续往前找（理论上不应该发生，因为如果不是我们关心的，processed_tool_call_ids里不会有它，除非有其他插件也在用这个集合）
                    plugin_logger.debug(f"工具适配器：工具ID '{tool_call_id}' 未匹配任何已知处理器。")
            
            # 如果当前历史条目不是 role:tool，或者不包含我们需要的字段，则继续向上一个条目检查
            # 如果已经检查了 مثلا 5-10 条，还没找到未处理的 role:tool，可以提前退出以优化性能
            # 但当前逻辑是遍历完所有 role:tool 的消息，直到找到一个未处理的或者所有都处理过了

        plugin_logger.debug("工具适配器：完成历史记录检查，未找到需要处理的新工具响应。")

    except Exception as e:
        plugin_logger.error(f"工具适配器：在 process_tool_response_from_history 中发生未捕获的严重错误: {e}", exc_info=True)
