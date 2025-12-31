import re
import math
from typing import Any, AsyncGenerator, Dict, List, Tuple
import aiohttp
import asyncio

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.star import Star, register, Context
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Plain, Node, Nodes

DEFAULT_WHATSLINK_URL = "https://whatslink.info" 
DEFAULT_TIMEOUT = 10 

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

@register("astrbot_plugin_magnet_preview", "Foolllll", "预览磁链信息", "1.0")
class MagnetPreviewer(Star):
    
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        self.output_as_link = config.get("output_image_as_direct_link", False)
        try:
            self.max_screenshots = max(0, min(5, int(config.get("max_screenshot_count", 3))))
        except (TypeError, ValueError):
            self.max_screenshots = 3
            logger.warning("Invalid max_screenshot_count config, using default 3.")

        self.whatslink_url = DEFAULT_WHATSLINK_URL
        self.api_url = f"{self.whatslink_url}/api/v1/link"

        self._magnet_regex = re.compile(r"(magnet:\?xt=urn:btih:[\w\d]{40}.*)")
        self._command_regex = re.compile(r"text='(.*?)'")
        self._hash_regex = re.compile(r"([a-fA-F0-9]{40})")
        
    async def terminate(self):
        logger.info("Magnet Previewer terminating")
        await super().terminate()

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.regex(r"(magnet:\?xt=urn:btih:[\w\d]{40}.*)|([a-fA-F0-9]{40})")
    async def handle_magnet(self, event: AstrMessageEvent) -> AsyncGenerator[Any, Any]:
        """处理磁力链接请求，根据配置决定输出方式"""
        
        plain_text = str(event.get_messages()[0])
        link = ""
        
        # 1. 提取磁力链接
        matches_magnet = self._magnet_regex.search(plain_text)
        if matches_magnet:
            link = matches_magnet.group(1).split('&')[0]
            
        # 如果没有找到完整的链接，则寻找裸哈希
        if not link:
            matches_hash = self._hash_regex.search(plain_text)
            if matches_hash:
                info_hash = matches_hash.group(1).upper() # 提取哈希并转大写
                link = f"magnet:?xt=urn:btih:{info_hash}"
        if not link:
            yield event.plain_result("⚠️ 格式错误，未找到有效的磁力链接。")
            return
            
        logger.info(f"解析磁力链接: {link}")

        # 2. 调用 API 解析
        data = await self._fetch_magnet_info(link)

        # 3. 处理 API 错误
        if not data or data.get('error'):
            error_msg = data.get('name', '未知错误') if data else 'API无响应'
            yield event.plain_result(f"⚠️ 解析失败: {error_msg.split('contact')[0].strip()}")
            return

        # 4. 生成结果消息并回复
        infos, screenshots_urls = self._sort_infos_and_get_urls(data)

        if self.output_as_link or not screenshots_urls:
            # 直链模式或无图片，发送纯文本
            result_message = self._format_text_result(infos, screenshots_urls)
            yield event.plain_result(result_message)
        else:
            # 图片模式，合并转发发送图文（每张图一个节点）
            async for result in self._generate_forward_result(event, infos, screenshots_urls):
                yield result

    async def _generate_forward_result(self, event: AstrMessageEvent, infos: List[str], screenshots_urls: List[str]) -> AsyncGenerator[Any, Any]:
        """生成并发送包含图片的合并转发消息，每张图片单独一个节点"""
        sender_id = event.get_self_id()
        forward_nodes: List[Node] = []
        screenshot_line_index = None
        
        # 1. 纯文本信息节点
        if screenshots_urls:
            screenshot_line_index = len(infos)
            infos.append(f"\n📸 预览截图 ({len(screenshots_urls)} 张):")

        # 2. 下载图片并逐张加入节点
        image_bytes_list = await self._download_screenshots(screenshots_urls)
        if image_bytes_list and screenshot_line_index is not None and len(image_bytes_list) != len(screenshots_urls):
            infos[screenshot_line_index] = (
                f"\n📸 预览截图 (成功 {len(image_bytes_list)}/{len(screenshots_urls)} 张):"
            )

        info_text = "\n".join(infos)

        # 将文本部分分割成节点
        split_texts = self._split_text_by_length(info_text, 4000)

        first_node_content = [Plain(text=split_texts[0])]
        forward_nodes.append(Node(uin=sender_id, name="磁力预览信息", content=first_node_content))

        for i, part_text in enumerate(split_texts[1:], 1):
            forward_nodes.append(Node(uin=sender_id, name=f"磁力预览信息 ({i+1})", content=[Plain(text=part_text)]))

        for image_bytes in image_bytes_list:
            image_component = Comp.Image.fromBytes(image_bytes)
            forward_nodes.append(Node(uin=sender_id, name="预览截图", content=[image_component]))
        
        # 3. 检查发送结果
        if not image_bytes_list and len(screenshots_urls) > 0:
            logger.warning("所有图片下载失败，回退到纯文本链接模式。")
            result_message = self._format_text_result(infos, screenshots_urls)
            yield event.plain_result("⚠️ 图片发送失败，已改为发送链接。\n\n" + result_message)
            return

        # 4. 合并转发发送
        merged_forward_message = Nodes(nodes=forward_nodes)
        yield event.chain_result([merged_forward_message])

    def _split_text_by_length(self, text: str, max_length: int = 4000) -> List[str]:
        """将文本按指定长度分割成一个字符串列表"""
        return [text[i:i + max_length] for i in range(0, len(text), max_length)]

    def _sort_infos_and_get_urls(self, info: dict) -> Tuple[List[str], List[str]]:
        file_type = str(info.get('file_type', 'unknown')).lower()
        base_info = [
            f"🔍 解析结果：\r",
            f"📝 名称：{info.get('name', '未知')}\r",
            f"📦 类型：{FILE_TYPE_MAP.get(file_type, FILE_TYPE_MAP['unknown'])}\r",
            f"📏 大小：{self._format_file_size(info.get('size', 0))}\r",
            f"📚 包含文件：{info.get('count', 0)}个"
        ]

        screenshots_urls = []
        raw_screenshots = info.get('screenshots')
        if isinstance(raw_screenshots, list) and self.max_screenshots > 0:
            for s in raw_screenshots[:self.max_screenshots]:
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

    async def _download_screenshots(self, screenshots_urls: List[str]) -> List[bytes]:
        """下载截图并返回原始字节列表"""
        if not screenshots_urls:
            return []

        timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [self._fetch_image_bytes(session, url) for url in screenshots_urls]
            results = await asyncio.gather(*tasks)
        return [result for result in results if result]

    async def _fetch_image_bytes(self, session: aiohttp.ClientSession, url: str) -> bytes | None:
        try:
            async with session.get(url) as img_response:
                img_response.raise_for_status()
                return await img_response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:
            logger.warning(f"❌ 下载截图失败 ({url}): {type(e).__name__} - {str(e)}")
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
