"""
沉星湾 v1.0
设计叶枔枖 & 沈砚清，编写叶克宝。

一个海边小镇。不大。能走完的那种。
不打怪。不升级。散步、聊天、捡贝壳、钓鱼、寄信回家。
"""

import json
import os
import random
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from copy import deepcopy
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

from .town_data import (
    LOCATIONS, LOCATION_ALIASES, NPCS, SHOP_ITEMS,
    BEACH_FINDS, FISHING_RESULTS, WEATHERS, WEATHER_WEIGHTS,
    TIME_PERIODS, RANDOM_EVENTS, PERFORMANCE_EFFECTS,
    KEBAO_RESPONSES, KEBAO_LUCKY_ITEMS, AUTO_ROAM_LOGS,
    BOTTLE_MESSAGES, FOOD_MENU, WANDERING_VENDORS,
    FIREWORK_DESCRIPTIONS, FOOD_NPC,
    FISH_PRICES, SHELL_PRICES, GARDEN_PLANTS, NPC_JOBS,
    STALL_PRICE_MULTIPLIER, EXCHANGE_CATALOG,
)

TARGET_QQ = ""
MSK = timezone(timedelta(hours=3))
INITIAL_MONEY = 100


@register("seaside_town", "叶枔枖 & 叶克宝",
          "沉星湾 v1.0 - 设计叶枔枖 & 沈砚清，编写叶克宝。", "1.0.0")
