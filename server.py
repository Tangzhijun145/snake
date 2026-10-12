# -*- coding: utf-8 -*-
"""联机贪吃蛇服务器：移动、碰撞、吃食、击杀归属、尸体掉落全部由服务端裁决，
浏览器只上报输入并渲染。WebSocket 20Hz 下发全量快照。

运行: python server.py   然后打开 http://localhost:8001
双人同屏：加入页填上"玩家2"昵称即可，玩家1 WASD+空格，玩家2 方向键+回车。
"""
import asyncio
import json
import os
import random
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import websockets

GRID_W, GRID_H = 72, 45
TICK = 1 / 20
MOVE_EVERY = 4
FOOD_COUNT = 10
FOOD_SCORE = 1
GOLD_SCORE = 5
GOLD_INTERVAL = 14.0
RESPAWN_DELAY = 2.0
BOT_COUNT = 3
START_LEN = 4
MIN_BOOST_LEN = 8
BOOST_DRAIN_MOVES = 3
MAX_DEATH_FOOD = 60
MAX_FOOD_TOTAL = FOOD_COUNT + MAX_DEATH_FOOD

PALETTE = ["#22c55e", "#38bdf8", "#f472b6", "#facc15",
           "#a78bfa", "#fb923c", "#4ade80", "#e879f9"]


class Snake:
    def __init__(self, pid, name, color, is_bot=False):
        self.id = pid
        self.name = name
        self.color = color
        self.is_bot = is_bot
        self.score = 0
        self.best = 0
        self.kills = 0
        self.deaths = 0
        self.alive = False
        self.body = deque()
        self.dir = (1, 0)
        self.dir_queue = deque()
        self.boost = False
        self.acc = 0
        self.drain_acc = 0
        self.grow = 0
        self.respawn_at = 0.0
        self.ws = None
        self.slot = 0            # 同一连接内的序号：0=WASD，1=方向键

    def spawn(self, occupied):
        cells = [(0, 0)]
        for _ in range(200):
            x = random.randint(4, GRID_W - 5)
            y = random.randint(4, GRID_H - 5)
            dx, dy = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            cells = [(x - dx * i, y - dy * i) for i in range(START_LEN)]
            if all(0 <= cx < GRID_W and 0 <= cy < GRID_H and (cx, cy) not in occupied
                   for cx, cy in cells):
                break
        self.body = deque(cells)
        self.dir = (cells[0][0] - cells[1][0], cells[0][1] - cells[1][1])
        self.dir_queue.clear()
        self.boost = False
        self.acc = 0
        self.drain_acc = 0
        self.grow = 0
        self.alive = True


