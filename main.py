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
    call_gemini_api # 新增导入
)

# 从新创建的适配器文件中导入处理函数
from .tool_adapter import process_tool_response_from_history # Updated import


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.3.8", "https://github.com/slot181/astrbot_plugin_tool_prompts") # 版本号更新
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temp_media_dir = None
        self._cleanup_task = None # 用于存储定时清理任务的引用
        self.processed_tool_call_ids = set() # 新增：用于跟踪已处理的工具调用ID

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
                # 启动时执行一次清理
                initial_cleanup_minutes = self.config.get("temp_file_cleanup_minutes", 60)
                if initial_cleanup_minutes > 0:
                    plugin_logger.info(f"插件初始化：执行一次性临时文件清理，目录: {self.temp_media_dir}, 清理周期: {initial_cleanup_minutes} 分钟。")
                    cleanup_temp_files(self.temp_media_dir, initial_cleanup_minutes)
                
                # 启动定时清理任务
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

        self.url_pattern = re.compile(r'(?:https?:)?//[^\s"\'`<>()[\]{}]+')
        self.path_pattern = re.compile(r'(?:[a-zA-Z]:\\|/)[^\s"\'`<>`()[\]{}]+')
        
        # 为 Gemini 工具读取配置
        self.gemini_api_key = self.config.get("gemini_api_key", None)
        self.gemini_model_name_for_media = self.config.get("gemini_model_name_for_media", "gemini-2.0-flash-exp") # 默认模型已改回
        self.gemini_base_url = self.config.get("gemini_base_url", "https://generativelanguage.googleapis.com").rstrip('/') # 新增基础URL配置
        
        if not self.gemini_api_key:
            plugin_logger.warning("Gemini API Key 未在插件配置中设置。understand_media_from_reply 工具将无法工作。")
        if not self.gemini_base_url:
            plugin_logger.warning("Gemini Base URL 未在插件配置中设置。understand_media_from_reply 工具将使用默认值或可能失败。")


        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 初始化完成。")

    async def _periodic_cleanup_task(self, cleanup_interval_minutes: int):
        """定期清理临时文件的后台任务"""
        if not self.temp_media_dir or cleanup_interval_minutes <= 0:
            plugin_logger.info("定时清理任务：临时目录无效或清理周期不合法，任务不执行。")
            return
        
        wait_seconds = cleanup_interval_minutes * 60
        plugin_logger.info(f"定时清理任务已启动，每 {cleanup_interval_minutes} 分钟 (即 {wait_seconds} 秒) 清理一次目录: {self.temp_media_dir}")
        while True:
            try:
                await asyncio.sleep(wait_seconds)
                plugin_logger.info(f"定时清理任务：开始执行临时文件清理。")
                cleanup_temp_files(self.temp_media_dir, cleanup_interval_minutes) # 使用相同的间隔作为最大年龄
            except asyncio.CancelledError:
                plugin_logger.info("定时清理任务已被取消。")
                break
            except Exception as e:
                plugin_logger.error(f"定时清理任务在执行过程中发生错误: {e}", exc_info=True)
                # 发生错误后，可以考虑是继续尝试还是终止任务
                # 为了简单起见，这里继续尝试，但可以添加错误计数和退出逻辑
                await asyncio.sleep(60) # 发生错误后等待1分钟再试，避免快速连续失败

    @filter.llm_tool(name="understand_media_from_reply")
    async def understand_media_from_reply(self, event: AstrMessageEvent, prompt: str) -> typing.AsyncGenerator[Comp.BaseMessageComponent, None]:
        """
        通过 Gemini API 理解引用消息中的视频或语音文件内容。
        重要：此工具仅在用户回复了一条包含单个视频或语音的消息时才能被调用。
        如果引用的消息不符合条件，或者媒体处理失败，将返回错误信息。

        Args:
            prompt (string): 对 Gemini 模型的指示，例如“总结这个视频”或“转录这段语音的主要内容”，应根据用户意图生成此提示。
        """
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
            else: # 尝试从 context 获取
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
            media_type_for_gemini = None # "video/mp4" or "audio/mp3"

            for seg in replied_segments:
                seg_type = seg.get('type')
                seg_data = seg.get('data', {})
                url = seg_data.get('url')
                file_ext = Path(url).suffix.lower() if url else ""

                if seg_type == 'video' and url:
                    media_url = url
                    media_type_for_gemini = "video/mp4"
                    plugin_logger.info(f"understand_media_from_reply: 在引用消息中找到视频: {media_url}")
                    break 
                elif seg_type == 'record' and url:
                    media_url = url
                    # Gemini 可能更喜欢 mp3 或其他常见音频格式，但这里我们先用 mp3 作为目标
                    # 实际的 MIME 类型应由 get_mime_type 确定，或根据需要进行转换
                    media_type_for_gemini = "audio/mp3" # 假设是mp3，如果原始是silk等需要转换
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

            # 检查文件大小
            try:
                file_size = downloaded_file_path.stat().st_size
                max_size_bytes = 20 * 1024 * 1024  # 20MB
                if file_size > max_size_bytes:
                    plugin_logger.warning(f"understand_media_from_reply: 文件 {downloaded_file_path} 过大 ({file_size} bytes > {max_size_bytes} bytes)。")
                    yield event.plain_result(f"错误：引用的媒体文件大小超过20MB限制，无法处理。")
                    if downloaded_file_path.exists(): # 清理已下载的大文件
                        downloaded_file_path.unlink()
                    return
            except Exception as e_stat:
                plugin_logger.error(f"understand_media_from_reply: 检查文件大小时出错: {downloaded_file_path}, {e_stat}", exc_info=True)
                yield event.plain_result("错误：检查媒体文件大小时发生内部错误。")
                if downloaded_file_path.exists(): # 清理
                    downloaded_file_path.unlink()
                return

            # 对于语音，如果原始格式不是mp3 (例如 .silk, .amr)，理想情况下这里应该有转换步骤
            # 目前假设下载的文件可以直接使用，或者 Gemini 支持其原始MIME类型
            actual_mime_type = get_mime_type(downloaded_file_path)
            if media_type_for_gemini == "audio/mp3" and actual_mime_type and "audio" in actual_mime_type:
                 # 如果是音频，使用实际检测到的MIME类型，除非它是非常规的且Gemini不支持
                 # 这里简化处理，如果下载的是常见音频，直接用其MIME，否则坚持audio/mp3并期望Gemini能处理
                 if actual_mime_type not in ["audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg", "audio/flac", "audio/aac"]: # 常见类型
                     plugin_logger.warning(f"understand_media_from_reply: 下载的语音文件MIME类型为 {actual_mime_type}，将尝试作为 audio/mp3 发送给Gemini。")
                 else:
                     media_type_for_gemini = actual_mime_type # 使用更准确的MIME类型
            elif not actual_mime_type: # 如果无法检测MIME
                 plugin_logger.warning(f"understand_media_from_reply: 无法检测下载文件的MIME类型: {downloaded_file_path}。将使用预设的 {media_type_for_gemini}")


            base64_content = file_to_base64(downloaded_file_path)
            if not base64_content:
                plugin_logger.error(f"understand_media_from_reply: 文件转 Base64 失败: {downloaded_file_path}")
                yield event.plain_result("错误：无法处理下载的媒体文件（Base64编码失败）。")
                return
            
            # 调用 Gemini API
            gemini_model = self.gemini_model_name_for_media
            plugin_logger.info(f"understand_media_from_reply: 使用模型 '{gemini_model}' 调用 Gemini API。")
            
            api_response_text = await call_gemini_api(
                base_url=self.gemini_base_url,
                api_key=self.gemini_api_key,
                model_name=gemini_model,
                mime_type=media_type_for_gemini, # 使用选择或检测到的MIME
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
            # 可以在这里添加临时文件清理逻辑，如果downloaded_file_path存在
            if 'downloaded_file_path' in locals() and downloaded_file_path and downloaded_file_path.exists():
                try:
                    downloaded_file_path.unlink()
                    plugin_logger.debug(f"understand_media_from_reply: 已清理临时文件 {downloaded_file_path}")
                except Exception as e_clean:
                    plugin_logger.error(f"understand_media_from_reply: 清理临时文件失败 {downloaded_file_path}: {e_clean}")


    @filter.on_llm_response(priority=1)
    async def on_llm_response_handler(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM响应后处理：发送工具调用通知，处理并分条发送媒体链接。"""
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

    # 移除旧的 on_llm_response 钩子 adapt_gemini_search_response
    # @filter.on_llm_response(priority=0)
    # async def adapt_gemini_search_response(self, event: AstrMessageEvent, resp: LLMResponse):
    #     """
    #     钩子函数，用于调用 tool_adapter 中的逻辑来处理特定工具的响应。
    #     """
    #     await handle_gemini_search_tool_response(self, event, resp) # 旧的调用

    @filter.after_message_sent(priority=0) # 新增 after_message_sent 钩子
    async def handle_message_sent_for_tool_response(self, event: AstrMessageEvent):
        """
        在消息发送后触发，用于调用 tool_adapter 中的逻辑，
        该逻辑会检查会话历史以处理特定的工具响应。
        """
        await process_tool_response_from_history(self, event)

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
            plugin_logger.debug(f"媒体处理：识别为图片: {path_or_url}")
            return Comp.Image.fromURL(path_or_url) if is_url else Comp.Image.fromFileSystem(path_or_url)
        if any(path_or_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi']):
            plugin_logger.debug(f"媒体处理：识别为视频: {path_or_url}")
            return Comp.Video.fromURL(path_or_url) if is_url else Comp.Video.fromFileSystem(path_or_url)
        if path_or_url.lower().endswith('.wav'):
            plugin_logger.debug(f"媒体处理：识别为音频: {path_or_url}")
            return Comp.Record(url=path_or_url) if is_url else Comp.Record(file=path_or_url)
        if any(path_or_url.lower().endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.txt']):
            plugin_logger.debug(f"媒体处理：识别为文档: {path_or_url}")
            return Comp.File(url=path_or_url, name=os.path.basename(path_or_url)) if is_url else Comp.File(file=path_or_url, name=os.path.basename(path_or_url))
        plugin_logger.debug(f"媒体处理：路径 '{path_or_url}' 未匹配任何已知媒体类型，将作为纯文本处理。")
        return Comp.Plain(text=path_or_url)

    async def _prepare_multimodal_parts(self, replied_message_segments: list) -> list:
        parts = []
        # 读取新的统一多模态处理开关
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
                            plugin_logger.debug(f"多模态启用：准备图片 part (data URI) for {media_url}")
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{base64_data}"}
                            })
                            # 同时添加原始 URL 作为文本
                            parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1} URL: {media_url}]"})
                            plugin_logger.debug(f"图片 part (original_url: {media_url}) 已准备，将发送给LLM的结构: {parts[-2:]}")
                        else:
                            parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，Base64编码失败，URL: {media_url}]"})
                    else:
                        parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1}，下载失败，URL: {media_url}]"})
                else: # multimodal_processing_enabled is False
                    parts.append({"type": "text", "text": f"[引用的图片{seg_idx+1} URL: {media_url}]"})
            elif seg_type in ['record', 'video'] and media_url:
                media_kind = "语音" if seg_type == 'record' else "视频"
                # 语音和视频统一转换为文本描述
                parts.append({"type": "text", "text": f"[引用的{media_kind}{seg_idx+1} URL: {media_url} ({'内容未转录' if seg_type == 'record' else ''})]"})
        return parts

    @filter.on_llm_request(priority=1)
    async def on_llm_request_handler(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM请求前处理：处理引用消息中的媒体，注入上下文。"""
        # 从此处移除了 cleanup_temp_files 的调用，将改为定时任务

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
                
                # 仅当成功解析出 reply_message_id_str 时才记录检测到引用
                if reply_message_id_str:
                    plugin_logger.debug(f"LLM请求预处理：检测到QQ引用消息，ID: {reply_message_id_str}")
                else: # 如果 segment 是 Reply 但没有有效 id
                    plugin_logger.warning(f"LLM请求预处理：找到Reply段，但无法确定其message_id。段内容: {segment}")
                break # 无论是否成功解析id，只要是Reply段就跳出循环

        if not reply_message_id_str: # 如果没有引用，则正常返回
            return

        # 后续逻辑仅在检测到有效引用ID时执行
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

            # plugin_logger.info(f"LLM请求预处理：尝试获取被引用消息详情，ID: {reply_message_id_str}") # 此日志有些冗余，上面已有处理引用的日志
            replied_message_detail = await client.api.call_action('get_msg', message_id=int(reply_message_id_str))
            
            if isinstance(replied_message_detail, dict) and 'message_id' in replied_message_detail and 'message' in replied_message_detail:
                plugin_logger.debug(f"LLM请求预处理：成功获取被引用消息详情。")
                original_sender_nickname = replied_message_detail.get('sender', {}).get('card') or replied_message_detail.get('sender', {}).get('nickname', '未知用户')
                replied_segments = replied_message_detail.get('message', [])
                if not isinstance(replied_segments, list): replied_segments = []
                
                processed_parts = await self._prepare_multimodal_parts(replied_segments)
                
                if processed_parts:
                    if req.contexts is None: req.contexts = []
                    system_prompt_entry = None
                    if req.contexts and req.contexts[0].get('role') == 'system':
                        system_prompt_entry = req.contexts.pop(0)
                    
                    # 获取插件配置中的统一多模态开关状态
                    multimodal_processing_enabled = self.config.get("enable_multimodal_processing", False)
                    plugin_logger.debug(f"on_llm_request_handler: multimodal_processing_enabled={multimodal_processing_enabled}")

                    # 判断是否应该构建多模态请求 (即开关打开且处理后的parts里确实有图片)
                    should_form_multimodal_request = multimodal_processing_enabled and any(p.get("type") == "image_url" for p in processed_parts)
                    
                    plugin_logger.debug(f"on_llm_request_handler: should_form_multimodal_request={should_form_multimodal_request}")
                    
                    actual_quoted_contexts = []
                    prefix = f"用户 {event.get_sender_name()} 引用了 {original_sender_nickname} 的消息内容如下:\n\"\"\"\n"
                    suffix = "\n\"\"\""

                    if should_form_multimodal_request:
                        # 构建多模态请求内容 (包含图片和文本)
                        content_parts_for_llm = []
                        if prefix.strip(): content_parts_for_llm.append({"type": "text", "text": prefix.strip()})
                        
                        current_text_batch = []
                        for part_data in processed_parts:
                            if part_data.get("type") == "text":
                                current_text_batch.append(part_data.get("text","").strip())
                            elif part_data.get("type") == "image_url": # 只处理 image_url 类型
                                if current_text_batch: # 先添加累积的文本
                                    combined_text = " ".join([s for s in current_text_batch if s])
                                    if combined_text:
                                        content_parts_for_llm.append({"type": "text", "text": combined_text})
                                    current_text_batch = []
                                content_parts_for_llm.append(part_data) # 添加 image_url part
                            # 其他类型的 part (如语音/视频的文本描述) 会被包含在 current_text_batch 中
                        
                        if current_text_batch: # 添加末尾剩余的文本
                            combined_text = " ".join([s for s in current_text_batch if s])
                            if combined_text:
                                content_parts_for_llm.append({"type": "text", "text": combined_text})
                        
                        if suffix.strip(): content_parts_for_llm.append({"type": "text", "text": suffix.strip()})

                        if content_parts_for_llm:
                             actual_quoted_contexts.append({"role": "user", "content": content_parts_for_llm})
                             plugin_logger.info("LLM请求预处理：已构建多模态引用上下文。")
                        else:
                            plugin_logger.info("LLM请求预处理：多模态处理后内容为空（未添加任何parts）。")
                    else: 
                        # 构建纯文本请求内容 (所有媒体都已转为文本描述)
                        all_text_from_parts = []
                        for part_data in processed_parts:
                            # 此时 processed_parts 中的所有元素都应该是 {"type": "text", "text": "..."} 结构
                            # 或者图片已转为 "[引用的图片 URL: ...]" 格式的文本
                            if part_data.get("type") == "text":
                                all_text_from_parts.append(part_data.get("text","").strip())
                            elif part_data.get("type") == "image_url": # 理论上不应进入此分支，因为 multimodal_processing_enabled 为 false
                                 all_text_from_parts.append(f"[引用的图片 URL: {part_data.get('image_url',{}).get('url','未知URL')}]")
                            # 其他类型已在 _prepare_multimodal_parts 中转为文本
                        
                        full_quoted_text = " ".join([s for s in all_text_from_parts if s]).strip()
                        if full_quoted_text:
                            actual_quoted_contexts.append({
                                "role": "user",
                                "content": prefix + full_quoted_text + suffix
                            })
                            plugin_logger.info("LLM请求预处理：已构建纯文本引用上下文。")
                        else:
                            plugin_logger.info("LLM请求预处理：纯文本引用内容为空。")

                    if actual_quoted_contexts:
                        new_contexts = []
                        if system_prompt_entry: new_contexts.append(system_prompt_entry)
                        new_contexts.extend(req.contexts) 
                        new_contexts.extend(actual_quoted_contexts) 
                        req.contexts = new_contexts
                        # req.prompt = " " # 保持此行注释，让 AstrBot 核心使用原始 req.prompt
                        plugin_logger.info(f"LLM请求预处理：已整合引用内容到 contexts。用户当前回复将由 AstrBot 核心处理。")
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
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def toolprompts_settings_group(self, event: AstrMessageEvent):
        """管理插件的多模态处理设置 (简写: /tps)。"""
        pass

    @toolprompts_settings_group.command("set_multimodal", alias={"sm"})
    async def set_multimodal_status(self, event: AstrMessageEvent, status: str):
        """
        启用/禁用引用消息的多模态处理。
        命令: /tps sm <on|off>
        参数: status (str): 'on'/'off' 或等效词 (true/false, enable/disable, 1/0)。
        """
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
            plugin_logger.info(f"Multimodal processing set to {new_status} by admin {event.get_sender_id()}/{event.get_sender_name()}.")
            await event.send(event.plain_result(reply_msg))
        except Exception as e:
            plugin_logger.error(f"保存插件配置失败 (set_multimodal_status): {e}", exc_info=True)
            await event.send(event.plain_result("错误：保存配置失败，请检查后台日志。"))

    @toolprompts_settings_group.command("get_multimodal_status", alias={"gms"})
    async def get_multimodal_status(self, event: AstrMessageEvent):
        """
        获取当前引用消息的多模态处理状态。
        命令: /tps gms
        """
        multimodal_enabled = self.config.get("enable_multimodal_processing", False)
        status_str = "已启用" if multimodal_enabled else "已禁用"
        await event.send(event.plain_result(f"当前引用消息的多模态处理状态为: {status_str}"))

    async def terminate(self):
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 正在终止...")

        # 取消定时清理任务
        if self._cleanup_task and not self._cleanup_task.done():
            plugin_logger.info("正在取消定时清理任务...")
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task # 等待任务实际取消
            except asyncio.CancelledError:
                plugin_logger.info("定时清理任务已成功取消。")
            except Exception as e:
                plugin_logger.error(f"等待定时清理任务取消时发生错误: {e}", exc_info=True)
        
        # 执行最后一次清理
        if self.temp_media_dir:
            cleanup_minutes = self.config.get("temp_file_cleanup_minutes", 60)
            if cleanup_minutes > 0: # 即使定时任务禁用了，这里也根据配置决定是否做最后清理
                plugin_logger.info(f"插件终止：执行最终临时文件清理，目录: {self.temp_media_dir}, 清理周期: {cleanup_minutes} 分钟。")
                cleanup_temp_files(self.temp_media_dir, cleanup_minutes) 
            else:
                 plugin_logger.info("插件终止：最终临时文件清理已禁用 (周期设置为0或更小)。")
        plugin_logger.info(f"插件 '{self.metadata.name if hasattr(self, 'metadata') else 'ToolCallNotifierPlugin'}' 已终止。")
