import re
import os
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import LLMResponse
import astrbot.api.message_components as Comp


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.0.2")
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 匹配URL和常见文件路径的正则表达式
        self.path_pattern = re.compile(r'(https?://[^\s]+|[/\\A-Za-z0-9_.-]+[.][a-zA-Z0-9]+)')

    @filter.on_llm_response()
    async def on_tool_call_notifier(self, event: AstrMessageEvent, resp: LLMResponse):
        """当LLM响应是工具调用时，发送通知"""
        if resp.role == "tool" and resp.tools_call_name:
            for tool_name in resp.tools_call_name:
                logger.info(f"检测到工具调用: {tool_name}")
                message = f"正在调用 {tool_name} 工具中……"
                result = event.plain_result(message)
                await event.send(result)

    @filter.on_decorating_result()
    async def on_media_link_decorator(self, event: AstrMessageEvent):
        """在发送消息前，将媒体链接和路径转换为可显示的组件"""
        result = event.get_result()
        if not result or not result.chain:
            return

        original_chain = result.chain
        new_chain = []

        for segment in original_chain:
            if not isinstance(segment, Comp.Plain):
                new_chain.append(segment)
                continue

            text = segment.text
            last_end = 0
            
            for match in self.path_pattern.finditer(text):
                # 添加匹配前的文本
                new_chain.append(Comp.Plain(text[last_end:match.start()]))
                
                path_or_url = match.group(0)
                media_segment = self._create_media_segment(path_or_url)
                new_chain.append(media_segment)
                
                last_end = match.end()
            
            # 添加最后一个匹配后的文本
            if last_end < len(text):
                new_chain.append(Comp.Plain(text[last_end:]))

        # 过滤掉空的Plain文本段
        result.chain = [s for s in new_chain if not (isinstance(s, Comp.Plain) and not s.text)]


    def _create_media_segment(self, path_or_url: str):
        """根据路径或URL创建对应的消息组件"""
        is_url = path_or_url.lower().startswith('http')
        # 简单的文件路径检查，可以根据需要加强
        is_file = os.path.exists(path_or_url)

        if not (is_url or is_file):
            return Comp.Plain(text=path_or_url)

        # 图片
        if any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
            return Comp.Image.fromURL(path_or_url) if is_url else Comp.Image.fromFileSystem(path_or_url)
        # 视频
        if any(path_or_url.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi']):
            return Comp.Video.fromURL(path_or_url) if is_url else Comp.Video.fromFileSystem(path_or_url)
        # 音频
        if path_or_url.lower().endswith('.wav'):
            # 假设 Record 组件有 fromURL 和 fromFileSystem 方法
            return Comp.Record(url=path_or_url) if is_url else Comp.Record(file=path_or_url)
        # 文档
        if any(path_or_url.lower().endswith(ext) for ext in ['.pdf', '.doc', '.docx', '.txt']):
            # 假设 File 组件有 fromURL 和 fromFileSystem 方法
            return Comp.File(url=path_or_url, name=os.path.basename(path_or_url)) if is_url else Comp.File(file=path_or_url, name=os.path.basename(path_or_url))

        return Comp.Plain(text=path_or_url)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        logger.info("工具调用和媒体链接处理插件已卸载。")