class Game:
    def __init__(self):
        self.snakes = {}
        self.food = []
        self.next_id = 1
        self.frame = 0
        self.events = []
        self.last_gold = time.time()
        self.paused = False
        self.paused_at = 0.0
        self.paused_by = None
        for _ in range(FOOD_COUNT):
            self.spawn_food()

    def set_paused(self, on, by_name):
        # 重生与金食物用的是 time.time()，暂停期间照样在走，恢复时要把这些
        # 绝对时间点整体后移，否则一解除暂停就集体复活、金食物立刻刷新。
        on = bool(on)
        if on == self.paused:
            return
        now = time.time()
        if on:
            self.paused_at = now
            self.paused_by = by_name
            for s in self.snakes.values():
                s.dir_queue.clear()
        else:
            dt = now - self.paused_at
            for s in self.snakes.values():
                if s.respawn_at:
                    s.respawn_at += dt
            self.last_gold += dt
            self.paused_by = None
        self.paused = on

    def occupancy(self):
        cells, owner = set(), {}
        for s in self.snakes.values():
            if s.alive:
                for c in s.body:
                    cells.add(c)
                    owner[c] = s.id
        return cells, owner

    def spawn_food(self, kind="normal"):
        if len(self.food) >= MAX_FOOD_TOTAL:
            return
        cells, _ = self.occupancy()
        taken = {(f["x"], f["y"]) for f in self.food}
        for _ in range(200):
            x, y = random.randint(0, GRID_W - 1), random.randint(0, GRID_H - 1)
            if (x, y) not in cells and (x, y) not in taken:
                self.food.append({"x": x, "y": y, "kind": kind})
                return

    def has_food_at(self, cell):
        return any((f["x"], f["y"]) == cell for f in self.food)

    def drop_food(self, cell):
        if len(self.food) >= MAX_FOOD_TOTAL or self.has_food_at(cell):
            return
        self.food.append({"x": cell[0], "y": cell[1], "kind": "normal"})

    def scatter_body(self, s):
        # s 已经置为 alive=False，不会算进占用格
        cells, _ = self.occupancy()
        dropped = 0
        for c in s.body:
            if dropped >= MAX_DEATH_FOOD:
                break
            if c in cells or self.has_food_at(c):
                continue
            self.food.append({"x": c[0], "y": c[1], "kind": "normal"})
            dropped += 1

    def add_player(self, ws, name, slot=0):
        pid = f"p{self.next_id}"
        self.next_id += 1
        color = PALETTE[(self.next_id - 2) % len(PALETTE)]
        s = Snake(pid, name, color)
        s.ws = ws
        s.slot = slot
        s.spawn(self.occupancy()[0])
        self.snakes[pid] = s
        self.events.append({"type": "join", "name": name})
        return s

    def add_bot(self):
        pid = f"b{self.next_id}"
        self.next_id += 1
        n = sum(1 for s in self.snakes.values() if s.is_bot) + 1
        s = Snake(pid, f"AI-Bot-{n}", "#94a3b8", is_bot=True)
        s.spawn(self.occupancy()[0])
        self.snakes[pid] = s
        return s

    def remove_player(self, pid):
        s = self.snakes.pop(pid, None)
        if s:
            self.events.append({"type": "leave", "name": s.name})

    def set_dir(self, s, dx, dy):
        # 队列上限 2，队满时用最新意图替换队尾，避免连按时丢掉最后一次输入
        if not s.alive or (dx, dy) == (0, 0):
            return
        if len(s.dir_queue) >= 2:
            s.dir_queue[-1] = (dx, dy)
        else:
            s.dir_queue.append((dx, dy))

    def set_boost(self, s, on):
        s.boost = bool(on) and s.alive and len(s.body) > MIN_BOOST_LEN

    def free_space(self, start, blocked, cap=220):
        seen = {start}
        stack = [start]
        while stack and len(seen) < cap:
            x, y = stack.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if (0 <= nx < GRID_W and 0 <= ny < GRID_H
                        and (nx, ny) not in blocked and (nx, ny) not in seen):
                    seen.add((nx, ny))
                    stack.append((nx, ny))
        return len(seen)

    def bot_decide(self, s):
        # 洪泛评估安全空间 + 贪心接近食物 + 规避其他蛇头
        cells, _ = self.occupancy()
        head = s.body[0]
        others = [o for o in self.snakes.values() if o.alive and o.id != s.id]

        dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        random.shuffle(dirs)        # 打散，避免多个 Bot 走位完全一致
        best_dir, best_score, best_space = s.dir, -1e18, 0
        need = len(s.body) + 3
        for dx, dy in dirs:
            nx, ny = head[0] + dx, head[1] + dy
            if not (0 <= nx < GRID_W and 0 <= ny < GRID_H) or (nx, ny) in cells:
                continue
            space = self.free_space((nx, ny), cells)
            safety = space * 6 if space >= need else -12000 + space * 40
            dist = min((abs(nx - f["x"]) + abs(ny - f["y"]) for f in self.food), default=0)
            risk = 0
            for o in others:
                d = abs(nx - o.body[0][0]) + abs(ny - o.body[0][1])
                if d < 3:
                    risk += (3 - d) * 40
            score = safety - dist * 12 - risk
            if score > best_score:
                best_score, best_space, best_dir = score, space, (dx, dy)
        self.set_dir(s, *best_dir)

        if self.frame % 10 == 0:
            dmin = min((abs(head[0] - f["x"]) + abs(head[1] - f["y"]) for f in self.food), default=0)
            s.boost = (len(s.body) > MIN_BOOST_LEN + 6 and best_space > 60 and dmin > 10)

    def step(self):
        self.frame += 1
        now = time.time()
        self.events = []
        cells, owner = self.occupancy()

        for s in self.snakes.values():
            if not s.alive and s.respawn_at and now >= s.respawn_at:
                s.spawn(cells)
                cells, owner = self.occupancy()

        if (now - self.last_gold >= GOLD_INTERVAL
                and not any(f["kind"] == "gold" for f in self.food)):
            self.spawn_food(kind="gold")
            self.last_gold = now
            self.events.append({"type": "gold"})

        movers = []
        for s in self.snakes.values():
            if not s.alive:
                continue
            if s.boost and len(s.body) <= MIN_BOOST_LEN:
                s.boost = False
            s.acc += 1
            if s.acc >= (1 if s.boost else MOVE_EVERY):
                s.acc = 0
                movers.append(s)

        for s in movers:
            if s.is_bot:
                self.bot_decide(s)

        deaths = {}
        nxt = {}
        target = {}
        for s in movers:
            if s.dir_queue:
                s.dir = s.dir_queue.popleft()
            hx, hy = s.body[0]
            nx, ny = hx + s.dir[0], hy + s.dir[1]
            nxt[s.id] = (nx, ny)

            if not (0 <= nx < GRID_W and 0 <= ny < GRID_H):
                deaths[s.id] = {"killer": None, "reason": "wall"}
                continue

            if (nx, ny) in cells:
                o = owner.get((nx, ny))
                if o != s.id:                     # 撞自己直接穿过，不死亡
                    deaths[s.id] = {"killer": o, "reason": "snake"}
                continue

            target.setdefault((nx, ny), []).append(s)

        for cell, group in target.items():
            if len(group) > 1:                    # 头对头两败俱伤，不计击杀
                for s in group:
                    deaths[s.id] = {"killer": None, "reason": "head-on"}

        for s in movers:
            if s.id in deaths:
                continue
            nx, ny = nxt[s.id]
            s.body.appendleft((nx, ny))
            got = 0
            for f in self.food:
                if (f["x"], f["y"]) == (nx, ny):
                    self.food.remove(f)
                    got = GOLD_SCORE if f["kind"] == "gold" else FOOD_SCORE
                    kind = f["kind"]
                    break
            else:
                kind = None
            if got:
                s.grow += got - 1     # 蛇头本帧已长出，剩余节数逐帧补齐
                s.score += got
                s.best = max(s.best, s.score)
                self.spawn_food()
                self.events.append({"type": "eat", "name": s.name, "x": nx, "y": ny,
                                    "kind": kind, "score": got})
            elif s.grow > 0:
                s.grow -= 1
            else:
                s.body.pop()

        for s in movers:
            if s.id in deaths or not s.boost:
                continue
            s.drain_acc += 1
            if s.drain_acc >= BOOST_DRAIN_MOVES:
                s.drain_acc = 0
                if len(s.body) > MIN_BOOST_LEN:
                    self.drop_food(s.body.pop())
                    s.score = max(0, s.score - 1)
                else:
                    s.boost = False

        for sid, info in deaths.items():
            s = self.snakes.get(sid)
            if not s or not s.alive:
                continue
            s.alive = False
            s.deaths += 1
            s.boost = False
            s.acc = 0
            s.drain_acc = 0
            s.grow = 0
            s.respawn_at = now + RESPAWN_DELAY
            s.best = max(s.best, s.score)
            self.scatter_body(s)
            s.body.clear()
            killer_id = info["killer"]
            if killer_id:
                k = self.snakes.get(killer_id)
                if k and k.alive:
                    k.kills += 1
                    self.events.append({"type": "kill", "name": k.name, "target": s.name})
            self.events.append({"type": "die", "name": s.name, "score": s.score,
                                "killer": killer_id, "reason": info["reason"]})
            s.score = 0

    def snapshot(self):
        # 暂停时按"暂停那一刻"算，否则倒计时会在暂停期间继续走
        now = self.paused_at if self.paused else time.time()
        return {
            "type": "state",
            "tick": self.frame,
            "grid": [GRID_W, GRID_H],
            "tick_rate": round(1 / TICK, 1),
            "paused": self.paused,
            "paused_by": self.paused_by,
            "players": [
                {
                    "id": s.id, "name": s.name, "color": s.color,
                    "alive": s.alive, "score": s.score, "best": s.best,
                    "kills": s.kills, "deaths": s.deaths,
                    "bot": s.is_bot, "len": len(s.body), "boost": s.boost,
                    "respawn_in": round(max(0.0, s.respawn_at - now), 1) if not s.alive else 0,
                    "body": [list(c) for c in s.body] if s.alive else [],
                }
                for s in self.snakes.values()
            ],
            "food": self.food,
            "events": self.events,
        }


