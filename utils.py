import os
import time
import base64
import mimetypes
import shutil
import aiohttp
import asyncio
from pathlib import Path
from astrbot.api import logger # 使用 AstrBot 的 logger

# 尝试从 AstrBot 内部获取 logger，如果失败则使用标准 logging
try:
    from astrbot.api import logger as plugin_logger
except ImportError:
    import logging
    plugin_logger = logging.getLogger("ToolPromptsPluginUtils")
    # 配置一个基本的 handler，如果 astrbot.api.logger 不可用
    if not plugin_logger.hasHandlers():
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        plugin_logger.addHandler(handler)
        plugin_logger.setLevel(logging.INFO)


TEMP_MEDIA_DIR_NAME = "temp_media"

def get_temp_media_dir(plugin_data_dir: Path) -> Path:
    """获取或创建插件的临时媒体存储目录"""
    temp_dir = plugin_data_dir / TEMP_MEDIA_DIR_NAME
    if not temp_dir.exists():
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
            plugin_logger.info(f"临时媒体目录已创建: {temp_dir}")
        except Exception as e:
            plugin_logger.error(f"创建临时媒体目录失败: {temp_dir}, 错误: {e}")
            return None # 或者抛出异常
    return temp_dir

async def download_media(url: str, temp_dir: Path, file_name_prefix: str = "downloaded_") -> Path | None:
    """从URL下载媒体文件到临时目录"""
    if not temp_dir:
        plugin_logger.error("下载媒体失败：临时目录无效或未初始化。")
        return None
    
    plugin_logger.info(f"开始下载媒体文件从 URL: {url} 到目录: {temp_dir}")
    try:
        # 确保目录存在 (虽然 get_temp_media_dir 应该已经创建了)
        if not temp_dir.exists():
            plugin_logger.warning(f"临时目录 {temp_dir} 不存在，尝试创建。")
            try:
                temp_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e_mkdir:
                plugin_logger.error(f"下载前创建临时目录失败: {temp_dir}, 错误: {e_mkdir}")
                return None

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status() # 如果状态码不是 2xx，则抛出异常
                
                # 尝试从URL或Content-Disposition获取文件名和扩展名
                content_disposition = response.headers.get('Content-Disposition')
                original_filename = None
                if content_disposition:
                    filenames = re.findall("filename\*?=([^']+''|[^;]+)", content_disposition)
                    if filenames:
                        fn = filenames[0]
                        if fn.lower().startswith("utf-8''"):
                            original_filename = urllib.parse.unquote(fn[7:], encoding='utf-8')
                        else:
                            original_filename = urllib.parse.unquote(fn)
                
                if not original_filename: # 从URL路径获取
                    parsed_url = urllib.parse.urlparse(url)
                    original_filename = os.path.basename(parsed_url.path)

                if not original_filename: # 最终回退
                    original_filename = "media_file"

                # 清理文件名并添加时间戳以确保唯一性
                base, ext = os.path.splitext(original_filename)
                # 移除或替换文件名中的非法字符 (简化版)
                safe_base = "".join(c if c.isalnum() or c in ('_','-') else '_' for c in base)
                timestamp = int(time.time() * 1000)
                filename = f"{file_name_prefix}{safe_base}_{timestamp}{ext if ext else '.tmp'}"
                
                file_path = temp_dir / filename
                with open(file_path, 'wb') as f:
                    while True:
                        chunk = await response.content.read(1024)
                        if not chunk:
                            break
                        f.write(chunk)
                plugin_logger.info(f"媒体文件成功下载并保存到: {file_path} (来自URL: {url})")
                return file_path
    except aiohttp.ClientResponseError as e_http:
        plugin_logger.error(f"下载媒体文件HTTP错误 (状态码: {e_http.status}, URL: {url}): {e_http.message}")
        return None
    except asyncio.TimeoutError:
        plugin_logger.error(f"下载媒体文件超时 (URL: {url})")
        return None
    except Exception as e:
        plugin_logger.error(f"下载媒体文件时发生未知错误 (URL: {url}): {e}", exc_info=True)
        return None

def get_mime_type(file_path: Path) -> str | None:
    """根据文件路径获取MIME类型"""
    if not file_path or not file_path.is_file():
        return None
    mime_type, _ = mimetypes.guess_type(file_path)
    # 为常见媒体类型提供更可靠的MIME类型（如果mimetypes库未返回）
    if not mime_type:
        ext = file_path.suffix.lower()
        if ext == ".jpg" or ext == ".jpeg":
            mime_type = "image/jpeg"
        elif ext == ".png":
            mime_type = "image/png"
        elif ext == ".gif":
            mime_type = "image/gif"
        elif ext == ".mp4":
            mime_type = "video/mp4"
        elif ext == ".mov":
            mime_type = "video/quicktime"
        elif ext == ".avi":
            mime_type = "video/x-msvideo"
        elif ext == ".wav":
            mime_type = "audio/wav"
        elif ext == ".mp3": # 虽然我们主要处理wav，但以防万一
            mime_type = "audio/mpeg"
    return mime_type

def file_to_base64(file_path: Path) -> str | None:
    """将文件内容编码为Base64字符串"""
    if not file_path or not file_path.is_file():
        plugin_logger.warning(f"无法将文件转为Base64：文件不存在或不是文件 - {file_path}")
        return None
    try:
        with open(file_path, 'rb') as f:
            encoded_string = base64.b64encode(f.read()).decode('utf-8')
        return encoded_string
    except Exception as e:
        plugin_logger.error(f"文件转Base64失败: {file_path}, 错误: {e}", exc_info=True)
        return None