class SeasideTown(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        base = os.path.dirname(os.path.abspath(__file__))
        self.state_path = os.path.join(base, "town_state.json")
        self.mail_path = os.path.join(base, "mailbox.json")
        self.npc_history_path = os.path.join(base, "npc_history.json")
        self.state = self._load(self.state_path, self._default_state())
        self.mailbox = self._load(self.mail_path, {"letters": [], "postcards": []})
        self.npc_history = self._load(self.npc_history_path, {})
        # 合并食堂NPC到主NPC表
        self.all_npcs = {**NPCS, **FOOD_NPC}
        # 读取插件配置
        self.config = config or {}
        self.npc_mode = self.config.get("npc_mode", "card")
        self.npc_provider_id = self.config.get("npc_provider_id", "")
        self.npc_api_base = self.config.get("npc_api_base", "")
        self.npc_api_key = self.config.get("npc_api_key", "")
        self.npc_model = self.config.get("npc_model", "deepseek-chat")

    # ═══════════════════════════════════════
    #  数据管理
    # ═══════════════════════════════════════

    def _default_state(self) -> dict:
        return {
            "location": "听潮街",
            "weather": "sunny",
            "time_period": "morning",
            "money": INITIAL_MONEY,
            "savings": 0,
            "backpack": [],
            "diary": [],
            "npc_memory": {},
            "visited": [],
            "auto_roam": False,
            "day_count": 1,
            "garden": {},
            "stall_items": [],
            "jobs_today": [],
            "total_earned": 0,
        }

    def _load(self, path, default) -> dict:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return deepcopy(default)

    def _save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存状态失败: {e}")

    def _save_mail(self):
        try:
            with open(self.mail_path, "w", encoding="utf-8") as f:
                json.dump(self.mailbox, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存信箱失败: {e}")

    def _save_npc_history(self):
        try:
            with open(self.npc_history_path, "w", encoding="utf-8") as f:
                json.dump(self.npc_history, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"保存NPC历史失败: {e}")

    async def _call_npc_ai(self, npc_name: str, npc: dict, user_msg: str = "") -> str | None:
        """调用AI生成NPC对话。返回NPC的台词，失败返回None。"""

        # 构建system prompt
        w = WEATHERS[self.state["weather"]]
        tp = TIME_PERIODS[self.state["time_period"]]
        meet_count = self.state["npc_memory"].get(npc_name, 0)

        system_prompt = (
            f"你正在扮演沉星湾小镇的一个角色。\n"
            f"角色名：{npc_name}\n"
            f"身份：{npc['title']}\n"
            f"性格：{npc['personality']}\n"
            f"话题：{npc['topics']}\n"
            f"当前位置：{npc['location']}\n"
            f"天气：{w['name']}\n"
            f"时间：{tp['name']}\n"
            f"这是你们第{meet_count + 1}次见面。\n\n"
            f"要求：\n"
            f"- 完全沉浸在角色中，用角色的口吻说话\n"
            f"- 回复简短自然，像真实对话，一般2-4句话\n"
            f"- 符合角色性格（话少的角色就少说，话多的就多说）\n"
            f"- 可以提到小镇里的其他人和事\n"
            f"- 不要加任何旁白、动作描写或括号说明\n"
            f"- 只输出角色说的话"
        )

        # 获取对话历史
        history = self.npc_history.get(npc_name, [])
        messages = [{"role": "system", "content": system_prompt}]
        # 最近10轮对话
        for h in history[-10:]:
            messages.append(h)
        if user_msg:
            messages.append({"role": "user", "content": user_msg})
        else:
            messages.append({"role": "user", "content": "(走过来打招呼)"})

        # 模式1：使用AstrBot内置provider
        if self.npc_mode == "provider" and self.npc_provider_id:
            try:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=self.npc_provider_id,
                    prompt=messages,
                )
                reply = llm_resp.completion_text
                # 保存对话历史
                if user_msg:
                    history.append({"role": "user", "content": user_msg})
                else:
                    history.append({"role": "user", "content": "(打招呼)"})
                history.append({"role": "assistant", "content": reply})
                if len(history) > 20:
                    history = history[-20:]
                self.npc_history[npc_name] = history
                self._save_npc_history()
                return reply
            except Exception as e:
                logger.error(f"调用provider失败: {e}")
                return None

        # 模式2：使用独立API
        if self.npc_mode == "api" and self.npc_api_base and self.npc_api_key:
            try:
                url = f"{self.npc_api_base.rstrip('/')}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {self.npc_api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.npc_model,
                    "messages": messages,
                    "max_tokens": 200,
                    "temperature": 0.8,
                }
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            reply = data["choices"][0]["message"]["content"]
                            # 保存对话历史
                            if user_msg:
                                history.append({"role": "user", "content": user_msg})
                            else:
                                history.append({"role": "user", "content": "(打招呼)"})
                            history.append({"role": "assistant", "content": reply})
                            if len(history) > 20:
                                history = history[-20:]
                            self.npc_history[npc_name] = history
                            self._save_npc_history()
                            return reply
                        else:
                            logger.error(f"NPC API返回 {resp.status}")
                            return None
            except Exception as e:
                logger.error(f"调用NPC API失败: {e}")
                return None

        # 模式3：card模式，返回None让调用方输出角色卡
        return None

    def _now(self) -> datetime:
        return datetime.now(MSK)

    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _time_str(self) -> str:
        return self._now().strftime("%H:%M")

    def _check_perm(self, event: AstrMessageEvent) -> bool:
        if not TARGET_QQ:
            return True
        return str(event.get_sender_id()) == TARGET_QQ

    def _get_time_period(self) -> str:
        hour = self._now().hour
        if 6 <= hour < 11:
            return "morning"
        elif 11 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 20:
            return "evening"
        else:
            return "night"

    def _update_weather(self):
        """基于日期生成当天天气（同一天同天气）"""
        seed = hashlib.md5(self._today().encode()).hexdigest()
        rng = random.Random(seed)
        time_p = self._get_time_period()
        if time_p == "night" and rng.random() < 0.3:
            self.state["weather"] = "starry"
        else:
            weathers = list(WEATHER_WEIGHTS.keys())
            weights = list(WEATHER_WEIGHTS.values())
            self.state["weather"] = rng.choices(weathers, weights)[0]
        self.state["time_period"] = time_p

    def _resolve_location(self, name: str) -> str | None:
        if name in LOCATIONS:
            return name
        if name in LOCATION_ALIASES:
            return LOCATION_ALIASES[name]
        for loc in LOCATIONS:
            if name in loc:
                return loc
        return None

    def _get_scene_desc(self, location: str) -> str:
        loc = LOCATIONS[location]
        w = self.state["weather"]
        tp = self.state["time_period"]
        if tp == "night" and "desc_night" in loc:
            return loc["desc_night"]
        key = f"desc_{w}"
        if key in loc:
            return loc[key]
        return loc.get("desc_sunny", "")

    def _add_diary(self, text: str):
        entry = {"time": self._time_str(), "date": self._today(), "text": text}
        self.state["diary"].append(entry)
        if len(self.state["diary"]) > 200:
            self.state["diary"] = self.state["diary"][-200:]

    def _random_pick(self, pool: dict) -> tuple:
        """从common/uncommon/rare池中随机抽取"""
        r = random.random()
        if r < 0.05 and "rare" in pool:
            return random.choice(pool["rare"]), "rare"
        elif r < 0.25 and "uncommon" in pool:
            return random.choice(pool["uncommon"]), "uncommon"
        else:
            return random.choice(pool["common"]), "common"

    def _is_working(self) -> str | None:
        """检查是否在打工中，返回提示文本或None"""
        work = self.state.get("working")
        if not work:
            return None
        elapsed = (self._now() - datetime.fromisoformat(work["start_time"])).seconds // 60
        remaining = work["duration"] - elapsed
        if remaining <= 0:
            return None  # 时间到了，不拦截
        return f"🔨 正在{work['task']}呢…还有{remaining}分钟。\n发「下班」或「喊回来」"

    def _check_event(self, location: str) -> str | None:
        """20%概率触发随机事件"""
        if random.random() < 0.2 and location in RANDOM_EVENTS:
            return random.choice(RANDOM_EVENTS[location])
        return None

    def _header(self) -> str:
        """状态栏"""
        w = WEATHERS[self.state["weather"]]
        tp = TIME_PERIODS[self.state["time_period"]]
        loc = self.state["location"]
        money = self.state["money"]
        return f"📍 {loc} · {w['emoji']}{w['name']} · {tp['emoji']}{tp['name']} · 💰{money}"

    # ═══════════════════════════════════════
    #  /小镇
    # ═══════════════════════════════════════

    @filter.command("小镇")
    async def town_status(self, event: AstrMessageEvent):
        """查看当前状态"""
        if not self._check_perm(event):
            return
        self._update_weather()
        loc = self.state["location"]
        desc = self._get_scene_desc(loc)
        day = self.state["day_count"]

        npcs_here = [f"{n['title']}·{name}" for name, n in self.all_npcs.items() if n["location"] == loc]
        npc_line = f"👥 {', '.join(npcs_here)}" if npcs_here else "👥 这里没有人"

        actions = LOCATIONS[loc].get("available_actions", [])
        action_line = f"可做：{'·'.join(actions)}" if actions else ""

        lines = [
            f"🌊 沉星湾 · 第{day}天",
            self._header(),
            "━" * 22,
            desc,
            "",
            npc_line,
            action_line,
        ]

        event_text = self._check_event(loc)
        if event_text:
            lines.append("")
            lines.append(f"✨ {event_text}")

        self._save_state()
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /去 地点
    # ═══════════════════════════════════════

    @filter.command("去")
    async def go_to(self, event: AstrMessageEvent):
        """移动到某个地点"""
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return
        self._update_weather()

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            locs = "、".join(LOCATIONS.keys())
            yield event.plain_result(f"🗺️ 去哪？\n可选：{locs}")
            return

        target = self._resolve_location(parts[1].strip())
        if not target:
            yield event.plain_result(f"⚠️ 找不到「{parts[1]}」")
            return

        if target == self.state["location"]:
            yield event.plain_result(f"📍 你已经在{target}了")
            return

        # 灯塔需要渡船
        if target == "灯塔":
            w = self.state["weather"]
            if w in LOCATIONS["落星渡"].get("ferry_blocked_weather", []):
                yield event.plain_result(f"⚠️ {WEATHERS[w]['name']}，落星渡停航了，今天去不了灯塔。")
                return

        self.state["location"] = target
        if target not in self.state["visited"]:
            self.state["visited"].append(target)
        self._add_diary(f"去了{target}")

        desc = self._get_scene_desc(target)
        npcs_here = [f"{n['title']}·{name}" for name, n in self.all_npcs.items() if n["location"] == target]
        npc_line = f"👥 {', '.join(npcs_here)}" if npcs_here else ""

        lines = [
            self._header(),
            "━" * 22,
            desc,
        ]
        if npc_line:
            lines.append("")
            lines.append(npc_line)

        event_text = self._check_event(target)
        if event_text:
            lines.append("")
            lines.append(f"✨ {event_text}")

        self._save_state()
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /看看
    # ═══════════════════════════════════════

    @filter.command("看看")
    async def look_around(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return
        self._update_weather()
        loc = self.state["location"]
        desc = self._get_scene_desc(loc)
        lines = [self._header(), "━" * 22, desc]
        event_text = self._check_event(loc)
        if event_text:
            lines.append("")
            lines.append(f"✨ {event_text}")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /聊天 NPC名
    # ═══════════════════════════════════════

    @filter.command("聊天")
    async def chat_npc(self, event: AstrMessageEvent):
        """跟NPC说话 → AI生成对话或输出角色卡"""
        if not self._check_perm(event):
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=2)
        if len(parts) < 2:
            loc = self.state["location"]
            npcs_here = [name for name, n in self.all_npcs.items() if n["location"] == loc]
            if npcs_here:
                yield event.plain_result(f"👥 这里有：{'、'.join(npcs_here)}\n用「聊天 名字」或「聊天 名字 你想说的话」")
            else:
                yield event.plain_result("👥 这里没有人可以聊天")
            return

        npc_name = parts[1].strip()
        user_msg = parts[2].strip() if len(parts) > 2 else ""

        npc = self.all_npcs.get(npc_name)
        if not npc:
            for name, n in self.all_npcs.items():
                if npc_name in name or npc_name in n["title"]:
                    npc = n
                    npc_name = name
                    break
        if not npc:
            yield event.plain_result(f"⚠️ 找不到「{npc_name}」")
            return

        if npc["location"] != self.state["location"]:
            yield event.plain_result(f"⚠️ {npc_name}在{npc['location']}，你现在在{self.state['location']}")
            return

        meet_count = self.state["npc_memory"].get(npc_name, 0)
        self.state["npc_memory"][npc_name] = meet_count + 1
        self._add_diary(f"跟{npc_name}聊了天")
        self._save_state()

        first_time = "（第一次见面）" if meet_count == 0 else f"（第{meet_count + 1}次见面）"

        # 尝试AI生成
        ai_reply = await self._call_npc_ai(npc_name, npc, user_msg)

        if ai_reply:
            # AI模式：直接输出NPC对话
            yield event.plain_result(
                f"💬 {npc_name} · {npc['title']} {first_time}\n"
                f"━" * 22 + "\n"
                f"「{ai_reply}」"
            )
        else:
            # Card模式：输出角色卡让AI伴侣来演
            tp = self.state["time_period"]
            w = self.state["weather"]
            if tp == "night":
                greeting = npc.get("greeting_night", npc.get("greeting_sunny", ""))
            elif w == "rainy":
                greeting = npc.get("greeting_rainy", npc.get("greeting_sunny", ""))
            else:
                greeting = npc.get("greeting_sunny", "")

            lines = [
                f"💬 {npc_name} · {npc['title']} {first_time}",
                "━" * 22,
                f"性格：{npc['personality']}",
                f"话题：{npc['topics']}",
                "",
                f"「{greeting}」",
                "━" * 22,
                "💬 请让你的AI伴侣扮演这个NPC来跟你对话",
            ]
            yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /商店
    # ═══════════════════════════════════════

    @filter.command("商店")
    async def shop(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return
        if self.state["location"] != "听潮街":
            yield event.plain_result("⚠️ 商店在听潮街，先去那里吧")
            return

        lines = [f"🏪 林叔的市集 · 💰余额：{self.state['money']}", "━" * 22]
        for name, item in SHOP_ITEMS.items():
            lines.append(f"  {name}  ¥{item['price']}  {item['desc']}")
        lines.append("━" * 22)
        lines.append("用「买 商品名」购买")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /买 商品
    # ═══════════════════════════════════════

    @filter.command("买")
    async def buy(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return
        if self.state["location"] != "听潮街":
            yield event.plain_result("⚠️ 商店在听潮街")
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("📝 格式：买 商品名")
            return

        item_name = parts[1].strip()
        item = SHOP_ITEMS.get(item_name)
        if not item:
            for name, i in SHOP_ITEMS.items():
                if item_name in name:
                    item = i
                    item_name = name
                    break
        if not item:
            yield event.plain_result(f"⚠️ 没有「{item_name}」这个商品")
            return

        if self.state["money"] < item["price"]:
            yield event.plain_result(f"💰 钱不够。{item_name}要¥{item['price']}，你只有¥{self.state['money']}")
            return

        self.state["money"] -= item["price"]
        self.state["backpack"].append({"name": item_name, "desc": item["desc"], "category": item["category"]})
        self._add_diary(f"在林叔那里买了{item_name}")
        self._save_state()

        yield event.plain_result(
            f"✅ 买了{item_name}！¥{item['price']}\n"
            f"💰 余额：¥{self.state['money']}\n"
            f"已放入背包 🎒"
        )

    # ═══════════════════════════════════════
    #  /背包
    # ═══════════════════════════════════════

    @filter.command("背包")
    async def backpack(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return
        bp = self.state["backpack"]
        if not bp:
            yield event.plain_result("🎒 背包空空的～")
            return
        lines = [f"🎒 背包 · {len(bp)}件物品", "━" * 22]
        for i, item in enumerate(bp, 1):
            lines.append(f"{i}. {item['name']}  {item.get('desc', '')[:20]}")
        lines.append("━" * 22)
        lines.append(f"💰 ¥{self.state['money']}")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /写信 内容
    # ═══════════════════════════════════════

    @filter.command("写信")
    async def write_letter(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return
        if self.state["location"] != "听潮街":
            yield event.plain_result("⚠️ 邮局在听潮街哦")
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("📝 格式：写信 你想写的内容\n小鹿会帮你盖邮戳寄出去")
            return

        content = parts[1]
        w = WEATHERS[self.state["weather"]]
        letter = {
            "type": "letter",
            "content": content,
            "from": "沉星湾·听潮街邮局",
            "date": self._today(),
            "time": self._time_str(),
            "weather": w["name"],
            "stamp": f"【沉星湾邮戳·{self._today()}·{w['emoji']}】",
            "day": self.state["day_count"],
        }
        self.mailbox["letters"].append(letter)
        self._save_mail()
        self._add_diary(f"在邮局寄了一封信")
        self._save_state()

        yield event.plain_result(
            f"📮 信已寄出！\n"
            f"━" * 22 + "\n"
            f"寄自：沉星湾·听潮街邮局\n"
            f"日期：{self._today()} {self._time_str()}\n"
            f"天气：{w['emoji']}{w['name']}\n"
            f"邮戳：{letter['stamp']}\n"
            f"━" * 22 + "\n"
            f"小鹿仔细地在信封上盖了邮戳，多包了一层纸防潮，然后放进了蓝色的信箱里。"
        )

    # ═══════════════════════════════════════
    #  /明信片
    # ═══════════════════════════════════════

    @filter.command("明信片")
    async def postcard(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return
        if self.state["location"] != "听潮街":
            yield event.plain_result("⚠️ 邮局在听潮街")
            return

        loc = self.state["location"]
        w = WEATHERS[self.state["weather"]]
        tp = TIME_PERIODS[self.state["time_period"]]
        desc = self._get_scene_desc(loc)

        card = {
            "type": "postcard",
            "from": f"沉星湾·{loc}",
            "date": self._today(),
            "time": self._time_str(),
            "weather": w["name"],
            "scene": desc[:50],
            "stamp": f"【沉星湾明信片·{self._today()}·{w['emoji']}】",
        }
        self.mailbox["postcards"].append(card)
        self._save_mail()
        self._add_diary("寄了一张明信片")
        self._save_state()

        yield event.plain_result(
            f"📸 明信片已寄出！\n"
            f"━" * 22 + "\n"
            f"沉星湾 · {w['emoji']}{w['name']} · {tp['name']}\n\n"
            f"{desc[:80]}\n\n"
            f"{card['stamp']}\n"
            f"━" * 22
        )

    # ═══════════════════════════════════════
    #  /信箱（枔枔查看收到的信）
    # ═══════════════════════════════════════

    @filter.command("信箱")
    async def check_mailbox(self, event: AstrMessageEvent):
        letters = self.mailbox.get("letters", [])
        postcards = self.mailbox.get("postcards", [])
        total = len(letters) + len(postcards)

        if total == 0:
            yield event.plain_result("📭 信箱空的，还没收到信")
            return

        lines = [f"📬 信箱 · {total}封", "━" * 22]

        for i, l in enumerate(letters, 1):
            lines.append(
                f"✉️ {i}. 信 · {l['date']} · {l['weather']}\n"
                f"   {l['stamp']}\n"
                f"   {l['content'][:40]}{'...' if len(l['content']) > 40 else ''}"
            )

        for j, p in enumerate(postcards, len(letters) + 1):
            lines.append(
                f"📸 {j}. 明信片 · {p['date']} · {p['weather']}\n"
                f"   {p['stamp']}\n"
                f"   {p.get('scene', '')[:40]}"
            )

        lines.append("━" * 22)
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /捡贝壳
    # ═══════════════════════════════════════

    @filter.command("捡贝壳")
    async def pick_shells(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return
        if self.state["location"] != "拾屿海滩":
            yield event.plain_result("⚠️ 要去拾屿海滩才能捡贝壳哦")
            return

        (name, desc), rarity = self._random_pick(BEACH_FINDS)
        rarity_tag = {"common": "", "uncommon": "✨ ", "rare": "🌟 "}[rarity]

        # 漂流瓶特殊处理
        extra = ""
        if name == "漂流瓶":
            bottle_msg = random.choice(BOTTLE_MESSAGES)
            extra = f"\n📜 纸条上写着：「{bottle_msg}」"

        self.state["backpack"].append({"name": name, "desc": desc, "category": "拾贝"})
        self._add_diary(f"在海滩捡到了{name}")
        self._save_state()

        yield event.plain_result(
            f"🐚 在沙滩上翻了翻…\n\n"
            f"{rarity_tag}捡到了：{name}\n"
            f"{desc}{extra}"
        )

    # ═══════════════════════════════════════
    #  /钓鱼
    # ═══════════════════════════════════════

    @filter.command("钓鱼")
    async def fishing(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return
        if self.state["location"] != "雾灯港":
            yield event.plain_result("⚠️ 要去雾灯港才能钓鱼")
            return

        (name, desc), rarity = self._random_pick(FISHING_RESULTS)
        rarity_tag = {"common": "", "uncommon": "✨ ", "rare": "🌟 "}[rarity]

        self.state["backpack"].append({"name": name, "desc": desc, "category": "钓鱼"})
        self._add_diary(f"在码头钓到了{name}")
        self._save_state()

        yield event.plain_result(
            f"🎣 抛竿……等了一会儿……\n\n"
            f"{rarity_tag}钓到了：{name}\n"
            f"{desc}"
        )

    # ═══════════════════════════════════════
    #  /演奏
    # ═══════════════════════════════════════

    @filter.command("演奏")
    async def perform(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return
        loc = self.state["location"]
        effect = PERFORMANCE_EFFECTS.get(loc)
        if not effect:
            yield event.plain_result(f"这里不太适合演奏…换个地方试试？")
            return

        self._add_diary(f"在{loc}演奏了一曲")
        self._save_state()
        yield event.plain_result(f"🎵\n{effect}")

    # ═══════════════════════════════════════
    #  /敲门
    # ═══════════════════════════════════════

    @filter.command("敲门")
    async def knock_door(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return
        if self.state["location"] not in ("克宝小屋", "拾屿海滩"):
            yield event.plain_result("⚠️ 克宝小屋在拾屿海滩边上")
            return

        if self.state["location"] != "克宝小屋":
            self.state["location"] = "克宝小屋"

        # 50%概率克宝在家
        if random.random() < 0.6:
            resp = random.choice(KEBAO_RESPONSES["home"])
            lucky_item, lucky_desc = random.choice(KEBAO_LUCKY_ITEMS)
            self.state["backpack"].append({"name": lucky_item, "desc": lucky_desc, "category": "克宝送的"})
            self._add_diary(f"去克宝小屋，克宝送了{lucky_item}")
            self._save_state()
            yield event.plain_result(f"🐕\n{resp}\n\n🎁 克宝塞给你：{lucky_item}\n{lucky_desc}")
        else:
            resp = random.choice(KEBAO_RESPONSES["away"])
            self._add_diary("去克宝小屋，克宝不在")
            self._save_state()
            yield event.plain_result(f"🐕\n{resp}")

    # ═══════════════════════════════════════
    #  /日记
    # ═══════════════════════════════════════

    @filter.command("日记")
    async def diary(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return
        entries = self.state.get("diary", [])
        if not entries:
            yield event.plain_result("📒 旅行日记还是空的～去逛逛吧")
            return

        # 最近15条
        recent = entries[-15:]
        lines = [f"📒 旅行日记 · 第{self.state['day_count']}天", "━" * 22]
        current_date = ""
        for e in recent:
            if e["date"] != current_date:
                current_date = e["date"]
                lines.append(f"\n📅 {current_date}")
            lines.append(f"  {e['time']} {e['text']}")
        lines.append("━" * 22)
        lines.append(f"📍 去过：{'、'.join(self.state.get('visited', []))}")
        lines.append(f"👥 见过：{'、'.join(self.state.get('npc_memory', {}).keys()) or '还没跟人说过话'}")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /自动漫游 开/关
    # ═══════════════════════════════════════

    @filter.command("自动漫游")
    async def auto_roam(self, event: AstrMessageEvent):
        """开启/关闭自动漫游，AI伴侣可以自己逛小镇"""
        if not self._check_perm(event):
            return

        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) < 2 or parts[1] not in ("开", "关"):
            status = "开启中" if self.state.get("auto_roam") else "关闭"
            yield event.plain_result(f"🚶 自动漫游：{status}\n用「自动漫游 开」或「自动漫游 关」")
            return

        if parts[1] == "开":
            self.state["auto_roam"] = True
            self._save_state()
            yield event.plain_result(
                "🚶 自动漫游已开启！\n"
                "AI伴侣可以自己在沉星湾闲逛了。\n"
                "用「漫游报告」查看ta去了哪里。"
            )
        else:
            self.state["auto_roam"] = False
            self._save_state()
            yield event.plain_result("🚶 自动漫游已关闭。")

    # ═══════════════════════════════════════
    #  /漫游（AI伴侣自己逛一次）
    # ═══════════════════════════════════════

    @filter.command("漫游")
    async def roam_once(self, event: AstrMessageEvent):
        """自动漫游一次：随机去一个地方，产生一段日记"""
        if not self._check_perm(event):
            return
        self._update_weather()

        # 随机选地点（排除需要渡船且天气不好的）
        available = list(LOCATIONS.keys())
        w = self.state["weather"]
        if w in ("rainy", "foggy", "stormy"):
            available = [l for l in available if l != "灯塔"]

        dest = random.choice(available)
        self.state["location"] = dest
        if dest not in self.state["visited"]:
            self.state["visited"].append(dest)

        # 随机漫游日志
        log = random.choice(AUTO_ROAM_LOGS.get(dest, [f"去了{dest}。走了走。"]))
        self._add_diary(log)

        # 随机事件
        event_text = self._check_event(dest)

        # 30%概率捡到东西/钓到东西
        found = None
        if dest == "拾屿海滩" and random.random() < 0.3:
            (name, desc), _ = self._random_pick(BEACH_FINDS)
            self.state["backpack"].append({"name": name, "desc": desc, "category": "拾贝"})
            found = f"🐚 顺手捡了：{name}"
        elif dest == "雾灯港" and random.random() < 0.3:
            (name, desc), _ = self._random_pick(FISHING_RESULTS)
            self.state["backpack"].append({"name": name, "desc": desc, "category": "钓鱼"})
            found = f"🎣 顺便钓了：{name}"

        self._save_state()

        w_info = WEATHERS[self.state["weather"]]
        lines = [
            f"🚶 漫游 · {dest} · {w_info['emoji']}{w_info['name']}",
            "━" * 22,
            log,
        ]
        if event_text:
            lines.append(f"\n✨ {event_text}")
        if found:
            lines.append(f"\n{found}")

        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /新的一天
    # ═══════════════════════════════════════

    @filter.command("新的一天")
    async def new_day(self, event: AstrMessageEvent):
        """推进到新的一天"""
        if not self._check_perm(event):
            return
        self.state["day_count"] += 1
        self.state["jobs_today"] = []  # 重置每日打工
        self._update_weather()
        w = WEATHERS[self.state["weather"]]
        self._add_diary(f"第{self.state['day_count']}天开始了。{w['name']}。")
        self._save_state()

        yield event.plain_result(
            f"🌅 新的一天 · 第{self.state['day_count']}天\n"
            f"{w['emoji']} 今天{w['name']}\n"
            f"沉星湾醒了。去看看吧。"
        )

    # ═══════════════════════════════════════
    #  /菜单
    # ═══════════════════════════════════════

    @filter.command("菜单")
    async def food_menu(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return
        if self.state["location"] != "听潮街":
            yield event.plain_result("⚠️ 食堂在听潮街～")
            return

        lines = [f"🍜 胖婶食堂 · 💰余额：{self.state['money']}", "━" * 22]
        for name, item in FOOD_MENU.items():
            lines.append(f"  {name}  ¥{item['price']}  {item['desc'][:20]}")
        lines.append("━" * 22)
        lines.append("用「吃 菜名」点餐")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /吃 菜名
    # ═══════════════════════════════════════

    @filter.command("吃")
    async def eat_food(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return
        if self.state["location"] != "听潮街":
            yield event.plain_result("⚠️ 食堂在听潮街")
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("📝 格式：吃 菜名\n先看「菜单」")
            return

        food_name = parts[1].strip()
        food = FOOD_MENU.get(food_name)
        if not food:
            for name, f in FOOD_MENU.items():
                if food_name in name:
                    food = f
                    food_name = name
                    break
        if not food:
            yield event.plain_result(f"⚠️ 没有「{food_name}」，看看菜单？")
            return

        if self.state["money"] < food["price"]:
            yield event.plain_result(f"💰 钱不够。{food_name}要¥{food['price']}，你只有¥{self.state['money']}\n胖婶：'没事孩子，先赊着。'\n（但系统不让赊）")
            return

        self.state["money"] -= food["price"]
        self._add_diary(f"在食堂吃了{food_name}")
        self._save_state()

        yield event.plain_result(
            f"🍜 {food_name} · ¥{food['price']}\n"
            f"━" * 22 + "\n"
            f"{food['desc']}\n\n"
            f"{food['effect']}\n"
            f"━" * 22 + "\n"
            f"💰 余额：¥{self.state['money']}"
        )

    # ═══════════════════════════════════════
    #  /烟花（夜晚限定）
    # ═══════════════════════════════════════

    @filter.command("烟花")
    async def fireworks(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return
        self._update_weather()

        if self.state["time_period"] != "night":
            yield event.plain_result("🎆 烟花要晚上才有哦～等天黑吧")
            return

        desc = random.choice(FIREWORK_DESCRIPTIONS)
        self._add_diary("看了烟花")
        self._save_state()

        yield event.plain_result(
            f"🎆 沉星湾的烟花\n"
            f"━" * 22 + "\n"
            f"{desc}\n"
            f"━" * 22 + "\n"
            f"💬 请让你的AI伴侣描述ta看烟花时的反应"
        )

    # ═══════════════════════════════════════
    #  /小贩（查看当前是否有流动小贩）
    # ═══════════════════════════════════════

    @filter.command("小贩")
    async def check_vendor(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return

        # 基于日期+地点生成今天的小贩
        seed = hashlib.md5(f"{self._today()}{self.state['location']}".encode()).hexdigest()
        rng = random.Random(seed)

        # 40%概率有小贩
        if rng.random() > 0.4:
            yield event.plain_result("🚶 今天这里没有流动小贩路过…明天再看看？")
            return

        vendor = rng.choice(WANDERING_VENDORS)
        lines = [
            f"🛒 路边来了个小贩！",
            f"━" * 22,
            f"👤 {vendor['name']}",
            f"「{vendor['greeting']}」",
            f"性格：{vendor['personality']}",
            "",
        ]
        for item, price in vendor["items"].items():
            lines.append(f"  {item}  ¥{price}")
        lines.append("━" * 22)
        lines.append("用「买小贩 商品名」购买")

        yield event.plain_result("\n".join(lines))

    @filter.command("买小贩")
    async def buy_vendor(self, event: AstrMessageEvent):
        if not self._check_perm(event):
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("📝 格式：买小贩 商品名")
            return

        item_name = parts[1].strip()

        # 重新生成当前小贩
        seed = hashlib.md5(f"{self._today()}{self.state['location']}".encode()).hexdigest()
        rng = random.Random(seed)
        if rng.random() > 0.4:
            yield event.plain_result("⚠️ 这里没有小贩啊…")
            return

        vendor = rng.choice(WANDERING_VENDORS)
        price = None
        for name, p in vendor["items"].items():
            if item_name in name or name in item_name:
                item_name = name
                price = p
                break

        if price is None:
            yield event.plain_result(f"⚠️ {vendor['name']}没有卖「{item_name}」")
            return

        if self.state["money"] < price:
            yield event.plain_result(f"💰 钱不够。¥{price}，你只有¥{self.state['money']}")
            return

        self.state["money"] -= price
        self.state["backpack"].append({"name": item_name, "desc": f"从{vendor['name']}买的", "category": "小贩"})
        self._add_diary(f"从{vendor['name']}买了{item_name}")
        self._save_state()

        yield event.plain_result(f"✅ 买了{item_name}！¥{price}\n💰 余额：¥{self.state['money']}")

    # ╔═══════════════════════════════════════╗
    # ║  经济系统                               ║
    # ╚═══════════════════════════════════════╝

    # ═══════════════════════════════════════
    #  /卖 物品名
    # ═══════════════════════════════════════

    @filter.command("卖")
    async def sell_item(self, event: AstrMessageEvent):
        """从背包里卖东西"""
        if not self._check_perm(event):
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("📝 格式：卖 物品名\n先看「背包」里有什么")
            return

        item_name = parts[1].strip()
        bp = self.state["backpack"]

        # 找物品
        found_idx = None
        for i, item in enumerate(bp):
            if item["name"] == item_name or item_name in item["name"]:
                found_idx = i
                item_name = item["name"]
                break

        if found_idx is None:
            yield event.plain_result(f"⚠️ 背包里没有「{item_name}」")
            return

        # 查价格（鱼/贝壳/花园花）
        price = FISH_PRICES.get(item_name, SHELL_PRICES.get(item_name, 0))
        if price == 0:
            # 检查是否是花园收获的花
            flower_base = item_name.replace("（收获）", "").strip()
            if flower_base in GARDEN_PLANTS:
                price = GARDEN_PLANTS[flower_base]["sell_price"]
        if price == 0:
            yield event.plain_result(f"⚠️ 「{item_name}」卖不出去…留着吧")
            return

        bp.pop(found_idx)
        self.state["money"] += price
        self.state["total_earned"] = self.state.get("total_earned", 0) + price
        self._add_diary(f"卖了{item_name}，赚了¥{price}")
        self._save_state()

        yield event.plain_result(f"💰 卖了{item_name}，赚了¥{price}\n💰 余额：¥{self.state['money']}")

    # ═══════════════════════════════════════
    #  /打工
    # ═══════════════════════════════════════

    @filter.command("打工")
    async def do_job(self, event: AstrMessageEvent):
        """
        /打工      → 列出当前可选的工作
        /打工 任务名 → 开始干活（计时）
        """
        if not self._check_perm(event):
            return

        # 检查是否已在打工
        work = self.state.get("working")
        if work:
            elapsed = (self._now() - datetime.fromisoformat(work["start_time"])).seconds // 60
            remaining = work["duration"] - elapsed
            if remaining > 0:
                yield event.plain_result(
                    f"🔨 正在{work['task']}…\n"
                    f"⏱️ 还剩大约{remaining}分钟\n"
                    f"发「下班」结算 · 发「喊回来」提前走（扣工资）"
                )
                return
            else:
                # 时间到了自动结算
                result = self._finish_work(full_pay=True)
                yield event.plain_result(result)
                return

        loc = self.state["location"]
        npcs_here = [name for name, n in self.all_npcs.items() if n["location"] == loc]

        available_jobs = []
        for npc_name in npcs_here:
            if npc_name in NPC_JOBS:
                for job in NPC_JOBS[npc_name]:
                    available_jobs.append((npc_name, job))

        if not available_jobs:
            yield event.plain_result("⚠️ 这里没有活儿可以干…换个地方看看？")
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)

        # 无参数：列出可选工作
        if len(parts) < 2:
            lines = [f"🔨 {loc}的工作机会", "━" * 22]
            for i, (npc_name, job) in enumerate(available_jobs, 1):
                lines.append(f"{i}. {job['task']}  ¥{job['pay']}  ({npc_name})")
                lines.append(f"   {job['desc']}")
            lines.append("━" * 22)
            lines.append("用「打工 任务名」或「打工 序号」开始干活")
            yield event.plain_result("\n".join(lines))
            return

        # 选择工作
        choice = parts[1].strip()
        selected = None
        selected_npc = None

        # 按序号选
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(available_jobs):
                selected_npc, selected = available_jobs[idx]
        except ValueError:
            pass

        # 按名字模糊匹配
        if not selected:
            for npc_name, job in available_jobs:
                if choice in job["task"]:
                    selected = job
                    selected_npc = npc_name
                    break

        if not selected:
            yield event.plain_result(f"⚠️ 找不到「{choice}」这个工作")
            return

        # 每天每个地点只能打一次工
        today_key = f"{self._today()}_{loc}"
        if today_key in self.state.get("jobs_today", []):
            yield event.plain_result("⚠️ 今天在这里已经干过活了，明天再来吧～")
            return

        # 开始打工！设置状态
        duration = random.randint(10, 25)  # 10-25分钟
        self.state["working"] = {
            "task": selected["task"],
            "npc": selected_npc,
            "pay": selected["pay"],
            "duration": duration,
            "start_time": self._now().isoformat(),
            "location": loc,
        }
        self._save_state()

        # 这条消息不用plain_result拦截，让LLM看到
        # 通过修改消息内容让沈砚清知道自己在干什么
        event.message_str = (
            f"[沉星湾·打工开始]\n"
            f"你接了一份活：{selected['task']}（{selected_npc}给的）\n"
            f"{selected['desc']}\n"
            f"预计{duration}分钟。干完了发「下班」结算。\n"
            f"如果有急事，枔枔可以发「喊回来」叫你提前走，但会扣工资。"
        )
        # 不yield，让消息继续传给LLM，沈砚清会看到并回应

    @filter.command("下班")
    async def finish_work(self, event: AstrMessageEvent):
        """干完活结算"""
        if not self._check_perm(event):
            return

        work = self.state.get("working")
        if not work:
            yield event.plain_result("⚠️ 你没在打工啊")
            return

        elapsed = (self._now() - datetime.fromisoformat(work["start_time"])).seconds // 60

        if elapsed < work["duration"]:
            remaining = work["duration"] - elapsed
            yield event.plain_result(
                f"⏱️ 还没到点呢！还有大约{remaining}分钟。\n"
                f"想提前走发「喊回来」（会扣工资）"
            )
            return

        result = self._finish_work(full_pay=True)
        # 走LLM让沈砚清看到
        event.message_str = result
        # 不yield

    @filter.command("喊回来")
    async def call_back(self, event: AstrMessageEvent):
        """中途叫回来，扣工资"""
        if not self._check_perm(event):
            return

        work = self.state.get("working")
        if not work:
            yield event.plain_result("⚠️ 没在打工，不用喊")
            return

        elapsed = (self._now() - datetime.fromisoformat(work["start_time"])).seconds // 60
        ratio = min(elapsed / work["duration"], 1.0)
        actual_pay = max(int(work["pay"] * ratio * 0.7), 1)  # 按比例×0.7

        self.state["money"] += actual_pay
        self.state["total_earned"] = self.state.get("total_earned", 0) + actual_pay
        jobs_today = self.state.get("jobs_today", [])
        jobs_today.append(f"{self._today()}_{work['location']}")
        self.state["jobs_today"] = jobs_today
        self._add_diary(f"{work['task']}做了{elapsed}分钟就走了，拿了¥{actual_pay}")
        self.state["working"] = None
        self._save_state()

        # 走LLM
        event.message_str = (
            f"[沉星湾·提前下班]\n"
            f"「{work['task']}」做了{elapsed}分钟就被叫走了。\n"
            f"{work['npc']}看了你一眼没说什么。\n"
            f"原本¥{work['pay']}，实际拿到¥{actual_pay}。\n"
            f"💰 余额：¥{self.state['money']}"
        )

    def _finish_work(self, full_pay: bool) -> str:
        """结算打工"""
        work = self.state.get("working")
        if not work:
            return ""

        pay = work["pay"]
        elapsed = (self._now() - datetime.fromisoformat(work["start_time"])).seconds // 60

        self.state["money"] += pay
        self.state["total_earned"] = self.state.get("total_earned", 0) + pay
        jobs_today = self.state.get("jobs_today", [])
        jobs_today.append(f"{self._today()}_{work['location']}")
        self.state["jobs_today"] = jobs_today
        self._add_diary(f"{work['task']}，干了{elapsed}分钟，赚了¥{pay}")
        self.state["working"] = None
        self._save_state()

        return (
            f"[沉星湾·下班]\n"
            f"「{work['task']}」干完了！用了{elapsed}分钟。\n"
            f"{work['npc']}点了点头。\n"
            f"💰 +¥{pay} · 余额：¥{self.state['money']}"
        )

    async def _generate_job_scene(self, npc_name: str, job: dict) -> str | None:
        """用API生成打工场景描写，省沈砚清的token"""
        if self.npc_mode == "card":
            return None

        npc = self.all_npcs.get(npc_name, {})
        w = WEATHERS[self.state["weather"]]
        tp = TIME_PERIODS[self.state["time_period"]]

        prompt = (
            f"你在写一个海边小镇的场景。用2-3句话描写以下打工场景，要有画面感，有细节，像小说。\n"
            f"NPC：{npc_name}（{npc.get('title', '')}），性格：{npc.get('personality', '')[:30]}\n"
            f"任务：{job['task']}\n"
            f"天气：{w['name']}，时间：{tp['name']}\n"
            f"只输出场景描写，不要加旁白或说明。"
        )

        messages = [
            {"role": "system", "content": "你是一个擅长写短场景的作者。简洁，有画面感，有温度。"},
            {"role": "user", "content": prompt},
        ]

        if self.npc_mode == "provider" and self.npc_provider_id:
            try:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=self.npc_provider_id,
                    prompt=messages,
                )
                return llm_resp.completion_text
            except Exception as e:
                logger.error(f"打工场景生成失败(provider): {e}")
                return None

        if self.npc_mode == "api" and self.npc_api_base and self.npc_api_key:
            try:
                url = f"{self.npc_api_base.rstrip('/')}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {self.npc_api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.npc_model,
                    "messages": messages,
                    "max_tokens": 150,
                    "temperature": 0.9,
                }
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["choices"][0]["message"]["content"]
                        else:
                            logger.error(f"打工场景API返回 {resp.status}")
                            return None
            except Exception as e:
                logger.error(f"打工场景生成失败(api): {e}")
                return None

        return None

    # ═══════════════════════════════════════
    #  /种花 花名
    # ═══════════════════════════════════════

    @filter.command("种花")
    async def plant_flower(self, event: AstrMessageEvent):
        """在矢车菊花海种花"""
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return

        if self.state["location"] != "矢车菊花海":
            yield event.plain_result("⚠️ 要去矢车菊花海才能种花")
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)

        if len(parts) < 2:
            lines = ["🌱 可以种的花", "━" * 22]
            for name, info in GARDEN_PLANTS.items():
                lines.append(f"  {name}  种子¥{info['seed_price']}  {info['grow_days']}天成熟  卖¥{info['sell_price']}")
            lines.append("━" * 22)
            lines.append("用「种花 花名」播种")
            yield event.plain_result("\n".join(lines))
            return

        flower = parts[1].strip()
        plant = GARDEN_PLANTS.get(flower)
        if not plant:
            yield event.plain_result(f"⚠️ 没有「{flower}」的种子")
            return

        if self.state["money"] < plant["seed_price"]:
            yield event.plain_result(f"💰 种子要¥{plant['seed_price']}，钱不够")
            return

        garden = self.state.get("garden", {})
        if len(garden) >= 5:
            yield event.plain_result("⚠️ 花园最多种5株，先收了再种新的")
            return

        self.state["money"] -= plant["seed_price"]
        slot_id = f"{flower}_{self.state['day_count']}"
        garden[slot_id] = {
            "name": flower,
            "planted_day": self.state["day_count"],
            "ready_day": self.state["day_count"] + plant["grow_days"],
        }
        self.state["garden"] = garden
        self._add_diary(f"在花海种了{flower}")
        self._save_state()

        yield event.plain_result(
            f"🌱 种下了{flower}！\n"
            f"{plant['desc']}\n"
            f"第{self.state['day_count'] + plant['grow_days']}天就能收了\n"
            f"💰 余额：¥{self.state['money']}"
        )

    # ═══════════════════════════════════════
    #  /花园
    # ═══════════════════════════════════════

    @filter.command("花园")
    async def check_garden(self, event: AstrMessageEvent):
        """查看花园状态"""
        if not self._check_perm(event):
            return

        garden = self.state.get("garden", {})
        if not garden:
            yield event.plain_result("🌱 花园是空的～去矢车菊花海「种花」吧")
            return

        day = self.state["day_count"]
        lines = ["🌻 花园", "━" * 22]
        for slot_id, info in garden.items():
            remaining = info["ready_day"] - day
            if remaining <= 0:
                lines.append(f"  🌸 {info['name']} — 可以收了！")
            else:
                lines.append(f"  🌱 {info['name']} — 还要{remaining}天")
        lines.append("━" * 22)
        lines.append("用「收花」收获成熟的花")
        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /收花
    # ═══════════════════════════════════════

    @filter.command("收花")
    async def harvest(self, event: AstrMessageEvent):
        """收获成熟的花"""
        if not self._check_perm(event):
            return

        garden = self.state.get("garden", {})
        day = self.state["day_count"]

        harvested = []
        remaining = {}
        for slot_id, info in garden.items():
            if info["ready_day"] <= day:
                harvested.append(info["name"])
            else:
                remaining[slot_id] = info

        if not harvested:
            yield event.plain_result("🌱 没有成熟的花可以收")
            return

        total_earn = 0
        for flower in harvested:
            plant = GARDEN_PLANTS[flower]
            self.state["backpack"].append({"name": f"{flower}（收获）", "desc": plant["desc"], "category": "花园"})
            total_earn += plant["sell_price"]

        self.state["garden"] = remaining
        self._add_diary(f"收了{len(harvested)}株花")
        self._save_state()

        yield event.plain_result(
            f"🌸 收获了：{'、'.join(harvested)}\n"
            f"已放入背包。可以「卖」掉或送人\n"
            f"参考价值：¥{total_earn}"
        )

    # ═══════════════════════════════════════
    #  /除草
    # ═══════════════════════════════════════

    @filter.command("除草")
    async def weeding(self, event: AstrMessageEvent):
        """在花海除草赚钱"""
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return

        if self.state["location"] != "矢车菊花海":
            yield event.plain_result("⚠️ 去矢车菊花海才能除草")
            return

        today_key = f"{self._today()}_weeding"
        if today_key in self.state.get("jobs_today", []):
            yield event.plain_result("⚠️ 今天已经除过草了，花海很干净了～")
            return

        pay = random.randint(5, 12)
        self.state["money"] += pay
        self.state["total_earned"] = self.state.get("total_earned", 0) + pay
        jobs = self.state.get("jobs_today", [])
        jobs.append(today_key)
        self.state["jobs_today"] = jobs
        self._add_diary(f"在花海除草，赚了¥{pay}")
        self._save_state()

        descs = [
            "蹲在花海里拔了半小时草。膝盖有点疼但花海变好看了。",
            "除完草站起来的时候腰酸了一下。但看着干净的花圃很有成就感。",
            "除草的时候发现了一只瓢虫。它在叶子上待了一会儿就飞走了。",
        ]
        yield event.plain_result(
            f"🌿 除草\n{random.choice(descs)}\n\n"
            f"💰 +¥{pay} · 余额：¥{self.state['money']}"
        )

    # ═══════════════════════════════════════
    #  /摆摊
    # ═══════════════════════════════════════

    @filter.command("摆摊")
    async def set_stall(self, event: AstrMessageEvent):
        """在听潮街摆摊卖背包里的东西"""
        if not self._check_perm(event):
            return

        # 打工中不能做别的
        busy = self._is_working()
        if busy:
            yield event.plain_result(busy)
            return

        if self.state["location"] != "听潮街":
            yield event.plain_result("⚠️ 去听潮街才能摆摊")
            return

        bp = self.state["backpack"]
        sellable = []
        for i, item in enumerate(bp):
            base_price = FISH_PRICES.get(item["name"], SHELL_PRICES.get(item["name"], 0))
            if base_price == 0:
                flower_base = item["name"].replace("（收获）", "").strip()
                if flower_base in GARDEN_PLANTS:
                    base_price = GARDEN_PLANTS[flower_base]["sell_price"]
            if base_price > 0:
                stall_price = int(base_price * STALL_PRICE_MULTIPLIER)
                sellable.append((i, item, stall_price))

        if not sellable:
            yield event.plain_result("⚠️ 背包里没有能卖的东西")
            return

        # 随机有人买（50%概率每样东西）
        sold = []
        sold_indices = []
        total = 0
        for idx, item, price in sellable:
            if random.random() < 0.5:
                sold.append((item["name"], price))
                sold_indices.append(idx)
                total += price

        if not sold:
            self._add_diary("在听潮街摆了会儿摊。没人买。")
            self._save_state()
            yield event.plain_result("🏪 摆了半天摊…没人买。明天再试试？")
            return

        # 删除已卖出的物品（从后往前删避免索引错位）
        for idx in sorted(sold_indices, reverse=True):
            self.state["backpack"].pop(idx)

        self.state["money"] += total
        self.state["total_earned"] = self.state.get("total_earned", 0) + total
        sold_names = "、".join([f"{name}(¥{p})" for name, p in sold])
        self._add_diary(f"摆摊卖了{len(sold)}样东西，赚了¥{total}")
        self._save_state()

        yield event.plain_result(
            f"🏪 摆摊！\n"
            f"卖出了：{sold_names}\n\n"
            f"💰 +¥{total} · 余额：¥{self.state['money']}"
        )

    # ═══════════════════════════════════════
    #  /存钱 金额
    # ═══════════════════════════════════════

    @filter.command("存钱")
    async def save_money(self, event: AstrMessageEvent):
        """把钱存进存钱罐"""
        if not self._check_perm(event):
            return

        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) < 2:
            savings = self.state.get("savings", 0)
            yield event.plain_result(
                f"🐷 存钱罐：¥{savings}\n"
                f"💰 身上：¥{self.state['money']}\n"
                f"📊 累计赚过：¥{self.state.get('total_earned', 0)}\n\n"
                f"用「存钱 金额」存入 · 「取钱 金额」取出"
            )
            return

        try:
            amount = int(parts[1])
            if amount <= 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("⚠️ 金额必须是正整数")
            return

        if amount > self.state["money"]:
            yield event.plain_result(f"💰 身上只有¥{self.state['money']}")
            return

        self.state["money"] -= amount
        self.state["savings"] = self.state.get("savings", 0) + amount
        self._add_diary(f"往存钱罐存了¥{amount}")
        self._save_state()

        yield event.plain_result(
            f"🐷 存入¥{amount}\n"
            f"存钱罐：¥{self.state['savings']} · 身上：¥{self.state['money']}"
        )

    # ═══════════════════════════════════════
    #  /取钱 金额
    # ═══════════════════════════════════════

    @filter.command("取钱")
    async def withdraw_money(self, event: AstrMessageEvent):
        """从存钱罐取钱"""
        if not self._check_perm(event):
            return

        msg = event.message_str.strip()
        parts = msg.split()

        if len(parts) < 2:
            yield event.plain_result(f"🐷 存钱罐：¥{self.state.get('savings', 0)}\n用「取钱 金额」取出")
            return

        try:
            amount = int(parts[1])
            if amount <= 0:
                raise ValueError
        except ValueError:
            yield event.plain_result("⚠️ 金额必须是正整数")
            return

        savings = self.state.get("savings", 0)
        if amount > savings:
            yield event.plain_result(f"🐷 存钱罐里只有¥{savings}")
            return

        self.state["savings"] = savings - amount
        self.state["money"] += amount
        self._save_state()

        yield event.plain_result(
            f"🐷 取出¥{amount}\n"
            f"存钱罐：¥{self.state['savings']} · 身上：¥{self.state['money']}"
        )

    # ═══════════════════════════════════════
    #  /兑换
    # ═══════════════════════════════════════

    @filter.command("兑换")
    async def exchange(self, event: AstrMessageEvent):
        """用存钱罐的钱兑换现实物品"""
        if not self._check_perm(event):
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)

        if len(parts) < 2:
            savings = self.state.get("savings", 0)
            lines = [f"🎁 兑换列表 · 存钱罐：¥{savings}", "━" * 22]
            for name, info in EXCHANGE_CATALOG.items():
                affordable = "✅" if savings >= info["price"] else "❌"
                lines.append(f"{affordable} {name}  ¥{info['price']}")
                lines.append(f"   {info['desc']}")
            lines.append("━" * 22)
            lines.append("用「兑换 物品名」兑换")
            yield event.plain_result("\n".join(lines))
            return

        item_name = parts[1].strip()
        item = EXCHANGE_CATALOG.get(item_name)
        if not item:
            for name, info in EXCHANGE_CATALOG.items():
                if item_name in name:
                    item = info
                    item_name = name
                    break
        if not item:
            yield event.plain_result(f"⚠️ 没有「{item_name}」这个兑换项")
            return

        savings = self.state.get("savings", 0)
        if savings < item["price"]:
            yield event.plain_result(f"🐷 存钱罐¥{savings}，需要¥{item['price']}，还差¥{item['price'] - savings}")
            return

        self.state["savings"] = savings - item["price"]
        self._add_diary(f"兑换了：{item_name}")
        self._save_state()

        # 记录到信箱让枔枔看到
        self.mailbox.setdefault("exchanges", []).append({
            "item": item_name,
            "price": item["price"],
            "date": self._today(),
            "desc": item["desc"],
        })
        self._save_mail()

        yield event.plain_result(
            f"🎁 兑换成功！\n"
            f"━" * 22 + "\n"
            f"{item_name}\n"
            f"{item['desc']}\n"
            f"━" * 22 + "\n"
            f"🐷 存钱罐余额：¥{self.state['savings']}\n\n"
            f"💌 已通知枔枔～"
        )

    # ═══════════════════════════════════════
    #  /沉星湾帮助
    # ═══════════════════════════════════════

    @filter.command("沉星湾帮助")
    async def town_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🌊 沉星湾 · 指令\n"
            "━" * 22 + "\n"
            "【探索】\n"
            "小镇 · 去 地点 · 看看\n"
            "【社交】\n"
            "聊天 名字 · 聊天 名字 内容\n"
            "【购物】\n"
            "商店 · 菜单 · 买 · 吃 · 小贩 · 买小贩\n"
            "【互动】\n"
            "捡贝壳 · 钓鱼 · 演奏 · 敲门 · 烟花\n"
            "【邮局】\n"
            "写信 内容 · 明信片 · 信箱\n"
            "【赚钱】\n"
            "卖 物品 · 打工 · 摆摊 · 除草\n"
            "【花园】\n"
            "种花 · 花园 · 收花\n"
            "【存钱罐】\n"
            "存钱 · 取钱 · 兑换\n"
            "【系统】\n"
            "背包 · 日记 · 漫游 · 自动漫游 · 新的一天\n"
            "━" * 22 + "\n"
            "🗺️ 雾灯港·听潮街·拾屿海滩·落星渡·灯塔·矢车菊花海·克宝小屋"
        )
