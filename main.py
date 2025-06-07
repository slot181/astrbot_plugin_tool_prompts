from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, LLMResponse


@register("tool_call_notifier", "PluginDeveloper", "一个LLM工具调用消息提示插件", "1.0.0")
class ToolCallNotifierPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_llm_response()
    async def on_tool_call_notifier(self, event: AstrMessageEvent, resp: LLMResponse):
        """当LLM响应是工具调用时，发送通知"""
        if resp.role == "tool" and resp.tools_call_name:
            for tool_name in resp.tools_call_name:
                logger.info(f"检测到工具调用: {tool_name}")
                message = f"正在调用 {tool_name} 工具中……"
                result = event.plain_result(message)
                await event.send(result)

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        logger.info("工具调用通知插件已卸载。")