game = Game()


async def ws_handler(ws):
    pids = []                    # 本连接拥有的蛇：单人 1 条，双人同屏 2 条
    try:
        # 首条消息必须是 join；带 name2 则再注册一条蛇
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        name = str(msg.get("name", "玩家"))[:12] if msg.get("type") == "join" else "玩家"
        s1 = game.add_player(ws, name, slot=0)
        pids.append(s1.id)
        if msg.get("type") == "join" and str(msg.get("name2", "")).strip():
            n2 = str(msg.get("name2")).strip()[:12] or "玩家2"
            if n2 == name:
                n2 = name + "2"
            s2 = game.add_player(ws, n2, slot=1)
            pids.append(s2.id)
        await ws.send(json.dumps({"type": "welcome", "id": pids[0], "ids": pids,
                                  "color": s1.color,
                                  "colors": [game.snakes[p].color for p in pids],
                                  "grid": [GRID_W, GRID_H], "tick_rate": round(1 / TICK, 1)}))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            t = msg.get("type")
            if not pids:
                continue
            slot = 1 if msg.get("slot") == 1 and len(pids) > 1 else 0
            cur = game.snakes.get(pids[slot])
            if not cur:
                continue
            if t == "dir":
                if not game.paused:
                    game.set_dir(cur, int(msg.get("dx", 0)), int(msg.get("dy", 0)))
            elif t == "boost":
                game.set_boost(cur, msg.get("on", False))
            elif t == "pause":
                game.set_paused(msg.get("on", False), cur.name)
            elif t == "ping":
                await ws.send(json.dumps({"type": "pong", "t": msg.get("t")}))
    except Exception:
        pass
    finally:
        for pid in pids:
            game.remove_player(pid)


