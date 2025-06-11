import re
import os
from pathlib import Path
import asyncio # 用于可能的异步操作
import typing # 导入 typing 用于 Any 类型提示

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from .utils import (
    get_temp_media_dir,
    download_media,
    get_mime_type,
    file_to_base64,
    cleanup_temp_files,
    plugin_logger
)


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.2.4", "https://github.com/slot181/astrbot_plugin_tool_prompts") # 版本号由用户管理
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temp_media_dir = None

        log_level_str = self.config.get("log_level", "INFO").upper()
        plugin_logger.setLevel(log_level_str)
        
        plugin_name_for_path = "astrbot_plugin_tool_prompts"
        if hasattr(self, 'metadata') and self.metadata and hasattr(self.metadata, 'name') and self.metadata.name:
            plugin_name_for_path = self.metadata.name
        else:
            plugin_logger.warning("无法从 self.metadata.name 获取插件名，将使用默认名 'astrbot_plugin_tool_prompts' 构建数据路径。")

        # 使用用户期望的路径结构：./data/plugins_data/<plugin_name>/
        # 这假设 AstrBot 从其根目录运行
        base_data_path = Path("./data/plugins_data") / plugin_name_for_path
        
        try:
            # base_data_path.mkdir(parents=True, exist_ok=True) # get_temp_media_dir 会创建父目录
            # plugin_logger.info(f"插件数据基础路径设置为: {base_data_path.resolve()}")
            self.temp_media_dir = get_temp_media_dir(base_data_path) # utils.get_temp_media_dir 会在 base_data_path 下创建 temp_media
            
            if self.temp_media_dir:
                plugin_logger.info(f"临时媒体目录已初始化: {self.temp_media_dir.resolve()}")
                cleanup_minutes = self.config.get("temp_file_cleanup_minutes", 60)
                if cleanup_minutes > 0:
                    plugin_logger.info(f"插件初始化：执行临时文件清理，目录: {self.temp_media_dir}, 清理周期: {cleanup_minutes} 分钟。")
                    cleanup_temp_files(self.temp_media_dir, cleanup_minutes)
                else:
                    plugin_logger.info("插件初始化：临时文件自动清理已禁用 (周期设置为0或更小)。")
            else:
                plugin_logger.error(f"临时媒体目录未能成功初始化于: {base_data_path}")
        except Exception as e:
            plugin_logger.error(f"创建插件数据基础路径 {base_data_path} 或临时媒体目录失败: {e}", exc_info=True)
            self.temp_media_dir = None

        self.url_pattern = re.compile(r'(?:https?:)?//[^\s"\'`<>()[\]{}]+')
        self.path_pattern = re.compile(r'(?:[a-zA-Z]:\\|/)[^\s"\'`<>`()[\]{}]+')
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 初始化完成。")

    @filter.on_llm_response(priority=1)
    async def on_llm_response_handler(self, event: AstrMessageEvent, resp: LLMResponse):
        if hasattr(event, '_media_processed_by_tool_prompts_plugin') and event._media_processed_by_tool_prompts_plugin:
            plugin_logger.debug("LLM响应处理器：事件已由本插件处理过媒体，跳过。")
            return
        
        if resp.role == "tool" and resp.tools_call_name:
            plugin_logger.info("LLM响应处理器：检测到工具调用。")
            for tool_name in resp.tools_call_name:
                message = f"正在调用 {tool_name} 工具中……"
                await event.send(event.plain_result(message))
            return

        if resp.role == "assistant" and resp.completion_text:
            text_to_process = resp.completion_text
            url_matches = list(self.url_pattern.finditer(text_to_process))
            path_matches = list(self.path_pattern.finditer(text_to_process))
            
            temp_matches = {}
            for match in url_matches + path_matches:
                match_str = match.group(0)
                if match_str not in temp_matches or \
                   (len(match_str) > len(temp_matches[match_str].group(0))) or \
                   (len(match_str) == len(temp_matches[match_str].group(0)) and match.start() < temp_matches[match_str].start()):
                    temp_matches[match_str] = match
            all_matches = sorted(list(temp_matches.values()), key=lambda m: m.start())

            if not all_matches:
                return
            plugin_logger.debug(f"LLM响应处理器：接收到原始文本: {text_to_process}")

            temp_processed_items = {}
            for match_obj in all_matches:
                original_match_str = match_obj.group(0)
                corrected_path = original_match_str
                if original_match_str.startswith("//"):
                    corrected_path = "https:" + original_match_str
                if self._is_media(corrected_path):
                    if corrected_path not in temp_processed_items:
                         temp_processed_items[corrected_path] = {'original': match_obj, 'corrected_path': corrected_path}
            processed_matches = sorted(list(temp_processed_items.values()), key=lambda item: item['original'].start())

            if not processed_matches:
                plugin_logger.debug("LLM响应处理器：未找到可识别的媒体，不进行特殊处理。")
                return

            plugin_logger.info("LLM响应处理器：找到可识别的媒体，将分条发送并阻止原始消息。")
            last_end = 0
            for item in processed_matches:
                match = item['original']
                corrected_path = item['corrected_path']
                plain_text_before = text_to_process[last_end:match.start()].strip()
                if plain_text_before:
                    await event.send(event.plain_result(plain_text_before))
                media_segment = self._create_media_segment(corrected_path)
                await event.send(event.chain_result([media_segment]))
                last_end = match.end()
            plain_text_after = text_to_process[last_end:].strip()
            if plain_text_after:
                await event.send(event.plain_result(plain_text_after))

            plugin_logger.info("LLM响应处理器：将原始响应文本替换为空格以防止重复发送，并标记事件已处理。")
            setattr(event, '_media_processed_by_tool_prompts_plugin', True)
            resp.completion_text = " "

    def _is_media(self, path_or_url: str) -> bool:
        has_media_extension = any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mov', '.avi', '.wav', '.pdf', '.doc', '.docx', '.txt'])
        if not has_media_extension:
            return False
        if path_or_url.startswith('/') or re.match(r'^[a-zA-Z]:\\', path_or_url):
            return os.path.exists(path_or_url)
        if path_or_url.lower().startswith('http:') or path_or_url.lower().startswith('https:'):
            return True
        return False

    def _create_media_segment(self, path_or_url: str):
        is_url = path_or_url.lower().startswith('http:') or path_or_url.lower().startswith('https:')
        if any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
            plugin_logger.info(f"媒体处理：识别为图片: {path_or_url}")
            return Comp.Image.fromURL(path_or_url) if is_url else Comp.Image.fromFileSystem(path_or_url)
        if any(path_or_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi']):
            plugin_logger.info(f"媒体处理：识别为视频: {path_or_url}")
            return Comp.Video.fromURL(path_or_url) if is_url else Comp.Video.fromFileSystem(path_or_url)
        if path_or_url.lower().endswith('.wav'):
            plugin_logger.info(f"媒体处理：识别为音频: {path_or_url}")
            return Comp.Record(url=path_or_url) if is_url else Comp.Record(file=path_or_url)
        if any(path_or_url.lower().endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.txt']):
            plugin_logger.info(f"媒体处理：识别为文档: {path_or_url}")
            return Comp.File(url=path_or_url, name=os.path.basename(path_or_url)) if is_url else Comp.File(file=path_or_url, name=os.path.basename(path_or_url))
        plugin_logger.debug(f"媒体处理：路径 '{path_or_url}' 未匹配任何已知媒体类型，将作为纯文本处理。")
        return Comp.Plain(text=path_or_url)

    async def _prepare_multimodal_parts(self, replied_message_segments: list) -> list:
        parts = []
        enable_gemini_native = self.config.get("enable_gemini_native_multimodal", False)
        # 新增：读取 provider 模式开关
        is_gemini_mode_active = self.config.get("is_gemini_provider_mode", False)
        enable_non_gemini_image = self.config.get("enable_non_gemini_multimodal_image", False)

        if is_gemini_mode_active:
            plugin_logger.info("当前提供商模式为: Gemini (基于插件配置 is_gemini_provider_mode)。")
        else:
            plugin_logger.info("当前提供商模式为: OpenAI/兼容 (基于插件配置 is_gemini_provider_mode)。")

        for seg_idx, seg_data in enumerate(replied_message_segments):
            seg_type = seg_data.get('type')
            seg_content_data = seg_data.get('data', {})
            media_url = seg_content_data.get('url')

            if seg_type == 'text' and seg_content_data.get('text'):
                parts.append({"type": "text", "text": seg_content_data['text'].strip()})
            elif seg_type == 'image' and media_url:
                # 修复 NameError: is_gemini_provider -> is_gemini_mode_active
                if is_gemini_mode_active and enable_gemini_native:
                    if not self.temp_media_dir:
                        plugin_logger.warning(f"Gemini图片处理跳过：临时目录未初始化。URL: {media_url}")
                        parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，下载失败，URL: {media_url}]"})
                        continue
                    downloaded_file = await download_media(media_url, self.temp_media_dir, "img_")
                    if downloaded_file:
                        mime_type = get_mime_type(downloaded_file) or "image/jpeg"
                        base64_data = file_to_base64(downloaded_file)
                        if base64_data:
                            plugin_logger.info(f"Gemini原生：准备图片 part (Base64) for {media_url}")
                            parts.append({"type": "inline_data", "mime_type": mime_type, "data": base64_data, "original_url": media_url})
                        else:
                            parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，Base64编码失败，URL: {media_url}]"})
                    else:
                        parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，下载失败，URL: {media_url}]"})
                # 修复 NameError: is_gemini_provider -> is_gemini_mode_active
                elif not is_gemini_mode_active and enable_non_gemini_image:
                    if not self.temp_media_dir:
                        plugin_logger.warning(f"非Gemini图片处理跳过：临时目录未初始化。URL: {media_url}")
                        parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，下载失败，URL: {media_url}]"})
                        continue
                    downloaded_file = await download_media(media_url, self.temp_media_dir, "img_")
                    if downloaded_file:
                        mime_type = get_mime_type(downloaded_file) or "image/jpeg"
                        base64_data = file_to_base64(downloaded_file)
                        if base64_data:
                            plugin_logger.info(f"非Gemini：准备图片 part (data URI) for {media_url}")
                            # OpenAI API 要求 image_url 对象只包含 "url" 和可选的 "detail"
                            # 移除 "original_url" 键，避免 API 错误
                            parts.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64_data}"
                                    # "detail": "auto" # 可以根据需要添加 detail
                                }
                                # original_url: media_url # 此信息仅供内部使用，不发送给API
                            })
                            plugin_logger.debug(f"非Gemini图片 part (original_url: {media_url}) 已准备，将发送给LLM的结构: {parts[-1]}")
                        else:
                            parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，Base64编码失败，URL: {media_url}]"})
                    else:
                        parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，下载失败，URL: {media_url}]"})
                else:
                    parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1} URL: {media_url}]"})
            elif seg_type in ['record', 'video'] and media_url:
                media_kind = "语音" if seg_type == 'record' else "视频"
                # 修复 NameError: is_gemini_provider -> is_gemini_mode_active
                if is_gemini_mode_active and enable_gemini_native:
                    if not self.temp_media_dir:
                        plugin_logger.warning(f"Gemini{media_kind}处理跳过：临时目录未初始化。URL: {media_url}")
                        parts.append({"type": "text", "text": f"[引用的{media_kind}{seg_idx+1}，下载失败，URL: {media_url}]"})
                        continue
                    downloaded_file = await download_media(media_url, self.temp_media_dir, f"{seg_type}_")
                    if downloaded_file:
                        mime_type = get_mime_type(downloaded_file) or "application/octet-stream"
                        base64_data = file_to_base64(downloaded_file)
                        if base64_data:
                            plugin_logger.info(f"Gemini原生：准备{media_kind} part (Base64) for {media_url}")
                            parts.append({"type": "inline_data", "mime_type": mime_type, "data": base64_data, "original_url": media_url})
                        else:
                            parts.append({"type": "text", "text": f"[引用的{media_kind}{seg_idx+1}，Base64编码失败，URL: {media_url}]"})
                    else:
                        parts.append({"type": "text", "text": f"[引用的{media_kind}{seg_idx+1}，下载失败，URL: {media_url}]"})
                else:
                    parts.append({"type": "text", "text": f"[引用的{media_kind}{seg_idx+1} URL: {media_url} ({'内容未转录' if seg_type == 'record' else ''})]"})
        return parts

    @filter.on_llm_request(priority=1)
    async def on_llm_request_handler(self, event: AstrMessageEvent, req: ProviderRequest):
        if self.temp_media_dir:
            cleanup_minutes = self.config.get("temp_file_cleanup_minutes", 60)
            if cleanup_minutes > 0:
                cleanup_temp_files(self.temp_media_dir, cleanup_minutes)

        if event.get_platform_name() != "aiocqhttp":
            return

        raw_message_chain = event.message_obj.message
        reply_message_id_str = None
        for segment in raw_message_chain:
            if isinstance(segment, Comp.Reply):
                if hasattr(segment, 'data') and isinstance(segment.data, dict) and 'id' in segment.data:
                    reply_message_id_str = str(segment.data['id'])
                elif hasattr(segment, 'id'):
                    reply_message_id_str = str(segment.id)
                if reply_message_id_str:
                    plugin_logger.info(f"LLM请求预处理：检测到QQ引用消息，ID: {reply_message_id_str}")
                else:
                    plugin_logger.warning(f"LLM请求预处理：找到Reply段，但无法确定其message_id。段内容: {segment}")
                break
        if not reply_message_id_str:
            return

        try:
            client = None
            if isinstance(event, AiocqhttpMessageEvent):
                client = event.bot
            else:
                platform_adapter = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform_adapter and hasattr(platform_adapter, 'get_client'):
                    client = platform_adapter.get_client()
                elif platform_adapter and hasattr(platform_adapter, 'client'):
                     client = platform_adapter.client
            if not client:
                plugin_logger.error("LLM请求预处理：无法获取到 aiocqhttp 客户端实例。")
                return

            plugin_logger.info(f"LLM请求预处理：尝试获取被引用消息详情，ID: {reply_message_id_str}")
            replied_message_detail = await client.api.call_action('get_msg', message_id=int(reply_message_id_str))
            
            if isinstance(replied_message_detail, dict) and 'message_id' in replied_message_detail and 'message' in replied_message_detail:
                plugin_logger.info(f"LLM请求预处理：成功获取被引用消息详情。")
                original_sender_nickname = replied_message_detail.get('sender', {}).get('card') or replied_message_detail.get('sender', {}).get('nickname', '未知用户')
                replied_segments = replied_message_detail.get('message', [])
                if not isinstance(replied_segments, list): replied_segments = []
                
                processed_parts = await self._prepare_multimodal_parts(replied_segments)
                
                if processed_parts:
                    if req.contexts is None: req.contexts = []
                    system_prompt_entry = None
                    if req.contexts and req.contexts[0].get('role') == 'system':
                        system_prompt_entry = req.contexts.pop(0)
                    
                    # 获取插件配置中的相关设置
                    is_gemini_mode_active_for_handler = self.config.get("is_gemini_provider_mode", False)
                    enable_gemini_native = self.config.get("enable_gemini_native_multimodal", False)
                    enable_non_gemini_image = self.config.get("enable_non_gemini_multimodal_image", False)
                    
                    plugin_logger.debug(f"on_llm_request_handler: is_gemini_mode_active_for_handler={is_gemini_mode_active_for_handler}, "
                                       f"enable_gemini_native={enable_gemini_native}, "
                                       f"enable_non_gemini_image={enable_non_gemini_image}")

                    is_gemini_multimodal_active = is_gemini_mode_active_for_handler and enable_gemini_native and any(p.get("type") != "text" for p in processed_parts)
                    is_other_multimodal_active = not is_gemini_mode_active_for_handler and enable_non_gemini_image and any(p.get("type") == "image_url" for p in processed_parts)
                    
                    plugin_logger.debug(f"on_llm_request_handler: is_gemini_multimodal_active={is_gemini_multimodal_active}, "
                                       f"is_other_multimodal_active={is_other_multimodal_active}")
                    
                    actual_quoted_contexts = []
                    prefix = f"用户 {event.get_sender_name()} 引用了 {original_sender_nickname} 的消息内容如下:\n\"\"\"\n"
                    suffix = "\n\"\"\""

                    if is_gemini_multimodal_active or is_other_multimodal_active:
                        content_parts_for_llm = []
                        if prefix.strip(): content_parts_for_llm.append({"type": "text", "text": prefix.strip()})
                        current_text_batch = []
                        for part_data in processed_parts:
                            if part_data.get("type") == "text":
                                current_text_batch.append(part_data.get("text",""))
                            else: 
                                if current_text_batch:
                                    content_parts_for_llm.append({"type": "text", "text": " ".join(current_text_batch)})
                                    current_text_batch = []
                                if part_data.get("type") == "inline_data" and is_gemini_multimodal_active:
                                    content_parts_for_llm.append({"inline_data": {"mime_type": part_data.get("mime_type"), "data": part_data.get("data")}})
                                elif part_data.get("type") == "image_url" and is_other_multimodal_active:
                                    content_parts_for_llm.append(part_data)
                                else: 
                                     current_text_batch.append(f"[{part_data.get('type')} @ {part_data.get('original_url', '未知URL')}]")
                        if current_text_batch:
                            content_parts_for_llm.append({"type": "text", "text": " ".join(current_text_batch)})
                        if suffix.strip(): content_parts_for_llm.append({"type": "text", "text": suffix.strip()})

                        if content_parts_for_llm:
                             actual_quoted_contexts.append({"role": "user", "content": content_parts_for_llm})
                        else:
                            plugin_logger.info("LLM请求预处理：多模态处理后内容为空。")
                    else: 
                        all_text_from_parts = []
                        for part_data in processed_parts:
                            if part_data.get("type") == "text":
                                all_text_from_parts.append(part_data.get("text",""))
                            elif part_data.get("type") == "inline_data":
                                all_text_from_parts.append(f"[引用的媒体文件: {part_data.get('mime_type')} @ {part_data.get('original_url','未知URL')}]")
                            elif part_data.get("type") == "image_url":
                                all_text_from_parts.append(f"[引用的图片: {part_data.get('original_url','未知URL')}]")
                        full_quoted_text = " ".join(all_text_from_parts).strip()
                        if full_quoted_text:
                            actual_quoted_contexts.append({
                                "role": "user",
                                "content": prefix + full_quoted_text + suffix
                            })
                        else:
                            plugin_logger.info("LLM请求预处理：纯文本处理后内容为空。")

                    if actual_quoted_contexts:
                        new_contexts = []
                        if system_prompt_entry: new_contexts.append(system_prompt_entry)
                        new_contexts.extend(req.contexts) 
                        new_contexts.extend(actual_quoted_contexts) 

                        if req.prompt and req.prompt.strip():
                            is_prompt_already_in_contexts = False
                            if new_contexts and new_contexts[-1].get('role') == 'user' and new_contexts[-1].get('content') == req.prompt:
                                is_prompt_already_in_contexts = True
                            if not is_prompt_already_in_contexts:
                                new_contexts.append({"role": "user", "content": req.prompt})
                        
                        req.contexts = new_contexts
                        req.prompt = " " 
                        plugin_logger.info(f"LLM请求预处理：已整合引用内容到 contexts。")
                        plugin_logger.debug(f"LLM请求预处理：新的 contexts: {req.contexts}")
                    else:
                        plugin_logger.info("LLM请求预处理：未构建有效的引用上下文条目。")
                        if system_prompt_entry and req.contexts : req.contexts.insert(0, system_prompt_entry) 
                else:
                    plugin_logger.info("LLM请求预处理：被引用的消息未解析出有效内容或处理后内容为空。")
            else:
                plugin_logger.warning(f"LLM请求预处理：调用 get_msg 失败或数据格式不符合预期: {replied_message_detail}")
        except Exception as e:
            plugin_logger.error(f"LLM请求预处理：处理QQ引用消息时发生错误: {e}", exc_info=True)

    # --- 新增指令组和指令 ---
    @filter.command_group("toolprompts_settings", alias={"tps"})
    @filter.permission_type(filter.PermissionType.ADMIN) # 指令组级别权限控制
    async def toolprompts_settings_group(self, event: AstrMessageEvent):
        """管理 Tool Prompts 插件的设置。"""
        # 当只输入主指令时，可以显示帮助信息或当前状态
        # 为简化，此处不处理，AstrBot默认会显示子指令列表
        pass

    @toolprompts_settings_group.command("provider_mode", alias={"pm"})
    async def set_provider_mode(self, event: AstrMessageEvent, mode: str):
        """
        设置多模态处理时使用的提供商模式。
        参数:
            mode (str): 'gemini' 或 'openai'
        """
        normalized_mode = mode.lower().strip()
        reply_msg = ""

        if normalized_mode == "gemini":
            self.config["is_gemini_provider_mode"] = True
            reply_msg = "提供商模式已成功设置为: Gemini。"
        elif normalized_mode == "openai":
            self.config["is_gemini_provider_mode"] = False
            reply_msg = "提供商模式已成功设置为: OpenAI (兼容)。"
        else:
            reply_msg = f"错误：无效的模式 '{mode}'。请使用 'gemini' 或 'openai'。"
            await event.send(event.plain_result(reply_msg))
            return

        try:
            self.config.save_config()
            plugin_logger.info(f"Provider mode set to {'Gemini' if self.config['is_gemini_provider_mode'] else 'OpenAI'} by admin {event.get_sender_id()}/{event.get_sender_name()}.")
            await event.send(event.plain_result(reply_msg))
        except Exception as e:
            plugin_logger.error(f"保存插件配置失败: {e}", exc_info=True)
            await event.send(event.plain_result("错误：保存配置失败，请检查后台日志。"))

    @toolprompts_settings_group.command("get_provider_mode", alias={"gpm"})
    async def get_provider_mode(self, event: AstrMessageEvent):
        """获取当前设置的提供商模式。"""
        is_gemini_mode = self.config.get("is_gemini_provider_mode", False)
        current_mode_str = "Gemini" if is_gemini_mode else "OpenAI (兼容)"
        await event.send(event.plain_result(f"当前提供商处理模式为: {current_mode_str}"))

    async def terminate(self):
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 正在终止...")
        if self.temp_media_dir:
            cleanup_minutes = self.config.get("temp_file_cleanup_minutes", 60)
            if cleanup_minutes > 0:
                plugin_logger.info(f"插件终止：执行最终临时文件清理，目录: {self.temp_media_dir}, 清理周期: {cleanup_minutes} 分钟。")
                cleanup_temp_files(self.temp_media_dir, cleanup_minutes) 
            else:
                 plugin_logger.info("插件终止：临时文件自动清理已禁用。")
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 已终止。")
