import re
import os
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import LLMResponse
import astrbot.api.message_components as Comp
from astrbot.api.provider import ProviderRequest # 确保导入 ProviderRequest
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent # 用于类型检查和获取client


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.1.5")
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 匹配URL (包括可能省略协议的//开头的) 和常见文件路径的正则表达式
        # URL模式：匹配 http(s):// 或 // 开头的链接，排除空格、引号、反引号、尖括号和各种括号
        self.url_pattern = re.compile(r'(?:https?:)?//[^\s"\'`<>()[\]{}]+')
        # 路径模式：匹配驱动器号或/开头的路径，排除空格、引号、反引号、尖括号和各种括号
        self.path_pattern = re.compile(r'(?:[a-zA-Z]:\\|/)[^\s"\'`<>`()[\]{}]+')

    @filter.on_llm_response(priority=1)
    async def on_llm_response_handler(self, event: AstrMessageEvent, resp: LLMResponse):
        """统一处理LLM响应，包括工具调用通知和媒体链接转换"""

        # 检查事件是否已被本插件处理过媒体内容
        if hasattr(event, '_media_processed_by_tool_prompts_plugin') and event._media_processed_by_tool_prompts_plugin:
            logger.debug("LLM响应处理器：事件已由本插件处理过媒体，跳过。")
            return
        
        if resp.role == "tool" and resp.tools_call_name:
            logger.info("LLM响应处理器：检测到工具调用。")
            for tool_name in resp.tools_call_name:
                message = f"正在调用 {tool_name} 工具中……"
                await event.send(event.plain_result(message))
            return

        if resp.role == "assistant" and resp.completion_text:
            text_to_process = resp.completion_text
            
            url_matches = list(self.url_pattern.finditer(text_to_process))
            path_matches = list(self.path_pattern.finditer(text_to_process))
            
            # 合并并基于匹配到的字符串进行去重
            temp_matches = {} # 使用字典来去重，键是匹配的字符串，值是匹配对象
            for match in url_matches + path_matches:
                match_str = match.group(0)
                # 如果新的匹配比已有的匹配更长或同样长但开始位置更早，则优先考虑（处理嵌套匹配的情况）
                # 或者简单地，如果还没见过这个字符串，就添加它
                if match_str not in temp_matches or \
                   (len(match_str) > len(temp_matches[match_str].group(0))) or \
                   (len(match_str) == len(temp_matches[match_str].group(0)) and match.start() < temp_matches[match_str].start()):
                    temp_matches[match_str] = match
            
            all_matches = sorted(list(temp_matches.values()), key=lambda m: m.start())

            if not all_matches:
                return

            logger.debug(f"LLM响应处理器：接收到原始文本: {text_to_process}")

            # 检查是否有任何一个匹配是有效的媒体，并基于 corrected_path 进行去重
            temp_processed_items = {} # 使用 corrected_path 作为键来去重
            for match_obj in all_matches: # all_matches 已经基于原始匹配字符串去重
                original_match_str = match_obj.group(0)
                corrected_path = original_match_str
                # 补全 // 开头的 URL
                if original_match_str.startswith("//"):
                    corrected_path = "https:" + original_match_str
                
                if self._is_media(corrected_path):
                    # 如果 corrected_path 尚未在 temp_processed_items 中，
                    # 或者新的匹配项开始位置更早（理论上 all_matches 已排序，这里主要是确保唯一性）
                    # 我们添加/更新它。由于 all_matches 的原始字符串已去重，
                    # 相同的 corrected_path 只能来自不同的原始字符串（如 "//url" 和 "https://url"）。
                    # 我们只保留第一个遇到的（按 all_matches 的顺序，即原始文本中的顺序）。
                    if corrected_path not in temp_processed_items:
                         temp_processed_items[corrected_path] = {'original': match_obj, 'corrected_path': corrected_path}
            
            processed_matches = sorted(list(temp_processed_items.values()), key=lambda item: item['original'].start())


            if not processed_matches:
                logger.debug("LLM响应处理器：未找到可识别的媒体，不进行特殊处理。")
                return

            logger.info("LLM响应处理器：找到可识别的媒体，将分条发送并阻止原始消息。")
            
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

            logger.info("LLM响应处理器：将原始响应文本替换为空格以防止重复发送，并标记事件已处理。")
            setattr(event, '_media_processed_by_tool_prompts_plugin', True) # 标记事件已被处理
            resp.completion_text = " "

    def _is_media(self, path_or_url: str) -> bool:
        has_media_extension = any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mov', '.avi', '.wav', '.pdf', '.doc', '.docx', '.txt'])
        if not has_media_extension:
            return False
        
        # 对于本地文件路径 (以 / 或驱动器号开头)，只要格式正确且有媒体后缀，就认为是媒体
        if path_or_url.startswith('/') or re.match(r'^[a-zA-Z]:\\', path_or_url):
            return os.path.exists(path_or_url)
        
        # 对于URL，我们假设它是可访问的，如果它有媒体后缀
        if path_or_url.lower().startswith('http:') or path_or_url.lower().startswith('https:'):
            return True
            
        return False # 其他情况（如相对路径但非媒体后缀）不视为有效媒体

    def _create_media_segment(self, path_or_url: str):
        is_url = path_or_url.lower().startswith('http:') or path_or_url.lower().startswith('https:')
        
        # 图片
        if any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
            logger.info(f"媒体处理：识别为图片: {path_or_url}")
            return Comp.Image.fromURL(path_or_url) if is_url else Comp.Image.fromFileSystem(path_or_url)
        # 视频
        if any(path_or_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi']):
            logger.info(f"媒体处理：识别为视频: {path_or_url}")
            return Comp.Video.fromURL(path_or_url) if is_url else Comp.Video.fromFileSystem(path_or_url)
        # 音频
        if path_or_url.lower().endswith('.wav'):
            logger.info(f"媒体处理：识别为音频: {path_or_url}")
            return Comp.Record(url=path_or_url) if is_url else Comp.Record(file=path_or_url)
        # 文档
        if any(path_or_url.lower().endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.txt']):
            logger.info(f"媒体处理：识别为文档: {path_or_url}")
            return Comp.File(url=path_or_url, name=os.path.basename(path_or_url)) if is_url else Comp.File(file=path_or_url, name=os.path.basename(path_or_url))

        logger.debug(f"媒体处理：路径 '{path_or_url}' 未匹配任何已知媒体类型（在_create_media_segment中），将作为纯文本处理。")
        return Comp.Plain(text=path_or_url) # Fallback

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        logger.info("工具调用和媒体链接处理插件已卸载。")

    @filter.on_llm_request(priority=1) # 优先级设为1，确保在默认处理前执行
    async def on_llm_request_handler(self, event: AstrMessageEvent, req: ProviderRequest):
        """处理用户消息中的引用，并将其内容添加到LLM请求中"""
        
        # 仅处理 aiocqhttp (QQ) 平台的引用
        if event.get_platform_name() != "aiocqhttp":
            return

        raw_message_chain = event.message_obj.message
        reply_message_id_str = None

        for segment in raw_message_chain:
            if isinstance(segment, Comp.Reply):
                # 尝试从 Comp.Reply 对象获取 message_id
                # 根据 nakuru-project 的 Reply 结构，id 通常在 segment.data['id']
                # 或者一些实现可能直接有 segment.id 属性
                if hasattr(segment, 'data') and isinstance(segment.data, dict) and 'id' in segment.data:
                    reply_message_id_str = str(segment.data['id'])
                elif hasattr(segment, 'id'): # 备用方案
                    reply_message_id_str = str(segment.id)
                
                if reply_message_id_str:
                    logger.info(f"LLM请求预处理：检测到QQ引用消息，ID: {reply_message_id_str}")
                else:
                    logger.warning(f"LLM请求预处理：找到Reply段，但无法确定其message_id。段内容: {segment}")
                break # 假设每条消息只有一个主要的引用段

        if not reply_message_id_str:
            return # 没有引用，或无法获取ID，正常处理

        try:
            client = None
            if isinstance(event, AiocqhttpMessageEvent):
                client = event.bot
            else:
                # 尝试通过 context 获取 platform adapter 再获取 client
                # 这部分可能需要根据 AstrBot 的具体实现调整
                platform_adapter = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
                if platform_adapter and hasattr(platform_adapter, 'get_client'):
                    client = platform_adapter.get_client()
                elif platform_adapter and hasattr(platform_adapter, 'client'): # 有些适配器可能直接暴露 client
                     client = platform_adapter.client


            if not client:
                logger.error("LLM请求预处理：无法获取到 aiocqhttp 客户端实例，无法处理引用消息。")
                return

            logger.info(f"LLM请求预处理：尝试使用 get_msg 获取被引用消息详情，ID: {reply_message_id_str}")
            replied_message_data_wrapper = await client.api.call_action('get_msg', message_id=int(reply_message_id_str))
            
            if replied_message_data_wrapper and replied_message_data_wrapper.get('status') == 'ok' and replied_message_data_wrapper.get('data'):
                replied_message_detail = replied_message_data_wrapper['data']
                original_sender_nickname = replied_message_detail.get('sender', {}).get('card') or replied_message_detail.get('sender', {}).get('nickname', '未知用户')
                
                extracted_contents = [] # 用于存储解析出的文本和图片URL描述

                if 'message' in replied_message_detail and isinstance(replied_message_detail['message'], list):
                    for seg_idx, seg_data in enumerate(replied_message_detail['message']):
                        seg_type = seg_data.get('type')
                        seg_content_data = seg_data.get('data', {})
                        
                        if seg_type == 'text' and seg_content_data.get('text'):
                            extracted_contents.append(seg_content_data['text'].strip())
                        elif seg_type == 'image' and seg_content_data.get('url'):
                            extracted_contents.append(f"[引用的图片{seg_idx+1} URL: {seg_content_data['url']}]")
                        elif seg_type == 'record' and seg_content_data.get('url'):
                             extracted_contents.append(f"[引用的语音{seg_idx+1} URL: {seg_content_data['url']}] (内容未转录)")
                        elif seg_type == 'video' and seg_content_data.get('url'):
                             extracted_contents.append(f"[引用的视频{seg_idx+1} URL: {seg_content_data['url']}]")
                        # 可以根据需要添加对其他类型如 face, at 等的处理
                
                if extracted_contents:
                    # 将解析到的引用内容整合到 LLM 请求的 contexts 中
                    # 确保 req.contexts 是一个列表
                    if req.contexts is None: # ProviderRequest 中的 contexts 可能为 None
                        req.contexts = []
                    
                    # 检查原始请求中是否有 system prompt，如果有，保持在最前面
                    system_prompt_entry = None
                    if req.contexts and req.contexts[0].get('role') == 'system':
                        system_prompt_entry = req.contexts.pop(0)

                    # 构建引用内容的上下文条目
                    # 将所有提取的引用内容合并为一个字符串，作为一条历史消息
                    full_quoted_content = " ".join(extracted_contents)
                    quoted_message_context = {
                        "role": "user", # 或者根据 original_sender_nickname 判断是否是 "assistant"
                        "content": f"用户 {event.get_sender_name()} 引用了 {original_sender_nickname} 的消息:\n\"\"\"\n{full_quoted_content}\n\"\"\""
                    }
                    
                    # 构建新的 contexts 列表
                    new_contexts = []
                    if system_prompt_entry:
                        new_contexts.append(system_prompt_entry)
                    
                    # 添加历史上下文 (来自原始 req.contexts)
                    new_contexts.extend(req.contexts)
                    
                    # 添加我们构建的引用消息上下文
                    new_contexts.append(quoted_message_context)
                    
                    # 添加当前用户的实际提问 (来自原始 req.prompt)
                    if req.prompt: # 确保 prompt 非空
                        # 避免重复添加如果 prompt 已在 contexts 末尾
                        is_prompt_already_in_contexts = False
                        if new_contexts and new_contexts[-1].get('role') == 'user' and new_contexts[-1].get('content') == req.prompt:
                            is_prompt_already_in_contexts = True
                        
                        if not is_prompt_already_in_contexts:
                            new_contexts.append({"role": "user", "content": req.prompt})
                    
                    req.contexts = new_contexts
                    req.prompt = None # 所有信息已移至 contexts

                    logger.info(f"LLM请求预处理：已将被引用消息内容整合到 LLM 请求的 contexts 中。")
                    logger.debug(f"LLM请求预处理：新的 contexts: {req.contexts}")
                else:
                    logger.info("LLM请求预处理：被引用的消息未解析出有效内容。")
            else:
                logger.warning(f"LLM请求预处理：调用 get_msg 获取引用消息失败或未返回有效数据: {replied_message_data_wrapper}")

        except Exception as e:
            logger.error(f"LLM请求预处理：处理QQ引用消息时发生错误: {e}", exc_info=True)
            # 出错时，不修改 req，让请求按原样发送
