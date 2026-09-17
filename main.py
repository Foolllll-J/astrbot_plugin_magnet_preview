import re
import math
import json
import asyncio
import time
import aiohttp
from io import BytesIO
from typing import Any, AsyncGenerator, Dict, List, Tuple
from PIL import Image, ImageFilter

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.star import Star, Context
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Plain, Node, Nodes

DEFAULT_WHATSLINK_URL = "https://whatslink.info"
DEFAULT_TIMEOUT = 10
MAX_FORWARD_DEPTH = 5

FILE_TYPE_MAP = {
    "folder": "📁 文件夹",
    "video": "🎥 视频",
    "image": "🌄 图片",
    "text": "📄 文本",
    "audio": "🎵 音频",
    "archive": "📦 压缩包",
    "document": "📑 文档",
    "unknown": "❓ 其他",
}


class MagnetPreviewer(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        trigger_conf = config.get("trigger_settings") or {}
        output_conf = config.get("output_settings") or {}

        output_mode = output_conf.get("output_mode", "image")
        if output_mode not in ("image", "link"):
            output_mode = "image"
        self.output_mode = output_mode
        self.qq_image_as_forward = output_conf.get("qq_image_as_forward", True)
        self.max_screenshots = max(
            0, min(5, int(output_conf.get("max_screenshot_count", 3)))
        )
        self.cover_mosaic_level = float(output_conf.get("cover_mosaic_level", 0.3))
        self.mask_media_for_telegram = output_conf.get(
            "mask_media_for_telegram", False
        )

        self.max_magnet_count = max(
            1, min(10, int(trigger_conf.get("max_magnet_count", 1)))
        )
        self.auto_parse = trigger_conf.get("auto_parse", True)
        self.enable_emoji_reaction = trigger_conf.get("enable_emoji_reaction", True)
        self.session_whitelist = [
            str(sid) for sid in trigger_conf.get("session_whitelist", [])
        ]
        self.loose_match = trigger_conf.get("loose_match", False)

        self.whatslink_url = DEFAULT_WHATSLINK_URL
        self.api_url = f"{self.whatslink_url}/api/v1/link"

        self._magnet_regex = re.compile(
            r"magnet:\?\s*xt\s*=\s*urn\s*:\s*btih\s*:\s*([a-zA-Z0-9]{32,40})",
            re.IGNORECASE,
        )
        self._command_regex = re.compile(r"text='(.*?)'")
        self._hash_regex = re.compile(r"\b([a-fA-F0-9]{40})\b", re.IGNORECASE)
        self._ed2k_regex = re.compile(
            r"ed2k://\s*\|file\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*([a-fA-F0-9]{32})\s*\|\s*/",
            re.IGNORECASE,
        )
        self._url_regex = re.compile(
            r"\b(?:https?://|www\.)[^\s<>'\"`]+", re.IGNORECASE
        )
        self._link_cache: dict = {}

    async def terminate(self):
        logger.info("磁链预览插件已终止")
        await super().terminate()

    @filter.command("磁链", alias=["磁力", "bt"])
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
        reply_text = ""

        # 检查是否有引用消息
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Reply):
                reply_id = seg.id
                # 优先使用 Reply 组件的 message_str 字段
                if hasattr(seg, "message_str") and seg.message_str:
                    reply_text = seg.message_str
                # 如果 message_str 为空，尝试使用 text 字段
                elif hasattr(seg, "text") and seg.text:
                    reply_text = seg.text
                # 如果都为空，尝试从 chain 中提取
                elif hasattr(seg, "chain") and seg.chain:
                    for chain_seg in seg.chain:
                        if isinstance(chain_seg, Comp.Plain):
                            reply_text += chain_seg.text
                break

        if reply_id:
            # 如果 Reply 组件有文本内容，直接使用
            if reply_text:
                target_text = reply_text
            else:
                # 回退到通过 API 获取引用消息（QQ 平台）
                try:
                    bot = getattr(event, "bot", None)
                    if bot:
                        res = await bot.api.call_action("get_msg", message_id=reply_id)
                        if res and "message" in res:
                            original_message = res["message"]
                            ref_text = ""
                            if isinstance(original_message, list):
                                for segment in original_message:
                                    seg_type = segment.get("type")
                                    seg_data = segment.get("data", {})
                                    if seg_type == "text":
                                        ref_text += seg_data.get("text", "") + " "
                                    elif seg_type == "forward":
                                        fid = seg_data.get("id")
                                        if fid:
                                            texts = await self._extract_forward_text(
                                                event, fid
                                            )
                                            ref_text += " ".join(texts) + " "
                                    elif seg_type == "json":
                                        json_str = seg_data.get("data")
                                        if json_str:
                                            try:
                                                data = json.loads(json_str)
                                                news = (
                                                    data.get("meta", {})
                                                    .get("detail", {})
                                                    .get("news", [])
                                                )
                                                for n in news:
                                                    if "text" in n:
                                                        ref_text += n["text"] + " "
                                            except Exception:
                                                pass
                            elif isinstance(original_message, str):
                                ref_text = original_message

                            if ref_text.strip():
                                target_text = ref_text
                except Exception as e:
                    logger.warning(f"获取引用消息失败: {e}")

        if not target_text and not is_all_numeric:
            target_text = arg

        if reply_id is not None:
            cache_key = (event.unified_msg_origin, reply_id)
            cached = self._link_cache.get(cache_key)
            if cached is not None and time.time() - cached[0] < 300:
                logger.debug(
                    f"[磁链缓存] 命中引用消息 {reply_id}，共 {len(cached[1])} 条磁链"
                )
                all_links = cached[1]
            else:
                all_links = self._extract_all_magnets(target_text)
                self._link_cache[cache_key] = (time.time(), all_links)
        else:
            all_links = self._extract_all_magnets(target_text)

        if not all_links:
            yield event.plain_result(
                "💡 请引用包含磁链的消息，或直接输入：磁链 magnet:?xt=..."
            )
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
                yield event.plain_result(
                    f"⚠️ 目标消息中只有 {len(all_links)} 条磁链，无法解析第 {target_index} 条。"
                )
                return
        else:
            links_to_process = all_links[: self.max_magnet_count]

        async for result in self._process_and_show_magnets(
            event, links_to_process, custom_blur_level
        ):
            yield result

        # 指令触发后阻止事件传播
        yield event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.regex(
        r"(?is).*?(?:magnet:\?\s*xt\s*=\s*urn\s*:\s*btih\s*:\s*[a-zA-Z0-9]{32,40}|ed2k://\s*\|file\|\s*[^|]+\s*\|\s*\d+\s*\|\s*[a-fA-F0-9]{32}\s*\|\s*/|\b[a-fA-F0-9]{40}\b).*"
    )
    async def handle_magnet_regex(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[Any, Any]:
        """正则触发的自动解析"""
        if (not event.is_private_chat()) and event.is_at_or_wake_command:
            return

        # 检查自动解析开关
        if not self.auto_parse:
            return

        # 检查白名单
        if not self._is_allowed(event):
            return

        plain_text = event.message_str
        # 自动解析默认仅处理显式磁链；宽松匹配模式下也匹配裸 40 位哈希
        links = self._extract_all_magnets(plain_text, include_bare_hash=self.loose_match)[
            : self.max_magnet_count
        ]

        if not links:
            return

        # 自动触发时贴表情（仅QQ平台）
        await self._set_emoji(event, 339)

        async for result in self._process_and_show_magnets(event, links):
            yield result

        # 阻止事件继续传播，避免 LLM 等插件重复处理
        yield event.stop_event()

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """检查当前会话是否允许运行。会话级白名单支持群号和私聊用户 ID。"""
        # 如果没有设置白名单，则全部会话都允许
        if not self.session_whitelist:
            return True

        session_id = event.get_group_id() or event.get_sender_id()
        if not session_id:
            return False

        # 处理 Telegram 群组 ID（可能包含 # 后缀）
        session_id = str(session_id).split("#")[0]
        return session_id in self.session_whitelist

    def _get_platform_name(self, event: AstrMessageEvent) -> str:
        """获取平台名，优先事件方法，失败时回退 unified_msg_origin 前缀。"""
        try:
            platform_name = event.get_platform_name()
            if platform_name:
                return str(platform_name)
        except Exception:
            pass

        umo = getattr(event, "unified_msg_origin", "") or ""
        if ":" in umo:
            return umo.split(":", 1)[0]
        return "unknown"

    def _is_aiocqhttp_platform(self, event: AstrMessageEvent) -> bool:
        """当前是否为 QQ(aiocqhttp) 平台。"""
        return self._get_platform_name(event) == "aiocqhttp"

    def _is_telegram_platform(self, event: AstrMessageEvent) -> bool:
        """当前是否为 Telegram 平台"""
        return self._get_platform_name(event) == "telegram"

    async def _send_telegram_album(
        self,
        event: AstrMessageEvent,
        infos: List[str],
        image_bytes_list: List[bytes],
        has_spoiler: bool = False,
    ) -> bool:
        """使用 Telegram Bot API 发送相册形式的消息，返回是否发送成功。"""
        try:
            from telegram import InputMediaPhoto
            from telegram.ext import ExtBot

            tg_bot = getattr(event, "client", None)
            if not tg_bot or not isinstance(tg_bot, ExtBot):
                logger.warning("无法获取 Telegram Bot 实例，回退到普通发送方式")
                return False

            chat_id = event.get_group_id() or event.get_sender_id()
            # 处理 Telegram 群组 ID（可能包含 # 后缀）
            chat_id = str(chat_id).split("#")[0]

            # 构建媒体组，使用 Telegram 原生 spoiler 功能
            media_group = [
                InputMediaPhoto(media=img_bytes, has_spoiler=has_spoiler)
                for img_bytes in image_bytes_list
            ]

            if not media_group:
                return False

            # 第一张图片带完整文本作为说明
            caption = "\n".join(infos)
            if len(caption) > 1024:
                caption = caption[:1020] + "..."
            media_group[0] = InputMediaPhoto(
                media=media_group[0].media, caption=caption, has_spoiler=has_spoiler
            )

            # 发送媒体组
            await tg_bot.send_media_group(chat_id=chat_id, media=media_group)
            return True

        except ImportError:
            logger.warning("未安装 telegram 库，无法使用相册功能")
            return False
        except Exception as e:
            logger.error(f"发送 Telegram 相册失败: {e}")
            return False

    def _extract_all_magnets(
        self, text: str, include_bare_hash: bool = True
    ) -> List[str]:
        """从文本中提取所有磁力链接和 ed2k 链接（去重）"""
        links = []
        seen_hashes = set()
        seen_ed2k = set()
        url_spans = [m.span() for m in self._url_regex.finditer(text)]

        # 1. 提取磁力链接
        for match in self._magnet_regex.finditer(text):
            info_hash = match.group(1).upper()
            if info_hash not in seen_hashes:
                links.append(f"magnet:?xt=urn:btih:{info_hash}")
                seen_hashes.add(info_hash)

        # 2. 提取裸哈希（可选），并过滤 URL 内部片段，避免误识别网站链接
        if include_bare_hash:
            for match in self._hash_regex.finditer(text):
                if self._is_span_in_url(match.span(), url_spans):
                    continue
                info_hash = match.group(1).upper()
                if info_hash not in seen_hashes:
                    links.append(f"magnet:?xt=urn:btih:{info_hash}")
                    seen_hashes.add(info_hash)

        # 3. 提取 ed2k 链接
        for match in self._ed2k_regex.finditer(text):
            ed2k_hash = match.group(3).upper()
            if ed2k_hash not in seen_ed2k:
                links.append(match.group(0))
                seen_ed2k.add(ed2k_hash)

        return links

    def _is_span_in_url(
        self, span: Tuple[int, int], url_spans: List[Tuple[int, int]]
    ) -> bool:
        """判断匹配片段是否位于 URL 内"""
        start, end = span
        for url_start, url_end in url_spans:
            if start < url_end and end > url_start:
                return True
        return False

    async def _extract_forward_text(
        self, event: AstrMessageEvent, forward_id: str, depth: int = 0
    ) -> List[str]:
        """提取合并转发消息中的文本内容"""
        if depth > MAX_FORWARD_DEPTH:
            return ["[已达到最大转发嵌套深度，后续内容省略]"]
        extracted_texts = []
        try:
            bot = getattr(event, "bot", None) or getattr(
                event.bot_event, "client", None
            )
            if bot:
                forward_data = await bot.api.call_action(
                    "get_forward_msg", id=forward_id
                )
                if forward_data and "messages" in forward_data:
                    for msg_node in forward_data["messages"]:
                        content = msg_node.get("message") or msg_node.get("content", [])
                        if (
                            isinstance(content, list)
                            and len(content) == 1
                            and isinstance(content[0], dict)
                            and content[0].get("type") == "forward"
                        ):
                            nested_id = content[0].get("data", {}).get("id")
                            if nested_id:
                                nested_texts = await self._extract_forward_text(
                                    event, nested_id, depth + 1
                                )
                                extracted_texts.extend(nested_texts)
                                continue
                        node_text = self._parse_node_content(msg_node)
                        if node_text:
                            extracted_texts.append(node_text)
                else:
                    logger.warning(
                        f"合并转发数据中未找到 messages 字段: {forward_data}"
                    )
        except Exception as e:
            logger.warning(f"提取转发消息失败: {e}")
        return extracted_texts

    def _parse_node_content(self, node: Dict[str, Any]) -> str:
        """解析单个消息节点的文本内容，支持多种结构"""
        # 优先从 message 或 content 字段获取内容
        content = node.get("message") or node.get("content")
        if not content:
            return ""

        # 1. 如果内容是字符串（可能是 JSON 序列化后的）
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    content = parsed
            except (json.JSONDecodeError, TypeError):
                return content

        # 2. 如果内容是列表（标准的 MessageChain 结构）
        text_parts = []
        if isinstance(content, list):
            for segment in content:
                if isinstance(segment, dict):
                    seg_type = segment.get("type")
                    seg_data = segment.get("data", {})
                    if seg_type == "text":
                        text_parts.append(seg_data.get("text", ""))
                    elif seg_type == "forward":
                        # 处理嵌套转发
                        nested_id = seg_data.get("id")
                        if nested_id:
                            pass
                        nested_content = seg_data.get("content")
                        if isinstance(nested_content, list):
                            for n_node in nested_content:
                                text_parts.append(self._parse_node_content(n_node))
                elif isinstance(segment, str):
                    text_parts.append(segment)

        return "".join(text_parts).strip()

    async def _process_and_show_magnets(
        self, event: AstrMessageEvent, links: List[str], custom_blur: float = None
    ) -> AsyncGenerator[Any, Any]:
        """统一的磁链处理和展示流程"""
        all_results = []
        for link in links:
            data = await self._fetch_magnet_info(link)

            if (
                not data
                or data.get("error")
                or self._is_unresolved_parse_result(data, link)
            ):
                error_msg = data.get("name", "未知错误") if data else "API无响应"
                if data and self._is_unresolved_parse_result(data, link):
                    error_msg = "未解析到有效资源信息，可能是无效磁链"
                all_results.append(
                    (
                        [
                            f"⚠️ 解析失败 ({link}): {error_msg.split('contact')[0].strip()}"
                        ],
                        [],
                    )
                )
            else:
                infos, screenshots_urls = self._sort_infos_and_get_urls(data)
                all_results.append((infos, screenshots_urls))

        if not all_results:
            return

        async for result in self._generate_preview_result(
            event, all_results, custom_blur
        ):
            yield result

    async def _set_emoji(self, event: AstrMessageEvent, emoji_id: int):
        """给消息贴表情（仅支持QQ平台）"""
        if not self.enable_emoji_reaction:
            return

        if not self._is_aiocqhttp_platform(event):
            return

        try:
            bot = getattr(event, "bot", None)
            if not bot:
                logger.debug("无法获取 bot 实例")
                return
            await bot.set_msg_emoji_like(
                message_id=event.message_obj.message_id,
                emoji_id=emoji_id,
                set=True,
            )
        except Exception as e:
            logger.debug(f"贴表情失败: {e}")

    async def _generate_preview_result(
        self,
        event: AstrMessageEvent,
        all_results: List[Tuple[List[str], List[str]]],
        custom_blur: float = None,
    ) -> AsyncGenerator[Any, Any]:
        """生成并发送预览结果，支持多个磁链。

        - 直链模式：所有平台统一发送文本 + 截图直链，不发送图片，无回退。
        - 图片模式：Telegram 走原生相册（不模糊，用原生遮罩）；QQ 走合并转发
          （可用 qq_image_as_forward 关闭，发送失败时退化为单条消息）；
          其余平台将文本与图片合并为一条消息直接发送。
        - 图片模式任何环节失败（拿不到图 / 处理异常 / 相册失败）统一回退到直链模式。
        """
        is_telegram = self._is_telegram_platform(event)
        is_qq = self._is_aiocqhttp_platform(event)

        # 指定了 custom_blur 时强制使用图片模式
        force_image_mode = custom_blur is not None
        is_image_mode = self.output_mode == "image" or force_image_mode

        if not is_image_mode:
            async for result in self._generate_link_result(event, all_results):
                yield result
            return

        processed: List[Tuple[int, str, List[Any]]] = []

        try:
            if is_telegram:
                # Telegram 使用原生相册，不做模糊处理（改用原生遮罩）
                if await self._send_telegram_album_result(event, all_results):
                    return
            else:
                processed = await self._prepare_image_results(all_results, custom_blur)
        except Exception as e:
            logger.warning(f"图片模式处理失败，回退为直链输出: {e}")
            processed = []

        if processed and any(images for _, _, images in processed):
            # QQ 特殊处理：默认以合并转发承载图片，发送失败则退化为单条消息
            if is_qq and self.qq_image_as_forward:
                nodes = self._build_image_nodes(event, processed, len(all_results))
                try:
                    await event.send(MessageChain([Nodes(nodes=nodes)]))
                    return
                except Exception as e:
                    logger.warning(f"图片合并转发发送失败，回退为单条消息: {e}")

            yield event.chain_result(self._build_image_chain(processed))
            return

        # 统一回退：图片模式失败一律回退到直链模式
        async for result in self._generate_link_result(event, all_results):
            yield result

    async def _generate_link_result(
        self,
        event: AstrMessageEvent,
        all_results: List[Tuple[List[str], List[str]]],
    ) -> AsyncGenerator[Any, Any]:
        """直链模式：所有平台统一发送文本 + 截图直链，不发送图片。"""
        for part_text in self._split_text_by_length(
            self._join_text_results(all_results), 4000
        ):
            if part_text:
                yield event.plain_result(part_text)

    async def _send_telegram_album_result(
        self,
        event: AstrMessageEvent,
        all_results: List[Tuple[List[str], List[str]]],
    ) -> bool:
        """Telegram：使用原生相册发送预览图，可选原生遮罩。返回是否已成功发送。"""
        all_infos = []
        all_image_bytes = []

        for i, (infos, screenshots_urls) in enumerate(all_results):
            if len(all_results) > 1:
                all_infos.append(f"🔗 磁链预览 #{i + 1}")
            all_infos.extend(infos)

            if screenshots_urls:
                image_bytes_list = await self._download_screenshots(screenshots_urls)
                all_image_bytes.extend(image_bytes_list)

        if not all_image_bytes:
            return False

        return await self._send_telegram_album(
            event, all_infos, all_image_bytes, self.mask_media_for_telegram
        )

    async def _prepare_image_results(
        self,
        all_results: List[Tuple[List[str], List[str]]],
        custom_blur: float = None,
    ) -> List[Tuple[int, str, List[Any]]]:
        """下载并处理全部预览图（含模糊），返回 [(序号, 展示文本, 图片组件)]。"""
        processed: List[Tuple[int, str, List[Any]]] = []

        for i, (infos, screenshots_urls) in enumerate(all_results):
            image_bytes_list = await self._download_screenshots(screenshots_urls)

            display_infos = list(infos)
            if len(all_results) > 1:
                display_infos.insert(0, f"🔗 磁链预览 #{i + 1}")
            if screenshots_urls:
                display_infos.append(
                    f"\n📸 预览截图 (成功 {len(image_bytes_list)}/{len(screenshots_urls)} 张):"
                )

            blur_level = (
                custom_blur if custom_blur is not None else self.cover_mosaic_level
            )
            image_components = []
            for img_bytes in image_bytes_list:
                if blur_level is not None:
                    img_bytes = self._apply_mosaic(img_bytes, blur_level)
                image_components.append(Comp.Image.fromBytes(img_bytes))

            processed.append((i, "\n".join(display_infos), image_components))

        return processed

    def _build_image_nodes(
        self,
        event: AstrMessageEvent,
        processed: List[Tuple[int, str, List[Any]]],
        total: int,
    ) -> List[Node]:
        """构建图片模式的合并转发节点（文本节点 + 图片节点）。"""
        sender_id = event.get_self_id()
        nodes: List[Node] = []

        for i, info_text, image_components in processed:
            for part_text in self._split_text_by_length(info_text, 4000):
                nodes.append(
                    Node(
                        uin=sender_id,
                        name=f"磁力预览信息 ({i + 1})" if total > 1 else "磁力预览信息",
                        content=[Plain(text=part_text)],
                    )
                )

            for image_component in image_components:
                nodes.append(
                    Node(
                        uin=sender_id,
                        name=f"预览截图 ({i + 1})" if total > 1 else "预览截图",
                        content=[image_component],
                    )
                )

        return nodes

    @staticmethod
    def _build_image_chain(processed: List[Tuple[int, str, List[Any]]]) -> List[Any]:
        """将文本与图片合并为一条消息链。"""
        chain: List[Any] = []
        texts = [info_text for _, info_text, _ in processed]
        if texts:
            chain.append(Plain(text="\n\u200b\n".join(texts)))
        for _, _, image_components in processed:
            chain.extend(image_components)
        return chain

    def _split_text_by_length(self, text: str, max_length: int = 4000) -> List[str]:
        """将文本按指定长度分割成一个字符串列表"""
        return [text[i : i + max_length] for i in range(0, len(text), max_length)]

    def _sort_infos_and_get_urls(self, info: dict) -> Tuple[List[str], List[str]]:
        file_type = str(info.get("file_type", "unknown")).lower()
        base_info = [
            "🔍 解析结果：",
            f"📝 名称：{info.get('name', '未知')}",
            f"📦 类型：{FILE_TYPE_MAP.get(file_type, FILE_TYPE_MAP['unknown'])}",
            f"📏 大小：{self._format_file_size(info.get('size', 0))}",
            f"📚 包含文件：{info.get('count', 0)}个",
        ]

        screenshots_urls = []
        raw_screenshots = info.get("screenshots")
        if isinstance(raw_screenshots, list) and self.max_screenshots > 0:
            for s in raw_screenshots[: self.max_screenshots]:
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
            message += "\n\u200b\n📸 预览截图链接："
            for i, url in enumerate(screenshots_urls):
                message += f"\n- 截图 {i + 1}: {url}"

        return message

    def _format_result_with_index(
        self,
        index: int,
        infos: List[str],
        screenshots_urls: List[str],
        total_results: int,
    ) -> str:
        """为多结果场景补齐统一标题，供直链模式复用。"""
        result_text = self._format_text_result(infos, screenshots_urls)
        if total_results > 1:
            result_text = f"🔗 磁链预览 #{index + 1}\n\u200b\n" + result_text
        return result_text

    def _join_text_results(self, all_results: List[Tuple[List[str], List[str]]]) -> str:
        """将多条结果拼接为纯文本，供最终兜底发送。"""
        texts = []
        total_results = len(all_results)
        for index, (infos, screenshots_urls) in enumerate(all_results):
            texts.append(
                self._format_result_with_index(
                    index, infos, screenshots_urls, total_results
                )
            )
        return "\n\u200b\n".join(texts)

    async def _fetch_magnet_info(self, magnet_link: str) -> Dict | None:
        """异步调用Whatslink API获取磁力信息"""
        params = {"url": magnet_link}
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (MagnetPreviewer)",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.api_url,
                    params=params,
                    headers=headers,
                    ssl=False,
                    timeout=DEFAULT_TIMEOUT,
                ) as resp:
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

    def _is_unresolved_parse_result(self, info: Dict | None, link: str) -> bool:
        """识别上游接口未真正解析出资源信息时返回的占位结果。"""
        if not isinstance(info, dict):
            return False

        hash_value = None
        hash_match = self._magnet_regex.search(link or "")
        if hash_match:
            hash_value = hash_match.group(1).upper()
        else:
            ed2k_match = self._ed2k_regex.search(link or "")
            if ed2k_match:
                hash_value = ed2k_match.group(3).upper()

        if not hash_value:
            return False

        name = str(info.get("name", "") or "").strip().upper()
        file_type = str(info.get("file_type", "unknown") or "unknown").strip().lower()

        try:
            size = int(info.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0

        try:
            count = int(info.get("count", 0) or 0)
        except (TypeError, ValueError):
            count = 0

        screenshots = info.get("screenshots")
        has_screenshots = isinstance(screenshots, list) and len(screenshots) > 0

        return (
            name == hash_value
            and file_type in {"unknown", "other"}
            and size <= 0
            and count <= 1
            and not has_screenshots
        )

    async def _download_screenshots(self, screenshots_urls: List[str]) -> List[bytes]:
        """下载截图并返回原始字节列表"""
        if not screenshots_urls:
            return []

        timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [self._fetch_image_bytes(session, url) for url in screenshots_urls]
            results = await asyncio.gather(*tasks)
        return [result for result in results if result]

    async def _fetch_image_bytes(
        self, session: aiohttp.ClientSession, url: str
    ) -> bytes | None:
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
        return (
            image_url.replace("https://whatslink.info", self.whatslink_url)
            if image_url
            else ""
        )

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

        size = size_bytes / (1024**unit_index)
        return f"{size:.2f} {units[unit_index]}"