def cleanup_temp_files(temp_dir: Path, max_age_minutes: int):
    """清理临时目录中超过指定时长的文件"""
    if not temp_dir or not temp_dir.is_dir() or max_age_minutes <= 0:
        if max_age_minutes > 0 : # 只有当配置了清理且目录无效时才警告
             plugin_logger.warning(f"临时文件清理跳过：目录无效 ({temp_dir}) 或清理周期配置不当 ({max_age_minutes} 分钟)。")
        return

    plugin_logger.info(f"开始清理临时文件目录: {temp_dir}, 清理周期: {max_age_minutes} 分钟")
    now = time.time()
    cleaned_count = 0
    try:
        for f in temp_dir.iterdir():
            if f.is_file():
                try:
                    file_age_seconds = now - f.stat().st_mtime
                    if file_age_seconds > (max_age_minutes * 60):
                        f.unlink()
                        plugin_logger.debug(f"已删除过期临时文件: {f}")
                        cleaned_count += 1
                except Exception as e_file:
                    plugin_logger.error(f"删除临时文件失败: {f}, 错误: {e_file}")
        if cleaned_count > 0:
            plugin_logger.info(f"临时文件清理完成，共删除 {cleaned_count} 个过期文件。")
        else:
            plugin_logger.info("临时文件清理完成，没有找到需要删除的过期文件。")
    except Exception as e_dir:
        plugin_logger.error(f"遍历临时文件目录失败: {temp_dir}, 错误: {e_dir}")

# 用于解析 Content-Disposition 和 URL 路径的额外导入
import re
import urllib.parse
import json # 新增导入

async def call_gemini_api(base_url: str, api_key: str, model_name: str, mime_type: str, base64_data: str, user_prompt: str) -> str | None:
    """
    调用 Gemini API 来处理媒体文件。

    Args:
        base_url: Gemini API 的基础端点。
        api_key: Gemini API 密钥。
        model_name: 要使用的 Gemini 模型名称。
        mime_type: 媒体文件的 MIME 类型 (例如 "video/mp4", "audio/mp3")。
        base64_data: 媒体文件的 Base64 编码数据。
        user_prompt: 用户提供的提示，指导模型如何理解媒体。

    Returns:
        从 Gemini API 返回的文本响应，如果失败则返回 None。
    """
    if not api_key:
        plugin_logger.error("Gemini API 调用失败：API Key 未配置。")
        return None
    
    if not base_url:
        plugin_logger.error("Gemini API 调用失败：Base URL 未配置。")
        return "错误：Gemini API 基础端点未配置。"


    # 确保 base_url 末尾没有斜杠，而路径开头有斜杠
    clean_base_url = base_url.rstrip('/')
    api_path = f"/v1beta/models/{model_name}:generateContent"
    api_url = f"{clean_base_url}{api_path}?key={api_key}"
    
    request_payload = {
        "contents": [{
            "parts": [
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64_data
                    }
                },
                {"text": user_prompt}
            ]
        }]
    }

    plugin_logger.info(f"向 Gemini API ({model_name}) 发送请求...")
    plugin_logger.debug(f"Gemini API 请求体 (数据部分已省略): {json.dumps({'contents': [{'parts': [{'inline_data': {'mime_type': mime_type, 'data': '...'}}, {'text': user_prompt}]}]})}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=request_payload) as response:
                response_json = await response.json()
                if response.status == 200:
                    plugin_logger.info("Gemini API 请求成功。")
                    # 提取文本内容，根据 Gemini API 的响应结构
                    # 通常在 response_json['candidates'][0]['content']['parts'][0]['text']
                    candidates = response_json.get("candidates")
                    if candidates and isinstance(candidates, list) and len(candidates) > 0:
                        content = candidates[0].get("content")
                        if content and isinstance(content, dict):
                            parts = content.get("parts")
                            if parts and isinstance(parts, list) and len(parts) > 0:
                                text_response = parts[0].get("text")
                                if text_response:
                                    plugin_logger.debug(f"Gemini API 响应文本: {text_response}")
                                    return str(text_response)
                                else:
                                    plugin_logger.warning("Gemini API 响应中未找到预期的文本内容 (parts[0]['text']缺失)。")
                            else:
                                plugin_logger.warning("Gemini API 响应中未找到预期的 'parts' 列表。")
                        else:
                            plugin_logger.warning("Gemini API 响应中未找到预期的 'content' 对象。")
                    else:
                        plugin_logger.warning("Gemini API 响应中未找到预期的 'candidates' 列表。")
                    
                    plugin_logger.warning(f"Gemini API 响应结构不符合预期，完整响应: {response_json}")
                    return f"Gemini API 调用成功，但无法解析响应文本。原始响应: {json.dumps(response_json)}"

                else:
                    plugin_logger.error(f"Gemini API 请求失败，状态码: {response.status}, 响应: {response_json}")
                    error_message = response_json.get("error", {}).get("message", "未知错误")
                    return f"Gemini API 错误: {error_message}"
    except aiohttp.ClientError as e:
        plugin_logger.error(f"调用 Gemini API 时发生 aiohttp 客户端错误: {e}", exc_info=True)
        return f"Gemini API 网络请求错误: {e}"
    except json.JSONDecodeError as e:
        plugin_logger.error(f"解析 Gemini API 响应时发生 JSON 解码错误: {e}", exc_info=True)
        raw_text = await response.text()
        return f"Gemini API 响应 JSON 解析错误。原始响应: {raw_text}"
    except Exception as e:
        plugin_logger.error(f"调用 Gemini API 时发生未知错误: {e}", exc_info=True)
        return f"调用 Gemini API 时发生未知错误: {e}"

if __name__ == '__main__':
    pass
