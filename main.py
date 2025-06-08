import re
import os
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import LLMResponse
import astrbot.api.message_components as Comp


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.0.5")
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 匹配Markdown链接和裸链接的正则表达式
        self.url_pattern = re.compile(r'\[.*?\]\((https?://[^\s)]+)\)|(https?://[^\s]+)')

    @filter.on_llm_response(priority=1)
    async def on_llm_response_handler(self, event: AstrMessageEvent, resp: LLMResponse):
        """统一处理LLM响应，包括工具调用通知和媒体链接转换"""
        
        # 1. 处理工具调用
        if resp.role == "tool" and resp.tools_call_name:
            logger.info("LLM响应处理器：检测到工具调用。")
            for tool_name in resp.tools_call_name:
                message = f"正在调用 {tool_name} 工具中……"
                await event.send(event.plain_result(message))
            return

        # 2. 处理媒体链接
        if resp.role == "assistant" and resp.completion_text:
            text_to_process = resp.completion_text
            
            matches = list(self.url_pattern.finditer(text_to_process))
            if not matches:
                return

            logger.debug(f"LLM响应处理器：接收到原始文本: {text_to_process}")

            # 预处理，检查是否包含有效的媒体链接
            has_valid_media = False
            for match in matches:
                url = match.group(1) or match.group(2)
                if self._is_media_url(url):
                    has_valid_media = True
                    break
            
            if not has_valid_media:
                logger.debug("LLM响应处理器：未找到可识别的媒体链接，不进行特殊处理。")
                return

            logger.info("LLM响应处理器：找到可识别的媒体链接，将分条发送并阻止原始消息。")
            
            last_end = 0
            for match in matches:
                plain_text_before = text_to_process[last_end:match.start()].strip()
                if plain_text_before:
                    await event.send(event.plain_result(plain_text_before))

                url = match.group(1) or match.group(2)
                
                media_segment = self._create_media_segment(url)
                await event.send(event.chain_result([media_segment]))
                
                last_end = match.end()

            plain_text_after = text_to_process[last_end:].strip()
            if plain_text_after:
                await event.send(event.plain_result(plain_text_after))

            logger.info("LLM响应处理器：将原始响应文本替换为空格以防止重复发送。")
            resp.completion_text = " "

    def _is_media_url(self, path_or_url: str) -> bool:
        """检查一个路径或URL是否是可识别的媒体类型"""
        if any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mov', '.avi', '.wav', '.pdf', '.doc', '.docx', '.txt']):
            return True
        return False

    def _create_media_segment(self, path_or_url: str):
        """根据路径或URL创建对应的消息组件"""
        is_url = path_or_url.lower().startswith('http')
        is_file = os.path.exists(path_or_url)

        if not (is_url or is_file):
            logger.warning(f"媒体处理：路径 '{path_or_url}' 既不是有效URL也不是本地文件，将作为纯文本发送。")
            return Comp.Plain(text=path_or_url)

        if any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
            logger.info(f"媒体处理：识别为图片: {path_or_url}")
            return Comp.Image.fromURL(path_or_url) if is_url else Comp.Image.fromFileSystem(path_or_url)
        if any(path_or_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi']):
            logger.info(f"媒体处理：识别为视频: {path_or_url}")
            return Comp.Video.fromURL(path_or_url) if is_url else Comp.Video.fromFileSystem(path_or_url)
        if path_or_url.lower().endswith('.wav'):
            logger.info(f"媒体处理：识别为音频: {path_or_url}")
            return Comp.Record(url=path_or_url) if is_url else Comp.Record(file=path_or_url)
        if any(path_or_url.lower().endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.txt']):
            logger.info(f"媒体处理：识别为文档: {path_or_url}")
            return Comp.File(url=path_or_url, name=os.path.basename(path_or_url)) if is_url else Comp.File(file=path_or_url, name=os.path.basename(path_or_url))

        logger.debug(f"媒体处理：链接 '{path_or_url}' 未匹配任何已知媒体类型，将作为纯文本处理。")
        return Comp.Plain(text=path_or_url)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        logger.info("工具调用和媒体链接处理插件已卸载。")
