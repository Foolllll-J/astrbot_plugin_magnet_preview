import re
import math
import asyncio
import aiohttp
from io import BytesIO
from typing import Any, AsyncGenerator, Dict, List, Tuple
from PIL import Image, ImageFilter

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
    'image': '🌄 图片',
    'text': '📄 文本',
    'audio': '🎵 音频',
    'archive': '📦 压缩包',
    'document': '📑 文档',
    'unknown': '❓ 其他'
}

@register("astrbot_plugin_magnet_preview", "Foolllll", "磁链预览助手", "1.2.1")
class MagnetPreviewer(Star):
    
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        self.output_as_link = config.get("output_as_link", False)
        self.max_screenshots = max(0, min(5, int(config.get("max_screenshot_count", 3))))
        self.cover_mosaic_level = float(config.get("cover_mosaic_level", 0.3))
        self.max_magnet_count = max(1, min(10, int(config.get("max_magnet_count", 1))))
        self.auto_parse = config.get("auto_parse", True)
        self.enable_emoji_reaction = config.get("enable_emoji_reaction", True)
        self.group_whitelist = [str(gid) for gid in config.get("group_whitelist", [])]

        self.whatslink_url = DEFAULT_WHATSLINK_URL
        self.api_url = f"{self.whatslink_url}/api/v1/link"

        self._magnet_regex = re.compile(r"magnet:\?xt=urn:btih:([a-zA-Z0-9]{32,40})", re.IGNORECASE)
        self._command_regex = re.compile(r"text='(.*?)'")
        self._hash_regex = re.compile(r"\b([a-fA-F0-9]{40})\b", re.IGNORECASE)
        
    async def terminate(self):
        logger.info("磁链预览插件已终止")
        await super().terminate()

    @filter.command("磁链", alias=["磁力"])
    async def magnet_cmd(self, event: AstrMessageEvent):
        """磁链解析指令，支持引用消息解析和直接输入"""
        if not self._is_allowed(event):
            return
            
        full_msg = event.message_str.strip()
        parts = full_msg.split(maxsplit=1)
        arg = parts[1] if len(parts) > 1 else ""

        target_text = ""
        target_index = -1
        custom_blur_level = None

        args = arg.split()
        
        is_all_numeric = True
        for a in args:
            if not a.isdigit():
                is_all_numeric = False
                break 
        
        if not is_all_numeric:
            target_text = arg 
            
        reply_id = None
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Reply):
                reply_id = seg.id
                break
        
        if reply_id:
            try:
                bot = getattr(event, 'bot', None) or getattr(event.bot_event, 'client', None)
                if bot:
                    res = await bot.api.call_action('get_msg', message_id=reply_id)
                    if res and 'message' in res:
                        original_message = res['message']
                        ref_text = ""
                        if isinstance(original_message, list):
                            for segment in original_message:
                                seg_type = segment.get("type")
                                seg_data = segment.get("data", {})
                                if seg_type == "text":
                                    ref_text += seg_data.get("text", "") + " "
                                elif seg_type == "forward":
                                    forward_id = seg_data.get("id")
                                    if forward_id:
                                        texts = await self._extract_forward_text(event, forward_id)
                                        ref_text += " ".join(texts) + " "
                        elif isinstance(original_message, str):
                            ref_text = original_message
                        
                        if ref_text.strip():
                            target_text = ref_text
            except Exception as e:
                logger.warning(f"获取引用消息失败: {e}")
        
        if not target_text and not is_all_numeric:
            target_text = arg
        
        all_links = self._extract_all_magnets(target_text)
        
        if not all_links:
            yield event.plain_result("💡 请引用包含磁链的消息，或直接输入：磁链 magnet:?xt=...")
            return

        if is_all_numeric and len(args) > 0:
            if len(args) >= 2:
                target_index = int(args[0])
                blur_val = int(args[1])
                custom_blur_level = max(0, min(10, blur_val)) / 10.0
            
            elif len(args) == 1:
                val = int(args[0])
                if len(all_links) == 1:
                    target_index = 1 
                    custom_blur_level = max(0, min(10, val)) / 10.0
                else:
                    target_index = val

        links_to_process = []
        if target_index > 0:
            if target_index <= len(all_links):
                links_to_process = [all_links[target_index - 1]]
            else:
                yield event.plain_result(f"⚠️ 目标消息中只有 {len(all_links)} 条磁链，无法解析第 {target_index} 条。")
                return
        else:
            links_to_process = all_links[:self.max_magnet_count]

        async for result in self._process_and_show_magnets(event, links_to_process, custom_blur_level):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.regex(r"(?s).*?(magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}|[a-fA-F0-9]{40}).*")
    async def handle_magnet_regex(self, event: AstrMessageEvent) -> AsyncGenerator[Any, Any]:
        """正则触发的自动解析"""
        # 检查自动解析开关
        if not self.auto_parse:
            return

        # 检查白名单
        if not self._is_allowed(event):
            return

        # 如果消息是以指令开头的，则不触发正则逻辑，避免重复触发
        if event.message_str.startswith(("磁链", "磁力", "/磁链", "/磁力")):
            return

        plain_text = event.message_str
        links = self._extract_all_magnets(plain_text)[:self.max_magnet_count]
        
        if not links:
            return

        # 自动触发时贴表情
        await self._set_emoji(event, 339)

        async for result in self._process_and_show_magnets(event, links):
            yield result

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """检查当前场景是否允许运行。私聊场景不受群组白名单限制"""
        # 如果是私聊场景，直接允许
        if not event.get_group_id():
            return True
            
        # 如果没有设置白名单，则所有群组都允许
        if not self.group_whitelist:
            return True
        
        gid = event.get_group_id()
        return str(gid) in self.group_whitelist

    def _extract_all_magnets(self, text: str) -> List[str]:
        """从文本中提取所有磁力链接（去重）"""
        links = []
        seen_hashes = set()
        
        # 1. 提取磁力链接
        for match in self._magnet_regex.finditer(text):
            info_hash = match.group(1).upper()
            if info_hash not in seen_hashes:
                links.append(f"magnet:?xt=urn:btih:{info_hash}")
                seen_hashes.add(info_hash)
            
        # 2. 提取裸哈希
        for match in self._hash_regex.finditer(text):
            info_hash = match.group(1).upper()
            if info_hash not in seen_hashes:
                links.append(f"magnet:?xt=urn:btih:{info_hash}")
                seen_hashes.add(info_hash)
        
        return links

    async def _extract_forward_text(self, event: AstrMessageEvent, forward_id: str) -> List[str]:
        """提取合并转发消息中的文本内容"""
        extracted_texts = []
        try:
            # 尝试获取适配器调用 API
            bot = getattr(event, 'bot', None) or getattr(event.bot_event, 'client', None)
            if bot:
                forward_data = await bot.api.call_action('get_forward_msg', id=forward_id)
                if forward_data and "messages" in forward_data:
                    for msg_node in forward_data["messages"]:
                        content = msg_node.get("message") or msg_node.get("content", [])
                        if isinstance(content, list):
                            for segment in content:
                                if segment.get("type") == "text":
                                    extracted_texts.append(segment.get("data", {}).get("text", ""))
                        elif isinstance(content, str):
                            extracted_texts.append(content)
        except Exception as e:
            logger.warning(f"提取转发消息失败: {e}")
        return extracted_texts

    async def _process_and_show_magnets(self, event: AstrMessageEvent, links: List[str], custom_blur: float = None) -> AsyncGenerator[Any, Any]:
        """统一的磁链处理和展示流程"""
        all_results = []
        for link in links:
            data = await self._fetch_magnet_info(link)
            
            if not data or data.get('error'):
                error_msg = data.get('name', '未知错误') if data else 'API无响应'
                all_results.append(([f"⚠️ 解析失败 ({link}): {error_msg.split('contact')[0].strip()}"], []))
            else:
                infos, screenshots_urls = self._sort_infos_and_get_urls(data)
                all_results.append((infos, screenshots_urls))

        if not all_results:
            return

        if len(all_results) == 1:
            infos, screenshots_urls = all_results[0]
            force_image_mode = custom_blur is not None
            
            if (self.output_as_link and not force_image_mode) or not screenshots_urls:
                yield event.plain_result(self._format_text_result(infos, screenshots_urls))
            else:
                async for result in self._generate_multi_forward_result(event, all_results, custom_blur):
                    yield result
        else:
            async for result in self._generate_multi_forward_result(event, all_results, custom_blur):
                yield result

    async def _set_emoji(self, event: AstrMessageEvent, emoji_id: int):
        """给消息贴表情"""
        if not self.enable_emoji_reaction:
            return

        try:
            await event.bot.set_msg_emoji_like( 
                message_id=event.message_obj.message_id, 
                emoji_id=emoji_id, 
                set=True, 
            ) 
        except Exception as e:
            logger.debug(f"贴表情失败: {e}")

    async def _generate_multi_forward_result(self, event: AstrMessageEvent, all_results: List[Tuple[List[str], List[str]]], custom_blur: float = None) -> AsyncGenerator[Any, Any]:
        """生成并发送合并转发消息，支持多个磁链结果（包含图片模式和直链模式）"""
        sender_id = event.get_self_id()
        forward_nodes: List[Node] = []
        
        # 如果指定了 custom_blur，强制使用图片模式
        force_image_mode = custom_blur is not None

        for i, (infos, screenshots_urls) in enumerate(all_results):
            if self.output_as_link and not force_image_mode:
                # 1. 直链模式：直接将包含链接的文本作为节点
                res_text = self._format_text_result(infos, screenshots_urls)
                if len(all_results) > 1:
                    res_text = f"🔗 磁链预览 #{i+1}\n\n" + res_text
                
                split_texts = self._split_text_by_length(res_text, 4000)
                for part_text in split_texts:
                    node_name = f"磁力预览信息 ({i+1})" if len(all_results) > 1 else "磁力预览信息"
                    forward_nodes.append(Node(uin=sender_id, name=node_name, content=[Plain(text=part_text)]))
            else:
                # 2. 图片模式：下载图片并分节点展示
                image_bytes_list = await self._download_screenshots(screenshots_urls)
                
                # 准备文本信息
                display_infos = list(infos)
                if len(all_results) > 1:
                    display_infos.insert(0, f"🔗 磁链预览 #{i+1}")

                if screenshots_urls:
                    display_infos.append(f"\n📸 预览截图 (成功 {len(image_bytes_list)}/{len(screenshots_urls)} 张):")

                info_text = "\n".join(display_infos)
                split_texts = self._split_text_by_length(info_text, 4000)

                # 添加文本节点
                for j, part_text in enumerate(split_texts):
                    node_name = "磁力预览信息"
                    if len(all_results) > 1:
                        node_name += f" ({i+1})"
                    forward_nodes.append(Node(uin=sender_id, name=node_name, content=[Plain(text=part_text)]))

                # 添加图片节点
                # 确定使用的模糊度
                blur_level = custom_blur if custom_blur is not None else self.cover_mosaic_level

                for img_bytes in image_bytes_list:
                    if blur_level is not None:
                        img_bytes = self._apply_mosaic(img_bytes, blur_level)
                    image_component = Comp.Image.fromBytes(img_bytes)
                    node_name = "预览截图"
                    if len(all_results) > 1:
                        node_name += f" ({i+1})"
                    forward_nodes.append(Node(uin=sender_id, name=node_name, content=[image_component]))

        if not forward_nodes:
            yield event.plain_result("⚠️ 未能生成有效的预览内容。")
            return

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

    def _apply_mosaic(self, image_data: bytes, level: float = None) -> bytes:
        """应用高斯模糊打码"""
        mosaic_level = level if level is not None else self.cover_mosaic_level
        
        if mosaic_level <= 0:
            return image_data

        try:
            with Image.open(BytesIO(image_data)) as img:
                # 转换为 RGB，防止 RGBA 等格式保存为 JPEG 时出错
                if img.mode != "RGB":
                    img = img.convert("RGB")
                
                # mosaic_level 为 0.0-1.0，转换为模糊半径
                blur_radius = mosaic_level * 10
                
                if blur_radius > 0:
                    img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
                
                buffered = BytesIO()
                img.save(buffered, format="JPEG", quality=85)
                return buffered.getvalue()
        except Exception as e:
            logger.error(f"应用模糊失败: {e}")
            return image_data

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
