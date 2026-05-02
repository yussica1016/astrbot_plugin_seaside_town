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

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from .town_data import (
    LOCATIONS, LOCATION_ALIASES, NPCS, SHOP_ITEMS,
    BEACH_FINDS, FISHING_RESULTS, WEATHERS, WEATHER_WEIGHTS,
    TIME_PERIODS, RANDOM_EVENTS, PERFORMANCE_EFFECTS,
    KEBAO_RESPONSES, KEBAO_LUCKY_ITEMS, AUTO_ROAM_LOGS,
    BOTTLE_MESSAGES,
)

TARGET_QQ = ""
MSK = timezone(timedelta(hours=3))
INITIAL_MONEY = 100


@register("seaside_town", "叶枔枖 & 叶克宝",
          "沉星湾 v1.0 - 设计叶枔枖 & 沈砚清，编写叶克宝。", "1.0.0")
class SeasideTown(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        base = os.path.dirname(os.path.abspath(__file__))
        self.state_path = os.path.join(base, "town_state.json")
        self.mail_path = os.path.join(base, "mailbox.json")
        self.state = self._load(self.state_path, self._default_state())
        self.mailbox = self._load(self.mail_path, {"letters": [], "postcards": []})

    # ═══════════════════════════════════════
    #  数据管理
    # ═══════════════════════════════════════

    def _default_state(self) -> dict:
        return {
            "location": "听潮街",
            "weather": "sunny",
            "time_period": "morning",
            "money": INITIAL_MONEY,
            "backpack": [],
            "diary": [],
            "npc_memory": {},
            "visited": [],
            "auto_roam": False,
            "day_count": 1,
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

        npcs_here = [f"{n['title']}·{name}" for name, n in NPCS.items() if n["location"] == loc]
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
        npcs_here = [f"{n['title']}·{name}" for name, n in NPCS.items() if n["location"] == target]
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
        """跟NPC说话 → 输出NPC信息，让AI伴侣来演"""
        if not self._check_perm(event):
            return

        msg = event.message_str.strip()
        parts = msg.split(maxsplit=1)
        if len(parts) < 2:
            loc = self.state["location"]
            npcs_here = [name for name, n in NPCS.items() if n["location"] == loc]
            if npcs_here:
                yield event.plain_result(f"👥 这里有：{'、'.join(npcs_here)}\n用「聊天 名字」找ta说话")
            else:
                yield event.plain_result("👥 这里没有人可以聊天")
            return

        npc_name = parts[1].strip()
        npc = NPCS.get(npc_name)
        if not npc:
            for name, n in NPCS.items():
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

        tp = self.state["time_period"]
        w = self.state["weather"]
        if tp == "night":
            greeting = npc.get("greeting_night", npc.get("greeting_sunny", ""))
        elif w == "rainy":
            greeting = npc.get("greeting_rainy", npc.get("greeting_sunny", ""))
        else:
            greeting = npc.get("greeting_sunny", "")

        meet_count = self.state["npc_memory"].get(npc_name, 0)
        self.state["npc_memory"][npc_name] = meet_count + 1
        self._add_diary(f"跟{npc_name}聊了天")
        self._save_state()

        first_time = "（第一次见面）" if meet_count == 0 else f"（第{meet_count + 1}次见面）"

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
    #  /沉星湾帮助
    # ═══════════════════════════════════════

    @filter.command("沉星湾帮助")
    async def town_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🌊 沉星湾 · 指令\n"
            "━" * 22 + "\n"
            "小镇 → 当前状态\n"
            "去 地点 → 移动\n"
            "看看 → 场景描写\n"
            "聊天 名字 → 找NPC说话\n"
            "商店 → 听潮街市集\n"
            "买 商品 → 购买\n"
            "背包 → 查看物品\n"
            "写信 内容 → 邮局寄信\n"
            "明信片 → 寄明信片\n"
            "信箱 → 查看收到的信\n"
            "捡贝壳 → 拾屿海滩\n"
            "钓鱼 → 雾灯港\n"
            "演奏 → 在当前地点演奏\n"
            "敲门 → 克宝小屋\n"
            "日记 → 旅行日记\n"
            "漫游 → 自动逛一次\n"
            "自动漫游 开/关 → AI自己逛\n"
            "新的一天 → 推进到明天\n"
            "━" * 22 + "\n"
            "🗺️ 地点：雾灯港·听潮街·拾屿海滩·落星渡·灯塔·矢车菊花海·克宝小屋"
        )
