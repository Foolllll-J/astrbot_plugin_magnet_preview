import re
import math
import base64
from typing import Any, AsyncGenerator, Dict, List, Tuple
import aiohttp

# --- 核心依赖 ---
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register, Context
import astrbot.api.message_components as Comp # 引入消息组件

# --- 固定常量 ---
DEFAULT_WHATSLINK_URL = "https://whatslink.info" 
DEFAULT_TIMEOUT = 10 # 增加一个默认超时常量

FILE_TYPE_MAP = {
    'folder': '📁 文件夹',
    'video': '🎥 视频',
    'image': '🖼 图片',
    'text': '📄 文本',
    'audio': '🎵 音频',
    'archive': '📦 压缩包',
    'document': '📑 文档',
    'unknown': '❓ 其他'
}

# 移除 MagnetResultStore 和 Redis 相关代码，简化为纯 API 插件

@register("astrbot_plugin_magnet_preview", "Foolllll", "预览磁力链接", "0.1")
class MagnetPreviewer(Star):
    # 注意：为了让框架能正常载入，这里的 config 必须是 AstrBotConfig 类型（不应设为 None）
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.output_as_link = config.get("output_image_as_direct_link", True)
        try:
            self.max_screenshots = max(0, min(5, int(config.get("max_screenshot_count", 3))))
        except (TypeError, ValueError):
            self.max_screenshots = 3
            logger.warning("Invalid max_screenshot_count config, using default 3.")

        self.whatslink_url = DEFAULT_WHATSLINK_URL
        self.api_url = f"{self.whatslink_url}/api/v1/link"

        # 预编译正则表达式
        self._magnet_regex = re.compile(r"(magnet:\?xt=urn:btih:[\w\d]{40}.*)")
        self._command_regex = re.compile(r"text='(.*?)'") 
        
    async def terminate(self):
        """清理资源"""
        logger.info("Magnet Previewer terminating")
        await super().terminate()

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.regex(r"magnet:\?xt=urn:btih:[\w\d]{40}.*")
    async def handle_magnet(self, event: AstrMessageEvent) -> AsyncGenerator[Any, Any]:
        """处理磁力链接请求，根据配置决定输出方式"""
        
        # 1. 提取磁力链接
        plain_text = str(event.get_messages()[0])
        link = ""
        try:
            # 尝试用原插件的方式提取
            matches = self._command_regex.findall(plain_text)
            command = matches[0]
            link = command.split("&")[0]
        except (IndexError, AttributeError):
            # 失败后，尝试用简单的正则提取第一个链接
            matches = self._magnet_regex.search(plain_text)
            if matches:
                link = matches.group(1).split('&')[0]
        
        if not link:
            yield event.plain_result("⚠️ 格式错误，未找到有效的磁力链接。")
            return
            
        yield event.plain_result(f"⚙️ 正在解析磁力链接：{link[:60]}...")

        # 2. 调用 API 解析
        data = await self._fetch_magnet_info(link)

        # 3. 处理 API 错误
        if not data:
            yield event.plain_result("⚠️ 解析失败：API无响应或网络错误。")
            return

        if data.get('error'):
            error_msg = data.get('name', '未知错误')
            yield event.plain_result(f"⚠️ 解析失败: {error_msg.split('contact')[0].strip()}")
            return

        # 4. 生成结果消息并回复
        # infos: 纯文本部分; screenshots_urls: 图片URL列表
        infos, screenshots_urls = self._sort_infos_and_get_urls(data)

        if self.output_as_link or not screenshots_urls:
            # 配置为输出链接 或 根本没有图片时，只发送纯文本
            result_message = self._format_text_result(infos, screenshots_urls)
            yield event.plain_result(result_message)
        else:
            # 配置为发送图片时
            async for result in self._generate_image_result(event, infos, screenshots_urls):
                yield result

    def _sort_infos_and_get_urls(self, info: dict) -> Tuple[List[str], List[str]]:
        """整理信息并获取截图URL，只获取配置数量内的URL"""
        
        # 整理基础信息
        file_type = str(info.get('file_type', 'unknown')).lower()
        base_info = [
            f"🔍 解析结果：\r",
            f"📝 名称：{info.get('name', '未知')}\r",
            f"📦 类型：{FILE_TYPE_MAP.get(file_type, FILE_TYPE_MAP['unknown'])}\r",
            f"📏 大小：{self._format_file_size(info.get('size', 0))}\r",
            f"📚 包含文件：{info.get('count', 0)}个"
        ]

        # 获取截图URL
        screenshots_urls = []
        # 异常处理：确保 screenshots 是列表
        raw_screenshots = info.get('screenshots')
        if isinstance(raw_screenshots, list) and self.max_screenshots > 0:
            for s in raw_screenshots[:self.max_screenshots]:
                # 异常处理：确保 s 是 dict 且有 screenshot 键
                try:
                    url = self.replace_image_url(s["screenshot"])
                    if url:
                        screenshots_urls.append(url)
                except (TypeError, KeyError):
                    logger.debug("跳过一张无效的截图数据。")
                    continue

        return base_info, screenshots_urls

    def _format_text_result(self, infos: List[str], screenshots_urls: List[str]) -> str:
        """生成纯文本回复，包含截图链接"""
        message = "\n".join(infos)
        
        if screenshots_urls:
            message += f"\n\n📸 预览截图链接："
            for i, url in enumerate(screenshots_urls):
                message += f"\n- 截图 {i+1}: {url}"
                
        return message

    async def _generate_image_result(self, event: AstrMessageEvent, infos: List[str], screenshots_urls: List[str]) -> AsyncGenerator[Any, Any]:
        """生成并发送包含图片的 chain_result 结果"""
        
        # 1. 纯文本信息组件
        chain: List[Comp.Component] = [Comp.Plain("\n".join(infos))]
        
        # 2. 尝试添加图片组件
        download_success = 0
        async with aiohttp.ClientSession() as session:
            for url in screenshots_urls:
                try:
                    # 下载并编码图片 (参考 YoushuSearchPlugin 的逻辑)
                    timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
                    async with session.get(url, timeout=timeout) as img_response:
                        img_response.raise_for_status()
                        image_bytes = await img_response.read()
                    
                    # 检查图片大小和类型，这里简化为直接编码
                    image_base64 = base64.b64encode(image_bytes).decode()
                    image_component = Comp.Image(file=f"base64://{image_base64}")
                    chain.append(image_component)
                    download_success += 1
                except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:
                    # 无感处理图片下载/编码异常，只记录日志，不中断主流程
                    logger.warning(f"❌ 下载并发送图片失败 ({url}): {type(e).__name__} - {str(e)}")
                    continue
        
        # 如果所有图片下载都失败了，给个提示
        if download_success == 0 and len(screenshots_urls) > 0:
            message_text = "\n\n⚠️ 无法发送图片，已改为发送链接。"
            yield event.plain_result("\n".join(infos) + message_text)
        elif download_success > 0:
            # 成功发送至少一张图，使用 chain_result
            yield event.chain_result(chain)
            
    async def _fetch_magnet_info(self, magnet_link: str) -> Dict | None:
        """异步调用Whatslink API获取磁力信息"""
        params = {"url": magnet_link}
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (MagnetPreviewer)"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.api_url, params=params, headers=headers, ssl=False, timeout=DEFAULT_TIMEOUT) as resp:
                    if resp.status != 200:
                        logger.error(f"API request failed with status: {resp.status}")
                        return None
                    return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"Network error during API call: {e}")
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred during fetch: {e}")
            return None

    def replace_image_url(self, image_url: str) -> str:
        """替换图片URL域名"""
        if not isinstance(image_url, str):
            return ""
        return image_url.replace("https://whatslink.info", self.whatslink_url) if image_url else ""

    @staticmethod
    def _format_file_size(size_bytes: int) -> str:
        """格式化文件大小"""
        try:
            size_bytes = int(size_bytes)
        except (TypeError, ValueError):
            return "0B"
            
        if not size_bytes:
            return "0B"

        units = ["B", "KB", "MB", "GB", "TB"]
        try:
            unit_index = min(int(math.log(size_bytes, 1024)), len(units) - 1)
        except ValueError: 
            return "0B"
            
        size = size_bytes / (1024 ** unit_index)
        return f"{size:.2f} {units[unit_index]}"