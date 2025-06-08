import re
import os
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import LLMResponse
import astrbot.api.message_components as Comp


@register("astrbot_plugin_tool_prompts", "PluginDeveloper", "一个LLM工具调用和媒体链接处理插件", "0.0.7")
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 分别匹配URL和常见文件路径的正则表达式
        self.url_pattern = re.compile(r'https?://[^\s"\'`<>]+')
        # 匹配Linux和Windows的绝对路径
        self.path_pattern = re.compile(r'(?:[a-zA-Z]:\\|/)[^\s"\'`<>]+')

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

        # 2. 处理媒体链接和路径
        if resp.role == "assistant" and resp.completion_text:
            text_to_process = resp.completion_text
            
            # 查找所有URL和文件路径的匹配项
            url_matches = list(self.url_pattern.finditer(text_to_process))
            path_matches = list(self.path_pattern.finditer(text_to_process))
            
            # 合并并排序所有匹配项
            all_matches = sorted(url_matches + path_matches, key=lambda m: m.start())

            if not all_matches:
                return

            logger.debug(f"LLM响应处理器：接收到原始文本: {text_to_process}")

            # 预处理，检查是否包含有效的媒体链接
            has_valid_media = any(self._is_media(match.group(0)) for match in all_matches)
            
            if not has_valid_media:
                logger.debug("LLM响应处理器：未找到可识别的媒体，不进行特殊处理。")
                return

            logger.info("LLM响应处理器：找到可识别的媒体，将分条发送并阻止原始消息。")
            
            last_end = 0
            for match in all_matches:
                # 发送匹配前的纯文本部分
                plain_text_before = text_to_process[last_end:match.start()].strip()
                if plain_text_before:
                    await event.send(event.plain_result(plain_text_before))

                # 提取URL或路径
                path_or_url = match.group(0)
                
                # 创建并发送媒体消息段
                media_segment = self._create_media_segment(path_or_url)
                await event.send(event.chain_result([media_segment]))
                
                last_end = match.end()

            # 发送最后一个匹配后的纯文本部分
            plain_text_after = text_to_process[last_end:].strip()
            if plain_text_after:
                await event.send(event.plain_result(plain_text_after))

            logger.info("LLM响应处理器：将原始响应文本替换为空格以防止重复发送。")
            resp.completion_text = " "

    def _is_media(self, path_or_url: str) -> bool:
        """检查一个路径或URL是否是可识别的媒体类型"""
        # 检查文件后缀
        has_media_extension = any(path_or_url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.mov', '.avi', '.wav', '.pdf', '.doc', '.docx', '.txt'])
        if not has_media_extension:
            return False
        
        # 如果是本地路径，额外检查文件是否存在
        if path_or_url.startswith('/') or re.match(r'^[a-zA-Z]:\\', path_or_url):
            return os.path.exists(path_or_url)
            
        return True

    def _create_media_segment(self, path_or_url: str):
        """根据路径或URL创建对应的消息组件"""
        is_url = path_or_url.lower().startswith('http')
        
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

        logger.debug(f"媒体处理：路径 '{path_or_url}' 未匹配任何已知媒体类型，将作为纯文本处理。")
        return Comp.Plain(text=path_or_url)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        logger.info("工具调用和媒体链接处理插件已卸载。")