async def game_loop():
    # 按绝对时间对齐睡眠，避免误差逐帧累积；落后过多时重新对齐，不做追赶爆发
    for _ in range(BOT_COUNT):
        game.add_bot()
    next_at = time.perf_counter()
    beat = 0
    while True:
        if game.paused:
            beat += 1
        else:
            game.step()
        # 暂停时不推进逻辑帧，但保留 5Hz 心跳，让新加入的客户端能拿到 paused 状态
        if not game.paused or beat % 4 == 1:
            data = json.dumps(game.snapshot(), separators=(",", ":"))
            conns = {}                # 双人同屏共用一个连接，按连接去重
            for s in game.snakes.values():
                if s.is_bot or not s.ws:
                    continue
                conns.setdefault(id(s.ws), [s.ws, []])[1].append(s.id)
            for ws, pids in conns.values():
                try:
                    await ws.send(data)
                except Exception:
                    for pid in pids:
                        game.remove_player(pid)
        next_at += TICK
        delay = next_at - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            next_at = time.perf_counter()


def raise_timer_resolution():
    # Windows 默认定时器精度约 15.6ms，不提升的话 20Hz 实际只有 ~16Hz
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


async def main():
    raise_timer_resolution()
    port_ws, port_http = 8765, 8001
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port_http), SimpleHTTPRequestHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async with websockets.serve(ws_handler, "0.0.0.0", port_ws, ping_interval=None):
        print("贪吃蛇云对战服务器已启动")
        print(f"  游戏页面: http://localhost:{port_http}")
        print(f"  WebSocket: ws://localhost:{port_ws}")
        print(f"  逻辑帧率: {round(1 / TICK, 1)}Hz   棋盘: {GRID_W}x{GRID_H}   AI Bot: {BOT_COUNT}")
        await game_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n服务器已停止")