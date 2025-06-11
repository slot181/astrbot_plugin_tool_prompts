import re
import os
from pathlib import Path
import asyncio # 用于可能的异步操作

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse, ProviderRequest, BaseProvider # 确保导入 ProviderRequest, BaseProvider
from astrbot.api.config import AstrBotConfig # 用于类型提示
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent # 用于类型检查和获取client

from .utils import (
    get_temp_media_dir,
    download_media,
    get_mime_type,
    file_to_base64,
    cleanup_temp_files,
    plugin_logger # 使用 utils 中的 logger
)


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.1.8", "https://github.com/slot181/astrbot_plugin_tool_prompts")
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig): # 添加 config 参数
        super().__init__(context)
        self.config = config
        self.temp_media_dir = None

        # 设置日志级别
        log_level_str = self.config.get("log_level", "INFO").upper()
        plugin_logger.setLevel(log_level_str)
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 日志级别设置为: {log_level_str}")

        # 初始化并清理临时媒体目录
        if hasattr(self, 'data_dir_path') and self.data_dir_path:
            self.temp_media_dir = get_temp_media_dir(Path(self.data_dir_path))
            if self.temp_media_dir:
                cleanup_minutes = self.config.get("temp_file_cleanup_minutes", 60)
                if cleanup_minutes > 0:
                    plugin_logger.info(f"插件初始化：执行临时文件清理，目录: {self.temp_media_dir}, 清理周期: {cleanup_minutes} 分钟。")
                    cleanup_temp_files(self.temp_media_dir, cleanup_minutes)
                else:
                    plugin_logger.info("插件初始化：临时文件自动清理已禁用 (周期设置为0或更小)。")
        else:
            plugin_logger.warning("插件初始化：无法获取插件数据目录 (self.data_dir_path)，临时文件功能可能受限。")

        self.url_pattern = re.compile(r'(?:https?:)?//[^\s"\'`<>()[\]{}]+')
        self.path_pattern = re.compile(r'(?:[a-zA-Z]:\\|/)[^\s"\'`<>`()[\]{}]+')
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 初始化完成。")

    @filter.on_llm_response(priority=1)
    async def on_llm_response_handler(self, event: AstrMessageEvent, resp: LLMResponse):
        """统一处理LLM响应，包括工具调用通知和媒体链接转换"""
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

    async def _prepare_multimodal_parts(self, replied_message_segments: list, current_provider: BaseProvider) -> list:
        parts = []
        enable_gemini_native = self.config.get("enable_gemini_native_multimodal", False)
        enable_non_gemini_image = self.config.get("enable_non_gemini_multimodal_image", False)
        
        provider_name = "unknown"
        if current_provider:
            if hasattr(current_provider, 'get_provider_name'):
                provider_name = current_provider.get_provider_name().lower()
            elif hasattr(current_provider, 'name'):
                 provider_name = current_provider.name.lower()

        for seg_idx, seg_data in enumerate(replied_message_segments):
            seg_type = seg_data.get('type')
            seg_content_data = seg_data.get('data', {})
            media_url = seg_content_data.get('url')

            if seg_type == 'text' and seg_content_data.get('text'):
                parts.append({"type": "text", "text": seg_content_data['text'].strip()})
            elif seg_type == 'image' and media_url:
                if provider_name == "gemini" and enable_gemini_native:
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
                elif provider_name != "gemini" and enable_non_gemini_image:
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
                            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_data}"}, "original_url": media_url})
                        else:
                            parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，Base64编码失败，URL: {media_url}]"})
                    else:
                        parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，下载失败，URL: {media_url}]"})
                else:
                    parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1} URL: {media_url}]"})
            elif seg_type in ['record', 'video'] and media_url:
                media_kind = "语音" if seg_type == 'record' else "视频"
                if provider_name == "gemini" and enable_gemini_native:
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

                current_provider = self.context.get_using_provider()
                processed_parts = await self._prepare_multimodal_parts(replied_segments, current_provider)
                
                if processed_parts:
                    if req.contexts is None: req.contexts = []
                    system_prompt_entry = None
                    if req.contexts and req.contexts[0].get('role') == 'system':
                        system_prompt_entry = req.contexts.pop(0)

                    provider_name_for_check = "unknown"
                    if current_provider:
                        if hasattr(current_provider, 'get_provider_name'): provider_name_for_check = current_provider.get_provider_name().lower()
                        elif hasattr(current_provider, 'name'): provider_name_for_check = current_provider.name.lower()
                    
                    is_gemini_native_mode = (provider_name_for_check == "gemini" and self.config.get("enable_gemini_native_multimodal", False))
                    is_non_gemini_image_mode = (provider_name_for_check != "gemini" and self.config.get("enable_non_gemini_multimodal_image", False) and any(p.get("type") == "image_url" for p in processed_parts))

                    actual_quoted_contexts = []
                    prefix = f"用户 {event.get_sender_name()} 引用了 {original_sender_nickname} 的消息内容如下:\n\"\"\"\n"
                    suffix = "\n\"\"\""

                    if (is_gemini_native_mode and any(p.get("type") != "text" for p in processed_parts)) or is_non_gemini_image_mode:
                        # 构建多部分 content (Gemini parts 或 OpenAI image_url parts)
                        content_parts_for_llm = []
                        # 先添加描述性文本前缀
                        if prefix.strip(): content_parts_for_llm.append({"type": "text", "text": prefix.strip()})
                        
                        # 添加处理过的媒体和文本 parts
                        # 对于 Gemini，它期望 text 和 inline_data 交错。
                        # 对于 OpenAI，它期望 text 和 image_url 交错。
                        current_text_batch = []
                        for part_data in processed_parts:
                            if part_data.get("type") == "text":
                                current_text_batch.append(part_data.get("text",""))
                            else: # inline_data or image_url
                                if current_text_batch: # 先提交累积的文本
                                    content_parts_for_llm.append({"type": "text", "text": " ".join(current_text_batch)})
                                    current_text_batch = []
                                # 添加媒体 part
                                if part_data.get("type") == "inline_data" and is_gemini_native_mode: # Gemini
                                    content_parts_for_llm.append({"inline_data": {"mime_type": part_data.get("mime_type"), "data": part_data.get("data")}})
                                elif part_data.get("type") == "image_url" and is_non_gemini_image_mode: # OpenAI
                                    content_parts_for_llm.append(part_data) #  {"type": "image_url", "image_url": ...}
                                else: # 媒体无法按预期模式处理，转为文本描述
                                     current_text_batch.append(f"[{part_data.get('type')} @ {part_data.get('original_url', '未知URL')}]")


                        if current_text_batch: # 添加末尾的文本
                            content_parts_for_llm.append({"type": "text", "text": " ".join(current_text_batch)})
                        
                        # 添加描述性文本后缀
                        if suffix.strip(): content_parts_for_llm.append({"type": "text", "text": suffix.strip()})

                        if content_parts_for_llm:
                             actual_quoted_contexts.append({"role": "user", "content": content_parts_for_llm})
                        else: # 如果没有有效 parts，则不添加此上下文
                            plugin_logger.info("LLM请求预处理：多模态处理后内容为空。")


                    else: # 纯文本模式
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
                        new_contexts.extend(req.contexts) # 原始历史
                        new_contexts.extend(actual_quoted_contexts) # 引用内容

                        if req.prompt and req.prompt.strip():
                            is_prompt_already_in_contexts = False
                            if new_contexts and new_contexts[-1].get('role') == 'user' and new_contexts[-1].get('content') == req.prompt:
                                is_prompt_already_in_contexts = True
                            if not is_prompt_already_in_contexts:
                                new_contexts.append({"role": "user", "content": req.prompt})
                        
                        req.contexts = new_contexts
                        req.prompt = " " # 按用户要求
                        plugin_logger.info(f"LLM请求预处理：已整合引用内容到 contexts。")
                        plugin_logger.debug(f"LLM请求预处理：新的 contexts 长度: {len(req.contexts)}")
                    else:
                        plugin_logger.info("LLM请求预处理：未构建有效的引用上下文条目。")
                        if system_prompt_entry and req.contexts : req.contexts.insert(0, system_prompt_entry) # 恢复 system prompt

                else:
                    plugin_logger.info("LLM请求预处理：被引用的消息未解析出有效内容或处理后内容为空。")
            else:
                plugin_logger.warning(f"LLM请求预处理：调用 get_msg 失败或数据格式不符合预期: {replied_message_detail}")
        except Exception as e:
            plugin_logger.error(f"LLM请求预处理：处理QQ引用消息时发生错误: {e}", exc_info=True)

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
