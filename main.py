import re
import os
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import LLMResponse
import astrbot.api.message_components as Comp


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.0.3")
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 匹配Markdown链接和裸链接的正则表达式
        self.url_pattern = re.compile(r'\[.*?\]\((https?://[^\s)]+)\)|(https?://[^\s]+)')

    @filter.on_llm_response()
    async def on_tool_call_notifier(self, event: AstrMessageEvent, resp: LLMResponse):
        """当LLM响应是工具调用时，发送通知"""
        if resp.role == "tool" and resp.tools_call_name:
            for tool_name in resp.tools_call_name:
                logger.info(f"检测到工具调用: {tool_name}")
                message = f"正在调用 {tool_name} 工具中……"
                result = event.plain_result(message)
                await event.send(result)

    @filter.on_decorating_result(priority=1)
    async def on_media_link_decorator(self, event: AstrMessageEvent):
        """在发送消息前，将媒体链接和路径转换为可显示的组件，并分条发送"""
        result = event.get_result()
        if not result or not result.chain:
            return

        text_to_process = ""
        for segment in result.chain:
            if isinstance(segment, Comp.Plain):
                text_to_process += segment.text
        
        if not text_to_process:
            return

        logger.debug(f"媒体处理插件：接收到原始文本: {text_to_process}")

        matches = list(self.url_pattern.finditer(text_to_process))
        
        # 如果没有找到任何链接，则不处理，让原始消息正常发送
        if not matches:
            logger.debug("媒体处理插件：未找到链接，不进行处理。")
            return

        # 找到了链接，阻止原始消息的发送
        logger.info("媒体处理插件：找到媒体链接，将阻止原始消息并分条发送。")
        event.stop_event()

        last_end = 0
        for match in matches:
            # 发送匹配前的纯文本部分
            plain_text_before = text_to_process[last_end:match.start()].strip()
            if plain_text_before:
                logger.debug(f"媒体处理插件：发送文本部分: {plain_text_before}")
                await event.send(event.plain_result(plain_text_before))

            # 提取URL（优先匹配Markdown格式）
            url = match.group(1) or match.group(2)
            logger.info(f"媒体处理插件：匹配到链接: {url}")
            
            # 创建并发送媒体消息段
            media_segment = self._create_media_segment(url)
            if media_segment:
                logger.debug(f"媒体处理插件：发送媒体部分: {media_segment}")
                await event.send(event.chain_result([media_segment]))
            
            last_end = match.end()

        # 发送最后一个匹配后的纯文本部分
        plain_text_after = text_to_process[last_end:].strip()
        if plain_text_after:
            logger.debug(f"媒体处理插件：发送末尾文本部分: {plain_text_after}")
            await event.send(event.plain_result(plain_text_after))

    def _create_media_segment(self, path_or_url: str):
        """根据路径或URL创建对应的消息组件"""
        is_url = path_or_url.lower().startswith('http')
        is_file = os.path.exists(path_or_url)

        if not (is_url or is_file):
            logger.warning(f"媒体处理插件：路径 '{path_or_url}' 既不是有效URL也不是本地文件。")
            return Comp.Plain(text=path_or_url)

        # 图片
        if any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
            logger.info(f"媒体处理插件：识别为图片: {path_or_url}")
            return Comp.Image.fromURL(path_or_url) if is_url else Comp.Image.fromFileSystem(path_or_url)
        # 视频
        if any(path_or_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi']):
            logger.info(f"媒体处理插件：识别为视频: {path_or_url}")
            return Comp.Video.fromURL(path_or_url) if is_url else Comp.Video.fromFileSystem(path_or_url)
        # 音频
        if path_or_url.lower().endswith('.wav'):
            logger.info(f"媒体处理插件：识别为音频: {path_or_url}")
            return Comp.Record(url=path_or_url) if is_url else Comp.Record(file=path_or_url)
        # 文档
        if any(path_or_url.lower().endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.txt']):
            logger.info(f"媒体处理插件：识别为文档: {path_or_url}")
            return Comp.File(url=path_or_url, name=os.path.basename(path_or_url)) if is_url else Comp.File(file=path_or_url, name=os.path.basename(path_or_url))

        logger.debug(f"媒体处理插件：链接 '{path_or_url}' 未匹配任何已知媒体类型，将作为纯文本处理。")
        return Comp.Plain(text=path_or_url)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        logger.info("工具调用和媒体链接处理插件已卸载。")
