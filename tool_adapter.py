import json
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Star # Star 通常在主插件类继承，这里可能不需要直接用

# 尝试从 AstrBot 内部获取 logger，如果失败则使用标准 logging
try:
    from astrbot.api import logger as plugin_logger
except ImportError:
    import logging
    plugin_logger = logging.getLogger("ToolPromptsToolAdapter")
    if not plugin_logger.hasHandlers():
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        plugin_logger.addHandler(handler)
        plugin_logger.setLevel(logging.INFO)

# 这个函数将作为钩子在插件的 Star 类中被注册
# 为了让这个文件中的钩子能被主插件的 Star 实例注册，
# 我们通常会将这个函数定义在 Star 类中，或者让 Star 类的一个方法调用这个。
# 但根据插件开发文档，钩子可以直接定义在模块级别，然后由 Star 实例的方法通过装饰器注册。
# 这里我们先定义函数，然后在 main.py 中想办法让插件实例注册它。
# 一个更简洁的方式是，这个 tool_adapter.py 包含一个类，这个类有这个钩子方法。
# 或者，更简单地，这个钩子函数直接在 main.py 的插件类中定义。

# 考虑到重构的目标是将逻辑分离到新文件，我们在这里定义处理函数，
# 然后在 main.py 的插件类中，创建一个方法用 @filter.on_llm_response 装饰，
# 并在这个方法内部调用这个新文件中的处理逻辑。
# 或者，如果 AstrBot 允许从外部文件直接注册钩子到某个 Star 实例，那会更直接。
# 从文档看，@filter.on_llm_response() 需要装饰一个插件类的方法。

async def handle_gemini_search_tool_response(plugin_instance: Star, event: AstrMessageEvent, resp: LLMResponse):
    """
    专门处理 gemini_integrator_mcp-gemini_web_search 工具调用后的响应。
    如果检测到此工具的成功响应，则提取 answerText 并单独发送。
    """
    # plugin_instance 参数是为了将来可能需要访问插件的 self.config 或 self.context
    
    # 检查是否是工具响应，并且是来自 gemini_web_search 工具
    if resp.role == "tool" and resp.tool_call_id and resp.tool_call_id.startswith("gemini_integrator_mcp-gemini_web_search"):
        plugin_logger.info(f"ToolAdapter: 检测到来自 '{resp.tool_call_id}' 工具的响应。")
        
        if resp.content:
            try:
                tool_content_json = json.loads(resp.content)
                answer_text = tool_content_json.get("answerText")
                
                if answer_text:
                    plugin_logger.info(f"ToolAdapter: 从工具响应中提取到 answerText。准备发送。")
                    # 使用 AstrBot 的方法发送消息
                    # event.send() 是异步的，需要 await
                    await event.send(event.plain_result(str(answer_text)))
                    plugin_logger.info(f"ToolAdapter: 已将 answerText 作为单独消息发送。")
                    
                    # 根据需求，我们只是提取并发送 answerText，原始的工具响应（包含sources等）
                    # 仍然会由 AstrBot 核心处理（例如，可能被加入到上下文中，或被LLM再次总结）。
                    # 如果不希望原始的工具响应的 content 被LLM看到或进一步处理，
                    # 可以在这里修改 resp.content，例如 resp.content = " "
                    # 但题目要求是“提取出来用astrbot给的方法将值的内容发送到消息平台里”，
                    # 并没有说要阻止原始响应进入后续流程。
                    # 且“工具调用后的发送给用户的消息提示这个代码逻辑要保留”，
                    # 这指的是LLM决定调用工具时的提示，与此处的工具执行结果处理不冲突。

                else:
                    plugin_logger.warning(f"ToolAdapter: 工具 '{resp.tool_call_id}' 的响应内容中未找到 'answerText'。内容: {resp.content}")
            except json.JSONDecodeError:
                plugin_logger.error(f"ToolAdapter: 解析工具 '{resp.tool_call_id}' 的响应内容失败 (非JSON格式)。内容: {resp.content}")
            except Exception as e:
                plugin_logger.error(f"ToolAdapter: 处理工具 '{resp.tool_call_id}' 响应时发生未知错误: {e}", exc_info=True)
        else:
            plugin_logger.warning(f"ToolAdapter: 工具 '{resp.tool_call_id}' 的响应内容为空。")

# 注意：这个文件本身不包含 @register 或 Star 类。
# handle_gemini_search_tool_response 函数需要被 main.py 中的插件类的一个方法调用，
# 或者 main.py 中的插件类直接包含这个逻辑但通过 @filter.on_llm_response 注册。
# 为了模块化，我们选择前者或在 main.py 中导入并注册。

# 最简单的集成方式是在 main.py 的 ToolCallNotifierPlugin 类中添加一个新的方法，
# 并用 @filter.on_llm_response 装饰它，然后在这个新方法中调用
# from .tool_adapter import handle_gemini_search_tool_response
# await handle_gemini_search_tool_response(self, event, resp)
