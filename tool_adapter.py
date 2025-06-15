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
SD_IMAGE_GEN_PREFIX = "sd_image_gen-generate_sd_image"
OPENAPI_SPEECH_PREFIX = "openapi_integrator_mcp-generate_speech"
GEMINI_EDIT_IMAGE_PREFIX = "gemini_integrator_mcp-gemini_edit_image" # 新增

# async def _handle_gemini_web_search(...) # 函数已移除

async def _handle_gemini_edit_image(event: AstrMessageEvent, tool_content_str: str, tool_name: str, plugin_instance: Star):
    """处理 gemini_edit_image 工具的响应，发送图片。"""
    try:
        tool_content_json = json.loads(tool_content_str)
        local_path = tool_content_json.get("localPath")
        cf_image_url = tool_content_json.get("cfImageUrl") # Cloudflare URL
        media_segment = None

        plugin_logger.info(f"工具适配器：[{tool_name}] 响应内容: localPath='{local_path}', cfImageUrl='{cf_image_url}'")

        if local_path and os.path.exists(local_path):
            plugin_logger.info(f"工具适配器：[{tool_name}] 检测到本地路径 '{local_path}'。")
            if hasattr(plugin_instance, 'plugin_base_data_path') and plugin_instance.plugin_base_data_path:
                stored_path = await store_media_in_plugin_data(local_path, plugin_instance.plugin_base_data_path)
                if stored_path:
                    plugin_logger.info(f"工具适配器：[{tool_name}] 图片已存至插件数据目录 '{stored_path}'，将使用此路径发送。")
                    media_segment = Comp.Image.fromFileSystem(str(stored_path))
                else:
                    plugin_logger.warning(f"工具适配器：[{tool_name}] 存储图片到插件数据目录失败，尝试使用原始本地路径 '{local_path}'。")
                    media_segment = Comp.Image.fromFileSystem(local_path)
            else:
                plugin_logger.warning(f"工具适配器：[{tool_name}] 插件实例缺少 plugin_base_data_path，直接使用原始本地路径 '{local_path}'。")
                media_segment = Comp.Image.fromFileSystem(local_path)
        elif cf_image_url:
            plugin_logger.info(f"工具适配器：[{tool_name}] 本地路径无效或未提供，尝试使用 Cloudflare URL '{cf_image_url}' 发送图片。")
            media_segment = Comp.Image.fromURL(cf_image_url)
        
        if media_segment:
            await event.send(event.chain_result([media_segment]))
            plugin_logger.info(f"工具适配器：[{tool_name}] 图片已发送。")
            return True
        else:
            plugin_logger.warning(f"工具适配器：[{tool_name}] 未能从 localPath 或 cfImageUrl 创建图片消息段。localPath: {local_path}, cfImageUrl: {cf_image_url}")
            
    except json.JSONDecodeError:
        plugin_logger.error(f"工具适配器：[{tool_name}] 解析内容JSON失败。内容: {tool_content_str}", exc_info=True)
    except Exception as e:
        plugin_logger.error(f"工具适配器：[{tool_name}] 处理时发生错误: {e}", exc_info=True)
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
            # plugin_instance.processed_tool_call_ids.add(tool_call_id) # 由调用者标记
            plugin_logger.info(f"工具适配器：[{tool_call_id}] 图片已发送。")
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
            # plugin_instance.processed_tool_call_ids.add(tool_call_id) # 由调用者标记
            plugin_logger.info(f"工具适配器：[{tool_call_id}] 语音已发送。")
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
    使用基于会话ID和历史记录索引的方式来跟踪已处理的条目。
    """
    try:
        if not hasattr(plugin_instance, 'session_processed_indices') or \
           not hasattr(plugin_instance, 'session_last_history_length'):
            plugin_logger.warning("工具适配器：插件实例缺少会话处理状态跟踪属性 (session_processed_indices 或 session_last_history_length)。")
            return
        
        conversation_manager = plugin_instance.context.conversation_manager
        session_id = await conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        
        if not session_id:
            plugin_logger.debug("工具适配器：无法获取当前会话ID，跳过处理。")
            return
        
        conversation = await conversation_manager.get_conversation(event.unified_msg_origin, session_id)
        if not conversation or not conversation.history:
            plugin_logger.debug(f"工具适配器：会话 {session_id} 不存在或历史记录为空。")
            # 如果会话历史为空，也可能意味着重置，确保清除旧的长度记录
            if plugin_instance.session_last_history_length.get(session_id, 0) > 0:
                 plugin_logger.info(f"工具适配器：会话 {session_id} 历史记录为空，可能已重置。清除已处理索引。")
                 plugin_instance.session_processed_indices[session_id] = set()
            plugin_instance.session_last_history_length[session_id] = 0
            return

        history_list = []
        try:
            history_list = json.loads(conversation.history)
            if not isinstance(history_list, list):
                plugin_logger.warning(f"工具适配器：会话 {session_id} 的历史非列表格式。")
                return
        except json.JSONDecodeError:
            plugin_logger.error(f"工具适配器：解析会话 {session_id} 的历史JSON失败。", exc_info=True)
            return
        
        current_history_length = len(history_list)
        last_known_length = plugin_instance.session_last_history_length.get(session_id, -1)

        processed_indices_for_session = plugin_instance.session_processed_indices.setdefault(session_id, set())

        # 会话重置检测：如果当前历史长度显著小于上次记录的长度
        # (且上次长度不是初始值-1，也不是0立即增长到非0)
        # 一个简单的判断是 current_history_length < last_known_length
        # 为避免因单条消息删除等小波动误判，可以设置一个更宽松的条件，
        # 但根据用户描述“索引值变小了”，直接比较长度即可。
        if last_known_length != -1 and current_history_length < last_known_length:
            plugin_logger.info(f"工具适配器：检测到会话 {session_id} 可能已重置 (当前长度 {current_history_length} < 上次记录长度 {last_known_length})。正在清除该会话的已处理索引记录。")
            processed_indices_for_session.clear()
            if hasattr(plugin_instance, '_save_processed_state'):
                plugin_instance._save_processed_state() # 保存状态
        
        # 只有当长度实际变化时才更新和保存，或者如果它是第一次被记录
        if plugin_instance.session_last_history_length.get(session_id) != current_history_length:
            plugin_instance.session_last_history_length[session_id] = current_history_length
            if hasattr(plugin_instance, '_save_processed_state'):
                plugin_instance._save_processed_state() # 保存状态
        
        if not history_list:
            plugin_logger.debug(f"工具适配器：会话 {session_id} 历史记录为空（重置后或初始）。")
            return

        plugin_logger.debug(f"工具适配器：检查会话 {session_id}。历史长度: {current_history_length}。已处理索引: {processed_indices_for_session}")

        # 从后向前遍历 (最新的条目优先)
        for i in range(current_history_length):
            original_index = current_history_length - 1 - i # 实际在 history_list 中的索引
            message_entry = history_list[original_index]
            
            plugin_logger.debug(f"工具适配器：会话 {session_id}，检查索引 {original_index}: {message_entry}") # 这条日志之前已添加

            if isinstance(message_entry, dict) and \
               message_entry.get("role") == "tool" and \
               "tool_call_id" in message_entry and \
               "content" in message_entry:
                
                # tool_call_id 在此插件的上下文中实际上是工具名，例如 "sd_image_gen-generate_sd_image"
                tool_name_from_history = message_entry.get("tool_call_id") 
                tool_content_str = message_entry.get("content")

                if not tool_name_from_history or not tool_content_str or not isinstance(tool_content_str, str):
                    plugin_logger.debug(f"工具适配器：会话 {session_id}，索引 {original_index}：跳过不完整的工具消息条目。")
                    continue

                # 使用 original_index 和 session_id 来唯一确定一个历史条目是否被处理过
                if original_index in processed_indices_for_session:
                    plugin_logger.debug(f"工具适配器：会话 {session_id}，索引 {original_index} (工具名: {tool_name_from_history}) 已处理过，继续检查更早的条目。")
                    continue

                handler_function = None
                # 注意：这里的 tool_name_from_history 就是用户之前指的 tool_call_id
                # if tool_name_from_history.startswith(GEMINI_WEB_SEARCH_PREFIX): # 已移除
                #     handler_function = _handle_gemini_web_search
                if tool_name_from_history.startswith(GEMINI_EDIT_IMAGE_PREFIX): # 新增
                    handler_function = _handle_gemini_edit_image
                elif tool_name_from_history.startswith(SD_IMAGE_GEN_PREFIX):
                    handler_function = _handle_sd_image_gen
                elif tool_name_from_history.startswith(OPENAPI_SPEECH_PREFIX):
                    handler_function = _handle_openapi_speech
                
                if handler_function:
                    plugin_logger.info(f"工具适配器：会话 {session_id}，索引 {original_index}：发现新的 '{tool_name_from_history}' 工具响应，准备处理。")
                    
                    # 将 tool_name_from_history 作为 tool_call_id 参数传递给处理函数，因为它们内部可能仍在使用这个名称进行日志记录
                    processed_successfully = await handler_function(event, tool_content_str, tool_name_from_history, plugin_instance)
                    
                    # 无论成功与否（只要不是致命异常），都标记此索引为已处理，以避免重复尝试
                    # 如果处理函数返回 False，表示它识别了内容但决定不处理或处理失败（例如内容不符合预期），
                    # 标记为已处理可以防止对同一无效/不匹配条目的重复尝试。
                    # 如果处理函数成功 (返回 True)，也标记为已处理。
                    if processed_successfully is True or processed_successfully is False:
                         processed_indices_for_session.add(original_index)
                         plugin_logger.info(f"工具适配器：会话 {session_id}，索引 {original_index} (工具名: {tool_name_from_history}) 已标记为已处理 (处理结果: {processed_successfully})。")
                         if hasattr(plugin_instance, '_save_processed_state'):
                            plugin_instance._save_processed_state() # 保存状态
                    
                    # 处理完一个就立即返回，等待下一次 after_message_sent 触发
                    # 这保持了每次只发送一个工具结果的行为
                    return 
                else:
                    plugin_logger.debug(f"工具适配器：会话 {session_id}，索引 {original_index}：工具名 '{tool_name_from_history}' 未匹配任何已知处理器。")
            
        plugin_logger.debug(f"工具适配器：会话 {session_id} 完成历史记录检查，未找到需要处理的新工具响应。")

    except Exception as e:
        plugin_logger.error(f"工具适配器：在 process_tool_response_from_history (会话 {session_id if 'session_id' in locals() else '未知'}) 中发生未捕获的严重错误: {e}", exc_info=True)
