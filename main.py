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
    plugin_logger,
    call_gemini_api
)

# 从新创建的适配器文件中导入处理函数
from .tool_adapter import process_tool_response_from_history


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.3.10", "https://github.com/slot181/astrbot_plugin_tool_prompts") # 版本号更新
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temp_media_dir = None
        self._cleanup_task = None 
        self.processed_tool_call_ids = set() 

        log_level_str = self.config.get("log_level", "INFO").upper()
        plugin_logger.setLevel(log_level_str)
        
        plugin_name_for_path = "astrbot_plugin_tool_prompts"
        if hasattr(self, 'metadata') and self.metadata and hasattr(self.metadata, 'name') and self.metadata.name:
            plugin_name_for_path = self.metadata.name
        else:
            plugin_logger.warning("无法从 self.metadata.name 获取插件名，将使用默认名 'astrbot_plugin_tool_prompts' 构建数据路径。")

        base_data_path = Path("./data/plugins_data") / plugin_name_for_path
        
        try:
            self.temp_media_dir = get_temp_media_dir(base_data_path)
            
            if self.temp_media_dir:
                plugin_logger.info(f"临时媒体目录已初始化: {self.temp_media_dir.resolve()}")
                initial_cleanup_minutes = self.config.get("temp_file_cleanup_minutes", 60)
                if initial_cleanup_minutes > 0:
                    plugin_logger.info(f"插件初始化：执行一次性临时文件清理，目录: {self.temp_media_dir}, 清理周期: {initial_cleanup_minutes} 分钟。")
                    cleanup_temp_files(self.temp_media_dir, initial_cleanup_minutes)
                
                if initial_cleanup_minutes > 0:
                    self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task(initial_cleanup_minutes))
                    plugin_logger.info(f"已启动定时清理任务，每 {initial_cleanup_minutes} 分钟执行一次。")
                else:
                    plugin_logger.info("插件初始化：定时临时文件自动清理已禁用 (周期设置为0或更小)。")
            else:
                plugin_logger.error(f"临时媒体目录未能成功初始化于: {base_data_path}")
        except Exception as e:
            plugin_logger.error(f"创建插件数据基础路径 {base_data_path} 或临时媒体目录失败: {e}", exc_info=True)
            self.temp_media_dir = None
        
        # 为 Gemini 工具读取配置
        self.gemini_api_key = self.config.get("gemini_api_key", None)
        self.gemini_model_name_for_media = self.config.get("gemini_model_name_for_media", "gemini-2.0-flash-exp")
        self.gemini_base_url = self.config.get("gemini_base_url", "https://generativelanguage.googleapis.com").rstrip('/')
        
        if not self.gemini_api_key:
            plugin_logger.warning("Gemini API Key 未在插件配置中设置。understand_media_from_reply 工具将无法工作。")
        if not self.gemini_base_url:
            plugin_logger.warning("Gemini Base URL 未在插件配置中设置。understand_media_from_reply 工具将使用默认值或可能失败。")

        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 初始化完成。")

    async def _periodic_cleanup_task(self, cleanup_interval_minutes: int):
        if not self.temp_media_dir or cleanup_interval_minutes <= 0:
            plugin_logger.info("定时清理任务：临时目录无效或清理周期不合法，任务不执行。")
            return
        
        wait_seconds = cleanup_interval_minutes * 60
        plugin_logger.info(f"定时清理任务已启动，每 {cleanup_interval_minutes} 分钟 (即 {wait_seconds} 秒) 清理一次目录: {self.temp_media_dir}")
        while True:
            try:
                await asyncio.sleep(wait_seconds)
                plugin_logger.info(f"定时清理任务：开始执行临时文件清理。")
                cleanup_temp_files(self.temp_media_dir, cleanup_interval_minutes)
            except asyncio.CancelledError:
                plugin_logger.info("定时清理任务已被取消。")
                break
            except Exception as e:
                plugin_logger.error(f"定时清理任务在执行过程中发生错误: {e}", exc_info=True)
                await asyncio.sleep(60)

    @filter.llm_tool(name="understand_media_from_reply")
    async def understand_media_from_reply(self, event: AstrMessageEvent, prompt: str) -> typing.AsyncGenerator[Comp.BaseMessageComponent, None]:
        plugin_logger.info(f"LLM工具 'understand_media_from_reply' 被调用，提示: {prompt}")

        if not self.gemini_api_key:
            plugin_logger.error("understand_media_from_reply: Gemini API Key 未配置。")
            yield event.plain_result("错误：Gemini API Key 未配置，无法处理媒体文件。")
            return

        if not self.temp_media_dir:
            plugin_logger.error("understand_media_from_reply: 临时媒体目录未初始化。")
            yield event.plain_result("错误：插件临时目录未正确初始化，无法处理媒体文件。")
            return

        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("错误：此工具目前仅支持 QQ 平台（aiocqhttp）。")
            return

        raw_message_chain = event.message_obj.message
        reply_message_id_str = None
        for segment in raw_message_chain:
            if isinstance(segment, Comp.Reply):
                reply_message_id_str = str(segment.data.get('id')) if hasattr(segment, 'data') and isinstance(segment.data, dict) else str(getattr(segment, 'id', None))
                break
        
        if not reply_message_id_str:
            yield event.plain_result("错误：此工具需要引用一条消息才能工作。")
            return

        try:
            client = None
            if isinstance(event, AiocqhttpMessageEvent):
                client = event.bot
            else:
                platform_adapter = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform_adapter: client = getattr(platform_adapter, 'client', getattr(platform_adapter, 'get_client', lambda: None)())

            if not client:
                plugin_logger.error("understand_media_from_reply: 无法获取 aiocqhttp 客户端。")
                yield event.plain_result("错误：无法连接到 QQ 平台，请检查插件或 AstrBot 配置。")
                return

            replied_message_detail = await client.api.call_action('get_msg', message_id=int(reply_message_id_str))
            
            if not (isinstance(replied_message_detail, dict) and 'message' in replied_message_detail):
                plugin_logger.warning(f"understand_media_from_reply: 获取引用消息详情失败或格式不符。ID: {reply_message_id_str}")
                yield event.plain_result("错误：无法获取被引用的消息详情。")
                return

            replied_segments = replied_message_detail.get('message', [])
            media_url = None
            media_type_for_gemini = None

            for seg in replied_segments:
                seg_type = seg.get('type')
                seg_data = seg.get('data', {})
                url = seg_data.get('url')
                
                if seg_type == 'video' and url:
                    media_url = url
                    media_type_for_gemini = "video/mp4"
                    plugin_logger.info(f"understand_media_from_reply: 在引用消息中找到视频: {media_url}")
                    break 
                elif seg_type == 'record' and url:
                    media_url = url
                    media_type_for_gemini = "audio/mp3"
                    plugin_logger.info(f"understand_media_from_reply: 在引用消息中找到语音: {media_url}")
                    break

            if not media_url or not media_type_for_gemini:
                yield event.plain_result("错误：引用的消息中未找到支持的视频或语音文件，或者文件URL无效。")
                return

            downloaded_file_path = await download_media(media_url, self.temp_media_dir, "gemini_media_")
            if not downloaded_file_path:
                plugin_logger.error(f"understand_media_from_reply: 下载媒体文件失败: {media_url}")
                yield event.plain_result(f"错误：无法下载引用的媒体文件: {media_url}")
                return

            try:
                file_size = downloaded_file_path.stat().st_size
                max_size_bytes = 20 * 1024 * 1024 
                if file_size > max_size_bytes:
                    plugin_logger.warning(f"understand_media_from_reply: 文件 {downloaded_file_path} 过大 ({file_size} bytes > {max_size_bytes} bytes)。")
                    yield event.plain_result(f"错误：引用的媒体文件大小超过20MB限制，无法处理。")
                    if downloaded_file_path.exists():
                        downloaded_file_path.unlink()
                    return
            except Exception as e_stat:
                plugin_logger.error(f"understand_media_from_reply: 检查文件大小时出错: {downloaded_file_path}, {e_stat}", exc_info=True)
                yield event.plain_result("错误：检查媒体文件大小时发生内部错误。")
                if downloaded_file_path.exists():
                    downloaded_file_path.unlink()
                return

            actual_mime_type = get_mime_type(downloaded_file_path)
            if media_type_for_gemini == "audio/mp3" and actual_mime_type and "audio" in actual_mime_type:
                 if actual_mime_type not in ["audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg", "audio/flac", "audio/aac"]:
                     plugin_logger.warning(f"understand_media_from_reply: 下载的语音文件MIME类型为 {actual_mime_type}，将尝试作为 audio/mp3 发送给Gemini。")
                 else:
                     media_type_for_gemini = actual_mime_type
            elif not actual_mime_type:
                 plugin_logger.warning(f"understand_media_from_reply: 无法检测下载文件的MIME类型: {downloaded_file_path}。将使用预设的 {media_type_for_gemini}")

            base64_content = file_to_base64(downloaded_file_path)
            if not base64_content:
                plugin_logger.error(f"understand_media_from_reply: 文件转 Base64 失败: {downloaded_file_path}")
                yield event.plain_result("错误：无法处理下载的媒体文件（Base64编码失败）。")
                return
            
            gemini_model = self.gemini_model_name_for_media
            plugin_logger.info(f"understand_media_from_reply: 使用模型 '{gemini_model}' 调用 Gemini API。")
            
            api_response_text = await call_gemini_api(
                base_url=self.gemini_base_url,
                api_key=self.gemini_api_key,
                model_name=gemini_model,
                mime_type=media_type_for_gemini,
                base64_data=base64_content,
                user_prompt=prompt
            )

            if api_response_text:
                yield event.plain_result(api_response_text)
            else:
                yield event.plain_result("错误：调用 Gemini API 理解媒体失败，或未返回有效文本。")

        except Exception as e:
            plugin_logger.error(f"understand_media_from_reply: 处理过程中发生错误: {e}", exc_info=True)
            yield event.plain_result(f"处理引用媒体时发生内部错误: {str(e)}")
        finally:
            if 'downloaded_file_path' in locals() and downloaded_file_path and downloaded_file_path.exists():
                try:
                    downloaded_file_path.unlink()
                    plugin_logger.debug(f"understand_media_from_reply: 已清理临时文件 {downloaded_file_path}")
                except Exception as e_clean:
                    plugin_logger.error(f"understand_media_from_reply: 清理临时文件失败 {downloaded_file_path}: {e_clean}")

    @filter.on_llm_response(priority=1)
    async def on_llm_response_handler(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM响应后处理：仅发送工具调用通知。"""
        
        if resp.role == "tool" and resp.tools_call_name:
            plugin_logger.info("LLM响应处理器：检测到工具调用。")
            for tool_name in resp.tools_call_name:
                message = f"正在调用 {tool_name} 工具中……"
                await event.send(event.plain_result(message))
            return 

    @filter.after_message_sent(priority=0)
    async def handle_message_sent_for_tool_response(self, event: AstrMessageEvent):
        """
        在消息发送后触发，用于调用 tool_adapter 中的逻辑，
        该逻辑会检查会话历史以处理特定的工具响应。
        """
        # plugin_logger.critical("!!!!!! MAIN: after_message_sent HOOK TRIGGERED !!!!!!") # 保留此日志用于调试
        await process_tool_response_from_history(self, event)

    async def _prepare_multimodal_parts(self, replied_message_segments: list) -> list:
        parts = []
        multimodal_processing_enabled = self.config.get("enable_multimodal_processing", False)

        if multimodal_processing_enabled:
            plugin_logger.info("多模态处理已启用。")
        else:
            plugin_logger.info("多模态处理已禁用。")

        for seg_idx, seg_data in enumerate(replied_message_segments):
            seg_type = seg_data.get('type')
            seg_content_data = seg_data.get('data', {})
            media_url = seg_content_data.get('url')

            if seg_type == 'text' and seg_content_data.get('text'):
                parts.append({"type": "text", "text": seg_content_data['text'].strip()})
            elif seg_type == 'image' and media_url:
                if multimodal_processing_enabled:
                    if not self.temp_media_dir:
                        plugin_logger.warning(f"图片多模态处理跳过：临时目录未初始化。URL: {media_url}")
                        parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，下载失败，URL: {media_url}]"})
                        continue
                    downloaded_file = await download_media(media_url, self.temp_media_dir, "img_")
                    if downloaded_file:
                        mime_type = get_mime_type(downloaded_file) or "image/jpeg"
                        base64_data = file_to_base64(downloaded_file)
                        if base64_data:
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{base64_data}"}
                            })
                            parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1} URL: {media_url}]"})
                        else:
                            parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，Base64编码失败，URL: {media_url}]"})
                    else:
                        parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，下载失败，URL: {media_url}]"})
                else: 
                    parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1} URL: {media_url}]"})
            elif seg_type in ['record', 'video'] and media_url:
                media_kind = "语音" if seg_type == 'record' else "视频"
                parts.append({"type": "text", "text": f"[引用的{media_kind}{seg_idx+1} URL: {media_url} ({'内容未转录' if seg_type == 'record' else ''})]"})
        return parts

    @filter.on_llm_request(priority=1)
    async def on_llm_request_handler(self, event: AstrMessageEvent, req: ProviderRequest):
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
                    plugin_logger.debug(f"LLM请求预处理：检测到QQ引用消息，ID: {reply_message_id_str}")
                else:
                    plugin_logger.warning(f"LLM请求预处理：找到Reply段，但无法确定其message_id。段内容: {segment}")
                break

        if not reply_message_id_str:
            return

        plugin_logger.info(f"LLM请求预处理：处理引用消息 ID: {reply_message_id_str}")
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

            replied_message_detail = await client.api.call_action('get_msg', message_id=int(reply_message_id_str))
            
            if isinstance(replied_message_detail, dict) and 'message_id' in replied_message_detail and 'message' in replied_message_detail:
                original_sender_nickname = replied_message_detail.get('sender', {}).get('card') or replied_message_detail.get('sender', {}).get('nickname', '未知用户')
                replied_segments = replied_message_detail.get('message', [])
                if not isinstance(replied_segments, list): replied_segments = []
                
                processed_parts = await self._prepare_multimodal_parts(replied_segments)
                
                if processed_parts:
                    if req.contexts is None: req.contexts = []
                    system_prompt_entry = None
                    if req.contexts and req.contexts[0].get('role') == 'system':
                        system_prompt_entry = req.contexts.pop(0)
                    
                    multimodal_processing_enabled = self.config.get("enable_multimodal_processing", False)
                    should_form_multimodal_request = multimodal_processing_enabled and any(p.get("type") == "image_url" for p in processed_parts)
                    
                    actual_quoted_contexts = []
                    prefix = f"用户 {event.get_sender_name()} 引用了 {original_sender_nickname} 的消息内容如下:\n\"\"\"\n"
                    suffix = "\n\"\"\""

                    if should_form_multimodal_request:
                        content_parts_for_llm = []
                        if prefix.strip(): content_parts_for_llm.append({"type": "text", "text": prefix.strip()})
                        current_text_batch = []
                        for part_data in processed_parts:
                            if part_data.get("type") == "text":
                                current_text_batch.append(part_data.get("text","").strip())
                            elif part_data.get("type") == "image_url":
                                if current_text_batch:
                                    combined_text = " ".join([s for s in current_text_batch if s])
                                    if combined_text: content_parts_for_llm.append({"type": "text", "text": combined_text})
                                    current_text_batch = []
                                content_parts_for_llm.append(part_data)
                        if current_text_batch:
                            combined_text = " ".join([s for s in current_text_batch if s])
                            if combined_text: content_parts_for_llm.append({"type": "text", "text": combined_text})
                        if suffix.strip(): content_parts_for_llm.append({"type": "text", "text": suffix.strip()})
                        if content_parts_for_llm:
                             actual_quoted_contexts.append({"role": "user", "content": content_parts_for_llm})
                    else: 
                        all_text_from_parts = []
                        for part_data in processed_parts:
                            if part_data.get("type") == "text":
                                all_text_from_parts.append(part_data.get("text","").strip())
                            elif part_data.get("type") == "image_url":
                                 all_text_from_parts.append(f"[引用的图片 URL: {part_data.get('image_url',{}).get('url','未知URL')}]")
                        full_quoted_text = " ".join([s for s in all_text_from_parts if s]).strip()
                        if full_quoted_text:
                            actual_quoted_contexts.append({"role": "user", "content": prefix + full_quoted_text + suffix})

                    if actual_quoted_contexts:
                        new_contexts = []
                        if system_prompt_entry: new_contexts.append(system_prompt_entry)
                        new_contexts.extend(req.contexts) 
                        new_contexts.extend(actual_quoted_contexts) 
                        req.contexts = new_contexts
                        plugin_logger.info(f"LLM请求预处理：已整合引用内容到 contexts。")
                    else:
                        if system_prompt_entry and req.contexts : req.contexts.insert(0, system_prompt_entry) 
            else:
                plugin_logger.warning(f"LLM请求预处理：调用 get_msg 失败或数据格式不符合预期: {replied_message_detail}")
        except Exception as e:
            plugin_logger.error(f"LLM请求预处理：处理QQ引用消息时发生错误: {e}", exc_info=True)

    @filter.command_group("toolprompts_settings", alias={"tps"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toolprompts_settings_group(self, event: AstrMessageEvent):
        """管理插件的多模态处理设置 (简写: /tps)。"""
        pass

    @toolprompts_settings_group.command("set_multimodal", alias={"sm"})
    async def set_multimodal_status(self, event: AstrMessageEvent, status: str):
        normalized_status = status.lower().strip()
        reply_msg = ""
        new_status = None

        if normalized_status in ["on", "true", "enable", "1"]:
            new_status = True
            reply_msg = "引用消息的多模态处理已启用。"
        elif normalized_status in ["off", "false", "disable", "0"]:
            new_status = False
            reply_msg = "引用消息的多模态处理已禁用。"
        else:
            reply_msg = f"错误：无效的状态 '{status}'。请使用 'on'/'true'/'enable'/'1' 或 'off'/'false'/'disable'/'0'。"
            await event.send(event.plain_result(reply_msg))
            return

        self.config["enable_multimodal_processing"] = new_status
        try:
            self.config.save_config()
            plugin_logger.info(f"多模态处理状态已由管理员 {event.get_sender_name()} 设置为: {new_status}。")
            await event.send(event.plain_result(reply_msg))
        except Exception as e:
            plugin_logger.error(f"保存插件配置失败 (set_multimodal_status): {e}", exc_info=True)
            await event.send(event.plain_result("错误：保存配置失败，请检查后台日志。"))

    @toolprompts_settings_group.command("get_multimodal_status", alias={"gms"})
    async def get_multimodal_status(self, event: AstrMessageEvent):
        multimodal_enabled = self.config.get("enable_multimodal_processing", False)
        status_str = "已启用" if multimodal_enabled else "已禁用"
        await event.send(event.plain_result(f"当前引用消息的多模态处理状态为: {status_str}"))

    async def terminate(self):
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 正在终止...")
        if self._cleanup_task and not self._cleanup_task.done():
            plugin_logger.info("正在取消定时清理任务...")
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                plugin_logger.info("定时清理任务已成功取消。")
            except Exception as e:
                plugin_logger.error(f"等待定时清理任务取消时发生错误: {e}", exc_info=True)
        
        if self.temp_media_dir:
            cleanup_minutes = self.config.get("temp_file_cleanup_minutes", 60)
            if cleanup_minutes > 0:
                plugin_logger.info(f"插件终止：执行最终临时文件清理，目录: {self.temp_media_dir}, 清理周期: {cleanup_minutes} 分钟。")
                cleanup_temp_files(self.temp_media_dir, cleanup_minutes) 
            else:
                 plugin_logger.info("插件终止：最终临时文件清理已禁用。")
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 已终止。")
